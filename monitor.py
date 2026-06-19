"""
BR Price Monitor — отдельный процесс мониторинга цен виртов Black Russia на FunPay.

Логика: раз в POLL_INTERVAL_SECONDS секунд парсит страницу валюты Black Russia
на FunPay (chips/186), фильтрует серверы по SERVER_RANGE, и шлёт алерт в Telegram,
если цена за 1kk на каком-либо лоте <= PRICE_THRESHOLD.

Никак не связан с основным Telegram-ботом на Railway — отдельный процесс,
свой requirements.txt, свой деплой.
"""

import json
import logging
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path

import requests
from bs4 import BeautifulSoup

# ───────────────────────── КОНФИГ ─────────────────────────

# Страница валюты Black Russia на FunPay (категория "Money" / "Валюта").
# Если у тебя сайт показывает цену не в рублях — открой funpay.com в браузере,
# зайди в свой аккаунт (RU-локаль) и скопируй cookie golden_key — см. FUNPAY_GOLDEN_KEY ниже.
FUNPAY_URL = "https://funpay.com/chips/186/"

# Порог: алертим, если цена за 1kk МЕНЬШЕ ИЛИ РАВНА этому значению.
PRICE_THRESHOLD_RUB = float(os.getenv("PRICE_THRESHOLD_RUB", "26"))

# Какие серверы интересуют — список номеров. None = все.
# Пример: {1,2,3,4,5,6,7,8,9,10,...,30}
SERVER_RANGE = set(range(1, 31))

# Как часто опрашивать страницу (секунды). Не ставь меньше 60 — банит IP/Cloudflare.
POLL_INTERVAL_SECONDS = int(os.getenv("POLL_INTERVAL_SECONDS", "180"))

# Telegram
TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

# Опционально: cookie авторизованной сессии FunPay (golden_key), если страница
# без авторизации отдаёт цены в $/€ вместо рублей, или отдаёт неполный список.
FUNPAY_GOLDEN_KEY = os.getenv("FUNPAY_GOLDEN_KEY", "")

# Не алертить повторно по тому же лоту, если цена не изменилась, чаще чем раз в N секунд.
REALERT_COOLDOWN_SECONDS = int(os.getenv("REALERT_COOLDOWN_SECONDS", "3600"))

STATE_FILE = Path(__file__).parent / "seen_lots.json"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("br_price_monitor")


@dataclass
class Lot:
    lot_id: str
    server_name: str
    server_number: int | None
    seller: str
    in_stock_kk: float
    price_per_kk: float
    url: str


# ───────────────────────── ПАРСИНГ ─────────────────────────

def fetch_html() -> str:
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept-Language": "ru-RU,ru;q=0.9",
    }
    cookies = {}
    if FUNPAY_GOLDEN_KEY:
        cookies["golden_key"] = FUNPAY_GOLDEN_KEY

    resp = requests.get(FUNPAY_URL, headers=headers, cookies=cookies, timeout=15)
    resp.raise_for_status()
    return resp.text


def parse_number(text: str) -> float:
    """'1 234,5' / '1234.5' / '1 234' -> float. Бросает ValueError на мусоре."""
    cleaned = text.replace("\xa0", " ").replace(" ", "").replace(",", ".")
    cleaned = re.sub(r"[^\d.]", "", cleaned)
    if not cleaned:
        raise ValueError(f"no digits in {text!r}")
    return float(cleaned)


def extract_server_number(server_name: str) -> int | None:
    match = re.search(r"№\s*(\d+)", server_name)
    return int(match.group(1)) if match else None


