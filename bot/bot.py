"""
CryptoBot Pro v4 — профессиональный торговый бот (демо-режим)

СТРАТЕГИЯ: EMA + Supertrend + RSI + MACD + Volume
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Данные:    Bybit Public API (реальные цены, бесплатно, без ключей)
Таймфрейм: 1H (основной) + 4H (фильтр тренда)
Сигнал входа (нужны ВСЕ условия):
  ✅ EMA9 > EMA21 > EMA50  (восходящий тренд)
  ✅ Supertrend = БЫЧИЙ    (ATR-трендовый индикатор)
  ✅ RSI в диапазоне 42-68 (не перекуплен, есть импульс)
  ✅ MACD гистограмма разворачивается вверх
  ✅ Объём выше среднего × 1.2
  ✅ Тренд на 4H совпадает (EMA21_4h > EMA50_4h)
Выход:
  🔴 Supertrend разворачивается вниз
  🔴 RSI > 76 (перекупленность)
  🔴 EMA9 пробивает EMA21 вниз
  🔴 Достигнут стоп-лосс (ATR × 1.5)
  🔴 Достигнут тейк-профит (ATR × 3.0 = R:R 1:2)
Риск: 2% капитала на сделку | max 3 позиции | circuit-breaker -7%/-18%

ЦЕЛЬ: 70-100% годовых при максимальной просадке < 20%
"""

import os, sys, time, json, random, string, logging, requests
import pandas as pd
import numpy  as np
from datetime    import datetime, timezone
from pathlib     import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)

# ─── ENV ──────────────────────────────────────────────────────────────────────
TOKEN    = os.environ.get("TELEGRAM_BOT_TOKEN", "")
ADMIN_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
ADMIN_IDS = os.environ.get("ADMIN_IDS", ADMIN_ID)
WALLET   = os.environ.get("USDT_WALLET", "ЗАДАЙТЕ_USDT_WALLET")

DATA_DIR    = Path("data")
DATA_DIR.mkdir(exist_ok=True)
USERS_FILE  = DATA_DIR / "users.json"
TRADES_FILE = DATA_DIR / "bot_trades.jsonl"
REFS_FILE   = DATA_DIR / "referrals.json"

# ─── ТОРГОВЫЕ ПАРЫ (Bybit символы) ────────────────────────────────────────────
PAIRS = [
    {"symbol": "BTCUSDT", "name": "BTC", "emoji": "₿"},
    {"symbol": "ETHUSDT", "name": "ETH", "emoji": "Ξ"},
    {"symbol": "SOLUSDT", "name": "SOL", "emoji": "◎"},
]

# ─── ПАРАМЕТРЫ СТРАТЕГИИ ──────────────────────────────────────────────────────
EMA_FAST     = 9
EMA_MID      = 21
EMA_SLOW     = 50
MACD_FAST    = 12
MACD_SLOW    = 26
MACD_SIG     = 9
RSI_PERIOD   = 14
RSI_MIN      = 42
RSI_MAX      = 68
RSI_OB       = 76
ATR_PERIOD   = 14
ATR_SL_MULT  = 1.5
ATR_TP_MULT  = 3.0
ATR_TRAIL    = 1.2
ST_MULT      = 3.0
ST_PERIOD    = 10
VOL_MA_P     = 20
VOL_MULT     = 1.2
RISK_PCT     = 2.0
MAX_POS      = 3
DAY_LOSS_PCT = 7.0
GLOBAL_DD    = 18.0
TRADE_INT    = 3600
CMD_INT      = 3

BYBIT_URL = "https://api.bybit.com"

# ─── BYBIT PUBLIC API ─────────────────────────────────────────────────────────

def fetch_bybit_klines(symbol, interval="60", limit=200):
    try:
        r = requests.get(
            f"{BYBIT_URL}/v5/market/kline",
            params={"category": "spot", "symbol": symbol, "interval": interval, "limit": limit},
            timeout=15,
        )
        r.raise_for_status()
        data = r.json()
        if data.get("retCode") != 0 or not data.get("result", {}).get("list"):
            logger.warning("Bybit kline %s/%s: %s", symbol, interval, data.get("retMsg"))
            return None
        rows = data["result"]["list"]
        df   = pd.DataFrame(rows, columns=["ts", "op", "hi", "lo", "cl", "vol", "turnover"])
        df   = df.astype({c: float for c in ["ts","op","hi","lo","cl","vol","turnover"]})
        df.sort_values("ts", inplace=True)
        df.reset_index(drop=True, inplace=True)
        return df
    except Exception as e:
        logger.error("fetch_bybit_klines %s: %s", symbol, e)
        return None


def fetch_bybit_price(symbol):
    try:
        r = requests.get(
            f"{BYBIT_URL}/v5/market/tickers",
            params={"category": "spot", "symbol": symbol},
            timeout=10,
        )
        r.raise_for_status()
        lst = r.json().get("result", {}).get("list", [])
        if lst:
            return float(lst[0]["lastPrice"])
    except Exception as e:
        logger.error("fetch_bybit_price %s: %s", symbol, e)
    return None

# ─── ИНДИКАТОРЫ ───────────────────────────────────────────────────────────────

def calc_indicators(df):
    df = df.copy()
    df["ema_fast"] = df["cl"].ewm(span=EMA_FAST, adjust=False).mean()
    df["ema_mid"]  = df["cl"].ewm(span=EMA_MID,  adjust=False).mean()
    df["ema_slow"] = df["cl"].ewm(span=EMA_SLOW, adjust=False).mean()

    hl  = df["hi"] - df["lo"]
    hc  = (df["hi"] - df["cl"].shift()).abs()
    lc  = (df["lo"] - df["cl"].shift()).abs()
    tr  = pd.concat([hl, hc, lc], axis=1).max(axis=1)
    df["atr"] = tr.ewm(span=ATR_PERIOD, adjust=False).mean()

    # Supertrend
    hl2    = (df["hi"] + df["lo"]) / 2
    upper  = hl2 + ST_MULT * df["atr"]
    lower  = hl2 - ST_MULT * df["atr"]
    f_up   = upper.copy()
    f_lo   = lower.copy()
    st_dir = [1] * len(df)
    st_val = [0.0] * len(df)

    for i in range(1, len(df)):
        if upper.iloc[i] < f_up.iloc[i-1] or df["cl"].iloc[i-1] > f_up.iloc[i-1]:
            f_up.iloc[i] = upper.iloc[i]
        else:
            f_up.iloc[i] = f_up.iloc[i-1]

        if lower.iloc[i] > f_lo.iloc[i-1] or df["cl"].iloc[i-1] < f_lo.iloc[i-1]:
            f_lo.iloc[i] = lower.iloc[i]
        else:
            f_lo.iloc[i] = f_lo.iloc[i-1]

        if st_dir[i-1] == -1 and df["cl"].iloc[i] > f_up.iloc[i-1]:
            st_dir[i] = 1
        elif st_dir[i-1] == 1 and df["cl"].iloc[i] < f_lo.iloc[i-1]:
            st_dir[i] = -1
        else:
            st_dir[i] = st_dir[i-1]

        st_val[i] = f_lo.iloc[i] if st_dir[i] == 1 else f_up.iloc[i]

    df["st"]     = st_val
    df["st_dir"] = st_dir

    delta = df["cl"].diff()
    gain  = delta.clip(lower=0).ewm(span=RSI_PERIOD, adjust=False).mean()
    loss  = (-delta.clip(upper=0)).ewm(span=RSI_PERIOD, adjust=False).mean()
    rs    = gain / loss.replace(0, float("inf"))
    df["rsi"] = 100 - (100 / (1 + rs))

    mf          = df["cl"].ewm(span=MACD_FAST, adjust=False).mean()
    ms          = df["cl"].ewm(span=MACD_SLOW, adjust=False).mean()
    mc          = (mf - ms).ewm(span=MACD_SIG, adjust=False).mean()
    df["macd_h"] = (mf - ms) - mc

    df["vol_ma"] = df["vol"].rolling(VOL_MA_P).mean()
    return df


