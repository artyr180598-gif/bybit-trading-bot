"""
BTC/USDT EMA Crossover Trading Bot
- Market data: Yahoo Finance (5-min BTC/USD candles, no IP restrictions)
- Order execution: Bybit spot API v5 (live/testnet) OR paper trading mode
- Notifications: Telegram
- Strategy: EMA 9 / EMA 21 crossover with 3% risk and 2% stop-loss

CONFIGURATION (edit below):
  TESTNET      = True   → Use Bybit testnet for real API calls
  PAPER_TRADE  = True   → Simulate trades locally (no API calls for orders)
                          Set False to execute real/testnet orders on Bybit

NOTE: Bybit blocks US-based IPs from their API (including testnet).
If running on a US server (e.g. Replit free tier), use PAPER_TRADE = True.
For real trading, deploy to a VPS outside the US, or use a Bybit mainnet
account with IP whitelisting disabled and set TESTNET = False, PAPER_TRADE = False.
"""

import os
import time
import logging
import requests
import pandas as pd
from datetime import datetime
from pybit.unified_trading import HTTP

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("bot/bot.log"),
    ],
)
logger = logging.getLogger(__name__)

# ── Configuration ─────────────────────────────────────────────────────────────
BYBIT_API_KEY = os.environ["BYBIT_API_KEY"]
BYBIT_API_SECRET = os.environ["BYBIT_API_SECRET"]
TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

SYMBOL = "BTCUSDT"
YAHOO_SYMBOL = "BTC-USD"
EMA_FAST = 9
EMA_SLOW = 21
RISK_PERCENT = 3.0          # % of balance risked per trade
STOP_LOSS_PCT = 0.02        # 2% below entry for stop-loss
CHECK_INTERVAL = 300        # seconds between checks (5 minutes)
CANDLE_COUNT = 100

TESTNET = True              # True = Bybit testnet, False = live mainnet
PAPER_TRADE = True          # True = simulate trades locally (no Bybit API needed)

# ── Paper trading state ───────────────────────────────────────────────────────
paper_state = {
    "usdt_balance": 10_000.0,   # starting simulated USDT balance
    "btc_balance": 0.0,
    "open_position": None,      # {"entry": price, "qty": qty, "stop_loss": price}
    "trade_count": 0,
}


# ── Bybit client ──────────────────────────────────────────────────────────────
def get_client() -> HTTP:
    return HTTP(testnet=TESTNET, api_key=BYBIT_API_KEY, api_secret=BYBIT_API_SECRET)


# ── Telegram ──────────────────────────────────────────────────────────────────
def send_telegram(message: str) -> bool:
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "HTML"}
    try:
        resp = requests.post(url, json=payload, timeout=10)
        if resp.status_code == 400 and "chat not found" in resp.text:
            logger.warning(
                "Telegram: 'chat not found'. Open Telegram, find your bot and "
                "send /start so it can message you."
            )
            return False
        resp.raise_for_status()
        logger.info("Telegram notification sent.")
        return True
    except Exception as e:
        logger.error(f"Telegram error: {e}")
        return False


# ── Market data (Yahoo Finance — no IP restrictions) ──────────────────────────
def get_klines() -> pd.DataFrame:
    """Fetch 5-minute BTC/USD candles from Yahoo Finance."""
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{YAHOO_SYMBOL}"
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
        "close": result["indicators"]["quote"][0]["close"],
    })
    df = df.dropna(subset=["close"]).reset_index(drop=True)
    df["close"] = df["close"].astype(float)
    if len(df) < EMA_SLOW + 2:
        raise ValueError(f"Not enough candles: {len(df)}, need {EMA_SLOW + 2}")
    return df.tail(CANDLE_COUNT).reset_index(drop=True)


def get_last_price() -> float:
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{YAHOO_SYMBOL}"
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


