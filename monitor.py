"""
BR Price Monitor v3 — мониторинг цен виртов Black Russia на FunPay + управление из Telegram.

Новое в v3 относительно v2:
- Бот слушает команды в Telegram (long polling, без вебхуков, тот же процесс):
    /scan              — запустить проверку прямо сейчас, не дожидаясь интервала
    /history [N]       — последние N алертов (по умолчанию 10, максимум 50)
    /status            — текущий порог, серверы, когда была последняя проверка
    /setthreshold <X>  — поменять порог цены на лету, без передеплоя
    /help              — список команд
- Алерты по лотам дешевле EXTRA_CHEAP_THRESHOLD_RUB (по умолчанию 19₽) получают
  заголовок "❗❗❗ ОЧЕНЬ ДЁШЕВО ❗❗❗" вместо обычного.
- Появилась history.json — лог всех отправленных алертов для команды /history.
  ВАЖНО: если у сервиса в Railway нет подключённого Volume, история и антиспам-
  состояние (seen_lots.json) сбрасываются при каждом редеплое/перезапуске —
  это файлы на диске контейнера, а не во внешнем хранилище.

Команды принимаются только из чата с TELEGRAM_CHAT_ID — сообщения из любых
других чатов игнорируются и логируются как попытка чужого доступа.
"""

import json
import logging
import os
import re
import statistics
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from bs4 import BeautifulSoup

# ───────────────────────── КОНФИГ ─────────────────────────

FUNPAY_URL = "https://funpay.com/chips/186/"

PRICE_THRESHOLD_RUB = float(os.getenv("PRICE_THRESHOLD_RUB", "26"))
EXTRA_CHEAP_THRESHOLD_RUB = float(os.getenv("EXTRA_CHEAP_THRESHOLD_RUB", "19"))

# None = отслеживать ВСЕ серверы Black Russia, без ограничения по номеру.
SERVER_RANGE: set[int] | None = None

POLL_INTERVAL_SECONDS = int(os.getenv("POLL_INTERVAL_SECONDS", "120"))
TELEGRAM_POLL_TIMEOUT = int(os.getenv("TELEGRAM_POLL_TIMEOUT", "10"))

TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]

# Можно указать несколько ID через запятую: "111111,222222" — все получат
# алерты, и команды (/scan, /top и т.д.) будут приниматься от любого из них.
TELEGRAM_CHAT_IDS = [
    chat_id.strip() for chat_id in os.environ["TELEGRAM_CHAT_ID"].split(",") if chat_id.strip()
]

FUNPAY_GOLDEN_KEY = os.environ.get("FUNPAY_GOLDEN_KEY", "")

REALERT_COOLDOWN_SECONDS = int(os.getenv("REALERT_COOLDOWN_SECONDS", "3600"))
SANITY_MIN_PRICE_RUB = float(os.getenv("SANITY_MIN_PRICE_RUB", "5"))
HISTORY_MAX_ENTRIES = int(os.getenv("HISTORY_MAX_ENTRIES", "300"))
DEBUG_ROWS = int(os.getenv("DEBUG_ROWS", "0"))

STATE_FILE = Path(__file__).parent / "seen_lots.json"
HISTORY_FILE = Path(__file__).parent / "history.json"

MOSCOW_TZ = timezone(timedelta(hours=3))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("br_price_monitor")

