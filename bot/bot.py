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


TOKEN      = os.environ.get("TELEGRAM_BOT_TOKEN", "")
ADMIN_ID   = os.environ.get("TELEGRAM_CHAT_ID", "")
ADMIN_IDS  = os.environ.get("ADMIN_IDS", ADMIN_ID)
WALLET     = os.environ.get("USDT_WALLET", "YOUR_TRC20_WALLET_HERE")
BYBIT_KEY  = os.environ.get("BYBIT_API_KEY", "")
BYBIT_SEC  = os.environ.get("BYBIT_API_SECRET", "")


DATA_DIR    = Path("data")
DATA_DIR.mkdir(exist_ok=True)
USERS_FILE  = DATA_DIR / "users.json"
TRADES_FILE = DATA_DIR / "bot_trades.jsonl"


PAIRS = [
    {"symbol": "BTCUSDT", "yahoo": "BTC-USD", "name": "BTC"},
    {"symbol": "ETHUSDT", "yahoo": "ETH-USD", "name": "ETH"},
    {"symbol": "SOLUSDT", "yahoo": "SOL-USD", "name": "SOL"},
]


EMA_FAST       = 50
EMA_SLOW       = 200
WR_PERIOD      = 14
WR_OVERSOLD    = -80
WR_OVERBOUGHT  = -20
MACD_FAST      = 12
MACD_SLOW      = 26
MACD_SIG       = 9
ATR_PERIOD     = 14
ATR_MULT       = 1.5
RSI_PERIOD     = 14
RISK_PCT       = 2.0
TRADE_INTERVAL = 1800
CANDLES        = 250
CMD_INTERVAL   = 5


STATES         = {}
LAST_UPDATE_ID = 0
BOT_STATES     = {}


def now_str():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


# ── Users DB ──────────────────────────────────────────────────────────────────

def load_users():
    if USERS_FILE.exists():
        try:
            with open(USERS_FILE) as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def save_users(u):
    with open(USERS_FILE, "w") as f:
        json.dump(u, f, indent=2, default=str)


def get_user(cid):
    users = load_users()
    uid = str(cid)
    if uid not in users:
        users[uid] = {
            "id": uid, "name": "", "joined": now_str(),
            "demo": {
                "balance": 1000.0, "start": 1000.0,
                "profit": 0.0, "trades": 0, "wins": 0, "loss": 0,
                "history": [],
            },
            "real": {
                "balance": 0.0, "deposited": 0.0,
                "profit": 0.0, "trades": 0, "wins": 0, "loss": 0,
                "history": [], "active": False,
                "pending": 0.0, "pending_txid": "",
                "withdrawals": [],
            },
            "notify": True,
        }
        save_users(users)
    return users[uid]


def save_user(cid, u):
    users = load_users()
    users[str(cid)] = u
    save_users(users)


def is_admin(cid):
    return str(cid) in [x.strip() for x in ADMIN_IDS.split(",")]


# ── Bot trading state ─────────────────────────────────────────────────────────

def load_bot_state(sym):
    f = DATA_DIR / (sym + "_state.json")
    if f.exists():
        try:
            with open(f) as fp:
                return json.load(fp)
        except Exception:
            pass
    return {"usdt": 10000.0, "coin": 0.0, "pos": None,
            "n": 0, "wins": 0, "loss": 0, "pnl": 0.0}


def save_bot_state(sym, s):
    with open(DATA_DIR / (sym + "_state.json"), "w") as f:
        json.dump(s, f, indent=2, default=str)


def log_trade(t):
    with open(TRADES_FILE, "a") as f:
        f.write(json.dumps(t, default=str) + "\n")


def all_trades():
    trades = []
    if TRADES_FILE.exists():
        with open(TRADES_FILE) as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        trades.append(json.loads(line))
                    except Exception:
                        pass
    return trades


# ── Telegram API ──────────────────────────────────────────────────────────────

def api(method, data=None):
    url = "https://api.telegram.org/bot" + TOKEN + "/" + method
    try:
        r = requests.post(url, json=data or {}, timeout=10)
        return r.json()
    except Exception as e:
        logger.error("API " + method + ": " + str(e))
        return {}


def send(cid, text, buttons=None, edit_msg_id=None):
    if not TOKEN:
        return
    data = {
        "chat_id": str(cid),
        "text": text,
        "parse_mode": "HTML",
    }
    if buttons:
        data["reply_markup"] = json.dumps({"inline_keyboard": buttons})
    if edit_msg_id:
        data["message_id"] = edit_msg_id
        api("editMessageText", data)
    else:
        api("sendMessage", data)


def answer_cb(cb_id):
    api("answerCallbackQuery", {"callback_query_id": cb_id})


# ── Keyboards ─────────────────────────────────────────────────────────────────

