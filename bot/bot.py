import os, sys, time, json, random, string, logging, requests
import pandas as pd
import numpy as np
from datetime import datetime, timezone
from pathlib import Path
import threading

logging.basicConfig(
level=logging.INFO,
format=”%(asctime)s [%(levelname)s] %(message)s”,
handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(**name**)

TOKEN        = os.environ.get(“TELEGRAM_BOT_TOKEN”, “”)
ADMIN_ID     = os.environ.get(“TELEGRAM_CHAT_ID”, “”)
ADMIN_IDS    = os.environ.get(“ADMIN_IDS”, ADMIN_ID)
WALLET       = os.environ.get(“USDT_WALLET”, “ZADAYTE_USDT_WALLET”)
BYBIT_KEY    = os.environ.get(“BYBIT_API_KEY”, “”)
BYBIT_SECRET = os.environ.get(“BYBIT_API_SECRET”, “”)
USE_TESTNET  = os.environ.get(“BYBIT_TESTNET”, “true”).lower() == “true”
LEVERAGE     = int(os.environ.get(“BYBIT_LEVERAGE”, “3”))
LIVE_MODE    = bool(BYBIT_KEY and BYBIT_SECRET)

DATA_DIR    = Path(“data”)
DATA_DIR.mkdir(exist_ok=True)
USERS_FILE  = DATA_DIR / “users.json”
TRADES_FILE = DATA_DIR / “bot_trades.jsonl”

PAIRS = [
{“symbol”: “BTCUSDT”, “name”: “BTC”, “emoji”: “BTC”, “min_qty”: 0.001},
{“symbol”: “ETHUSDT”, “name”: “ETH”, “emoji”: “ETH”, “min_qty”: 0.01},
{“symbol”: “SOLUSDT”, “name”: “SOL”, “emoji”: “SOL”, “min_qty”: 0.1},
]

DEMO_COINS = [
{“symbol”: “BTCUSDT”, “name”: “Bitcoin”,  “short”: “BTC”, “cg_id”: “bitcoin”},
{“symbol”: “ETHUSDT”, “name”: “Ethereum”, “short”: “ETH”, “cg_id”: “ethereum”},
{“symbol”: “SOLUSDT”, “name”: “Solana”,   “short”: “SOL”, “cg_id”: “solana”},
]

# Индикаторы

EMA_MID      = 21
EMA_SLOW     = 50
RSI_PERIOD   = 14
ATR_PERIOD   = 14
ATR_SL_MULT  = 1.5
ATR_TP_MULT  = 2.5
ST_MULT      = 3.0
ST_PERIOD    = 10
MACD_FAST    = 12
MACD_SLOW    = 26
MACD_SIG     = 9
RISK_PCT     = 1.5
MAX_POS      = 3
DAY_LOSS_PCT = 5.0
GLOBAL_DD    = 15.0
DEMO_LEVERAGE = 2

# Интервалы

TRADE_INT    = 14400   # 4 часа для реального
DEMO_INT     = 900     # 15 минут для демо
STATUS_INT   = 1800    # 30 минут статус
CMD_INT      = 3

STATES       = {}
BOT_STATES   = {}
PENDING      = {}

def now_str():
return datetime.now(timezone.utc).strftime(”%d.%m.%Y %H:%M UTC”)

def fmt(v, spec=”,.2f”):
try:
return format(float(v), spec)
except Exception:
return str(v)

def sign(v):
return “+” if float(v) >= 0 else “”

def pct_val(p, b):
return float(p) / float(b) * 100 if float(b) > 0 else 0.0

def wr_calc(w, l):
return round(w / max(w + l, 1) * 100)

def gen_ref():
return “”.join(random.choices(string.ascii_uppercase + string.digits, k=8))

# ── Users ──────────────────────────────────────────────────────────────────────

def load_users():
if USERS_FILE.exists():
try:
with open(USERS_FILE, encoding=“utf-8”) as f:
return json.load(f)
except Exception:
pass
return {}

def save_users(u):
with open(USERS_FILE, “w”, encoding=“utf-8”) as f:
json.dump(u, f, indent=2, ensure_ascii=False, default=str)

def get_user(cid):
users = load_users()
uid   = str(cid)
if uid not in users:
users[uid] = {
“id”: uid, “name”: “”, “joined”: now_str(),
“demo”: {
“balance”: 1000.0, “start”: 1000.0,
“profit”: 0.0, “trades”: 0, “wins”: 0, “loss”: 0,
“history”: [], “positions”: [],
},
“real”: {
“balance”: 0.0, “deposited”: 0.0,
“profit”: 0.0, “trades”: 0, “wins”: 0, “loss”: 0,
“history”: [], “active”: False,
“pending”: 0.0, “pending_txid”: “”,
“withdrawals”: [],
},
“notify”: True,
“ref_code”: gen_ref(),
}
save_users(users)
else:
# Миграция старых пользователей
if “positions” not in users[uid][“demo”]:
users[uid][“demo”][“positions”] = []
save_users(users)
return users[uid]

def save_user(cid, u):
users = load_users()
users[str(cid)] = u
save_users(users)

def is_admin(cid):
return str(cid) in [x.strip() for x in ADMIN_IDS.split(”,”) if x.strip()]

# ── Bot trading state ──────────────────────────────────────────────────────────

def load_bot_state(sym):
f = DATA_DIR / (sym + “_state.json”)
if f.exists():
try:
with open(f, encoding=“utf-8”) as fp:
return json.load(fp)
except Exception:
pass
return {
“usdt”: 10000.0, “n”: 0, “wins”: 0, “loss”: 0, “pnl”: 0.0,
“peak”: 10000.0, “day_start”: 10000.0, “day_date”: “”,
“halted”: False, “halt_until”: 0, “pos”: None,
}

def save_bot_state(sym, s):
with open(DATA_DIR / (sym + “_state.json”), “w”, encoding=“utf-8”) as f:
json.dump(s, f, indent=2, ensure_ascii=False, default=str)

def log_trade(t):
with open(TRADES_FILE, “a”, encoding=“utf-8”) as f:
f.write(json.dumps(t, ensure_ascii=False, default=str) + “\n”)

def all_trades():
trades = []
if TRADES_FILE.exists():
with open(TRADES_FILE, encoding=“utf-8”) as f:
for line in f:
l = line.strip()
if l:
try:
trades.append(json.loads(l))
except Exception:
pass
return trades

def active_positions():
return sum(1 for s in BOT_STATES.values() if s.get(“pos”))

# ── Market data ────────────────────────────────────────────────────────────────

def fetch_klines(symbol, interval=“240”, limit=200):
try:
r = requests.get(
“https://api.bybit.com/v5/market/kline”,
params={“category”: “linear”, “symbol”: symbol,
“interval”: interval, “limit”: limit},
timeout=15,
)
r.raise_for_status()
data = r.json()
if data.get(“retCode”) != 0:
return None
rows = data[“result”][“list”]
df   = pd.DataFrame(rows, columns=[“ts”,“op”,“hi”,“lo”,“cl”,“vol”,“turnover”])
df   = df.astype({c: float for c in df.columns})
df.sort_values(“ts”, inplace=True)
df.reset_index(drop=True, inplace=True)
return df
except Exception as e:
logger.error(“fetch_klines %s: %s”, symbol, e)
return None

def fetch_price(symbol):
try:
r = requests.get(
“https://api.bybit.com/v5/market/tickers”,
params={“category”: “linear”, “symbol”: symbol},
timeout=10,
)
lst = r.json().get(“result”, {}).get(“list”, [])
if lst:
return float(lst[0][“lastPrice”])
except Exception as e:
logger.error(“fetch_price %s: %s”, symbol, e)
return None

def fetch_demo_price(symbol, cg_id=None):
# Bybit spot
try:
r = requests.get(
“https://api.bybit.com/v5/market/tickers”,
params={“category”: “spot”, “symbol”: symbol},
timeout=8,
)
lst = r.json().get(“result”, {}).get(“list”, [])
if lst:
return float(lst[0][“lastPrice”]), “Bybit”
except Exception:
pass
# OKX
try:
inst = symbol.replace(“USDT”, “-USDT”)
r = requests.get(
“https://www.okx.com/api/v5/market/ticker”,
params={“instId”: inst}, timeout=8,
)
data = r.json().get(“data”, [])
if data:
return float(data[0][“last”]), “OKX”
except Exception:
pass
# CoinGecko
if cg_id:
try:
r = requests.get(
“https://api.coingecko.com/api/v3/simple/price”,
params={“ids”: cg_id, “vs_currencies”: “usd”},
timeout=10,
)
p = r.json().get(cg_id, {}).get(“usd”)
if p:
return float(p), “CoinGecko”
except Exception:
pass
return None, None

def demo_coin_by_symbol(symbol):
for c in DEMO_COINS:
if c[“symbol”] == symbol:
return c
return None

# ── Indicators ─────────────────────────────────────────────────────────────────

def calc_indicators(df):
df = df.copy()
df[“ema_mid”]  = df[“cl”].ewm(span=EMA_MID,  adjust=False).mean()
df[“ema_slow”] = df[“cl”].ewm(span=EMA_SLOW, adjust=False).mean()

```
hl = df["hi"] - df["lo"]
hc = (df["hi"] - df["cl"].shift()).abs()
lc = (df["lo"] - df["cl"].shift()).abs()
tr = pd.concat([hl, hc, lc], axis=1).max(axis=1)
df["atr"] = tr.ewm(span=ATR_PERIOD, adjust=False).mean()

# Supertrend
hl2   = (df["hi"] + df["lo"]) / 2
upper = hl2 + ST_MULT * df["atr"]
lower = hl2 - ST_MULT * df["atr"]
f_up  = upper.copy()
f_lo  = lower.copy()
st    = [1] * len(df)
for i in range(1, len(df)):
    f_up.iloc[i] = upper.iloc[i] if (upper.iloc[i] < f_up.iloc[i-1] or df["cl"].iloc[i-1] > f_up.iloc[i-1]) else f_up.iloc[i-1]
    f_lo.iloc[i] = lower.iloc[i] if (lower.iloc[i] > f_lo.iloc[i-1] or df["cl"].iloc[i-1] < f_lo.iloc[i-1]) else f_lo.iloc[i-1]
    if st[i-1] == -1 and df["cl"].iloc[i] > f_up.iloc[i-1]:
        st[i] = 1
    elif st[i-1] == 1 and df["cl"].iloc[i] < f_lo.iloc[i-1]:
        st[i] = -1
    else:
        st[i] = st[i-1]
df["st_dir"] = st

# RSI
delta = df["cl"].diff()
gain  = delta.clip(lower=0).ewm(span=RSI_PERIOD, adjust=False).mean()
loss  = (-delta.clip(upper=0)).ewm(span=RSI_PERIOD, adjust=False).mean()
rs    = gain / loss.replace(0, float("inf"))
df["rsi"] = 100 - (100 / (1 + rs))

# MACD
mf = df["cl"].ewm(span=MACD_FAST, adjust=False).mean()
ms = df["cl"].ewm(span=MACD_SLOW, adjust=False).mean()
mc = (mf - ms).ewm(span=MACD_SIG, adjust=False).mean()
df["macd_h"] = (mf - ms) - mc
return df
```

def get_signal_strict(df, trend_1d):
“”“Строгий сигнал для реального счёта (с EMA200 фильтром)”””
if len(df) < 3:
return None
c = df.iloc[-1]
p = df.iloc[-2]
ema_bull = c[“ema_mid”] > c[“ema_slow”]
ema_bear = c[“ema_mid”] < c[“ema_slow”]
st_bull  = c[“st_dir”] == 1
st_bear  = c[“st_dir”] == -1
macd_up  = c[“macd_h”] >= 0 and c[“macd_h”] > p[“macd_h”]
macd_dn  = c[“macd_h”] <= 0 and c[“macd_h”] < p[“macd_h”]
if ema_bull and st_bull and 40 <= c[“rsi”] <= 65 and macd_up and trend_1d >= 0:
return “LONG”
if ema_bear and st_bear and 35 <= c[“rsi”] <= 60 and macd_dn and trend_1d <= 0:
return “SHORT”
return None

def get_signal_demo(df):
“””
Мягкий сигнал для демо счёта — торгует в ОБЕ стороны.
Не требует EMA200. Достаточно Supertrend + RSI + MACD.
“””
if len(df) < 3:
return None
c = df.iloc[-1]
p = df.iloc[-2]
st_bull  = c[“st_dir”] == 1
st_bear  = c[“st_dir”] == -1
macd_up  = c[“macd_h”] >= 0 and c[“macd_h”] > p[“macd_h”]
macd_dn  = c[“macd_h”] <= 0 and c[“macd_h”] < p[“macd_h”]
rsi_long = 35 <= c[“rsi”] <= 68
rsi_short= 32 <= c[“rsi”] <= 65
if st_bull and macd_up and rsi_long:
return “LONG”
if st_bear and macd_dn and rsi_short:
return “SHORT”
return None

def get_daily_trend(symbol):
df1d = fetch_klines(symbol, interval=“D”, limit=60)
if df1d is None or len(df1d) < 55:
return 0
df1d = calc_indicators(df1d)
last = df1d.iloc[-1]
return 1 if last[“cl”] > last[“ema_slow”] else -1 if last[“cl”] < last[“ema_slow”] else 0

# ── Demo auto-trading ──────────────────────────────────────────────────────────

def demo_open_auto(user, symbol, side, usdt_amount):
“”“Открыть демо позицию автоматически”””
d    = user[“demo”]
poss = d.get(“positions”, [])
if any(p[“symbol”] == symbol for p in poss):
return False, “already_open”
if len(poss) >= MAX_POS:
return False, “max_pos”
if usdt_amount > d[“balance”]:
return False, “no_balance”
coin = demo_coin_by_symbol(symbol)
if not coin:
return False, “no_coin”
price, source = fetch_demo_price(symbol, coin.get(“cg_id”))
if not price:
return False, “no_price”
qty = round(usdt_amount / price, 6)
pos = {
“symbol”: symbol, “name”: coin[“name”], “short”: coin[“short”],
“side”: side, “entry”: price, “qty”: qty,
“usdt”: usdt_amount, “lev”: DEMO_LEVERAGE,
“source”: source, “ts”: now_str(),
# SL/TP уровни
“sl”: round(price * (1 - 0.04), 4) if side == “LONG” else round(price * (1 + 0.04), 4),
“tp”: round(price * (1 + 0.06), 4) if side == “LONG” else round(price * (1 - 0.06), 4),
}
poss.append(pos)
d[“positions”] = poss
d[“balance”]   = round(d[“balance”] - usdt_amount, 4)
return True, pos

def demo_close_auto(user, symbol):
“”“Закрыть демо позицию автоматически”””
d    = user[“demo”]
poss = d.get(“positions”, [])
pos  = next((p for p in poss if p[“symbol”] == symbol), None)
if not pos:
return False, “not_found”, None
coin = demo_coin_by_symbol(symbol)
exit_price, _ = fetch_demo_price(symbol, coin.get(“cg_id”) if coin else None)
if not exit_price:
return False, “no_price”, None
entry  = pos[“entry”]
qty    = pos[“qty”]
lev    = pos.get(“lev”, DEMO_LEVERAGE)
usdt   = pos[“usdt”]
side   = pos[“side”]
if side == “LONG”:
pnl = (exit_price - entry) / entry * usdt * lev
else:
pnl = (entry - exit_price) / entry * usdt * lev
pnl = round(pnl, 4)
d[“balance”] = round(d[“balance”] + usdt + pnl, 4)
d[“trades”] += 1
if pnl >= 0:
d[“wins”] += 1
else:
d[“loss”] += 1
d[“profit”] = round(d.get(“profit”, 0) + pnl, 4)
hist = d.get(“history”, [])
hist.append({
“symbol”: symbol, “side”: side,
“entry”: entry, “exit”: exit_price,
“usdt”: usdt, “lev”: lev,
“pnl”: pnl, “ts”: now_str(),
})
d[“history”]   = hist[-50:]
d[“positions”] = [p for p in poss if p[“symbol”] != symbol]
return True, pnl, exit_price

def monitor_demo_all_users():
“””
Главная функция авто-торговли демо:
1. Проверяет SL/TP открытых позиций
2. Ищет новые сигналы и открывает позиции
“””
users   = load_users()
changed = False

```
# Получаем данные рынка один раз для всех
market_data = {}
for pair in PAIRS:
    df = fetch_klines(pair["symbol"], "240", 120)
    if df is not None and len(df) >= 60:
        df = calc_indicators(df)
        price = fetch_demo_price(pair["symbol"])[0]
        market_data[pair["symbol"]] = {"df": df, "price": price}

for uid, user in users.items():
    d    = user["demo"]
    poss = d.get("positions", [])

    # Шаг 1: Проверяем SL/TP открытых позиций
    for pos in list(poss):
        sym  = pos["symbol"]
        md   = market_data.get(sym, {})
        price = md.get("price")
        if not price:
            continue
        side = pos["side"]
        hit_sl = (side == "LONG" and price <= pos["sl"]) or \
                 (side == "SHORT" and price >= pos["sl"])
        hit_tp = (side == "LONG" and price >= pos["tp"]) or \
                 (side == "SHORT" and price <= pos["tp"])
        if hit_sl or hit_tp:
            reason = "TAKE-PROFIT" if hit_tp else "STOP-LOSS"
            ok, pnl, exit_p = demo_close_auto(user, sym)
            if ok:
                changed = True
                icon = "WIN" if pnl >= 0 else "LOSS"
                if user.get("notify", True):
                    send(uid,
                         "<b>" + icon + " " + reason + " | " + pos["short"] + " " + side + "</b>\n"
                         "Вход: $" + fmt(pos["entry"]) + " | Выход: $" + fmt(exit_p) + "\n"
                         "P&L: " + sign(pnl) + "$" + fmt(abs(pnl)) + "\n"
                         "Баланс: <b>$" + fmt(d["balance"]) + "</b>",
                         [[{"text": "Демо-аккаунт", "callback_data": "demo_trade"}]])
                logger.info("DEMO %s %s %s %s P&L=%s", uid, sym, side, reason, pnl)

    # Шаг 2: Ищем новые сигналы (если есть место для позиции)
    if len(d.get("positions", [])) < MAX_POS and d["balance"] >= 20:
        for pair in PAIRS:
            sym = pair["symbol"]
            md  = market_data.get(sym, {})
            df  = md.get("df")
            if df is None:
                continue
            # Пропускаем если уже есть позиция
            if any(p["symbol"] == sym for p in d.get("positions", [])):
                continue
            sig = get_signal_demo(df)
            if sig:
                amount = round(d["balance"] * 0.3, 2)  # 30% баланса на сделку
                amount = max(amount, 10.0)
                ok, result = demo_open_auto(user, sym, sig, amount)
                if ok:
                    changed = True
                    pos = result
                    if user.get("notify", True):
                        icon = "LONG" if sig == "LONG" else "SHORT"
                        send(uid,
                             "<b>" + icon + " | " + pair["name"] + " (авто)</b>\n"
                             "Цена входа: $" + fmt(pos["entry"]) + "\n"
                             "Сумма: $" + fmt(pos["usdt"]) + " x" + str(DEMO_LEVERAGE) + "\n"
                             "SL: $" + fmt(pos["sl"]) + " | TP: $" + fmt(pos["tp"]) + "\n"
                             "Баланс: <b>$" + fmt(d["balance"]) + "</b>",
                             [[{"text": "Мои позиции", "callback_data": "demo_positions"}]])
                    logger.info("DEMO OPEN %s %s %s @ %s", uid, sym, sig, pos["entry"])

    users[uid] = user

if changed:
    save_users(users)
```

# ── Real bot trading ───────────────────────────────────────────────────────────

def do_open_real(pair, price, atr, side):
s   = BOT_STATES[pair[“symbol”]]
sym = pair[“symbol”]
if s.get(“pos”) or active_positions() >= MAX_POS:
return None
if s.get(“halted”) and time.time() < s.get(“halt_until”, 0):
return None
sl = round(price - ATR_SL_MULT * atr, 2) if side == “LONG” else round(price + ATR_SL_MULT * atr, 2)
tp = round(price + ATR_TP_MULT * atr, 2) if side == “LONG” else round(price - ATR_TP_MULT * atr, 2)
risk   = s[“usdt”] * (RISK_PCT / 100)
sl_dist= abs(price - sl)
qty    = round(risk / max(sl_dist, 0.0001), 6)
qty    = max(qty, pair[“min_qty”])
margin = qty * price / LEVERAGE
if margin > s[“usdt”] * 0.95:
qty    = round(s[“usdt”] * 0.95 * LEVERAGE / price, 6)
margin = qty * price / LEVERAGE
s[“usdt”] -= margin
s[“n”]    += 1
s[“pos”]   = {
“side”: side, “entry”: price, “qty”: qty,
“sl”: sl, “tp”: tp, “atr”: atr,
“time”: now_str(), “margin”: margin,
}
save_bot_state(sym, s)
t = {**s[“pos”], “pair”: pair[“name”], “action”: “OPEN”, “equity”: round(s[“usdt”] + margin, 2)}
log_trade(t)
return t

def do_close_real(pair, price, reason=“SIGNAL”):
s   = BOT_STATES[pair[“symbol”]]
sym = pair[“symbol”]
pos = s.get(“pos”)
if not pos:
return None
side   = pos[“side”]
qty    = pos[“qty”]
margin = pos[“margin”]
pnl    = (price - pos[“entry”]) * qty * LEVERAGE if side == “LONG”   
else (pos[“entry”] - price) * qty * LEVERAGE
pnl    = round(pnl, 4)
s[“usdt”]  += margin + pnl
s[“pnl”]   += pnl
s[“n”]     += 1
if pnl >= 0:
s[“wins”] += 1
else:
s[“loss”] += 1
if s[“usdt”] > s.get(“peak”, 0):
s[“peak”] = s[“usdt”]
t = {
“pair”: pair[“name”], “action”: “CLOSE”,
“side”: side, “qty”: qty,
“entry”: pos[“entry”], “price”: price,
“pnl”: pnl, “pnl_pct”: round(pnl / margin * 100, 2) if margin else 0,
“reason”: reason, “time”: now_str(), “equity”: round(s[“usdt”], 2),
}
s[“pos”] = None
save_bot_state(sym, s)
log_trade(t)
distribute_real(pair[“name”], pnl, pnl >= 0)
return t

def check_exits_real(pair, price, df4h):
s   = BOT_STATES[pair[“symbol”]]
pos = s.get(“pos”)
if not pos:
return False
side   = pos[“side”]
hit_sl = (side == “LONG” and price <= pos[“sl”]) or   
(side == “SHORT” and price >= pos[“sl”])
hit_tp = (side == “LONG” and price >= pos[“tp”]) or   
(side == “SHORT” and price <= pos[“tp”])
if hit_sl:
t = do_close_real(pair, price, “STOP-LOSS”)
if t:
send(ADMIN_ID,
“<b>STOP-LOSS | “ + pair[“name”] + “ “ + side + “</b>\n”
“P&L: “ + sign(t[“pnl”]) + “$” + fmt(abs(t[“pnl”])) + “\n”
“Баланс: $” + fmt(BOT_STATES[pair[“symbol”]][“usdt”]))
return True
if hit_tp:
t = do_close_real(pair, price, “TAKE-PROFIT”)
if t:
send(ADMIN_ID,
“<b>TAKE-PROFIT | “ + pair[“name”] + “ “ + side + “</b>\n”
“P&L: +” + fmt(t[“pnl”]) + “\n”
“Баланс: $” + fmt(BOT_STATES[pair[“symbol”]][“usdt”]))
return True
return False

def circuit_breaker(sym):
s     = BOT_STATES[sym]
today = datetime.now(timezone.utc).strftime(”%Y-%m-%d”)
if s.get(“day_date”) != today:
s[“day_date”]  = today
s[“day_start”] = s[“usdt”]
s[“halted”]    = False
equity = s[“usdt”]
if s.get(“day_start”, 0) > 0:
dd = (s[“day_start”] - equity) / s[“day_start”] * 100
if dd > DAY_LOSS_PCT and not s.get(“halted”):
s[“halted”]     = True
s[“halt_until”] = time.time() + 86400
save_bot_state(sym, s)
send(ADMIN_ID, “<b>CIRCUIT BREAKER “ + sym + “</b>\nДневной убыток: “ + fmt(dd, “.1f”) + “%\nПауза 24ч”)
return True
if s.get(“halted”) and time.time() >= s.get(“halt_until”, 0):
s[“halted”]     = False
s[“halt_until”] = 0
s[“day_start”]  = s[“usdt”]
save_bot_state(sym, s)
save_bot_state(sym, s)
return s.get(“halted”, False)

def distribute_real(pair_name, pnl, is_win):
users     = load_users()
total_dep = sum(u[“real”][“deposited”] for u in users.values() if u[“real”][“active”])
if total_dep <= 0 or pnl == 0:
return
for uid, u in users.items():
r = u[“real”]
if not r[“active”] or r[“deposited”] <= 0:
continue
share    = r[“deposited”] / total_dep
user_pnl = round(pnl * share, 4)
r[“profit”]  += user_pnl
r[“balance”] += user_pnl
r[“trades”]  += 1
if is_win:
r[“wins”] += 1
else:
r[“loss”] += 1
r[“history”].append({“pair”: pair_name, “pnl”: user_pnl, “time”: now_str()})
if r[“balance”] > r.get(“peak”, 0):
r[“peak”] = r[“balance”]
u[“real”] = r
users[uid] = u
if u.get(“notify”) and user_pnl != 0:
icon = “WIN” if is_win else “LOSS”
send(uid,
“<b>” + icon + “ | “ + pair_name + “</b>\n”
“P&L: “ + sign(user_pnl) + “$” + fmt(abs(user_pnl)) + “\n”
“Баланс: <b>$” + fmt(r[“balance”]) + “</b>”)
save_users(users)

# ── Telegram ───────────────────────────────────────────────────────────────────

def api(method, data=None):
if not TOKEN:
return {}
try:
r = requests.post(
“https://api.telegram.org/bot” + TOKEN + “/” + method,
json=data or {}, timeout=15,
)
return r.json()
except Exception as e:
logger.error(“TG %s: %s”, method, e)
return {}

def send(cid, text, buttons=None):
if not TOKEN:
return
d = {“chat_id”: str(cid), “text”: str(text)[:4096], “parse_mode”: “HTML”}
if buttons:
d[“reply_markup”] = json.dumps({“inline_keyboard”: buttons})
api(“sendMessage”, d)

def answer_cb(cb_id):
api(“answerCallbackQuery”, {“callback_query_id”: cb_id})

# ── Keyboards ──────────────────────────────────────────────────────────────────

def kb_main():
return [
[{“text”: “Demo Account”,   “callback_data”: “demo_trade”},
{“text”: “My Account”,     “callback_data”: “account”}],
[{“text”: “Live Stats”,     “callback_data”: “stats”},
{“text”: “Market”,         “callback_data”: “market”}],
[{“text”: “Deposit”,        “callback_data”: “deposit”},
{“text”: “Withdraw”,       “callback_data”: “withdraw”}],
[{“text”: “Trade History”,  “callback_data”: “history”},
{“text”: “Help”,           “callback_data”: “help”}],
]

def kb_back():
return [[{“text”: “Main Menu”, “callback_data”: “menu”}]]

def kb_demo():
return [
[{“text”: “My Demo Positions”, “callback_data”: “demo_positions”}],
[{“text”: “Demo History”,      “callback_data”: “demo_history”}],
[{“text”: “Reset Demo ($1000)”,“callback_data”: “demo_reset”}],
[{“text”: “Main Menu”,         “callback_data”: “menu”}],
]

def kb_deposit():
return [
[{“text”: “$50”,    “callback_data”: “dep_50”},
{“text”: “$100”,   “callback_data”: “dep_100”},
{“text”: “$200”,   “callback_data”: “dep_200”}],
[{“text”: “$500”,   “callback_data”: “dep_500”},
{“text”: “$1000”,  “callback_data”: “dep_1000”},
{“text”: “Custom”, “callback_data”: “dep_custom”}],
[{“text”: “Cancel”, “callback_data”: “menu”}],
]

# ── Screens ────────────────────────────────────────────────────────────────────

def screen_main(cid):
user  = get_user(cid)
d     = user[“demo”]
r     = user[“real”]
d_pct = pct_val(d[“balance”] - d[“start”], d[“start”])
r_pct = pct_val(r[“profit”], r[“deposited”]) if r[“deposited”] > 0 else 0.0
poss  = d.get(“positions”, [])
tot_pnl = sum(s.get(“pnl”, 0) for s in BOT_STATES.values())
mode  = “LIVE (Bybit)” if LIVE_MODE else “DEMO”
text  = (
“<b>CryptoBot Pro v5</b>\n\n”
“Mode: “ + mode + “ | Leverage: “ + str(LEVERAGE) + “x\n”
“Strategy: EMA + Supertrend + RSI + MACD\n\n”
“Hello, <b>” + (user[“name”] or “Investor”) + “</b>!\n\n”
“Demo:    <b>$” + fmt(d[“balance”]) + “</b>  “ + sign(d_pct) + fmt(d_pct, “.1f”) + “%\n”
“Real:    <b>$” + fmt(r[“balance”]) + “</b>  “ + sign(r_pct) + fmt(r_pct, “.1f”) + “%\n”
“Demo positions: “ + str(len(poss)) + “/3\n\n”
“Bot P&L: <code>” + sign(tot_pnl) + “$” + fmt(abs(tot_pnl)) + “</code>”
)
send(cid, text, kb_main())

def screen_demo(cid):
user  = get_user(cid)
d     = user[“demo”]
poss  = d.get(“positions”, [])
pct   = pct_val(d[“balance”] - d[“start”], d[“start”])
wr    = wr_calc(d[“wins”], d[“loss”])
text  = (
“<b>Demo Account</b>\n\n”
“Balance: <b>$” + fmt(d[“balance”]) + “</b>\n”
“Profit:  <code>” + sign(pct) + fmt(pct, “.1f”) + “%</code>\n”
“Trades:  “ + str(d[“trades”]) + “ | WR: “ + str(wr) + “%\n”
“Open positions: “ + str(len(poss)) + “/3\n\n”
“Bot trades automatically every 15 min.\n”
“Uses real prices from Bybit/OKX/CoinGecko.\n”
“Leverage: “ + str(DEMO_LEVERAGE) + “x | SL: -4% | TP: +6%\n\n”
)
if poss:
text += “<b>Open positions:</b>\n”
for p in poss:
price, _ = fetch_demo_price(p[“symbol”])
fl = 0.0
if price:
if p[“side”] == “LONG”:
fl = (price - p[“entry”]) / p[“entry”] * p[“usdt”] * p[“lev”]
else:
fl = (p[“entry”] - price) / p[“entry”] * p[“usdt”] * p[“lev”]
fl = round(fl, 2)
text += (
p[“short”] + “ “ + p[“side”] + “ x” + str(p[“lev”]) + “\n”
“  Entry: $” + fmt(p[“entry”]) + “ | Now: $” + (fmt(price) if price else “?”) + “\n”
“  P&L: <code>” + sign(fl) + “$” + fmt(abs(fl)) + “</code>\n”
“  SL: $” + fmt(p[“sl”]) + “ | TP: $” + fmt(p[“tp”]) + “\n\n”
)
send(cid, text, kb_demo())

def screen_demo_positions(cid):
user  = get_user(cid)
poss  = user[“demo”].get(“positions”, [])
if not poss:
send(cid, “No open positions.\n\nBot will open positions automatically when signal appears.”, kb_back())
return
text = “<b>Open Demo Positions</b>\n\n”
btns = []
for p in poss:
price, _ = fetch_demo_price(p[“symbol”])
fl = 0.0
if price:
if p[“side”] == “LONG”:
fl = (price - p[“entry”]) / p[“entry”] * p[“usdt”] * p[“lev”]
else:
fl = (p[“entry”] - price) / p[“entry”] * p[“usdt”] * p[“lev”]
fl = round(fl, 2)
text += (
“<b>” + p[“short”] + “ “ + p[“side”] + “</b> x” + str(p[“lev”]) + “\n”
“Entry: $” + fmt(p[“entry”]) + “ | Now: $” + (fmt(price) if price else “?”) + “\n”
“P&L: <code>” + sign(fl) + “$” + fmt(abs(fl)) + “</code>\n”
“SL: $” + fmt(p[“sl”]) + “ | TP: $” + fmt(p[“tp”]) + “\n\n”
)
btns.append([{“text”: “Close “ + p[“short”] + “ “ + p[“side”],
“callback_data”: “demo_close_” + p[“symbol”]}])
btns.append([{“text”: “Back”, “callback_data”: “demo_trade”}])
send(cid, text, btns)

def screen_demo_history(cid):
user = get_user(cid)
hist = user[“demo”].get(“history”, [])[-10:]
if not hist:
send(cid, “No closed trades yet.”, kb_back())
return
text = “<b>Demo History (last 10)</b>\n\n”
for h in reversed(hist):
ico = “WIN” if h[“pnl”] >= 0 else “LOSS”
text += ico + “ “ + h[“symbol”][:3] + “ “ + h[“side”] + “ “ + sign(h[“pnl”]) + “$” + fmt(abs(h[“pnl”])) + “ | “ + h[“ts”][:16] + “\n”
total = sum(h[“pnl”] for h in hist)
text += “\nTotal: <code>” + sign(total) + “$” + fmt(abs(total)) + “</code>”
send(cid, text, kb_back())

def screen_stats(cid):
trades = all_trades()
closes = [t for t in trades if t.get(“action”) == “CLOSE”]
wins   = [t for t in closes if t.get(“pnl”, 0) >= 0]
total_pnl = sum(t.get(“pnl”, 0) for t in closes)
wr    = wr_calc(len(wins), len(closes) - len(wins))
users = load_users()
dep   = sum(u[“real”][“deposited”] for u in users.values())
text  = (
“<b>Bot Statistics</b>\n\n”
“Closed trades: “ + str(len(closes)) + “\n”
“Win rate: “ + str(wr) + “%\n”
“Total P&L: <code>” + sign(total_pnl) + “$” + fmt(abs(total_pnl)) + “</code>\n\n”
“<b>Per pair:</b>\n”
)
for pair in PAIRS:
s   = BOT_STATES.get(pair[“symbol”], {})
pos = s.get(“pos”)
pos_txt = “”
if pos:
price = fetch_price(pair[“symbol”])
if price:
fl = (price - pos[“entry”]) * pos[“qty”] * LEVERAGE
if pos[“side”] == “SHORT”:
fl = -fl
pos_txt = “ | Float: “ + sign(fl) + “$” + fmt(abs(fl))
text += (
pair[“name”] + “: $” + fmt(s.get(“usdt”, 10000)) +
“ W:” + str(s.get(“wins”, 0)) + “ L:” + str(s.get(“loss”, 0)) +
pos_txt + “\n”
)
text += “\nTotal deposited: $” + fmt(dep)
send(cid, text, kb_back())

def screen_market(cid):
text = “<b>Market Status</b>\n\n”
for pair in PAIRS:
price = fetch_price(pair[“symbol”])
df4h  = fetch_klines(pair[“symbol”], “240”, 80)
if df4h is None or len(df4h) < 60 or not price:
text += pair[“name”] + “: no data\n\n”
continue
df4h  = calc_indicators(df4h)
c     = df4h.iloc[-1]
trend = “UP” if c[“ema_mid”] > c[“ema_slow”] else “DOWN”
st    = “Bull” if c[“st_dir”] == 1 else “Bear”
rsi   = round(c[“rsi”])
sig_d = get_signal_demo(df4h)
sig_r = get_signal_strict(df4h, get_daily_trend(pair[“symbol”]))
text += (
“<b>” + pair[“name”] + “</b> $” + fmt(price) + “\n”
“  Trend: “ + trend + “ | ST: “ + st + “ | RSI: “ + str(rsi) + “\n”
“  Demo signal: “ + (sig_d or “none”) + “\n”
“  Real signal: “ + (sig_r or “none”) + “\n\n”
)
send(cid, text, kb_back())

def screen_account(cid):
user  = get_user(cid)
d     = user[“demo”]
r     = user[“real”]
d_pct = pct_val(d[“balance”] - d[“start”], d[“start”])
r_pct = pct_val(r[“profit”], r[“deposited”]) if r[“deposited”] > 0 else 0.0
text  = (
“<b>My Account</b>\n\n”
“<b>Demo</b>\n”
“Balance: $” + fmt(d[“balance”]) + “\n”
“Profit: “ + sign(d_pct) + fmt(d_pct, “.1f”) + “%\n”
“Trades: “ + str(d[“trades”]) + “ | WR: “ + str(wr_calc(d[“wins”], d[“loss”])) + “%\n\n”
“<b>Real</b>\n”
“Balance: $” + fmt(r[“balance”]) + “\n”
“Deposited: $” + fmt(r[“deposited”]) + “\n”
“Profit: “ + sign(r_pct) + fmt(r_pct, “.1f”) + “%\n”
“Status: “ + (“Active” if r[“active”] else “Inactive”) + “\n\n”
“Joined: “ + user[“joined”]
)
btns = [
[{“text”: “Deposit”,   “callback_data”: “deposit”},
{“text”: “Withdraw”,  “callback_data”: “withdraw”}],
[{“text”: “Main Menu”, “callback_data”: “menu”}],
]
send(cid, text, btns)

def screen_deposit(cid):
send(cid,
“<b>Deposit USDT</b>\n\n”
“Wallet (TRC-20):\n<code>” + WALLET + “</code>\n\n”
“Minimum: $50 | Credited within 30 min”,
kb_deposit())

def screen_help(cid):
send(cid,
“<b>CryptoBot Pro v5</b>\n\n”
“<b>Demo Account:</b>\n”
“- Bot trades automatically every 15 min\n”
“- Uses real prices (Bybit/OKX/CoinGecko)\n”
“- Leverage 2x | SL -4% | TP +6%\n”
“- Start balance: $1,000 virtual\n\n”
“<b>Real Account:</b>\n”
“- Deposit USDT (min $50)\n”
“- Bot trades with strict strategy (4H + 1D)\n”
“- Profit distributed after each trade\n\n”
“<b>Strategy:</b>\n”
“- Supertrend + RSI + MACD\n”
“- 4H timeframe | Leverage “ + str(LEVERAGE) + “x\n”
“- Circuit breaker: -5% day / -15% peak\n\n”
“<b>Commands:</b>\n”
“/start - main menu\n”
“/status - market status\n”
“/demo - demo account”,
kb_back())

# ── Update processor ───────────────────────────────────────────────────────────

LAST_UPDATE_ID = 0

def get_updates():
global LAST_UPDATE_ID
r = api(“getUpdates”, {“offset”: LAST_UPDATE_ID + 1, “timeout”: 1})
for upd in r.get(“result”, []):
LAST_UPDATE_ID = upd[“update_id”]
msg = upd.get(“message”, {})
cb  = upd.get(“callback_query”, {})
if msg:
on_message(msg)
if cb:
on_callback(cb)

def on_message(msg):
cid  = str(msg[“chat”][“id”])
text = msg.get(“text”, “”).strip()
name = msg.get(“from”, {}).get(“first_name”, “”)
state = STATES.get(cid, {})

```
user = get_user(cid)
if name and not user.get("name"):
    user["name"] = name
    save_user(cid, user)

st = state.get("state", "")

if st == "txid_waiting":
    amt  = state.get("amount", 0)
    user["real"]["pending"]      = amt
    user["real"]["pending_txid"] = text
    save_user(cid, user)
    STATES.pop(cid, None)
    send(cid, "Deposit request submitted!\nAmount: $" + str(amt) + "\nStatus: Pending (up to 30 min)", kb_back())
    for adm in ADMIN_IDS.split(","):
        send(adm.strip(),
             "<b>New Deposit</b>\n"
             "User: " + (user["name"] or cid) + " (ID:" + cid + ")\n"
             "Amount: $" + str(amt) + "\n"
             "TXID: " + text,
             [[{"text": "Confirm $" + str(int(amt)), "callback_data": "adm_confirm_" + cid + "_" + str(float(amt))}]])
    return

if st == "wd_waiting":
    parts = text.strip().split()
    if len(parts) < 2:
        send(cid, "Format: WALLET AMOUNT")
        return
    wallet_addr = parts[0]
    try:
        amt = float(parts[1])
    except Exception:
        send(cid, "Invalid amount")
        return
    user = get_user(cid)
    if amt > user["real"]["balance"]:
        send(cid, "Insufficient balance: $" + fmt(user["real"]["balance"]))
        return
    user["real"]["withdrawals"].append({"wallet": wallet_addr, "amount": amt, "time": now_str(), "status": "pending"})
    save_user(cid, user)
    STATES.pop(cid, None)
    send(cid, "Withdrawal submitted!\nAmount: $" + fmt(amt) + "\nProcessing: up to 24h", kb_back())
    for adm in ADMIN_IDS.split(","):
        send(adm.strip(),
             "<b>Withdrawal Request</b>\n"
             "User: " + (user["name"] or cid) + "\n"
             "Amount: $" + fmt(amt) + "\n"
             "Wallet: " + wallet_addr,
             [[{"text": "Pay $" + str(int(amt)), "callback_data": "adm_pay_" + cid + "_" + str(amt)}]])
    return

if st == "custom_dep":
    try:
        amt = float(text)
        if amt < 50:
            send(cid, "Minimum $50")
            return
        STATES.pop(cid, None)
        send(cid,
             "Send <b>$" + fmt(amt) + " USDT</b> (TRC-20) to:\n<code>" + WALLET + "</code>",
             [[{"text": "I sent payment", "callback_data": "depsent_" + str(amt)}],
              [{"text": "Cancel", "callback_data": "menu"}]])
    except Exception:
        send(cid, "Enter a number. Example: 150")
    return

if text in ["/start", "/menu"]:
    user = get_user(cid)
    user["name"] = name
    save_user(cid, user)
    screen_main(cid)
elif text == "/demo":
    screen_demo(cid)
elif text in ["/status", "/market"]:
    screen_market(cid)
elif text == "/stats":
    screen_stats(cid)
elif text == "/admin" and is_admin(cid):
    users = load_users()
    total_dep = sum(u["real"]["deposited"] for u in users.values())
    send(cid,
         "<b>Admin Panel</b>\n\n"
         "Users: " + str(len(users)) + "\n"
         "Total deposited: $" + fmt(total_dep),
         [[{"text": "All Users",    "callback_data": "adm_users"},
           {"text": "Deposits",    "callback_data": "adm_deposits"}],
          [{"text": "Withdrawals", "callback_data": "adm_withdrawals"},
           {"text": "Main Menu",   "callback_data": "menu"}]])
else:
    screen_main(cid)
```

def on_callback(cb):
cid  = str(cb[“from”][“id”])
data = cb.get(“data”, “”)
answer_cb(cb[“id”])

```
if data == "menu":
    screen_main(cid)
elif data == "demo_trade":
    screen_demo(cid)
elif data == "demo_positions":
    screen_demo_positions(cid)
elif data == "demo_history":
    screen_demo_history(cid)
elif data == "demo_reset":
    user = get_user(cid)
    user["demo"] = {
        "balance": 1000.0, "start": 1000.0,
        "profit": 0.0, "trades": 0, "wins": 0, "loss": 0,
        "history": [], "positions": [],
    }
    save_user(cid, user)
    send(cid, "Demo reset! Balance: <b>$1,000</b>", kb_back())
elif data.startswith("demo_close_"):
    sym  = data.replace("demo_close_", "")
    user = get_user(cid)
    ok, pnl, exit_p = demo_close_auto(user, sym)
    if ok:
        save_user(cid, user)
        ico = "WIN" if pnl >= 0 else "LOSS"
        send(cid,
             ico + " Closed!\n"
             "P&L: " + sign(pnl) + "$" + fmt(abs(pnl)) + "\n"
             "Balance: <b>$" + fmt(user["demo"]["balance"]) + "</b>",
             kb_back())
    else:
        send(cid, "Error: " + str(pnl), kb_back())
elif data == "account":
    screen_account(cid)
elif data == "stats":
    screen_stats(cid)
elif data == "market":
    screen_market(cid)
elif data == "deposit":
    screen_deposit(cid)
elif data == "withdraw":
    user = get_user(cid)
    if user["real"]["balance"] <= 0:
        send(cid, "No funds to withdraw.", kb_back())
        return
    STATES[cid] = {"state": "wd_waiting"}
    send(cid, "Enter wallet and amount:\nFormat: WALLET AMOUNT\nExample: TXxxxxxxxx 100", kb_back())
elif data == "history":
    screen_demo_history(cid)
elif data == "help":
    screen_help(cid)
elif data.startswith("dep_"):
    amt_str = data.replace("dep_", "")
    if amt_str == "custom":
        STATES[cid] = {"state": "custom_dep"}
        send(cid, "Enter deposit amount (min $50):", kb_back())
    else:
        amt = float(amt_str)
        send(cid,
             "Send <b>$" + fmt(amt) + " USDT</b> (TRC-20) to:\n<code>" + WALLET + "</code>",
             [[{"text": "I sent payment", "callback_data": "depsent_" + str(amt)}],
              [{"text": "Cancel", "callback_data": "menu"}]])
elif data.startswith("depsent_"):
    amt  = float(data.split("_")[1])
    STATES[cid] = {"state": "txid_waiting", "amount": amt}
    send(cid, "Enter transaction ID (TXID):")
elif data.startswith("adm_confirm_") and is_admin(cid):
    parts  = data.replace("adm_confirm_", "").split("_")
    target = parts[0]
    amt    = float(parts[1])
    user   = get_user(target)
    user["real"]["balance"]   += amt
    user["real"]["deposited"] += amt
    user["real"]["active"]     = True
    user["real"]["pending"]    = 0.0
    save_user(target, user)
    send(target, "<b>Deposit Confirmed!</b>\n$" + fmt(amt) + " added.\nBalance: $" + fmt(user["real"]["balance"]))
    send(cid, "Confirmed $" + str(int(amt)) + " for " + target)
elif data.startswith("adm_pay_") and is_admin(cid):
    parts  = data.replace("adm_pay_", "").split("_")
    target = parts[0]
    amt    = float(parts[1])
    user   = get_user(target)
    if user["real"]["balance"] >= amt:
        user["real"]["balance"] -= amt
        save_user(target, user)
        send(target, "<b>Withdrawal Paid!</b>\n$" + fmt(amt) + " sent.")
        send(cid, "Paid $" + str(int(amt)) + " to " + target)
elif data == "adm_users" and is_admin(cid):
    users = load_users()
    text  = "<b>All Users</b>\n\n"
    for uid, u in list(users.items())[:20]:
        text += uid + " | " + (u.get("name") or "?") + " | $" + fmt(u["real"]["deposited"]) + "\n"
    send(cid, text, kb_back())
elif data == "adm_deposits" and is_admin(cid):
    users = load_users()
    total = sum(u["real"]["deposited"] for u in users.values())
    send(cid, "Total deposited: $" + fmt(total), kb_back())
elif data == "adm_withdrawals" and is_admin(cid):
    users = load_users()
    text  = "<b>Pending Withdrawals</b>\n\n"
    found = False
    btns  = []
    for uid, u in users.items():
        for req in u["real"].get("withdrawals", []):
            if req.get("status") == "pending":
                found = True
                text += (u.get("name") or uid) + ": $" + fmt(req["amount"]) + " -> " + req["wallet"][:10] + "...\n"
                btns.append([{"text": "Pay $" + str(int(req["amount"])), "callback_data": "adm_pay_" + uid + "_" + str(req["amount"])}])
    if not found:
        text += "None"
    btns.append([{"text": "Back", "callback_data": "menu"}])
    send(cid, text, btns)
```

# ── Main loops ─────────────────────────────────────────────────────────────────

def telegram_loop():
logger.info(“Telegram polling started”)
while True:
try:
get_updates()
except Exception as e:
logger.error(“telegram_loop: %s”, e)
time.sleep(CMD_INT)

def demo_trading_loop():
“”“Автоторговля демо счетов каждые 15 минут”””
logger.info(“Demo trading loop started (every 15 min)”)
while True:
try:
logger.info(“Demo: checking signals…”)
monitor_demo_all_users()
except Exception as e:
logger.error(“demo_loop: %s”, e)
time.sleep(DEMO_INT)

def real_trading_loop():
“”“Реальная торговля каждые 4 часа”””
logger.info(“Real trading loop started (every 4h)”)
while True:
try:
logger.info(“Real: checking signals…”)
for pair in PAIRS:
sym = pair[“symbol”]
try:
if circuit_breaker(sym):
continue
s     = BOT_STATES[sym]
price = fetch_price(sym)
if price is None:
continue
df4h = fetch_klines(sym, “240”, 200)
if df4h is None or len(df4h) < 60:
continue
df4h = calc_indicators(df4h)
if s.get(“pos”):
check_exits_real(pair, price, df4h)
continue
trend_1d = get_daily_trend(sym)
sig      = get_signal_strict(df4h, trend_1d)
if sig:
t = do_open_real(pair, price, df4h.iloc[-1][“atr”], sig)
if t:
send(ADMIN_ID,
“<b>” + sig + “ | “ + pair[“name”] + “</b>\n”
“Price: $” + fmt(price) + “\n”
“SL: $” + fmt(t[“sl”]) + “ | TP: $” + fmt(t[“tp”]) + “\n”
“Risk: “ + str(RISK_PCT) + “% | Leverage: “ + str(LEVERAGE) + “x”)
else:
c = df4h.iloc[-1]
logger.info(“Real %s: no signal | RSI=%.0f ST=%d”, sym, c[“rsi”], c[“st_dir”])
except Exception as e:
logger.error(“real_loop %s: %s”, sym, e)
time.sleep(2)
except Exception as e:
logger.error(“real_trading_loop: %s”, e)
time.sleep(TRADE_INT)

def status_loop():
“”“Статус каждые 30 минут”””
time.sleep(60)  # первый статус через минуту после запуска
while True:
try:
text = “<b>Market Status (30 min)</b>\n\n”
any_sig = False
for pair in PAIRS:
price = fetch_price(pair[“symbol”])
df4h  = fetch_klines(pair[“symbol”], “240”, 80)
if df4h is None or len(df4h) < 60 or not price:
continue
df4h  = calc_indicators(df4h)
c     = df4h.iloc[-1]
trend = “UP” if c[“ema_mid”] > c[“ema_slow”] else “DOWN”
st    = “Bull” if c[“st_dir”] == 1 else “Bear”
rsi   = round(c[“rsi”])
sig_d = get_signal_demo(df4h)
sig_r = get_signal_strict(df4h, get_daily_trend(pair[“symbol”]))
s     = BOT_STATES.get(pair[“symbol”], {})
pos   = s.get(“pos”)
pos_txt = “”
if pos:
fl = (price - pos[“entry”]) * pos[“qty”] * LEVERAGE
if pos[“side”] == “SHORT”:
fl = -fl
pos_txt = “ | “ + pos[“side”] + “ “ + sign(fl) + “$” + fmt(abs(fl))
text += (
“<b>” + pair[“name”] + “</b> $” + fmt(price) + “\n”
“  Trend: “ + trend + “ | ST: “ + st + “ | RSI: “ + str(rsi) + pos_txt + “\n”
)
if sig_d:
text += “  Demo signal: <b>” + sig_d + “</b>\n”
any_sig = True
if sig_r:
text += “  Real signal: <b>” + sig_r + “</b>\n”
text += “\n”
if not any_sig:
text += “No signals yet — bot is waiting for right entry”
send(ADMIN_ID, text)
except Exception as e:
logger.error(“status_loop: %s”, e)
time.sleep(STATUS_INT)

def run():
global BOT_STATES
BOT_STATES = {p[“symbol”]: load_bot_state(p[“symbol”]) for p in PAIRS}
logger.info(“CryptoBot Pro v5 starting”)
logger.info(“Mode: %s”, “LIVE” if LIVE_MODE else “DEMO”)

```
if TOKEN:
    send(ADMIN_ID,
         "<b>CryptoBot Pro v5 - STARTED</b>\n\n"
         "Mode: " + ("LIVE Bybit" if LIVE_MODE else "DEMO") + "\n"
         "Pairs: BTC | ETH | SOL\n"
         "Strategy: EMA + Supertrend + RSI + MACD\n\n"
         "Demo: auto-trades every 15 min\n"
         "Real: checks every 4h (strict conditions)\n\n"
         "/start - main menu\n"
         "/demo - demo account\n"
         "/status - market status")

# Запускаем все циклы в отдельных потоках
threads = [
    threading.Thread(target=telegram_loop,    daemon=True),
    threading.Thread(target=demo_trading_loop, daemon=True),
    threading.Thread(target=real_trading_loop, daemon=True),
    threading.Thread(target=status_loop,       daemon=True),
]
for t in threads:
    t.start()
logger.info("All threads started")

# Главный поток держит программу живой
while True:
    time.sleep(60)
```

if **name** == “**main**”:
run()