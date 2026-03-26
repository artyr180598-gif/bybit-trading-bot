# “””
Advanced Multi-Pair Trading Bot v2.0

Pairs:    BTC/USDT, ETH/USDT, SOL/USDT
Timeframe: 30 minutes
Strategy:

- Trend filter:  EMA 50 > EMA 200 (only trade with trend)
- Entry signal:  Williams %R < -80 (oversold) + MACD bullish cross
- Exit signal:   Williams %R > -20 (overbought) OR MACD bearish cross
- stop_loss_multiplier = 1.5
- Risk:          2% of balance per trade
  Notifications: Telegram (per pair)
  Mode: PAPER_TRADE = True (safe simulation)
import os
import time
import logging
import requests
import pandas as pd
from datetime import datetime, timezone

logging.basicConfig(
level=logging.INFO,
format=”%(asctime)s [%(levelname)s] %(message)s”,
handlers=[
logging.StreamHandler(),
logging.FileHandler(“bot/bot.log”),
],
)
logger = logging.getLogger(**name**)

BYBIT_API_KEY      = os.environ[“BYBIT_API_KEY”]
BYBIT_API_SECRET   = os.environ[“BYBIT_API_SECRET”]
TELEGRAM_BOT_TOKEN = os.environ[“TELEGRAM_BOT_TOKEN”]
TELEGRAM_CHAT_ID   = os.environ[“TELEGRAM_CHAT_ID”]

PAIRS = [
{“symbol”: “BTCUSDT”, “yahoo”: “BTC-USD”, “name”: “BTC”},
{“symbol”: “ETHUSDT”, “yahoo”: “ETH-USD”, “name”: “ETH”},
{“symbol”: “SOLUSDT”, “yahoo”: “SOL-USD”, “name”: “SOL”},
]

EMA_FAST        = 50
EMA_SLOW        = 200
WILLIAMS_PERIOD = 14
MACD_FAST       = 12
MACD_SLOW       = 26
MACD_SIGNAL     = 9
ATR_PERIOD      = 14
ATR_MULTIPLIER  = 1.5
RISK_PERCENT    = 2.0
CHECK_INTERVAL  = 1800
CANDLE_COUNT    = 250
PAPER_TRADE     = True
WR_OVERSOLD     = -80
WR_OVERBOUGHT   = -20

def make_paper_state(balance=3333.0):
return {
“usdt_balance”:  balance,
“coin_balance”:  0.0,
“open_position”: None,
“trade_count”:   0,
“wins”:          0,
“losses”:        0,
“total_pnl”:     0.0,
“trade_log”:     [],
}

paper_states = {p[“symbol”]: make_paper_state() for p in PAIRS}

def send_telegram(message: str) -> bool:
url     = f”https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage”
payload = {“chat_id”: TELEGRAM_CHAT_ID, “text”: message, “parse_mode”: “HTML”}
try:
resp = requests.post(url, json=payload, timeout=10)
resp.raise_for_status()
return True
except Exception as e:
logger.error(f”Telegram error: {e}”)
return False

def get_klines(yahoo_symbol: str):
try:
url  = f”https://query1.finance.yahoo.com/v8/finance/chart/{yahoo_symbol}”
resp = requests.get(
url,
params={“interval”: “30m”, “range”: “60d”},
headers={“User-Agent”: “Mozilla/5.0”},
timeout=15,
)
resp.raise_for_status()
data   = resp.json()
result = data[“chart”][“result”][0]
timestamps = result.get(“timestamp”, [])
quotes     = result[“indicators”][“quote”][0]
df = pd.DataFrame({
“open_time”: timestamps,
“high”:      quotes.get(“high”, []),
“low”:       quotes.get(“low”, []),
“close”:     quotes.get(“close”, []),
“volume”:    quotes.get(“volume”, []),
})
df = df.dropna(subset=[“close”, “high”, “low”]).reset_index(drop=True)
df[“close”]  = df[“close”].astype(float)
df[“high”]   = df[“high”].astype(float)
df[“low”]    = df[“low”].astype(float)
df[“volume”] = df[“volume”].astype(float)
if len(df) < EMA_SLOW + 10:
logger.info(f”Not enough candles yet: {len(df)}, need {EMA_SLOW + 10}”)
return None
return df.tail(CANDLE_COUNT).reset_index(drop=True)
except Exception as e:
logger.error(f”Data fetch error for {yahoo_symbol}: {e}”)
return None

