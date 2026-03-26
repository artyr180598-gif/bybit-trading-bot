import requests

BASE_URL = "https://api-testnet.bybit.com"

def get_klines(symbol, interval="30", limit=200):
    url = f"{BASE_URL}/v5/market/kline"
    params = {
        "category": "linear",
        "symbol": symbol,
        "interval": interval,
        "limit": limit
    }

    r = requests.get(url, params=params, timeout=10)
    data = r.json()["result"]["list"]
    data.reverse()

    closes = [float(x[4]) for x in data]
    highs = [float(x[2]) for x in data]
    lows = [float(x[3]) for x in data]

    return closes, highs, lows
