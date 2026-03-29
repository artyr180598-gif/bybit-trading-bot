# CryptoBot Pro v5

Telegram-бот для торговли на Bybit с поддержкой демо-режима.

## Стратегия

EMA Crossover + Supertrend + RSI + MACD (4H + 1D)

## Переменные окружения

| Переменная | Описание |
|---|---|
| `TELEGRAM_BOT_TOKEN` | Токен Telegram бота |
| `TELEGRAM_CHAT_ID` | ID чата администратора |
| `ADMIN_IDS` | ID администраторов (через запятую) |
| `USDT_WALLET` | Кошелёк USDT TRC-20 для пополнений |
| `BYBIT_API_KEY` | API ключ Bybit (для LIVE режима) |
| `BYBIT_API_SECRET` | API секрет Bybit (для LIVE режима) |
| `BYBIT_TESTNET` | `true` = тестовая сеть, `false` = основная |
| `BYBIT_LEVERAGE` | Плечо (по умолчанию: 3) |

## Запуск

```bash
pip install -r requirements.txt
python bot.py
```

## Режимы

- **DEMO** — симуляция с реальными ценами (без API ключей)
- **LIVE** — реальная торговля через Bybit Testnet/Mainnet