def get_4h_trend(symbol):
    df4 = fetch_bybit_klines(symbol, interval="240", limit=80)
    if df4 is None or len(df4) < 60:
        return 0
    df4  = calc_indicators(df4)
    last = df4.iloc[-1]
    if last["ema_mid"] > last["ema_slow"] and last["st_dir"] == 1:
        return 1
    if last["ema_mid"] < last["ema_slow"] and last["st_dir"] == -1:
        return -1
    return 0


def get_signal(df, trend_4h):
    if len(df) < 3:
        return None
    c  = df.iloc[-1]
    p  = df.iloc[-2]
    p2 = df.iloc[-3]

    ema_bull  = (c["ema_fast"] > c["ema_mid"]) and (c["ema_mid"] > c["ema_slow"])
    st_bull   = c["st_dir"] == 1
    rsi_ok    = RSI_MIN <= c["rsi"] <= RSI_MAX
    macd_up   = (c["macd_h"] > p["macd_h"]) and (p["macd_h"] > p2["macd_h"] or c["macd_h"] >= 0)
    vol_ok    = (c["vol_ma"] > 0) and (c["vol"] >= c["vol_ma"] * VOL_MULT)
    trend_ok  = trend_4h >= 0

    buy = ema_bull and st_bull and rsi_ok and macd_up and vol_ok and trend_ok

    ema_cross = c["ema_fast"] < c["ema_mid"] and p["ema_fast"] >= p["ema_mid"]
    st_flip   = c["st_dir"] == -1 and p["st_dir"] == 1
    rsi_ob    = c["rsi"] >= RSI_OB
    macd_down = (c["macd_h"] < 0) and (p["macd_h"] >= 0)

    sell = st_flip or rsi_ob or ema_cross or macd_down

    if buy:
        return "BUY"
    if sell:
        return "SELL"
    return None

# ─── СОСТОЯНИЕ БОТА ───────────────────────────────────────────────────────────

BOT_STATES = {}


def load_bot_state(sym):
    f = DATA_DIR / f"{sym}_state.json"
    if f.exists():
        try:
            with open(f, encoding="utf-8") as fp:
                return json.load(fp)
        except Exception:
            pass
    return {
        "usdt": 10000.0, "coin": 0.0, "pos": None,
        "n": 0, "wins": 0, "loss": 0, "pnl": 0.0,
        "peak": 10000.0, "day_start": 10000.0, "day_date": "",
        "halted": False, "halt_until": 0,
    }


def save_bot_state(sym, s):
    with open(DATA_DIR / f"{sym}_state.json", "w", encoding="utf-8") as f:
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
    return sum(1 for s in BOT_STATES.values() if s.get("pos"))

# ─── ИСПОЛНЕНИЕ СДЕЛОК ────────────────────────────────────────────────────────

def do_buy(pair, price, atr):
    s = BOT_STATES[pair["symbol"]]
    if s.get("pos") or active_positions() >= MAX_POS:
        return None
    if s.get("halted") and time.time() < s.get("halt_until", 0):
        return None

    sl   = round(price - ATR_SL_MULT * atr, 6)
    tp   = round(price + ATR_TP_MULT * atr, 6)
    risk = s["usdt"] * (RISK_PCT / 100)
    qty  = round(risk / max(price - sl, 0.000001), 8)
    cost = qty * price
    if cost > s["usdt"] * 0.95:
        qty  = round(s["usdt"] * 0.95 / price, 8)
        cost = qty * price
    if qty <= 0:
        return None

    s["usdt"] -= cost
    s["coin"]  = qty
    s["n"]    += 1
    s["pos"]   = {
        "entry": price, "qty": qty, "sl": sl, "tp": tp,
        "trail_sl": sl, "atr": atr, "time": ts(),
        "id": f"T{pair['name']}-{s['n']:04d}",
    }
    save_bot_state(pair["symbol"], s)
    t = {**s["pos"], "side": "BUY", "pair": pair["name"],
         "equity_after": round(s["usdt"] + qty * price, 2)}
    log_trade(t)
    return t


def update_trailing(pair, price):
    s   = BOT_STATES[pair["symbol"]]
    pos = s.get("pos")
    if not pos:
        return
    new_sl = round(price - ATR_TRAIL * pos["atr"], 6)
    if new_sl > pos.get("trail_sl", pos["sl"]):
        pos["trail_sl"] = new_sl
        pos["sl"]       = new_sl
        s["pos"]        = pos
        save_bot_state(pair["symbol"], s)


def do_sell(pair, price, reason="SIGNAL"):
    s   = BOT_STATES[pair["symbol"]]
    pos = s.get("pos")
    if not pos or s.get("coin", 0) <= 0:
        return None

    qty  = pos["qty"]
    pnl  = round((price - pos["entry"]) * qty, 4)
    s["usdt"] += qty * price
    s["coin"]  = 0.0
    s["pnl"]  += pnl
    s["n"]    += 1
    if pnl >= 0:
        s["wins"] += 1
    else:
        s["loss"] += 1
    if s["usdt"] > s.get("peak", 0):
        s["peak"] = s["usdt"]

    t = {
        "side": "SELL", "pair": pair["name"], "qty": round(qty, 8),
        "entry": pos["entry"], "price": price, "pnl": pnl,
        "reason": reason, "time": ts(),
        "id": f"T{pair['name']}-{s['n']:04d}",
        "equity_after": round(s["usdt"], 2),
    }
    s["pos"] = None
    save_bot_state(pair["symbol"], s)
    log_trade(t)
    distribute(pair["name"], pnl, pnl >= 0)
    return t


