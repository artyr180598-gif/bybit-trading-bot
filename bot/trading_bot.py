import os
import time
import logging
import requests
import pandas as pd
import ccxt
from datetime import datetime, timezone

# ── Настройка логирования ────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler()]
)
logger = logging.getLogger(__name__)

# ── Конфигурация из Railway ──────────────────────────────
# Проверь, чтобы в Railway переменные назывались именно так
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

# ── Настройка Bybit (Исправляем ошибку 403) ──────────────
EXCHANGE = ccxt.bybit({
    'enableRateLimit': True,
    'options': {'brokerId': 'CCXT'},
    'headers': {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
    }
})

PAIRS = ["BTC/USDT", "ETH/USDT", "SOL/USDT"]
EMA_FAST = 9
EMA_SLOW = 21
RSI_PERIOD = 14
RISK_PERCENT = 6.0       # Агрессивность Chris Bets
TRAILING_PCT = 0.015     # Трейлинг-стоп 1.5%
CHECK_INTERVAL = 300     # 5 минут

# Состояние портфеля (Paper Trading)
paper_states = {symbol: {
    "balance": 3333.3, 
    "pos": None, 
    "total_pnl": 0.0, 
    "wins": 0, 
    "losses": 0
} for symbol in PAIRS}

def send_telegram(message):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        logger.warning("Telegram: Переменные окружения не настроены!")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "HTML"}, timeout=10)
    except Exception as e:
        logger.error(f"Ошибка отправки в Telegram: {e}")

def get_data(symbol):
    try:
        # Получаем свечи напрямую с Bybit
        bars = EXCHANGE.fetch_ohlcv(symbol, timeframe='5m', limit=100)
        df = pd.DataFrame(bars, columns=['ts', 'open', 'high', 'low', 'close', 'vol'])
        
        # Индикаторы
        df['ema_f'] = df['close'].ewm(span=EMA_FAST, adjust=False).mean()
        df['ema_s'] = df['close'].ewm(span=EMA_SLOW, adjust=False).mean()
        
        delta = df['close'].diff()
        gain = delta.clip(lower=0).rolling(window=RSI_PERIOD).mean()
        loss = (-delta.clip(upper=0)).rolling(window=RSI_PERIOD).mean()
        df['rsi'] = 100 - (100 / (1 + (gain / loss.replace(0, 1e-9))))
        return df
    except Exception as e:
        logger.error(f"Ошибка получения данных {symbol}: {e}")
        return None

def run_bot():
    logger.info("--- ЗАПУСК CHRIS BETS PRO ---")
    send_telegram("🚀 <b>Chris Bets PRO: Бот запущен</b>\nРежим: Симуляция\nРиск: 6% | Трейлинг: 1.5%")

    while True:
        for symbol in PAIRS:
            df = get_data(symbol)
            if df is None or df.empty: continue
            
            curr_p = df['close'].iloc[-1]
            rsi = df['rsi'].iloc[-1]
            state = paper_states[symbol]
            
            # 1. Логика выхода (Трейлинг-стоп или RSI перекупленность)
            if state["pos"]:
                pos = state["pos"]
                if curr_p > pos["peak"]:
                    pos["peak"] = curr_p
                    pos["stop"] = pos["peak"] * (1 - TRAILING_PCT)
                
                if curr_p <= pos["stop"] or rsi >= 75:
                    pnl = (curr_p - pos["entry"]) * pos["qty"]
                    state["balance"] += (pos["invested"] + pnl)
                    state["total_pnl"] += pnl
                    icon = "✅" if pnl > 0 else "❌"
                    if pnl > 0: state["wins"] += 1 
                    else: state["losses"] += 1
                    
                    send_telegram(f"{icon} <b>SELL {symbol}</b>\nЦена: ${curr_p:,.2f}\nПрофит: ${pnl:+.2f}\nБаланс: ${state['balance']:,.2f}")
                    state["pos"] = None
                continue

            # 2. Логика входа (Пересечение EMA + RSI фильтр)
            ema_cross_up = df['ema_f'].iloc[-1] > df['ema_s'].iloc[-1] and df['ema_f'].iloc[-2] <= df['ema_s'].iloc[-2]
            if ema_cross_up and (45 <= rsi <= 68):
                invest = state["balance"] * (RISK_PERCENT / 100)
                state["balance"] -= invest
                state["pos"] = {
                    "entry": curr_p, "qty": invest/curr_p, "peak": curr_p, 
                    "stop": curr_p * (1 - TRAILING_PCT), "invested": invest
                }
                send_telegram(f"🟢 <b>BUY {symbol}</b>\nЦена: ${curr_p:,.2f}\nОбъем: {state['pos']['qty']:.4f}\nСтоп: ${state['pos']['stop']:,.2f}")

        time.sleep(CHECK_INTERVAL)

if __name__ == "__main__":
    run_bot()
