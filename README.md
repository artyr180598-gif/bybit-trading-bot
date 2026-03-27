# Bybit Trading Bot

Algorithmic crypto trading bot (paper mode) for BTC, ETH, SOL.

## Strategy
- EMA 50/200 trend filter
- Williams %R entry signal
- MACD histogram crossover confirmation
- RSI filter (40–65 buy zone)
- Dynamic ATR x1.5 stop-loss

## Timeframe
30-minute candles via Yahoo Finance

## Setup

1. Clone the repo
2. Install dependencies: `pip install -r requirements.txt`
3. Set environment variables:
   - `TELEGRAM_BOT_TOKEN`
   - `TELEGRAM_CHAT_ID`
   - `BYBIT_API_KEY`
   - `BYBIT_API_SECRET`
4. Run: `python bot/bot.py`

## Telegram Commands
- `/status` — current positions and balances
- `/history` — last 10 trades per pair
- `/summary` — daily stats
- `/help` — all commands

## Note
Runs in **paper trading** mode by default. Trade history is saved to `data/`.
