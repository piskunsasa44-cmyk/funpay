"""
BR Price Monitor v2 — отдельный процесс мониторинга цен виртов Black Russia на FunPay.

Логика: раз в POLL_INTERVAL_SECONDS секунд парсит страницу валюты Black Russia
на FunPay (chips/186), фильтрует серверы по SERVER_RANGE, и шлёт алерт в Telegram,
если цена за 1kk <= PRICE_THRESHOLD_RUB.

ИЗМЕНЕНО в v2 относительно первой версии:
1. FUNPAY_GOLDEN_KEY теперь ОБЯЗАТЕЛЕН. Без авторизованной сессии страница может
   отдавать цены в другой валюте (например $) — тогда любое маленькое число
   проходит порог "<=26", и бот шлёт почти все лоты подряд. Именно это было
   причиной "рандомных" алертов в первой версии.
2. Добавлена санити-проверка: перед тем как слать алерты, скрипт считает
   медианную цену по всем найденным лотам. Если она неправдоплодобно низкая
   (ниже SANITY_MIN_PRICE) — это явный признак, что валюта/парсинг сломаны,
   и скрипт НЕ шлёт алерты в этом цикле, а пишет понятную ошибку в лог.
3. Добавлен DEBUG_ROWS — печатает в лог сырой текст строк таблицы для ручной
   проверки разметки, если что-то снова пойдёт не так.
"""

import json
import logging
import os
import re
import statistics
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import requests
from bs4 import BeautifulSoup

# ───────────────────────── КОНФИГ ─────────────────────────

FUNPAY_URL = "https://funpay.com/chips/186/"

PRICE_THRESHOLD_RUB = float(os.getenv("PRICE_THRESHOLD_RUB", "26"))

SERVER_RANGE = set(range(1, 31))

POLL_INTERVAL_SECONDS = int(os.getenv("POLL_INTERVAL_SECONDS", "180"))

TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

# ОБЯЗАТЕЛЬНО. Без него цены могут парситься не в рублях.
FUNPAY_GOLDEN_KEY = os.environ.get("FUNPAY_GOLDEN_KEY", "")

REALERT_COOLDOWN_SECONDS = int(os.getenv("REALERT_COOLDOWN_SECONDS", "3600"))

# Если медианная цена по всем найденным лотам ниже этого значения — считаем,
# что валюта/парсинг сломаны, и не шлём алерты в этом цикле.
# Реальные вирты BR на FunPay не торгуются по копейкам — если медиана меньше
# этого числа, что-то не так с парсингом, а не с рынком.
SANITY_MIN_PRICE_RUB = float(os.getenv("SANITY_MIN_PRICE_RUB", "5"))

DEBUG_ROWS = int(os.getenv("DEBUG_ROWS", "0"))

STATE_FILE = Path(__file__).parent / "seen_lots.json"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("br_price_monitor")

if not FUNPAY_GOLDEN_KEY:
    log.error(
        "FUNPAY_GOLDEN_KEY не задан. Без авторизованной сессии цены на странице "
        "могут показываться не в рублях, и порог сравнения будет работать неверно "
        "(это и было причиной 'случайных' алертов). Смотри README, раздел "
        "'Как достать FUNPAY_GOLDEN_KEY'. Останавливаюсь, чтобы не спамить мусором."
    )
    sys.exit(1)


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
    cookies = {"golden_key": FUNPAY_GOLDEN_KEY}

    resp = requests.get(FUNPAY_URL, headers=headers, cookies=cookies, timeout=15)
    resp.raise_for_status()
    return resp.text


def parse_number(text: str) -> float:
    cleaned = text.replace("\xa0", " ").replace(" ", "").replace(",", ".")
    cleaned = re.sub(r"[^\d.]", "", cleaned)
    if not cleaned:
        raise ValueError(f"no digits in {text!r}")
    return float(cleaned)


def extract_server_number(server_name: str) -> int | None:
    match = re.search(r"№\s*(\d+)", server_name)
    return int(match.group(1)) if match else None


def dump_raw_row(row, index: int) -> None:
    full_text = row.get_text(" | ", strip=True)
    log.info("RAW ROW #%d: %s", index, full_text)
    for cls in ("tc-server", "tc-desc", "tc-user", "tc-amount", "tc-price"):
        el = row.select_one(f".{cls}")
        log.info("  .%s -> %r", cls, el.get_text(" ", strip=True) if el else "NOT FOUND")


def parse_lots(html: str) -> list[Lot]:
    soup = BeautifulSoup(html, "html.parser")
    rows = soup.select("a.tc-item")

    if DEBUG_ROWS > 0:
        for i, row in enumerate(rows[:DEBUG_ROWS]):
            dump_raw_row(row, i)

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


def prices_look_sane(lots: list[Lot]) -> bool:
    """Если медианная цена подозрительно низкая — валюта/парсинг сломаны."""
    prices = [lot.price_per_kk for lot in lots if lot.price_per_kk > 0]
    if not prices:
        return True
    median_price = statistics.median(prices)
    if median_price < SANITY_MIN_PRICE_RUB:
        log.error(
            "Медианная цена по %d лотам = %.3f₽ — это нереально дёшево для виртов BR. "
            "Похоже, цены не в рублях (нет авторизации?) или сломаны селекторы. "
            "Алерты в этом цикле ОТКЛЮЧЕНЫ. Включи DEBUG_ROWS=5 и проверь логи.",
            len(prices),
            median_price,
        )
        return False
    return True


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


def send_telegram_text(text: str) -> None:
    api_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        requests.post(api_url, json={"chat_id": TELEGRAM_CHAT_ID, "text": text}, timeout=10)
    except requests.RequestException as exc:
        log.error("Не удалось отправить служебное сообщение в Telegram: %s", exc)


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

    if not lots:
        return state

    if not prices_look_sane(lots):
        return state

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
        "Старт мониторинга v2. Порог=%.0f₽/1kk, серверы=%s, интервал=%dс, golden_key=%s",
        PRICE_THRESHOLD_RUB,
        sorted(SERVER_RANGE) if SERVER_RANGE else "все",
        POLL_INTERVAL_SECONDS,
        "задан" if FUNPAY_GOLDEN_KEY else "НЕ ЗАДАН",
    )
    state = load_state()
    first_run = True

    while True:
        try:
            state = run_once(state)
            save_state(state)
            if first_run:
                send_telegram_text("BR Price Monitor запущен и работает.")
                first_run = False
        except requests.RequestException as exc:
            log.error("Сетевая ошибка при запросе FunPay: %s", exc)
        except Exception as exc:  # noqa: BLE001
            log.exception("Непредвиденная ошибка в цикле мониторинга: %s", exc)

        time.sleep(POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
