"""
Multi-Pair EMA Crossover Trading Bot
- Pairs: BTC/USDT, ETH/USDT, SOL/USDT
- Market data: Yahoo Finance (5-min candles)
- Strategy: EMA 9/21 crossover + RSI filter
- Risk: 3% per trade, 2% stop-loss
- Notifications: Telegram (separate for each pair)
- Mode: PAPER_TRADE = True (safe simulation)
"""

import os
import time
import logging
import requests
import pandas as pd
from datetime import datetime, timezone
from pybit.unified_trading import HTTP

# ── Logging ──────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("bot/bot.log"),
    ],
)
logger = logging.getLogger(__name__)

# ── Configuration ────────────────────────────────────────
BYBIT_API_KEY    = os.environ["BYBIT_API_KEY"]
BYBIT_API_SECRET = os.environ["BYBIT_API_SECRET"]
TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID   = os.environ["TELEGRAM_CHAT_ID"]

# Trading pairs: (Bybit symbol, Yahoo Finance symbol)
PAIRS = [
    {"symbol": "BTCUSDT",  "yahoo": "BTC-USD",  "name": "BTC"},
    {"symbol": "ETHUSDT",  "yahoo": "ETH-USD",  "name": "ETH"},
    {"symbol": "SOLUSDT",  "yahoo": "SOL-USD",  "name": "SOL"},
]

EMA_FAST       = 9
EMA_SLOW       = 21
RSI_PERIOD     = 14
RSI_BUY_MIN    = 45
RSI_BUY_MAX    = 68
RSI_SELL_MIN   = 52
RISK_PERCENT   = 3.0       # % of balance risked per trade
STOP_LOSS_PCT  = 0.02      # 2% stop-loss
CHECK_INTERVAL = 300       # seconds between checks (5 min)
CANDLE_COUNT   = 100
PAPER_TRADE    = True      # True = simulation, False = real money

# ── Paper trading state (one per pair) ───────────────────
def make_paper_state(balance=3333.0):
    """Each pair gets equal share of $10,000 virtual balance."""
    return {
        "usdt_balance":   balance,
        "coin_balance":   0.0,
        "open_position":  None,
        "trade_count":    0,
        "wins":           0,
        "losses":         0,
        "total_pnl":      0.0,
    }

paper_states = {p["symbol"]: make_paper_state() for p in PAIRS}

# ── Telegram ─────────────────────────────────────────────
def send_telegram(message: str) -> bool:
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "HTML",
    }
    try:
        resp = requests.post(url, json=payload, timeout=10)
        if resp.status_code == 400 and "chat not found" in resp.text:
            logger.warning("Telegram: chat not found. Send /start to your bot.")
            return False
        resp.raise_for_status()
        return True
    except Exception as e:
        logger.error(f"Telegram error: {e}")
        return False