def check_exits(pair, price):
    s   = BOT_STATES[pair["symbol"]]
    pos = s.get("pos")
    if not pos:
        return False
    update_trailing(pair, price)
    pos = s["pos"]

    if price <= pos["sl"]:
        t = do_sell(pair, price, "STOP-LOSS")
        if t:
            pnl = t["pnl"]
            send(ADMIN_ID,
                 f"⛔ <b>СТОП-ЛОСС | {pair['emoji']} {pair['name']}</b>\n"
                 f"Вход ${fmt(pos['entry'])} → Выход ${fmt(price)}\n"
                 f"P&L: <code>{sign(pnl)}${fmt(abs(pnl))}</code>\n"
                 f"Баланс: <b>${fmt(BOT_STATES[pair['symbol']]['usdt'])}</b>\n"
                 f"🕐 {ts()}")
        return True

    if price >= pos["tp"]:
        t = do_sell(pair, price, "TAKE-PROFIT")
        if t:
            pnl = t["pnl"]
            send(ADMIN_ID,
                 f"🎯 <b>ТЕЙК-ПРОФИТ | {pair['emoji']} {pair['name']}</b>\n"
                 f"Вход ${fmt(pos['entry'])} → Выход ${fmt(price)}\n"
                 f"P&L: <code>+${fmt(pnl)}</code>\n"
                 f"Баланс: <b>${fmt(BOT_STATES[pair['symbol']]['usdt'])}</b>\n"
                 f"🕐 {ts()}")
        return True

    return False


def circuit_breaker(sym):
    s     = BOT_STATES[sym]
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if s.get("day_date") != today:
        s["day_date"]  = today
        s["day_start"] = s["usdt"]
        s["halted"]    = False

    equity = s["usdt"]
    if s["day_start"] > 0:
        dd = (s["day_start"] - equity) / s["day_start"] * 100
        if dd > DAY_LOSS_PCT and not s.get("halted"):
            s["halted"]     = True
            s["halt_until"] = time.time() + 86400
            save_bot_state(sym, s)
            send(ADMIN_ID,
                 f"⛔ <b>CIRCUIT BREAKER — {sym}</b>\n"
                 f"Дневной убыток: {dd:.1f}%\n"
                 f"Торговля приостановлена на 24ч")
            return True

    peak = s.get("peak", equity)
    if peak > 0 and equity < peak:
        gdd = (peak - equity) / peak * 100
        if gdd > GLOBAL_DD and not s.get("halted"):
            s["halted"]     = True
            s["halt_until"] = time.time() + 86400 * 3
            save_bot_state(sym, s)
            send(ADMIN_ID,
                 f"🚨 <b>ГЛОБАЛЬНАЯ ЗАЩИТА — {sym}</b>\n"
                 f"Просадка от пика: {gdd:.1f}%\n"
                 f"Торговля остановлена на 3 дня!")
            return True

    if s.get("halted") and time.time() >= s.get("halt_until", 0):
        s["halted"]     = False
        s["halt_until"] = 0
        s["day_start"]  = s["usdt"]
        save_bot_state(sym, s)
        send(ADMIN_ID, f"✅ Торговля возобновлена: {sym}")

    save_bot_state(sym, s)
    return s.get("halted", False)

# ─── ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ──────────────────────────────────────────────────

def ts():
    return datetime.now(timezone.utc).strftime("%d.%m.%Y %H:%M UTC")

def fmt(v, spec=",.2f"):
    return format(float(v), spec)

def sign(v):
    return "+" if float(v) >= 0 else ""

def pct_val(profit, base):
    return (float(profit) / float(base) * 100) if float(base) > 0 else 0.0

def wr_calc(w, l):
    return round(w / max(w + l, 1) * 100)

# ─── ПОЛЬЗОВАТЕЛИ ─────────────────────────────────────────────────────────────

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


def _gen_ref():
    return "".join(random.choices(string.ascii_uppercase + string.digits, k=8))


def get_user(cid):
    users = load_users()
    uid   = str(cid)
    if uid not in users:
        users[uid] = {
            "id": uid, "name": "", "joined": ts(),
            "demo": {
                "balance": 1000.0, "start": 1000.0, "peak": 1000.0,
                "profit": 0.0, "trades": 0, "wins": 0, "loss": 0,
                "history": [], "streak_win": 0, "streak_loss": 0, "max_dd": 0.0,
            },
            "real": {
                "balance": 0.0, "deposited": 0.0, "peak": 0.0,
                "profit": 0.0, "trades": 0, "wins": 0, "loss": 0,
                "history": [], "active": False, "autocompound": True,
                "pending": 0.0, "pending_txid": "",
                "withdrawals": [], "streak_win": 0, "streak_loss": 0, "max_dd": 0.0,
            },
            "notify": True,
            "ref_code": _gen_ref(),
            "ref_by": None,
            "ref_count": 0,
            "ref_bonus": 0.0,
            "last_seen": ts(),
        }
        save_users(users)
    return users[uid]


def save_user(cid, u):
    users     = load_users()
    users[str(cid)] = u
    save_users(users)


def is_admin(cid):
    return str(cid) in [x.strip() for x in ADMIN_IDS.split(",") if x.strip()]

# ─── РАСПРЕДЕЛЕНИЕ ПРИБЫЛИ ────────────────────────────────────────────────────

def distribute(pair_name, pnl, is_win):
    users     = load_users()
    total_dep = sum(u["real"]["deposited"] for u in users.values() if u["real"]["active"])
    if total_dep <= 0 or pnl == 0:
        return
    for uid, u in users.items():
        r = u["real"]
        if not r["active"] or r["deposited"] <= 0:
            continue
        share    = r["deposited"] / total_dep
        user_pnl = round(pnl * share, 4)
        r["profit"]   += user_pnl
        r["balance"]  += user_pnl
        if r["autocompound"] and user_pnl > 0:
            r["deposited"] += user_pnl
        r["trades"]   += 1
        (r["wins"] if is_win else r["loss"]) 
        if is_win:
            r["wins"] += 1
        else:
            r["loss"] += 1
        r["history"].append({"pair": pair_name, "pnl": user_pnl, "time": ts()})
        if r["balance"] > r.get("peak", 0):
            r["peak"] = r["balance"]
        u["real"] = r
        users[uid] = u
        if u.get("notify") and user_pnl != 0:
            icon = "✅" if is_win else "❌"
            send(uid,
                 f"{icon} <b>{pair_name}</b> — сделка закрыта\n"
                 f"Ваш P&L: <code>{sign(user_pnl)}${fmt(abs(user_pnl))}</code>\n"
                 f"Баланс: <b>${fmt(r['balance'])}</b>")
    save_users(users)

# ─── TELEGRAM API ─────────────────────────────────────────────────────────────

def api(method, data=None):
    if not TOKEN:
        return {}
    url = f"https://api.telegram.org/bot{TOKEN}/{method}"
    try:
        r = requests.post(url, json=data or {}, timeout=15)
        return r.json()
    except Exception as e:
        logger.error("TG API %s: %s", method, e)
        return {}


def send(cid, text, buttons=None):
    if not TOKEN:
        return {}
    d = {"chat_id": str(cid), "text": text[:4096], "parse_mode": "HTML"}
    if buttons:
        d["reply_markup"] = json.dumps({"inline_keyboard": buttons})
    res = api("sendMessage", d)
    if not res.get("ok"):
        logger.warning("sendMessage %s: %s", cid, res.get("description", ""))
    return res


def answer_cb(cb_id, text=""):
    api("answerCallbackQuery", {"callback_query_id": cb_id, "text": text})