def kb_main():
    return [
        [{"text": "My Account", "callback_data": "account"},
         {"text": "Live Stats", "callback_data": "stats"}],
        [{"text": "Deposit", "callback_data": "deposit"},
         {"text": "Withdraw", "callback_data": "withdraw"}],
        [{"text": "Trade History", "callback_data": "history"},
         {"text": "Help", "callback_data": "help"}],
    ]


def kb_account():
    return [
        [{"text": "Demo Account", "callback_data": "demo"},
         {"text": "Real Account", "callback_data": "real"}],
        [{"text": "Deposit", "callback_data": "deposit"},
         {"text": "Withdraw", "callback_data": "withdraw"}],
        [{"text": "Main Menu", "callback_data": "menu"}],
    ]


def kb_deposit():
    return [
        [{"text": "$50",   "callback_data": "dep_50"},
         {"text": "$100",  "callback_data": "dep_100"},
         {"text": "$200",  "callback_data": "dep_200"}],
        [{"text": "$500",  "callback_data": "dep_500"},
         {"text": "$1000", "callback_data": "dep_1000"},
         {"text": "Custom","callback_data": "dep_custom"}],
        [{"text": "Cancel", "callback_data": "menu"}],
    ]


def kb_confirm_deposit(amount):
    return [
        [{"text": "I sent the payment", "callback_data": "dep_sent_" + str(amount)}],
        [{"text": "Cancel", "callback_data": "menu"}],
    ]


def kb_withdraw():
    return [
        [{"text": "Request Withdrawal", "callback_data": "wd_request"}],
        [{"text": "Cancel", "callback_data": "menu"}],
    ]


def kb_back():
    return [[{"text": "Main Menu", "callback_data": "menu"}]]


def kb_admin():
    return [
        [{"text": "All Users",          "callback_data": "adm_users"},
         {"text": "Bot Stats",          "callback_data": "adm_stats"}],
        [{"text": "Pending Deposits",   "callback_data": "adm_deposits"},
         {"text": "Pending Withdrawals","callback_data": "adm_withdrawals"}],
        [{"text": "Main Menu", "callback_data": "menu"}],
    ]


# ── Screen builders ───────────────────────────────────────────────────────────

def screen_main(cid):
    user = get_user(cid)
    demo = user["demo"]
    real = user["real"]
    demo_pct = ((demo["balance"] - demo["start"]) / demo["start"] * 100) if demo["start"] > 0 else 0
    real_pct = (real["profit"] / real["deposited"] * 100) if real["deposited"] > 0 else 0
    text = (
        "<b>CryptoBot Pro</b>\n\n"
        "Welcome, " + user["name"] + "!\n\n"
        "<b>Demo:</b> $" + format(demo["balance"], ",.2f") +
        " (" + format(demo_pct, "+.1f") + "%)\n"
        "<b>Real:</b> $" + format(real["balance"], ",.2f") +
        " (" + format(real_pct, "+.1f") + "%)\n\n"
        "Strategy: EMA50/200 + Williams%R + MACD + ATR\n"
        "Timeframe: 30 min | Risk: 2%"
    )
    send(cid, text, kb_main())


def screen_account(cid):
    user = get_user(cid)
    demo = user["demo"]
    real = user["real"]
    demo_pct = ((demo["balance"] - demo["start"]) / demo["start"] * 100) if demo["start"] > 0 else 0
    real_pct = (real["profit"] / real["deposited"] * 100) if real["deposited"] > 0 else 0
    wr_d = round(demo["wins"] / max(demo["wins"] + demo["loss"], 1) * 100)
    wr_r = round(real["wins"] / max(real["wins"] + real["loss"], 1) * 100)
    text = (
        "<b>My Account</b>\n\n"
        "<b>Demo Account</b>\n"
        "Balance: $" + format(demo["balance"], ",.2f") + "\n"
        "Profit: $" + format(demo["balance"] - demo["start"], "+.2f") +
        " (" + format(demo_pct, "+.1f") + "%)\n"
        "Trades: " + str(demo["trades"]) +
        " | W:" + str(demo["wins"]) + " L:" + str(demo["loss"]) +
        " (" + str(wr_d) + "%)\n\n"
        "<b>Real Account</b>\n"
        "Balance: $" + format(real["balance"], ",.2f") + "\n"
        "Deposited: $" + format(real["deposited"], ",.2f") + "\n"
        "Profit: $" + format(real["profit"], "+.2f") +
        " (" + format(real_pct, "+.1f") + "%)\n"
        "Trades: " + str(real["trades"]) +
        " | W:" + str(real["wins"]) + " L:" + str(real["loss"]) +
        " (" + str(wr_r) + "%)\n"
        "Status: " + ("Active" if real["active"] else "Inactive") + "\n\n"
        "Joined: " + user["joined"]
    )
    send(cid, text, kb_account())


