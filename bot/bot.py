"""
SiteGuard Bot — Telegram-бот для анализа сайтов на уязвимости
═══════════════════════════════════════════════════════════════════════════════

ЧТО ДЕЛАЕТ:
    Пользователь присылает боту адрес сайта (например, example.com), бот
    запускает набор БЕЗОПАСНЫХ проверок (SSL, заголовки безопасности, открытые
    файлы, cookies, защита почты и др.) и присылает понятный отчёт с оценкой и
    рекомендациями «как починить». Идеально для аудита своих сайтов и сайтов
    клиентов (с их разрешения).

КАК УСТРОЕНО (для новичка):
    bot.py      — этот файл: общение с Telegram (приём команд, отправка ответов)
    scanner.py  — «движок»: сами проверки сайта
    report.py   — оформление результата в красивый текст

ЗАПУСК:
    1. Создайте бота у @BotFather, получите токен.
    2. Задайте переменную окружения TELEGRAM_BOT_TOKEN=<ваш токен>
    3. python bot/bot.py
    (На Railway всё это уже настроено через Procfile.)

ВАЖНО ПРО ЗАКОН И ЭТИКУ:
    Сканируйте только свои сайты или сайты, на которые у вас есть письменное
    разрешение владельца. Бот делает только пассивные/лёгкие проверки и не
    наносит вреда, но запускать сканирование чужих ресурсов без согласия —
    незаконно.
"""

import os
import sys
import json
import time
import logging
import threading
from pathlib import Path

import requests

from scanner import scan
from report import build_report

# ─── ЛОГИ ──────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)

# ─── КОНФИГУРАЦИЯ (из переменных окружения) ────────────────────────────────────
TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
ADMIN_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
API_URL = f"https://api.telegram.org/bot{TOKEN}"

# Где храним данные (кто принял условия, история сканов)
DATA_DIR = Path("data")
DATA_DIR.mkdir(exist_ok=True)
USERS_FILE = DATA_DIR / "users.json"
SCANS_FILE = DATA_DIR / "scans.jsonl"

# Защита от перегрузки: сколько одновременных сканов разрешаем всего,
# и как часто один пользователь может запускать скан.
MAX_CONCURRENT_SCANS = 3
USER_COOLDOWN = 20  # секунд между сканами одного пользователя

_scan_semaphore = threading.Semaphore(MAX_CONCURRENT_SCANS)
_last_scan_at: dict[str, float] = {}   # chat_id -> время последнего скана
_busy_users: set[str] = set()          # кто прямо сейчас сканирует

BOT_USERNAME = ""


# ─── ХРАНИЛИЩЕ ПОЛЬЗОВАТЕЛЕЙ ───────────────────────────────────────────────────

