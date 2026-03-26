def get_signal(df):
    c = df.iloc[-1]
    p = df.iloc[-2]

    uptrend = c["ema50"] > c["ema200"]

    wr_exit = p["wr"] <= -80 and c["wr"] > -80
    macd_up = p["hist"] < 0 and c["hist"] >= 0
    macd_rising = c["hist"] > p["hist"]

    if uptrend and wr_exit and (macd_up or macd_rising):
        return "BUY"

    wr_over = c["wr"] > -20
    macd_down = p["hist"] >= 0 and c["hist"] < 0

    if (wr_over and macd_down) or not uptrend:
        return "SELL"

    return None
