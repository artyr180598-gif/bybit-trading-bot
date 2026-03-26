import os
from dotenv import load_dotenv

load_dotenv()

PAIRS = [
    {"symbol": "BTCUSDT", "name": "BTC"},
    {"symbol": "ETHUSDT", "name": "ETH"},
    {"symbol": "SOLUSDT", "name": "SOL"},
]

TIMEFRAME = "30"
HIGHER_TF = "60"

RISK_PERCENT = 1.0
ATR_MULTIPLIER = 1.5

CHECK_INTERVAL = 1800

MAX_TRADES_PER_DAY = 3
MAX_LOSSES = 3

START_BALANCE = 3333

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