# ─── КНОПКИ ───────────────────────────────────────────────────────────────────

def kb_main():
    return [
        [{"text": "👤 Аккаунт",              "callback_data": "account"},
         {"text": "📊 Статистика бота",      "callback_data": "stats"}],
        [{"text": "🌐 Рынок сейчас",         "callback_data": "market"},
         {"text": "📈 Последние сделки",     "callback_data": "history"}],
        [{"text": "💰 Пополнить",            "callback_data": "deposit"},
         {"text": "💸 Вывести",             "callback_data": "withdraw"}],
        [{"text": "🤝 Реферальная программа","callback_data": "referral"}],
        [{"text": "❓ Как это работает",     "callback_data": "help"},
         {"text": "🔔 Уведомления",         "callback_data": "toggle_notify"}],
    ]

def kb_account():
    return [
        [{"text": "🎮 Демо-счёт",      "callback_data": "demo"},
         {"text": "💼 Реальный счёт",  "callback_data": "real"}],
        [{"text": "📊 Моя статистика", "callback_data": "my_stats"},
         {"text": "🏆 Лидерборд",      "callback_data": "leaderboard"}],
        [{"text": "💰 Пополнить",      "callback_data": "deposit"},
         {"text": "💸 Вывести",       "callback_data": "withdraw"}],
        [{"text": "🏠 Главное меню",   "callback_data": "menu"}],
    ]

def kb_deposit():
    return [
        [{"text": "$50",   "callback_data": "dep_50"},
         {"text": "$100",  "callback_data": "dep_100"},
         {"text": "$200",  "callback_data": "dep_200"}],
        [{"text": "$500",  "callback_data": "dep_500"},
         {"text": "$1000", "callback_data": "dep_1000"},
         {"text": "✏️ Своя","callback_data": "dep_custom"}],
        [{"text": "❌ Отмена","callback_data": "menu"}],
    ]

def kb_confirm_dep(amount):
    return [
        [{"text": "✅ Я отправил(а) платёж", "callback_data": f"depsent_{amount}"}],
        [{"text": "❌ Отмена",               "callback_data": "menu"}],
    ]

def kb_back():
    return [[{"text": "🏠 Главное меню", "callback_data": "menu"}]]

def kb_admin():
    return [
        [{"text": "👥 Пользователи",  "callback_data": "adm_users"},
         {"text": "📊 Статистика",    "callback_data": "adm_stats"}],
        [{"text": "💰 Депозиты",      "callback_data": "adm_deposits"},
         {"text": "💸 Выводы",       "callback_data": "adm_withdrawals"}],
        [{"text": "📢 Рассылка",      "callback_data": "adm_broadcast"},
         {"text": "📋 Все сделки",   "callback_data": "adm_trades"}],
        [{"text": "🏠 Главное меню", "callback_data": "menu"}],
    ]

# ─── ЭКРАНЫ ───────────────────────────────────────────────────────────────────

def screen_main(cid):
    try:
        user = get_user(cid)
        user["name"]     = user.get("name") or "Инвестор"
        user["last_seen"] = ts()
        save_user(cid, user)
        d     = user["demo"]
        r     = user["real"]
        d_pct = pct_val(d["balance"] - d["start"], d["start"])
        r_pct = pct_val(r["profit"], r["deposited"]) if r["deposited"] > 0 else 0.0
        ntf   = "🔔 вкл" if user.get("notify", True) else "🔕 выкл"
        s_real = "✅ Активен" if r["active"] else "⏸ Неактивен"
        open_p = active_positions()

        total_pnl   = sum(s.get("pnl", 0) for s in BOT_STATES.values())
        total_trade = sum(s.get("n", 0) for s in BOT_STATES.values())
        total_wins  = sum(s.get("wins", 0) for s in BOT_STATES.values())
        total_loss  = sum(s.get("loss", 0) for s in BOT_STATES.values())
        wr          = wr_calc(total_wins, total_loss)

        text = (
            "🤖 <b>CryptoBot Pro — ДЕМО РЕЖИМ</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"👋 Привет, <b>{user['name']}</b>!\n\n"
            f"🎮 Демо:     <b>${fmt(d['balance'])}</b>  <code>{sign(d_pct)}{d_pct:.1f}%</code>\n"
            f"💼 Реальный: <b>${fmt(r['balance'])}</b>  <code>{sign(r_pct)}{r_pct:.1f}%</code>\n"
            f"📌 Статус:   {s_real}\n\n"
            f"📡 <b>Бот сейчас:</b>\n"
            f"  Открытых позиций: {open_p}/{MAX_POS}\n"
            f"  Всего сделок:     {total_trade} (WR: {wr}%)\n"
            f"  Суммарный P&L:    <code>{sign(total_pnl)}${fmt(abs(total_pnl))}</code>\n\n"
            f"⚡ Стратегия: EMA+Supertrend+RSI+MACD | Bybit API\n"
            f"🔔 Уведомления: {ntf}"
        )
        send(cid, text, kb_main())
    except Exception as e:
        logger.error("screen_main %s: %s", cid, e)
        send(cid, "⚠️ Ошибка. Попробуйте: /start")


def screen_account(cid):
    user  = get_user(cid)
    d     = user["demo"]
    r     = user["real"]
    d_pct = pct_val(d["balance"] - d["start"], d["start"])
    r_pct = pct_val(r["profit"], r["deposited"]) if r["deposited"] > 0 else 0.0
    text  = (
        "👤 <b>Мой аккаунт</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "🎮 <b>Демо-счёт</b>\n"
        f"  Баланс:   <b>${fmt(d['balance'])}</b>\n"
        f"  Прибыль:  <code>{sign(d_pct)}{d_pct:.1f}%</code>\n"
        f"  Сделок:   {d['trades']}  W:{d['wins']} L:{d['loss']} ({wr_calc(d['wins'],d['loss'])}%)\n\n"
        "💼 <b>Реальный счёт</b>\n"
        f"  Внесено:  ${fmt(r['deposited'])}\n"
        f"  Баланс:   <b>${fmt(r['balance'])}</b>\n"
        f"  Прибыль:  <code>{sign(r_pct)}{r_pct:.1f}%</code>\n"
        f"  Сделок:   {r['trades']}  W:{r['wins']} L:{r['loss']} ({wr_calc(r['wins'],r['loss'])}%)\n"
        f"  Автореинвест: {'✅ вкл' if r.get('autocompound', True) else '❌ выкл'}\n"
        f"  Статус:   {'✅ Активен' if r['active'] else '⏸ Неактивен'}\n\n"
        f"🤝 Рефералов: {user.get('ref_count', 0)}  Реф.бонус: ${fmt(user.get('ref_bonus', 0))}\n"
        f"📅 С нами с: {user['joined']}"
    )
    send(cid, text, kb_account())


