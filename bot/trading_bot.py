import os
import time
import json
import logging
from datetime import datetime
import pandas as pd
import requests
import ccxt

# ==========================================
# ⚙️ НАСТРОЙКИ СЕРВЕРА И ЛОГИРОВАНИЯ
# ==========================================

# Настройка логов точно как в Railway
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S,%f'[:-3]
)
logger = logging.getLogger(__name__)

# Токены подтягиваются автоматически из Railway (Environment Variables)
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

# ==========================================
# 📈 ПАРАМЕТРЫ СТРАТЕГИИ (CHRIS BETS PRO)
# ==========================================

EXCHANGE = ccxt.bybit({'enableRateLimit': True})
PAIRS = ['BTC/USDT', 'ETH/USDT', 'SOL/USDT']
TIMEFRAME = '5m'
CHECK_INTERVAL = 300  # 5 минут

RISK_PER_TRADE = 0.06       # Средняя агрессивность: 6% от баланса
TRAILING_STOP_PCT = 0.015   # Трейлинг-стоп: 1.5%
EMA_FAST = 9
EMA_SLOW = 21

STATS_FILE = 'strategy_stats.json'
paper_balance = 10000.0
active_trades = {}

# ==========================================
# 🧠 ЛОГИКА И ИНДИКАТОРЫ
# ==========================================

def send_telegram_msg(msg):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        logger.warning("Telegram токен или Chat ID не найдены в переменных окружения!")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        requests.post(url, json={'chat_id': TELEGRAM_CHAT_ID, 'text': msg})
    except Exception as e:
        logger.error(f"Ошибка отправки Telegram: {e}")

def load_stats():
    if os.path.exists(STATS_FILE):
        with open(STATS_FILE, 'r') as f:
            return json.load(f)
    return {
        "rsi_buy_min": 45,
        "rsi_buy_max": 68,
        "rsi_sell_min": 52,
        "trades_count": 0,
        "adjustments": 0
    }

def save_stats(stats):
    with open(STATS_FILE, 'w') as f:
        json.dump(stats, f, indent=4)

def calculate_indicators(df):
    df['EMA_9'] = df['close'].ewm(span=EMA_FAST, adjust=False).mean()
    df['EMA_21'] = df['close'].ewm(span=EMA_SLOW, adjust=False).mean()
    
    delta = df['close'].diff()
    gain = (delta.where(delta > 0, 0)).rolling(window=14).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=14).mean()
    rs = gain / loss
    df['RSI'] = 100 - (100 / (1 + rs))
    return df

def get_market_data(symbol):
    try:
        bars = EXCHANGE.fetch_ohlcv(symbol, timeframe=TIMEFRAME, limit=50)
        df = pd.DataFrame(bars, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        return calculate_indicators(df)
    except Exception as e:
        logger.error(f"Ошибка получения данных Bybit {symbol}: {e}")
        return None

# ==========================================
# 🚀 ГЛАВНЫЙ ЦИКЛ БОТА
# ==========================================

def run_bot():
    global paper_balance
    stats = load_stats()
    
    logger.info("Starting Multi-Pair Trading Bot (PRO)")
    logger.info(f"Pairs: {PAIRS}")
    logger.info("Strategy: EMA 9/21 + RSI(14) + Trailing Stop")
    logger.info("Mode: PAPER TRADING")
    
    start_msg = (
        "🤖 Multi-Pair Trading Bot Started\n"
        "Mode: PAPER TRADING\n"
        f"Pairs: {' | '.join([p.replace('/USDT', '') for p in PAIRS])}\n"
        "Strategy: EMA 9/21 + RSI(14) + Trailing Stop\n"
        f"Risk per trade: {RISK_PER_TRADE*100}% | Trailing stop: {TRAILING_STOP_PCT*100}%\n"
        "🧠 Self-learning ON\n"
        f"💼 Balance: ${paper_balance:,.2f} USDT"
    )
    send_telegram_msg(start_msg)

    while True:
        for symbol in PAIRS:
            df = get_market_data(symbol)
            if df is None or df.empty:
                continue
                
            current_price = df['close'].iloc[-1]
            ema9 = df['EMA_9'].iloc[-1]
            ema21 = df['EMA_21'].iloc[-1]
            rsi = df['RSI'].iloc[-1]
            
            clean_symbol = symbol.replace('/USDT', '')
            logger.info(f"{clean_symbol} ${current_price:.2f} | EMA9={ema9:.2f} | EMA21={ema21:.2f} | RSI={rsi:.1f}")

            # Логика ВЫХОДА (Трейлинг-стоп)
            if symbol in active_trades:
                trade = active_trades[symbol]
                
                if current_price > trade['peak_price']:
                    trade['peak_price'] = current_price
                    trade['trailing_stop_price'] = trade['peak_price'] * (1 - TRAILING_STOP_PCT)
                
                hit_stop = current_price <= trade['trailing_stop_price']
                rsi_exit = rsi >= stats['rsi_sell_min'] and ema9 < ema21
                
                if hit_stop or rsi_exit:
                    profit_usdt = (current_price - trade['entry_price']) * trade['qty']
                    paper_balance += (trade['invested'] + profit_usdt)
                    reason = "TRAILING STOP" if hit_stop else "RSI/EMA EXIT"
                    
                    msg = (
                        f"🚨 TRADE CLOSED ({reason})\n"
                        f"Pair: {clean_symbol}\n"
                        f"Entry: ${trade['entry_price']:.2f} | Exit: ${current_price:.2f}\n"
                        f"PnL: ${profit_usdt:.2f}\n"
                        f"💼 New Balance: ${paper_balance:,.2f}"
                    )
                    send_telegram_msg(msg)
                    logger.info(f"Closed {clean_symbol} position. PnL: ${profit_usdt:.2f}")
                    del active_trades[symbol]
                    
                    stats['trades_count'] += 1
                    save_stats(stats)
                continue 

            # Логика ВХОДА (Покупка)
            if ema9 > ema21 and stats['rsi_buy_min'] <= rsi <= stats['rsi_buy_max']:
                invest_amount = paper_balance * RISK_PER_TRADE
                qty = invest_amount / current_price
                paper_balance -= invest_amount
                
                active_trades[symbol] = {
                    'entry_price': current_price,
                    'qty': qty,
                    'invested': invest_amount,
                    'peak_price': current_price,
                    'trailing_stop_price': current_price * (1 - TRAILING_STOP_PCT)
                }
                
                msg = (
                    f"🟢 BUY Signal Executed\n"
                    f"Symbol: {clean_symbol}\n"
                    f"Price: ${current_price:.2f}\n"
                    f"Risk: {RISK_PER_TRADE*100}%\n"
                    f"💼 Remaining Balance: ${paper_balance:,.2f}"
                )
                send_telegram_msg(msg)
                logger.info(f"Opened {clean_symbol} position at ${current_price:.2f}")
                
        time.sleep(CHECK_INTERVAL)

if __name__ == "__main__":
    run_bot()
