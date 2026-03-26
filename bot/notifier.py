import requests
from config import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID

def send(msg):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    requests.post(url, json={
        "chat_id": TELEGRAM_CHAT_ID,
        "text": msg
    }, timeout=10)