# ── Market data ───────────────────────────────────────────
def get_klines(yahoo_symbol: str) -> pd.DataFrame:
    """Fetch 5-minute candles from Yahoo Finance."""
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{yahoo_symbol}"
    resp = requests.get(
        url,
        params={"interval": "5m", "range": "1d"},
        headers={"User-Agent": "Mozilla/5.0"},
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()
    result = data["chart"]["result"][0]
    df = pd.DataFrame({
        "open_time": result["timestamp"],
        "close":     result["indicators"]["quote"][0]["close"],
    })
    df = df.dropna(subset=["close"]).reset_index(drop=True)
    df["close"] = df["close"].astype(float)
    if len(df) < EMA_SLOW + 2:
        raise ValueError(f"Not enough candles: {len(df)}")
    return df.tail(CANDLE_COUNT).reset_index(drop=True)

def get_last_price(yahoo_symbol: str) -> float:
    """Get current price."""
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{yahoo_symbol}"
    resp = requests.get(
        url,
        params={"interval": "1m", "range": "1d"},
        headers={"User-Agent": "Mozilla/5.0"},
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()
    closes = data["chart"]["result"][0]["indicators"]["quote"][0]["close"]
    return float(next(c for c in reversed(closes) if c is not None))

# ── Indicators ────────────────────────────────────────────
def compute_indicators(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["ema_fast"] = df["close"].ewm(span=EMA_FAST, adjust=False).mean()
    df["ema_slow"] = df["close"].ewm(span=EMA_SLOW, adjust=False).mean()
    # RSI
    delta = df["close"].diff()
    gain  = delta.clip(lower=0).ewm(span=RSI_PERIOD, adjust=False).mean()
    loss  = (-delta.clip(upper=0)).ewm(span=RSI_PERIOD, adjust=False).mean()
    rs    = gain / loss.replace(0, float("inf"))
    df["rsi"] = 100 - (100 / (1 + rs))
    return df

def detect_signal(df: pd.DataFrame) -> str | None:
    prev_fast = df["ema_fast"].iloc[-2]
    prev_slow = df["ema_slow"].iloc[-2]
    curr_fast = df["ema_fast"].iloc[-1]
    curr_slow = df["ema_slow"].iloc[-1]
    rsi       = df["rsi"].iloc[-1]

    # BUY: EMA crossover up + RSI in buy zone
    if prev_fast <= prev_slow and curr_fast > curr_slow:
        if RSI_BUY_MIN <= rsi <= RSI_BUY_MAX:
            return "BUY"

    # SELL: EMA crossover down + RSI above sell minimum
    if prev_fast >= prev_slow and curr_fast < curr_slow:
        if rsi >= RSI_SELL_MIN:
            return "SELL"

    return None

# ── Paper trading ─────────────────────────────────────────
def paper_buy(pair: dict, price: float) -> dict | None:
    state = paper_states[pair["symbol"]]
    usdt  = state["usdt_balance"]
    amount = usdt * (RISK_PERCENT / 100)
    qty    = round(amount / price, 6)
    cost   = qty * price

    if cost > usdt:
        logger.warning(f"{pair['name']}: Insufficient balance for buy.")
        return None
    if state["open_position"]:
        logger.info(f"{pair['name']}: Already in position, skipping BUY.")
        return None

    stop_loss = round(price * (1 - STOP_LOSS_PCT), 4)
    state["usdt_balance"]  -= cost
    state["coin_balance"]  += qty
    state["open_position"]  = {"entry": price, "qty": qty, "stop_loss": stop_loss}
    state["trade_count"]   += 1

    return {
        "side": "BUY", "qty": qty, "price": price,
        "stop_loss": stop_loss,
        "order_id": f"PAPER-{pair['name']}-{state['trade_count']:04d}",
    }

def paper_sell(pair: dict, price: float, stop_loss_hit=False) -> dict | None:
    state = paper_states[pair["symbol"]]
    qty   = state["coin_balance"]

    if qty <= 0.000001:
        logger.warning(f"{pair['name']}: No coins to sell.")
        return None

    proceeds = qty * price
    state["usdt_balance"] += proceeds
    state["coin_balance"]  = 0.0

    pnl = pnl_pct = 0.0
    entry = None
    if state["open_position"]:
        entry   = state["open_position"]["entry"]
        pnl     = (price - entry) * qty
        pnl_pct = ((price - entry) / entry) * 100
        state["total_pnl"] += pnl
        if pnl >= 0:
            state["wins"] += 1
        else:
            state["losses"] += 1
        state["open_position"] = None

    state["trade_count"] += 1

    return {
        "side": "SELL", "qty": round(qty, 6), "price": price,
        "pnl": pnl, "pnl_pct": pnl_pct,
        "stop_loss_hit": stop_loss_hit,
        "order_id": f"PAPER-{pair['name']}-{state['trade_count']:04d}",
    }

def check_stop_loss(pair: dict, price: float) -> bool:
    state = paper_states[pair["symbol"]]
    pos   = state["open_position"]
    if pos and price <= pos["stop_loss"]:
        logger.warning(f"{pair['name']}: Stop-loss triggered at ${price:.2f}")
        trade = paper_sell(pair, price, stop_loss_hit=True)
        if trade:
            send_telegram(format_message(pair, trade))
        return True
    return False

# ── Message formatting ────────────────────────────────────
def format_message(pair: dict, trade: dict) -> str:
    state = paper_states[pair["symbol"]]
    ts    = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    side  = trade["side"]

    if trade.get("stop_loss_hit"):
        emoji, header = "⚠️", "STOP-LOSS TRIGGERED"
    elif side == "BUY":
        emoji, header = "🟢", "BUY Signal Executed"
    else:
        emoji, header = "🔴", "SELL Signal Executed"

    lines = [
        f"{emoji} <b>{header}</b>",
        f"📅 {ts}",
        f"📦 Mode: PAPER",
        f"💱 Pair: {pair['name']}/USDT",
        f"💰 Price: ${trade['price']:,.2f}",
        f"📊 Qty: {trade['qty']} {pair['name']}",
    ]

    if side == "BUY":
        lines.append(f"🛡 Stop-Loss: ${trade['stop_loss']:,.2f} (2% below)")
        lines.append(f"⚠️ Risk: {RISK_PERCENT}% of balance")

    if side == "SELL" or trade.get("stop_loss_hit"):
        pnl     = trade.get("pnl", 0)
        pnl_pct = trade.get("pnl_pct", 0)
        icon    = "✅" if pnl >= 0 else "❌"
        lines.append(f"{icon} PnL: ${pnl:+.2f} ({pnl_pct:+.2f}%)")

    lines += [
        f"💼 Balance: ${state['usdt_balance']:,.2f} USDT + {state['coin_balance']:.6f} {pair['name']}",
        f"📈 Total PnL: ${state['total_pnl']:+.2f}",
        f"🏆 Wins: {state['wins']} | ❌ Losses: {state['losses']}",
        f"🎫 Order ID: {trade['order_id']}",
    ]
    return "\n".join(lines)

def format_summary() -> str:
    """Daily summary for all pairs."""
    lines = ["📊 <b>Daily Summary — All Pairs</b>", ""]
    total_pnl = 0.0
    for pair in PAIRS:
        s = paper_states[pair["symbol"]]
        total_pnl += s["total_pnl"]
        lines.append(
            f"<b>{pair['name']}</b>: ${s['usdt_balance']:,.2f} USDT | "
            f"PnL: ${s['total_pnl']:+.2f} | "
            f"W:{s['wins']} L:{s['losses']}"
        )
    lines += ["", f"💰 <b>Total PnL: ${total_pnl:+.2f}</b>"]
    return "\n".join(lines)

# ── Main loop ─────────────────────────────────────────────
def run_bot():
    logger.info("Starting Multi-Pair Trading Bot")
    logger.info(f"Pairs: {[p['name'] for p in PAIRS]}")
    logger.info(f"Strategy: EMA {EMA_FAST}/{EMA_SLOW} + RSI({RSI_PERIOD})")
    logger.info(f"Mode: {'PAPER TRADING' if PAPER_TRADE else 'LIVE TRADING'}")

    startup_msg = (
        f"🤖 <b>Multi-Pair Trading Bot Started</b>\n"
        f"Mode: {'PAPER TRADING' if PAPER_TRADE else '⚠️ LIVE TRADING'}\n"
        f"Pairs: BTC/USDT | ETH/USDT | SOL/USDT\n"
        f"Strategy: EMA {EMA_FAST}/{EMA_SLOW} + RSI({RSI_PERIOD})\n"
        f"Risk per trade: {RISK_PERCENT}% | Stop-loss: {int(STOP_LOSS_PCT*100)}%\n"
        f"Check interval: {CHECK_INTERVAL}s\n"
        f"💼 Starting balance: $3,333 USDT per pair ($10,000 total)"
    )
    send_telegram(startup_msg)

    check_count = 0
    summary_every = 288  # ~24 hours at 5-min intervals

    while True:
        check_count += 1

        for pair in PAIRS:
            try:
                df    = get_klines(pair["yahoo"])
                df    = compute_indicators(df)
                price = get_last_price(pair["yahoo"])

                curr_fast = df["ema_fast"].iloc[-1]
                curr_slow = df["ema_slow"].iloc[-1]
                rsi       = df["rsi"].iloc[-1]

                logger.info(
                    f"{pair['name']} ${price:,.2f} | "
                    f"EMA{EMA_FAST}={curr_fast:.2f} EMA{EMA_SLOW}={curr_slow:.2f} | "
                    f"RSI={rsi:.1f}"
                )

                # Check stop-loss first
                if PAPER_TRADE and paper_states[pair["symbol"]]["open_position"]:
                    if check_stop_loss(pair, price):
                        continue

                signal = detect_signal(df)

                if signal == "BUY":
                    trade = paper_buy(pair, price) if PAPER_TRADE else None
                    if trade:
                        send_telegram(format_message(pair, trade))
                        logger.info(f"{pair['name']} BUY executed at ${price:,.2f}")

                elif signal == "SELL":
                    trade = paper_sell(pair, price) if PAPER_TRADE else None
                    if trade:
                        send_telegram(format_message(pair, trade))
                        logger.info(f"{pair['name']} SELL executed at ${price:,.2f}")

            except Exception as e:
                err = f"❌ <b>{pair['name']} Error</b>\n{e}"
                logger.error(err)
                send_telegram(err)

        # Daily summary
        if check_count % summary_every == 0:
            send_telegram(format_summary())

        time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    run_bot()