def screen_demo(cid):
    user = get_user(cid)
    d    = user["demo"]
    pct  = ((d["balance"] - d["start"]) / d["start"] * 100) if d["start"] > 0 else 0
    wr   = round(d["wins"] / max(d["wins"] + d["loss"], 1) * 100)
    last = d["history"][-5:]
    hist = ""
    for t in reversed(last):
        icon  = "+" if t.get("pnl", 0) >= 0 else "-"
        hist += "\n" + icon + " $" + format(abs(t.get("pnl", 0)), ".2f") + " " + t.get("pair", "") + " " + t.get("time", "")[:10]
    text = (
        "<b>Demo Account</b>\n\n"
        "Balance: $" + format(d["balance"], ",.2f") + "\n"
        "Start: $" + format(d["start"], ",.2f") + "\n"
        "Profit: $" + format(d["balance"] - d["start"], "+.2f") +
        " (" + format(pct, "+.1f") + "%)\n"
        "Trades: " + str(d["trades"]) +
        " | W:" + str(d["wins"]) + " L:" + str(d["loss"]) +
        " (" + str(wr) + "%)\n\n"
        "<b>Last 5 trades:</b>" +
        (hist if hist else "\nNo trades yet") + "\n\n"
        "Demo = $1,000 virtual | Same strategy as real"
    )
    send(cid, text, kb_back())


def screen_real(cid):
    user   = get_user(cid)
    r      = user["real"]
    pct    = (r["profit"] / r["deposited"] * 100) if r["deposited"] > 0 else 0
    wr     = round(r["wins"] / max(r["wins"] + r["loss"], 1) * 100)
    last   = r["history"][-5:]
    hist   = ""
    for t in reversed(last):
        icon  = "+" if t.get("pnl", 0) >= 0 else "-"
        hist += "\n" + icon + " $" + format(abs(t.get("pnl", 0)), ".2f") + " " + t.get("pair", "") + " " + t.get("time", "")[:10]
    status = "Active - bot is trading your funds" if r["active"] else "Inactive - deposit to activate"
    text   = (
        "<b>Real Account</b>\n\n"
        "Status: " + status + "\n"
        "Balance: $" + format(r["balance"], ",.2f") + "\n"
        "Deposited: $" + format(r["deposited"], ",.2f") + "\n"
        "Profit: $" + format(r["profit"], "+.2f") +
        " (" + format(pct, "+.1f") + "%)\n"
        "Trades: " + str(r["trades"]) +
        " | W:" + str(r["wins"]) + " L:" + str(r["loss"]) +
        " (" + str(wr) + "%)\n\n"
        "<b>Last 5 trades:</b>" +
        (hist if hist else "\nNo trades yet")
    )
    btns = [
        [{"text": "Deposit", "callback_data": "deposit"},
         {"text": "Withdraw", "callback_data": "withdraw"}],
        [{"text": "Main Menu", "callback_data": "menu"}],
    ]
    send(cid, text, btns)


def screen_deposit(cid):
    text = (
        "<b>Deposit USDT</b>\n\n"
        "Select amount:\n\n"
        "Minimum: $50 USDT\n"
        "Network: TRC20\n"
        "Your funds start working immediately after confirmation."
    )
    send(cid, text, kb_deposit())


def screen_deposit_details(cid, amount):
    text = (
        "<b>Deposit $" + str(amount) + " USDT</b>\n\n"
        "Send exactly <b>" + str(amount) + " USDT</b> to:\n\n"
        "<code>" + WALLET + "</code>\n\n"
        "Network: TRC20 (Tron)\n\n"
        "After sending - press the button below.\n"
        "Then enter your transaction ID (TXID).\n\n"
        "Funds activated within 30 minutes."
    )
    send(cid, text, kb_confirm_deposit(amount))


def screen_stats(cid):
    trades = all_trades()
    sells  = [t for t in trades if t.get("side") == "SELL"]
    total  = len(sells)
    wins   = len([t for t in sells if t.get("pnl", 0) >= 0])
    wr     = round(wins / max(total, 1) * 100)
    pnl    = sum(t.get("pnl", 0) for t in sells)
    users  = load_users()
    active = len([u for u in users.values() if u["real"]["active"]])
    dep    = sum(u["real"]["deposited"] for u in users.values())
    text   = "<b>Live Statistics</b>\n\n<b>Bot Performance:</b>\n"
    for pair in PAIRS:
        s    = BOT_STATES.get(pair["symbol"], {})
        up   = ""
        if s.get("pos"):
            price = fetch_price(pair["yahoo"])
            if price:
                fl = (price - s["pos"]["entry"]) * s.get("coin", 0)
                up = " | Float: $" + format(fl, "+.2f")
            else:
                up = " | Position open"
        wr_p = round(s.get("wins", 0) / max(s.get("wins", 0) + s.get("loss", 0), 1) * 100)
        text += (
            pair["name"] + ": PnL $" + format(s.get("pnl", 0), "+.2f") +
            " W:" + str(s.get("wins", 0)) + " L:" + str(s.get("loss", 0)) +
            " (" + str(wr_p) + "%)" + up + "\n"
        )
    text += (
        "\n<b>Overall:</b>\n"
        "Trades: " + str(total) + " | Win rate: " + str(wr) + "%\n"
        "Total PnL: $" + format(pnl, "+.2f") + "\n\n"
        "<b>Community:</b>\n"
        "Active investors: " + str(active) + "\n"
        "Total deposited: $" + format(dep, ",.2f")
    )
    send(cid, text, kb_back())


