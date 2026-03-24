import os
import time
import json
import logging
import pandas as pd
import requests
import ccxt

# ==========================================
# ⚙️ ЛОГИРОВАНИЕ (Railway Style)
# ==========================================
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger(__name__)

# Данные подтягиваются из Railway Variables
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

# ==========================================
# 📈 ПАРАМЕТРЫ ПРОФИ (КРИС 6% РИСК)
# ==========================================
# Используем прокси-урл для обхода блокировки 403 на Railway
EXCHANGE = ccxt.bybit({
    'enableRateLimit': True,
    'options': {'brokerId': 'CCXT'},
    'hostname': 'api.bybit.com', # Прямой хост
})

# Если Bybit все еще блокирует сервер, CCXT сам попробует альтернативные эндпоинты
PAIRS = ['BTC/USDT', 'ETH/USDT', 'SOL/USDT']
TIMEFRAME = '5m'
RISK_PER_TRADE = 0.06       # Риск 6% (Агрессивный профи)
TRAILING_STOP_PCT = 0.015   # Трейлинг 1.5%

STATS_FILE = 'strategy_stats.json'
paper_balance = 10000.0
active_trades = {}

def send_telegram_msg(msg):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        logger.warning("Telegram Variables NOT FOUND in Railway!")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        requests.post(url, json={'chat_id': TELEGRAM_CHAT_ID, 'text': msg}, timeout=10)
    except Exception as e:
        logger.error(f"TG Error: {e}")

def get_market_data(symbol):
    try:
        # Fetch OHLCV с обработкой ошибок сети
        bars = EXCHANGE.fetch_ohlcv(symbol, timeframe=TIMEFRAME, limit=50)
        df = pd.DataFrame(bars, columns=['ts', 'open', 'high', 'low', 'close', 'vol'])
        
        # Индикаторы
        df['ema9'] = df['close'].ewm(span=9, adjust=False).mean()
        df['ema21'] = df['close'].ewm(span=21, adjust=False).mean()
        
        delta = df['close'].diff()
        gain = (delta.where(delta > 0, 0)).rolling(window=14).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(window=14).mean()
        df['rsi'] = 100 - (100 / (1 + (gain / loss)))
        return df
    except Exception as e:
        logger.error(f"Bybit Access Error ({symbol}): {e}")
        return None

def run_bot():
    global paper_balance
    logger.info("--- CHRIS BETS PRO BOT STARTED ---")
    send_telegram_msg("🚀 Бот Крис запущен!\nРиск: 6% | Трейлинг: 1.5%\nБиржа: Bybit")

    while True:
        for symbol in PAIRS:
            df = get_market_data(symbol)
            if df is None or df.empty: continue
            
            curr = df['close'].iloc[-1]
            e9, e21, rsi = df['ema9'].iloc[-1], df['ema21'].iloc[-1], df['rsi'].iloc[-1]
            
            # Логика входа
            if symbol not in active_trades:
                if e9 > e21 and 45 <= rsi <= 68:
                    invest = paper_balance * RISK_PER_TRADE
                    active_trades[symbol] = {
                        'entry': curr, 'qty': invest/curr, 'peak': curr,
                        'stop': curr * (1 - TRAILING_STOP_PCT), 'invested': invest
                    }
                    paper_balance -= invest
                    send_telegram_msg(f"🟢 ВХОД: {symbol}\nЦена: ${curr:.2f}\nБаланс: ${paper_balance:.2f}")

            # Логика выхода (Трейлинг)
            else:
                trade = active_trades[symbol]
                if curr > trade['peak']:
                    trade['peak'] = curr
                    trade['stop'] = trade['peak'] * (1 - TRAILING_STOP_PCT)
                
                if curr <= trade['stop'] or (rsi >= 70):
                    pnl = (curr - trade['entry']) * trade['qty']
                    paper_balance += (trade['invested'] + pnl)
                    send_telegram_msg(f"🔴 ВЫХОД: {symbol}\nPnL: ${pnl:.2f}\nБаланс: ${paper_balance:.2f}")
                    del active_trades[symbol]

        time.sleep(300)

if __name__ == "__main__":
    run_bot()