def get_last_price(yahoo_symbol: str):
try:
url  = f”https://query1.finance.yahoo.com/v8/finance/chart/{yahoo_symbol}”
resp = requests.get(
url,
params={“interval”: “1m”, “range”: “1d”},
headers={“User-Agent”: “Mozilla/5.0”},
timeout=15,
)
resp.raise_for_status()
data   = resp.json()
closes = data[“chart”][“result”][0][“indicators”][“quote”][0][“close”]
price  = next((c for c in reversed(closes) if c is not None), None)
return float(price) if price else None
except Exception as e:
logger.error(f”Price fetch error: {e}”)
return None

def compute_indicators(df: pd.DataFrame) -> pd.DataFrame:
df = df.copy()
df[“ema_fast”] = df[“close”].ewm(span=EMA_FAST, adjust=False).mean()
df[“ema_slow”] = df[“close”].ewm(span=EMA_SLOW, adjust=False).mean()
highest_high   = df[“high”].rolling(window=WILLIAMS_PERIOD).max()
lowest_low     = df[“low”].rolling(window=WILLIAMS_PERIOD).min()
df[“williams_r”] = ((highest_high - df[“close”]) / (highest_high - lowest_low).replace(0, 1)) * -100
ema_fast_macd    = df[“close”].ewm(span=MACD_FAST, adjust=False).mean()
ema_slow_macd    = df[“close”].ewm(span=MACD_SLOW, adjust=False).mean()
df[“macd”]       = ema_fast_macd - ema_slow_macd
df[“macd_signal”] = df[“macd”].ewm(span=MACD_SIGNAL, adjust=False).mean()
df[“macd_hist”]  = df[“macd”] - df[“macd_signal”]
high_low   = df[“high”] - df[“low”]
high_close = (df[“high”] - df[“close”].shift()).abs()
low_close  = (df[“low”]  - df[“close”].shift()).abs()
true_range = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
df[“atr”]  = true_range.ewm(span=ATR_PERIOD, adjust=False).mean()
return df

def detect_signal(df: pd.DataFrame):
curr = df.iloc[-1]
prev = df.iloc[-2]
in_uptrend         = curr[“ema_fast”] > curr[“ema_slow”]
wr_curr            = curr[“williams_r”]
wr_prev            = prev[“williams_r”]
macd_curr          = curr[“macd_hist”]
macd_prev          = prev[“macd_hist”]
wr_exits_oversold  = wr_prev <= WR_OVERSOLD and wr_curr > WR_OVERSOLD
macd_turns_bullish = macd_prev < 0 and macd_curr >= 0
if in_uptrend and wr_exits_oversold and macd_turns_bullish:
return “BUY”
wr_overbought      = wr_curr >= WR_OVERBOUGHT
macd_turns_bearish = macd_prev >= 0 and macd_curr < 0
trend_reversed     = curr[“ema_fast”] < curr[“ema_slow”]
if wr_overbought or macd_turns_bearish or trend_reversed:
return “SELL”
return None

def paper_buy(pair: dict, price: float, atr: float):
state = paper_states[pair[“symbol”]]
if state[“open_position”]:
return None
usdt      = state[“usdt_balance”]
amount    = usdt * (RISK_PERCENT / 100)
qty       = round(amount / price, 6)
cost      = qty * price
if cost > usdt:
return None
stop_loss = round(price - (ATR_MULTIPLIER * atr), 4)
state[“usdt_balance”]  -= cost
state[“coin_balance”]  += qty
state[“open_position”]  = {“entry”: price, “qty”: qty, “stop_loss”: stop_loss, “atr”: atr}
state[“trade_count”]   += 1
return {“side”: “BUY”, “qty”: qty, “price”: price, “stop_loss”: stop_loss, “atr”: atr,
“order_id”: f”PAPER-{pair[‘name’]}-{state[‘trade_count’]:04d}”}