if not FUNPAY_GOLDEN_KEY:
    log.error(
        "FUNPAY_GOLDEN_KEY не задан. Без авторизованной сессии цены могут "
        "парситься не в рублях. Смотри README. Останавливаюсь."
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


def fmt_ts(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=MOSCOW_TZ).strftime("%d.%m %H:%M")


# ───────────────────────── ПАРСИНГ FUNPAY ─────────────────────────

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
    prices = [lot.price_per_kk for lot in lots if lot.price_per_kk > 0]
    if not prices:
        return True
    median_price = statistics.median(prices)
    if median_price < SANITY_MIN_PRICE_RUB:
        log.error(
            "Медианная цена по %d лотам = %.3f₽ — нереально дёшево. "
            "Похоже, golden_key протух или сломались селекторы. Алерты отключены в этом цикле.",
            len(prices),
            median_price,
        )
        return False
    return True


# ───────────────────────── TELEGRAM: ОТПРАВКА ─────────────────────────

def send_telegram_text(text: str) -> None:
    api_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    for chat_id in TELEGRAM_CHAT_IDS:
        try:
            resp = requests.post(
                api_url,
                json={"chat_id": chat_id, "text": text, "disable_web_page_preview": True},
                timeout=10,
            )
            resp.raise_for_status()
        except requests.RequestException as exc:
            log.error("Не удалось отправить сообщение в Telegram (chat_id=%s): %s", chat_id, exc)


def send_telegram_alert(lot: Lot) -> None:
    extra_cheap = lot.price_per_kk <= EXTRA_CHEAP_THRESHOLD_RUB
    header = "❗❗❗ ОЧЕНЬ ДЁШЕВО ❗❗❗" if extra_cheap else "🟢 Цена ниже порога"

    text = (
        f"{header}\n\n"
        f"Сервер: {lot.server_name}\n"
        f"Цена: {lot.price_per_kk}₽/1kk\n"
        f"В наличии: {lot.in_stock_kk}kk\n"
        f"Продавец: {lot.seller}\n"
    )
    if lot.url:
        text += f"\n{lot.url}"
    send_telegram_text(text)


# ───────────────────────── TELEGRAM: ПРИЁМ КОМАНД ─────────────────────────

def get_updates(offset: int) -> tuple[list[dict], int]:
    api_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates"
    params = {"timeout": TELEGRAM_POLL_TIMEOUT, "offset": offset}
    resp = requests.get(api_url, params=params, timeout=TELEGRAM_POLL_TIMEOUT + 10)
    resp.raise_for_status()
    data = resp.json()
    if not data.get("ok"):
        return [], offset

    updates = data.get("result", [])
    new_offset = offset
    for update in updates:
        new_offset = max(new_offset, update["update_id"] + 1)
    return updates, new_offset


def send_help() -> None:
    send_telegram_text(
        "Команды:\n"
        "/scan — проверить цены прямо сейчас\n"
        "/top [N] — N самых дёшевых лотов сейчас, по всем серверам, без привязки к порогу (по умолчанию 10)\n"
        "/history [N] — последние N алертов (по умолчанию 10, максимум 50)\n"
        "/status — текущий порог, пауза/работа, статистика последней проверки\n"
        "/setthreshold <число> — изменить порог цены на лету\n"
        "/pause — остановить автопроверку (можно всё равно /scan вручную)\n"
        "/resume — снова включить автопроверку\n"
        "/help — это сообщение"
    )


def send_status(runtime: dict) -> None:
    last_scan_ts = runtime["last_scan_ts"]
    last_scan_str = fmt_ts(last_scan_ts) if last_scan_ts else "ещё не было"
    state_str = "⏸ на паузе" if runtime["paused"] else "▶️ работает"
    text = (
        f"Статус: {state_str}\n"
        f"Порог алерта: {runtime['threshold']}₽/1kk\n"
        f"Порог '❗очень дёшево': {EXTRA_CHEAP_THRESHOLD_RUB}₽/1kk\n"
        f"Серверы: все\n"
        f"Интервал проверки: {POLL_INTERVAL_SECONDS}с\n"
        f"Последняя проверка: {last_scan_str}\n"
        f"Лотов найдено: {runtime['last_found']}, ниже порога: {runtime['last_matched']}"
    )
    send_telegram_text(text)


def send_top(n: int) -> None:
    send_telegram_text("⏳ Смотрю текущие цены по всем серверам...")
    try:
        html = fetch_html()
        lots = parse_lots(html)
    except requests.RequestException:
        send_telegram_text("Не получилось дотянуться до FunPay, попробуй чуть позже.")
        return

    if not lots:
        send_telegram_text("Лотов не нашлось — возможно, страница не отдала данные.")
        return
    if not prices_look_sane(lots):
        send_telegram_text("Цены сейчас выглядят неадекватно (похоже на проблему с golden_key) — пропускаю.")
        return

    cheapest = sorted(lots, key=lambda l: l.price_per_kk)[:n]
    lines = [f"Топ-{len(cheapest)} самых дёшевых лотов сейчас (из {len(lots)} найденных):\n"]
    for lot in cheapest:
        mark = " ❗" if lot.price_per_kk <= EXTRA_CHEAP_THRESHOLD_RUB else ""
        lines.append(f"{lot.server_name} — {lot.price_per_kk}₽/1kk ({lot.seller}){mark}")
    send_telegram_text("\n".join(lines))


def send_history(history: list[dict], n: int) -> None:
    if not history:
        send_telegram_text("Алертов пока не было.")
        return
    recent = history[-n:][::-1]
    lines = [f"Последние {len(recent)} алертов:\n"]
    for entry in recent:
        lines.append(
            f"{fmt_ts(entry['ts'])} — {entry['server']} — "
            f"{entry['price']}₽/1kk ({entry['seller']})"
        )
    send_telegram_text("\n".join(lines))


def process_update(update: dict, runtime: dict, history: list[dict]) -> None:
    message = update.get("message")
    if not message:
        return

    chat_id = str(message.get("chat", {}).get("id", ""))
    if chat_id not in TELEGRAM_CHAT_IDS:
        log.warning("Команда от чужого chat_id=%s, игнорирую: %r", chat_id, message.get("text"))
        return

    text = (message.get("text") or "").strip()
    if not text.startswith("/"):
        return

    parts = text.split()
    cmd = parts[0].lower().split("@")[0]

    if cmd in ("/scan", "/search", "/check"):
        runtime["force_scan"] = True
        send_telegram_text("🔍 Запускаю проверку вне расписания...")
    elif cmd == "/top":
        n = 10
        if len(parts) > 1 and parts[1].isdigit():
            n = min(int(parts[1]), 30)
        send_top(n)
    elif cmd == "/history":
        n = 10
        if len(parts) > 1 and parts[1].isdigit():
            n = min(int(parts[1]), 50)
        send_history(history, n)
    elif cmd == "/status":
        send_status(runtime)
    elif cmd == "/setthreshold":
        if len(parts) > 1:
            try:
                new_value = float(parts[1].replace(",", "."))
                runtime["threshold"] = new_value
                send_telegram_text(f"Порог обновлён: {new_value}₽/1kk")
            except ValueError:
                send_telegram_text("Не понял число. Пример: /setthreshold 22")
        else:
            send_telegram_text(
                f"Текущий порог: {runtime['threshold']}₽/1kk. Чтобы изменить: /setthreshold 22"
            )
    elif cmd == "/pause":
        runtime["paused"] = True
        send_telegram_text("⏸ Автопроверка на паузе. /scan всё ещё работает вручную, /resume — включить обратно.")
    elif cmd == "/resume":
        runtime["paused"] = False
        send_telegram_text("▶️ Автопроверка снова включена.")
    elif cmd in ("/help", "/start"):
        send_help()
    else:
        send_telegram_text("Не знаю такую команду. /help — список команд.")


# ───────────────────────── СОСТОЯНИЕ ─────────────────────────

def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except json.JSONDecodeError:
            log.warning("seen_lots.json повреждён, начинаю с пустого состояния")
    return {}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2))