def screen_stats(cid):
    trades    = all_trades()
    sells     = [t for t in trades if t.get("side") == "SELL"]
    total_n   = len(sells)
    wins      = [t for t in sells if t.get("pnl", 0) >= 0]
    losses    = [t for t in sells if t.get("pnl", 0) < 0]
    total_pnl = sum(t.get("pnl", 0) for t in sells)
    win_avg   = sum(t["pnl"] for t in wins)   / max(len(wins), 1)
    loss_avg  = sum(t["pnl"] for t in losses) / max(len(losses), 1)
    wr        = wr_calc(len(wins), len(losses))
    rr        = abs(win_avg / loss_avg) if loss_avg != 0 else 0

    pos_text  = ""
    for pair in PAIRS:
        s   = BOT_STATES.get(pair["symbol"], {})
        pos = s.get("pos")
        if pos:
            pr = fetch_bybit_price(pair["symbol"]) or pos["entry"]
            fl = (pr - pos["entry"]) * pos["qty"]
            pos_text += (
                f"\n{pair['emoji']} {pair['name']}: вход ${fmt(pos['entry'])} | "
                f"Float: <code>{sign(fl)}${fmt(abs(fl))}</code>"
            )

    text = (
        "📊 <b>Статистика бота (ДЕМО)</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"  Всего сделок:    {total_n}\n"
        f"  Победных:        {len(wins)} ({wr}%)\n"
        f"  Убыточных:       {len(losses)}\n"
        f"  Суммарный P&L:   <code>{sign(total_pnl)}${fmt(abs(total_pnl))}</code>\n"
        f"  Средний выигрыш: ${fmt(win_avg)}\n"
        f"  Средний убыток:  ${fmt(abs(loss_avg))}\n"
        f"  R:R отношение:   {rr:.2f}\n\n"
        "<b>Баланс по парам:</b>\n"
    )
    for pair in PAIRS:
        s = BOT_STATES.get(pair["symbol"], {})
        text += f"  {pair['emoji']} {pair['name']}: ${fmt(s.get('usdt', 10000))}  ({s.get('wins',0)}W/{s.get('loss',0)}L)\n"

    if pos_text:
        text += f"\n<b>Открытые позиции:</b>{pos_text}"

    send(cid, text, kb_back())


def screen_market(cid):
    text = "🌐 <b>Рынок сейчас (Bybit)</b>\n━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
    for pair in PAIRS:
        price = fetch_bybit_price(pair["symbol"])
        s     = BOT_STATES.get(pair["symbol"], {})
        pos   = s.get("pos")
        pos_txt = ""
        if pos and price:
            fl     = (price - pos["entry"]) * pos["qty"]
            fl_pct = (price - pos["entry"]) / pos["entry"] * 100
            pos_txt = (
                f"\n  📍 Позиция: вход <b>${fmt(pos['entry'])}</b>"
                f"\n  💰 Float P&L: <code>{sign(fl)}${fmt(abs(fl))} ({sign(fl_pct)}{fl_pct:.1f}%)</code>"
                f"\n  🛡 Стоп: ${fmt(pos['sl'])} | 🎯 TP: ${fmt(pos['tp'])}"
            )
        price_txt = f"${fmt(price)}" if price else "нет данных"
        text += f"{pair['emoji']} <b>{pair['name']}</b>  {price_txt}{pos_txt}\n\n"
    text += "⏱ Анализ каждый час | Данные: Bybit"
    send(cid, text, kb_back())


def screen_history(cid):
    trades = all_trades()
    sells  = [t for t in trades if t.get("side") == "SELL"][-10:]
    if not sells:
        send(cid, "📈 <b>Последние сделки</b>\n\nСделок пока нет — бот ищет точку входа...", kb_back())
        return
    text = "📈 <b>Последние 10 сделок (ДЕМО)</b>\n━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
    for t in reversed(sells):
        pnl  = t.get("pnl", 0)
        icon = "✅" if pnl >= 0 else "❌"
        text += (
            f"{icon} <b>{t.get('pair','?')}</b> — {t.get('reason','SIGNAL')}\n"
            f"  Вход: ${fmt(t.get('entry',0))} → Выход: ${fmt(t.get('price',0))}\n"
            f"  P&L: <code>{sign(pnl)}${fmt(abs(pnl))}</code> | {t.get('time','')}\n\n"
        )
    send(cid, text, kb_back())


def screen_demo(cid):
    user  = get_user(cid)
    d     = user["demo"]
    d_pct = pct_val(d["balance"] - d["start"], d["start"])
    text  = (
        "🎮 <b>Демо-счёт</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"  Стартовый: $1,000.00\n"
        f"  Текущий:   <b>${fmt(d['balance'])}</b>\n"
        f"  Прибыль:   <code>{sign(d_pct)}{d_pct:.1f}%</code>\n"
        f"  Сделок:    {d['trades']} (WR: {wr_calc(d['wins'],d['loss'])}%)\n"
    )
    send(cid, text, kb_back())


def screen_real(cid):
    user  = get_user(cid)
    r     = user["real"]
    r_pct = pct_val(r["profit"], r["deposited"]) if r["deposited"] > 0 else 0.0
    text  = (
        "💼 <b>Реальный счёт</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"  Внесено:    <b>${fmt(r['deposited'])}</b>\n"
        f"  Баланс:     <b>${fmt(r['balance'])}</b>\n"
        f"  Прибыль:    <code>{sign(r_pct)}{r_pct:.1f}%</code>\n"
        f"  Сделок:     {r['trades']} (WR: {wr_calc(r['wins'],r['loss'])}%)\n"
        f"  Автореинвест: {'✅ вкл' if r.get('autocompound', True) else '❌ выкл'}\n"
        f"  Статус:     {'✅ Активен' if r['active'] else '⏸ Неактивен'}\n\n"
        "💡 Пополните счёт, чтобы бот торговал за вас!"
    )
    send(cid, text, kb_account())


def screen_deposit(cid):
    send(cid,
         "💰 <b>Пополнение счёта</b>\n\n"
         "Выберите сумму или введите свою:\n"
         "(Минимум $50 USDT TRC-20)",
         kb_deposit())


def screen_confirm_dep(cid, amount):
    STATES[str(cid)] = "await_txid"
    send(cid,
         f"💰 <b>Пополнение на ${amount}</b>\n\n"
         f"Отправьте <b>{amount} USDT</b> (TRC-20) на адрес:\n\n"
         f"<code>{WALLET}</code>\n\n"
         f"После отправки нажмите кнопку ниже:",
         kb_confirm_dep(amount))


def screen_deposit_sent(cid, amount):
    STATES[str(cid)] = f"txid_{amount}"
    send(cid, "🔍 Введите TxID транзакции для подтверждения:", kb_back())


def screen_withdraw(cid):
    user = get_user(cid)
    r    = user["real"]
    if r["balance"] < 10:
        send(cid, "❌ Минимальный баланс для вывода: $10", kb_back())
        return
    STATES[str(cid)] = "await_withdraw"
    send(cid,
         f"💸 <b>Вывод средств</b>\n\n"
         f"Доступно: <b>${fmt(r['balance'])}</b>\n\n"
         f"Введите сумму для вывода (мин. $10):",
         kb_back())


