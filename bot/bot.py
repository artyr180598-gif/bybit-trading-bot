"""
CryptoBot Pro v5 — Автоматическая торговля (Demo + Live)
═══════════════════════════════════════════════════════════════
СТРАТЕГИЯ: Scoring 3/4 — EMA + Supertrend + RSI + MACD (4H + 1D)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Рынок:     USDT Perpetual Futures (BTC, ETH, SOL)
Таймфрейм: 4H свечи, анализ каждые 15 минут
Плечо:     3x (настраивается через env LEVERAGE)

ВХОД LONG (3 из 4 индикаторов):
  ✅ EMA21 > EMA50          (восходящий тренд)
  ✅ Supertrend = БЫЧИЙ    (ATR-трендовый индикатор)
  ✅ RSI в диапазоне 38-72 (есть импульс, не перекуплен)
  ✅ MACD гистограмма растёт
  + 1D тренд как бонус-фильтр (не обязателен)

ВХОД SHORT (3 из 4 индикаторов):
  ✅ EMA21 < EMA50          (нисходящий тренд)
  ✅ Supertrend = МЕДВЕЖИЙ
  ✅ RSI в диапазоне 28-62
  ✅ MACD гистограмма падает
  + 1D тренд как бонус-фильтр (не обязателен)

ВЫХОД:
  🎯 Тейк-профит: ATR × 2.5 (R:R = 1:1.7)
  ⛔ Стоп-лосс:   ATR × 1.5
  📈 Трейлинг-стоп активируется при 50% пути к TP

РИСК-МЕНЕДЖМЕНТ:
  • 2% капитала на сделку
  • Максимум 3 позиции одновременно
  • Circuit-breaker: -5% за день / -15% от пика
  • Плечо 3x

РЕЖИМЫ:
  • DEMO   — симуляция с реальными ценами Bybit (без API)
  • LIVE   — реальная торговля через Bybit Testnet/Mainnet
"""

import os, sys, time, json, random, string, logging, requests
import pandas as pd
import numpy  as np
from datetime import datetime, timezone
from pathlib  import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)

# ─── КОНФИГУРАЦИЯ ─────────────────────────────────────────────────────────────
TOKEN       = os.environ.get("TELEGRAM_BOT_TOKEN", "")
ADMIN_ID    = os.environ.get("TELEGRAM_CHAT_ID", "")
ADMIN_IDS   = os.environ.get("ADMIN_IDS", ADMIN_ID)
WALLET      = os.environ.get("USDT_WALLET", "ЗАДАЙТЕ_USDT_WALLET")

# Bybit API (для реальной торговли)
BYBIT_KEY    = os.environ.get("BYBIT_API_KEY", "").strip()
BYBIT_SECRET = os.environ.get("BYBIT_API_SECRET", "").strip()
USE_TESTNET  = os.environ.get("BYBIT_TESTNET", "true").lower() == "true"
LEVERAGE     = int(os.environ.get("BYBIT_LEVERAGE", "3"))

# LIVE_MODE = True только если ключи реально заполнены и не заглушки
_keys_ok   = (bool(BYBIT_KEY and BYBIT_SECRET)
              and len(BYBIT_KEY) > 10
              and BYBIT_KEY not in ("your_api_key_here", "YOUR_KEY"))
_demo_env  = os.environ.get("DEMO_MODE", "false").lower() == "true"
LIVE_MODE  = _keys_ok and not _demo_env

DATA_DIR   = Path("data")
DATA_DIR.mkdir(exist_ok=True)
USERS_FILE    = DATA_DIR / "users.json"
TRADES_FILE   = DATA_DIR / "bot_trades.jsonl"
DEPOSITS_FILE = DATA_DIR / "pending_deposits.json"

BOT_USERNAME = ""   # заполняется при старте через getMe

# ─── ТОРГОВЫЕ ПАРЫ ────────────────────────────────────────────────────────────
PAIRS = [
    {"symbol": "BTCUSDT", "name": "BTC", "emoji": "₿",  "min_qty": 0.001},
    {"symbol": "ETHUSDT", "name": "ETH", "emoji": "Ξ",  "min_qty": 0.01},
    {"symbol": "SOLUSDT", "name": "SOL", "emoji": "◎",  "min_qty": 0.1},
]

# ─── МОНЕТЫ ДЛЯ ДЕМО-ТОРГОВЛИ ─────────────────────────────────────────────────
DEMO_COINS = [
    {"symbol": "BTCUSDT",  "name": "Bitcoin",  "short": "BTC",  "emoji": "₿",  "cg_id": "bitcoin"},
    {"symbol": "ETHUSDT",  "name": "Ethereum", "short": "ETH",  "emoji": "Ξ",  "cg_id": "ethereum"},
    {"symbol": "SOLUSDT",  "name": "Solana",   "short": "SOL",  "emoji": "◎",  "cg_id": "solana"},
    {"symbol": "BNBUSDT",  "name": "BNB",      "short": "BNB",  "emoji": "🔶", "cg_id": "binancecoin"},
    {"symbol": "XRPUSDT",  "name": "Ripple",   "short": "XRP",  "emoji": "💧", "cg_id": "ripple"},
    {"symbol": "ADAUSDT",  "name": "Cardano",  "short": "ADA",  "emoji": "🔵", "cg_id": "cardano"},
]
DEMO_LEVERAGE = 2  # фиксированное плечо для демо-счёта

# ─── ПАРАМЕТРЫ СТРАТЕГИИ ──────────────────────────────────────────────────────
EMA_MID      = 21
EMA_SLOW     = 50
RSI_PERIOD   = 14
RSI_LONG_MIN = 38
RSI_LONG_MAX = 72
RSI_SHORT_MIN= 28
RSI_SHORT_MAX= 62
ATR_PERIOD   = 14
ATR_SL_MULT  = 1.5
ATR_TP_MULT  = 2.5    # мягче: быстрее TP
ATR_TRAIL    = 1.0
ST_MULT      = 3.0
ST_PERIOD    = 10
MACD_FAST    = 12
MACD_SLOW    = 26
MACD_SIG     = 9
RISK_PCT     = 2.0    # 2% риска на сделку
MAX_POS      = 3
DAY_LOSS_PCT = 15.0   # дневной лимит потерь (% от капитала)
GLOBAL_DD    = 30.0   # максимальная просадка от пика (%)
TRADE_INT    = 900     # анализ каждые 15 минут
SL_CHECK_INT = 900     # 15 минут (проверка SL/TP)
CMD_INT      = 3

BYBIT_URL     = "https://api.bybit.com"
BYBIT_TEST_URL= "https://api-testnet.bybit.com"

# ─── BYBIT PUBLIC API (котировки) ─────────────────────────────────────────────

def _bybit_url():
    return BYBIT_TEST_URL if USE_TESTNET else BYBIT_URL

def _okx_symbol(symbol):
    """Конвертировать символ Bybit в формат OKX: BTCUSDT → BTC-USDT"""
    return symbol.replace("USDT", "-USDT")


def _okx_bar(interval):
    """Конвертировать интервал Bybit в OKX: 240 → 4H, D → 1D"""
    mapping = {"1": "1m", "3": "3m", "5": "5m", "15": "15m", "30": "30m",
               "60": "1H", "120": "2H", "240": "4H", "360": "6H",
               "720": "12H", "D": "1D", "W": "1W", "M": "1M"}
    return mapping.get(str(interval), "4H")


def fetch_klines_okx(symbol, interval="240", limit=200):
    """Резервный источник свечей — OKX (публичный API, без ключей)"""
    try:
        bar = _okx_bar(interval)
        inst = _okx_symbol(symbol)
        r = requests.get(
            "https://www.okx.com/api/v5/market/candles",
            params={"instId": inst, "bar": bar, "limit": min(limit, 300)},
            timeout=15,
        )
        data = r.json().get("data", [])
        if not data:
            return None
        # OKX возвращает: [ts, open, high, low, close, vol, volCcy, ...]
        # порядок — от новых к старым, разворачиваем
        rows = [[float(c[0]), float(c[1]), float(c[2]), float(c[3]), float(c[4]), float(c[5]), 0.0]
                for c in reversed(data)]
        df = pd.DataFrame(rows, columns=["ts","op","hi","lo","cl","vol","turnover"])
        df.sort_values("ts", inplace=True)
        df.reset_index(drop=True, inplace=True)
        logger.info("fetch_klines %s: используем OKX (Bybit недоступен)", symbol)
        return df
    except Exception as e:
        logger.error("fetch_klines_okx %s: %s", symbol, e)
        return None


def fetch_klines(symbol, interval="240", limit=200):
    """Получить свечи: сначала Bybit, при ошибке — OKX"""
    try:
        r = requests.get(
            f"{BYBIT_URL}/v5/market/kline",
            params={"category": "linear", "symbol": symbol,
                    "interval": interval, "limit": limit},
            timeout=15,
        )
        r.raise_for_status()
        data = r.json()
        if data.get("retCode") != 0:
            logger.warning("kline %s/%s: %s — пробуем OKX", symbol, interval, data.get("retMsg"))
            return fetch_klines_okx(symbol, interval, limit)
        rows = data["result"]["list"]
        df   = pd.DataFrame(rows, columns=["ts","op","hi","lo","cl","vol","turnover"])
        df   = df.astype({c: float for c in df.columns})
        df.sort_values("ts", inplace=True)
        df.reset_index(drop=True, inplace=True)
        return df
    except Exception as e:
        logger.warning("fetch_klines Bybit %s: %s — пробуем OKX", symbol, e)
        return fetch_klines_okx(symbol, interval, limit)


def fetch_price(symbol):
    """Получить цену: сначала Bybit, при ошибке — OKX, затем CoinGecko"""
    # 1. Bybit futures (основной источник)
    try:
        r = requests.get(
            f"{BYBIT_URL}/v5/market/tickers",
            params={"category": "linear", "symbol": symbol},
            timeout=10,
        )
        lst = r.json().get("result", {}).get("list", [])
        if lst:
            return float(lst[0]["lastPrice"])
    except Exception as e:
        logger.warning("fetch_price Bybit %s: %s — пробуем OKX", symbol, e)

    # 2. OKX (резерв)
    try:
        inst = _okx_symbol(symbol)
        r = requests.get(
            "https://www.okx.com/api/v5/market/ticker",
            params={"instId": inst},
            timeout=8,
        )
        data = r.json().get("data", [])
        if data:
            logger.info("fetch_price %s: используем OKX", symbol)
            return float(data[0]["last"])
    except Exception as e:
        logger.warning("fetch_price OKX %s: %s", symbol, e)

    return None


# ─── МУЛЬТИ-ИСТОЧНИК ЦЕН (без API ключей) ─────────────────────────────────────

def fetch_price_bybit_spot(symbol):
    """Цена через Bybit spot (публичный, без авторизации)"""
    try:
        r = requests.get(
            "https://api.bybit.com/v5/market/tickers",
            params={"category": "spot", "symbol": symbol},
            timeout=8,
        )
        lst = r.json().get("result", {}).get("list", [])
        if lst:
            return float(lst[0]["lastPrice"])
    except Exception:
        pass
    return None


def fetch_price_okx(symbol):
    """Цена через OKX (публичный API, без ключей)"""
    # symbol: BTCUSDT → BTC-USDT
    inst = symbol.replace("USDT", "-USDT")
    try:
        r = requests.get(
            "https://www.okx.com/api/v5/market/ticker",
            params={"instId": inst},
            timeout=8,
        )
        data = r.json().get("data", [])
        if data:
            return float(data[0]["last"])
    except Exception:
        pass
    return None


def fetch_price_coingecko(cg_id):
    """Цена через CoinGecko (публичный API, без ключей)"""
    try:
        r = requests.get(
            "https://api.coingecko.com/api/v3/simple/price",
            params={"ids": cg_id, "vs_currencies": "usd"},
            timeout=10,
        )
        return float(r.json()[cg_id]["usd"])
    except Exception:
        pass
    return None


def fetch_demo_price(symbol, cg_id=None):
    """
    Получить цену монеты из нескольких источников:
    1. Bybit spot (публичный, без ключей)
    2. OKX (публичный)
    3. CoinGecko (публичный)
    Возвращает (цена, источник) или (None, None)
    """
    p = fetch_price_bybit_spot(symbol)
    if p:
        return p, "Bybit"
    p = fetch_price_okx(symbol)
    if p:
        return p, "OKX"
    if cg_id:
        p = fetch_price_coingecko(cg_id)
        if p:
            return p, "CoinGecko"
    return None, None


def demo_coin_by_symbol(symbol):
    """Найти монету в DEMO_COINS по символу"""
    for c in DEMO_COINS:
        if c["symbol"] == symbol:
            return c
    return None


# ─── ДЕМО ПОЗИЦИИ — ЛОГИКА ────────────────────────────────────────────────────

def demo_get_positions(user):
    """Получить список открытых демо-позиций (миграция старых юзеров)"""
    d = user["demo"]
    if "positions" not in d:
        d["positions"] = []
    return d["positions"]


def demo_open_pos(user, symbol, side, usdt_amount):
    """
    Открыть демо-позицию.
    usdt_amount — сумма в USDT без плеча (из баланса).
    Возвращает (True, сообщение) или (False, причина_ошибки).
    """
    d    = user["demo"]
    poss = demo_get_positions(user)

    # Уже есть позиция по этой паре?
    if any(p["symbol"] == symbol for p in poss):
        return False, "По этой монете уже открыта позиция. Сначала закройте её."

    # Максимум 3 позиции
    if len(poss) >= 3:
        return False, "Максимум 3 открытых позиции одновременно."

    # Минимум $5
    if usdt_amount < 5:
        return False, "Минимальная сумма сделки: $5."

    # Баланс достаточен?
    if usdt_amount > d["balance"]:
        return False, f"Недостаточно баланса. Доступно: ${fmt(d['balance'])}"

    coin = demo_coin_by_symbol(symbol)
    if not coin:
        return False, "Монета не найдена."

    price, source = fetch_demo_price(symbol, coin.get("cg_id"))
    if not price:
        return False, "Не удалось получить цену. Попробуйте позже."

    qty = round(usdt_amount / price, 6)

    pos = {
        "symbol":  symbol,
        "name":    coin["name"],
        "short":   coin["short"],
        "emoji":   coin["emoji"],
        "side":    side,        # "LONG" или "SHORT"
        "entry":   price,
        "qty":     qty,
        "usdt":    usdt_amount,
        "lev":     DEMO_LEVERAGE,
        "source":  source,
        "ts":      ts(),
    }
    poss.append(pos)
    d["balance"] = round(d["balance"] - usdt_amount, 4)
    return True, pos


def demo_close_pos(user, symbol):
    """
    Закрыть демо-позицию по символу.
    Возвращает (True, pnl, exit_price) или (False, причина).
    """
    d    = user["demo"]
    poss = demo_get_positions(user)
    pos  = next((p for p in poss if p["symbol"] == symbol), None)
    if not pos:
        return False, "Позиция не найдена.", None

    coin = demo_coin_by_symbol(symbol)
    exit_price, _ = fetch_demo_price(symbol, coin.get("cg_id") if coin else None)
    if not exit_price:
        return False, "Не удалось получить цену закрытия.", None

    entry  = pos["entry"]
    qty    = pos["qty"]
    lev    = pos.get("lev", DEMO_LEVERAGE)
    usdt   = pos["usdt"]
    side   = pos["side"]

    # P&L с учётом плеча
    if side == "LONG":
        pnl = (exit_price - entry) / entry * usdt * lev
    else:
        pnl = (entry - exit_price) / entry * usdt * lev

    pnl = round(pnl, 4)

    # Возвращаем вложенные + P&L
    d["balance"] = round(d["balance"] + usdt + pnl, 4)
    if d["balance"] > d.get("peak", d["balance"]):
        d["peak"] = d["balance"]

    d["trades"] += 1
    if pnl >= 0:
        d["wins"]          += 1
        d["streak_win"]    = d.get("streak_win", 0) + 1
        d["streak_loss"]   = 0
    else:
        d["loss"]          += 1
        d["streak_loss"]   = d.get("streak_loss", 0) + 1
        d["streak_win"]    = 0

    d["profit"] = round(d.get("profit", 0) + pnl, 4)

    # Сохраняем в историю (последние 30)
    hist = d.get("history", [])
    hist.append({
        "symbol": symbol, "side": side,
        "entry":  entry,  "exit": exit_price,
        "usdt":   usdt,   "lev":  lev,
        "pnl":    pnl,    "ts":   ts(),
    })
    d["history"] = hist[-30:]

    # Убираем из открытых позиций
    d["positions"] = [p for p in poss if p["symbol"] != symbol]
    return True, pnl, exit_price


