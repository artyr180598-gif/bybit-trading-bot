import os
import sys
import time
import json
import logging
import requests
import pandas as pd
from datetime import datetime, timezone
from pathlib import Path


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)


TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID   = os.environ.get("TELEGRAM_CHAT_ID", "")
BYBIT_API_KEY      = os.environ.get("BYBIT_API_KEY", "")
BYBIT_API_SECRET   = os.environ.get("BYBIT_API_SECRET", "")


PAIRS = [
    {"symbol": "BTCUSDT", "yahoo": "BTC-USD", "name": "BTC"},
    {"symbol": "ETHUSDT", "yahoo": "ETH-USD", "name": "ETH"},
    {"symbol": "SOLUSDT", "yahoo": "SOL-USD", "name": "SOL"},
]


EMA_FAST      = 50
EMA_SLOW      = 200
WR_PERIOD     = 14
WR_OVERSOLD   = -80
WR_OVERBOUGHT = -20
MACD_FAST     = 12
MACD_SLOW     = 26
MACD_SIG      = 9
ATR_PERIOD    = 14
ATR_MULT      = 1.5
RSI_PERIOD    = 14
RISK_PCT      = 2.0
INTERVAL      = 1800
CANDLES       = 250
PAPER         = True
DATA_DIR      = Path("data")
DATA_DIR.mkdir(exist_ok=True)


# ── Persistent storage ────────────────────────────────────────────────────────

def save_state(sym, state):
    try:
        f = DATA_DIR / (sym + "_state.json")
        data = dict(state)
        data["pos"] = state["pos"]
        data["log"] = state["log"]
        with open(f, "w") as fp:
            json.dump(data, fp, indent=2, default=str)
    except Exception as e:
        logger.error("save_state: " + str(e))


def load_state(sym):
    try:
        f = DATA_DIR / (sym + "_state.json")
        if f.exists():
            with open(f) as fp:
                d = json.load(fp)
            logger.info("Loaded state for " + sym + " | PnL: $" + str(d.get("pnl", 0)))
            return d
    except Exception as e:
        logger.error("load_state: " + str(e))
    return None


def save_trade(sym, trade):
    try:
        f = DATA_DIR / (sym + "_trades.jsonl")
        with open(f, "a") as fp:
            fp.write(json.dumps(trade, default=str) + "\n")
    except Exception as e:
        logger.error("save_trade: " + str(e))


def load_all_trades(sym):
    trades = []
    try:
        f = DATA_DIR / (sym + "_trades.jsonl")
        if f.exists():
            with open(f) as fp:
                for line in fp:
                    line = line.strip()
                    if line:
                        try:
                            trades.append(json.loads(line))
                        except Exception:
                            pass
    except Exception as e:
        logger.error("load_trades: " + str(e))
    return trades


def make_state(sym, balance=3333.0):
    existing = load_state(sym)
    if existing:
        return existing
    return {
        "usdt":       balance,
        "coin":       0.0,
        "pos":        None,
        "n":          0,
        "wins":       0,
        "loss":       0,
        "pnl":        0.0,
        "log":        [],
        "started_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
    }


S = {p["symbol"]: make_state(p["symbol"]) for p in PAIRS}


# ── Telegram ──────────────────────────────────────────────────────────────────