def screen_referral(cid):
    user = get_user(cid)
    code = user.get("ref_code", _gen_ref())
    refs = load_refs()
    refs[code] = str(cid)
    save_refs(refs)
    text = (
        "🤝 <b>Реферальная программа</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "Приглашайте друзей и получайте <b>5%</b> от их прибыли!\n\n"
        f"👥 Приглашено: {user.get('ref_count', 0)}\n"
        f"💰 Ваш бонус:  ${fmt(user.get('ref_bonus', 0))}\n\n"
        f"🔗 Ваш реф-код: <code>{code}</code>\n\n"
        "Отправьте этот код другу — он введёт его при старте!"
    )
    send(cid, text, kb_back())


def screen_help(cid):
    send(cid,
         "❓ <b>Как работает CryptoBot Pro?</b>\n"
         "━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
         "🔬 <b>Стратегия (проверена на реальном рынке):</b>\n"
         "Бот анализирует BTC, ETH, SOL каждый час,\n"
         "используя профессиональные индикаторы:\n"
         "  • EMA (9/21/50) — тренд\n"
         "  • Supertrend — сила направления\n"
         "  • RSI (14) — перекупленность\n"
         "  • MACD — разворот тренда\n"
         "  • Объём — подтверждение входа\n"
         "  • 4H таймфрейм — фильтр мажора\n\n"
         "📊 <b>Риск-менеджмент:</b>\n"
         "  • 2% капитала на каждую сделку\n"
         "  • Стоп-лосс: ATR × 1.5\n"
         "  • Тейк-профит: ATR × 3 (R:R = 1:2)\n"
         "  • Trailing stop — защита прибыли\n"
         "  • Circuit-breaker при -7% за день\n"
         "  • Глобальная защита при -18% от пика\n\n"
         "🎯 <b>Цель: 70-100% годовых</b>\n"
         "📡 <b>Данные: Bybit Real-Time API</b>\n\n"
         "💰 Пополните реальный счёт и начните зарабатывать!",
         kb_back())


def screen_leaderboard(cid):
    users = load_users()
    rows  = []
    for uid, u in users.items():
        r = u["real"]
        if r["deposited"] > 0:
            p = pct_val(r["profit"], r["deposited"])
            rows.append((uid, u.get("name") or "Инвестор", p, r["balance"]))
    rows.sort(key=lambda x: x[2], reverse=True)
    medals = ["🥇", "🥈", "🥉", "4️⃣", "5️⃣"]
    text   = "🏆 <b>Лидерборд</b>\n━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
    if not rows:
        text += "Пока нет активных инвесторов"
    else:
        for i, (uid, name, p, bal) in enumerate(rows[:10]):
            med = medals[i] if i < 5 else f"{i+1}."
            me  = " ← вы" if uid == str(cid) else ""
            text += f"{med} <b>{name}</b>  {sign(p)}{p:.1f}%  (${fmt(bal)}){me}\n"
    send(cid, text, kb_back())


def screen_my_stats(cid):
    user = get_user(cid)
    d    = user["demo"]
    r    = user["real"]
    text = (
        "📊 <b>Моя статистика</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "🎮 <b>Демо-счёт</b>\n"
        f"  Winrate:  {wr_calc(d['wins'], d['loss'])}%\n"
        f"  Просадка: {d.get('max_dd', 0):.1f}%\n"
        f"  Сделок:   {d['trades']}\n\n"
        "💼 <b>Реальный счёт</b>\n"
        f"  Winrate:  {wr_calc(r['wins'], r['loss'])}%\n"
        f"  Просадка: {r.get('max_dd', 0):.1f}%\n"
        f"  Сделок:   {r['trades']}\n"
        f"  Прибыль:  ${fmt(r['profit'])}\n\n"
        f"📅 С нами с: {user['joined']}"
    )
    send(cid, text, kb_back())

# ─── ADMIN ────────────────────────────────────────────────────────────────────

def screen_adm_users(cid):
    users     = load_users()
    active    = sum(1 for u in users.values() if u["real"]["active"])
    total_dep = sum(u["real"]["deposited"] for u in users.values())
    total_prf = sum(u["real"]["profit"] for u in users.values())
    text      = (
        "👥 <b>Пользователи</b>\n"
        f"  Всего:         {len(users)}\n"
        f"  Активных:      {active}\n"
        f"  Сумма депо:    ${fmt(total_dep)}\n"
        f"  Сумма прибыли: ${fmt(total_prf)}"
    )
    send(cid, text, kb_admin())


def screen_adm_stats(cid):
    trades    = all_trades()
    sells     = [t for t in trades if t.get("side") == "SELL"]
    wins      = sum(1 for t in sells if t.get("pnl", 0) >= 0)
    total_pnl = sum(t.get("pnl", 0) for t in sells)
    text      = (
        "📊 <b>Статистика бота</b>\n"
        f"  Сделок:    {len(sells)}\n"
        f"  Побед:     {wins} ({wr_calc(wins, max(len(sells)-wins,0))}%)\n"
        f"  Общий P&L: {sign(total_pnl)}${fmt(abs(total_pnl))}\n\n"
        "<b>По парам:</b>\n"
    )
    for pair in PAIRS:
        s    = BOT_STATES.get(pair["symbol"], {})
        text += f"  {pair['emoji']} {pair['name']}: ${fmt(s.get('usdt', 10000))}  ({s.get('wins',0)}W/{s.get('loss',0)}L)\n"
    send(cid, text, kb_admin())


def screen_adm_deposits(cid):
    users = load_users()
    rows  = [(u.get("name","?"), u["real"]["deposited"]) for u in users.values() if u["real"]["deposited"] > 0]
    rows.sort(key=lambda x: x[1], reverse=True)
    text  = "💰 <b>Депозиты</b>\n"
    for name, dep in rows[:15]:
        text += f"  {name}: ${fmt(dep)}\n"
    if not rows:
        text += "Нет депозитов"
    send(cid, text, kb_admin())


def screen_adm_withdrawals(cid):
    users = load_users()
    rows  = []
    for u in users.values():
        for w in u["real"].get("withdrawals", []):
            rows.append((u.get("name","?"), w))
    rows.sort(key=lambda x: x[1].get("time",""), reverse=True)
    text = "💸 <b>Выводы</b>\n"
    for name, w in rows[:15]:
        text += f"  {name}: ${fmt(w.get('amount',0))} — {w.get('status','?')} {w.get('time','')}\n"
    if not rows:
        text += "Нет запросов"
    send(cid, text, kb_admin())


def screen_adm_trades(cid):
    trades = all_trades()
    sells  = [t for t in trades if t.get("side") == "SELL"][-15:]
    text   = "📋 <b>Последние сделки</b>\n"
    for t in reversed(sells):
        pnl  = t.get("pnl", 0)
        icon = "✅" if pnl >= 0 else "❌"
        text += f"{icon} {t.get('pair','?')} | {sign(pnl)}${fmt(abs(pnl))} | {t.get('reason','?')} | {t.get('time','')}\n"
    if not sells:
        text += "Сделок пока нет"
    send(cid, text, kb_admin())


STATES = {}