def parse_lots(html: str) -> list[Lot]:
    """
    Селекторы ниже — стандартная разметка таблиц лотов FunPay (tc-item / tc-server /
    tc-desc / tc-user / tc-amount / tc-price). Разметка площадки может поменяться —
    если парсер вернёт 0 лотов при живом рынке, открой страницу в браузере,
    F12 -> Elements, и подправь селекторы под актуальную верстку.
    """
    soup = BeautifulSoup(html, "html.parser")
    rows = soup.select("a.tc-item")

    lots: list[Lot] = []
    for row in rows:
        try:
            server_el = row.select_one(".tc-server")
            seller_el = row.select_one(".tc-user .media-user-name") or row.select_one(".tc-user")
            amount_el = row.select_one(".tc-amount")
            price_el = row.select_one(".tc-price")

            if not (server_el and amount_el and price_el):
                continue

            server_name = server_el.get_text(strip=True)
            seller = seller_el.get_text(strip=True) if seller_el else "unknown"
            in_stock_kk = parse_number(amount_el.get_text(strip=True))
            price_per_kk = parse_number(price_el.get_text(strip=True))
            lot_id = row.get("data-href") or row.get("href") or f"{server_name}|{seller}|{price_per_kk}"
            url = row.get("href", "")
            if url and url.startswith("/"):
                url = "https://funpay.com" + url

            lots.append(
                Lot(
                    lot_id=lot_id,
                    server_name=server_name,
                    server_number=extract_server_number(server_name),
                    seller=seller,
                    in_stock_kk=in_stock_kk,
                    price_per_kk=price_per_kk,
                    url=url,
                )
            )
        except (ValueError, AttributeError) as exc:
            log.debug("Пропустил строку при парсинге: %s", exc)
            continue

    return lots


# ───────────────────────── TELEGRAM ─────────────────────────

def send_telegram_alert(lot: Lot) -> None:
    text = (
        f"🟢 Цена ниже порога ({PRICE_THRESHOLD_RUB}₽/1kk)\n\n"
        f"Сервер: {lot.server_name}\n"
        f"Цена: {lot.price_per_kk}₽/1kk\n"
        f"В наличии: {lot.in_stock_kk}kk\n"
        f"Продавец: {lot.seller}\n"
    )
    if lot.url:
        text += f"\n{lot.url}"

    api_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        resp = requests.post(
            api_url,
            json={"chat_id": TELEGRAM_CHAT_ID, "text": text, "disable_web_page_preview": True},
            timeout=10,
        )
        resp.raise_for_status()
    except requests.RequestException as exc:
        log.error("Не удалось отправить алерт в Telegram: %s", exc)


# ───────────────────────── СОСТОЯНИЕ (антиспам) ─────────────────────────

def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except json.JSONDecodeError:
            log.warning("seen_lots.json повреждён, начинаю с пустого состояния")
    return {}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2))


def should_alert(state: dict, lot: Lot, now: float) -> bool:
    record = state.get(lot.lot_id)
    if record is None:
        return True
    price_dropped = lot.price_per_kk < record["price"]
    cooldown_passed = (now - record["ts"]) > REALERT_COOLDOWN_SECONDS
    return price_dropped or cooldown_passed


# ───────────────────────── ОСНОВНОЙ ЦИКЛ ─────────────────────────

def run_once(state: dict) -> dict:
    html = fetch_html()
    lots = parse_lots(html)
    log.info("Распарсено лотов: %d", len(lots))

    now = time.time()
    matched = 0

    for lot in lots:
        if SERVER_RANGE is not None and lot.server_number not in SERVER_RANGE:
            continue
        if lot.price_per_kk > PRICE_THRESHOLD_RUB:
            continue

        matched += 1
        if should_alert(state, lot, now):
            log.info("АЛЕРТ: %s — %.2f₽/1kk (%s)", lot.server_name, lot.price_per_kk, lot.seller)
            send_telegram_alert(lot)
            state[lot.lot_id] = {"price": lot.price_per_kk, "ts": now}

    log.info("Лотов ниже порога %.0f₽: %d", PRICE_THRESHOLD_RUB, matched)
    return state


def main() -> None:
    log.info(
        "Старт мониторинга. Порог=%.0f₽/1kk, серверы=%s, интервал=%dс",
        PRICE_THRESHOLD_RUB,
        sorted(SERVER_RANGE) if SERVER_RANGE else "все",
        POLL_INTERVAL_SECONDS,
    )
    state = load_state()

    while True:
        try:
            state = run_once(state)
            save_state(state)
        except requests.RequestException as exc:
            log.error("Сетевая ошибка при запросе FunPay: %s", exc)
        except Exception as exc:  # noqa: BLE001 — не даём процессу упасть насовсем
            log.exception("Непредвиденная ошибка в цикле мониторинга: %s", exc)

        time.sleep(POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
