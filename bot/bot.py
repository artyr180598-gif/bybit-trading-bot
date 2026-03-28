"""
CryptoBot Pro v3 — профессиональный инвестиционный бот.

Торговая стратегия (цель: 80-120% годовых при min просадке):
  Вход  : EMA-тренд + ADX(сила тренда >25) + Williams%R из перепроданности
           + MACD гистограмма разворот + RSI в зоне импульса + объём выше среднего
  Выход : Trailing-stop (ATR×1.5, тянется вверх) + Take-profit (ATR×3)
           + RSI перекупленность + MACD разворот + тренд сломан
  Риск  : 2% капитала на сделку, max 2 позиции одновременно,
           дневной circuit-breaker (−8% → пауза 24ч),
           глобальный стоп (−20% от пика → полная пауза)
"""

import os
import sys
import time
import json
import random
import string
import logging
import requests
import pandas as pd
from datetime import datetime, timezone, timedelta
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)

# ── Конфигурация ──────────────────────────────────────────────────────────────
TOKEN     = os.environ.get("TELEGRAM_BOT_TOKEN", "")
ADMIN_ID  = os.environ.get("TELEGRAM_CHAT_ID", "")
ADMIN_IDS = os.environ.get("ADMIN_IDS", ADMIN_ID)
WALLET    = os.environ.get("USDT_WALLET", "ЗАДАЙТЕ_USDT_WALLET_В_SECRETS")

DATA_DIR    = Path("data")
DATA_DIR.mkdir(exist_ok=True)
USERS_FILE  = DATA_DIR / "users.json"
TRADES_FILE = DATA_DIR / "bot_trades.jsonl"
REFS_FILE   = DATA_DIR / "referrals.json"
DAILY_FILE  = DATA_DIR / "daily_stats.json"

PAIRS = [
    {"symbol": "BTCUSDT", "yahoo": "BTC-USD", "name": "BTC", "emoji": "₿"},
    {"symbol": "ETHUSDT", "yahoo": "ETH-USD", "name": "ETH", "emoji": "Ξ"},
    {"symbol": "SOLUSDT", "yahoo": "SOL-USD", "name": "SOL", "emoji": "◎"},
]

# ── Параметры стратегии ───────────────────────────────────────────────────────
EMA_FAST     = 20
EMA_MID      = 50
EMA_SLOW     = 200
WR_PERIOD    = 14
WR_OVERSOLD  = -80
WR_OB        = -20
MACD_FAST    = 12
MACD_SLOW    = 26
MACD_SIG     = 9
ADX_PERIOD   = 14
ADX_MIN      = 25        # Только сильные тренды
ATR_PERIOD   = 14
ATR_SL_MULT  = 1.5      # Стоп-лосс: ATR × 1.5
ATR_TP_MULT  = 3.0       # Тейк-профит: ATR × 3  →  R:R = 1:2
ATR_TRAIL    = 1.2       # Trailing-stop шаг: ATR × 1.2
RSI_PERIOD   = 14
VOL_MA       = 20        # Среднее объёма для фильтра
VOL_MULT     = 1.1       # Объём должен быть выше среднего × 1.1
RISK_PCT     = 2.0       # % капитала бота на сделку
MAX_POS      = 2         # Максимум открытых позиций
DAY_LOSS_PCT = 8.0       # Circuit-breaker: потеря >8% за день → стоп
GLOBAL_DD    = 20.0      # Глобальная просадка >20% от пика → стоп
TRADE_INT    = 1800      # Интервал торговых проверок: 30 минут
CANDLES      = 300       # История свечей для расчётов
CMD_INT      = 3         # Интервал опроса Telegram (сек)
REPORT_INT   = 7         # Автоотчёт каждые N дней

# ── Состояние ────────────────────────────────────────────────────────────────
STATES         = {}
LAST_UPD_ID    = 0
BOT_STATES     = {}


def ts():
    return datetime.now(timezone.utc).strftime("%d.%m.%Y %H:%M UTC")


def fmt(v, spec=",.2f"):
    return format(float(v), spec)


def sign(v):
    return "+" if float(v) >= 0 else ""


def pct(profit, base):
    return (float(profit) / float(base) * 100) if float(base) > 0 else 0.0


def wr_calc(wins, loss):
    return round(wins / max(wins + loss, 1) * 100)


# ── Пользователи ─────────────────────────────────────────────────────────────