def load_users() -> dict:
    if USERS_FILE.exists():
        try:
            return json.loads(USERS_FILE.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def save_users(users: dict) -> None:
    USERS_FILE.write_text(json.dumps(users, ensure_ascii=False, indent=2),
                          encoding="utf-8")


def get_user(chat_id: str) -> dict:
    users = load_users()
    return users.get(str(chat_id), {})


def set_user(chat_id: str, **fields) -> None:
    users = load_users()
    u = users.get(str(chat_id), {})
    u.update(fields)
    users[str(chat_id)] = u
    save_users(users)


def has_accepted(chat_id: str) -> bool:
    return bool(get_user(chat_id).get("accepted"))


def log_scan(chat_id: str, result: dict) -> None:
    """Сохраняем краткую запись о скане (для статистики)."""
    try:
        rec = {
            "chat_id": str(chat_id),
            "host": result.get("host", ""),
            "grade": result.get("grade", ""),
            "score": result.get("score", ""),
            "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        with open(SCANS_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception as e:
        logger.warning("log_scan: %s", e)


# ─── НИЗКОУРОВНЕВОЕ ОБЩЕНИЕ С TELEGRAM ─────────────────────────────────────────

def api(method: str, payload: dict | None = None, _timeout: int = 30) -> dict:
    """Вызвать метод Telegram Bot API. Возвращает разобранный JSON."""
    if not TOKEN:
        return {}
    try:
        r = requests.post(f"{API_URL}/{method}", json=payload or {},
                          timeout=_timeout)
        return r.json()
    except Exception as e:
        logger.warning("api %s: %s", method, e)
        return {}


def send(chat_id, text: str, buttons=None) -> dict:
    """Отправить сообщение (HTML). buttons — список рядов inline-кнопок."""
    payload = {
        "chat_id": str(chat_id),
        "text": text[:4096],
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    if buttons:
        payload["reply_markup"] = {"inline_keyboard": buttons}
    res = api("sendMessage", payload)
    if not res.get("ok"):
        logger.warning("send → %s: %s", chat_id, res.get("description", ""))
    return res


def edit(chat_id, message_id, text: str, buttons=None) -> dict:
    """Изменить ранее отправленное сообщение (для «живого» прогресса)."""
    payload = {
        "chat_id": str(chat_id),
        "message_id": message_id,
        "text": text[:4096],
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    if buttons:
        payload["reply_markup"] = {"inline_keyboard": buttons}
    return api("editMessageText", payload)


def answer_cb(cb_id: str, text: str = "") -> None:
    api("answerCallbackQuery", {"callback_query_id": cb_id, "text": text})


# ─── ТЕКСТЫ И КНОПКИ ───────────────────────────────────────────────────────────

DISCLAIMER = (
    "🛡 <b>SiteGuard — анализ сайтов на уязвимости</b>\n"
    "━━━━━━━━━━━━━━━━━━━━━━━━\n"
    "Я проверяю сайты на типовые проблемы безопасности и присылаю понятный "
    "отчёт с рекомендациями.\n\n"
    "⚠️ <b>Важное условие использования:</b>\n"
    "Запускайте проверку только для сайтов, которыми вы <b>владеете</b>, или на "
    "которые у вас есть <b>разрешение</b> владельца. Сканирование чужих сайтов "
    "без согласия может быть незаконным.\n\n"
    "Все проверки безопасные — они не ломают сайт и не меняют на нём данные.\n\n"
    "Нажимая «Принимаю», вы подтверждаете, что будете соблюдать это условие."
)

WELCOME = (
    "🛡 <b>Готово! Можно сканировать.</b>\n"
    "━━━━━━━━━━━━━━━━━━━━━━━━\n"
    "Просто пришлите мне адрес сайта, например:\n"
    "<code>example.com</code>\n"
    "или <code>https://example.com</code>\n\n"
    "Я проверю:\n"
    "🔐 SSL-сертификат и HTTPS\n"
    "📋 Заголовки безопасности (HSTS, CSP, X-Frame-Options…)\n"
    "🍪 Флаги cookies (Secure, HttpOnly, SameSite)\n"
    "📂 Открытые файлы (.git, .env, бэкапы, дампы БД)\n"
    "📁 Листинг директорий\n"
    "📧 Защиту почты от подделки (SPF, DMARC)\n"
    "ℹ️ Утечку версий ПО и настройки CORS\n\n"
    "Команды: /scan &lt;адрес&gt; · /help"
)

HELP = (
    "❓ <b>Помощь</b>\n"
    "━━━━━━━━━━━━━━━━━━━━━━━━\n"
    "<b>Как пользоваться:</b>\n"
    "Пришлите адрес сайта текстом — я начну проверку. Через 10–30 секунд придёт "
    "отчёт с оценкой (A–F) и списком найденных проблем с решениями.\n\n"
    "<b>Команды:</b>\n"
    "/start — начало работы\n"
    "/scan example.com — запустить проверку\n"
    "/help — эта справка\n\n"
    "<b>Что значит оценка?</b>\n"
    "🟢 A/B — хорошо · 🟡 C — средне · 🟠 D — слабо · 🔴 F — есть критичные "
    "проблемы.\n\n"
    "<b>Безопасно ли это для сайта?</b>\n"
    "Да. Бот только читает то, что сайт сам отдаёт. Он не перебирает пароли, не "
    "ломает и не нагружает сайт.\n\n"
    "⚠️ Сканируйте только свои сайты или с разрешения владельца."
)


def kb_accept():
    return [[{"text": "✅ Принимаю условия", "callback_data": "accept"}]]


# ─── ЛОГИКА СКАНИРОВАНИЯ ───────────────────────────────────────────────────────

def run_scan(chat_id: str, target: str) -> None:
    """
    Выполнить скан в отдельном потоке, показывая прогресс и присылая отчёт.
    Вызывается из обработчика — не блокирует приём новых сообщений.
    """
    chat_id = str(chat_id)

    # Антифлуд: один скан за раз на пользователя + cooldown
    if chat_id in _busy_users:
        send(chat_id, "⏳ Я ещё сканирую предыдущий сайт. Подождите, пожалуйста.")
        return
    last = _last_scan_at.get(chat_id, 0)
    wait = USER_COOLDOWN - (time.time() - last)
    if wait > 0:
        send(chat_id, f"⏳ Слишком часто. Подождите {int(wait) + 1} c и попробуйте снова.")
        return

    # Стартовое сообщение, которое будем обновлять как «прогресс-бар»
    msg = send(chat_id, f"🔍 <b>Начинаю проверку</b>\n<code>{target}</code>\n\n⏳ Подключаюсь…")
    message_id = msg.get("result", {}).get("message_id")

    # throttle для editMessageText (Telegram не любит частые правки)
    state = {"last_edit": 0.0}

    def progress(text: str):
        if not message_id:
            return
        now = time.time()
        if now - state["last_edit"] < 1.2:  # не чаще раза в ~1.2 c
            return
        state["last_edit"] = now
        edit(chat_id, message_id,
             f"🔍 <b>Проверяю сайт</b>\n<code>{target}</code>\n\n{text}")

    _busy_users.add(chat_id)
    _last_scan_at[chat_id] = time.time()
    acquired = _scan_semaphore.acquire(timeout=120)
    try:
        if not acquired:
            send(chat_id, "⚠️ Сейчас слишком много проверок одновременно. "
                          "Попробуйте через минуту.")
            return

        result = scan(target, progress=progress)
        log_scan(chat_id, result)

        # Финальный статус в стартовом сообщении
        if message_id:
            if result.get("ok"):
                edit(chat_id, message_id,
                     f"✅ <b>Проверка завершена</b>\n<code>{target}</code>\n\n"
                     f"Оценка: <b>{result['grade']}</b> ({result['score']}/100). "
                     f"Отчёт ниже 👇")
            else:
                edit(chat_id, message_id, f"❌ <b>Не удалось</b>\n<code>{target}</code>")

        # Отправляем отчёт (может быть несколько сообщений)
        for part in build_report(result):
            send(chat_id, part)
            time.sleep(0.4)  # лёгкая пауза, чтобы не упереться в лимиты Telegram

    except Exception as e:
        logger.error("run_scan %s: %s", target, e)
        send(chat_id, f"❌ Ошибка при сканировании: {e}")
    finally:
        if acquired:
            _scan_semaphore.release()
        _busy_users.discard(chat_id)
        _last_scan_at[chat_id] = time.time()


# ─── ОБРАБОТКА ВХОДЯЩИХ ОБНОВЛЕНИЙ ─────────────────────────────────────────────

def process_update(update: dict) -> None:
    """Разбираем одно обновление от Telegram (сообщение или нажатие кнопки)."""

    # 1) Нажатие inline-кнопки
    if "callback_query" in update:
        cb = update["callback_query"]
        chat_id = str(cb["message"]["chat"]["id"])
        data = cb.get("data", "")
        answer_cb(cb["id"])
        if data == "accept":
            set_user(chat_id, accepted=True,
                     username=cb["from"].get("username", ""))
            send(chat_id, WELCOME)
        return

    # 2) Обычное сообщение
    msg = update.get("message") or update.get("edited_message")
    if not msg:
        return
    chat_id = str(msg["chat"]["id"])
    text = (msg.get("text") or "").strip()
    if not text:
        return

    # Команды
    if text.startswith("/start"):
        if has_accepted(chat_id):
            send(chat_id, WELCOME)
        else:
            send(chat_id, DISCLAIMER, buttons=kb_accept())
        return

    if text.startswith("/help"):
        send(chat_id, HELP)
        return

    # Пока не принял условия — просим принять
    if not has_accepted(chat_id):
        send(chat_id, DISCLAIMER, buttons=kb_accept())
        return

    # /scan <адрес>  или просто текст с адресом
    if text.startswith("/scan"):
        target = text[5:].strip()
        if not target:
            send(chat_id, "Укажите адрес: <code>/scan example.com</code>")
            return
    else:
        target = text

    # Простейшая проверка, что это похоже на адрес сайта
    candidate = target.split()[0]
    if "." not in candidate or " " in target.strip():
        send(chat_id, "Это не похоже на адрес сайта. Пришлите домен, например: "
                      "<code>example.com</code>")
        return

    # Запускаем скан в отдельном потоке, чтобы бот оставался отзывчивым
    threading.Thread(target=run_scan, args=(chat_id, candidate),
                     daemon=True).start()


# ─── ГЛАВНЫЙ ЦИКЛ (long polling) ───────────────────────────────────────────────

def poll() -> None:
    """
    Бесконечно спрашиваем у Telegram новые сообщения (long polling).
    Telegram держит соединение POLL_TIMEOUT секунд, поэтому таймаут запроса
    должен быть чуть больше.
    """
    POLL_TIMEOUT = 25
    REQ_TIMEOUT = POLL_TIMEOUT + 10
    offset = 0
    logger.info("Запущен опрос Telegram…")
    while True:
        try:
            r = requests.get(
                f"{API_URL}/getUpdates",
                params={"offset": offset, "timeout": POLL_TIMEOUT, "limit": 20},
                timeout=REQ_TIMEOUT,
            )
            data = r.json()
            for u in data.get("result", []):
                offset = u["update_id"] + 1
                try:
                    process_update(u)
                except Exception as e:
                    logger.error("process_update: %s", e)
        except Exception as e:
            logger.warning("poll: %s — повтор через 5 c", e)
            time.sleep(5)


def main() -> None:
    global BOT_USERNAME
    if not TOKEN:
        logger.error("TELEGRAM_BOT_TOKEN не задан. Укажите токен бота от @BotFather.")
        sys.exit(1)

    me = api("getMe")
    if me.get("ok"):
        BOT_USERNAME = me["result"].get("username", "")
        logger.info("🤖 Бот запущен: @%s", BOT_USERNAME)
    else:
        logger.warning("Не удалось получить getMe — проверьте токен.")

    if ADMIN_ID:
        send(ADMIN_ID,
             "🛡 <b>SiteGuard запущен!</b>\n"
             "Пришлите адрес сайта, чтобы проверить его на уязвимости.\n"
             "Команды: /start · /help")

    poll()


if __name__ == "__main__":
    main()