def screen_history(cid):
    user = get_user(cid)
    dh   = user["demo"]["history"][-8:]
    rh   = user["real"]["history"][-8:]
    text = "<b>Trade History</b>\n\n<b>Demo (last 8):</b>\n"
    if not dh:
        text += "No trades yet\n"
    else:
        for t in reversed(dh):
            icon  = "WIN" if t.get("pnl", 0) >= 0 else "LOSS"
            text += icon + " $" + format(t.get("pnl", 0), "+.2f") + " " + t.get("pair", "") + " " + t.get("time", "")[:10] + "\n"
    text += "\n<b>Real (last 8):</b>\n"
    if not rh:
        text += "No trades yet\n"
    else:
        for t in reversed(rh):
            icon  = "WIN" if t.get("pnl", 0) >= 0 else "LOSS"
            text += icon + " $" + format(t.get("pnl", 0), "+.2f") + " " + t.get("pair", "") + " " + t.get("time", "")[:10] + "\n"
    send(cid, text, kb_back())


def screen_withdraw(cid):
    user = get_user(cid)
    bal  = user["real"]["balance"]
    if bal <= 0:
        send(cid, "No funds to withdraw.\nDeposit first to start earning.", kb_back())
        return
    text = (
        "<b>Withdraw USDT</b>\n\n"
        "Available: $" + format(bal, ",.2f") + " USDT\n\n"
        "Enter your TRC20 wallet and amount:\n"
        "Format: <code>WALLET AMOUNT</code>\n"
        "Example: <code>TXxxxxxxxx 100</code>"
    )
    STATES[str(cid)] = {"state": "wd_waiting"}
    send(cid, text, kb_back())


def screen_help(cid):
    text = (
        "<b>How CryptoBot Pro Works</b>\n\n"
        "<b>Demo Account</b>\n"
        "$1,000 virtual money\n"
        "Test the strategy risk-free\n"
        "Same algorithm as real\n\n"
        "<b>Real Account</b>\n"
        "Deposit min $50 USDT (TRC20)\n"
        "Bot trades automatically 24/7\n"
        "Profits credited after each trade\n"
        "Withdraw anytime\n\n"
        "<b>Trading Strategy</b>\n"
        "Pairs: BTC, ETH, SOL\n"
        "Timeframe: 30 minutes\n"
        "EMA 50/200 trend filter\n"
        "Williams %R entry signal\n"
        "MACD confirmation\n"
        "Dynamic ATR stop-loss\n"
        "RSI overbought filter\n"
        "Risk per trade: 2%\n\n"
        "<b>Support</b>\n"
        "Contact admin for help"
    )
    send(cid, text, kb_back())


def screen_admin(cid):
    users  = load_users()
    active = len([u for u in users.values() if u["real"]["active"]])
    dep    = sum(u["real"]["deposited"] for u in users.values())
    pend   = len([u for u in users.values() if u["real"].get("pending", 0) > 0])
    text   = (
        "<b>Admin Panel</b>\n\n"
        "Total users: " + str(len(users)) + "\n"
        "Active investors: " + str(active) + "\n"
        "Total deposited: $" + format(dep, ",.2f") + "\n"
        "Pending deposits: " + str(pend)
    )
    send(cid, text, kb_admin())


def screen_admin_users(cid):
    users = load_users()
    text  = "<b>All Users (" + str(len(users)) + ")</b>\n\n"
    for uid, u in users.items():
        r     = u["real"]
        text += (
            u["name"] + " (ID:" + uid + ")\n"
            "    Real: $" + format(r["balance"], ",.2f") +
            " | Dep: $" + format(r["deposited"], ",.2f") +
            " | " + ("Active" if r["active"] else "Inactive") + "\n"
            "    Demo: $" + format(u["demo"]["balance"], ",.2f") + "\n\n"
        )
    send(cid, text, [[{"text": "Back", "callback_data": "admin"}]])


def screen_admin_deposits(cid):
    users = load_users()
    text  = "<b>Pending Deposits</b>\n\n"
    found = False
    btns  = []
    for uid, u in users.items():
        if u["real"].get("pending", 0) > 0:
            found = True
            amt   = u["real"]["pending"]
            txid  = u["real"].get("pending_txid", "N/A")
            text += (
                u["name"] + " (ID:" + uid + ")\n"
                "Amount: $" + format(amt, ",.2f") + "\n"
                "TXID: " + txid[:20] + "...\n\n"
            )
            btns.append([{"text": "Confirm $" + str(int(amt)) + " for " + u["name"],
                           "callback_data": "adm_confirm_" + uid + "_" + str(amt)}])
    if not found:
        text += "No pending deposits"
    btns.append([{"text": "Back", "callback_data": "admin"}])
    send(cid, text, btns)