def tg(msg):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    url = "https://api.telegram.org/bot" + TELEGRAM_BOT_TOKEN + "/sendMessage"
    try:
        requests.post(
            url,
            json={"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "HTML"},
            timeout=10,
        )
    except Exception as e:
        logger.error("TG: " + str(e))


def check_telegram_commands():
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    url = "https://api.telegram.org/bot" + TELEGRAM_BOT_TOKEN + "/getUpdates"
    try:
        r = requests.get(url, params={"timeout": 1}, timeout=5)
        updates = r.json().get("result", [])
        if not updates:
            return
        last_id = None
        for upd in updates:
            last_id = upd["update_id"]
            msg = upd.get("message", {})
            text = msg.get("text", "").strip().lower()
            if text == "/status":
                tg(build_status())
            elif text == "/history":
                tg(build_history())
            elif text == "/summary":
                tg(build_summary())
            elif text == "/help":
                tg(build_help())
        if last_id:
            requests.get(
                url,
                params={"offset": last_id + 1, "timeout": 1},
                timeout=5,
            )
    except Exception as e:
        logger.error("check_commands: " + str(e))


# ── Market data ───────────────────────────────────────────────────────────────

def fetch_candles(yahoo_sym):
    try:
        url = "https://query1.finance.yahoo.com/v8/finance/chart/" + yahoo_sym
        r = requests.get(
            url,
            params={"interval": "30m", "range": "60d"},
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=15,
        )
        r.raise_for_status()
        res = r.json()["chart"]["result"][0]
        ts  = res.get("timestamp", [])
        q   = res["indicators"]["quote"][0]
        df  = pd.DataFrame({
            "ts":  ts,
            "hi":  q.get("high",   []),
            "lo":  q.get("low",    []),
            "cl":  q.get("close",  []),
            "vol": q.get("volume", []),
        })
        df = df.dropna(subset=["cl", "hi", "lo"]).reset_index(drop=True)
        for c in ["cl", "hi", "lo", "vol"]:
            df[c] = df[c].astype(float)
        if len(df) < EMA_SLOW + 10:
            logger.info("Not enough candles: " + str(len(df)))
            return None
        return df.tail(CANDLES).reset_index(drop=True)
    except Exception as e:
        logger.error("Candles " + yahoo_sym + ": " + str(e))
        return None


def fetch_price(yahoo_sym):
    try:
        url = "https://query1.finance.yahoo.com/v8/finance/chart/" + yahoo_sym
        r = requests.get(
            url,
            params={"interval": "1m", "range": "1d"},
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=15,
        )
        r.raise_for_status()
        closes = r.json()["chart"]["result"][0]["indicators"]["quote"][0]["close"]
        p = next((c for c in reversed(closes) if c is not None), None)
        return float(p) if p else None
    except Exception as e:
        logger.error("Price: " + str(e))
        return None


# ── Indicators ────────────────────────────────────────────────────────────────

def calc_indicators(df):
    df = df.copy()
    df["ef"] = df["cl"].ewm(span=EMA_FAST, adjust=False).mean()
    df["es"] = df["cl"].ewm(span=EMA_SLOW, adjust=False).mean()
    hh = df["hi"].rolling(WR_PERIOD).max()
    ll = df["lo"].rolling(WR_PERIOD).min()
    df["wr"] = ((hh - df["cl"]) / (hh - ll).replace(0, 1)) * -100
    mf = df["cl"].ewm(span=MACD_FAST, adjust=False).mean()
    ms = df["cl"].ewm(span=MACD_SLOW, adjust=False).mean()
    mc = (mf - ms).ewm(span=MACD_SIG, adjust=False).mean()
    df["mh"] = (mf - ms) - mc
    hl = df["hi"] - df["lo"]
    hc = (df["hi"] - df["cl"].shift()).abs()
    lc = (df["lo"] - df["cl"].shift()).abs()
    tr = pd.concat([hl, hc, lc], axis=1).max(axis=1)
    df["atr"] = tr.ewm(span=ATR_PERIOD, adjust=False).mean()
    delta = df["cl"].diff()
    gain  = delta.clip(lower=0).ewm(span=RSI_PERIOD, adjust=False).mean()
    loss  = (-delta.clip(upper=0)).ewm(span=RSI_PERIOD, adjust=False).mean()
    rs    = gain / loss.replace(0, float("inf"))
    df["rsi"] = 100 - (100 / (1 + rs))
    return df


def get_signal(df):
    c = df.iloc[-1]
    p = df.iloc[-2]
    uptrend = c["ef"] > c["es"]
    buy = (
        uptrend
        and p["wr"] <= WR_OVERSOLD
        and c["wr"] > WR_OVERSOLD
        and p["mh"] < 0
        and c["mh"] >= 0
        and 40 <= c["rsi"] <= 65
    )
    sell = (
        c["wr"] >= WR_OVERBOUGHT
        or (p["mh"] >= 0 and c["mh"] < 0)
        or not uptrend
        or c["rsi"] >= 75
    )
    if buy:
        return "BUY"
    if sell:
        return "SELL"
    return None


# ── Trade execution ───────────────────────────────────────────────────────────

def do_buy(pair, price, atr):
    s = S[pair["symbol"]]
    if s["pos"]:
        return None
    amt  = s["usdt"] * (RISK_PCT / 100)
    qty  = round(amt / price, 6)
    cost = qty * price
    if cost > s["usdt"]:
        return None
    sl = round(price - ATR_MULT * atr, 6)
    s["usdt"] -= cost
    s["coin"] += qty
    s["n"]    += 1
    s["pos"]   = {
        "entry": price,
        "qty":   qty,
        "sl":    sl,
        "atr":   atr,
        "time":  datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
    }
    trade = {
        "side":  "BUY",
        "qty":   qty,
        "price": price,
        "sl":    sl,
        "atr":   atr,
        "time":  s["pos"]["time"],
        "id":    "P-" + pair["name"] + "-" + str(s["n"]).zfill(4),
    }
    save_trade(pair["symbol"], trade)
    save_state(pair["symbol"], s)
    return trade


def do_sell(pair, price, reason="SIGNAL"):
    s   = S[pair["symbol"]]
    qty = s["coin"]
    if qty <= 0.000001:
        return None
    s["usdt"] += qty * price
    s["coin"]  = 0.0
    pnl = pnl_pct = 0.0
    entry = entry_time = None
    if s["pos"]:
        entry      = s["pos"]["entry"]
        entry_time = s["pos"].get("time", "")
        pnl        = (price - entry) * qty
        pnl_pct    = (price - entry) / entry * 100
        s["pnl"]  += pnl
        if pnl >= 0:
            s["wins"] += 1
        else:
            s["loss"] += 1
        s["pos"] = None
    s["n"] += 1
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    log_entry = {
        "bought_at": entry_time,
        "sold_at":   ts,
        "entry":     entry,
        "exit":      price,
        "qty":       round(qty, 6),
        "pnl":       round(pnl, 4),
        "pnl_pct":   round(pnl_pct, 4),
        "reason":    reason,
        "result":    "WIN" if pnl >= 0 else "LOSS",
    }
    s["log"].append(log_entry)
    if len(s["log"]) > 50:
        s["log"] = s["log"][-50:]
    trade = {
        "side":    "SELL",
        "qty":     round(qty, 6),
        "price":   price,
        "pnl":     pnl,
        "pnl_pct": pnl_pct,
        "reason":  reason,
        "time":    ts,
        "id":      "P-" + pair["name"] + "-" + str(s["n"]).zfill(4),
    }
    save_trade(pair["symbol"], trade)
    save_state(pair["symbol"], s)
    return trade


def check_sl(pair, price):
    s = S[pair["symbol"]]
    if s["pos"] and price <= s["pos"]["sl"]:
        logger.warning(pair["name"] + " SL hit at $" + str(price))
        t = do_sell(pair, price, reason="STOP-LOSS")
        if t:
            tg(build_trade_msg(pair, t))
        return True
    return False


# ── Message builders ──────────────────────────────────────────────────────────

def build_trade_msg(pair, trade):
    s      = S[pair["symbol"]]
    side   = trade["side"]
    reason = trade.get("reason", "SIGNAL")
    ts     = trade.get("time", "")
    if reason == "STOP-LOSS":
        header = "[!] STOP-LOSS | " + pair["name"]
    elif side == "BUY":
        header = "[BUY] " + pair["name"]
    else:
        header = "[SELL] " + pair["name"]
    lines = [
        "<b>" + header + "</b>",
        "Time: " + ts,
        "Price: $" + format(trade["price"], ",.4f"),
        "Qty: " + str(trade["qty"]) + " " + pair["name"],
    ]
    if side == "BUY":
        lines += [
            "Stop-Loss: $" + format(trade["sl"], ",.4f"),
            "ATR: $" + format(trade["atr"], ".2f"),
            "Risk: " + str(RISK_PCT) + "%",
        ]
    if side == "SELL" or reason == "STOP-LOSS":
        pnl     = trade.get("pnl", 0)
        pnl_pct = trade.get("pnl_pct", 0)
        result  = "WIN" if pnl >= 0 else "LOSS"
        lines  += [
            result + " PnL: $" + format(pnl, "+.2f") + " (" + format(pnl_pct, "+.2f") + "%)",
            "Reason: " + reason,
        ]
    wr = round(s["wins"] / max(s["wins"] + s["loss"], 1) * 100)
    lines += [
        "Balance: $" + format(s["usdt"], ",.2f"),
        "Total PnL: $" + format(s["pnl"], "+.2f"),
        "W:" + str(s["wins"]) + " L:" + str(s["loss"]) + " (" + str(wr) + "% winrate)",
        "ID: " + trade["id"],
    ]
    return "\n".join(lines)


def build_status():
    lines = ["<b>STATUS - All Pairs</b>", ""]
    for pair in PAIRS:
        s = S[pair["symbol"]]
        price = fetch_price(pair["yahoo"])
        price_str = "$" + format(price, ",.2f") if price else "N/A"
        wr = round(s["wins"] / max(s["wins"] + s["loss"], 1) * 100)
        lines.append("<b>" + pair["name"] + "</b>")
        lines.append("      Price now: " + price_str)
        lines.append("      Balance: $" + format(s["usdt"], ",.2f"))
        lines.append("      PnL: $" + format(s["pnl"], "+.2f"))
        lines.append("      W:" + str(s["wins"]) + " L:" + str(s["loss"]) + " (" + str(wr) + "%)")
        if s["pos"]:
            entry   = s["pos"]["entry"]
            sl      = s["pos"]["sl"]
            cur_pnl = 0.0
            if price:
                cur_pnl = (price - entry) * s["coin"]
            lines.append(
                "      OPEN @ $" + format(entry, ",.2f") +
                " | SL: $" + format(sl, ",.2f") +
                " | Now: $" + format(cur_pnl, "+.2f")
            )
        else:
            lines.append("      No open position")
        lines.append("")
    lines.append("Strategy: EMA50/200 + WR + MACD + ATR + RSI")
    lines.append("Timeframe: 30min | Risk: 2%")
    return "\n".join(lines)


def build_history():
    lines = ["<b>TRADE HISTORY - Last 10 per pair</b>", ""]
    for pair in PAIRS:
        trades      = load_all_trades(pair["symbol"])
        sell_trades = [t for t in trades if t.get("side") == "SELL"]
        last10      = sell_trades[-10:]
        lines.append("<b>" + pair["name"] + "</b> (" + str(len(sell_trades)) + " closed trades)")
        if not last10:
            lines.append("      No closed trades yet")
        else:
            for t in reversed(last10):
                pnl    = t.get("pnl", 0)
                result = "WIN" if pnl >= 0 else "LOSS"
                lines.append(
                    "   " + result +
                    " $" + format(pnl, "+.2f") +
                    " @ $" + format(t.get("price", 0), ",.2f") +
                    " | " + t.get("reason", "") +
                    " | " + t.get("time", "")[:16]
                )
        lines.append("")
    return "\n".join(lines)


def build_summary():
    lines     = ["<b>DAILY SUMMARY</b>", ""]
    tot       = 0.0
    tw = tl   = 0
    for pair in PAIRS:
        s   = S[pair["symbol"]]
        tot += s["pnl"]
        tw  += s["wins"]
        tl  += s["loss"]
        wr   = round(s["wins"] / max(s["wins"] + s["loss"], 1) * 100)
        pos  = "No position"
        if s["pos"]:
            pos = "Open @ $" + format(s["pos"]["entry"], ",.2f") + " | SL: $" + format(s["pos"]["sl"], ",.2f")
        lines += [
            "<b>" + pair["name"] + "</b>",
            "     Balance: $" + format(s["usdt"], ",.2f"),
            "     PnL: $" + format(s["pnl"], "+.2f"),
            "     W:" + str(s["wins"]) + " L:" + str(s["loss"]) + " (" + str(wr) + "%)",
            "     " + pos,
            "",
        ]
    twr       = round(tw / max(tw + tl, 1) * 100)
    start_bal = 10000.0
    lines    += [
        "Total PnL: $" + format(tot, "+.2f"),
        "Overall: W:" + str(tw) + " L:" + str(tl) + " (" + str(twr) + "% winrate)",
        "Start balance: $" + format(start_bal, ",.2f"),
    ]
    return "\n".join(lines)


def build_help():
    return (
        "<b>Bot Commands</b>\n\n"
        "/status - current positions and balances\n"
        "/history - last 10 trades per pair\n"
        "/summary - full daily summary\n"
        "/help - this message\n\n"
        "<b>Strategy:</b> EMA50/200 + Williams%R + MACD + ATR + RSI\n"
        "<b>Timeframe:</b> 30 minutes\n"
        "<b>Risk:</b> 2% per trade\n"
        "<b>Stop-loss:</b> Dynamic ATR x1.5\n"
        "<b>Pairs:</b> BTC, ETH, SOL"
    )


# ── Main loop ─────────────────────────────────────────────────────────────────

def run():
    logger.info("Bot v2 starting")
    tg(
        "<b>Advanced Bot v2 - STARTED</b>\n"
        "Mode: PAPER TRADING\n"
        "Pairs: BTC | ETH | SOL\n"
        "Strategy: EMA50/200 + Williams%R + MACD + ATR + RSI\n"
        "Timeframe: 30min | Risk: 2%\n"
        "Balance: $3333 per pair\n\n"
        "Commands:\n"
        "/status - positions\n"
        "/history - trade history\n"
        "/summary - daily stats\n"
        "/help - all commands\n\n"
        "History is saved permanently!\n"
        "Bot remembers all trades after restart."
    )

    check_count = 0
    while True:
        check_count += 1
        logger.info("--- Check #" + str(check_count) + " ---")

        check_telegram_commands()

        for pair in PAIRS:
            try:
                df = fetch_candles(pair["yahoo"])
                if df is None:
                    continue
                df    = calc_indicators(df)
                price = fetch_price(pair["yahoo"])
                if price is None:
                    continue
                c  = df.iloc[-1]
                up = c["ef"] > c["es"]
                logger.info(
                    pair["name"] + " $" + format(price, ",.2f") +
                    " EMA50=" + format(c["ef"], ".2f") +
                    " EMA200=" + format(c["es"], ".2f") +
                    " WR=" + format(c["wr"], ".1f") +
                    " MACD=" + format(c["mh"], ".4f") +
                    " RSI=" + format(c["rsi"], ".1f") +
                    " ATR=" + format(c["atr"], ".2f") +
                    " Trend=" + ("UP" if up else "DOWN")
                )
                s = S[pair["symbol"]]
                if s["pos"] and check_sl(pair, price):
                    continue
                sig = get_signal(df)
                if sig == "BUY":
                    t = do_buy(pair, price, c["atr"])
                    if t:
                        tg(build_trade_msg(pair, t))
                        logger.info(pair["name"] + " BUY @ $" + str(price))
                elif sig == "SELL" and s["pos"]:
                    t = do_sell(pair, price, reason="SIGNAL")
                    if t:
                        tg(build_trade_msg(pair, t))
                        logger.info(pair["name"] + " SELL @ $" + str(price))
            except Exception as e:
                msg = "ERROR " + pair["name"] + ": " + str(e)
                logger.error(msg)
                tg("<b>Error " + pair["name"] + "</b>\n" + str(e))
            time.sleep(2)

        if check_count % 48 == 0:
            tg(build_summary())

        logger.info("Sleeping 30 min")
        time.sleep(INTERVAL)


if __name__ == "__main__":
    run()