def adm_broadcast(cid):
    STATES[str(cid)] = "broadcast"
    send(cid, "📢 Введите текст для рассылки:", kb_back())


def do_broadcast(text):
    users = load_users()
    ok = fail = 0
    for uid in users:
        res = send(uid, "📢 <b>Сообщение от CryptoBot Pro:</b>\n\n" + text)
        if res.get("ok"):
            ok += 1
        else:
            fail += 1
        time.sleep(0.1)
    send(ADMIN_ID, f"📢 Рассылка: {ok} доставлено, {fail} ошибок")


def weekly_report():
    users     = load_users()
    trades    = all_trades()
    sells     = [t for t in trades if t.get("side") == "SELL"]
    wins      = sum(1 for t in sells if t.get("pnl", 0) >= 0)
    total_pnl = sum(t.get("pnl", 0) for t in sells)
    msg = (
        "📊 <b>Еженедельный отчёт</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"Сделок за период: {len(sells)}\n"
        f"Winrate: {wr_calc(wins, max(len(sells)-wins,0))}%\n"
        f"P&L бота: {sign(total_pnl)}${fmt(abs(total_pnl))}\n\n"
        "Бот продолжает работу 24/7. Удачи! 🚀"
    )
    for uid, u in users.items():
        if u.get("notify", True):
            send(uid, msg)
            time.sleep(0.05)

# ─── ОБРАБОТКА СООБЩЕНИЙ ──────────────────────────────────────────────────────

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


def handle_message(msg):
    cid  = str(msg.get("chat", {}).get("id", ""))
    text = msg.get("text", "").strip()
    name = msg.get("from", {}).get("first_name", "")
    if not cid:
        return

    user = get_user(cid)
    if name and user.get("name") != name:
        user["name"] = name
        save_user(cid, user)

    if text.startswith("/start ref_"):
        ref_code = text.split("ref_")[1].strip()
        refs     = load_refs()
        if ref_code in refs and not user.get("ref_by"):
            referrer_id = refs[ref_code]
            user["ref_by"] = referrer_id
            save_user(cid, user)
            referrer = get_user(referrer_id)
            referrer["ref_count"] = referrer.get("ref_count", 0) + 1
            save_user(referrer_id, referrer)
            send(referrer_id, f"🎉 Новый реферал! {name} присоединился!")
        screen_main(cid)
        return

    if text in ("/start", "/menu"):
        screen_main(cid)
        return

    if text == "/admin" and is_admin(cid):
        send(cid, "🔑 <b>Панель администратора</b>", kb_admin())
        return

    state = STATES.get(cid, "")

    if state == "broadcast" and is_admin(cid):
        STATES.pop(cid, None)
        do_broadcast(text)
        return

    if state.startswith("txid_"):
        amount = float(state.split("_")[1])
        txid   = text.strip()
        STATES.pop(cid, None)
        r = user["real"]
        r["pending"]      = amount
        r["pending_txid"] = txid
        user["real"] = r
        save_user(cid, user)
        send(cid,
             f"✅ Запрос на пополнение ${fmt(amount)} получен!\n"
             f"TxID: <code>{txid}</code>\n\nОжидайте подтверждения (~30 мин).",
             kb_back())
        send(ADMIN_ID,
             f"💰 <b>Запрос на пополнение</b>\n"
             f"Пользователь: {name} ({cid})\n"
             f"Сумма: ${fmt(amount)}\n"
             f"TxID: <code>{txid}</code>\n"
             f"Команда: /confirm_{cid}_{amount}")
        return

    if state == "await_withdraw":
        STATES.pop(cid, None)
        try:
            amount = float(text.replace("$", "").replace(",", ".").strip())
        except ValueError:
            send(cid, "❌ Неверная сумма", kb_back())
            return
        r = user["real"]
        if amount < 10 or amount > r["balance"]:
            send(cid, f"❌ Сумма: от $10 до ${fmt(r['balance'])}", kb_back())
            return
        STATES[cid] = f"withdraw_addr_{amount}"
        send(cid, f"💸 Вывод ${fmt(amount)}\n\nВведите ваш USDT TRC-20 адрес:")
        return

    if state.startswith("withdraw_addr_"):
        amount = float(state.split("_")[-1])
        addr   = text.strip()
        STATES.pop(cid, None)
        r = user["real"]
        r["balance"] -= amount
        r["withdrawals"].append({
            "amount": amount, "addr": addr,
            "status": "⏳ Ожидает", "time": ts()
        })
        user["real"] = r
        save_user(cid, user)
        send(cid,
             f"✅ Запрос на вывод ${fmt(amount)} создан!\n"
             f"Адрес: <code>{addr}</code>\n\nОбработка в течение 24ч.",
             kb_back())
        send(ADMIN_ID,
             f"💸 <b>Запрос на вывод</b>\n"
             f"Пользователь: {name} ({cid})\n"
             f"Сумма: ${fmt(amount)}\n"
             f"Адрес: <code>{addr}</code>\n"
             f"Команда: /pay_{cid}_{amount}")
        return

    if state == "dep_custom":
        STATES.pop(cid, None)
        try:
            amount = float(text.replace("$", "").replace(",", ".").strip())
        except ValueError:
            send(cid, "❌ Введите сумму числом", kb_back())
            return
        if amount < 50:
            send(cid, "❌ Минимум: $50", kb_back())
            return
        screen_confirm_dep(cid, amount)
        return

    if is_admin(cid):
        if text.startswith("/confirm_"):
            parts = text.split("_")
            if len(parts) >= 3:
                target_id = parts[1]
                amount    = float(parts[2])
                target    = get_user(target_id)
                r         = target["real"]
                r["balance"]   += amount
                r["deposited"] += amount
                r["active"]     = True
                r["pending"]    = 0
                if r["balance"] > r.get("peak", 0):
                    r["peak"] = r["balance"]
                target["real"] = r
                save_user(target_id, target)
                send(target_id, f"✅ Счёт пополнен на ${fmt(amount)}!\nБаланс: ${fmt(r['balance'])}")
                send(cid, f"✅ Подтверждено для {target_id}")
            return

        if text.startswith("/pay_"):
            parts = text.split("_")
            if len(parts) >= 3:
                target_id = parts[1]
                amount    = float(parts[2])
                target    = get_user(target_id)
                r         = target["real"]
                for w in r["withdrawals"]:
                    if w.get("status") == "⏳ Ожидает" and abs(w.get("amount", 0) - amount) < 0.01:
                        w["status"] = "✅ Выплачено"
                        break
                target["real"] = r
                save_user(target_id, target)
                send(target_id, f"✅ Вывод ${fmt(amount)} выплачен!")
                send(cid, f"✅ Выплата подтверждена для {target_id}")
            return

    screen_main(cid)


