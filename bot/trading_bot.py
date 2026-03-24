import os
import time
import logging
import requests
import pandas as pd
import ccxt
from datetime import datetime, timezone

# ── Logging ──────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler()],
)
logger = logging.getLogger(__name__)

# ── Configuration (Railway Variables) ─────────────────────
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID   = os.environ.get("TELEGRAM_CHAT_ID")

# Инициализация Bybit через CCXT для обхода блокировок
EXCHANGE = ccxt.bybit({
    'enableRateLimit': True,
    'options': {'brokerId': 'CCXT'}
})

PAIRS = ["BTC/USDT", "ETH/USDT", "SOL/USDT"]

EMA_FAST       = 9
EMA_SLOW       = 21
RSI_PERIOD     = 14
RSI_BUY_MIN    = 45
RSI_BUY_MAX    = 68
RSI_SELL_MIN   = 52
RISK_PERCENT   = 6.0       # 6% риск (Средняя агрессивность Chris Bets)
TRAILING_PCT   = 0.015     # 1.5% трейлинг-стоп
CHECK_INTERVAL = 300       # 5 минут
PAPER_TRADE    = True      

# ── State ────────────────────────────────────────────────
paper_states = {symbol: {
    "balance": 3333.3, 
    "pos": None, 
    "total_pnl": 0.0, 
    "wins": 0, 
    "losses": 0
} for symbol in PAIRS}

# ── Telegram ─────────────────────────────────────────────
def send_telegram(message: str):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID: return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "HTML"}, timeout=10)
    except Exception as e: logger.error(f"Telegram error: {e}")

# ── Market Data (Bybit) ──────────────────────────────────
def get_data(symbol):
    try:
        bars = EXCHANGE.fetch_ohlcv(symbol, timeframe='5m', limit=100)
        df = pd.DataFrame(bars, columns=['ts', 'open', 'high', 'low', 'close', 'vol'])
        df['ema_f'] = df['close'].ewm(span=EMA_FAST, adjust=False).mean()
        df['ema_s'] = df['close'].ewm(span=EMA_SLOW, adjust=False).mean()
        
        delta = df['close'].diff()
        gain = delta.clip(lower=0).rolling(window=RSI_PERIOD).mean()
        loss = (-delta.clip(upper=0)).rolling(window=RSI_PERIOD).mean()
        df['rsi'] = 100 - (100 / (1 + (gain / loss)))
        return df
    except Exception as e:
        logger.error(f"Bybit data error {symbol}: {e}")
        return None

# ── Logic ────────────────────────────────────────────────
def run_bot():
    logger.info("Chris Bets PRO Bot Started")
    send_telegram(f"🚀 <b>Chris Bets PRO Started</b>\nRisk: {RISK_PERCENT}% | Trailing: {TRAILING_PCT*100}%")

    while True:
        for symbol in PAIRS:
            df = get_data(symbol)
            if df is None: continue
            
            curr_p = df['close'].iloc[-1]
            rsi = df['rsi'].iloc[-1]
            state = paper_states[symbol]
            
            # 1. Проверка Трейлинг-стопа
            if state["pos"]:
                pos = state["pos"]
                # Обновляем пик цены для трейлинга
                if curr_p > pos["peak"]:
                    pos["peak"] = curr_p
                    pos["stop"] = pos["peak"] * (1 - TRAILING_PCT)
                
                # Выход по стопу или RSI
                if curr_p <= pos["stop"] or (rsi >= 75):
                    pnl = (curr_p - pos["entry"]) * pos["qty"]
                    state["balance"] += (pos["invested"] + pnl)
                    state["total_pnl"] += pnl
                    icon = "✅" if pnl > 0 else "❌"
                    if pnl > 0: state["wins"] += 1 
                    else: state["losses"] += 1
                    
                    send_telegram(f"{icon} <b>SELL {symbol}</b>\nPnL: ${pnl:.2f}\nBalance: ${state['balance']:.2f}")
                    state["pos"] = None
                continue

            # 2. Вход в позицию (EMA Crossover + RSI)
            if df['ema_f'].iloc[-1] > df['ema_s'].iloc[-1] and df['ema_f'].iloc[-2] <= df['ema_s'].iloc[-2]:
                if RSI_BUY_MIN <= rsi <= RSI_BUY_MAX:
                    invest = state["balance"] * (RISK_PERCENT / 100)
                    state["balance"] -= invest
                    state["pos"] = {
                        "entry": curr_p, "qty": invest/curr_p, "peak": curr_p, 
                        "stop": curr_p * (1 - TRAILING_PCT), "invested": invest
                    }
                    send_telegram(f"🟢 <b>BUY {symbol}</b>\nPrice: ${curr_p:.2f}\nRisk: {RISK_PERCENT}%")

        time.sleep(CHECK_INTERVAL)

if __name__ == "__main__":
    run_bot()