def screen_admin_withdrawals(cid):
    users = load_users()
    text  = "<b>Pending Withdrawals</b>\n\n"
    found = False
    btns  = []
    for uid, u in users.items():
        for req in u["real"].get("withdrawals", []):
            if req.get("status") == "pending":
                found = True
                text += (
                    u["name"] + " (ID:" + uid + ")\n"
                    "Amount: $" + format(req["amount"], ",.2f") + "\n"
                    "Wallet: " + req["wallet"] + "\n\n"
                )
                btns.append([{"text": "Pay $" + str(int(req["amount"])) + " to " + u["name"],
                               "callback_data": "adm_pay_" + uid + "_" + str(req["amount"])}])
    if not found:
        text += "No pending withdrawals"
    btns.append([{"text": "Back", "callback_data": "admin"}])
    send(cid, text, btns)


# ── Profit distribution ───────────────────────────────────────────────────────

def distribute(pair_name, pnl, is_win):
    users      = load_users()
    total_real = sum(u["real"]["balance"] for u in users.values() if u["real"]["active"] and u["real"]["balance"] > 0)
    ts         = now_str()
    changed    = False
    for uid, user in users.items():
        r = user["real"]
        d = user["demo"]
        if r["active"] and r["balance"] > 0 and total_real > 0:
            share    = r["balance"] / total_real
            user_pnl = round(pnl * share, 4)
            r["balance"] += user_pnl
            r["profit"]  += user_pnl
            r["trades"]  += 1
            if is_win:
                r["wins"] += 1
            else:
                r["loss"] += 1
            r["history"].append({"pair": pair_name, "pnl": user_pnl, "time": ts, "type": "real"})
            if len(r["history"]) > 200:
                r["history"] = r["history"][-200:]
            if user.get("notify", True) and user_pnl != 0:
                icon = "WIN" if user_pnl >= 0 else "LOSS"
                send(
                    uid,
                    "<b>" + icon + " Trade: " + pair_name + "</b>\n"
                    "Your profit: $" + format(user_pnl, "+.2f") + "\n"
                    "Balance: $" + format(r["balance"], ",.2f"),
                    [[{"text": "My Account", "callback_data": "account"}]],
                )
            changed = True
        else:
            share    = d["balance"] / 1000.0 if d["balance"] > 0 else 0
            demo_pnl = round(pnl * share * 0.05, 4)
            d["balance"] += demo_pnl
            d["profit"]  += demo_pnl
            d["trades"]  += 1
            if is_win:
                d["wins"] += 1
            else:
                d["loss"] += 1
            d["history"].append({"pair": pair_name, "pnl": demo_pnl, "time": ts, "type": "demo"})
            if len(d["history"]) > 200:
                d["history"] = d["history"][-200:]
            changed = True
        users[uid] = user
    if changed:
        save_users(users)


# ── Market data & indicators ──────────────────────────────────────────────────

def fetch_candles(sym):
    try:
        url = "https://query1.finance.yahoo.com/v8/finance/chart/" + sym
        r   = requests.get(url, params={"interval": "30m", "range": "60d"},
                           headers={"User-Agent": "Mozilla/5.0"}, timeout=15)
        r.raise_for_status()
        res = r.json()["chart"]["result"][0]
        q   = res["indicators"]["quote"][0]
        df  = pd.DataFrame({
            "ts":  res.get("timestamp", []),
            "hi":  q.get("high",   []),
            "lo":  q.get("low",    []),
            "cl":  q.get("close",  []),
            "vol": q.get("volume", []),
        })
        df = df.dropna(subset=["cl", "hi", "lo"]).reset_index(drop=True)
        for c in ["cl", "hi", "lo", "vol"]:
            df[c] = df[c].astype(float)
        if len(df) < EMA_SLOW + 10:
            return None
        return df.tail(CANDLES).reset_index(drop=True)
    except Exception as e:
        logger.error("Candles " + sym + ": " + str(e))
        return None


def fetch_price(sym):
    try:
        url = "https://query1.finance.yahoo.com/v8/finance/chart/" + sym
        r   = requests.get(url, params={"interval": "1m", "range": "1d"},
                           headers={"User-Agent": "Mozilla/5.0"}, timeout=15)
        r.raise_for_status()
        closes = r.json()["chart"]["result"][0]["indicators"]["quote"][0]["close"]
        p = next((c for c in reversed(closes) if c is not None), None)
        return float(p) if p else None
    except Exception as e:
        logger.error("Price " + sym + ": " + str(e))
        return None