def load_history() -> list[dict]:
    if HISTORY_FILE.exists():
        try:
            return json.loads(HISTORY_FILE.read_text())
        except json.JSONDecodeError:
            log.warning("history.json повреждён, начинаю с пустой истории")
    return []


def save_history(history: list[dict]) -> None:
    HISTORY_FILE.write_text(json.dumps(history, ensure_ascii=False, indent=2))


def should_alert(state: dict, lot: Lot, now: float) -> bool:
    record = state.get(lot.lot_id)
    if record is None:
        return True
    price_dropped = lot.price_per_kk < record["price"]
    cooldown_passed = (now - record["ts"]) > REALERT_COOLDOWN_SECONDS
    return price_dropped or cooldown_passed


# ───────────────────────── СКАНИРОВАНИЕ ─────────────────────────

def run_once(state: dict, history: list[dict], threshold: float) -> tuple[dict, list[dict], dict]:
    html = fetch_html()
    lots = parse_lots(html)
    found = len(lots)

    if not lots:
        return state, history, {"found": 0, "matched": 0}

    if not prices_look_sane(lots):
        return state, history, {"found": found, "matched": 0}

    now = time.time()
    matched = 0

    for lot in lots:
        if SERVER_RANGE is not None and lot.server_number not in SERVER_RANGE:
            continue
        if lot.price_per_kk > threshold:
            continue

        matched += 1
        if should_alert(state, lot, now):
            log.info("АЛЕРТ: %s — %.2f₽/1kk (%s)", lot.server_name, lot.price_per_kk, lot.seller)
            send_telegram_alert(lot)
            state[lot.lot_id] = {"price": lot.price_per_kk, "ts": now}
            history.append(
                {
                    "ts": now,
                    "server": lot.server_name,
                    "price": lot.price_per_kk,
                    "seller": lot.seller,
                    "url": lot.url,
                }
            )

    if len(history) > HISTORY_MAX_ENTRIES:
        history = history[-HISTORY_MAX_ENTRIES:]

    return state, history, {"found": found, "matched": matched}