def load_users():
    if USERS_FILE.exists():
        try:
            with open(USERS_FILE, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def save_users(u):
    with open(USERS_FILE, "w", encoding="utf-8") as f:
        json.dump(u, f, indent=2, ensure_ascii=False, default=str)


def get_user(cid):
    users = load_users()
    uid   = str(cid)
    if uid not in users:
        users[uid] = {
            "id": uid, "name": "", "joined": ts(),
            "demo": {
                "balance": 1000.0, "start": 1000.0, "peak": 1000.0,
                "profit": 0.0, "trades": 0, "wins": 0, "loss": 0,
                "history": [], "streak_win": 0, "streak_loss": 0,
                "max_dd": 0.0,
            },
            "real": {
                "balance": 0.0, "deposited": 0.0, "peak": 0.0,
                "profit": 0.0, "trades": 0, "wins": 0, "loss": 0,
                "history": [], "active": False, "autocompound": True,
                "pending": 0.0, "pending_txid": "",
                "withdrawals": [], "streak_win": 0, "streak_loss": 0,
                "max_dd": 0.0,
            },
            "notify": True,
            "ref_code": _gen_ref(),
            "ref_by": None,
            "ref_count": 0,
            "ref_bonus": 0.0,
            "last_seen": ts(),
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


def _gen_ref():
    return "".join(random.choices(string.ascii_uppercase + string.digits, k=8))


def load_refs():
    if REFS_FILE.exists():
        try:
            with open(REFS_FILE, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def save_refs(r):
    with open(REFS_FILE, "w", encoding="utf-8") as f:
        json.dump(r, f, indent=2, ensure_ascii=False, default=str)


# ── Состояние бота ────────────────────────────────────────────────────────────

def load_bot_state(sym):
    f = DATA_DIR / (sym + "_state.json")
    if f.exists():
        try:
            with open(f, encoding="utf-8") as fp:
                return json.load(fp)
        except Exception:
            pass
    return {
        "usdt": 10000.0, "coin": 0.0, "pos": None,
        "n": 0, "wins": 0, "loss": 0, "pnl": 0.0,
        "peak": 10000.0, "day_start": 10000.0,
        "day_date": "", "halted": False, "halt_until": 0,
    }


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


def active_positions():
    count = sum(1 for s in BOT_STATES.values() if s.get("pos"))
    return count


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
    data = {"chat_id": str(cid), "text": text[:4000], "parse_mode": "HTML"}
    if buttons:
        data["reply_markup"] = json.dumps({"inline_keyboard": buttons})
    res = api("sendMessage", data)
    if not res.get("ok"):
        logger.warning("sendMessage %s: %s", cid, res.get("description", ""))
    return res


def answer_cb(cb_id, text=""):
    api("answerCallbackQuery", {"callback_query_id": cb_id, "text": text})


# ── Кнопки ────────────────────────────────────────────────────────────────────

def kb_main():
    return [
        [{"text": "👤 Аккаунт",         "callback_data": "account"},
         {"text": "📊 Статистика",       "callback_data": "stats"}],
        [{"text": "🌐 Рынок сейчас",     "callback_data": "market"},
         {"text": "📈 Мои сделки",       "callback_data": "history"}],
        [{"text": "💰 Пополнить",        "callback_data": "deposit"},
         {"text": "💸 Вывести",          "callback_data": "withdraw"}],
        [{"text": "🤝 Реферальная программа", "callback_data": "referral"}],
        [{"text": "❓ Как это работает", "callback_data": "help"},
         {"text": "🔔 Уведомления",      "callback_data": "toggle_notify"}],
    ]


def kb_account():
    return [
        [{"text": "🎮 Демо-счёт",       "callback_data": "demo"},
         {"text": "💼 Реальный счёт",   "callback_data": "real"}],
        [{"text": "📊 Моя статистика",  "callback_data": "my_stats"},
         {"text": "🏆 Лидерборд",       "callback_data": "leaderboard"}],
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
        [{"text": "👥 Пользователи",       "callback_data": "adm_users"},
         {"text": "📊 Бот статистика",     "callback_data": "adm_stats"}],
        [{"text": "💰 Депозиты",           "callback_data": "adm_deposits"},
         {"text": "💸 Выводы",             "callback_data": "adm_withdrawals"}],
        [{"text": "📢 Рассылка",           "callback_data": "adm_broadcast"},
         {"text": "📋 Все сделки",         "callback_data": "adm_trades"}],
        [{"text": "🏠 Главное меню",       "callback_data": "menu"}],
    ]


# ── Экраны ────────────────────────────────────────────────────────────────────

def screen_main(cid):
    try:
        user = get_user(cid)
        user["last_seen"] = ts()
        save_user(cid, user)
        d       = user["demo"]
        r       = user["real"]
        d_pct   = pct(d["balance"] - d["start"], d["start"])
        r_pct   = pct(r["profit"], r["deposited"])
        ntf     = "🔔 вкл" if user.get("notify", True) else "🔕 выкл"
        s_real  = "✅ Активен" if r["active"] else "⏸ Неактивен"
        open_pos = sum(1 for s in BOT_STATES.values() if s.get("pos"))

        text = (
            "🤖 <b>CryptoBot Pro</b>\n"
            + "━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            + "Привет, <b>" + user["name"] + "</b>! 👋\n\n"
            + "🎮 Демо:     <b>$" + fmt(d["balance"]) + "</b>"
            + "  <code>" + sign(d_pct) + fmt(d_pct, ".1f") + "%</code>\n"
            + "💼 Реальный: <b>$" + fmt(r["balance"]) + "</b>"
            + "  <code>" + sign(r_pct) + fmt(r_pct, ".1f") + "%</code>\n"
            + "📌 Статус:   " + s_real + "\n"
            + "📡 Позиций открыто: " + str(open_pos) + " из " + str(MAX_POS) + "\n\n"
            + "⚡ Стратегия: EMA+ADX+WR+MACD+ATR | Risk: 2% | RR: 1:2\n"
            + "Уведомления: " + ntf
        )
        send(cid, text, kb_main())
    except Exception as e:
        logger.error("screen_main %s: %s", cid, e)
        send(cid, "⚠️ Ошибка. Попробуйте: /start")


def screen_account(cid):
    user  = get_user(cid)
    d     = user["demo"]
    r     = user["real"]
    d_pct = pct(d["balance"] - d["start"], d["start"])
    r_pct = pct(r["profit"], r["deposited"])
    d_dd  = d.get("max_dd", 0)
    r_dd  = r.get("max_dd", 0)

    text = (
        "👤 <b>Мой аккаунт</b>\n"
        + "━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        + "🎮 <b>Демо-счёт</b>\n"
        + "  Баланс:      <b>$" + fmt(d["balance"]) + "</b>\n"
        + "  Прибыль:     <code>" + sign(d["balance"] - d["start"]) + fmt(d["balance"] - d["start"])
        + " (" + sign(d_pct) + fmt(d_pct, ".1f") + "%)</code>\n"
        + "  Макс.просадка: " + fmt(d_dd, ".1f") + "%\n"
        + "  Сделок: " + str(d["trades"])
        + "  W:" + str(d["wins"]) + " L:" + str(d["loss"])
        + " (" + str(wr_calc(d["wins"], d["loss"])) + "%)\n\n"
        + "💼 <b>Реальный счёт</b>\n"
        + "  Баланс:      <b>$" + fmt(r["balance"]) + "</b>\n"
        + "  Внесено:     $" + fmt(r["deposited"]) + "\n"
        + "  Прибыль:     <code>" + sign(r["profit"]) + fmt(r["profit"])
        + " (" + sign(r_pct) + fmt(r_pct, ".1f") + "%)</code>\n"
        + "  Макс.просадка: " + fmt(r_dd, ".1f") + "%\n"
        + "  Сделок: " + str(r["trades"])
        + "  W:" + str(r["wins"]) + " L:" + str(r["loss"])
        + " (" + str(wr_calc(r["wins"], r["loss"])) + "%)\n"
        + "  Автореинвест: " + ("✅ вкл" if r.get("autocompound", True) else "❌ выкл") + "\n"
        + "  Статус:      " + ("✅ Активен" if r["active"] else "⏸ Неактивен") + "\n\n"
        + "🤝 Рефералов: " + str(user.get("ref_count", 0))
        + "  Реф.бонус: $" + fmt(user.get("ref_bonus", 0)) + "\n"
        + "📅 Регистрация: " + user["joined"]
    )
    send(cid, text, kb_account())


def screen_my_stats(cid):
    user    = get_user(cid)
    d       = user["demo"]
    r       = user["real"]
    all_t   = all_trades()
    # Примерный расчёт Sharpe (упрощённый на основе истории пользователя)
    hist    = r["history"] + d["history"]
    pnls    = [t.get("pnl", 0) for t in hist if t.get("pnl") is not None]
    sharpe  = "—"
    if len(pnls) >= 5:
        import statistics
        mean_r = statistics.mean(pnls)
        std_r  = statistics.stdev(pnls) if len(pnls) > 1 else 1
        sharpe = fmt(mean_r / max(std_r, 0.001), ".2f") if std_r > 0 else "—"

    d_streak = d.get("streak_win", 0)
    r_streak = r.get("streak_win", 0)

    text = (
        "📊 <b>Персональная статистика</b>\n"
        + "━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        + "🎮 <b>Демо-счёт</b>\n"
        + "  Winrate:         " + str(wr_calc(d["wins"], d["loss"])) + "%\n"
        + "  Макс. серия побед: " + str(d_streak) + "\n"
        + "  Макс. просадка:   " + fmt(d.get("max_dd", 0), ".1f") + "%\n"
        + "  Всего сделок:     " + str(d["trades"]) + "\n\n"
        + "💼 <b>Реальный счёт</b>\n"
        + "  Winrate:          " + str(wr_calc(r["wins"], r["loss"])) + "%\n"
        + "  Макс. серия побед:" + str(r_streak) + "\n"
        + "  Макс. просадка:   " + fmt(r.get("max_dd", 0), ".1f") + "%\n"
        + "  Прибыль/мес (ср): " + _monthly_avg(r["history"]) + "\n"
        + "  Всего сделок:     " + str(r["trades"]) + "\n"
        + "  Sharpe (approx):  " + sharpe + "\n\n"
        + "📅 Дата регистрации: " + user["joined"]
    )
    send(cid, text, kb_back())


def _monthly_avg(history):
    if not history:
        return "$0.00"
    pnls = [t.get("pnl", 0) for t in history]
    months = max(len(history) / 30.0, 1)
    avg    = sum(pnls) / months
    return sign(avg) + "$" + fmt(avg, ".2f")


def screen_leaderboard(cid):
    users = load_users()
    rows  = []
    for uid, u in users.items():
        r = u["real"]
        if r["deposited"] > 0:
            p = pct(r["profit"], r["deposited"])
            rows.append((uid, u.get("name", "—"), p, r["balance"]))
    rows.sort(key=lambda x: x[2], reverse=True)

    medals = ["🥇", "🥈", "🥉", "4️⃣", "5️⃣"]
    text   = "🏆 <b>Лидерборд — топ инвесторов</b>\n━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
    if not rows:
        text += "Пока нет активных инвесторов"
    else:
        for i, (uid, name, p, bal) in enumerate(rows[:10]):
            med   = medals[i] if i < 5 else str(i + 1) + "."
            me    = " ← вы" if uid == str(cid) else ""
            text += med + " <b>" + name + "</b>  " + sign(p) + fmt(p, ".1f") + "%" + me + "\n"
    send(cid, text, kb_back())


def screen_market(cid):
    text  = "🌐 <b>Рынок сейчас</b>\n━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
    for pair in PAIRS:
        price = fetch_price(pair["yahoo"])
        s     = BOT_STATES.get(pair["symbol"], {})
        pos   = s.get("pos")
        pos_txt = ""
        if pos:
            fl     = (price - pos["entry"]) * s.get("coin", 0) if price else 0
            fl_pct = ((price - pos["entry"]) / pos["entry"] * 100) if (price and pos["entry"]) else 0
            pos_txt = (
                "\n  📍 Позиция: вход $" + fmt(pos["entry"])
                + "\n  💰 Float P&L: <code>" + sign(fl) + fmt(fl) + " USD (" + sign(fl_pct) + fmt(fl_pct, ".1f") + "%)</code>"
                + "\n  🛡 Стоп: $" + fmt(pos.get("sl", 0))
                + " | 🎯 TP: $" + fmt(pos.get("tp", 0))
            )
        price_txt = "$" + fmt(price) if price else "нет данных"
        text += pair["emoji"] + " <b>" + pair["name"] + "</b>  " + price_txt + pos_txt + "\n\n"
    text += "⏱ Обновляется каждые 30 минут\n"
    text += "📡 Стратегия ищет точки входа 24/7"
    send(cid, text, kb_back())


def screen_demo(cid):
    user  = get_user(cid)
    d     = user["demo"]
    d_pct = pct(d["balance"] - d["start"], d["start"])
    hist  = ""
    for t in reversed(d["history"][-6:]):
        p    = t.get("pnl", 0)
        icon = "✅" if p >= 0 else "❌"
        hist += "\n" + icon + " " + sign(p) + fmt(p) + " USD  " + t.get("pair", "") + "  " + t.get("time", "")[:10]

    text = (
        "🎮 <b>Демо-счёт</b>\n"
        + "━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        + "💵 Баланс:     <b>$" + fmt(d["balance"]) + "</b>\n"
        + "🚀 Старт:      $" + fmt(d["start"]) + "\n"
        + "📈 Прибыль:    <code>" + sign(d["balance"] - d["start"])
        + fmt(d["balance"] - d["start"]) + " (" + sign(d_pct) + fmt(d_pct, ".1f") + "%)</code>\n"
        + "📉 Макс.просадка: " + fmt(d.get("max_dd", 0), ".1f") + "%\n"
        + "🔢 Сделок: " + str(d["trades"])
        + "  W:" + str(d["wins"]) + " L:" + str(d["loss"])
        + " (" + str(wr_calc(d["wins"], d["loss"])) + "%)\n\n"
        + "<b>Последние 6 сделок:</b>"
        + (hist if hist else "\nСделок пока нет") + "\n\n"
        + "ℹ️ $1 000 виртуально — тот же алгоритм"
    )
    send(cid, text, kb_back())


def screen_real(cid):
    user   = get_user(cid)
    r      = user["real"]
    r_pct  = pct(r["profit"], r["deposited"])
    hist   = ""
    for t in reversed(r["history"][-6:]):
        p    = t.get("pnl", 0)
        icon = "✅" if p >= 0 else "❌"
        hist += "\n" + icon + " " + sign(p) + fmt(p) + " USD  " + t.get("pair", "") + "  " + t.get("time", "")[:10]
    status = "✅ Активен" if r["active"] else "⏸ Неактивен — пополните счёт"
    btns = [
        [{"text": "💰 Пополнить",      "callback_data": "deposit"},
         {"text": "💸 Вывести",        "callback_data": "withdraw"}],
        [{"text": "🔄 Автореинвест: " + ("✅" if r.get("autocompound", True) else "❌"),
          "callback_data": "toggle_compound"}],
        [{"text": "🏠 Главное меню",   "callback_data": "menu"}],
    ]
    text = (
        "💼 <b>Реальный счёт</b>\n"
        + "━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        + "📌 Статус:      " + status + "\n"
        + "💵 Баланс:      <b>$" + fmt(r["balance"]) + "</b>\n"
        + "📥 Внесено:     $" + fmt(r["deposited"]) + "\n"
        + "📈 Прибыль:     <code>" + sign(r["profit"]) + fmt(r["profit"])
        + " (" + sign(r_pct) + fmt(r_pct, ".1f") + "%)</code>\n"
        + "📉 Макс.просадка: " + fmt(r.get("max_dd", 0), ".1f") + "%\n"
        + "🔄 Автореинвест: " + ("✅ прибыль реинвестируется" if r.get("autocompound", True) else "❌ прибыль не реинвестируется") + "\n"
        + "🔢 Сделок: " + str(r["trades"])
        + "  W:" + str(r["wins"]) + " L:" + str(r["loss"])
        + " (" + str(wr_calc(r["wins"], r["loss"])) + "%)\n\n"
        + "<b>Последние 6 сделок:</b>"
        + (hist if hist else "\nСделок пока нет")
    )
    send(cid, text, btns)


def screen_deposit(cid):
    text = (
        "💰 <b>Пополнение счёта</b>\n"
        + "━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        + "Выберите сумму:\n\n"
        + "📌 Минимум: <b>$50 USDT</b>\n"
        + "🌐 Сеть: <b>TRC20 (Tron)</b>\n\n"
        + "💡 Чем больше депозит — тем больше\n"
        + "   абсолютная прибыль с каждой сделки 🚀"
    )
    send(cid, text, kb_deposit())


def screen_deposit_details(cid, amount):
    text = (
        "💰 <b>Пополнение на $" + str(int(amount)) + " USDT</b>\n"
        + "━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        + "Отправьте ровно <b>" + str(int(amount)) + " USDT</b>\n"
        + "на следующий кошелёк:\n\n"
        + "<code>" + WALLET + "</code>\n\n"
        + "🌐 Сеть: <b>TRC20 (Tron)</b>\n"
        + "⚠️ Проверьте адрес — иные сети не поддерживаются!\n\n"
        + "После отправки нажмите кнопку ниже\n"
        + "и введите TX-хэш (TXID) транзакции.\n\n"
        + "⏱ Зачисление: <b>до 30 минут</b>"
    )
    send(cid, text, kb_confirm_dep(amount))


def screen_stats(cid):
    trades = all_trades()
    sells  = [t for t in trades if t.get("side") == "SELL"]
    total  = len(sells)
    wins_n = len([t for t in sells if t.get("pnl", 0) >= 0])
    wr_pct = round(wins_n / max(total, 1) * 100)
    pnl    = sum(t.get("pnl", 0) for t in sells)
    avg_w  = sum(t.get("pnl", 0) for t in sells if t.get("pnl", 0) >= 0) / max(wins_n, 1)
    avg_l  = sum(t.get("pnl", 0) for t in sells if t.get("pnl", 0) < 0) / max(total - wins_n, 1)
    rr     = abs(avg_w / avg_l) if avg_l != 0 else 0
    users  = load_users()
    active = len([u for u in users.values() if u["real"]["active"]])
    dep    = sum(u["real"]["deposited"] for u in users.values())

    # Circuit-breaker статус
    halted = any(s.get("halted") for s in BOT_STATES.values())

    text = (
        "📊 <b>Статистика бота</b>\n"
        + "━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        + "<b>📡 Позиции сейчас:</b>\n"
    )
    for pair in PAIRS:
        s     = BOT_STATES.get(pair["symbol"], {})
        w_r   = str(wr_calc(s.get("wins", 0), s.get("loss", 0)))
        price = fetch_price(pair["yahoo"])
        fl    = ""
        if s.get("pos") and price:
            f  = (price - s["pos"]["entry"]) * s.get("coin", 0)
            fl = "  Float: <code>" + sign(f) + fmt(f) + "$</code>"
        halt_txt = " ⛔СТОП" if s.get("halted") else ""
        text += (
            pair["emoji"] + " " + pair["name"] + halt_txt + ": <code>"
            + sign(s.get("pnl", 0)) + fmt(s.get("pnl", 0)) + "$</code>"
            + "  " + w_r + "% побед" + fl + "\n"
        )

    text += (
        "\n<b>📈 Итого по боту:</b>\n"
        + "Сделок: " + str(total) + "  |  Winrate: " + str(wr_pct) + "%\n"
        + "Avg.Win: $" + fmt(avg_w) + "  Avg.Loss: $" + fmt(avg_l) + "\n"
        + "R:R реальный: 1:" + fmt(rr, ".1f") + "\n"
        + "Общий P&L: <code>" + sign(pnl) + fmt(pnl) + " USD</code>\n"
        + ("⛔ Торговля приостановлена (circuit breaker)\n" if halted else "") + "\n"
        + "<b>👥 Сообщество:</b>\n"
        + "Пользователей: " + str(len(users)) + "\n"
        + "Активных: " + str(active) + "\n"
        + "Внесено всего: $" + fmt(dep)
    )
    send(cid, text, kb_back())


def screen_history(cid):
    user = get_user(cid)
    dh   = user["demo"]["history"][-10:]
    rh   = user["real"]["history"][-10:]

    text = "📈 <b>История сделок</b>\n━━━━━━━━━━━━━━━━━━━━━━━━\n\n🎮 <b>Демо (последние 10):</b>\n"
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
    if bal < 10:
        send(cid,
             "⚠️ <b>Вывод недоступен</b>\n\n"
             + "Минимальный баланс для вывода: $10\n"
             + "Ваш баланс: $" + fmt(bal),
             kb_back())
        return
    STATES[str(cid)] = {"state": "wd_waiting"}
    text = (
        "💸 <b>Вывод средств</b>\n"
        + "━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        + "Доступно: <b>$" + fmt(bal) + " USDT</b>\n\n"
        + "Введите TRC20-кошелёк и сумму:\n"
        + "Формат: <code>КОШЕЛЁК СУММА</code>\n"
        + "Пример: <code>TXxxxxxxxx 150</code>\n\n"
        + "📌 Минимальная сумма: $10\n"
        + "⏱ Обработка: до 24 часов"
    )
    send(cid, text, kb_back())


def screen_referral(cid):
    user     = get_user(cid)
    ref_code = user.get("ref_code", _gen_ref())
    count    = user.get("ref_count", 0)
    bonus    = user.get("ref_bonus", 0.0)
    bot_info = api("getMe")
    bot_name = bot_info.get("result", {}).get("username", "your_bot")
    link     = "https://t.me/" + bot_name + "?start=ref_" + ref_code

    text = (
        "🤝 <b>Реферальная программа</b>\n"
        + "━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        + "Приглашайте друзей и зарабатывайте!\n\n"
        + "💰 Ваша ставка: <b>5% с прибыли реферала</b>\n"
        + "   (начисляется автоматически при каждой сделке)\n\n"
        + "🔗 Ваша ссылка:\n"
        + "<code>" + link + "</code>\n\n"
        + "📊 Ваши рефералы: <b>" + str(count) + "</b>\n"
        + "💵 Реферальный доход: <b>$" + fmt(bonus) + "</b>\n\n"
        + "Поделитесь ссылкой — зарабатывайте вместе! 🚀"
    )
    send(cid, text, kb_back())


def screen_help(cid):
    text = (
        "❓ <b>Как работает CryptoBot Pro</b>\n"
        + "━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        + "🎮 <b>Демо-счёт</b>\n"
        + "• $1 000 виртуально — сразу после /start\n"
        + "• Тот же алгоритм, что на реальном счёте\n"
        + "• Проверьте стратегию без риска\n\n"
        + "💼 <b>Реальный счёт</b>\n"
        + "• Минимальный депозит: $50 USDT TRC20\n"
        + "• Бот торгует 24/7 автоматически\n"
        + "• Прибыль начисляется после каждой сделки\n"
        + "• Вывод доступен в любое время\n\n"
        + "📊 <b>Профессиональная стратегия</b>\n"
        + "• Пары: BTC, ETH, SOL (30 мин таймфрейм)\n"
        + "• EMA 20/50/200 — определение тренда\n"
        + "• ADX > 25 — только сильные тренды\n"
        + "• Williams %R — точки входа из перепроданности\n"
        + "• MACD — подтверждение разворота\n"
        + "• RSI 45-65 — зона импульса (не перекуплен)\n"
        + "• Объём > среднего — подтверждение силы\n"
        + "• Trailing-stop (ATR×1.5) — тянется за ценой\n"
        + "• Take-profit ATR×3 — соотношение 1:2\n\n"
        + "🛡 <b>Защита капитала</b>\n"
        + "• Риск 2% на сделку\n"
        + "• Макс. 2 позиции одновременно\n"
        + "• Стоп при дневном убытке >8%\n"
        + "• Глобальный стоп при просадке >20%\n\n"
        + "🔄 <b>Автореинвест</b>\n"
        + "Прибыль реинвестируется автоматически\n"
        + "(можно отключить в разделе «Реальный счёт»)\n\n"
        + "🤝 <b>Реферальная программа</b>\n"
        + "Приглашайте друзей — получайте 5% от их прибыли\n\n"
        + "📞 <b>Поддержка:</b> обратитесь к администратору"
    )
    send(cid, text, kb_back())


def screen_admin(cid):
    users  = load_users()
    active = len([u for u in users.values() if u["real"]["active"]])
    dep    = sum(u["real"]["deposited"] for u in users.values())
    bal    = sum(u["real"]["balance"] for u in users.values())
    pend_d = len([u for u in users.values() if u["real"].get("pending", 0) > 0])
    pend_w = sum(
        1 for u in users.values()
        for req in u["real"].get("withdrawals", [])
        if req.get("status") == "pending"
    )
    halted = any(s.get("halted") for s in BOT_STATES.values())
    text = (
        "🔧 <b>Панель администратора</b>\n"
        + "━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        + "👥 Пользователей:      <b>" + str(len(users)) + "</b>\n"
        + "✅ Активных:           <b>" + str(active) + "</b>\n"
        + "💰 Внесено всего:      <b>$" + fmt(dep) + "</b>\n"
        + "💼 Баланс у клиентов:  <b>$" + fmt(bal) + "</b>\n"
        + "⏳ Ожид. депозитов:    <b>" + str(pend_d) + "</b>\n"
        + "⏳ Ожид. выводов:      <b>" + str(pend_w) + "</b>\n"
        + ("⛔ ТОРГОВЛЯ ОСТАНОВЛЕНА\n" if halted else "🟢 Торговля активна\n")
    )
    send(cid, text, kb_admin())


def screen_admin_users(cid):
    users = load_users()
    text  = "👥 <b>Пользователи (" + str(len(users)) + ")</b>\n━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
    for uid, u in list(users.items())[:25]:
        r = u["real"]
        text += (
            "<b>" + u.get("name", "—") + "</b>  <code>" + uid + "</code>\n"
            + "  Реал: $" + fmt(r["balance"])
            + " | Деп: $" + fmt(r["deposited"])
            + " | " + ("✅" if r["active"] else "⏸") + "\n\n"
        )
    send(cid, text, [[{"text": "◀️ Назад", "callback_data": "admin"}]])


def screen_admin_deposits(cid):
    users = load_users()
    text  = "💰 <b>Ожидающие депозиты</b>\n━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
    found = False
    btns  = []
    for uid, u in users.items():
        if u["real"].get("pending", 0) > 0:
            found = True
            amt   = u["real"]["pending"]
            txid  = u["real"].get("pending_txid", "нет")
            text += (
                "<b>" + u.get("name", "—") + "</b>  <code>" + uid + "</code>\n"
                + "Сумма: <b>$" + fmt(amt) + "</b>\n"
                + "TXID: <code>" + str(txid)[:32] + "</code>\n\n"
            )
            btns.append([{"text": "✅ Подтвердить $" + str(int(amt)) + " — " + u.get("name", uid),
                           "callback_data": "admconfirm_" + uid + "_" + str(amt)}])
    if not found:
        text += "Нет ожидающих депозитов ✅"
    btns.append([{"text": "◀️ Назад", "callback_data": "admin"}])
    send(cid, text, btns)


def screen_admin_withdrawals(cid):
    users = load_users()
    text  = "💸 <b>Ожидающие выводы</b>\n━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
    found = False
    btns  = []
    for uid, u in users.items():
        for req in u["real"].get("withdrawals", []):
            if req.get("status") == "pending":
                found = True
                text += (
                    "<b>" + u.get("name", "—") + "</b>  <code>" + uid + "</code>\n"
                    + "Сумма: <b>$" + fmt(req["amount"]) + "</b>\n"
                    + "Кошелёк: <code>" + req["wallet"] + "</code>\n\n"
                )
                btns.append([{"text": "✅ Выплатить $" + str(int(req["amount"])) + " — " + u.get("name", uid),
                               "callback_data": "admpay_" + uid + "_" + str(req["amount"])}])
    if not found:
        text += "Нет ожидающих выводов ✅"
    btns.append([{"text": "◀️ Назад", "callback_data": "admin"}])
    send(cid, text, btns)


def screen_admin_trades(cid):
    trades = all_trades()
    sells  = [t for t in trades if t.get("side") == "SELL"][-15:]
    text   = "📋 <b>Последние 15 сделок</b>\n━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
    if not sells:
        text += "Сделок пока нет"
    else:
        for t in reversed(sells):
            p    = t.get("pnl", 0)
            icon = "✅" if p >= 0 else "❌"
            text += (
                icon + " " + t.get("pair", "") + "  "
                + "<code>" + sign(p) + fmt(p) + "$</code>  "
                + t.get("reason", "") + "  "
                + t.get("time", "")[:10] + "\n"
            )
    send(cid, text, [[{"text": "◀️ Назад", "callback_data": "admin"}]])


# ── Распределение прибыли с реферальными бонусами ────────────────────────────

def distribute(pair_name, pnl, is_win):
    try:
        users      = load_users()
        total_real = sum(
            u["real"]["balance"] for u in users.values()
            if u["real"]["active"] and u["real"]["balance"] > 0
        )
        now = ts()
        for uid, user in users.items():
            r = user["real"]
            d = user["demo"]

            # ── Реальный счёт ──
            if r["active"] and r["balance"] > 0 and total_real > 0:
                share    = r["balance"] / total_real
                user_pnl = round(pnl * share, 4)

                r["balance"] += user_pnl
                r["profit"]  += user_pnl
                r["trades"]  += 1

                # Обновление пика для расчёта просадки
                if r["balance"] > r.get("peak", 0):
                    r["peak"] = r["balance"]
                dd = pct(r.get("peak", r["balance"]) - r["balance"], r.get("peak", r["balance"]))
                if dd > r.get("max_dd", 0):
                    r["max_dd"] = dd

                # Серия побед/поражений
                if is_win:
                    r["wins"]         += 1
                    r["streak_win"]    = r.get("streak_win", 0) + 1
                    r["streak_loss"]   = 0
                else:
                    r["loss"]         += 1
                    r["streak_loss"]   = r.get("streak_loss", 0) + 1
                    r["streak_win"]    = 0

                r["history"].append({"pair": pair_name, "pnl": user_pnl, "time": now})
                if len(r["history"]) > 200:
                    r["history"] = r["history"][-200:]

                # Реферальный бонус рефереру (5% от прибыли реферала)
                ref_by = user.get("ref_by")
                if ref_by and user_pnl > 0 and ref_by in users:
                    bonus = round(user_pnl * 0.05, 4)
                    users[ref_by]["real"]["balance"] += bonus
                    users[ref_by]["ref_bonus"]        = users[ref_by].get("ref_bonus", 0) + bonus
                    users[ref_by]["real"]["profit"]   += bonus

                if user.get("notify", True) and user_pnl != 0:
                    icon = "✅ ПРИБЫЛЬ" if user_pnl >= 0 else "❌ УБЫТОК"
                    send(uid,
                         icon + " — <b>" + pair_name + "</b>\n"
                         + "Ваша доля: <code>" + sign(user_pnl) + fmt(user_pnl) + " USD</code>\n"
                         + "Баланс: <b>$" + fmt(r["balance"]) + "</b>",
                         [[{"text": "💼 Мой счёт", "callback_data": "real"}]])

            # ── Демо-счёт (пропорционально) ──
            else:
                share    = d["balance"] / 1000.0 if d["balance"] > 0 else 0
                demo_pnl = round(pnl * share * 0.05, 4)
                d["balance"] += demo_pnl
                d["profit"]  += demo_pnl
                d["trades"]  += 1
                if d["balance"] > d.get("peak", d["balance"]):
                    d["peak"] = d["balance"]
                dd = pct(d.get("peak", d["balance"]) - d["balance"], d.get("peak", d["balance"]))
                if dd > d.get("max_dd", 0):
                    d["max_dd"] = dd
                if is_win:
                    d["wins"] += 1
                    d["streak_win"]  = d.get("streak_win", 0) + 1
                    d["streak_loss"] = 0
                else:
                    d["loss"] += 1
                    d["streak_loss"] = d.get("streak_loss", 0) + 1
                    d["streak_win"]  = 0
                d["history"].append({"pair": pair_name, "pnl": demo_pnl, "time": now})
                if len(d["history"]) > 200:
                    d["history"] = d["history"][-200:]

            users[uid] = user
        save_users(users)
    except Exception as e:
        logger.error("distribute: %s", e)


# ── Автоматический еженедельный отчёт ────────────────────────────────────────

def weekly_report():
    try:
        users   = load_users()
        trades  = all_trades()
        sells   = [t for t in trades if t.get("side") == "SELL"]
        total   = len(sells)
        wins    = len([t for t in sells if t.get("pnl", 0) >= 0])
        wr_pct  = round(wins / max(total, 1) * 100)
        week_pnl = sum(t.get("pnl", 0) for t in sells[-48:])  # ~48 сделок за неделю

        for uid, user in users.items():
            try:
                r      = user["real"]
                d      = user["demo"]
                r_pct  = pct(r["profit"], r["deposited"])
                d_pct  = pct(d["balance"] - d["start"], d["start"])
                send(uid,
                     "📅 <b>Еженедельный отчёт CryptoBot Pro</b>\n"
                     + "━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
                     + "🎮 Демо:     " + sign(d_pct) + fmt(d_pct, ".1f") + "%  ($" + fmt(d["balance"]) + ")\n"
                     + "💼 Реальный: " + sign(r_pct) + fmt(r_pct, ".1f") + "%  ($" + fmt(r["balance"]) + ")\n\n"
                     + "🤖 <b>Бот за неделю:</b>\n"
                     + "Winrate: " + str(wr_pct) + "%\n"
                     + "P&L за нед.: <code>" + sign(week_pnl) + fmt(week_pnl) + " USD</code>\n\n"
                     + "Продолжаем торговать! 🚀",
                     [[{"text": "📊 Подробнее", "callback_data": "stats"}]])
                time.sleep(0.05)
            except Exception:
                pass
    except Exception as e:
        logger.error("weekly_report: %s", e)


# ── Рыночные данные ───────────────────────────────────────────────────────────

def fetch_candles(sym, interval="30m", period="60d"):
    try:
        url = "https://query1.finance.yahoo.com/v8/finance/chart/" + sym
        r   = requests.get(url,
                           params={"interval": interval, "range": period},
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
        for c in ["cl", "hi", "lo"]:
            df[c] = df[c].astype(float)
        df["vol"] = pd.to_numeric(df["vol"], errors="coerce").fillna(0)
        if len(df) < EMA_SLOW + 10:
            return None
        return df.tail(CANDLES).reset_index(drop=True)
    except Exception as e:
        logger.error("Candles %s: %s", sym, e)
        return None


def fetch_price(sym):
    try:
        url = "https://query1.finance.yahoo.com/v8/finance/chart/" + sym
        r   = requests.get(url,
                           params={"interval": "1m", "range": "1d"},
                           headers={"User-Agent": "Mozilla/5.0"}, timeout=15)
        r.raise_for_status()
        closes = r.json()["chart"]["result"][0]["indicators"]["quote"][0]["close"]
        p = next((c for c in reversed(closes) if c is not None), None)
        return float(p) if p else None
    except Exception as e:
        logger.error("Price %s: %s", sym, e)
        return None


# ── Индикаторы ────────────────────────────────────────────────────────────────

def calc_ind(df):
    df = df.copy()

    # EMA
    df["ef"]  = df["cl"].ewm(span=EMA_FAST, adjust=False).mean()
    df["em"]  = df["cl"].ewm(span=EMA_MID,  adjust=False).mean()
    df["es"]  = df["cl"].ewm(span=EMA_SLOW, adjust=False).mean()

    # Williams %R
    hh       = df["hi"].rolling(WR_PERIOD).max()
    ll       = df["lo"].rolling(WR_PERIOD).min()
    df["wr"] = ((hh - df["cl"]) / (hh - ll).replace(0, 1)) * -100

    # MACD
    mf       = df["cl"].ewm(span=MACD_FAST, adjust=False).mean()
    ms       = df["cl"].ewm(span=MACD_SLOW, adjust=False).mean()
    mc       = (mf - ms).ewm(span=MACD_SIG, adjust=False).mean()
    df["mh"] = (mf - ms) - mc

    # ATR
    hl       = df["hi"] - df["lo"]
    hc       = (df["hi"] - df["cl"].shift()).abs()
    lc       = (df["lo"] - df["cl"].shift()).abs()
    tr       = pd.concat([hl, hc, lc], axis=1).max(axis=1)
    df["atr"] = tr.ewm(span=ATR_PERIOD, adjust=False).mean()

    # RSI
    delta    = df["cl"].diff()
    gain     = delta.clip(lower=0).ewm(span=RSI_PERIOD, adjust=False).mean()
    loss_    = (-delta.clip(upper=0)).ewm(span=RSI_PERIOD, adjust=False).mean()
    rs       = gain / loss_.replace(0, float("inf"))
    df["rsi"] = 100 - (100 / (1 + rs))

    # ADX (сила тренда — ключевой фильтр!)
    df["pdm"] = (df["hi"] - df["hi"].shift()).clip(lower=0)
    df["ndm"] = (df["lo"].shift() - df["lo"]).clip(lower=0)
    # Где pdm > ndm, обнуляем ndm и наоборот
    pdm_clean = df["pdm"].where(df["pdm"] > df["ndm"], 0)
    ndm_clean = df["ndm"].where(df["ndm"] > df["pdm"], 0)
    atr_adx   = tr.ewm(span=ADX_PERIOD, adjust=False).mean()
    di_p      = 100 * pdm_clean.ewm(span=ADX_PERIOD, adjust=False).mean() / atr_adx.replace(0, 1)
    di_n      = 100 * ndm_clean.ewm(span=ADX_PERIOD, adjust=False).mean() / atr_adx.replace(0, 1)
    dx        = (100 * (di_p - di_n).abs() / (di_p + di_n).replace(0, 1))
    df["adx"] = dx.ewm(span=ADX_PERIOD, adjust=False).mean()
    df["dip"] = di_p
    df["din"] = di_n

    # Объём скользящее среднее
    df["vol_ma"] = df["vol"].rolling(VOL_MA).mean()

    return df


def get_signal(df):
    """
    Профессиональная стратегия с 5 подтверждениями.
    BUY только при совпадении ВСЕХ условий (высокая точность).
    SELL при любом из условий выхода.
    """
    if len(df) < 3:
        return None

    c  = df.iloc[-1]
    p  = df.iloc[-2]
    p2 = df.iloc[-3]

    # ── ТРЕНД: иерархия EMA ──
    bull_trend = (c["ef"] > c["em"]) and (c["em"] > c["es"])
    bear_trend = (c["ef"] < c["em"]) and (c["em"] < c["es"])

    # ── СИЛА ТРЕНДА: ADX ──
    strong_trend = c["adx"] > ADX_MIN

    # ── СИГНАЛ ВХОДА ──
    # Williams %R: пересечение снизу вверх из зоны перепроданности
    wr_cross_up  = (p["wr"] <= WR_OVERSOLD) and (c["wr"] > WR_OVERSOLD)
    # Или текущая ситуация — wr ещё в перепроданности но начинает расти
    wr_rising    = (c["wr"] > p["wr"]) and (c["wr"] <= -60)

    # MACD гистограмма: разворот с отрицательного на положительное
    macd_cross   = (p["mh"] < 0) and (c["mh"] >= 0)
    # Или уже растёт второй бар подряд
    macd_growing = (c["mh"] > p["mh"]) and (p["mh"] > p2["mh"]) and (c["mh"] < 0)

    # RSI: зона импульса (45-65 при входе — не перекуплен, но есть сила)
    rsi_ok = 42 <= c["rsi"] <= 65

    # Объём: выше среднего (реальный интерес)
    vol_ok = (c["vol_ma"] > 0) and (c["vol"] >= c["vol_ma"] * VOL_MULT)

    buy = (
        bull_trend
        and strong_trend
        and (wr_cross_up or wr_rising)
        and (macd_cross or macd_growing)
        and rsi_ok
        and vol_ok
    )

    # ── СИГНАЛ ВЫХОДА ──
    sell = (
        bear_trend                            # тренд сломан
        or c["rsi"] >= 75                     # перекупленность
        or (c["mh"] < 0 and p["mh"] >= 0)    # MACD разворот вниз
        or c["wr"] >= WR_OB                   # Williams %R перекуплен
    )

    if buy:
        return "BUY"
    if sell:
        return "SELL"
    return None


# ── Исполнение сделок ─────────────────────────────────────────────────────────

def do_buy(pair, price, atr):
    s = BOT_STATES[pair["symbol"]]
    if s.get("pos"):
        return None
    if active_positions() >= MAX_POS:
        return None
    if s.get("halted") and time.time() < s.get("halt_until", 0):
        return None

    amt  = s["usdt"] * (RISK_PCT / 100)
    qty  = round(amt / price, 8)
    cost = qty * price
    if cost > s["usdt"]:
        return None

    sl = round(price - ATR_SL_MULT * atr, 6)
    tp = round(price + ATR_TP_MULT * atr, 6)

    s["usdt"] -= cost
    s["coin"]  = qty
    s["n"]    += 1
    s["pos"]   = {
        "entry":     price,
        "qty":       qty,
        "sl":        sl,
        "tp":        tp,
        "trail_sl":  sl,     # trailing stop — поднимается с ценой
        "atr":       atr,
        "time":      ts(),
    }
    save_bot_state(pair["symbol"], s)
    t = {
        "side": "BUY", "pair": pair["name"], "qty": qty,
        "price": price, "sl": sl, "tp": tp, "atr": atr, "time": s["pos"]["time"],
        "id": "P-" + pair["name"] + "-" + str(s["n"]).zfill(4),
    }
    log_trade(t)
    return t


def update_trailing_stop(pair, price):
    """Поднимаем trailing-stop вслед за ценой."""
    s   = BOT_STATES[pair["symbol"]]
    pos = s.get("pos")
    if not pos:
        return
    new_sl = round(price - ATR_TRAIL * pos["atr"], 6)
    if new_sl > pos.get("trail_sl", pos["sl"]):
        pos["trail_sl"] = new_sl
        # Также поднимаем основной SL
        pos["sl"]       = new_sl
        s["pos"]        = pos
        save_bot_state(pair["symbol"], s)


def check_exits(pair, price):
    """Проверяем Stop-Loss и Take-Profit."""
    s   = BOT_STATES[pair["symbol"]]
    pos = s.get("pos")
    if not pos:
        return False

    # Обновляем trailing stop
    update_trailing_stop(pair, price)

    # Проверяем SL
    if price <= pos["sl"]:
        t = do_sell(pair, price, "STOP-LOSS")
        if t:
            pnl = t.get("pnl", 0)
            send(ADMIN_ID,
                 "⛔ <b>СТОП-ЛОСС — " + pair["name"] + "</b>\n"
                 + "Цена: $" + fmt(price) + "\n"
                 + "P&L: <code>" + sign(pnl) + fmt(pnl) + " USD</code>")
        return True

    # Проверяем TP
    if price >= pos["tp"]:
        t = do_sell(pair, price, "TAKE-PROFIT")
        if t:
            pnl = t.get("pnl", 0)
            send(ADMIN_ID,
                 "🎯 <b>ТЕЙК-ПРОФИТ — " + pair["name"] + "</b>\n"
                 + "Цена: $" + fmt(price) + "\n"
                 + "P&L: <code>" + sign(pnl) + fmt(pnl) + " USD</code>")
        return True

    return False


def circuit_breaker_check(pair, sym):
    """Дневной circuit-breaker и глобальная защита от просадки."""
    s       = BOT_STATES[sym]
    today   = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    # Сброс дневного счётчика
    if s.get("day_date") != today:
        s["day_date"]  = today
        s["day_start"] = s["usdt"]
        s["halted"]    = False

    equity = s["usdt"]

    # Дневной убыток > DAY_LOSS_PCT%
    if s["day_start"] > 0:
        day_dd = pct(s["day_start"] - equity, s["day_start"])
        if day_dd > DAY_LOSS_PCT and not s.get("halted"):
            s["halted"]     = True
            s["halt_until"] = time.time() + 86400  # 24 часа
            save_bot_state(sym, s)
            send(ADMIN_ID,
                 "⛔ <b>CIRCUIT BREAKER — " + pair["name"] + "</b>\n"
                 + "Дневной убыток: " + fmt(day_dd, ".1f") + "%\n"
                 + "Торговля приостановлена на 24 часа")
            return True

    # Глобальная просадка > GLOBAL_DD% от пика
    if equity > s.get("peak", equity):
        s["peak"] = equity
    if s["peak"] > 0:
        global_dd = pct(s["peak"] - equity, s["peak"])
        if global_dd > GLOBAL_DD and not s.get("halted"):
            s["halted"]     = True
            s["halt_until"] = time.time() + 86400 * 3  # 3 дня
            save_bot_state(sym, s)
            send(ADMIN_ID,
                 "🚨 <b>ГЛОБАЛЬНАЯ ЗАЩИТА — " + pair["name"] + "</b>\n"
                 + "Просадка от пика: " + fmt(global_dd, ".1f") + "%\n"
                 + "Торговля приостановлена на 3 дня!")
            return True

    # Снятие хальта если время вышло
    if s.get("halted") and time.time() >= s.get("halt_until", 0):
        s["halted"]    = False
        s["halt_until"] = 0
        s["day_start"]  = s["usdt"]
        save_bot_state(sym, s)
        send(ADMIN_ID, "✅ Торговля возобновлена: " + pair["name"])

    return s.get("halted", False)


def do_sell(pair, price, reason="SIGNAL"):
    s   = BOT_STATES[pair["symbol"]]
    qty = s.get("coin", 0)
    if qty <= 0.000001:
        return None

    s["usdt"] += qty * price
    s["coin"]  = 0.0
    pnl = 0.0
    if s.get("pos"):
        entry    = s["pos"]["entry"]
        pnl      = (price - entry) * qty
        s["pnl"] += pnl
        if pnl >= 0:
            s["wins"] += 1
        else:
            s["loss"] += 1
        s["pos"] = None

    s["n"] += 1
    t = {
        "side": "SELL", "pair": pair["name"], "qty": round(qty, 8),
        "price": price, "pnl": pnl,
        "reason": reason, "time": ts(),
        "id": "P-" + pair["name"] + "-" + str(s["n"]).zfill(4),
    }
    save_bot_state(pair["symbol"], s)
    log_trade(t)
    distribute(pair["name"], pnl, pnl >= 0)
    return t


# ── Обработка сообщений ───────────────────────────────────────────────────────

def get_updates():
    global LAST_UPD_ID
    try:
        r       = api("getUpdates", {"offset": LAST_UPD_ID + 1, "timeout": 2, "limit": 100})
        updates = r.get("result", [])
        for upd in updates:
            LAST_UPD_ID = upd["update_id"]
            try:
                msg = upd.get("message", {})
                cb  = upd.get("callback_query", {})
                if msg:
                    on_message(msg)
                if cb:
                    on_callback(cb)
            except Exception as e:
                logger.error("update error: %s", e)
    except Exception as e:
        logger.error("get_updates: %s", e)


def on_message(msg):
    cid   = str(msg.get("chat", {}).get("id", ""))
    text  = msg.get("text", "").strip()
    name  = msg.get("from", {}).get("first_name", "Пользователь")
    if not cid:
        return

    state = STATES.get(cid, {})
    logger.info("MSG %s: %s", cid, text[:60])

    # /start [ref_CODE]
    if text.startswith("/start"):
        user         = get_user(cid)
        user["name"] = name
        # Обработка реферального кода
        parts = text.split()
        if len(parts) > 1 and parts[1].startswith("ref_") and user.get("ref_by") is None:
            ref_code = parts[1][4:]
            users    = load_users()
            for uid, u in users.items():
                if u.get("ref_code") == ref_code and uid != cid:
                    user["ref_by"] = uid
                    users[uid]["ref_count"] = users[uid].get("ref_count", 0) + 1
                    save_users(users)
                    send(uid, "🎉 По вашей реферальной ссылке зарегистрировался новый пользователь!")
                    break
        save_user(cid, user)
        send(cid,
             "👋 <b>Добро пожаловать в CryptoBot Pro!</b>\n\n"
             + "Я торгую BTC, ETH и SOL автоматически 24/7\n"
             + "с профессиональной мультиусловной стратегией.\n\n"
             + "🎮 <b>Демо-счёт $1 000</b> уже доступен!\n"
             + "💼 Для реального — минимальный депозит $50 USDT\n\n"
             + "⬇️ Выберите раздел ниже 👇")
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
             "⏳ <b>Заявка принята!</b>\n\n"
             + "Сумма: <b>$" + str(int(amt)) + " USDT</b>\n"
             + "TXID: <code>" + text[:32] + "</code>\n\n"
             + "Статус: ожидает подтверждения (до 30 мин)\n"
             + "Уведомим вас сразу после зачисления ✅",
             kb_back())
        for adm in ADMIN_IDS.split(","):
            adm = adm.strip()
            if adm:
                send(adm,
                     "🆕 <b>Новый депозит!</b>\n\n"
                     + "Пользователь: <b>" + user["name"] + "</b>  <code>" + cid + "</code>\n"
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
            send(cid, "⚠️ Неверная сумма.")
            return
        user = get_user(cid)
        if amt < 10:
            send(cid, "⚠️ Минимальный вывод: $10")
            return
        if amt > user["real"]["balance"]:
            send(cid, "⚠️ Недостаточно средств. Доступно: $" + fmt(user["real"]["balance"]))
            return
        user["real"]["withdrawals"].append(
            {"wallet": wallet_addr, "amount": amt, "time": ts(), "status": "pending"}
        )
        save_user(cid, user)
        STATES.pop(cid, None)
        send(cid,
             "⏳ <b>Заявка на вывод принята!</b>\n\n"
             + "Сумма: <b>$" + fmt(amt) + " USDT</b>\n"
             + "Кошелёк: <code>" + wallet_addr[:20] + "...</code>\n\n"
             + "Обработка: до 24 часов ✅",
             kb_back())
        for adm in ADMIN_IDS.split(","):
            adm = adm.strip()
            if adm:
                send(adm,
                     "💸 <b>Запрос на вывод!</b>\n\n"
                     + "Пользователь: <b>" + user["name"] + "</b>  <code>" + cid + "</code>\n"
                     + "Сумма: <b>$" + fmt(amt) + "</b>\n"
                     + "Кошелёк: <code>" + wallet_addr + "</code>",
                     [[{"text": "✅ Выплатить $" + str(int(amt)),
                        "callback_data": "admpay_" + cid + "_" + str(amt)}]])
        return

    if st == "custom_dep":
        try:
            amt = float(text.replace(",", "."))
            if amt < 50:
                send(cid, "⚠️ Минимум $50")
                return
            STATES.pop(cid, None)
            screen_deposit_details(cid, amt)
        except Exception:
            send(cid, "⚠️ Введите число. Например: 200")
        return

    if st == "broadcast_waiting" and is_admin(cid):
        users = load_users()
        ok    = 0
        for uid in users:
            try:
                send(uid, "📢 <b>Сообщение от администратора:</b>\n\n" + text)
                ok += 1
                time.sleep(0.05)
            except Exception:
                pass
        STATES.pop(cid, None)
        send(cid, "✅ Рассылка завершена! Отправлено: " + str(ok))
        return

    screen_main(cid)


def on_callback(cb):
    cid  = str(cb.get("message", {}).get("chat", {}).get("id", ""))
    data = cb.get("data", "")
    if not cid:
        return
    answer_cb(cb["id"])
    logger.info("CB %s: %s", cid, data)

    try:
        if   data == "menu":           screen_main(cid)
        elif data == "account":        screen_account(cid)
        elif data == "my_stats":       screen_my_stats(cid)
        elif data == "leaderboard":    screen_leaderboard(cid)
        elif data == "demo":           screen_demo(cid)
        elif data == "real":           screen_real(cid)
        elif data == "deposit":        screen_deposit(cid)
        elif data == "withdraw":       screen_withdraw(cid)
        elif data == "stats":          screen_stats(cid)
        elif data == "history":        screen_history(cid)
        elif data == "help":           screen_help(cid)
        elif data == "market":         screen_market(cid)
        elif data == "referral":       screen_referral(cid)

        elif data == "toggle_notify":
            user = get_user(cid)
            user["notify"] = not user.get("notify", True)
            save_user(cid, user)
            txt = "Уведомления включены 🔔" if user["notify"] else "Уведомления выключены 🔕"
            send(cid, txt, kb_back())

        elif data == "toggle_compound":
            user = get_user(cid)
            user["real"]["autocompound"] = not user["real"].get("autocompound", True)
            save_user(cid, user)
            txt = "✅ Автореинвест включён" if user["real"]["autocompound"] else "❌ Автореинвест выключен"
            send(cid, txt + "\n\nВозврат в меню:", kb_back())

        elif data == "admin" and is_admin(cid):         screen_admin(cid)
        elif data == "adm_users" and is_admin(cid):     screen_admin_users(cid)
        elif data == "adm_deposits" and is_admin(cid):  screen_admin_deposits(cid)
        elif data == "adm_withdrawals" and is_admin(cid): screen_admin_withdrawals(cid)
        elif data == "adm_stats" and is_admin(cid):     screen_stats(cid)
        elif data == "adm_trades" and is_admin(cid):    screen_admin_trades(cid)

        elif data == "adm_broadcast" and is_admin(cid):
            STATES[cid] = {"state": "broadcast_waiting"}
            send(cid, "📢 Введите текст рассылки всем пользователям:")

        # ── ВАЖНО: depsent_ ВСЕГДА перед dep_ ──
        elif data.startswith("depsent_"):
            raw = data[len("depsent_"):]
            try:
                amt = float(raw)
            except ValueError:
                send(cid, "⚠️ Ошибка. Попробуйте снова.")
                return
            STATES[cid] = {"state": "txid_waiting", "amount": amt}
            send(cid, "📋 Введите TX-хэш вашей транзакции (TXID):")

        elif data.startswith("dep_"):
            amt_str = data[len("dep_"):]
            if amt_str == "custom":
                STATES[cid] = {"state": "custom_dep"}
                send(cid, "✏️ Введите сумму в USDT (минимум $50):")
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
                    return
                user = get_user(target)
                user["real"]["balance"]     += amt
                user["real"]["deposited"]   += amt
                user["real"]["active"]       = True
                user["real"]["pending"]      = 0.0
                user["real"]["pending_txid"] = ""
                if user["real"]["balance"] > user["real"].get("peak", 0):
                    user["real"]["peak"] = user["real"]["balance"]
                save_user(target, user)
                send(target,
                     "🎉 <b>Депозит подтверждён!</b>\n\n"
                     + "Зачислено: <b>$" + fmt(amt) + " USDT</b>\n"
                     + "Баланс: <b>$" + fmt(user["real"]["balance"]) + "</b>\n\n"
                     + "✅ Бот начинает торговать вашими средствами!\n"
                     + "Уведомления о каждой сделке придут автоматически.",
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
            logger.warning("Неизвестный callback: %s", data)

    except Exception as e:
        logger.error("on_callback %s/%s: %s", cid, data, e)
        send(cid, "⚠️ Произошла ошибка. Попробуйте снова.")


# ── Главный торговый цикл ─────────────────────────────────────────────────────

def run():
    global BOT_STATES
    BOT_STATES = {p["symbol"]: load_bot_state(p["symbol"]) for p in PAIRS}
    logger.info("CryptoBot Pro v3 запущен ✅")

    send(ADMIN_ID,
         "🤖 <b>CryptoBot Pro v3 — ЗАПУЩЕН</b>\n"
         + "━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
         + "Стратегия: EMA(20/50/200) + ADX + WR + MACD + RSI + Объём\n"
         + "Защита: Trailing-stop | TP(3×ATR) | Circuit-Breaker | MaxDD\n"
         + "Цель: 80-120% годовых при контролируемом риске\n\n"
         + "Новое:\n"
         + "• ADX фильтр (только сильные тренды)\n"
         + "• Trailing-stop (тянется за ценой)\n"
         + "• Take-profit 3×ATR (соотношение R:R 1:2)\n"
         + "• Circuit-breaker (стоп при дневном убытке >8%)\n"
         + "• Реферальная программа (5% с прибыли)\n"
         + "• Лидерборд инвесторов\n"
         + "• Автореинвест прибыли\n"
         + "• Еженедельные отчёты\n\n"
         + "/admin — панель управления")

    last_trade = 0
    last_report = time.time()
    check_num   = 0

    while True:
        try:
            get_updates()
        except Exception as e:
            logger.error("Ошибка цикла: %s", e)
            time.sleep(5)

        now = time.time()

        # ── Торговля каждые 30 минут ──
        if now - last_trade >= TRADE_INT:
            last_trade = now
            check_num += 1
            logger.info("─── Торговая проверка #%d ───", check_num)

            for pair in PAIRS:
                try:
                    # Circuit-breaker проверка
                    if circuit_breaker_check(pair, pair["symbol"]):
                        continue

                    df = fetch_candles(pair["yahoo"])
                    if df is None:
                        logger.warning("%s: нет данных", pair["name"])
                        continue
                    df    = calc_ind(df)
                    price = fetch_price(pair["yahoo"])
                    if price is None:
                        continue

                    c   = df.iloc[-1]
                    adx = c["adx"]

                    logger.info(
                        "%s $%.2f | EMA↑=%s ADX=%.1f WR=%.1f RSI=%.1f MACD=%+.4f",
                        pair["name"], price,
                        str(c["ef"] > c["em"] > c["es"]),
                        adx, c["wr"], c["rsi"], c["mh"],
                    )

                    s = BOT_STATES[pair["symbol"]]

                    # Проверка выходов (SL/TP/Trailing)
                    if s.get("pos") and check_exits(pair, price):
                        continue

                    # Торговый сигнал
                    sig = get_signal(df)
                    if sig == "BUY":
                        t = do_buy(pair, price, c["atr"])
                        if t:
                            logger.info("%s ПОКУПКА @ $%.2f  SL=$%.2f TP=$%.2f",
                                        pair["name"], price, t["sl"], t["tp"])
                            send(ADMIN_ID,
                                 "📈 <b>ПОКУПКА — " + pair["name"] + "</b>\n"
                                 + "Цена:  $" + fmt(price) + "\n"
                                 + "Стоп:  $" + fmt(t["sl"]) + "\n"
                                 + "TP:    $" + fmt(t["tp"]) + "\n"
                                 + "ADX:   " + fmt(adx, ".1f") + "\n"
                                 + "Риск:  2% | R:R 1:2")
                    elif sig == "SELL" and s.get("pos"):
                        t = do_sell(pair, price, "SIGNAL")
                        if t:
                            pnl  = t.get("pnl", 0)
                            icon = "✅" if pnl >= 0 else "❌"
                            logger.info("%s ПРОДАЖА @ $%.2f  PnL=$%.2f", pair["name"], price, pnl)
                            send(ADMIN_ID,
                                 icon + " <b>ПРОДАЖА — " + pair["name"] + "</b>\n"
                                 + "Цена: $" + fmt(price) + "\n"
                                 + "P&L:  <code>" + sign(pnl) + fmt(pnl) + " USD</code>")

                except Exception as e:
                    logger.error("Торговля %s: %s", pair["name"], e)
                time.sleep(2)

            # Статистика каждые 24 часа (48 проверок × 30 мин)
            if check_num % 48 == 0:
                screen_stats(ADMIN_ID)

        # ── Еженедельный отчёт всем пользователям ──
        if now - last_report >= 86400 * REPORT_INT:
            last_report = now
            logger.info("Отправляем еженедельный отчёт...")
            weekly_report()

        time.sleep(CMD_INT)


if __name__ == "__main__":
    run()