def paper_sell(pair: dict, price: float, reason: str = “SIGNAL”):
state = paper_states[pair[“symbol”]]
qty   = state[“coin_balance”]
if qty <= 0.000001:
return None
proceeds = qty * price
state[“usdt_balance”] += proceeds
state[“coin_balance”]  = 0.0
pnl = pnl_pct = 0.0
entry = None
if state[“open_position”]:
entry   = state[“open_position”][“entry”]
pnl     = (price - entry) * qty
pnl_pct = ((price - entry) / entry) * 100
state[“total_pnl”] += pnl
if pnl >= 0:
state[“wins”] += 1
else:
state[“losses”] += 1
state[“open_position”] = None
state[“trade_count”] += 1
trade = {“side”: “SELL”, “qty”: round(qty, 6), “price”: price,
“pnl”: pnl, “pnl_pct”: pnl_pct, “reason”: reason,
“order_id”: f”PAPER-{pair[‘name’]}-{state[‘trade_count’]:04d}”}
state[“trade_log”].append({“entry”: entry, “exit”: price, “pnl”: pnl, “reason”: reason,
“time”: datetime.now(timezone.utc).strftime(”%Y-%m-%d %H:%M”)})
return trade

def check_stop_loss(pair: dict, price: float) -> bool:
state = paper_states[pair[“symbol”]]
pos   = state[“open_position”]
if pos and price <= pos[“stop_loss”]:
logger.warning(f”{pair[‘name’]}: ATR Stop-loss at ${price:.2f}”)
trade = paper_sell(pair, price, reason=“STOP-LOSS”)
if trade:
send_telegram(format_message(pair, trade))
return True
return False

def format_message(pair: dict, trade: dict) -> str:
state  = paper_states[pair[“symbol”]]
ts     = datetime.now(timezone.utc).strftime(”%Y-%m-%d %H:%M:%S UTC”)
side   = trade[“side”]
reason = trade.get(“reason”, “SIGNAL”)
if reason == “STOP-LOSS”:
emoji, header = “⚠️”, “ATR STOP-LOSS TRIGGERED”
elif side == “BUY”:
emoji, header = “🟢”, “BUY Signal Executed”
else:
emoji, header = “🔴”, “SELL Signal Executed”
lines = [f”{emoji} <b>{header}</b>”, f”📅 {ts}”, f”📦 Mode: PAPER v2.0”,
f”💱 Pair: {pair[‘name’]}/USDT”, f”💰 Price: ${trade[‘price’]:,.4f}”,
f”📊 Qty: {trade[‘qty’]} {pair[‘name’]}”]
if side == “BUY”:
lines += [f”🛡 Stop-Loss: ${trade[‘stop_loss’]:,.4f} (ATR x{ATR_MULTIPLIER})”,
f”📐 ATR: ${trade[‘atr’]:,.2f}”, f”⚠️ Risk: {RISK_PERCENT}%”]
if side == “SELL” or reason == “STOP-LOSS”:
pnl     = trade.get(“pnl”, 0)
pnl_pct = trade.get(“pnl_pct”, 0)
icon    = “✅” if pnl >= 0 else “❌”
lines  += [f”{icon} PnL: ${pnl:+.2f} ({pnl_pct:+.2f}%)”, f”📋 Reason: {reason}”]
lines += [f”💼 Balance: ${state[‘usdt_balance’]:,.2f} USDT”,
f”📈 Total PnL: ${state[‘total_pnl’]:+.2f}”,
f”🏆 W:{state[‘wins’]} ❌ L:{state[‘losses’]}”, f”🎫 {trade[‘order_id’]}”]
return “\n”.join(lines)

