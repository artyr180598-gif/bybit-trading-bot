# CryptoBot Pro — Bybit Trading Bot

Multi-user Telegram trading bot with demo & real accounts, deposit/withdraw flow, admin panel, and automatic profit distribution. Trades BTC, ETH, SOL in paper mode.

## Features

- **Demo account** — $1,000 virtual per user, same strategy as real
- **Real account** — deposit USDT TRC20, bot trades and distributes profits
- **Profit distribution** — each trade's P&L split proportionally across all active investors
- **Deposit flow** — user selects amount → gets wallet address → sends TXID → admin confirms
- **Withdrawal flow** — user requests → admin approves and pays
- **Admin panel** — `/admin` command: view users, pending deposits/withdrawals, bot stats
- **Inline buttons** — fully menu-driven, no commands needed for users

## Strategy

- Adaptive multi-profile strategy selection at startup (backtest over last 365 days)
- Profiles: `balanced`, `momentum`, `mean_reversion`, `defensive`
- Indicators core: EMA trend structure + Supertrend + RSI zone filter + MACD histogram direction
- Dynamic ATR-based SL/TP/Trailing and position sizing by risk
- Daily trend regime filter and strong-trend validation (ADX)

## Setup

1. Clone the repo
2. Install dependencies:
   ```
   pip install -r requirements.txt
   ```
3. Set environment variables:

   | Variable | Description |
   |---|---|
   | `TELEGRAM_BOT_TOKEN` | Your bot token from @BotFather |
   | `TELEGRAM_CHAT_ID` | Your Telegram chat ID (admin) |
   | `BYBIT_API_KEY` | Bybit API key |
   | `BYBIT_API_SECRET` | Bybit API secret |
   | `USDT_WALLET` | Your TRC20 USDT wallet for deposits |
   | `ADMIN_IDS` | Comma-separated admin chat IDs (defaults to `TELEGRAM_CHAT_ID`) |

4. Run:
   ```
   python bot/bot.py
   ```

## Telegram User Commands

- `/start` — open main menu
- `/admin` — admin panel (admins only)

## Admin Actions

- Confirm deposits
- Pay withdrawal requests
- View all users and balances
- View bot trading stats

## Data

All state saved to `data/` folder:
- `users.json` — all user accounts
- `bot_trades.jsonl` — trade log
- `{SYM}_state.json` — per-pair bot state

## Note

Runs in **paper trading** mode by default. Real money is never traded — profit/loss is simulated and distributed to user accounts for demonstration purposes.