# ── EMA + signal ──────────────────────────────────────────────────────────────
def compute_emas(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["ema_fast"] = df["close"].ewm(span=EMA_FAST, adjust=False).mean()
    df["ema_slow"] = df["close"].ewm(span=EMA_SLOW, adjust=False).mean()
    return df


def detect_crossover(df: pd.DataFrame) -> str | None:
    prev_fast, prev_slow = df["ema_fast"].iloc[-2], df["ema_slow"].iloc[-2]
    curr_fast, curr_slow = df["ema_fast"].iloc[-1], df["ema_slow"].iloc[-1]
    if prev_fast <= prev_slow and curr_fast > curr_slow:
        return "BUY"
    if prev_fast >= prev_slow and curr_fast < curr_slow:
        return "SELL"
    return None


# ── Paper trading ─────────────────────────────────────────────────────────────
def paper_buy(price: float) -> dict | None:
    usdt = paper_state["usdt_balance"]
    amount = usdt * (RISK_PERCENT / 100)
    qty = round(amount / price, 6)
    cost = qty * price

    if cost > usdt:
        logger.warning("Paper trade: insufficient USDT balance.")
        return None

    paper_state["usdt_balance"] -= cost
    paper_state["btc_balance"] += qty
    stop_loss = round(price * (1 - STOP_LOSS_PCT), 2)
    paper_state["open_position"] = {"entry": price, "qty": qty, "stop_loss": stop_loss}
    paper_state["trade_count"] += 1

    return {
        "side": "BUY",
        "qty": qty,
        "price": price,
        "stop_loss": stop_loss,
        "order_id": f"PAPER-{paper_state['trade_count']:04d}",
    }


def paper_sell(price: float) -> dict | None:
    qty = paper_state["btc_balance"]
    if qty <= 0.000001:
        logger.warning("Paper trade: no BTC to sell.")
        return None

    proceeds = qty * price
    paper_state["usdt_balance"] += proceeds
    paper_state["btc_balance"] = 0.0

    entry = None
    if paper_state["open_position"]:
        entry = paper_state["open_position"]["entry"]
        paper_state["open_position"] = None

    paper_state["trade_count"] += 1

    result = {
        "side": "SELL",
        "qty": round(qty, 6),
        "price": price,
        "order_id": f"PAPER-{paper_state['trade_count']:04d}",
    }
    if entry:
        pnl = (price - entry) * qty
        pnl_pct = ((price - entry) / entry) * 100
        result["pnl"] = pnl
        result["pnl_pct"] = pnl_pct

    return result


def check_paper_stop_loss(price: float) -> bool:
    """Returns True if stop-loss was triggered."""
    pos = paper_state["open_position"]
    if pos and price <= pos["stop_loss"]:
        logger.warning(
            f"Stop-loss triggered! Price ${price:,.2f} hit stop ${pos['stop_loss']:,.2f}"
        )
        trade = paper_sell(price)
        if trade:
            trade["stop_loss_hit"] = True
            send_telegram(format_trade_message(trade))
        return True
    return False


# ── Live Bybit order execution ────────────────────────────────────────────────
def get_lot_size_filter(client: HTTP) -> dict:
    resp = client.get_instruments_info(category="spot", symbol=SYMBOL)
    filters = resp["result"]["list"][0]["lotSizeFilter"]
    return {
        "min_qty": float(filters["minOrderQty"]),
        "qty_step": float(filters["basePrecision"]),
    }


def round_qty(qty: float, step: float) -> float:
    precision = len(str(step).rstrip("0").split(".")[-1]) if "." in str(step) else 0
    return round(round(qty / step) * step, precision)


def live_buy(client: HTTP, price: float) -> dict | None:
    resp = client.get_wallet_balance(accountType="UNIFIED", coin="USDT")
    usdt = 0.0
    for coin in resp["result"]["list"][0]["coin"]:
        if coin["coin"] == "USDT":
            usdt = float(coin["walletBalance"])
    lot = get_lot_size_filter(client)
    qty = round_qty((usdt * RISK_PERCENT / 100) / price, lot["qty_step"])
    if qty < lot["min_qty"]:
        logger.warning(f"Qty {qty} below min {lot['min_qty']}. Skipping BUY.")
        return None
    stop_loss = round(price * (1 - STOP_LOSS_PCT), 2)
    resp = client.place_order(
        category="spot", symbol=SYMBOL, side="Buy", orderType="Market", qty=str(qty)
    )
    return {"side": "BUY", "qty": qty, "price": price, "stop_loss": stop_loss,
            "order_id": resp["result"].get("orderId", "N/A")}


def live_sell(client: HTTP, price: float) -> dict | None:
    resp = client.get_wallet_balance(accountType="UNIFIED", coin="BTC")
    btc = 0.0
    for coin in resp["result"]["list"][0]["coin"]:
        if coin["coin"] == "BTC":
            btc = float(coin["walletBalance"])
    lot = get_lot_size_filter(client)
    qty = round_qty(btc, lot["qty_step"])
    if qty < lot["min_qty"]:
        logger.warning(f"BTC {btc} below min. Skipping SELL.")
        return None
    resp = client.place_order(
        category="spot", symbol=SYMBOL, side="Sell", orderType="Market", qty=str(qty)
    )
    return {"side": "SELL", "qty": qty, "price": price,
            "order_id": resp["result"].get("orderId", "N/A")}


# ── Message formatting ────────────────────────────────────────────────────────
def format_trade_message(trade: dict) -> str:
    mode = "PAPER" if PAPER_TRADE else ("TESTNET" if TESTNET else "LIVE")
    ts = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
    side = trade["side"]
    emoji = "🟢" if side == "BUY" else "🔴"

    if trade.get("stop_loss_hit"):
        emoji = "⚠️"
        header = "STOP-LOSS TRIGGERED"
    else:
        header = f"{side} Signal Executed"

    lines = [
        f"{emoji} <b>{header}</b>",
        f"📅 {ts}",
        f"🏷 Mode: {mode}",
        f"💱 Symbol: {SYMBOL}",
        f"📊 Strategy: EMA {EMA_FAST}/{EMA_SLOW} Crossover",
        f"💰 Price: ${trade['price']:,.2f}",
        f"📦 Qty: {trade['qty']} BTC",
    ]

    if side == "BUY":
        lines.append(f"🛡 Risk: {RISK_PERCENT}% of balance")
        if "stop_loss" in trade:
            lines.append(
                f"🚨 Stop-Loss: ${trade['stop_loss']:,.2f} ({int(STOP_LOSS_PCT * 100)}% below)"
            )

    if side == "SELL" and "pnl" in trade:
        pnl = trade["pnl"]
        pnl_pct = trade["pnl_pct"]
        pnl_emoji = "✅" if pnl >= 0 else "❌"
        lines.append(f"{pnl_emoji} PnL: ${pnl:+,.2f} ({pnl_pct:+.2f}%)")

    if PAPER_TRADE:
        lines.append(
            f"💼 Paper Balance: ${paper_state['usdt_balance']:,.2f} USDT + "
            f"{paper_state['btc_balance']:.6f} BTC"
        )

    lines.append(f"🔖 Order ID: {trade['order_id']}")
    return "\n".join(lines)


# ── Main loop ─────────────────────────────────────────────────────────────────
def run_bot() -> None:
    mode_label = "PAPER TRADING" if PAPER_TRADE else ("TESTNET" if TESTNET else "LIVE")
    logger.info(f"Starting EMA {EMA_FAST}/{EMA_SLOW} bot — {SYMBOL} — {mode_label}")
    logger.info("Market data: Yahoo Finance (5-min BTC/USD candles)")
    if PAPER_TRADE:
        logger.info(f"Paper trading — starting balance: ${paper_state['usdt_balance']:,.2f} USDT")

    client = None if PAPER_TRADE else get_client()

    startup_msg = (
        f"🤖 <b>Trading Bot Started</b>\n"
        f"Mode: {mode_label}\n"
        f"Symbol: {SYMBOL}\n"
        f"Strategy: EMA {EMA_FAST} / EMA {EMA_SLOW} crossover\n"
        f"Risk per trade: {RISK_PERCENT}%\n"
        f"Stop-loss: {int(STOP_LOSS_PCT * 100)}%\n"
        f"Check interval: {CHECK_INTERVAL}s"
    )
    if PAPER_TRADE:
        startup_msg += f"\n💼 Starting balance: ${paper_state['usdt_balance']:,.2f} USDT"

    if not send_telegram(startup_msg):
        logger.warning(
            "Telegram startup failed. Send /start to your bot in Telegram, "
            "then verify TELEGRAM_CHAT_ID is your numeric user ID."
        )

    while True:
        try:
            df = get_klines()
            df = compute_emas(df)
            signal = detect_crossover(df)
            price = get_last_price()

            curr_fast = df["ema_fast"].iloc[-1]
            curr_slow = df["ema_slow"].iloc[-1]

            logger.info(
                f"BTC=${price:,.2f} | EMA{EMA_FAST}={curr_fast:.2f} | "
                f"EMA{EMA_SLOW}={curr_slow:.2f} | Signal={signal or 'NONE'}"
            )

            # Check stop-loss on open paper position
            if PAPER_TRADE and paper_state["open_position"]:
                if check_paper_stop_loss(price):
                    time.sleep(CHECK_INTERVAL)
                    continue

            if signal == "BUY":
                if PAPER_TRADE:
                    trade = paper_buy(price)
                else:
                    trade = live_buy(client, price)
                if trade:
                    send_telegram(format_trade_message(trade))
                    logger.info(f"BUY executed: {trade}")

            elif signal == "SELL":
                if PAPER_TRADE:
                    trade = paper_sell(price)
                else:
                    trade = live_sell(client, price)
                if trade:
                    send_telegram(format_trade_message(trade))
                    logger.info(f"SELL executed: {trade}")

        except KeyboardInterrupt:
            logger.info("Bot stopped by user.")
            send_telegram("🛑 <b>Trading Bot Stopped</b>\nBot was manually stopped.")
            break
        except Exception as e:
            err_msg = f"❌ <b>Bot Error</b>\n{type(e).__name__}: {e}"
            logger.error(err_msg)
            send_telegram(err_msg)

        time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    run_bot()