def calc_ind(df):
    df       = df.copy()
    df["ef"] = df["cl"].ewm(span=EMA_FAST, adjust=False).mean()
    df["es"] = df["cl"].ewm(span=EMA_SLOW, adjust=False).mean()
    hh       = df["hi"].rolling(WR_PERIOD).max()
    ll       = df["lo"].rolling(WR_PERIOD).min()
    df["wr"] = ((hh - df["cl"]) / (hh - ll).replace(0, 1)) * -100
    mf       = df["cl"].ewm(span=MACD_FAST, adjust=False).mean()
    ms       = df["cl"].ewm(span=MACD_SLOW, adjust=False).mean()
    mc       = (mf - ms).ewm(span=MACD_SIG, adjust=False).mean()
    df["mh"] = (mf - ms) - mc
    hl       = df["hi"] - df["lo"]
    hc       = (df["hi"] - df["cl"].shift()).abs()
    lc       = (df["lo"] - df["cl"].shift()).abs()
    tr       = pd.concat([hl, hc, lc], axis=1).max(axis=1)
    df["atr"] = tr.ewm(span=ATR_PERIOD, adjust=False).mean()
    delta    = df["cl"].diff()
    gain     = delta.clip(lower=0).ewm(span=RSI_PERIOD, adjust=False).mean()
    loss     = (-delta.clip(upper=0)).ewm(span=RSI_PERIOD, adjust=False).mean()
    rs       = gain / loss.replace(0, float("inf"))
    df["rsi"] = 100 - (100 / (1 + rs))
    return df


def get_signal(df):
    c   = df.iloc[-1]
    p   = df.iloc[-2]
    up  = c["ef"] > c["es"]
    buy = (up and p["wr"] <= WR_OVERSOLD and c["wr"] > WR_OVERSOLD
           and p["mh"] < 0 and c["mh"] >= 0 and 40 <= c["rsi"] <= 65)
    sell = (c["wr"] >= WR_OVERBOUGHT or (p["mh"] >= 0 and c["mh"] < 0)
            or not up or c["rsi"] >= 75)
    if buy:
        return "BUY"
    if sell:
        return "SELL"
    return None


# ── Trade execution ───────────────────────────────────────────────────────────

def do_buy(pair, price, atr):
    s    = BOT_STATES[pair["symbol"]]
    if s["pos"]:
        return None
    amt  = s["usdt"] * (RISK_PCT / 100)
    qty  = round(amt / price, 6)
    cost = qty * price
    if cost > s["usdt"]:
        return None
    sl        = round(price - ATR_MULT * atr, 6)
    s["usdt"] -= cost
    s["coin"] += qty
    s["n"]    += 1
    s["pos"]   = {"entry": price, "qty": qty, "sl": sl, "atr": atr, "time": now_str()}
    save_bot_state(pair["symbol"], s)
    t = {"side": "BUY", "pair": pair["name"], "qty": qty, "price": price,
         "sl": sl, "atr": atr, "time": s["pos"]["time"],
         "id": "P-" + pair["name"] + "-" + str(s["n"]).zfill(4)}
    log_trade(t)
    return t


def do_sell(pair, price, reason="SIGNAL"):
    s   = BOT_STATES[pair["symbol"]]
    qty = s["coin"]
    if qty <= 0.000001:
        return None
    s["usdt"]  += qty * price
    s["coin"]   = 0.0
    pnl = pnl_pct = 0.0
    if s["pos"]:
        entry   = s["pos"]["entry"]
        pnl     = (price - entry) * qty
        pnl_pct = (price - entry) / entry * 100
        s["pnl"] += pnl
        if pnl >= 0:
            s["wins"] += 1
        else:
            s["loss"] += 1
        s["pos"] = None
    s["n"] += 1
    ts = now_str()
    t  = {"side": "SELL", "pair": pair["name"], "qty": round(qty, 6),
          "price": price, "pnl": pnl, "pnl_pct": pnl_pct,
          "reason": reason, "time": ts,
          "id": "P-" + pair["name"] + "-" + str(s["n"]).zfill(4)}
    save_bot_state(pair["symbol"], s)
    log_trade(t)
    distribute(pair["name"], pnl, pnl >= 0)
    return t


def check_sl(pair, price):
    s = BOT_STATES[pair["symbol"]]
    if s["pos"] and price <= s["pos"]["sl"]:
        t = do_sell(pair, price, reason="STOP-LOSS")
        if t:
            pnl = t.get("pnl", 0)
            send(ADMIN_ID,
                 "<b>STOP-LOSS " + pair["name"] + "</b>\n"
                 "Price: $" + format(price, ",.2f") + "\n"
                 "PnL: $" + format(pnl, "+.2f"))
        return True
    return False


# ── Update processor ──────────────────────────────────────────────────────────

def get_updates():
    global LAST_UPDATE_ID
    r       = api("getUpdates", {"offset": LAST_UPDATE_ID + 1, "timeout": 1})
    updates = r.get("result", [])
    for upd in updates:
        LAST_UPDATE_ID = upd["update_id"]
        msg = upd.get("message", {})
        cb  = upd.get("callback_query", {})
        if msg:
            on_message(msg)
        if cb:
            on_callback(cb)