def demo_float_pnl(pos):
    """Плавающий P&L позиции (без закрытия)"""
    coin = demo_coin_by_symbol(pos["symbol"])
    price, _ = fetch_demo_price(pos["symbol"], coin.get("cg_id") if coin else None)
    if not price:
        return None
    entry = pos["entry"]
    usdt  = pos["usdt"]
    lev   = pos.get("lev", DEMO_LEVERAGE)
    if pos["side"] == "LONG":
        return round((price - entry) / entry * usdt * lev, 4)
    else:
        return round((entry - price) / entry * usdt * lev, 4)

# ─── BYBIT TRADING API (реальные ордера) ──────────────────────────────────────

import hmac, hashlib

def _bybit_sign(payload_str: str, ts: int) -> str:
    """
    Правильная подпись Bybit API v5:
      GET:  payload = query_string (e.g. "accountType=UNIFIED")
      POST: payload = json_body    (e.g. '{"category":"linear",...}')
    Формула: HMAC-SHA256(secret, timestamp + api_key + recv_window + payload)
    """
    raw = f"{ts}{BYBIT_KEY}5000{payload_str}"
    return hmac.new(
        BYBIT_SECRET.encode("utf-8"),
        raw.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


# Флаг: Bybit вернул HTML вместо JSON (IP заблокирован) → прекращаем авторизованные вызовы
_bybit_ip_blocked = False

def bybit_request(method, endpoint, params=None):
    """Подписанный запрос к Bybit API v5.
    Если Bybit возвращает не-JSON (HTML-страница блокировки IP) —
    автоматически переключаемся в режим без Bybit на эту сессию.
    """
    global _bybit_ip_blocked
    if not LIVE_MODE or _bybit_ip_blocked:
        return {"retCode": 0, "result": {}}
    params = params or {}
    ts_ms  = int(time.time() * 1000)
    base   = _bybit_url()
    try:
        if method == "GET":
            query_string = "&".join(f"{k}={v}" for k, v in sorted(params.items()))
            sign = _bybit_sign(query_string, ts_ms)
            headers = {
                "X-BAPI-API-KEY":     BYBIT_KEY,
                "X-BAPI-SIGN":        sign,
                "X-BAPI-TIMESTAMP":   str(ts_ms),
                "X-BAPI-RECV-WINDOW": "5000",
            }
            resp = requests.get(f"{base}{endpoint}", params=params, headers=headers, timeout=15)
        else:
            body = json.dumps(params, separators=(",", ":"))
            sign = _bybit_sign(body, ts_ms)
            headers = {
                "X-BAPI-API-KEY":     BYBIT_KEY,
                "X-BAPI-SIGN":        sign,
                "X-BAPI-TIMESTAMP":   str(ts_ms),
                "X-BAPI-RECV-WINDOW": "5000",
                "Content-Type":       "application/json",
            }
            resp = requests.post(f"{base}{endpoint}", data=body, headers=headers, timeout=15)

        # Проверяем что ответ — JSON, а не HTML (страница блокировки IP)
        content_type = resp.headers.get("Content-Type", "")
        if "html" in content_type or (resp.text and resp.text.strip().startswith("<")):
            _bybit_ip_blocked = True
            logger.warning("⚠️ Bybit API недоступен с текущего IP (Railway) — "
                           "переключаюсь в DEMO режим. Установите DEMO_MODE=true в Railway Variables.")
            if ADMIN_ID:
                send(ADMIN_ID,
                     "⚠️ <b>Bybit API заблокирован с IP сервера Railway.</b>\n"
                     "Бот переключён в DEMO режим.\n\n"
                     "Чтобы убрать это сообщение: Railway → Variables → "
                     "добавьте <code>DEMO_MODE=true</code>")
            return {}

        result = resp.json()
        if result.get("retCode") not in (0, None):
            logger.warning("Bybit %s %s → %s: %s",
                           method, endpoint, result.get("retCode"), result.get("retMsg"))
        return result
    except json.JSONDecodeError:
        # Пустой или нечитаемый ответ — скорее всего IP заблокирован
        _bybit_ip_blocked = True
        logger.warning("⚠️ Bybit вернул нечитаемый ответ на %s %s — "
                       "переключаюсь в DEMO (добавьте DEMO_MODE=true в Railway Variables)",
                       method, endpoint)
        return {}
    except Exception as e:
        logger.warning("bybit_request %s %s: %s", method, endpoint, e)
        return {}


def set_leverage(symbol):
    """Установить плечо для пары"""
    if not LIVE_MODE:
        return
    bybit_request("POST", "/v5/position/set-leverage", {
        "category": "linear", "symbol": symbol,
        "buyLeverage": str(LEVERAGE), "sellLeverage": str(LEVERAGE)
    })


def place_order(symbol, side, qty, sl_price, tp_price, reduce_only=False):
    """
    Разместить ордер на Bybit
    side: 'Buy' или 'Sell'
    """
    if not LIVE_MODE:
        return {"retCode": 0, "result": {"orderId": f"DEMO_{int(time.time())}"}}

    params = {
        "category":    "linear",
        "symbol":      symbol,
        "side":        side,
        "orderType":   "Market",
        "qty":         str(qty),
        "reduceOnly":  reduce_only,
        "timeInForce": "GoodTillCancel",
    }
    if not reduce_only:
        params["stopLoss"] = str(round(sl_price, 2))
        params["takeProfit"] = str(round(tp_price, 2))
        params["slTriggerBy"] = "MarkPrice"
        params["tpTriggerBy"] = "MarkPrice"

    result = bybit_request("POST", "/v5/order/create", params)
    if result.get("retCode") != 0:
        logger.error("place_order %s %s: %s", symbol, side, result.get("retMsg"))
    return result


def close_position(symbol, qty, side):
    """Закрыть позицию (side = сторона закрытия: Buy чтобы закрыть Short, Sell чтобы закрыть Long)"""
    return place_order(symbol, side, qty, 0, 0, reduce_only=True)


def get_bybit_balance():
    """
    Получить баланс USDT на Bybit.
    Пробует UNIFIED → CONTRACT → SPOT, возвращает первый найденный.
    """
    if not LIVE_MODE:
        return None
    last_err = "нет ответа"
    for account_type in ("UNIFIED", "CONTRACT", "SPOT"):
        try:
            r   = bybit_request("GET", "/v5/account/wallet-balance",
                                {"accountType": account_type})
            rc  = r.get("retCode", -1)
            msg = r.get("retMsg", "")
            if rc != 0:
                last_err = f"{account_type}: [{rc}] {msg}"
                logger.warning("balance %s", last_err)
                continue
            lst = r.get("result", {}).get("list", [])
            if not lst:
                last_err = f"{account_type}: пустой список"
                continue
            for coin in lst[0].get("coin", []):
                if coin.get("coin") == "USDT":
                    bal = float(coin.get("walletBalance", 0) or 0)
                    logger.info("Bybit balance (%s): $%.2f", account_type, bal)
                    return bal
            last_err = f"{account_type}: USDT не найден среди монет"
        except Exception as e:
            last_err = f"{account_type}: {e}"
            logger.warning("get_bybit_balance %s: %s", account_type, e)
    logger.error("Баланс не получен. Причина: %s", last_err)
    return None


def bybit_debug_info():
    """Полная диагностика Bybit API — для команды /debug"""
    if not LIVE_MODE:
        return "DEMO режим — API ключи не заданы"
    net  = "Testnet" if USE_TESTNET else "Mainnet"
    url  = _bybit_url()
    key  = f"...{BYBIT_KEY[-6:]}" if BYBIT_KEY else "не задан"
    rows = [f"Ключ: {key}", f"Сеть: {net}", f"URL: {url}", ""]
    for account_type in ("UNIFIED", "CONTRACT", "SPOT"):
        try:
            r   = bybit_request("GET", "/v5/account/wallet-balance",
                                {"accountType": account_type})
            rc  = r.get("retCode", "?")
            msg = r.get("retMsg", "")
            if rc == 0:
                lst   = r.get("result", {}).get("list", [])
                coins = []
                if lst:
                    for coin in lst[0].get("coin", []):
                        coins.append(f"{coin.get('coin')}=${coin.get('walletBalance','?')}")
                rows.append(f"OK {account_type}: {', '.join(coins) if coins else 'пусто'}")
            else:
                rows.append(f"ERR {account_type}: [{rc}] {msg}")
        except Exception as e:
            rows.append(f"EXC {account_type}: {e}")
    return "\n".join(rows)


def get_bybit_positions():
    """Получить открытые позиции"""
    if not LIVE_MODE:
        return {}
    positions = {}
    for p in PAIRS:
        r = bybit_request("GET", "/v5/position/list",
                          {"category": "linear", "symbol": p["symbol"]})
        try:
            lst = r["result"]["list"]
            for pos in lst:
                if float(pos.get("size", 0)) > 0:
                    positions[p["symbol"]] = pos
        except Exception:
            pass
    return positions

# ─── ИНДИКАТОРЫ ───────────────────────────────────────────────────────────────

def calc_indicators(df):
    df = df.copy()
    df["ema_mid"]  = df["cl"].ewm(span=EMA_MID,  adjust=False).mean()
    df["ema_slow"] = df["cl"].ewm(span=EMA_SLOW, adjust=False).mean()

    hl = df["hi"] - df["lo"]
    hc = (df["hi"] - df["cl"].shift()).abs()
    lc = (df["lo"] - df["cl"].shift()).abs()
    tr = pd.concat([hl, hc, lc], axis=1).max(axis=1)
    df["atr"] = tr.ewm(span=ATR_PERIOD, adjust=False).mean()

    # Supertrend
    hl2   = (df["hi"] + df["lo"]) / 2
    upper = hl2 + ST_MULT * df["atr"]
    lower = hl2 - ST_MULT * df["atr"]
    f_up  = upper.copy()
    f_lo  = lower.copy()
    st_dir = [1] * len(df)

    for i in range(1, len(df)):
        if upper.iloc[i] < f_up.iloc[i-1] or df["cl"].iloc[i-1] > f_up.iloc[i-1]:
            f_up.iloc[i] = upper.iloc[i]
        else:
            f_up.iloc[i] = f_up.iloc[i-1]
        if lower.iloc[i] > f_lo.iloc[i-1] or df["cl"].iloc[i-1] < f_lo.iloc[i-1]:
            f_lo.iloc[i] = lower.iloc[i]
        else:
            f_lo.iloc[i] = f_lo.iloc[i-1]
        if st_dir[i-1] == -1 and df["cl"].iloc[i] > f_up.iloc[i-1]:
            st_dir[i] = 1
        elif st_dir[i-1] == 1 and df["cl"].iloc[i] < f_lo.iloc[i-1]:
            st_dir[i] = -1
        else:
            st_dir[i] = st_dir[i-1]

    df["st_dir"] = st_dir

    # RSI
    delta = df["cl"].diff()
    gain  = delta.clip(lower=0).ewm(span=RSI_PERIOD, adjust=False).mean()
    loss  = (-delta.clip(upper=0)).ewm(span=RSI_PERIOD, adjust=False).mean()
    rs    = gain / loss.replace(0, float("inf"))
    df["rsi"] = 100 - (100 / (1 + rs))

    # MACD
    mf          = df["cl"].ewm(span=MACD_FAST, adjust=False).mean()
    ms          = df["cl"].ewm(span=MACD_SLOW, adjust=False).mean()
    mc          = (mf - ms).ewm(span=MACD_SIG, adjust=False).mean()
    df["macd_h"] = (mf - ms) - mc
    return df


def get_daily_trend(symbol):
    """Получить дневной тренд (1D) — фильтр для снижения ложных сигналов"""
    df1d = fetch_klines(symbol, interval="D", limit=60)
    if df1d is None or len(df1d) < 55:
        return 0
    df1d = calc_indicators(df1d)
    last = df1d.iloc[-1]
    if last["cl"] > last["ema_slow"]:
        return 1   # Бычий
    if last["cl"] < last["ema_slow"]:
        return -1  # Медвежий
    return 0


def get_signal(df4h, trend_1d):
    """
    Мягкая стратегия: достаточно 3 из 4 индикаторов.
    1D тренд используется как бонус-фильтр, но не блокирует сигнал.
    Возвращает: 'LONG', 'SHORT' или None
    """
    if len(df4h) < 3:
        return None
    c = df4h.iloc[-1]
    p = df4h.iloc[-2]

    ema_bull  = c["ema_mid"] > c["ema_slow"]
    ema_bear  = c["ema_mid"] < c["ema_slow"]
    st_bull   = c["st_dir"] == 1
    st_bear   = c["st_dir"] == -1
    rsi_long  = RSI_LONG_MIN  <= c["rsi"] <= RSI_LONG_MAX
    rsi_short = RSI_SHORT_MIN <= c["rsi"] <= RSI_SHORT_MAX
    macd_up   = c["macd_h"] > p["macd_h"]   # гистограмма растёт (любое значение)
    macd_down = c["macd_h"] < p["macd_h"]   # гистограмма падает

    long_score  = sum([ema_bull, st_bull, rsi_long,  macd_up])
    short_score = sum([ema_bear, st_bear, rsi_short, macd_down])

    # 3 из 4 + дневной тренд не против
    if long_score >= 3 and trend_1d >= 0:
        return "LONG"
    if short_score >= 3 and trend_1d <= 0:
        return "SHORT"
    # Все 4 совпали — входим даже против дневного тренда
    if long_score == 4:
        return "LONG"
    if short_score == 4:
        return "SHORT"
    return None


def check_exit_signal(df4h, side):
    """Проверить сигнал на выход из позиции"""
    c = df4h.iloc[-1]
    p = df4h.iloc[-2]
    if side == "LONG":
        return (c["st_dir"] == -1 and p["st_dir"] == 1) or \
               (c["ema_mid"] < c["ema_slow"]) or \
               c["rsi"] > 75
    if side == "SHORT":
        return (c["st_dir"] == 1 and p["st_dir"] == -1) or \
               (c["ema_mid"] > c["ema_slow"]) or \
               c["rsi"] < 25
    return False

# ─── СОСТОЯНИЕ БОТА ───────────────────────────────────────────────────────────

BOT_STATES = {}


def load_bot_state(sym):
    f = DATA_DIR / f"{sym}_state.json"
    if f.exists():
        try:
            with open(f, encoding="utf-8") as fp:
                return json.load(fp)
        except Exception:
            pass
    return {
        "usdt": 10000.0, "n": 0, "wins": 0, "loss": 0, "pnl": 0.0,
        "peak": 10000.0, "day_start": 10000.0, "day_date": "",
        "halted": False, "halt_until": 0, "pos": None,
    }


def save_bot_state(sym, s):
    with open(DATA_DIR / f"{sym}_state.json", "w", encoding="utf-8") as f:
        json.dump(s, f, indent=2, ensure_ascii=False, default=str)


def log_trade(t):
    with open(TRADES_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(t, ensure_ascii=False, default=str) + "\n")


def all_trades():
    trades = []
    if TRADES_FILE.exists():
        with open(TRADES_FILE, encoding="utf-8") as f:
            for line in f:
                l = line.strip()
                if l:
                    try:
                        trades.append(json.loads(l))
                    except Exception:
                        pass
    return trades


def active_positions():
    return sum(1 for s in BOT_STATES.values() if s.get("pos"))

# ─── ИСПОЛНЕНИЕ СДЕЛОК ────────────────────────────────────────────────────────

def do_open(pair, price, atr, side):
    """
    Открыть позицию (LONG или SHORT).
    Капитал берётся из демо-пула пользователей — деньги реально замораживаются
    на их балансах пропорционально.
    """
    s   = BOT_STATES[pair["symbol"]]
    sym = pair["symbol"]
    if s.get("pos") or active_positions() >= MAX_POS:
        return None
    if s.get("halted") and time.time() < s.get("halt_until", 0):
        return None

    # Используем общий пул пользователей как капитал.
    # Если пул пуст (нет пользователей) — торгуем на внутреннем виртуальном
    # капитале бота ($10 000 на пару), чтобы анализ и сделки всегда шли.
    if LIVE_MODE:
        cap = s["usdt"]
    else:
        cap = pool_balance()
        if cap < 10:
            cap = s["usdt"]
            logger.info("%s: пул пользователей пуст — используем внутренний капитал $%.2f", sym, cap)

    if cap < 10:
        logger.warning("%s: недостаточно капитала ($%.2f)", sym, cap)
        return None

    if side == "LONG":
        sl = price - ATR_SL_MULT * atr
        tp = price + ATR_TP_MULT * atr
    else:
        sl = price + ATR_SL_MULT * atr
        tp = price - ATR_TP_MULT * atr

    sl = round(sl, 2)
    tp = round(tp, 2)

    # Размер позиции: риск RISK_PCT% от пула
    risk    = cap * (RISK_PCT / 100)
    sl_dist = abs(price - sl)
    qty     = round(risk / max(sl_dist, 0.0001), 6)
    qty     = max(qty, pair["min_qty"])

    # С учётом плеча — не больше 30% пула на одну позицию
    position_value = qty * price
    margin_needed  = position_value / LEVERAGE
    max_margin     = cap * 0.30
    if margin_needed > max_margin:
        qty           = round(max_margin * LEVERAGE / price, 6)
        qty           = max(qty, pair["min_qty"])
        margin_needed = qty * price / LEVERAGE

    # Реальный ордер на Bybit (только если LIVE_MODE и ключи валидны)
    _use_live = LIVE_MODE
    if _use_live:
        set_leverage(sym)
        order_side = "Buy" if side == "LONG" else "Sell"
        result     = place_order(sym, order_side, qty, sl, tp)
        ret_code   = result.get("retCode", -1)
        ret_msg    = result.get("retMsg", "unknown")
        if ret_code != 0:
            if ret_code in (10003, 10004, 10005):  # невалидный API ключ
                logger.error("Ошибка ордера %s: %s — переключаюсь в DEMO", sym, ret_msg)
                _use_live = False   # автофолбэк в демо
            else:
                logger.error("Ошибка ордера %s: %s", sym, ret_msg)
                return None
        else:
            order_id = result.get("result", {}).get("orderId", "")

    if not _use_live:
        order_id = f"DEMO_{sym}_{int(time.time())}"

    margin = round(qty * price / LEVERAGE, 4)

    # Заморозить деньги у пользователей (берём из их балансов)
    if not _use_live:
        locks = pool_lock(pair["name"], margin)
        # Уведомляем каждого пользователя о заморозке
        if locks:
            users_data = load_users()
            for uid, user_margin in locks.items():
                u = users_data.get(uid, {})
                if u.get("notify", True):
                    try:
                        icon = "📈" if side == "LONG" else "📉"
                        send(uid,
                             f"🔒 <b>Бот открыл {side} {pair['emoji']} {pair['name']}</b>\n"
                             f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
                             f"Вход: <b>${fmt(price)}</b> | {icon} {side}\n"
                             f"Заморожено с вашего баланса: <b>${fmt(user_margin)}</b>\n"
                             f"Доступный баланс: <b>${fmt(u['demo'].get('balance', 0))}</b>\n"
                             f"SL: ${fmt(sl)} | TP: ${fmt(tp)}\n"
                             f"🕐 {ts()}")
                    except Exception:
                        pass

    # Обновляем состояние бота
    s["usdt"] = max(s.get("usdt", 10000) - margin, 0)
    # s["n"] считается только при ЗАКРЫТИИ, не при открытии
    s["pos"]   = {
        "side": side, "entry": price, "qty": qty,
        "sl": sl, "tp": tp, "atr": atr,
        "trail_sl": sl, "time": ts(),
        "id": order_id,
        "margin": margin,
    }
    save_bot_state(sym, s)
    t = {**s["pos"], "pair": pair["name"], "action": "OPEN",
         "equity": round(cap, 2)}
    log_trade(t)
    return t


def update_trailing(pair, price):
    """Обновить трейлинг-стоп"""
    s   = BOT_STATES[pair["symbol"]]
    pos = s.get("pos")
    if not pos:
        return
    atr  = pos["atr"]
    side = pos["side"]

    if side == "LONG":
        new_sl = round(price - ATR_TRAIL * atr, 2)
        if new_sl > pos.get("trail_sl", pos["sl"]) and new_sl > pos["sl"]:
            pos["sl"] = pos["trail_sl"] = new_sl
    else:
        new_sl = round(price + ATR_TRAIL * atr, 2)
        if new_sl < pos.get("trail_sl", pos["sl"]) and new_sl < pos["sl"]:
            pos["sl"] = pos["trail_sl"] = new_sl

    s["pos"] = pos
    save_bot_state(pair["symbol"], s)


def do_close(pair, price, reason="SIGNAL"):
    """Закрыть позицию"""
    s   = BOT_STATES[pair["symbol"]]
    sym = pair["symbol"]
    pos = s.get("pos")
    if not pos:
        return None

    side = pos["side"]
    qty  = pos["qty"]

    if side == "LONG":
        pnl = (price - pos["entry"]) * qty * LEVERAGE
    else:
        pnl = (pos["entry"] - price) * qty * LEVERAGE

    pnl = round(pnl, 4)

    # Реальное закрытие на Bybit
    if LIVE_MODE:
        close_side = "Sell" if side == "LONG" else "Buy"
        close_position(sym, qty, close_side)

    # Обновляем баланс
    margin     = pos["margin"]
    s["usdt"] += margin + pnl
    s["pnl"]  += pnl
    s["n"]    += 1
    if pnl >= 0:
        s["wins"] += 1
    else:
        s["loss"] += 1
    if s["usdt"] > s.get("peak", 0):
        s["peak"] = s["usdt"]

    t = {
        "pair": pair["name"], "action": "CLOSE",
        "side": side, "qty": qty,
        "entry": pos["entry"], "price": price,
        "pnl": pnl, "pnl_pct": round(pnl / margin * 100, 2) if margin else 0,
        "reason": reason, "time": ts(),
        "equity": round(s["usdt"], 2),
    }
    s["pos"] = None
    save_bot_state(sym, s)
    log_trade(t)
    distribute(pair["name"], pnl, pnl >= 0)          # для реальных инвесторов
    pool_release(pair["name"], pnl, pnl >= 0)         # вернуть деньги демо-пула
    return t


def check_exits(pair, price, df4h):
    """Проверить SL/TP и сигнальный выход"""
    s   = BOT_STATES[pair["symbol"]]
    pos = s.get("pos")
    if not pos:
        return False
    update_trailing(pair, price)
    pos  = s["pos"]
    side = pos["side"]

    hit_sl = (side == "LONG"  and price <= pos["sl"]) or \
             (side == "SHORT" and price >= pos["sl"])
    hit_tp = (side == "LONG"  and price >= pos["tp"]) or \
             (side == "SHORT" and price <= pos["tp"])

    if hit_sl:
        t   = do_close(pair, price, "STOP-LOSS")
        if t:
            pnl  = t["pnl"]
            msg  = (
                f"⛔ <b>СТОП-ЛОСС | {pair['emoji']} {pair['name']} {side}</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"Вход: <b>${fmt(pos['entry'])}</b> → Выход: <b>${fmt(price)}</b>\n"
                f"P&L: <code>{sign(pnl)}${fmt(abs(pnl))}</code> "
                f"({sign(t['pnl_pct'])}{t['pnl_pct']:.1f}%)\n"
                f"🕐 {ts()}"
            )
            notify_all_users(msg)
        return True

    if hit_tp:
        t   = do_close(pair, price, "TAKE-PROFIT")
        if t:
            pnl  = t["pnl"]
            msg  = (
                f"🎯 <b>ТЕЙК-ПРОФИТ | {pair['emoji']} {pair['name']} {side}</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"Вход: <b>${fmt(pos['entry'])}</b> → Выход: <b>${fmt(price)}</b>\n"
                f"P&L: <code>+${fmt(pnl)}</code> "
                f"({sign(t['pnl_pct'])}{t['pnl_pct']:.1f}%)\n"
                f"🕐 {ts()}"
            )
            notify_all_users(msg)
        return True

    if df4h is not None and check_exit_signal(df4h, side):
        t   = do_close(pair, price, "SIGNAL-EXIT")
        if t:
            pnl  = t["pnl"]
            icon = "✅" if pnl >= 0 else "❌"
            msg  = (
                f"{icon} <b>ВЫХОД | {pair['emoji']} {pair['name']} {side}</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"Вход: ${fmt(pos['entry'])} → Выход: ${fmt(price)}\n"
                f"P&L: <code>{sign(pnl)}${fmt(abs(pnl))}</code>\n"
                f"🕐 {ts()}"
            )
            notify_all_users(msg)
        return True

    return False


def _true_equity(s):
    """
    Реальный капитал = свободные деньги + маржа в открытой позиции.
    Нужен чтобы circuit breaker не путал залог маржи с убытком.
    """
    equity = s.get("usdt", 10000)
    pos    = s.get("pos")
    if pos:
        equity += pos.get("margin", 0)   # добавляем обратно заложенную маржу
    return equity


def circuit_breaker(sym):
    s     = BOT_STATES[sym]
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    # Сброс дневного счётчика в начале нового дня
    if s.get("day_date") != today:
        s["day_date"]  = today
        s["day_start"] = _true_equity(s)   # истинный капитал, не s["usdt"]
        s["halted"]    = False

    # Истинный капитал: свободные средства + маржа в открытой позиции
    equity = _true_equity(s)

    # Проверка дневного лимита убытков (только по РЕАЛЬНЫМ потерям)
    if s["day_start"] > 0:
        dd = (s["day_start"] - equity) / s["day_start"] * 100
        if dd > DAY_LOSS_PCT and not s.get("halted"):
            s["halted"]     = True
            s["halt_until"] = time.time() + 86400
            save_bot_state(sym, s)
            send(ADMIN_ID,
                 f"⛔ <b>CIRCUIT BREAKER — {sym}</b>\n"
                 f"Дневной убыток: {dd:.1f}% (лимит {DAY_LOSS_PCT}%)\n"
                 f"Торговля приостановлена на 24ч")
            return True

    # Проверка глобальной просадки от исторического пика
    peak = s.get("peak", equity)
    if peak > 0 and equity < peak:
        gdd = (peak - equity) / peak * 100
        if gdd > GLOBAL_DD and not s.get("halted"):
            s["halted"]     = True
            s["halt_until"] = time.time() + 86400 * 3
            save_bot_state(sym, s)
            send(ADMIN_ID,
                 f"🚨 <b>ГЛОБАЛЬНАЯ ЗАЩИТА — {sym}</b>\n"
                 f"Просадка от пика: {gdd:.1f}% (лимит {GLOBAL_DD}%)\n"
                 f"Торговля остановлена на 3 дня!")
            return True

    # Снятие блокировки после паузы
    if s.get("halted") and time.time() >= s.get("halt_until", 0):
        s["halted"]     = False
        s["halt_until"] = 0
        s["day_start"]  = _true_equity(s)
        save_bot_state(sym, s)
        send(ADMIN_ID, f"✅ Торговля возобновлена: {sym}")

    save_bot_state(sym, s)
    return s.get("halted", False)

# ─── УТИЛИТЫ ──────────────────────────────────────────────────────────────────

def ts():
    return datetime.now(timezone.utc).strftime("%d.%m.%Y %H:%M UTC")

def fmt(v, spec=",.2f"):
    return format(float(v), spec)

def sign(v):
    return "+" if float(v) >= 0 else ""

def pct_val(profit, base):
    return (float(profit) / float(base) * 100) if float(base) > 0 else 0.0

def wr_calc(w, l):
    return round(w / max(w + l, 1) * 100)

# ─── ПОЛЬЗОВАТЕЛИ ─────────────────────────────────────────────────────────────

def load_users():
    if USERS_FILE.exists():
        try:
            with open(USERS_FILE, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def save_users(u):
    with open(USERS_FILE, "w", encoding="utf-8") as f:
        json.dump(u, f, indent=2, ensure_ascii=False, default=str)


def _gen_ref():
    return "".join(random.choices(string.ascii_uppercase + string.digits, k=8))


def get_user(cid):
    users = load_users()
    uid   = str(cid)
    if uid not in users:
        users[uid] = {
            "id": uid, "name": "", "joined": ts(),
            "demo": {
                "balance": 1000.0, "start": 1000.0, "peak": 1000.0,
                "locked": 0.0,
                "profit": 0.0, "trades": 0, "wins": 0, "loss": 0,
                "history": [], "streak_win": 0, "streak_loss": 0, "max_dd": 0.0,
            },
            "real": {
                "balance": 0.0, "deposited": 0.0, "peak": 0.0,
                "profit": 0.0, "trades": 0, "wins": 0, "loss": 0,
                "history": [], "active": False, "autocompound": True,
                "pending": 0.0, "pending_txid": "",
                "withdrawals": [], "streak_win": 0, "streak_loss": 0, "max_dd": 0.0,
            },
            "notify": True,
            "ref_code": _gen_ref(),
            "ref_by": None, "ref_count": 0, "ref_bonus": 0.0,
            "last_seen": ts(),
        }
        save_users(users)
    return users[uid]


def save_user(cid, u):
    users = load_users()
    users[str(cid)] = u
    save_users(users)


def is_admin(cid):
    return str(cid) in [x.strip() for x in ADMIN_IDS.split(",") if x.strip()]


# ─── ЗАЯВКИ НА ПОПОЛНЕНИЕ ─────────────────────────────────────────────────────

def load_pending_deposits():
    try:
        if DEPOSITS_FILE.exists():
            return json.loads(DEPOSITS_FILE.read_text())
    except Exception:
        pass
    return {}

def save_pending_deposits(deps):
    try:
        DEPOSITS_FILE.write_text(json.dumps(deps, ensure_ascii=False, indent=2))
    except Exception as e:
        logger.error("save_pending_deposits: %s", e)

def add_pending_deposit(uid, amount, username=""):
    deps = load_pending_deposits()
    deps[str(uid)] = {"amount": amount, "username": username, "time": ts()}
    save_pending_deposits(deps)

def remove_pending_deposit(uid):
    deps = load_pending_deposits()
    deps.pop(str(uid), None)
    save_pending_deposits(deps)

# ─── УВЕДОМЛЕНИЯ ВСЕМ ПОЛЬЗОВАТЕЛЯМ ──────────────────────────────────────────

def notify_all_users(text):
    """Отправить сообщение всем пользователям с включёнными уведомлениями.
    Администратор всегда получает уведомления, даже если не писал /start.
    """
    users = load_users()
    sent  = set()

    # Администратор получает ВСЕ уведомления о сделках
    if ADMIN_ID:
        try:
            send(ADMIN_ID, text)
        except Exception:
            pass
        sent.add(str(ADMIN_ID))

    # Остальные зарегистрированные пользователи
    for uid, u in users.items():
        if uid not in sent and u.get("notify", True):
            try:
                send(uid, text)
            except Exception:
                pass

# ─── РАСПРЕДЕЛЕНИЕ ПРИБЫЛИ ────────────────────────────────────────────────────

def distribute(pair_name, pnl, is_win):
    users     = load_users()
    total_dep = sum(u["real"]["deposited"] for u in users.values() if u["real"]["active"])
    if total_dep <= 0 or pnl == 0:
        return
    for uid, u in users.items():
        r = u["real"]
        if not r["active"] or r["deposited"] <= 0:
            continue
        share    = r["deposited"] / total_dep
        user_pnl = round(pnl * share, 4)
        r["profit"]  += user_pnl
        r["balance"] += user_pnl
        if r["autocompound"] and user_pnl > 0:
            r["deposited"] += user_pnl
        r["trades"] += 1
        if is_win:
            r["wins"]          += 1
            r["streak_win"]    = r.get("streak_win", 0) + 1
            r["streak_loss"]   = 0
        else:
            r["loss"]          += 1
            r["streak_loss"]   = r.get("streak_loss", 0) + 1
            r["streak_win"]    = 0
        r["history"].append({"pair": pair_name, "pnl": user_pnl, "time": ts()})
        r["history"] = r["history"][-30:]   # ограничение истории
        if r["balance"] > r.get("peak", 0):
            r["peak"] = r["balance"]
        # Максимальная просадка реального счёта
        if r.get("deposited", 0) > 0:
            dd_r = (r["deposited"] - r["balance"]) / r["deposited"] * 100
            if dd_r > r.get("max_dd", 0):
                r["max_dd"] = round(dd_r, 2)
        u["real"] = r
        users[uid] = u
        if u.get("notify") and user_pnl != 0:
            icon = "✅" if is_win else "❌"
            send(uid,
                 f"{icon} <b>{pair_name}</b> — сделка закрыта\n"
                 f"P&L: <code>{sign(user_pnl)}${fmt(abs(user_pnl))}</code>\n"
                 f"Баланс: <b>${fmt(r['balance'])}</b>")
    save_users(users)


# ─── ПУЛ ПОЛЬЗОВАТЕЛЕЙ — управление капиталом ─────────────────────────────────

def pool_balance():
    """Суммарный доступный демо-баланс всех пользователей (не включая locked)"""
    users = load_users()
    return sum(u["demo"].get("balance", 0) for u in users.values())


def pool_total():
    """Полный капитал пула: balance + locked (для оценки доли каждого)"""
    users = load_users()
    return sum(
        u["demo"].get("balance", 0) + u["demo"].get("locked", 0)
        for u in users.values()
    )


def pool_lock(pair_name, margin):
    """
    Заморозить margin из демо-балансов всех пользователей пропорционально.
    Каждой паре ведётся отдельный учёт (locked_by_pair), чтобы при закрытии
    одной из нескольких позиций не разморозились чужие деньги.
    Возвращает {uid: locked_amount}.
    """
    users = load_users()
    total = sum(u["demo"].get("balance", 0) for u in users.values())
    if total <= 0:
        return {}
    locks = {}
    for uid, u in users.items():
        d   = u["demo"]
        bal = d.get("balance", 0)
        if bal <= 0:
            continue
        share       = bal / total
        user_margin = round(margin * share, 4)
        d["balance"] = round(bal - user_margin, 4)
        d["locked"]  = round(d.get("locked", 0) + user_margin, 4)
        # Храним сколько заморожено именно для этой пары
        lbp = d.get("locked_by_pair", {})
        lbp[pair_name] = round(lbp.get(pair_name, 0) + user_margin, 4)
        d["locked_by_pair"] = lbp
        locks[uid]   = user_margin
        u["demo"]    = d
        users[uid]   = u
    save_users(users)
    return locks


def pool_release(pair_name, pnl, is_win):
    """
    Разморозить locked средства ТОЛЬКО для данной пары + распределить P&L.
    Это позволяет правильно работать когда одновременно открыто 2–3 позиции:
    закрытие BTC не разморозит деньги, заложенные под ETH или SOL.
    """
    users = load_users()

    # Суммарно заморожено под эту конкретную пару по всем пользователям
    total_pair_locked = sum(
        u["demo"].get("locked_by_pair", {}).get(pair_name, 0)
        for u in users.values()
    )
    if total_pair_locked <= 0:
        # Фолбэк: если учёт по парам не ведётся (старые данные) — используем total locked
        total_pair_locked = sum(u["demo"].get("locked", 0) for u in users.values())
        if total_pair_locked <= 0:
            return
        fallback = True
    else:
        fallback = False

    for uid, u in users.items():
        d = u["demo"]
        if fallback:
            pair_locked = d.get("locked", 0)
        else:
            pair_locked = d.get("locked_by_pair", {}).get(pair_name, 0)

        if pair_locked <= 0:
            continue

        share    = pair_locked / total_pair_locked
        user_pnl = round(pnl * share, 4)
        returned = round(pair_locked + user_pnl, 4)

        # Возвращаем деньги на баланс (только ту долю, что была в этой паре)
        d["balance"] = round(d.get("balance", 0) + returned, 4)

        # Уменьшаем total locked на сумму этой пары
        if fallback:
            d["locked"] = 0.0
        else:
            d["locked"] = round(max(0.0, d.get("locked", 0) - pair_locked), 4)
            lbp = d.get("locked_by_pair", {})
            lbp.pop(pair_name, None)
            d["locked_by_pair"] = lbp

        d["profit"]  = round(d.get("profit", 0) + user_pnl, 4)
        d["trades"]  = d.get("trades", 0) + 1
        if is_win:
            d["wins"]          = d.get("wins", 0) + 1
            d["streak_win"]    = d.get("streak_win", 0) + 1
            d["streak_loss"]   = 0
        else:
            d["loss"]          = d.get("loss", 0) + 1
            d["streak_loss"]   = d.get("streak_loss", 0) + 1
            d["streak_win"]    = 0
        prev_balance = returned - user_pnl   # баланс до этой сделки ≈ pair_locked
        if d["balance"] > d.get("peak", 0):
            d["peak"] = d["balance"]
        # Максимальная просадка демо-счёта
        start_bal = d.get("start", 1000)
        if start_bal > 0 and d["balance"] < start_bal:
            dd_demo = (start_bal - d["balance"]) / start_bal * 100
            if dd_demo > d.get("max_dd", 0):
                d["max_dd"] = round(dd_demo, 2)
        hist = d.get("history", [])
        hist.append({"pair": pair_name, "pnl": user_pnl, "time": ts()})
        d["history"] = hist[-30:]

        # Проверяем достижения (только при росте баланса)
        MILESTONES = [1100, 1250, 1500, 2000, 3000, 5000]
        if user_pnl > 0:
            for ms in MILESTONES:
                # Баланс ПЕРЕСЁК milestone снизу вверх
                if (returned - user_pnl) < ms <= d["balance"]:
                    try:
                        send(uid,
                             f"🏆 <b>Достижение!</b>\n"
                             f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
                             f"Ваш демо-баланс достиг <b>${ms:,}</b>! 🎉\n"
                             f"Текущий баланс: <b>${fmt(d['balance'])}</b>\n"
                             f"Прирост от старта: +${fmt(d['balance'] - d.get('start', 1000))}\n\n"
                             f"Готовы перейти на реальный счёт? 💰")
                    except Exception:
                        pass
                    break

        u["demo"]    = d
        users[uid]   = u

        # Реферальный бонус: 5% от прибыли уходит реферреру
        if user_pnl > 0 and u.get("ref_by"):
            ref_id  = u["ref_by"]
            ref_cut = round(user_pnl * 0.05, 4)
            ru      = users.get(str(ref_id))
            if ru:
                ru["demo"]["balance"] = round(ru["demo"].get("balance", 0) + ref_cut, 4)
                ru["ref_bonus"]       = round(ru.get("ref_bonus", 0) + ref_cut, 4)
                users[str(ref_id)]    = ru

        # Личное уведомление пользователю о результате
        if u.get("notify", True):
            icon     = "✅" if is_win else "❌"
            sign_str = "+" if user_pnl >= 0 else ""
            try:
                send(uid,
                     f"{icon} <b>Сделка закрыта: {pair_name}</b>\n"
                     f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
                     f"Ваш результат: <code>{sign_str}${fmt(abs(user_pnl))}</code>\n"
                     f"Возвращено на баланс: <b>${fmt(returned)}</b>\n"
                     f"Доступный баланс: <b>${fmt(d['balance'])}</b>")
            except Exception:
                pass
    save_users(users)

# ─── TELEGRAM ─────────────────────────────────────────────────────────────────

def api(method, data=None, _timeout=20):
    """Вызов Telegram Bot API с повторными попытками при сбое сети."""
    if not TOKEN:
        return {}
    url = f"https://api.telegram.org/bot{TOKEN}/{method}"
    for attempt in range(3):
        try:
            r = requests.post(url, json=data or {}, timeout=_timeout)
            return r.json()
        except Exception as e:
            if attempt < 2:
                time.sleep(3)
            else:
                # getUpdates таймауты — ожидаемое поведение, пишем DEBUG
                if method == "getUpdates":
                    logger.debug("TG %s: %s", method, e)
                else:
                    logger.error("TG %s: %s", method, e)
    return {}


def send(cid, text, buttons=None):
    if not TOKEN:
        return {}
    d = {"chat_id": str(cid), "text": text[:4096], "parse_mode": "HTML"}
    if buttons:
        d["reply_markup"] = json.dumps({"inline_keyboard": buttons})
    res = api("sendMessage", d)
    if not res.get("ok"):
        logger.warning("sendMessage %s: %s", cid, res.get("description", ""))
    return res


def answer_cb(cb_id, text=""):
    api("answerCallbackQuery", {"callback_query_id": cb_id, "text": text})

# ─── КНОПКИ ───────────────────────────────────────────────────────────────────

def kb_main():
    mode = "🟢 LIVE (Bybit)" if LIVE_MODE else "🎮 DEMO"
    return [
        [{"text": "🎮 Демо-трейдинг",      "callback_data": "demo_trade"},
         {"text": "👤 Аккаунт",            "callback_data": "account"}],
        [{"text": "📊 Статистика",         "callback_data": "stats"},
         {"text": "🌐 Рынок",              "callback_data": "market"}],
        [{"text": "📈 Сделки",             "callback_data": "history"},
         {"text": "❓ Стратегия",          "callback_data": "strategy"}],
        [{"text": "💰 Пополнить",          "callback_data": "deposit"},
         {"text": "💸 Вывести",           "callback_data": "withdraw"}],
        [{"text": "🤝 Реферальная",        "callback_data": "referral"},
         {"text": f"⚡ Режим: {mode}",     "callback_data": "mode_info"}],
    ]

def kb_back():
    return [[{"text": "🏠 Главное меню", "callback_data": "menu"}]]


def kb_demo_back():
    return [[{"text": "◀️ Демо-торговля", "callback_data": "demo_trade"}]]


def kb_demo_trade(positions):
    """Клавиатура главного демо-экрана"""
    rows = [
        [{"text": "📊 Открыть Long/Short", "callback_data": "demo_open_menu"}],
    ]
    if positions:
        rows.append([{"text": "📌 Мои позиции", "callback_data": "demo_positions"}])
    rows.append([{"text": "📜 История сделок", "callback_data": "demo_history"}])
    rows.append([{"text": "🔄 Сбросить баланс ($1000)", "callback_data": "demo_reset"}])
    rows.append([{"text": "🏠 Главное меню", "callback_data": "menu"}])
    return rows


def kb_demo_coin_select(action):
    """Клавиатура выбора монеты (action: long / short)"""
    rows = []
    row  = []
    for i, c in enumerate(DEMO_COINS):
        row.append({"text": f"{c['emoji']} {c['short']}", "callback_data": f"demo_{action}_{c['symbol']}"})
        if len(row) == 3:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([{"text": "◀️ Назад", "callback_data": "demo_trade"}])
    return rows


def kb_demo_amount(symbol, side):
    """Клавиатура выбора суммы для демо-сделки"""
    amounts = [10, 25, 50, 100, 200]
    rows    = []
    row     = []
    for a in amounts:
        row.append({"text": f"${a}", "callback_data": f"demo_exec_{side}_{symbol}_{a}"})
        if len(row) == 3:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([{"text": "◀️ Назад", "callback_data": f"demo_open_{side}"}])
    return rows


def kb_demo_positions(poss):
    """Кнопки закрытия открытых позиций"""
    rows = []
    for p in poss:
        rows.append([{"text": f"❌ Закрыть {p['emoji']} {p['short']} {p['side']}",
                      "callback_data": f"demo_close_{p['symbol']}"}])
    rows.append([{"text": "◀️ Демо-торговля", "callback_data": "demo_trade"}])
    return rows

def kb_account():
    return [
        [{"text": "🎮 Демо-трейдинг",  "callback_data": "demo_trade"},
         {"text": "💼 Реальный счёт",  "callback_data": "real"}],
        [{"text": "📊 Моя статистика", "callback_data": "my_stats"},
         {"text": "🏆 Лидерборд",      "callback_data": "leaderboard"}],
        [{"text": "💰 Пополнить",      "callback_data": "deposit"},
         {"text": "💸 Вывести",       "callback_data": "withdraw"}],
        [{"text": "🏠 Главное меню",   "callback_data": "menu"}],
    ]

def kb_deposit():
    return [
        [{"text": "$50",   "callback_data": "dep_50"},
         {"text": "$100",  "callback_data": "dep_100"},
         {"text": "$200",  "callback_data": "dep_200"}],
        [{"text": "$500",  "callback_data": "dep_500"},
         {"text": "$1000", "callback_data": "dep_1000"},
         {"text": "✏️ Своя","callback_data": "dep_custom"}],
        [{"text": "❌ Отмена","callback_data": "menu"}],
    ]

def kb_confirm_dep(amount):
    return [
        [{"text": "✅ Я отправил платёж", "callback_data": f"depsent_{amount}"}],
        [{"text": "❌ Отмена",           "callback_data": "menu"}],
    ]

def kb_admin_dep(uid, amount):
    """Кнопки подтверждения/отклонения депозита для администратора"""
    a = int(amount * 100)   # храним в центах чтобы избежать проблем с точкой
    return [
        [{"text": "✅ Подтвердить платёж", "callback_data": f"dep_ok_{uid}_{a}"},
         {"text": "❌ Отклонить",          "callback_data": f"dep_no_{uid}_{a}"}],
    ]

def kb_admin():
    return [
        [{"text": "👥 Пользователи", "callback_data": "adm_users"},
         {"text": "📊 Статистика",   "callback_data": "adm_stats"}],
        [{"text": "💰 Депозиты",     "callback_data": "adm_deposits"},
         {"text": "💸 Выводы",       "callback_data": "adm_withdrawals"}],
        [{"text": "📢 Рассылка",     "callback_data": "adm_broadcast"},
         {"text": "📋 Все сделки",   "callback_data": "adm_trades"}],
        [{"text": "🔄 Сброс CB",     "callback_data": "adm_reset_cb"}],
        [{"text": "🏠 Главное меню", "callback_data": "menu"}],
    ]

# ─── ЭКРАНЫ ───────────────────────────────────────────────────────────────────

def screen_welcome(cid, name=""):
    """Экран приветствия для новых пользователей (показывается один раз)."""
    greeting = f"<b>{name}</b>, добро пожаловать!" if name else "Добро пожаловать!"
    text = (
        "━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "🤖  <b>CryptoBot Pro v5</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"👋 {greeting}\n\n"
        "Это <b>алгоритмический торговый бот</b>,\n"
        "который торгует криптовалютой вместо тебя.\n"
        "Без эмоций. Без усталости. 24/7.\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "⚡ <b>Как это работает:</b>\n\n"
        "  📊 Анализирует <b>BTC · ETH · SOL</b>\n"
        "       каждые 15 минут по 4 индикаторам\n\n"
        "  📈 Открывает сделки когда рынок\n"
        "       даёт чёткий сигнал — LONG или SHORT\n\n"
        "  🛡 Управляет рисками автоматически:\n"
        f"       стоп-лосс, тейк-профит, плечо {LEVERAGE}x\n\n"
        "  💰 Распределяет прибыль между\n"
        "       всеми участниками пула\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "🎮 <b>Тебе начислено $1,000 демо-баланса.</b>\n\n"
        "Наблюдай как бот торгует в реальном времени\n"
        "с реальными ценами Bybit — без риска потерять деньги.\n\n"
        "Когда будешь готов — пополни счёт и\n"
        "бот начнёт торговать <b>на твои реальные деньги</b>.\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "🚀 Нажми кнопку ниже чтобы начать:"
    )
    kb = [[{"text": "🚀 Начать торговлю", "callback_data": "menu"}]]
    send(cid, text, kb)


def screen_main(cid):
    try:
        user = get_user(cid)
        user["name"]     = user.get("name") or "Инвестор"
        user["last_seen"] = ts()
        save_user(cid, user)
        d     = user["demo"]
        r     = user["real"]

        locked      = d.get("locked", 0)
        total_demo  = d["balance"] + locked          # полный капитал пользователя
        d_pct       = pct_val(total_demo - d["start"], d["start"])
        r_pct       = pct_val(r["profit"], r["deposited"]) if r["deposited"] > 0 else 0.0

        total_pnl   = sum(s.get("pnl", 0) for s in BOT_STATES.values())
        total_trade = sum(s.get("n", 0) for s in BOT_STATES.values())   # закрытых сделок
        total_wins  = sum(s.get("wins", 0) for s in BOT_STATES.values())
        total_loss  = sum(s.get("loss", 0) for s in BOT_STATES.values())
        wr          = wr_calc(total_wins, total_loss)
        open_p      = active_positions()

        mode_str    = "🟢 <b>LIVE — Bybit Testnet</b>" if LIVE_MODE else "🎮 <b>DEMO (Симуляция)</b>"

        # Реальный баланс с Bybit если LIVE
        bybit_bal = ""
        if LIVE_MODE:
            bal = get_bybit_balance()
            if bal is not None:
                bybit_bal = f"\n💳 Bybit USDT: <b>${fmt(bal)}</b>"

        # Строка с демо-балансом: если есть заморозка — показываем куда ушли деньги
        if locked > 0:
            demo_line = (
                f"🎮 Демо:     <b>${fmt(d['balance'])}</b>  <code>{sign(d_pct)}{d_pct:.1f}%</code>\n"
                f"   🔒 В позициях: <b>${fmt(locked)}</b>  (всего ${fmt(total_demo)})\n"
            )
        else:
            demo_line = f"🎮 Демо:     <b>${fmt(d['balance'])}</b>  <code>{sign(d_pct)}{d_pct:.1f}%</code>\n"

        # Открытые позиции бота с плавающим P&L
        bot_pos_lines = ""
        unreal_pnl    = 0.0
        for pair in PAIRS:
            s   = BOT_STATES.get(pair["symbol"], {})
            pos = s.get("pos")
            if pos:
                pr = fetch_price(pair["symbol"])
                if pr:
                    fl = (pr - pos["entry"]) * pos["qty"] * LEVERAGE
                    if pos["side"] == "SHORT":
                        fl = -fl
                    unreal_pnl += fl
                    icon = "📈" if pos["side"] == "LONG" else "📉"
                    color = "+" if fl >= 0 else ""
                    bot_pos_lines += (
                        f"\n  {pair['emoji']} {pair['name']} {icon} {pos['side']}"
                        f" | Float: <code>{color}${fmt(abs(fl))}</code>"
                    )

        text = (
            f"🤖 <b>CryptoBot Pro v5</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"⚡ Режим: {mode_str}{bybit_bal}\n"
            f"📊 Стратегия: EMA21/50 + Supertrend + RSI/MACD\n"
            f"⏱ Таймфрейм: 4H + 1D фильтр | Плечо: {LEVERAGE}x\n\n"
            f"👋 Привет, <b>{user['name']}</b>!\n\n"
            f"{demo_line}"
            f"💼 Реальный: <b>${fmt(r['balance'])}</b>  <code>{sign(r_pct)}{r_pct:.1f}%</code>\n\n"
            f"📡 <b>Бот торгует сейчас:</b>\n"
            f"  Позиций открыто: {open_p}/{MAX_POS}\n"
        )
        if bot_pos_lines:
            text += f"{bot_pos_lines}\n"
            if unreal_pnl != 0:
                text += f"  Нереализованный P&L: <code>{sign(unreal_pnl)}${fmt(abs(unreal_pnl))}</code>\n"
        text += (
            f"\n  Закрыто сделок: {total_trade} (WR: {wr}%)\n"
            f"  Реализованный P&L: <code>{sign(total_pnl)}${fmt(abs(total_pnl))}</code>\n"
        )
        send(cid, text, kb_main())
    except Exception as e:
        logger.error("screen_main %s: %s", cid, e)
        send(cid, "⚠️ Ошибка. /start")


def screen_strategy(cid):
    # Получаем текущие данные по каждой паре для живого анализа
    pair_lines = ""
    for pair in PAIRS:
        try:
            df   = fetch_klines(pair["symbol"], "240", 80)
            if df is not None and len(df) >= 60:
                df    = calc_indicators(df)
                trend = get_daily_trend(pair["symbol"])
                sig   = get_signal(df, trend)
                c     = df.iloc[-1]
                st_ico = "🟢" if c["st_dir"] == 1 else "🔴"
                ema_ico = "📈" if c["ema_mid"] > c["ema_slow"] else "📉"
                trend_ico = "📈" if trend > 0 else ("📉" if trend < 0 else "➡️")
                sig_str = f"⚡ <b>{sig}</b>" if sig else "⏸ Нет сигнала"
                pair_lines += (
                    f"\n{pair['emoji']} <b>{pair['name']}</b> — {sig_str}\n"
                    f"   RSI: {c['rsi']:.0f} | ST: {st_ico} | EMA: {ema_ico} | 1D: {trend_ico}\n"
                )
        except Exception:
            pair_lines += f"\n{pair['emoji']} {pair['name']} — данные недоступны\n"

    text = (
        "📈 <b>Стратегия бота v5</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "Бот торгует <b>BTC, ETH, SOL</b> в оба направления.\n"
        "Он не живой, но анализирует 4 индикатора и\n"
        "принимает решение на основе <b>математики</b>, без эмоций.\n\n"
        "🟢 <b>LONG (ставка на рост)</b> — когда:\n"
        "  • EMA21 пересекла EMA50 снизу вверх\n"
        "  • Supertrend показывает бычий рынок\n"
        "  • RSI в зоне 38–72 (не перегрет)\n"
        "  • MACD-гистограмма начинает расти\n"
        "  → Нужно 3 из 4 + дневной тренд вверх\n\n"
        "🔴 <b>SHORT (ставка на падение)</b> — когда:\n"
        "  • EMA21 пересекла EMA50 сверху вниз\n"
        "  • Supertrend показывает медвежий рынок\n"
        "  • RSI в зоне 28–62 (без перепроданности)\n"
        "  • MACD-гистограмма начинает падать\n"
        "  → Нужно 3 из 4 + дневной тренд вниз\n\n"
        "⚙️ <b>Защита капитала:</b>\n"
        f"  • Риск: {RISK_PCT}% на сделку | Плечо: {LEVERAGE}x\n"
        f"  • Стоп-лосс: ATR×{ATR_SL_MULT} | Тейк-профит: ATR×{ATR_TP_MULT}\n"
        "  • Трейлинг-стоп — стоп двигается за ценой\n"
        f"  • Circuit-breaker: стоп при -{DAY_LOSS_PCT}% в день\n\n"
        "📊 <b>Сигналы прямо сейчас:</b>"
        f"{pair_lines}\n"
        "💡 Бот сам выбирает LONG или SHORT — смотрит на рынок\n"
        "каждые 15 минут. Сейчас рынок медвежий → входы SHORT.\n"
        "Когда рынок развернётся → будут LONG."
    )
    send(cid, text, kb_back())


def screen_stats(cid):
    trades  = all_trades()
    closes  = [t for t in trades if t.get("action") == "CLOSE"]
    wins    = [t for t in closes if t.get("pnl", 0) >= 0]
    losses  = [t for t in closes if t.get("pnl", 0) < 0]
    total_n = len(closes)
    total_pnl = sum(t.get("pnl", 0) for t in closes)
    win_avg = sum(t["pnl"] for t in wins)   / max(len(wins), 1)
    loss_avg= sum(t["pnl"] for t in losses) / max(len(losses), 1)
    wr      = wr_calc(len(wins), len(losses))
    rr      = abs(win_avg / loss_avg) if loss_avg != 0 else 0

    longs  = [t for t in closes if t.get("side") == "LONG"]
    shorts = [t for t in closes if t.get("side") == "SHORT"]

    pos_text = ""
    for pair in PAIRS:
        s   = BOT_STATES.get(pair["symbol"], {})
        pos = s.get("pos")
        if pos and (pr := fetch_price(pair["symbol"])):
            side = pos["side"]
            fl   = (pr - pos["entry"]) * pos["qty"] * LEVERAGE
            if side == "SHORT":
                fl = -fl
            pos_text += (
                f"\n{pair['emoji']} {pair['name']} {side}: вход ${fmt(pos['entry'])} | "
                f"Float: <code>{sign(fl)}${fmt(abs(fl))}</code>"
            )

    text = (
        f"📊 <b>Статистика бота</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"  Закрытых сделок:  {total_n}\n"
        f"  Long / Short:     {len(longs)} / {len(shorts)}\n"
        f"  Победных:         {len(wins)} ({wr}%)\n"
        f"  Убыточных:        {len(losses)}\n"
        f"  Суммарный P&L:    <code>{sign(total_pnl)}${fmt(abs(total_pnl))}</code>\n"
        f"  Средний выигрыш:  ${fmt(win_avg)}\n"
        f"  Средний убыток:   ${fmt(abs(loss_avg))}\n"
        f"  R:R:              {rr:.2f}\n\n"
        "<b>Баланс по парам:</b>\n"
    )
    for pair in PAIRS:
        s = BOT_STATES.get(pair["symbol"], {})
        text += f"  {pair['emoji']} {pair['name']}: ${fmt(s.get('usdt', 10000))}  ({s.get('wins',0)}W/{s.get('loss',0)}L)\n"

    if pos_text:
        text += f"\n<b>Открытые позиции:</b>{pos_text}"

    send(cid, text, kb_back())


def screen_market(cid):
    text = "🌐 <b>Рынок сейчас (Bybit Futures)</b>\n━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
    for pair in PAIRS:
        price = fetch_price(pair["symbol"])
        s     = BOT_STATES.get(pair["symbol"], {})
        pos   = s.get("pos")
        df4h  = fetch_klines(pair["symbol"], "240", 80)
        sig_txt = ""
        if df4h is not None and len(df4h) >= 60:
            df4h   = calc_indicators(df4h)
            trend  = get_daily_trend(pair["symbol"])
            sig    = get_signal(df4h, trend)
            c      = df4h.iloc[-1]
            rsi_v  = c["rsi"]
            st_ico = "🟢" if c["st_dir"] == 1 else "🔴"
            sig_txt = (
                f"\n  RSI: {rsi_v:.0f} | ST: {st_ico} | "
                f"EMA: {'📈' if c['ema_mid']>c['ema_slow'] else '📉'}"
            )
            if sig:
                sig_txt += f" | Сигнал: <b>{sig}</b>"

        pos_txt = ""
        if pos and price:
            side = pos["side"]
            fl   = (price - pos["entry"]) * pos["qty"] * LEVERAGE
            if side == "SHORT":
                fl = -fl
            fl_pct = fl / pos["margin"] * 100 if pos.get("margin") else 0
            pos_txt = (
                f"\n  📍 {side}: вход <b>${fmt(pos['entry'])}</b>"
                f"\n  💰 Float: <code>{sign(fl)}${fmt(abs(fl))} ({sign(fl_pct)}{fl_pct:.1f}%)</code>"
                f"\n  ⛔ SL: ${fmt(pos['sl'])} | 🎯 TP: ${fmt(pos['tp'])}"
            )

        price_txt = f"${fmt(price)}" if price else "нет данных"
        text += f"{pair['emoji']} <b>{pair['name']}</b>  {price_txt}{sig_txt}{pos_txt}\n\n"

    text += f"⏱ Анализ каждые 15 минут | Плечо {LEVERAGE}x"
    send(cid, text, kb_back())


def screen_history(cid):
    text = "📈 <b>Сделки бота</b>\n━━━━━━━━━━━━━━━━━━━━━━━━\n\n"

    # ── Открытые позиции бота прямо сейчас ──
    open_lines = ""
    for pair in PAIRS:
        s   = BOT_STATES.get(pair["symbol"], {})
        pos = s.get("pos")
        if not pos:
            continue
        price = fetch_price(pair["symbol"])
        if not price:
            continue
        side = pos["side"]
        fl   = (price - pos["entry"]) * pos["qty"] * LEVERAGE
        if side == "SHORT":
            fl = -fl
        icon      = "📈" if side == "LONG" else "📉"
        fl_icon   = "🟢" if fl >= 0 else "🔴"
        sl_dist   = abs(price - pos["sl"])
        tp_dist   = abs(price - pos["tp"])
        open_lines += (
            f"{icon} <b>{pair['name']}</b> {side} ×{LEVERAGE}x  [АКТИВНА]\n"
            f"  Вход:    ${fmt(pos['entry'])}\n"
            f"  Сейчас:  ${fmt(price)}\n"
            f"  SL:      ${fmt(pos['sl'])}  (до SL: ${fmt(sl_dist)})\n"
            f"  TP:      ${fmt(pos['tp'])}  (до TP: ${fmt(tp_dist)})\n"
            f"  Float:   {fl_icon} <code>{sign(fl)}${fmt(abs(fl))}</code>\n"
            f"  Открыта: {pos.get('time', pos.get('ts', '—'))}\n\n"
        )

    if open_lines:
        text += "⚡ <b>Открытые сейчас:</b>\n" + open_lines
    else:
        s_halted = any(BOT_STATES.get(p["symbol"], {}).get("halted") for p in PAIRS)
        if s_halted:
            text += "⏸ <b>Торговля приостановлена</b> (circuit-breaker)\n\n"
        else:
            text += "⏳ Нет открытых позиций — бот ищет сигнал...\n\n"

    # ── История закрытых сделок ──
    trades = all_trades()
    closes = [t for t in trades if t.get("action") == "CLOSE"][-10:]
    if closes:
        text += "📋 <b>Последние закрытые:</b>\n"
        for t in reversed(closes):
            pnl  = t.get("pnl", 0)
            side = t.get("side", "?")
            icon = "✅" if pnl >= 0 else "❌"
            text += (
                f"{icon} <b>{t.get('pair','?')}</b> {side}"
                f" → {t.get('reason','?')}\n"
                f"  ${fmt(t.get('entry',0))} → ${fmt(t.get('price',0))}"
                f"  <code>{sign(pnl)}${fmt(abs(pnl))}</code>"
                f" ({sign(t.get('pnl_pct',0))}{t.get('pnl_pct',0):.1f}%)\n"
                f"  {t.get('time','')}\n\n"
            )
    else:
        text += "📋 <b>Закрытых сделок пока нет.</b>\n"
        text += "История появится после первого закрытия позиции."

    send(cid, text, kb_back())


# ─── ДЕМО-ТОРГОВЛЯ — ЭКРАНЫ ───────────────────────────────────────────────────

def screen_demo_trade(cid):
    """Главный экран демо-торговли с пул-капиталом"""
    user   = get_user(cid)
    d      = user["demo"]
    locked = d.get("locked", 0)
    avail  = d.get("balance", 0)
    total  = avail + locked
    profit = d.get("profit", 0)
    pct    = pct_val(profit, d.get("start", 1000))
    wr     = wr_calc(d["wins"], d["loss"])

    # Подсчитать плавающий P&L бота (долю пользователя)
    total_locked_pool = sum(
        u["demo"].get("locked", 0) for u in load_users().values()
    )
    user_float = 0.0
    if locked > 0 and total_locked_pool > 0:
        user_share = locked / total_locked_pool
        for pair in PAIRS:
            s   = BOT_STATES.get(pair["symbol"], {})
            pos = s.get("pos")
            if pos:
                price = fetch_price(pair["symbol"])
                if price:
                    side = pos["side"]
                    fl   = (price - pos["entry"]) * pos["qty"] * LEVERAGE
                    if side == "SHORT":
                        fl = -fl
                    user_float += fl * user_share

    user_float = round(user_float, 4)

    text = (
        "🤖 <b>Бот управляет вашими деньгами</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"💰 Доступно:       <b>${fmt(avail)}</b>\n"
    )
    if locked > 0:
        fl_str = f"<code>{sign(user_float)}${fmt(abs(user_float))}</code>"
        text += (
            f"🔒 В торговле:     <b>${fmt(locked)}</b>\n"
            f"📊 Float P&L:      {fl_str}\n"
        )
    text += (
        f"💼 Итого капитал:  <b>${fmt(total)}</b>\n"
        f"📈 Всего прибыль:  <code>{sign(pct)}{pct:.1f}%</code>\n"
        f"📊 Сделок:  {d['trades']}  |  WR: {wr}%\n\n"
    )

    # Позиции бота с долей пользователя
    bot_pos_text = ""
    for pair in PAIRS:
        s   = BOT_STATES.get(pair["symbol"], {})
        pos = s.get("pos")
        if pos:
            price = fetch_price(pair["symbol"])
            if price:
                side = pos["side"]
                fl   = (price - pos["entry"]) * pos["qty"] * LEVERAGE
                if side == "SHORT":
                    fl = -fl
                direction = "📈" if side == "LONG" else "📉"
                # Доля пользователя в этой позиции
                u_share = (locked / total_locked_pool) if total_locked_pool > 0 and locked > 0 else 0
                u_fl    = round(fl * u_share, 4)
                bot_pos_text += (
                    f"  {direction} {pair['emoji']} <b>{pair['name']}</b> {side} ×{LEVERAGE}x\n"
                    f"     Вход: ${fmt(pos['entry'])} → Цена: ${fmt(price)}\n"
                    f"     Ваш Float: <code>{sign(u_fl)}${fmt(abs(u_fl))}</code> "
                    f"| Заморожено вашего: <b>${fmt(round(locked, 2))}</b>\n"
                )

    if bot_pos_text:
        text += f"🤖 <b>Активные позиции бота:</b>\n{bot_pos_text}\n"
    elif locked > 0:
        text += "🔄 Бот анализирует рынок...\n"
    else:
        text += "💤 Бот ждёт сигнала для входа\n"

    # Пользовательские позиции (ручные)
    poss = demo_get_positions(user)
    if poss:
        text += "\n📌 <b>Мои ручные позиции:</b>\n"
        for p in poss:
            fl     = demo_float_pnl(p)
            fl_str = f"<code>{sign(fl)}${fmt(abs(fl))}</code>" if fl is not None else "<i>загрузка...</i>"
            direction = "📈" if p["side"] == "LONG" else "📉"
            text += (
                f"  {direction} {p['emoji']} <b>{p['short']}</b> {p['side']}"
                f" × {p['lev']}x | Вход: ${fmt(p['entry'])} | P&L: {fl_str}\n"
            )

    send(cid, text, kb_demo_trade(poss))


def screen_demo_open_menu(cid):
    """Выбор направления: Long или Short"""
    text = (
        "📊 <b>Открыть позицию</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "📈 <b>LONG</b> — ставишь на рост\n"
        "   Зарабатываешь когда цена растёт\n\n"
        "📉 <b>SHORT</b> — ставишь на падение\n"
        "   Зарабатываешь когда цена падает\n\n"
        "Плечо: <b>2x</b>  |  Выбери направление:"
    )
    kb = [
        [{"text": "📈 LONG (рост)",    "callback_data": "demo_open_long"},
         {"text": "📉 SHORT (падение)", "callback_data": "demo_open_short"}],
        [{"text": "◀️ Назад", "callback_data": "demo_trade"}],
    ]
    send(cid, text, kb)


def screen_demo_select_coin(cid, side):
    """Выбор монеты для Long/Short"""
    side_ru  = "LONG 📈" if side == "long" else "SHORT 📉"
    text = (
        f"📊 <b>Открыть {side_ru}</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "Выбери монету:"
    )
    send(cid, text, kb_demo_coin_select(side))


def screen_demo_select_amount(cid, side, symbol):
    """Выбор суммы в USDT для позиции"""
    user  = get_user(cid)
    d     = user["demo"]
    coin  = demo_coin_by_symbol(symbol)
    if not coin:
        send(cid, "❌ Монета не найдена", kb_demo_back())
        return

    price, source = fetch_demo_price(symbol, coin.get("cg_id"))
    price_str = f"${fmt(price)} <i>({source})</i>" if price else "<i>нет данных</i>"
    side_ru   = "LONG 📈" if side == "long" else "SHORT 📉"

    text = (
        f"📊 <b>{coin['emoji']} {coin['name']} — {side_ru}</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"💲 Цена:    {price_str}\n"
        f"💳 Баланс:  <b>${fmt(d['balance'])}</b>\n"
        f"⚡ Плечо:   {DEMO_LEVERAGE}x\n\n"
        "Выбери сумму сделки (из баланса):"
    )
    send(cid, text, kb_demo_amount(symbol, side))


def screen_demo_positions(cid):
    """Экран открытых позиций с P&L и кнопками закрытия"""
    user  = get_user(cid)
    poss  = demo_get_positions(user)

    if not poss:
        send(cid, "📌 Открытых позиций нет.\n\nОткрой первую сделку!", kb_demo_back())
        return

    send(cid, "⏳ Загружаю текущие цены...")

    text = "📌 <b>Открытые позиции</b>\n━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
    for p in poss:
        fl    = demo_float_pnl(p)
        coin  = demo_coin_by_symbol(p["symbol"])
        cur_p, src = fetch_demo_price(p["symbol"], coin.get("cg_id") if coin else None)
        cur_str = f"${fmt(cur_p)}" if cur_p else "?"
        fl_str  = f"{sign(fl)}${fmt(abs(fl))}" if fl is not None else "?"
        chg_pct = ((cur_p / p["entry"]) - 1) * 100 if cur_p else 0
        direction = "📈" if p["side"] == "LONG" else "📉"
        text += (
            f"{direction} <b>{p['emoji']} {p['short']}</b> {p['side']} ×{p['lev']}x\n"
            f"   Вход:   ${fmt(p['entry'])}\n"
            f"   Сейчас: {cur_str}  ({sign(chg_pct)}{chg_pct:.2f}%)\n"
            f"   Сумма:  ${fmt(p['usdt'])} → эффект. ${fmt(p['usdt']*p['lev'])}\n"
            f"   P&L:    <b><code>{fl_str}</code></b>\n"
            f"   Дата:   {p['ts']}\n\n"
        )

    send(cid, text, kb_demo_positions(poss))


def screen_demo_history(cid):
    """История последних 10 закрытых демо-сделок"""
    user  = get_user(cid)
    d     = user["demo"]
    hist  = d.get("history", [])

    if not hist:
        send(cid, "📜 Историй сделок нет.\n\nОткрой и закрой первую сделку!", kb_demo_back())
        return

    total_pnl = sum(h["pnl"] for h in hist)
    text = (
        "📜 <b>История демо-сделок</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
    )
    for h in reversed(hist[-10:]):
        ico  = "✅" if h["pnl"] >= 0 else "❌"
        text += (
            f"{ico} <b>{h['symbol'][:3]}</b> {h['side']}  "
            f"<code>{sign(h['pnl'])}${fmt(abs(h['pnl']))}</code>\n"
            f"   {fmt(h['entry'])} → {fmt(h['exit'])}  ×{h['lev']}x  {h['ts']}\n"
        )
    text += f"\n💹 Итого: <b><code>{sign(total_pnl)}${fmt(abs(total_pnl))}</code></b>"
    send(cid, text, kb_demo_back())


def screen_account(cid):
    user  = get_user(cid)
    d     = user["demo"]
    r     = user["real"]
    d_pct = pct_val(d["balance"] - d["start"], d["start"])
    r_pct = pct_val(r["profit"], r["deposited"]) if r["deposited"] > 0 else 0.0
    text  = (
        "👤 <b>Мой аккаунт</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "🎮 <b>Демо-счёт</b>\n"
        f"  Баланс:  <b>${fmt(d['balance'])}</b>\n"
        f"  Прибыль: <code>{sign(d_pct)}{d_pct:.1f}%</code>\n"
        f"  Сделок:  {d['trades']} (WR: {wr_calc(d['wins'],d['loss'])}%)\n\n"
        "💼 <b>Реальный счёт</b>\n"
        f"  Внесено:  ${fmt(r['deposited'])}\n"
        f"  Баланс:   <b>${fmt(r['balance'])}</b>\n"
        f"  Прибыль:  <code>{sign(r_pct)}{r_pct:.1f}%</code>\n"
        f"  Сделок:   {r['trades']} (WR: {wr_calc(r['wins'],r['loss'])}%)\n"
        f"  Реинвест: {'✅' if r.get('autocompound', True) else '❌'}\n"
        f"  Статус:   {'✅ Активен' if r['active'] else '⏸ Неактивен'}\n\n"
        f"🤝 Рефералов: {user.get('ref_count',0)} | Реф.бонус: ${fmt(user.get('ref_bonus',0))}\n"
        f"📅 С нами с: {user['joined']}"
    )
    send(cid, text, kb_account())


def screen_deposit(cid):
    text = (
        f"💰 <b>Пополнение счёта</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"Кошелёк USDT (TRC-20):\n"
        f"<code>{WALLET}</code>\n\n"
        f"После отправки нажмите кнопку подтверждения.\n"
        f"Минимум: $50 | Зачисление: 10-30 мин"
    )
    send(cid, text, kb_deposit())


def screen_referral(cid):
    user = get_user(cid)
    code = user.get("ref_code", _gen_ref())
    if not user.get("ref_code"):
        user["ref_code"] = code
        save_user(cid, user)

    if BOT_USERNAME:
        ref_link = f"https://t.me/{BOT_USERNAME}?start={code}"
    else:
        ref_link = f"ваш_бот?start={code} (имя бота не определено — перезапустите)"

    count   = user.get("ref_count", 0)
    bonus   = user.get("ref_bonus", 0.0)
    ref_by  = user.get("ref_by")

    text = (
        "🤝 <b>Реферальная программа</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "За каждого приглашённого друга:\n"
        "  • <b>5% от его прибыли</b> — автоматически на ваш демо-счёт\n"
        "  • Другу — <b>+$50 к демо-балансу</b> при регистрации\n\n"
        f"📎 Ваша ссылка:\n"
        f"<code>{ref_link}</code>\n\n"
        f"👥 Приглашено: <b>{count}</b> чел.\n"
        f"💰 Заработано: <b>${fmt(bonus)}</b>\n"
        + (f"🔗 Вас пригласил: ID {ref_by}\n" if ref_by else "")
    )
    send(cid, text, kb_back())


def screen_mode_info(cid):
    if LIVE_MODE:
        send(cid, "⏳ Запрашиваю данные с Bybit...")
        bal = get_bybit_balance()

        # Открытые позиции
        pos_text = ""
        open_n   = 0
        for pair in PAIRS:
            s   = BOT_STATES.get(pair["symbol"], {})
            pos = s.get("pos")
            if pos:
                open_n += 1
                price = fetch_price(pair["symbol"]) or pos["entry"]
                side  = pos["side"]
                fl    = (price - pos["entry"]) * pos["qty"] * LEVERAGE
                if side == "SHORT":
                    fl = -fl
                pos_text += (
                    f"\n  {pair['emoji']} {pair['name']} <b>{side}</b>"
                    f" | Float: <code>{sign(fl)}${fmt(abs(fl))}</code>"
                )

        # Статистика бота
        trades    = all_trades()
        closes    = [t for t in trades if t.get("action") == "CLOSE"]
        total_pnl = sum(t.get("pnl", 0) for t in closes)
        wins      = sum(1 for t in closes if t.get("pnl", 0) >= 0)
        wr        = wr_calc(wins, len(closes) - wins)

        net_str = "🌐 Testnet" if USE_TESTNET else "🌐 Mainnet"
        bal_str = f"<b>${fmt(bal)}</b>" if bal is not None else "<i>ошибка API</i>"

        text = (
            "🟢 <b>LIVE режим — Bybit</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"🔑 API: ...{BYBIT_KEY[-6:] if BYBIT_KEY else '—'}  |  {net_str}\n"
            f"📊 Плечо: {LEVERAGE}x  |  Риск: {RISK_PCT}%/сделка\n\n"
            f"💳 <b>Баланс USDT: {bal_str}</b>\n\n"
            f"📌 Открытых позиций: {open_n}/{MAX_POS}"
        )
        if pos_text:
            text += f"\n{pos_text}"
        text += (
            f"\n\n📈 Закрытых сделок: {len(closes)}\n"
            f"🏆 Винрейт: {wr}%\n"
            f"💹 Суммарный P&L: <code>{sign(total_pnl)}${fmt(abs(total_pnl))}</code>"
        )

        if bal is None:
            text += (
                "\n\n⚠️ <b>Не удалось получить баланс.</b>\n"
                "Проверьте:\n"
                "• API ключ имеет права на чтение аккаунта\n"
                "• BYBIT_TESTNET = true (если используете testnet)\n"
                "• Аккаунт на testnet.bybit.com пополнен"
            )
    else:
        text = (
            "🎮 <b>Режим: DEMO (Симуляция)</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            "Реальные цены Bybit, виртуальные сделки.\n\n"
            "Чтобы включить реальную торговлю:\n"
            "1. Зайди на <b>testnet.bybit.com</b>\n"
            "2. API → создай ключ с правами Trade + Read\n"
            "3. В Railway добавь переменные:\n"
            "   BYBIT_API_KEY = ...\n"
            "   BYBIT_API_SECRET = ...\n"
            "   BYBIT_TESTNET = true\n"
            "4. Redeploy — и бот торгует реально!"
        )
    send(cid, text, kb_back())


def screen_leaderboard(cid):
    users  = load_users()
    actives = [(uid, u) for uid, u in users.items()
               if u["real"]["deposited"] > 0 or u["demo"]["trades"] > 0]
    actives.sort(key=lambda x: x[1]["real"]["profit"] + x[1]["demo"]["balance"] - 1000, reverse=True)
    text = "🏆 <b>Лидерборд</b>\n━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
    medals = ["🥇", "🥈", "🥉"]
    for i, (uid, u) in enumerate(actives[:10]):
        m    = medals[i] if i < 3 else f"{i+1}."
        pnl  = u["real"]["profit"] + (u["demo"]["balance"] - 1000)
        name = u.get("name") or f"Инвестор{uid[-4:]}"
        text += f"{m} <b>{name}</b> — <code>{sign(pnl)}${fmt(abs(pnl))}</code>\n"
    if not actives:
        text += "Пока никого нет. Будь первым! 🚀"
    send(cid, text, kb_back())

# ─── ADMIN ЭКРАНЫ ─────────────────────────────────────────────────────────────

def screen_admin(cid):
    users      = load_users()
    total_dep  = sum(u["real"]["deposited"] for u in users.values() if u["real"]["active"])
    total_users= len(users)
    trades     = all_trades()
    closes     = [t for t in trades if t.get("action") == "CLOSE"]
    total_pnl  = sum(t.get("pnl", 0) for t in closes)
    mode_lbl   = "🟢 LIVE Bybit" if LIVE_MODE else "🎮 DEMO"

    text = (
        f"🛠 <b>Админ-панель</b> [{mode_lbl}]\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"👥 Пользователей: {total_users}\n"
        f"💰 Всего внесено: ${fmt(total_dep)}\n"
        f"📊 Сделок закрыто: {len(closes)}\n"
        f"💹 Суммарный P&L: <code>{sign(total_pnl)}${fmt(abs(total_pnl))}</code>\n"
        f"📌 Открытых позиций: {active_positions()}/{MAX_POS}"
    )
    send(cid, text, kb_admin())

# ─── ОБРАБОТКА КОМАНД ─────────────────────────────────────────────────────────

PENDING_INPUTS = {}


def process_update(update):
    msg = update.get("message", {})
    cb  = update.get("callback_query", {})

    if msg:
        cid  = str(msg["chat"]["id"])
        text = msg.get("text", "")
        name = msg.get("from", {}).get("first_name", "")

        user = get_user(cid)
        if name and not user.get("name"):
            user["name"] = name
            save_user(cid, user)

        # Реферальный ввод
        if cid in PENDING_INPUTS:
            mode = PENDING_INPUTS.pop(cid)
            if mode == "ref_code":
                users = load_users()
                ref_uid = next((u for u, v in users.items() if v.get("ref_code") == text.strip()), None)
                if ref_uid and ref_uid != cid:
                    user = get_user(cid)
                    if not user.get("ref_by"):
                        user["ref_by"] = ref_uid
                        save_user(cid, user)
                        ref_user = users[ref_uid]
                        ref_user["ref_count"] = ref_user.get("ref_count", 0) + 1
                        save_user(ref_uid, ref_user)
                        send(cid, "✅ Реферальный код принят!")
                    else:
                        send(cid, "❌ Вы уже использовали реферальный код")
                else:
                    send(cid, "❌ Код не найден")
                return
            elif mode.startswith("dep_custom"):
                try:
                    amount = float(text.strip().replace("$",""))
                    if amount < 50:
                        send(cid, "❌ Минимальная сумма $50")
                        return
                    send(cid,
                         f"💳 Переведите <b>${fmt(amount)}</b> USDT (TRC-20):\n"
                         f"<code>{WALLET}</code>",
                         kb_confirm_dep(amount))
                except Exception:
                    send(cid, "❌ Введите корректную сумму")
                return
            elif mode.startswith("broadcast"):
                users = load_users()
                count = 0
                for uid in users:
                    try:
                        send(uid, f"📢 <b>Сообщение от администратора:</b>\n\n{text}")
                        count += 1
                    except Exception:
                        pass
                send(cid, f"✅ Рассылка отправлена {count} пользователям")
                return

        if text.startswith("/start"):
            parts = text.split()

            # Реферальная обработка
            if len(parts) > 1:
                ref = parts[1]
                all_u = load_users()
                ref_uid = next((u for u, v in all_u.items() if v.get("ref_code") == ref), None)
                if ref_uid and ref_uid != cid and not user.get("ref_by"):
                    user["ref_by"] = ref_uid
                    save_user(cid, user)
                    ref_user = get_user(ref_uid)
                    ref_user["ref_count"] = ref_user.get("ref_count", 0) + 1
                    save_user(ref_uid, ref_user)
                    send(ref_uid,
                         f"🎉 <b>По вашей ссылке зарегистрировался новый пользователь!</b>\n"
                         f"Вы получите 5% от его прибыли автоматически.")

            # Считаем пользователя "новым" если у него нет ни одной сделки
            # и welcome ещё не был показан — это защищает существующих пользователей
            has_history = (
                user.get("demo", {}).get("trades", 0) > 0 or
                user.get("real", {}).get("trades", 0) > 0 or
                abs(user.get("demo", {}).get("balance", 1000) - 1000) > 0.01
            )
            if not user.get("welcomed") and not has_history:
                # Реферальный бонус другу: +$50 к демо если пришёл по ссылке
                if user.get("ref_by"):
                    bonus_amount = 50.0
                    user["demo"]["balance"] = round(user["demo"].get("balance", 1000) + bonus_amount, 4)
                    user["demo"]["start"]   = user["demo"]["balance"]  # пересчитываем старт
                # Новый пользователь — показываем приветственный экран один раз
                user["welcomed"] = True
                save_user(cid, user)
                screen_welcome(cid, name=name)
            else:
                if not user.get("welcomed"):
                    user["welcomed"] = True   # молча ставим флаг для старых аккаунтов
                    save_user(cid, user)
                screen_main(cid)
        elif text == "/stats":
            screen_stats(cid)
        elif text == "/market":
            screen_market(cid)
        elif text == "/demo":
            screen_demo_trade(cid)
        elif text == "/balance":
            screen_mode_info(cid)
        elif text == "/debug":
            send(cid, "⏳ Проверяю подключение к Bybit...")
            info = bybit_debug_info()
            send(cid,
                 f"🔧 <b>Диагностика Bybit API</b>\n"
                 f"━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
                 f"<code>{info}</code>",
                 kb_back())
        elif text == "/admin" and is_admin(cid):
            screen_admin(cid)
        elif text == "/strategy":
            screen_strategy(cid)
        else:
            screen_main(cid)

    elif cb:
        cid   = str(cb["from"]["id"])
        data  = cb.get("data", "")
        cb_id = cb["id"]
        answer_cb(cb_id)

        # ── ДЕПОЗИТЫ: ПОДТВЕРЖДЕНИЕ/ОТКЛОНЕНИЕ (проверяем ПЕРВЫМИ, до общего dep_) ──

        if data.startswith("dep_ok_"):
            # Администратор подтверждает платёж
            if not is_admin(cid):
                answer_cb(cb_id, "⛔ Нет доступа")
                return
            try:
                _, _, uid, a_cents = data.split("_", 3)
                amount = int(a_cents) / 100
            except Exception:
                answer_cb(cb_id, "⚠️ Ошибка разбора данных")
                return
            user = get_user(uid)
            user["real"]["deposited"] += amount
            user["real"]["balance"]   += amount
            user["real"]["active"]     = True
            if user["real"]["balance"] > user["real"].get("peak", 0):
                user["real"]["peak"] = user["real"]["balance"]
            save_user(uid, user)
            remove_pending_deposit(uid)
            send(cid,
                 f"✅ <b>Платёж подтверждён!</b>\n"
                 f"Пользователь ID: <code>{uid}</code>\n"
                 f"Зачислено: <b>${fmt(amount)}</b>\n"
                 f"Новый баланс: <b>${fmt(user['real']['balance'])}</b>")
            send(uid,
                 f"✅ <b>Пополнение подтверждено!</b>\n"
                 f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
                 f"💵 Зачислено: <b>${fmt(amount)}</b>\n"
                 f"💳 Текущий баланс: <b>${fmt(user['real']['balance'])}</b>\n"
                 f"🚀 Деньги уже в работе — бот торгует на ваш счёт!\n"
                 f"🕐 {ts()}")

        elif data.startswith("dep_no_"):
            # Администратор отклоняет платёж
            if not is_admin(cid):
                answer_cb(cb_id, "⛔ Нет доступа")
                return
            try:
                _, _, uid, a_cents = data.split("_", 3)
                amount = int(a_cents) / 100
            except Exception:
                answer_cb(cb_id, "⚠️ Ошибка разбора данных")
                return
            remove_pending_deposit(uid)
            send(cid,
                 f"❌ <b>Платёж отклонён.</b>\n"
                 f"Пользователь ID: <code>{uid}</code> | Сумма: ${fmt(amount)}")
            send(uid,
                 f"❌ <b>Пополнение не подтверждено</b>\n"
                 f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
                 f"💵 Сумма: ${fmt(amount)}\n\n"
                 f"Платёж не найден или не прошёл. Если вы точно отправили — "
                 f"напишите в поддержку с чеком транзакции.")

        # ── ВЫБОР СУММЫ ДЕПОЗИТА (кнопки dep_50, dep_100 и т.п.) ──
        elif data.startswith("dep_") and data not in ("dep_custom",) \
                and not data.startswith(("dep_ok_", "dep_no_", "depsent_")):
            try:
                amount = int(data.split("_")[1])
            except (IndexError, ValueError):
                return
            send(cid,
                 f"💳 Переведите <b>${fmt(amount)}</b> USDT (TRC-20):\n"
                 f"<code>{WALLET}</code>",
                 kb_confirm_dep(amount))

        elif data == "dep_custom":
            PENDING_INPUTS[cid] = "dep_custom"
            send(cid, "✏️ Введите сумму пополнения (минимум $50):", kb_back())

        elif data.startswith("depsent_"):
            amount   = float(data.split("_")[1])
            username = cb["from"].get("username") or cb["from"].get("first_name", "?")
            if is_admin(cid):
                # Администратор подтверждает себе сам
                user = get_user(cid)
                user["real"]["deposited"] += amount
                user["real"]["balance"]   += amount
                user["real"]["active"]     = True
                if user["real"]["balance"] > user["real"].get("peak", 0):
                    user["real"]["peak"] = user["real"]["balance"]
                save_user(cid, user)
                send(cid, f"✅ Баланс пополнен на <b>${fmt(amount)}</b>!\n"
                          f"Текущий баланс: <b>${fmt(user['real']['balance'])}</b>")
            else:
                # Сохраняем заявку и отправляем КНОПКИ администратору
                add_pending_deposit(cid, amount, username)
                send(ADMIN_ID,
                     f"💰 <b>НОВЫЙ ЗАПРОС НА ПОПОЛНЕНИЕ</b>\n"
                     f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
                     f"👤 Пользователь: @{username} (ID: <code>{cid}</code>)\n"
                     f"💵 Сумма: <b>${fmt(amount)}</b>\n"
                     f"🕐 Время: {ts()}\n\n"
                     f"Проверьте поступление на кошелёк и нажмите кнопку:",
                     kb_admin_dep(cid, amount))
                send(cid,
                     f"⏳ <b>Заявка на пополнение отправлена!</b>\n"
                     f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
                     f"💵 Сумма: <b>${fmt(amount)}</b>\n"
                     f"🕐 Ожидайте подтверждения администратора (10-30 мин)\n\n"
                     f"Как только платёж придёт — деньги зачислятся автоматически ✅")

        elif data.startswith("withdraw"):
            user = get_user(cid)
            bal  = user["real"]["balance"]
            send(cid,
                 f"💸 <b>Вывод средств</b>\n\n"
                 f"Доступно: <b>${fmt(bal)}</b>\n"
                 f"Для вывода напишите: /withdraw [сумма] [адрес]\n"
                 f"Минимум $10 | Комиссия 1%",
                 kb_back())
        elif data == "menu":
            screen_main(cid)
        elif data == "account":
            screen_account(cid)
        elif data == "stats":
            screen_stats(cid)
        elif data == "market":
            screen_market(cid)
        elif data == "history":
            screen_history(cid)
        elif data == "deposit":
            screen_deposit(cid)
        elif data == "referral":
            screen_referral(cid)
        elif data == "strategy":
            screen_strategy(cid)
        elif data == "mode_info":
            screen_mode_info(cid)
        elif data == "leaderboard":
            screen_leaderboard(cid)
        # ── Демо-трейдинг ───────────────────────────────────────────────────────
        elif data == "demo_trade":
            screen_demo_trade(cid)

        elif data == "demo_open_menu":
            screen_demo_open_menu(cid)

        elif data == "demo_open_long":
            screen_demo_select_coin(cid, "long")

        elif data == "demo_open_short":
            screen_demo_select_coin(cid, "short")

        elif data.startswith("demo_long_") or data.startswith("demo_short_"):
            # demo_long_BTCUSDT  или  demo_short_ETHUSDT
            parts  = data.split("_", 2)          # ["demo","long","BTCUSDT"]
            side   = parts[1]
            symbol = parts[2]
            screen_demo_select_amount(cid, side, symbol)

        elif data.startswith("demo_exec_"):
            # demo_exec_long_BTCUSDT_50
            parts  = data.split("_")             # ["demo","exec","long","BTCUSDT","50"]
            side   = parts[2]                    # long / short
            symbol = parts[3]
            amount = float(parts[4])

            send(cid, f"⏳ Открываю {'LONG 📈' if side=='long' else 'SHORT 📉'} {symbol}...")
            user = get_user(cid)
            ok, result = demo_open_pos(user, symbol, side.upper(), amount)
            if ok:
                pos  = result
                coin = demo_coin_by_symbol(symbol)
                save_user(cid, user)
                send(cid,
                     f"✅ <b>Позиция открыта!</b>\n\n"
                     f"{pos['emoji']} <b>{pos['name']}</b>  {pos['side']} ×{pos['lev']}x\n"
                     f"💲 Цена входа: <b>${fmt(pos['entry'])}</b>\n"
                     f"💵 Сумма: ${fmt(pos['usdt'])} → эффективная ${fmt(pos['usdt']*pos['lev'])}\n"
                     f"📡 Источник цены: {pos['source']}\n\n"
                     f"💳 Остаток баланса: <b>${fmt(user['demo']['balance'])}</b>",
                     kb_demo_back())
            else:
                send(cid, f"❌ {result}", kb_demo_back())

        elif data == "demo_positions":
            screen_demo_positions(cid)

        elif data.startswith("demo_close_"):
            # demo_close_BTCUSDT
            symbol = data.replace("demo_close_", "")
            send(cid, f"⏳ Закрываю позицию {symbol}...")
            user = get_user(cid)
            ok, pnl_or_err, exit_price = demo_close_pos(user, symbol)
            if ok:
                coin = demo_coin_by_symbol(symbol)
                save_user(cid, user)
                ico  = "✅ Прибыль" if pnl_or_err >= 0 else "❌ Убыток"
                send(cid,
                     f"{ico}: <b><code>{sign(pnl_or_err)}${fmt(abs(pnl_or_err))}</code></b>\n\n"
                     f"{coin['emoji'] if coin else ''} {symbol[:3]} закрыт по ${fmt(exit_price)}\n"
                     f"💳 Баланс: <b>${fmt(user['demo']['balance'])}</b>",
                     kb_demo_back())
            else:
                send(cid, f"❌ {pnl_or_err}", kb_demo_back())

        elif data == "demo_history":
            screen_demo_history(cid)

        elif data == "demo_reset":
            user = get_user(cid)
            d    = user["demo"]
            # Если пользователь сбрасывает баланс пока деньги в пуле — снимаем их с пула
            locked_now = d.get("locked", 0)
            if locked_now > 0:
                # Убираем долю этого пользователя из locked_by_pair бота
                # (деньги просто списываем — позиция у бота останется, но баланс у этого юзера будет чистый)
                lbp = d.get("locked_by_pair", {})
                for pair_name, pair_locked in lbp.items():
                    # Корректируем пул других пользователей не трогаем — просто обнуляем у этого
                    pass  # pool_release обработает правильно когда позиция закроется
            # Сброс демо-счёта
            d["positions"]    = []
            d["balance"]      = 1000.0
            d["start"]        = 1000.0
            d["peak"]         = 1000.0
            d["profit"]       = 0.0
            d["trades"]       = 0
            d["wins"]         = 0
            d["loss"]         = 0
            d["history"]      = []
            d["streak_win"]   = 0
            d["streak_loss"]  = 0
            d["locked"]       = 0.0   # ← FIX: очищаем замороженные средства
            d["locked_by_pair"] = {}  # ← FIX: очищаем учёт по парам
            d["max_dd"]       = 0.0
            save_user(cid, user)
            send(cid, "🔄 <b>Демо-баланс сброшен!</b>\nСтартовый баланс: <b>$1,000</b>", kb_demo_back())

        # ── Старый быстрый просмотр демо-счёта ──────────────────────────────────
        elif data == "demo":
            screen_demo_trade(cid)

        elif data == "real":
            user = get_user(cid)
            r    = user["real"]
            pct  = pct_val(r["profit"], r["deposited"]) if r["deposited"] > 0 else 0.0
            send(cid,
                 f"💼 <b>Реальный счёт</b>\n"
                 f"Внесено:  ${fmt(r['deposited'])}\n"
                 f"Баланс:   <b>${fmt(r['balance'])}</b>\n"
                 f"Прибыль:  <code>{sign(pct)}{pct:.1f}%</code>\n"
                 f"Сделок:   {r['trades']}", kb_back())
        elif data == "my_stats":
            user = get_user(cid)
            d    = user["demo"]
            r    = user["real"]
            send(cid,
                 f"📊 <b>Моя статистика</b>\n"
                 f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
                 f"🎮 Демо-сделок: {d['trades']} | WR: {wr_calc(d['wins'],d['loss'])}%\n"
                 f"💼 Реал-сделок: {r['trades']} | WR: {wr_calc(r['wins'],r['loss'])}%\n"
                 f"🤝 Рефералов:  {user.get('ref_count',0)}\n"
                 f"💎 Реф.бонус:  ${fmt(user.get('ref_bonus',0))}", kb_back())
        elif data == "toggle_notify":
            user = get_user(cid)
            user["notify"] = not user.get("notify", True)
            save_user(cid, user)
            state = "включены 🔔" if user["notify"] else "выключены 🔕"
            send(cid, f"Уведомления {state}", kb_back())
        elif data.startswith("adm_") and is_admin(cid):
            handle_admin_cb(cid, data)


def handle_admin_cb(cid, data):
    if data == "adm_users":
        users = load_users()
        text  = f"👥 <b>Пользователи ({len(users)})</b>\n\n"
        for uid, u in list(users.items())[:15]:
            r = u["real"]
            text += f"• {uid} | Деп: ${fmt(r['deposited'])} | {'✅' if r['active'] else '⏸'}\n"
        send(cid, text, kb_back())
    elif data == "adm_stats":
        screen_admin(cid)
    elif data == "adm_deposits":
        users  = load_users()
        total  = sum(u["real"]["deposited"] for u in users.values())
        active = sum(1 for u in users.values() if u["real"]["active"])
        send(cid, f"💰 Депозиты\nВсего внесено: ${fmt(total)}\nАктивных: {active}", kb_back())
    elif data == "adm_trades":
        trades = all_trades()
        closes = [t for t in trades if t.get("action") == "CLOSE"][-10:]
        text   = "📋 <b>Последние сделки</b>\n\n"
        for t in reversed(closes):
            pnl  = t.get("pnl", 0)
            icon = "✅" if pnl >= 0 else "❌"
            text += f"{icon} {t.get('pair')} {t.get('side')} {sign(pnl)}${fmt(abs(pnl))}\n"
        send(cid, text or "Нет сделок", kb_back())
    elif data == "adm_broadcast":
        PENDING_INPUTS[cid] = "broadcast"
        send(cid, "✏️ Введите текст рассылки:", kb_back())
    elif data == "adm_reset_cb":
        if not is_admin(cid):
            return
        reset_lines = []
        for pair in PAIRS:
            s = BOT_STATES.get(pair["symbol"], {})
            if s.get("halted"):
                s["halted"]      = False
                s["halt_until"]  = 0
                s["day_start"]   = _true_equity(s)
                BOT_STATES[pair["symbol"]] = s
                save_bot_state(pair["symbol"], s)
                reset_lines.append(f"  ✅ {pair['name']}: circuit-breaker снят")
            else:
                reset_lines.append(f"  ℹ️ {pair['name']}: не был остановлен")
        result_text = "\n".join(reset_lines) if reset_lines else "Нет остановленных пар"
        send(cid,
             f"🔄 <b>Сброс Circuit Breaker</b>\n"
             f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
             f"{result_text}",
             kb_admin())

# ─── ГЛАВНЫЙ ТОРГОВЫЙ ЦИКЛ ────────────────────────────────────────────────────

def trading_loop():
    """Основной цикл торговли — анализирует рынок каждые 15 минут"""
    logger.info("=" * 60)
    logger.info("CryptoBot Pro v5 — Торговый цикл запущен")
    logger.info(f"Режим:     {'LIVE (Bybit)' if LIVE_MODE else 'DEMO (Симуляция)'}")
    logger.info(f"Testnet:   {USE_TESTNET}")
    logger.info(f"Плечо:     {LEVERAGE}x")
    logger.info(f"Пар:       {len(PAIRS)}")
    logger.info("=" * 60)

    # Инициализируем состояния
    for pair in PAIRS:
        BOT_STATES[pair["symbol"]] = load_bot_state(pair["symbol"])

    last_trade       = 0
    last_report      = 0
    last_daily_users = 0   # ежедневный личный отчёт пользователям
    last_sl_check    = 0
    check_num        = 0

    while True:
        try:
            now = time.time()

            # Проверка SL/TP каждые 15 минут (без индикаторов — только по цене)
            if now - last_sl_check >= SL_CHECK_INT:
                last_sl_check = now
                for pair in PAIRS:
                    if BOT_STATES.get(pair["symbol"], {}).get("pos"):
                        try:
                            price = fetch_price(pair["symbol"])
                            if price:
                                check_exits(pair, price, None)
                        except Exception as e:
                            logger.error("SL/TP check %s: %s", pair["symbol"], e)

            # Торговый анализ каждые 15 минут
            if now - last_trade >= TRADE_INT:
                last_trade = now
                check_num += 1
                logger.info("🔍 Анализ рынка #%d...", check_num)

                scan_lines = []   # для сводного уведомления пользователям

                for pair in PAIRS:
                    sym = pair["symbol"]
                    try:
                        if circuit_breaker(sym):
                            scan_lines.append(f"  {pair['emoji']} {pair['name']}: ⛔ circuit-breaker")
                            continue

                        s     = BOT_STATES[sym]
                        price = fetch_price(sym)
                        if price is None:
                            continue

                        # Загрузить 4H свечи и рассчитать индикаторы
                        df4h = fetch_klines(sym, "240", 200)
                        if df4h is None or len(df4h) < 60:
                            logger.warning("%s: недостаточно данных", sym)
                            continue
                        df4h = calc_indicators(df4h)

                        # Проверить выходы из текущей позиции
                        if s.get("pos"):
                            exited = check_exits(pair, price, df4h)
                            if exited:
                                continue

                        # Искать новый сигнал
                        trend_1d = get_daily_trend(sym)
                        sig      = get_signal(df4h, trend_1d)
                        c        = df4h.iloc[-1]
                        atr      = c["atr"]

                        if sig and not s.get("pos"):
                            t = do_open(pair, price, atr, sig)
                            if t:
                                icon = "📈" if sig == "LONG" else "📉"
                                logger.info("%s %s @ $%.2f  SL=%.2f  TP=%.2f",
                                            sym, sig, price, t["sl"], t["tp"])
                                open_msg = (
                                     f"{icon} <b>БОТ ОТКРЫЛ {sig} | {pair['emoji']} {pair['name']}</b>\n"
                                     f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
                                     f"💵 Цена входа:  <b>${fmt(price)}</b>\n"
                                     f"⛔ Стоп-лосс:   <b>${fmt(t['sl'])}</b>\n"
                                     f"🎯 Тейк-профит: <b>${fmt(t['tp'])}</b>\n"
                                     f"📊 RSI: {c['rsi']:.0f} | "
                                     f"ST: {'🟢 Бычий' if c['st_dir']==1 else '🔴 Медвежий'}\n"
                                     f"📅 1D тренд: {'📈 Бычий' if trend_1d>0 else '📉 Медвежий' if trend_1d<0 else '↔️ Нейтральный'}\n"
                                     f"⚖️ Риск: {RISK_PCT}% | Плечо: {LEVERAGE}x | Demo режим\n"
                                     f"🕐 {ts()}"
                                )
                                notify_all_users(open_msg)
                                scan_lines.append(f"  {pair['emoji']} {pair['name']}: {icon} ВХОД {sig} @ ${fmt(price)}")
                        else:
                            pos = s.get("pos")
                            if pos:
                                side  = pos["side"]
                                fl    = (price - pos["entry"]) * pos["qty"] * LEVERAGE
                                if side == "SHORT":
                                    fl = -fl
                                icon_pos = "📈" if side == "LONG" else "📉"
                                scan_lines.append(
                                    f"  {pair['emoji']} {pair['name']}: {icon_pos} {side} открыт "
                                    f"| Float: {sign(fl)}${fmt(abs(fl))}"
                                )
                            else:
                                trend_lbl = "📈" if trend_1d > 0 else "📉" if trend_1d < 0 else "↔️"
                                macd_up = c["macd_h"] > df4h.iloc[-2]["macd_h"]
                                long_s  = sum([c["ema_mid"]>c["ema_slow"], c["st_dir"]==1,
                                               RSI_LONG_MIN<=c["rsi"]<=RSI_LONG_MAX, macd_up])
                                scan_lines.append(
                                    f"  {pair['emoji']} {pair['name']}: ${fmt(price)} | "
                                    f"RSI {c['rsi']:.0f} | ST {'🟢' if c['st_dir']==1 else '🔴'} | "
                                    f"1D {trend_lbl} | нет сигнала"
                                )
                                logger.info("%s нет сигнала | RSI=%.0f ST=%d Trend1D=%s",
                                            sym, c["rsi"], c["st_dir"], trend_lbl)

                    except Exception as e:
                        logger.error("Анализ %s: %s", sym, e)
                        scan_lines.append(f"  {pair['emoji']} {pair['name']}: ⚠️ ошибка")

                    time.sleep(2)

                # Каждые 4 итерации (1 час) — сводка рынка всем пользователям
                if check_num % 4 == 0 and scan_lines:
                    active = sum(1 for s in BOT_STATES.values() if s.get("pos"))
                    mode   = "🔴 LIVE" if LIVE_MODE else "🟡 DEMO"
                    scan_text = (
                        f"🤖 <b>Анализ рынка #{check_num}</b>  {mode}\n"
                        f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
                        f"{''.join(l + chr(10) for l in scan_lines)}"
                        f"\n📌 Позиций открыто: {active}/{MAX_POS}\n"
                        f"🕐 {ts()}"
                    )
                    notify_all_users(scan_text)

                # Каждые 96 итераций (~1 день при 15-мин цикле) — детальная сводка
                if check_num % 96 == 0:
                    screen_stats(ADMIN_ID)

            # Еженедельный отчёт
            if now - last_report >= 86400 * 7:
                last_report = now
                trades    = all_trades()
                closes    = [t for t in trades if t.get("action") == "CLOSE"]
                # Фильтруем только сделки за последние 7 дней
                week = [t for t in closes
                        if t.get("time", "")[:10] >= datetime.fromtimestamp(now - 86400*7,
                           timezone.utc).strftime("%Y-%m-%d")]
                if not week:
                    week = closes[-50:]   # фолбэк: если нет дат — берём последние 50
                pnl_w   = sum(t.get("pnl", 0) for t in week)
                wins_w  = sum(1 for t in week if t.get("pnl", 0) >= 0)
                total_w = len(week)
                total_eq = sum(s.get("usdt", 0) + (s.get("pos", {}) or {}).get("margin", 0)
                               for s in BOT_STATES.values())
                send(ADMIN_ID,
                     f"📅 <b>Еженедельный отчёт</b>\n"
                     f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
                     f"Сделок за неделю: {total_w}\n"
                     f"WR: {wr_calc(wins_w, total_w - wins_w)}%\n"
                     f"P&L: <code>{sign(pnl_w)}${fmt(abs(pnl_w))}</code>\n"
                     f"Капитал бота: ${fmt(total_eq)}\n"
                     f"🕐 {ts()}")

            # Ежедневный личный отчёт каждому пользователю (раз в 24 часа)
            if now - last_daily_users >= 86400:
                last_daily_users = now
                all_u      = load_users()
                active_pos = active_positions()
                for uid, u in all_u.items():
                    if not u.get("notify", True):
                        continue
                    d = u.get("demo", {})
                    bal    = d.get("balance", 0) + d.get("locked", 0)
                    start  = d.get("start", 1000)
                    profit = d.get("profit", 0)
                    trades = d.get("trades", 0)
                    wins   = d.get("wins", 0)
                    wr     = wr_calc(wins, trades - wins)
                    pct    = pct_val(bal - start, start)
                    trend  = "📈" if profit >= 0 else "📉"
                    try:
                        send(uid,
                             f"📅 <b>Ваш ежедневный отчёт</b>\n"
                             f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
                             f"💰 Демо-баланс:  <b>${fmt(bal)}</b>  "
                             f"({sign(pct)}{pct:.1f}%)\n"
                             f"📊 Сделок всего: <b>{trades}</b> | "
                             f"WR: <b>{wr}%</b>\n"
                             f"💹 Суммарный P&L: <code>{sign(profit)}${fmt(abs(profit))}</code>\n"
                             f"📌 Открытых позиций: <b>{active_pos}/{MAX_POS}</b>\n\n"
                             f"{trend} Бот работает 24/7. Следующие сделки уже на подходе.\n"
                             f"🕐 {ts()}")
                    except Exception:
                        pass

        except KeyboardInterrupt:
            logger.info("Остановка торгового цикла...")
            break
        except Exception as e:
            logger.error("Главный цикл: %s", e)
            time.sleep(30)

        time.sleep(CMD_INT)

# ─── TELEGRAM POLLING ─────────────────────────────────────────────────────────

def poll_telegram():
    """Telegram long-polling в отдельном потоке.

    Важно: Telegram держит соединение TG_POLL_TIMEOUT секунд ожидая обновления.
    requests timeout должен быть БОЛЬШЕ чем TG_POLL_TIMEOUT, иначе Read timed out.
    """
    import threading
    TG_POLL_TIMEOUT = 25          # Telegram держит соединение N секунд
    REQ_TIMEOUT     = TG_POLL_TIMEOUT + 10   # requests ждёт чуть дольше
    offset = 0

    def _poll():
        nonlocal offset
        logger.info("Telegram polling запущен (poll=%ds, req_timeout=%ds)...",
                    TG_POLL_TIMEOUT, REQ_TIMEOUT)
        while True:
            try:
                upds = api("getUpdates",
                           {"offset": offset, "timeout": TG_POLL_TIMEOUT, "limit": 10},
                           _timeout=REQ_TIMEOUT)
                for u in upds.get("result", []):
                    offset = u["update_id"] + 1
                    try:
                        process_update(u)
                    except Exception as e:
                        logger.error("process_update: %s", e)
            except Exception as e:
                logger.warning("polling loop: %s — retry in 5s", e)
                time.sleep(5)

    t = threading.Thread(target=_poll, daemon=True)
    t.start()
    return t

# ─── ТОЧКА ВХОДА ──────────────────────────────────────────────────────────────

def run():
    if not TOKEN:
        logger.warning("TELEGRAM_BOT_TOKEN не задан — Telegram отключён")
    if not ADMIN_ID:
        logger.warning("TELEGRAM_CHAT_ID не задан")
    if LIVE_MODE:
        logger.info("🟢 LIVE режим: Bybit %s", "Testnet" if USE_TESTNET else "Mainnet")
        bal = get_bybit_balance()
        if bal is not None:
            logger.info("💳 Баланс Bybit USDT: $%.2f", bal)
        else:
            logger.warning("Не удалось получить баланс с Bybit — проверьте API ключи")
    else:
        logger.info("🎮 DEMO режим — торговля симулируется")

    if TOKEN:
        # Получаем имя бота для реферальных ссылок
        global BOT_USERNAME
        me = api("getMe")
        if me.get("ok"):
            BOT_USERNAME = me["result"].get("username", "")
            logger.info("🤖 Бот: @%s", BOT_USERNAME)

        poll_telegram()
        if ADMIN_ID:
            # Автоматически регистрируем администратора, если ещё нет в базе
            # Это гарантирует получение всех уведомлений без ручного /start
            get_user(ADMIN_ID)
            logger.info("✅ Администратор %s зарегистрирован в базе пользователей", ADMIN_ID)

            # Проверяем ожидающие подтверждения депозиты
            pending = load_pending_deposits()
            if pending:
                lines = "\n".join(
                    f"  • ID {uid}: ${fmt(d['amount'])} от @{d.get('username','?')} ({d.get('time','')})"
                    for uid, d in pending.items()
                )
                send(ADMIN_ID,
                     f"⏳ <b>Ожидают подтверждения {len(pending)} депозит(а):</b>\n{lines}\n\n"
                     f"Чтобы подтвердить — попросите пользователя нажать «Я отправил платёж» ещё раз, "
                     f"или зайдите в Депозиты в админ-панели.")

            mode_lbl = f"🟢 LIVE Bybit ({'Testnet' if USE_TESTNET else 'Mainnet'})" if LIVE_MODE else "🎮 DEMO"
            send(ADMIN_ID,
                 f"🚀 <b>CryptoBot Pro v5 запущен!</b>\n"
                 f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
                 f"⚡ Режим: {mode_lbl}\n"
                 f"📊 Стратегия: EMA21/50 + Supertrend + RSI/MACD\n"
                 f"⏱ Таймфрейм: 4H + 1D фильтр\n"
                 f"🎯 Цель: 70-100% годовых\n"
                 f"🕐 {ts()}")

    trading_loop()


if __name__ == "__main__":
    run()