def format_daily_summary() -> str:
lines     = [“📊 <b>Daily Summary v2.0</b>”, “”]
total_pnl = 0.0
total_w   = total_l = 0
for pair in PAIRS:
s          = paper_states[pair[“symbol”]]
total_pnl += s[“total_pnl”]
total_w   += s[“wins”]
total_l   += s[“losses”]
winrate    = round(s[“wins”] / max(s[“wins”] + s[“losses”], 1) * 100)
pos_info   = f” | 📌 Open @ ${s[‘open_position’][‘entry’]:,.2f}” if s[“open_position”] else “”
lines.append(f”<b>{pair[‘name’]}</b>: ${s[‘usdt_balance’]:,.2f} | “
f”PnL: ${s[‘total_pnl’]:+.2f} | W:{s[‘wins’]} L:{s[‘losses’]} ({winrate}%){pos_info}”)
total_wr = round(total_w / max(total_w + total_l, 1) * 100)
lines += [””, f”💰 <b>Total PnL: ${total_pnl:+.2f}</b>”,
f”🏆 W:{total_w} L:{total_l} ({total_wr}% winrate)”,
f”⚙️ EMA {EMA_FAST}/{EMA_SLOW} + Williams %R + MACD + ATR”]
return “\n”.join(lines)

def run_bot():
logger.info(“Advanced Trading Bot v2.0 Starting”)
startup_msg = (
f”🤖 <b>Advanced Trading Bot v2.0</b>\n”
f”Mode: PAPER TRADING\n”
f”Pairs: BTC | ETH | SOL\n”
f”Strategy: EMA {EMA_FAST}/{EMA_SLOW} + Williams %R + MACD + ATR\n”
f”Timeframe: 30 min | Risk: {RISK_PERCENT}%\n”
f”💼 $3,333 per pair ($10,000 total)\n\n”
f”✅ vs v1.0:\n”
f”• EMA 200 trend filter\n”
f”• Williams %R precise entry\n”
f”• MACD confirmation\n”
f”• Dynamic ATR stop-loss\n”
f”• 30min timeframe”
)
send_telegram(startup_msg)

```
check_count   = 0
summary_every = 48

while True:
    check_count += 1
    logger.info(f"--- Check #{check_count} ---")

    for pair in PAIRS:
        try:
            df = get_klines(pair["yahoo"])
            if df is None:
                continue
            df    = compute_indicators(df)
            price = get_last_price(pair["yahoo"])
            if price is None:
                continue
            curr       = df.iloc[-1]
            atr        = curr["atr"]
            in_uptrend = curr["ema_fast"] > curr["ema_slow"]
            logger.info(
                f"{pair['name']} ${price:,.2f} | "
                f"EMA{EMA_FAST}={curr['ema_fast']:.2f} EMA{EMA_SLOW}={curr['ema_slow']:.2f} | "
                f"W%R={curr['williams_r']:.1f} MACD={curr['macd_hist']:.4f} ATR={atr:.2f} | "
                f"Trend={'UP' if in_uptrend else 'DOWN'}"
            )
            state = paper_states[pair["symbol"]]
            if PAPER_TRADE and state["open_position"]:
                if check_stop_loss(pair, price):
                    continue
            signal = detect_signal(df)
            if signal == "BUY" and PAPER_TRADE:
                trade = paper_buy(pair, price, atr)
                if trade:
                    send_telegram(format_message(pair, trade))
                    logger.info(f"{pair['name']} BUY @ ${price:,.2f}")
            elif signal == "SELL" and PAPER_TRADE:
                if state["open_position"]:
                    trade = paper_sell(pair, price, reason="SIGNAL")
                    if trade:
                        send_telegram(format_message(pair, trade))
                        logger.info(f"{pair['name']} SELL @ ${price:,.2f}")
        except Exception as e:
            err = f"❌ <b>{pair['name']} Error</b>\n{type(e).__name__}: {e}"
            logger.error(err)
            send_telegram(err)
        time.sleep(2)

    if check_count % summary_every == 0:
        send_telegram(format_daily_summary())

    logger.info(f"Sleeping 30 minutes...")
    time.sleep(CHECK_INTERVAL)
```

if **name** == “**main**”:
run_bot()