# ───────────────────────── ОСНОВНОЙ ЦИКЛ ─────────────────────────

def main() -> None:
    log.info(
        "Старт мониторинга v3. Порог=%.0f₽/1kk, ❗порог=%.0f₽, интервал=%dс, golden_key=%s",
        PRICE_THRESHOLD_RUB,
        EXTRA_CHEAP_THRESHOLD_RUB,
        POLL_INTERVAL_SECONDS,
        "задан" if FUNPAY_GOLDEN_KEY else "НЕ ЗАДАН",
    )

    state = load_state()
    history = load_history()
    runtime = {
        "threshold": PRICE_THRESHOLD_RUB,
        "force_scan": False,
        "paused": False,
        "last_scan_ts": 0.0,
        "last_found": 0,
        "last_matched": 0,
    }
    offset = 0

    send_telegram_text(
        "BR Price Monitor запущен и работает.\n"
        "Команды: /scan /top /history /status /setthreshold /pause /resume /help"
    )

    while True:
        try:
            updates, offset = get_updates(offset)
            for update in updates:
                process_update(update, runtime, history)
        except requests.RequestException as exc:
            log.error("Ошибка получения команд из Telegram: %s", exc)
            time.sleep(5)

        now = time.time()
        due = (not runtime["paused"]) and (now - runtime["last_scan_ts"]) >= POLL_INTERVAL_SECONDS
        if runtime["force_scan"] or due:
            try:
                state, history, stats = run_once(state, history, runtime["threshold"])
                save_state(state)
                save_history(history)
                runtime["last_scan_ts"] = now
                runtime["last_found"] = stats["found"]
                runtime["last_matched"] = stats["matched"]
                log.info("Распарсено лотов: %d", stats["found"])
                log.info("Лотов ниже порога %.0f₽: %d", runtime["threshold"], stats["matched"])
            except requests.RequestException as exc:
                log.error("Сетевая ошибка при запросе FunPay: %s", exc)
            except Exception:
                log.exception("Непредвиденная ошибка в цикле мониторинга")
            finally:
                runtime["force_scan"] = False


if __name__ == "__main__":
    main()
