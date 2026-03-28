import os
import sys
import time
import json
import logging
import requests
import pandas as pd
from datetime import datetime, timezone
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)

TOKEN     = os.environ.get("TELEGRAM_BOT_TOKEN", "")
ADMIN_ID  = os.environ.get("TELEGRAM_CHAT_ID", "")
ADMIN_IDS = os.environ.get("ADMIN_IDS", ADMIN_ID)
WALLET    = os.environ.get("USDT_WALLET", "ЗАДАЙТЕ_USDT_WALLET_В_SECRETS")
BYBIT_KEY = os.environ.get("BYBIT_API_KEY", "")
BYBIT_SEC = os.environ.get("BYBIT_API_SECRET", "")

DATA_DIR    = Path("data")
DATA_DIR.mkdir(exist_ok=True)
USERS_FILE  = DATA_DIR / "users.json"
TRADES_FILE = DATA_DIR / "bot_trades.jsonl"

PAIRS = [
    {"symbol": "BTCUSDT", "yahoo": "BTC-USD", "name": "BTC"},
    {"symbol": "ETHUSDT", "yahoo": "ETH-USD", "name": "ETH"},
    {"symbol": "SOLUSDT", "yahoo": "SOL-USD", "name": "SOL"},
]

EMA_FAST       = 50
EMA_SLOW       = 200
WR_PERIOD      = 14
WR_OVERSOLD    = -80
WR_OVERBOUGHT  = -20
MACD_FAST      = 12
MACD_SLOW      = 26
MACD_SIG       = 9
ATR_PERIOD     = 14
ATR_MULT       = 1.5
RSI_PERIOD     = 14
RISK_PCT       = 2.0
TRADE_INTERVAL = 1800
CANDLES        = 250
CMD_INTERVAL   = 3

STATES         = {}
LAST_UPDATE_ID = 0
BOT_STATES     = {}


def now_str():
    return datetime.now(timezone.utc).strftime("%d.%m.%Y %H:%M UTC")


def fmt(val, spec=",.2f"):
    return format(val, spec)


# ── База пользователей ────────────────────────────────────────────────────────

