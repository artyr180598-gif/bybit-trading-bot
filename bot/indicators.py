import pandas as pd

def compute(closes, highs, lows):
    df = pd.DataFrame({
        "close": closes,
        "high": highs,
        "low": lows
    })

    df["ema50"] = df["close"].ewm(span=50).mean()
    df["ema200"] = df["close"].ewm(span=200).mean()

    high = df["high"].rolling(14).max()
    low = df["low"].rolling(14).min()
    df["wr"] = (high - df["close"]) / (high - low) * -100

    macd_fast = df["close"].ewm(span=12).mean()
    macd_slow = df["close"].ewm(span=26).mean()
    df["macd"] = macd_fast - macd_slow
    df["signal"] = df["macd"].ewm(span=9).mean()
    df["hist"] = df["macd"] - df["signal"]

    tr = (df["high"] - df["low"])
    df["atr"] = tr.rolling(14).mean()

    return df.dropna()
