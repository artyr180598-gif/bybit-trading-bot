import time
from bot import exchange, indicators, strategy, execution, notifier, state
from config import *

states = {p["symbol"]: state.create(START_BALANCE) for p in PAIRS}

def higher_tf_trend(closes):
    import pandas as pd
    ema50 = pd.Series(closes).ewm(span=50).mean()
    ema200 = pd.Series(closes).ewm(span=200).mean()
    return ema50.iloc[-1] > ema200.iloc[-1]

while True:
    for pair in PAIRS:
        try:
            closes30, highs30, lows30 = exchange.get_klines(pair["symbol"], TIMEFRAME)
            closes1h, _, _ = exchange.get_klines(pair["symbol"], HIGHER_TF)

            if not higher_tf_trend(closes1h):
                continue

            df = indicators.compute(closes30, highs30, lows30)

            signal = strategy.get_signal(df)
            price = df.iloc[-1]["close"]
            atr = df.iloc[-1]["atr"]

            if atr < price * 0.003:
                continue

            s = states[pair["symbol"]]

            if s["losses"] >= MAX_LOSSES or s["trades_today"] >= MAX_TRADES_PER_DAY:
                continue

            # BUY
            if signal == "BUY" and not s["position"]:
                qty = execution.buy(s, price, atr, RISK_PERCENT)
                notifier.send(f"🟢 {pair['name']} BUY {price} qty={qty}")

            # SELL
            elif signal == "SELL" and s["position"]:
                pnl = execution.sell(s, price)
                notifier.send(f"🔴 {pair['name']} SELL {price} PnL={round(pnl,2)}")

            # Trailing stop
            if s["position"]:
                execution.update_trailing(s, price, atr)

                if price <= s["position"]["sl"]:
                    pnl = execution.sell(s, price)
                    notifier.send(f"⚠️ SL HIT {pair['name']} PnL={round(pnl,2)}")

        except Exception as e:
            notifier.send(f"❌ ERROR {pair['name']}: {e}")

        time.sleep(2)

    time.sleep(CHECK_INTERVAL)
