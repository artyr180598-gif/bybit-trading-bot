def calc_position(balance, atr, risk_percent):
    risk_amount = balance * (risk_percent / 100)
    stop_distance = atr * 1.5

    qty = risk_amount / stop_distance
    return round(qty, 6)