def on_message(msg):
    cid   = str(msg["chat"]["id"])
    text  = msg.get("text", "").strip()
    name  = msg.get("from", {}).get("first_name", "User")
    state = STATES.get(cid, {})

    if text == "/start":
        user = get_user(cid)
        user["name"] = name
        save_user(cid, user)
        screen_main(cid)
        return

    if text == "/admin" and is_admin(cid):
        screen_admin(cid)
        return

    st = state.get("state", "")

    if st == "txid_waiting":
        amt  = state.get("amount", 0)
        user = get_user(cid)
        user["real"]["pending"]      = amt
        user["real"]["pending_txid"] = text
        save_user(cid, user)
        STATES.pop(cid, None)
        send(
            cid,
            "Deposit request submitted!\n\n"
            "Amount: $" + str(amt) + " USDT\n"
            "TXID: " + text[:20] + "...\n"
            "Status: Pending (up to 30 min)",
            kb_back(),
        )
        for adm in ADMIN_IDS.split(","):
            send(
                adm.strip(),
                "<b>New Deposit Request</b>\n\n"
                "User: " + user["name"] + " (ID:" + cid + ")\n"
                "Amount: $" + str(amt) + "\n"
                "TXID: " + text,
                [[{"text": "Confirm deposit $" + str(int(amt)),
                   "callback_data": "adm_confirm_" + cid + "_" + str(float(amt))}]],
            )
        return

    if st == "wd_waiting":
        parts = text.strip().split()
        if len(parts) < 2:
            send(cid, "Format: WALLET AMOUNT\nExample: TXxxxxxxxx 100")
            return
        wallet_addr = parts[0]
        try:
            amt = float(parts[1])
        except Exception:
            send(cid, "Invalid amount.")
            return
        user = get_user(cid)
        if amt > user["real"]["balance"]:
            send(cid, "Insufficient balance: $" + format(user["real"]["balance"], ",.2f"))
            return
        user["real"]["withdrawals"].append(
            {"wallet": wallet_addr, "amount": amt, "time": now_str(), "status": "pending"}
        )
        save_user(cid, user)
        STATES.pop(cid, None)
        send(
            cid,
            "Withdrawal request submitted!\n\n"
            "Amount: $" + format(amt, ",.2f") + "\n"
            "Wallet: " + wallet_addr[:10] + "...\n"
            "Processing: up to 24 hours",
            kb_back(),
        )
        for adm in ADMIN_IDS.split(","):
            send(
                adm.strip(),
                "<b>Withdrawal Request</b>\n\n"
                "User: " + user["name"] + " (ID:" + cid + ")\n"
                "Amount: $" + format(amt, ",.2f") + "\n"
                "Wallet: " + wallet_addr,
                [[{"text": "Pay $" + str(int(amt)),
                   "callback_data": "adm_pay_" + cid + "_" + str(amt)}]],
            )
        return

    if st == "custom_dep":
        try:
            amt = float(text)
            if amt < 50:
                send(cid, "Minimum is $50")
                return
            STATES.pop(cid, None)
            screen_deposit_details(cid, amt)
        except Exception:
            send(cid, "Enter a number. Example: 150")
        return

    screen_main(cid)