def load_users():
    if USERS_FILE.exists():
        try:
            with open(USERS_FILE, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def save_users(users):
    with open(USERS_FILE, "w", encoding="utf-8") as f:
        json.dump(users, f, indent=2, ensure_ascii=False, default=str)


def get_user(cid):
    users = load_users()
    uid   = str(cid)
    if uid not in users:
        users[uid] = {
            "id": uid, "name": "", "joined": now_str(),
            "demo": {
                "balance": 1000.0, "start": 1000.0,
                "profit": 0.0, "trades": 0, "wins": 0, "loss": 0,
                "history": [],
            },
            "real": {
                "balance": 0.0, "deposited": 0.0,
                "profit": 0.0, "trades": 0, "wins": 0, "loss": 0,
                "history": [], "active": False,
                "pending": 0.0, "pending_txid": "",
                "withdrawals": [],
            },
            "notify": True,
        }
        save_users(users)
        logger.info("Новый пользователь: %s", uid)
    return users[uid]


def save_user(cid, u):
    users = load_users()
    users[str(cid)] = u
    save_users(users)


def is_admin(cid):
    return str(cid) in [x.strip() for x in ADMIN_IDS.split(",") if x.strip()]


# ── Состояние торгового бота ──────────────────────────────────────────────────

def load_bot_state(sym):
    f = DATA_DIR / (sym + "_state.json")
    if f.exists():
        try:
            with open(f, encoding="utf-8") as fp:
                return json.load(fp)
        except Exception:
            pass
    return {"usdt": 10000.0, "coin": 0.0, "pos": None,
            "n": 0, "wins": 0, "loss": 0, "pnl": 0.0}


def save_bot_state(sym, s):
    with open(DATA_DIR / (sym + "_state.json"), "w", encoding="utf-8") as f:
        json.dump(s, f, indent=2, ensure_ascii=False, default=str)


def log_trade(t):
    with open(TRADES_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(t, ensure_ascii=False, default=str) + "\n")


def all_trades():
    trades = []
    if TRADES_FILE.exists():
        with open(TRADES_FILE, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        trades.append(json.loads(line))
                    except Exception:
                        pass
    return trades


# ── Telegram API ──────────────────────────────────────────────────────────────

def api(method, data=None):
    if not TOKEN:
        return {}
    url = "https://api.telegram.org/bot" + TOKEN + "/" + method
    try:
        r = requests.post(url, json=data or {}, timeout=15)
        return r.json()
    except Exception as e:
        logger.error("API %s: %s", method, e)
        return {}


def send(cid, text, buttons=None):
    if not TOKEN:
        return {}
    data = {"chat_id": str(cid), "text": text, "parse_mode": "HTML"}
    if buttons:
        data["reply_markup"] = json.dumps({"inline_keyboard": buttons})
    result = api("sendMessage", data)
    if not result.get("ok"):
        logger.warning("sendMessage %s: %s", cid, result.get("description", ""))
    return result


def answer_cb(cb_id, text=""):
    api("answerCallbackQuery", {"callback_query_id": cb_id, "text": text})


# ── Кнопки ────────────────────────────────────────────────────────────────────

def kb_main():
    return [
        [{"text": "👤 Мой аккаунт",    "callback_data": "account"},
         {"text": "📊 Статистика",      "callback_data": "stats"}],
        [{"text": "💰 Пополнить",       "callback_data": "deposit"},
         {"text": "💸 Вывести",         "callback_data": "withdraw"}],
        [{"text": "📈 История сделок",  "callback_data": "history"},
         {"text": "❓ Помощь",          "callback_data": "help"}],
        [{"text": "🔔 Уведомления вкл/выкл", "callback_data": "toggle_notify"}],
    ]


def kb_account():
    return [
        [{"text": "🎮 Демо-счёт",       "callback_data": "demo"},
         {"text": "💼 Реальный счёт",   "callback_data": "real"}],
        [{"text": "💰 Пополнить",       "callback_data": "deposit"},
         {"text": "💸 Вывести",         "callback_data": "withdraw"}],
        [{"text": "🏠 Главное меню",    "callback_data": "menu"}],
    ]


def kb_deposit():
    return [
        [{"text": "$50",   "callback_data": "dep_50"},
         {"text": "$100",  "callback_data": "dep_100"},
         {"text": "$200",  "callback_data": "dep_200"}],
        [{"text": "$500",  "callback_data": "dep_500"},
         {"text": "$1000", "callback_data": "dep_1000"},
         {"text": "✏️ Своя", "callback_data": "dep_custom"}],
        [{"text": "❌ Отмена", "callback_data": "menu"}],
    ]


def kb_confirm_dep(amount):
    return [
        [{"text": "✅ Я отправил(а) платёж", "callback_data": "depsent_" + str(amount)}],
        [{"text": "❌ Отмена",               "callback_data": "menu"}],
    ]


def kb_back():
    return [[{"text": "🏠 Главное меню", "callback_data": "menu"}]]


def kb_admin():
    return [
        [{"text": "👥 Пользователи",      "callback_data": "adm_users"},
         {"text": "📊 Статистика",        "callback_data": "adm_stats"}],
        [{"text": "💰 Депозиты",          "callback_data": "adm_deposits"},
         {"text": "💸 Выводы",            "callback_data": "adm_withdrawals"}],
        [{"text": "📢 Рассылка",          "callback_data": "adm_broadcast"}],
        [{"text": "🏠 Главное меню",      "callback_data": "menu"}],
    ]


# ── Вспомогательные функции ───────────────────────────────────────────────────

def pct(profit, start):
    return (profit / start * 100) if start > 0 else 0.0


def wr(wins, loss):
    return round(wins / max(wins + loss, 1) * 100)


def sign(val):
    return "+" if val >= 0 else ""


# ── Экраны ────────────────────────────────────────────────────────────────────

def screen_main(cid):
    try:
        user        = get_user(cid)
        d           = user["demo"]
        r           = user["real"]
        d_profit    = d["balance"] - d["start"]
        d_pct       = pct(d_profit, d["start"])
        r_pct       = pct(r["profit"], r["deposited"])
        notify_txt  = "включены 🔔" if user.get("notify", True) else "выключены 🔕"
        status_txt  = "✅ Активен" if r["active"] else "⏸ Неактивен"

        text = (
            "🤖 <b>CryptoBot Pro</b>\n"
            + "━━━━━━━━━━━━━━━━━━━━━\n\n"
            + "Привет, <b>" + user["name"] + "</b>! 👋\n\n"
            + "🎮 <b>Демо-счёт:</b>  $" + fmt(d["balance"])
            + "  (" + sign(d_pct) + fmt(d_pct, ".1f") + "%)\n"
            + "💼 <b>Реальный:</b>   $" + fmt(r["balance"])
            + "  (" + sign(r_pct) + fmt(r_pct, ".1f") + "%)\n"
            + "📌 <b>Статус:</b>     " + status_txt + "\n\n"
            + "📊 Стратегия: EMA50/200 + WR + MACD + ATR\n"
            + "⏱ Таймфрейм: 30 мин  |  ⚠️ Риск: 2%\n\n"
            + "Уведомления: " + notify_txt
        )
        send(cid, text, kb_main())
    except Exception as e:
        logger.error("screen_main %s: %s", cid, e)
        send(cid, "⚠️ Ошибка. Попробуйте: /start")


def screen_account(cid):
    user    = get_user(cid)
    d       = user["demo"]
    r       = user["real"]
    d_pct   = pct(d["balance"] - d["start"], d["start"])
    r_pct   = pct(r["profit"], r["deposited"])

    text = (
        "👤 <b>Мой аккаунт</b>\n"
        + "━━━━━━━━━━━━━━━━━━━━━\n\n"
        + "🎮 <b>Демо-счёт</b>\n"
        + "  Баланс:  <b>$" + fmt(d["balance"]) + "</b>\n"
        + "  Прибыль: <code>" + sign(d["balance"] - d["start"]) + fmt(d["balance"] - d["start"])
        + " USD (" + sign(d_pct) + fmt(d_pct, ".1f") + "%)</code>\n"
        + "  Сделок:  " + str(d["trades"])
        + "  |  W:" + str(d["wins"]) + " L:" + str(d["loss"])
        + " (" + str(wr(d["wins"], d["loss"])) + "%)\n\n"
        + "💼 <b>Реальный счёт</b>\n"
        + "  Баланс:  <b>$" + fmt(r["balance"]) + "</b>\n"
        + "  Внесено: $" + fmt(r["deposited"]) + "\n"
        + "  Прибыль: <code>" + sign(r["profit"]) + fmt(r["profit"])
        + " USD (" + sign(r_pct) + fmt(r_pct, ".1f") + "%)</code>\n"
        + "  Сделок:  " + str(r["trades"])
        + "  |  W:" + str(r["wins"]) + " L:" + str(r["loss"])
        + " (" + str(wr(r["wins"], r["loss"])) + "%)\n"
        + "  Статус:  " + ("✅ Активен" if r["active"] else "⏸ Неактивен") + "\n\n"
        + "📅 Регистрация: " + user["joined"]
    )
    send(cid, text, kb_account())


def screen_demo(cid):
    user  = get_user(cid)
    d     = user["demo"]
    d_pct = pct(d["balance"] - d["start"], d["start"])
    hist  = ""
    for t in reversed(d["history"][-5:]):
        p    = t.get("pnl", 0)
        icon = "✅" if p >= 0 else "❌"
        hist += "\n" + icon + " " + sign(p) + fmt(p) + " USD  " + t.get("pair", "") + "  " + t.get("time", "")[:10]

    text = (
        "🎮 <b>Демо-счёт</b>\n"
        + "━━━━━━━━━━━━━━━━━━━━━\n\n"
        + "💵 Баланс:  <b>$" + fmt(d["balance"]) + "</b>\n"
        + "🚀 Старт:   $" + fmt(d["start"]) + "\n"
        + "📈 Прибыль: <code>" + sign(d["balance"] - d["start"]) + fmt(d["balance"] - d["start"])
        + " USD (" + sign(d_pct) + fmt(d_pct, ".1f") + "%)</code>\n"
        + "🔢 Сделок:  " + str(d["trades"])
        + "  |  W:" + str(d["wins"]) + " L:" + str(d["loss"])
        + " (" + str(wr(d["wins"], d["loss"])) + "% побед)\n\n"
        + "<b>Последние 5 сделок:</b>"
        + (hist if hist else "\nСделок пока нет") + "\n\n"
        + "ℹ️ Демо: $1 000 виртуально — та же стратегия"
    )
    send(cid, text, kb_back())


def screen_real(cid):
    user   = get_user(cid)
    r      = user["real"]
    r_pct  = pct(r["profit"], r["deposited"])
    hist   = ""
    for t in reversed(r["history"][-5:]):
        p    = t.get("pnl", 0)
        icon = "✅" if p >= 0 else "❌"
        hist += "\n" + icon + " " + sign(p) + fmt(p) + " USD  " + t.get("pair", "") + "  " + t.get("time", "")[:10]
    status = "✅ Активен — бот торгует вашими средствами" if r["active"] else "⏸ Неактивен — пополните счёт"

    text = (
        "💼 <b>Реальный счёт</b>\n"
        + "━━━━━━━━━━━━━━━━━━━━━\n\n"
        + "📌 Статус:  " + status + "\n"
        + "💵 Баланс:  <b>$" + fmt(r["balance"]) + "</b>\n"
        + "📥 Внесено: $" + fmt(r["deposited"]) + "\n"
        + "📈 Прибыль: <code>" + sign(r["profit"]) + fmt(r["profit"])
        + " USD (" + sign(r_pct) + fmt(r_pct, ".1f") + "%)</code>\n"
        + "🔢 Сделок:  " + str(r["trades"])
        + "  |  W:" + str(r["wins"]) + " L:" + str(r["loss"])
        + " (" + str(wr(r["wins"], r["loss"])) + "% побед)\n\n"
        + "<b>Последние 5 сделок:</b>"
        + (hist if hist else "\nСделок пока нет")
    )
    btns = [
        [{"text": "💰 Пополнить",    "callback_data": "deposit"},
         {"text": "💸 Вывести",      "callback_data": "withdraw"}],
        [{"text": "🏠 Главное меню", "callback_data": "menu"}],
    ]
    send(cid, text, btns)


def screen_deposit(cid):
    text = (
        "💰 <b>Пополнение счёта</b>\n"
        + "━━━━━━━━━━━━━━━━━━━━━\n\n"
        + "Выберите сумму пополнения:\n\n"
        + "📌 Минимум: <b>$50 USDT</b>\n"
        + "🌐 Сеть: <b>TRC20 (Tron)</b>\n\n"
        + "После пополнения бот сразу начнёт торговать 🚀"
    )
    send(cid, text, kb_deposit())


def screen_deposit_details(cid, amount):
    text = (
        "💰 <b>Пополнение на $" + str(int(amount)) + " USDT</b>\n"
        + "━━━━━━━━━━━━━━━━━━━━━\n\n"
        + "Отправьте ровно <b>" + str(int(amount)) + " USDT</b>\n"
        + "на кошелёк:\n\n"
        + "<code>" + WALLET + "</code>\n\n"
        + "🌐 Сеть: <b>TRC20 (Tron)</b>\n"
        + "⚠️ Внимательно проверьте адрес!\n\n"
        + "После отправки нажмите кнопку ниже\n"
        + "и введите TX-хэш транзакции.\n\n"
        + "⏱ Зачисление: до 30 минут"
    )
    send(cid, text, kb_confirm_dep(amount))


def screen_stats(cid):
    trades = all_trades()
    sells  = [t for t in trades if t.get("side") == "SELL"]
    total  = len(sells)
    wins   = len([t for t in sells if t.get("pnl", 0) >= 0])
    wr_pct = round(wins / max(total, 1) * 100)
    pnl    = sum(t.get("pnl", 0) for t in sells)
    users  = load_users()
    active = len([u for u in users.values() if u["real"]["active"]])
    dep    = sum(u["real"]["deposited"] for u in users.values())

    text = (
        "📊 <b>Статистика бота</b>\n"
        + "━━━━━━━━━━━━━━━━━━━━━\n\n"
        + "<b>🤖 Результаты по парам:</b>\n"
    )
    for pair in PAIRS:
        s    = BOT_STATES.get(pair["symbol"], {})
        fl   = ""
        if s.get("pos"):
            price = fetch_price(pair["yahoo"])
            if price:
                f  = (price - s["pos"]["entry"]) * s.get("coin", 0)
                fl = "  Float: <code>" + sign(f) + fmt(f) + "$</code>"
        w_r  = str(wr(s.get("wins", 0), s.get("loss", 0)))
        text += (
            pair["name"] + ": <code>" + sign(s.get("pnl", 0)) + fmt(s.get("pnl", 0)) + "$</code>"
            + "  W:" + str(s.get("wins", 0)) + " L:" + str(s.get("loss", 0))
            + " (" + w_r + "%)" + fl + "\n"
        )
    text += (
        "\n<b>📈 Итого:</b>\n"
        + "Закрытых сделок: " + str(total) + "  |  Побед: " + str(wr_pct) + "%\n"
        + "Общая прибыль: <code>" + sign(pnl) + fmt(pnl) + " USD</code>\n\n"
        + "<b>👥 Сообщество:</b>\n"
        + "Активных инвесторов: " + str(active) + "\n"
        + "Всего внесено: $" + fmt(dep)
    )
    send(cid, text, kb_back())


def screen_history(cid):
    user = get_user(cid)
    dh   = user["demo"]["history"][-10:]
    rh   = user["real"]["history"][-10:]

    text = (
        "📈 <b>История сделок</b>\n"
        + "━━━━━━━━━━━━━━━━━━━━━\n\n"
        + "🎮 <b>Демо (последние 10):</b>\n"
    )
    if not dh:
        text += "Сделок пока нет\n"
    else:
        for t in reversed(dh):
            p    = t.get("pnl", 0)
            icon = "✅" if p >= 0 else "❌"
            text += icon + " <code>" + sign(p) + fmt(p) + " USD</code>  " + t.get("pair", "") + "  " + t.get("time", "")[:10] + "\n"

    text += "\n💼 <b>Реальный (последние 10):</b>\n"
    if not rh:
        text += "Сделок пока нет\n"
    else:
        for t in reversed(rh):
            p    = t.get("pnl", 0)
            icon = "✅" if p >= 0 else "❌"
            text += icon + " <code>" + sign(p) + fmt(p) + " USD</code>  " + t.get("pair", "") + "  " + t.get("time", "")[:10] + "\n"

    send(cid, text, kb_back())


def screen_withdraw(cid):
    user = get_user(cid)
    bal  = user["real"]["balance"]
    if bal <= 0:
        send(cid,
             "⚠️ <b>Вывод недоступен</b>\n\n"
             + "На реальном счёте нет средств.\n"
             + "Пополните счёт, чтобы начать торговлю.",
             kb_back())
        return
    STATES[str(cid)] = {"state": "wd_waiting"}
    text = (
        "💸 <b>Вывод средств</b>\n"
        + "━━━━━━━━━━━━━━━━━━━━━\n\n"
        + "Доступно: <b>$" + fmt(bal) + " USDT</b>\n\n"
        + "Введите кошелёк TRC20 и сумму:\n"
        + "Формат: <code>КОШЕЛЁК СУММА</code>\n"
        + "Пример: <code>TXxxxxxxxx 100</code>\n\n"
        + "Минимальная сумма вывода: $10"
    )
    send(cid, text, kb_back())


def screen_help(cid):
    text = (
        "❓ <b>Как работает CryptoBot Pro</b>\n"
        + "━━━━━━━━━━━━━━━━━━━━━\n\n"
        + "🎮 <b>Демо-счёт</b>\n"
        + "• $1 000 виртуальных средств\n"
        + "• Тест стратегии без риска\n"
        + "• Тот же алгоритм, что на реальном\n\n"
        + "💼 <b>Реальный счёт</b>\n"
        + "• Минимальный депозит $50 USDT (TRC20)\n"
        + "• Бот торгует автоматически 24/7\n"
        + "• Прибыль начисляется после каждой сделки\n"
        + "• Вывод доступен в любое время\n\n"
        + "📊 <b>Торговая стратегия</b>\n"
        + "• Пары: BTC, ETH, SOL\n"
        + "• Таймфрейм: 30 минут\n"
        + "• EMA 50/200 — фильтр тренда\n"
        + "• Williams %R — сигнал входа\n"
        + "• MACD — подтверждение\n"
        + "• ATR × 1.5 — динамический стоп-лосс\n"
        + "• RSI — фильтр перекупленности\n"
        + "• Риск на сделку: 2%\n\n"
        + "🔔 <b>Уведомления</b>\n"
        + "Получайте оповещения о сделках.\n"
        + "Вкл/выкл в главном меню.\n\n"
        + "📞 <b>Поддержка</b>\n"
        + "Напишите администратору."
    )
    send(cid, text, kb_back())


def screen_admin(cid):
    users  = load_users()
    active = len([u for u in users.values() if u["real"]["active"]])
    dep    = sum(u["real"]["deposited"] for u in users.values())
    pend_d = len([u for u in users.values() if u["real"].get("pending", 0) > 0])
    pend_w = sum(
        1 for u in users.values()
        for req in u["real"].get("withdrawals", [])
        if req.get("status") == "pending"
    )
    text = (
        "🔧 <b>Панель администратора</b>\n"
        + "━━━━━━━━━━━━━━━━━━━━━\n\n"
        + "👥 Всего пользователей:    <b>" + str(len(users)) + "</b>\n"
        + "✅ Активных инвесторов:    <b>" + str(active) + "</b>\n"
        + "💰 Всего внесено:          <b>$" + fmt(dep) + "</b>\n"
        + "⏳ Ожид. депозитов:        <b>" + str(pend_d) + "</b>\n"
        + "⏳ Ожид. выводов:          <b>" + str(pend_w) + "</b>"
    )
    send(cid, text, kb_admin())


def screen_admin_users(cid):
    users = load_users()
    text  = "👥 <b>Пользователи (" + str(len(users)) + ")</b>\n━━━━━━━━━━━━━━━━━━━━━\n\n"
    for uid, u in list(users.items())[:30]:
        r     = u["real"]
        text += (
            "<b>" + u.get("name", "—") + "</b>  (<code>" + uid + "</code>)\n"
            + "  Реал: $" + fmt(r["balance"])
            + " | Деп: $" + fmt(r["deposited"])
            + " | " + ("✅" if r["active"] else "⏸") + "\n"
            + "  Демо: $" + fmt(u["demo"]["balance"]) + "\n\n"
        )
    if len(users) > 30:
        text += "... ещё " + str(len(users) - 30) + "\n"
    send(cid, text, [[{"text": "◀️ Назад", "callback_data": "admin"}]])


def screen_admin_deposits(cid):
    users = load_users()
    text  = "💰 <b>Ожидающие депозиты</b>\n━━━━━━━━━━━━━━━━━━━━━\n\n"
    found = False
    btns  = []
    for uid, u in users.items():
        if u["real"].get("pending", 0) > 0:
            found = True
            amt   = u["real"]["pending"]
            txid  = u["real"].get("pending_txid", "нет")
            text += (
                "<b>" + u.get("name", "—") + "</b>  (<code>" + uid + "</code>)\n"
                + "Сумма: <b>$" + fmt(amt) + "</b>\n"
                + "TXID: <code>" + str(txid)[:30] + "</code>\n\n"
            )
            btns.append([{"text": "✅ Подтвердить $" + str(int(amt)) + " — " + u.get("name", uid),
                           "callback_data": "admconfirm_" + uid + "_" + str(amt)}])
    if not found:
        text += "Нет ожидающих депозитов ✅"
    btns.append([{"text": "◀️ Назад", "callback_data": "admin"}])
    send(cid, text, btns)


def screen_admin_withdrawals(cid):
    users = load_users()
    text  = "💸 <b>Ожидающие выводы</b>\n━━━━━━━━━━━━━━━━━━━━━\n\n"
    found = False
    btns  = []
    for uid, u in users.items():
        for req in u["real"].get("withdrawals", []):
            if req.get("status") == "pending":
                found = True
                text += (
                    "<b>" + u.get("name", "—") + "</b>  (<code>" + uid + "</code>)\n"
                    + "Сумма: <b>$" + fmt(req["amount"]) + "</b>\n"
                    + "Кошелёк: <code>" + req["wallet"] + "</code>\n\n"
                )
                btns.append([{"text": "✅ Выплатить $" + str(int(req["amount"])) + " — " + u.get("name", uid),
                               "callback_data": "admpay_" + uid + "_" + str(req["amount"])}])
    if not found:
        text += "Нет ожидающих выводов ✅"
    btns.append([{"text": "◀️ Назад", "callback_data": "admin"}])
    send(cid, text, btns)


# ── Распределение прибыли ─────────────────────────────────────────────────────

def distribute(pair_name, pnl, is_win):
    try:
        users      = load_users()
        total_real = sum(
            u["real"]["balance"] for u in users.values()
            if u["real"]["active"] and u["real"]["balance"] > 0
        )
        ts      = now_str()
        changed = False
        for uid, user in users.items():
            r = user["real"]
            d = user["demo"]
            if r["active"] and r["balance"] > 0 and total_real > 0:
                share    = r["balance"] / total_real
                user_pnl = round(pnl * share, 4)
                r["balance"] += user_pnl
                r["profit"]  += user_pnl
                r["trades"]  += 1
                if is_win:
                    r["wins"] += 1
                else:
                    r["loss"] += 1
                r["history"].append({"pair": pair_name, "pnl": user_pnl, "time": ts, "type": "real"})
                if len(r["history"]) > 200:
                    r["history"] = r["history"][-200:]
                if user.get("notify", True) and user_pnl != 0:
                    icon = "✅ ПРИБЫЛЬ" if user_pnl >= 0 else "❌ УБЫТОК"
                    send(
                        uid,
                        icon + " — <b>" + pair_name + "</b>\n"
                        + "Ваша доля: <code>" + sign(user_pnl) + fmt(user_pnl) + " USD</code>\n"
                        + "Баланс: <b>$" + fmt(r["balance"]) + "</b>",
                        [[{"text": "💼 Мой счёт", "callback_data": "real"}]],
                    )
                changed = True
            else:
                share    = d["balance"] / 1000.0 if d["balance"] > 0 else 0
                demo_pnl = round(pnl * share * 0.05, 4)
                d["balance"] += demo_pnl
                d["profit"]  += demo_pnl
                d["trades"]  += 1
                if is_win:
                    d["wins"] += 1
                else:
                    d["loss"] += 1
                d["history"].append({"pair": pair_name, "pnl": demo_pnl, "time": ts, "type": "demo"})
                if len(d["history"]) > 200:
                    d["history"] = d["history"][-200:]
                changed = True
            users[uid] = user
        if changed:
            save_users(users)
    except Exception as e:
        logger.error("distribute: %s", e)


# ── Рыночные данные и индикаторы ──────────────────────────────────────────────

def fetch_candles(sym):
    try:
        url = "https://query1.finance.yahoo.com/v8/finance/chart/" + sym
        r   = requests.get(url, params={"interval": "30m", "range": "60d"},
                           headers={"User-Agent": "Mozilla/5.0"}, timeout=20)
        r.raise_for_status()
        res = r.json()["chart"]["result"][0]
        q   = res["indicators"]["quote"][0]
        df  = pd.DataFrame({
            "ts":  res.get("timestamp", []),
            "hi":  q.get("high",   []),
            "lo":  q.get("low",    []),
            "cl":  q.get("close",  []),
            "vol": q.get("volume", []),
        })
        df = df.dropna(subset=["cl", "hi", "lo"]).reset_index(drop=True)
        for c in ["cl", "hi", "lo", "vol"]:
            df[c] = df[c].astype(float)
        if len(df) < EMA_SLOW + 10:
            return None
        return df.tail(CANDLES).reset_index(drop=True)
    except Exception as e:
        logger.error("Candles %s: %s", sym, e)
        return None


def fetch_price(sym):
    try:
        url = "https://query1.finance.yahoo.com/v8/finance/chart/" + sym
        r   = requests.get(url, params={"interval": "1m", "range": "1d"},
                           headers={"User-Agent": "Mozilla/5.0"}, timeout=15)
        r.raise_for_status()
        closes = r.json()["chart"]["result"][0]["indicators"]["quote"][0]["close"]
        p = next((c for c in reversed(closes) if c is not None), None)
        return float(p) if p else None
    except Exception as e:
        logger.error("Price %s: %s", sym, e)
        return None


def calc_ind(df):
    df       = df.copy()
    df["ef"] = df["cl"].ewm(span=EMA_FAST, adjust=False).mean()
    df["es"] = df["cl"].ewm(span=EMA_SLOW, adjust=False).mean()
    hh       = df["hi"].rolling(WR_PERIOD).max()
    ll       = df["lo"].rolling(WR_PERIOD).min()
    df["wr"] = ((hh - df["cl"]) / (hh - ll).replace(0, 1)) * -100
    mf       = df["cl"].ewm(span=MACD_FAST, adjust=False).mean()
    ms       = df["cl"].ewm(span=MACD_SLOW, adjust=False).mean()
    mc       = (mf - ms).ewm(span=MACD_SIG, adjust=False).mean()
    df["mh"] = (mf - ms) - mc
    hl       = df["hi"] - df["lo"]
    hc       = (df["hi"] - df["cl"].shift()).abs()
    lc       = (df["lo"] - df["cl"].shift()).abs()
    tr       = pd.concat([hl, hc, lc], axis=1).max(axis=1)
    df["atr"] = tr.ewm(span=ATR_PERIOD, adjust=False).mean()
    delta    = df["cl"].diff()
    gain     = delta.clip(lower=0).ewm(span=RSI_PERIOD, adjust=False).mean()
    loss_    = (-delta.clip(upper=0)).ewm(span=RSI_PERIOD, adjust=False).mean()
    rs       = gain / loss_.replace(0, float("inf"))
    df["rsi"] = 100 - (100 / (1 + rs))
    return df


def get_signal(df):
    c   = df.iloc[-1]
    p   = df.iloc[-2]
    up  = c["ef"] > c["es"]
    buy = (
        up
        and p["wr"] <= WR_OVERSOLD
        and c["wr"] > WR_OVERSOLD
        and p["mh"] < 0
        and c["mh"] >= 0
        and 40 <= c["rsi"] <= 65
    )
    sell = (
        c["wr"] >= WR_OVERBOUGHT
        or (p["mh"] >= 0 and c["mh"] < 0)
        or not up
        or c["rsi"] >= 75
    )
    if buy:
        return "BUY"
    if sell:
        return "SELL"
    return None


# ── Исполнение сделок ─────────────────────────────────────────────────────────

def do_buy(pair, price, atr):
    s   = BOT_STATES[pair["symbol"]]
    if s["pos"]:
        return None
    amt  = s["usdt"] * (RISK_PCT / 100)
    qty  = round(amt / price, 6)
    cost = qty * price
    if cost > s["usdt"]:
        return None
    sl        = round(price - ATR_MULT * atr, 6)
    s["usdt"] -= cost
    s["coin"] += qty
    s["n"]    += 1
    s["pos"]   = {"entry": price, "qty": qty, "sl": sl, "atr": atr, "time": now_str()}
    save_bot_state(pair["symbol"], s)
    t = {
        "side": "BUY", "pair": pair["name"], "qty": qty,
        "price": price, "sl": sl, "atr": atr, "time": s["pos"]["time"],
        "id": "P-" + pair["name"] + "-" + str(s["n"]).zfill(4),
    }
    log_trade(t)
    return t


def do_sell(pair, price, reason="SIGNAL"):
    s   = BOT_STATES[pair["symbol"]]
    qty = s["coin"]
    if qty <= 0.000001:
        return None
    s["usdt"] += qty * price
    s["coin"]  = 0.0
    pnl = pnl_pct = 0.0
    if s["pos"]:
        entry    = s["pos"]["entry"]
        pnl      = (price - entry) * qty
        pnl_pct  = (price - entry) / entry * 100
        s["pnl"] += pnl
        if pnl >= 0:
            s["wins"] += 1
        else:
            s["loss"] += 1
        s["pos"] = None
    s["n"] += 1
    ts = now_str()
    t  = {
        "side": "SELL", "pair": pair["name"], "qty": round(qty, 6),
        "price": price, "pnl": pnl, "pnl_pct": pnl_pct,
        "reason": reason, "time": ts,
        "id": "P-" + pair["name"] + "-" + str(s["n"]).zfill(4),
    }
    save_bot_state(pair["symbol"], s)
    log_trade(t)
    distribute(pair["name"], pnl, pnl >= 0)
    return t


def check_sl(pair, price):
    s = BOT_STATES[pair["symbol"]]
    if s["pos"] and price <= s["pos"]["sl"]:
        t = do_sell(pair, price, reason="STOP-LOSS")
        if t:
            send(ADMIN_ID,
                 "⛔ <b>СТОП-ЛОСС — " + pair["name"] + "</b>\n"
                 + "Цена: $" + fmt(price) + "\n"
                 + "P&L: <code>" + sign(t.get("pnl", 0)) + fmt(t.get("pnl", 0)) + " USD</code>")
        return True
    return False


# ── Обработка обновлений ──────────────────────────────────────────────────────

def get_updates():
    global LAST_UPDATE_ID
    try:
        r       = api("getUpdates", {"offset": LAST_UPDATE_ID + 1, "timeout": 2, "limit": 100})
        updates = r.get("result", [])
        for upd in updates:
            LAST_UPDATE_ID = upd["update_id"]
            try:
                msg = upd.get("message", {})
                cb  = upd.get("callback_query", {})
                if msg:
                    on_message(msg)
                if cb:
                    on_callback(cb)
            except Exception as e:
                logger.error("Обработка update: %s", e)
    except Exception as e:
        logger.error("get_updates: %s", e)


def on_message(msg):
    cid   = str(msg.get("chat", {}).get("id", ""))
    text  = msg.get("text", "").strip()
    name  = msg.get("from", {}).get("first_name", "Пользователь")
    if not cid:
        return

    state = STATES.get(cid, {})
    logger.info("Сообщение от %s: %s", cid, text[:60])

    if text.startswith("/start"):
        user         = get_user(cid)
        user["name"] = name
        save_user(cid, user)
        send(cid,
             "👋 <b>Добро пожаловать в CryptoBot Pro!</b>\n\n"
             + "Я торгую BTC, ETH, SOL автоматически 24/7\n"
             + "по профессиональной стратегии.\n\n"
             + "🎮 <b>Демо-счёт:</b> $1 000 виртуально — сразу!\n"
             + "💼 <b>Реальный:</b> от $50 USDT TRC20\n\n"
             + "Нажмите кнопку ниже 👇")
        screen_main(cid)
        return

    if text == "/admin" and is_admin(cid):
        screen_admin(cid)
        return

    st = state.get("state", "")

    if st == "txid_waiting":
        amt  = state.get("amount", 0)
        user = get_user(cid)
        user["real"]["pending"]      = amt
        user["real"]["pending_txid"] = text
        save_user(cid, user)
        STATES.pop(cid, None)
        send(cid,
             "⏳ <b>Заявка на пополнение принята!</b>\n\n"
             + "Сумма: <b>$" + str(int(amt)) + " USDT</b>\n"
             + "TXID: <code>" + text[:30] + "</code>\n\n"
             + "Статус: ожидает подтверждения (до 30 мин)\n"
             + "Уведомим вас после зачисления ✅",
             kb_back())
        for adm in ADMIN_IDS.split(","):
            adm = adm.strip()
            if adm:
                send(adm,
                     "🆕 <b>Новый депозит!</b>\n\n"
                     + "Пользователь: <b>" + user["name"] + "</b>  (<code>" + cid + "</code>)\n"
                     + "Сумма: <b>$" + str(int(amt)) + "</b>\n"
                     + "TXID: <code>" + text + "</code>",
                     [[{"text": "✅ Подтвердить $" + str(int(amt)),
                        "callback_data": "admconfirm_" + cid + "_" + str(float(amt))}]])
        return

    if st == "wd_waiting":
        parts = text.strip().split()
        if len(parts) < 2:
            send(cid, "⚠️ Формат: <code>КОШЕЛЁК СУММА</code>\nПример: <code>TXxxxxxxxx 100</code>")
            return
        wallet_addr = parts[0]
        try:
            amt = float(parts[1])
        except Exception:
            send(cid, "⚠️ Неверная сумма. Введите число.")
            return
        user = get_user(cid)
        if amt < 10:
            send(cid, "⚠️ Минимальная сумма вывода $10")
            return
        if amt > user["real"]["balance"]:
            send(cid, "⚠️ Недостаточно средств. Доступно: $" + fmt(user["real"]["balance"]))
            return
        user["real"]["withdrawals"].append(
            {"wallet": wallet_addr, "amount": amt, "time": now_str(), "status": "pending"}
        )
        save_user(cid, user)
        STATES.pop(cid, None)
        send(cid,
             "⏳ <b>Заявка на вывод принята!</b>\n\n"
             + "Сумма: <b>$" + fmt(amt) + " USDT</b>\n"
             + "Кошелёк: <code>" + wallet_addr[:15] + "...</code>\n\n"
             + "Обработка: до 24 часов ✅",
             kb_back())
        for adm in ADMIN_IDS.split(","):
            adm = adm.strip()
            if adm:
                send(adm,
                     "💸 <b>Запрос на вывод!</b>\n\n"
                     + "Пользователь: <b>" + user["name"] + "</b>  (<code>" + cid + "</code>)\n"
                     + "Сумма: <b>$" + fmt(amt) + "</b>\n"
                     + "Кошелёк: <code>" + wallet_addr + "</code>",
                     [[{"text": "✅ Выплатить $" + str(int(amt)),
                        "callback_data": "admpay_" + cid + "_" + str(amt)}]])
        return

    if st == "custom_dep":
        try:
            amt = float(text.replace(",", "."))
            if amt < 50:
                send(cid, "⚠️ Минимальная сумма $50")
                return
            STATES.pop(cid, None)
            screen_deposit_details(cid, amt)
        except Exception:
            send(cid, "⚠️ Введите число. Например: 150")
        return

    if st == "broadcast_waiting" and is_admin(cid):
        users  = load_users()
        count  = 0
        errors = 0
        for uid in users:
            try:
                send(uid, "📢 <b>Сообщение от администратора:</b>\n\n" + text)
                count += 1
                time.sleep(0.05)
            except Exception:
                errors += 1
        STATES.pop(cid, None)
        send(cid, "✅ Рассылка завершена!\nОтправлено: " + str(count) + "\nОшибок: " + str(errors))
        return

    screen_main(cid)


def on_callback(cb):
    cid  = str(cb.get("message", {}).get("chat", {}).get("id", ""))
    data = cb.get("data", "")
    if not cid:
        return
    answer_cb(cb["id"])
    logger.info("Callback от %s: %s", cid, data)

    try:
        if data == "menu":
            screen_main(cid)

        elif data == "account":
            screen_account(cid)

        elif data == "demo":
            screen_demo(cid)

        elif data == "real":
            screen_real(cid)

        elif data == "deposit":
            screen_deposit(cid)

        elif data == "withdraw":
            screen_withdraw(cid)

        elif data == "stats":
            screen_stats(cid)

        elif data == "history":
            screen_history(cid)

        elif data == "help":
            screen_help(cid)

        elif data == "toggle_notify":
            user = get_user(cid)
            user["notify"] = not user.get("notify", True)
            save_user(cid, user)
            status = "включены 🔔" if user["notify"] else "выключены 🔕"
            send(cid, "Уведомления " + status, kb_back())

        elif data == "admin" and is_admin(cid):
            screen_admin(cid)

        elif data == "adm_users" and is_admin(cid):
            screen_admin_users(cid)

        elif data == "adm_deposits" and is_admin(cid):
            screen_admin_deposits(cid)

        elif data == "adm_withdrawals" and is_admin(cid):
            screen_admin_withdrawals(cid)

        elif data == "adm_stats" and is_admin(cid):
            screen_stats(cid)

        elif data == "adm_broadcast" and is_admin(cid):
            STATES[cid] = {"state": "broadcast_waiting"}
            send(cid, "📢 Введите текст рассылки:")

        # ── ВАЖНО: depsent_ проверяется ДО dep_ ──
        elif data.startswith("depsent_"):
            raw = data[len("depsent_"):]
            try:
                amt = float(raw)
            except ValueError:
                logger.error("depsent_ parse error: %s", raw)
                send(cid, "⚠️ Ошибка. Попробуйте снова.")
                return
            STATES[cid] = {"state": "txid_waiting", "amount": amt}
            send(cid, "📋 Введите TX-хэш вашей транзакции:")

        elif data.startswith("dep_"):
            amt_str = data[len("dep_"):]
            if amt_str == "custom":
                STATES[cid] = {"state": "custom_dep"}
                send(cid, "✏️ Введите сумму пополнения в USDT (минимум $50):")
            else:
                try:
                    screen_deposit_details(cid, float(amt_str))
                except ValueError:
                    send(cid, "⚠️ Ошибка. Попробуйте снова.")

        elif data.startswith("admconfirm_") and is_admin(cid):
            rest  = data[len("admconfirm_"):]
            parts = rest.split("_", 1)
            if len(parts) == 2:
                target = parts[0]
                try:
                    amt = float(parts[1])
                except ValueError:
                    send(cid, "⚠️ Ошибка парсинга суммы.")
                    return
                user = get_user(target)
                user["real"]["balance"]     += amt
                user["real"]["deposited"]   += amt
                user["real"]["active"]       = True
                user["real"]["pending"]      = 0.0
                user["real"]["pending_txid"] = ""
                save_user(target, user)
                send(target,
                     "🎉 <b>Депозит подтверждён!</b>\n\n"
                     + "Зачислено: <b>$" + fmt(amt) + " USDT</b>\n"
                     + "Баланс: <b>$" + fmt(user["real"]["balance"]) + "</b>\n\n"
                     + "✅ Бот начинает торговать вашими средствами!\n"
                     + "Вы будете получать уведомления о каждой сделке.",
                     [[{"text": "💼 Мой реальный счёт", "callback_data": "real"}]])
                send(cid, "✅ Депозит $" + str(int(amt)) + " подтверждён для " + target)

        elif data.startswith("admpay_") and is_admin(cid):
            rest  = data[len("admpay_"):]
            parts = rest.split("_", 1)
            if len(parts) == 2:
                target = parts[0]
                try:
                    amt = float(parts[1])
                except ValueError:
                    send(cid, "⚠️ Ошибка парсинга суммы.")
                    return
                user = get_user(target)
                if user["real"]["balance"] >= amt:
                    user["real"]["balance"] -= amt
                    for req in user["real"]["withdrawals"]:
                        if req.get("status") == "pending" and abs(req.get("amount", 0) - amt) < 0.01:
                            req["status"] = "paid"
                            break
                    save_user(target, user)
                    send(target,
                         "✅ <b>Вывод выполнен!</b>\n\n"
                         + "Отправлено: <b>$" + fmt(amt) + " USDT</b>\n"
                         + "Остаток: <b>$" + fmt(user["real"]["balance"]) + "</b>",
                         kb_back())
                    send(cid, "✅ Выплачено $" + str(int(amt)) + " пользователю " + target)
                else:
                    send(cid, "⚠️ Недостаточно средств у пользователя " + target)

        else:
            logger.warning("Неизвестный callback: %s от %s", data, cid)

    except Exception as e:
        logger.error("on_callback %s / %s: %s", cid, data, e)
        send(cid, "⚠️ Произошла ошибка. Попробуйте снова.")


# ── Главный цикл ──────────────────────────────────────────────────────────────

def run():
    global BOT_STATES
    BOT_STATES = {p["symbol"]: load_bot_state(p["symbol"]) for p in PAIRS}
    logger.info("CryptoBot Pro запущен ✅")

    send(ADMIN_ID,
         "🤖 <b>CryptoBot Pro — ЗАПУЩЕН</b>\n"
         + "━━━━━━━━━━━━━━━━━━━━━\n\n"
         + "Режим: БУМАЖНАЯ ТОРГОВЛЯ\n"
         + "Пары: BTC | ETH | SOL\n"
         + "Стратегия: EMA50/200 + WR + MACD + ATR + RSI\n"
         + "Таймфрейм: 30 мин  |  Риск: 2%\n\n"
         + "Функции:\n"
         + "• Демо-счёт $1 000 на каждого пользователя\n"
         + "• Реальные счета с пополнением\n"
         + "• Автораспределение прибыли\n"
         + "• Депозиты/выводы\n"
         + "• Панель администратора\n"
         + "• Рассылка пользователям\n\n"
         + "/admin — открыть панель")

    last_trade_check = 0
    check_num        = 0

    while True:
        try:
            get_updates()
        except Exception as e:
            logger.error("Ошибка цикла: %s", e)
            time.sleep(5)

        now = time.time()
        if now - last_trade_check >= TRADE_INTERVAL:
            last_trade_check = now
            check_num       += 1
            logger.info("Торговая проверка #%d", check_num)
            for pair in PAIRS:
                try:
                    df = fetch_candles(pair["yahoo"])
                    if df is None:
                        continue
                    df    = calc_ind(df)
                    price = fetch_price(pair["yahoo"])
                    if price is None:
                        continue
                    c  = df.iloc[-1]
                    up = c["ef"] > c["es"]
                    logger.info("%s $%.2f  WR=%.1f  RSI=%.1f  Тренд=%s",
                                pair["name"], price, c["wr"], c["rsi"],
                                "ВВЕРХ" if up else "ВНИЗ")
                    s = BOT_STATES[pair["symbol"]]
                    if s["pos"] and check_sl(pair, price):
                        continue
                    sig = get_signal(df)
                    if sig == "BUY":
                        t = do_buy(pair, price, c["atr"])
                        if t:
                            logger.info("%s ПОКУПКА @ $%.2f", pair["name"], price)
                            send(ADMIN_ID,
                                 "📈 <b>ПОКУПКА — " + pair["name"] + "</b>\n"
                                 + "Цена: $" + fmt(price) + "\n"
                                 + "Стоп: $" + fmt(t["sl"]) + "\n"
                                 + "Риск: 2%")
                    elif sig == "SELL" and s["pos"]:
                        t = do_sell(pair, price, reason="SIGNAL")
                        if t:
                            pnl  = t.get("pnl", 0)
                            icon = "✅" if pnl >= 0 else "❌"
                            logger.info("%s ПРОДАЖА @ $%.2f  PnL=%.2f", pair["name"], price, pnl)
                            send(ADMIN_ID,
                                 icon + " <b>ПРОДАЖА — " + pair["name"] + "</b>\n"
                                 + "Цена: $" + fmt(price) + "\n"
                                 + "P&L: <code>" + sign(pnl) + fmt(pnl) + " USD</code>")
                except Exception as e:
                    logger.error("Торговля %s: %s", pair["name"], e)
                time.sleep(2)

            if check_num % 48 == 0:
                screen_stats(ADMIN_ID)

        time.sleep(CMD_INTERVAL)


if __name__ == "__main__":
    run()
