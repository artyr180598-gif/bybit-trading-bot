from bot.risk import calc_position

def buy(state, price, atr, risk_percent):
    qty = calc_position(state["balance"], atr, risk_percent)

    state["position"] = {
        "entry": price,
        "qty": qty,
        "sl": price - atr * 1.5,
        "tp_done": False
    }

    state["balance"] -= qty * price
    return qty


def sell(state, price):
    pos = state["position"]

    pnl = (price - pos["entry"]) * pos["qty"]

    state["balance"] += pos["qty"] * price
    state["position"] = None

    if pnl >= 0:
        state["wins"] += 1
    else:
        state["losses"] += 1

    return pnl


def update_trailing(state, price, atr):
    pos = state["position"]
    if not pos:
        return

    new_sl = price - atr * 1.5
    if new_sl > pos["sl"]:
        pos["sl"] = new_sl