def on_callback(cb):
    cid  = str(cb["message"]["chat"]["id"])
    data = cb.get("data", "")
    answer_cb(cb["id"])

    if data == "menu":
        screen_main(cid)
    elif data == "account":
        screen_account(cid)
    elif data == "demo":
        screen_demo(cid)
    elif data == "real":
        screen_real(cid)
    elif data == "deposit":
        screen_deposit(cid)
    elif data == "withdraw":
        screen_withdraw(cid)
    elif data == "stats":
        screen_stats(cid)
    elif data == "history":
        screen_history(cid)
    elif data == "help":
        screen_help(cid)
    elif data == "admin" and is_admin(cid):
        screen_admin(cid)
    elif data == "adm_users" and is_admin(cid):
        screen_admin_users(cid)
    elif data == "adm_deposits" and is_admin(cid):
        screen_admin_deposits(cid)
    elif data == "adm_withdrawals" and is_admin(cid):
        screen_admin_withdrawals(cid)
    elif data == "adm_stats" and is_admin(cid):
        screen_stats(cid)
    elif data.startswith("dep_"):
        amt_str = data.replace("dep_", "")
        if amt_str == "custom":
            STATES[cid] = {"state": "custom_dep"}
            send(cid, "Enter deposit amount in USDT (min $50):")
        else:
            screen_deposit_details(cid, float(amt_str))
    elif data.startswith("dep_sent_"):
        amt = float(data.replace("dep_sent_", ""))
        STATES[cid] = {"state": "txid_waiting", "amount": amt}
        send(cid, "Enter your transaction ID (TXID):")
    elif data.startswith("adm_confirm_") and is_admin(cid):
        parts = data.replace("adm_confirm_", "").split("_")
        if len(parts) == 2:
            target = parts[0]
            amt    = float(parts[1])
            user   = get_user(target)
            user["real"]["balance"]     += amt
            user["real"]["deposited"]   += amt
            user["real"]["active"]       = True
            user["real"]["pending"]      = 0.0
            user["real"]["pending_txid"] = ""
            save_user(target, user)
            send(
                target,
                "<b>Deposit Confirmed!</b>\n\n"
                "$" + format(amt, ",.2f") + " USDT added.\n"
                "Balance: $" + format(user["real"]["balance"], ",.2f") + "\n\n"
                "Your funds are now being traded!",
                [[{"text": "My Account", "callback_data": "account"}]],
            )
            send(cid, "Deposit $" + str(int(amt)) + " confirmed for user " + target)
    elif data.startswith("adm_pay_") and is_admin(cid):
        parts = data.replace("adm_pay_", "").split("_")
        if len(parts) == 2:
            target = parts[0]
            amt    = float(parts[1])
            user   = get_user(target)
            if user["real"]["balance"] >= amt:
                user["real"]["balance"] -= amt
                for req in user["real"]["withdrawals"]:
                    if req.get("status") == "pending" and req.get("amount") == amt:
                        req["status"] = "paid"
                        break
                save_user(target, user)
                send(
                    target,
                    "<b>Withdrawal Paid!</b>\n\n"
                    "$" + format(amt, ",.2f") + " USDT sent.\n"
                    "Remaining: $" + format(user["real"]["balance"], ",.2f"),
                    kb_back(),
                )
                send(cid, "Paid $" + str(int(amt)) + " to user " + target)
            else:
                send(cid, "Insufficient balance for user " + target)


# ── Main ──────────────────────────────────────────────────────────────────────

def run():
    global BOT_STATES
    BOT_STATES = {p["symbol"]: load_bot_state(p["symbol"]) for p in PAIRS}
    logger.info("CryptoBot Pro starting")
    send(
        ADMIN_ID,
        "<b>CryptoBot Pro - STARTED</b>\n\n"
        "Mode: PAPER TRADING\n"
        "Pairs: BTC | ETH | SOL\n"
        "Strategy: EMA50/200 + WR + MACD + ATR + RSI\n"
        "Timeframe: 30min | Risk: 2%\n\n"
        "Features:\n"
        "- Demo $1,000 per user\n"
        "- Real accounts with deposits\n"
        "- Profit distribution per trade\n"
        "- Deposit/withdraw flow\n"
        "- Admin panel\n\n"
        "Admin: /admin",
    )

    last_trade_check = 0
    check_num        = 0

    while True:
        get_updates()
        now = time.time()
        if now - last_trade_check >= TRADE_INTERVAL:
            last_trade_check = now
            check_num       += 1
            logger.info("Trade check #" + str(check_num))
            for pair in PAIRS:
                try:
                    df = fetch_candles(pair["yahoo"])
                    if df is None:
                        continue
                    df    = calc_ind(df)
                    price = fetch_price(pair["yahoo"])
                    if price is None:
                        continue
                    c  = df.iloc[-1]
                    up = c["ef"] > c["es"]
                    logger.info(
                        pair["name"] + " $" + format(price, ",.2f") +
                        " WR=" + format(c["wr"], ".1f") +
                        " RSI=" + format(c["rsi"], ".1f") +
                        " Trend=" + ("UP" if up else "DOWN")
                    )
                    s = BOT_STATES[pair["symbol"]]
                    if s["pos"] and check_sl(pair, price):
                        continue
                    sig = get_signal(df)
                    if sig == "BUY":
                        t = do_buy(pair, price, c["atr"])
                        if t:
                            logger.info(pair["name"] + " BUY @ $" + str(price))
                            send(ADMIN_ID,
                                 "<b>BUY " + pair["name"] + "</b>\n"
                                 "Price: $" + format(price, ",.2f") + "\n"
                                 "SL: $" + format(t["sl"], ",.2f") + "\n"
                                 "Risk: 2%")
                    elif sig == "SELL" and s["pos"]:
                        t = do_sell(pair, price, reason="SIGNAL")
                        if t:
                            pnl = t.get("pnl", 0)
                            logger.info(pair["name"] + " SELL @ $" + str(price) + " PnL=" + str(round(pnl, 2)))
                            send(ADMIN_ID,
                                 "<b>SELL " + pair["name"] + "</b>\n"
                                 "Price: $" + format(price, ",.2f") + "\n"
                                 "PnL: $" + format(pnl, "+.2f"))
                except Exception as e:
                    logger.error("Trade " + pair["name"] + ": " + str(e))
                time.sleep(2)

            if check_num % 48 == 0:
                screen_stats(ADMIN_ID)

        time.sleep(CMD_INTERVAL)


if __name__ == "__main__":
    run()