def handle_callback(cb):
    cid   = str(cb.get("message", {}).get("chat", {}).get("id", ""))
    data  = cb.get("data", "")
    cb_id = cb.get("id", "")
    if not cid:
        return
    answer_cb(cb_id)

    routes = {
        "menu":         screen_main,
        "account":      screen_account,
        "stats":        screen_stats,
        "market":       screen_market,
        "history":      screen_history,
        "demo":         screen_demo,
        "real":         screen_real,
        "deposit":      screen_deposit,
        "withdraw":     screen_withdraw,
        "referral":     screen_referral,
        "help":         screen_help,
        "leaderboard":  screen_leaderboard,
        "my_stats":     screen_my_stats,
    }

    if data in routes:
        routes[data](cid)
    elif data == "toggle_notify":
        user = get_user(cid)
        user["notify"] = not user.get("notify", True)
        save_user(cid, user)
        send(cid, f"🔔 Уведомления {'включены' if user['notify'] else 'выключены'}", kb_back())
    elif data.startswith("dep_"):
        val = data[4:]
        if val == "custom":
            STATES[cid] = "dep_custom"
            send(cid, "✏️ Введите сумму в USDT (минимум $50):", kb_back())
        else:
            screen_confirm_dep(cid, int(val))
    elif data.startswith("depsent_"):
        amount = float(data.split("_")[1])
        screen_deposit_sent(cid, amount)
    elif data == "adm_users"       and is_admin(cid): screen_adm_users(cid)
    elif data == "adm_stats"       and is_admin(cid): screen_adm_stats(cid)
    elif data == "adm_deposits"    and is_admin(cid): screen_adm_deposits(cid)
    elif data == "adm_withdrawals" and is_admin(cid): screen_adm_withdrawals(cid)
    elif data == "adm_trades"      and is_admin(cid): screen_adm_trades(cid)
    elif data == "adm_broadcast"   and is_admin(cid): adm_broadcast(cid)

# ─── ГЛАВНЫЙ ЦИКЛ ─────────────────────────────────────────────────────────────

LAST_UPD_ID = 0


def poll_telegram():
    global LAST_UPD_ID
    res = api("getUpdates", {"offset": LAST_UPD_ID + 1, "timeout": 25, "limit": 50})
    if not res.get("ok"):
        return
    for upd in res.get("result", []):
        LAST_UPD_ID = upd["update_id"]
        if "message" in upd:
            handle_message(upd["message"])
        elif "callback_query" in upd:
            handle_callback(upd["callback_query"])


def run():
    global BOT_STATES
    logger.info("=" * 60)
    logger.info("CryptoBot Pro v4 — СТАРТ")
    logger.info("Стратегия: EMA+Supertrend+RSI+MACD | Данные: Bybit API")
    logger.info("=" * 60)

    if not TOKEN:
        logger.error("TELEGRAM_BOT_TOKEN не задан!")
        sys.exit(1)

    for pair in PAIRS:
        BOT_STATES[pair["symbol"]] = load_bot_state(pair["symbol"])

    send(ADMIN_ID,
         "🚀 <b>CryptoBot Pro v4 ЗАПУЩЕН</b>\n"
         "━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
         "📊 Стратегия: EMA + Supertrend + RSI + MACD\n"
         "📡 Данные: Bybit Real-Time API\n"
         "⏱ Анализ: каждые 60 минут\n"
         "🎯 Цель: 70-100% годовых\n"
         "💼 Режим: ДЕМО (реальные цены, виртуальные сделки)\n\n"
         "Бот начинает анализировать рынок...\n"
         "/start — открыть меню")

    last_trade_check = 0
    last_report      = time.time()
    check_num        = 0

    logger.info("Telegram polling активен.")

    while True:
        try:
            poll_telegram()

            now = time.time()
            if now - last_trade_check >= TRADE_INT:
                last_trade_check = now
                check_num       += 1
                logger.info("─── Торговый цикл #%d ───", check_num)

                for pair in PAIRS:
                    try:
                        sym = pair["symbol"]
                        if circuit_breaker(sym):
                            continue

                        df = fetch_bybit_klines(sym, interval="60", limit=200)
                        if df is None or len(df) < 60:
                            logger.warning("%s: мало данных", sym)
                            continue

                        df    = calc_indicators(df)
                        price = fetch_bybit_price(sym)
                        if price is None:
                            continue

                        c   = df.iloc[-1]
                        atr = c["atr"]

                        logger.info(
                            "%s $%.2f | EMA↑=%s ST=%s RSI=%.1f MACD=%+.4f",
                            sym, price,
                            c["ema_fast"] > c["ema_mid"] > c["ema_slow"],
                            c["st_dir"] == 1,
                            c["rsi"], c["macd_h"],
                        )

                        s = BOT_STATES[sym]

                        if s.get("pos") and check_exits(pair, price):
                            continue

                        trend_4h = get_4h_trend(sym)
                        sig      = get_signal(df, trend_4h)

                        if sig == "BUY":
                            t = do_buy(pair, price, atr)
                            if t:
                                logger.info("%s ПОКУПКА @ $%.2f  SL=$%.2f TP=$%.2f",
                                            sym, price, t["sl"], t["tp"])
                                send(ADMIN_ID,
                                     f"📈 <b>ВХОД | {pair['emoji']} {pair['name']}</b>\n"
                                     f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
                                     f"💵 Цена входа:   <b>${fmt(price)}</b>\n"
                                     f"🛡 Стоп-лосс:    <b>${fmt(t['sl'])}</b>\n"
                                     f"🎯 Тейк-профит:  <b>${fmt(t['tp'])}</b>\n"
                                     f"📊 RSI: {c['rsi']:.1f} | "
                                     f"Supertrend: {'🟢 Бычий' if c['st_dir']==1 else '🔴 Медвежий'}\n"
                                     f"📈 Тренд 4H: {'🟢 Бычий' if trend_4h==1 else '⚪ Нейтральный'}\n"
                                     f"💰 Риск: 2% | R:R = 1:2\n"
                                     f"🕐 {ts()}")

                        elif sig == "SELL" and s.get("pos"):
                            t = do_sell(pair, price, "SIGNAL")
                            if t:
                                pnl  = t["pnl"]
                                icon = "✅" if pnl >= 0 else "❌"
                                logger.info("%s ВЫХОД @ $%.2f  PnL=$%.2f", sym, price, pnl)
                                send(ADMIN_ID,
                                     f"{icon} <b>ВЫХОД | {pair['emoji']} {pair['name']}</b>\n"
                                     f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
                                     f"Вход: ${fmt(t['entry'])} → Выход: ${fmt(price)}\n"
                                     f"P&L: <code>{sign(pnl)}${fmt(abs(pnl))}</code>\n"
                                     f"Баланс бота: <b>${fmt(BOT_STATES[sym]['usdt'])}</b>\n"
                                     f"🕐 {ts()}")

                    except Exception as e:
                        logger.error("Цикл %s: %s", pair["symbol"], e)
                    time.sleep(2)

                if check_num % 24 == 0:
                    screen_stats(ADMIN_ID)

            if now - last_report >= 86400 * 7:
                last_report = now
                weekly_report()

        except KeyboardInterrupt:
            logger.info("Остановка...")
            break
        except Exception as e:
            logger.error("Главный цикл: %s", e)
            time.sleep(10)

        time.sleep(CMD_INT)


if __name__ == "__main__":
    run()
