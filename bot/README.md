# CryptoBot Pro v5

Telegram-бот для торговли на Bybit с поддержкой демо-режима.

## Стратегия

Адаптивный подбор профиля стратегии на последних 365 днях истории:

- `balanced` (базовый трендовый профиль)
- `momentum` (пробой и ускорение)
- `mean_reversion` (контртренд во флэте)
- `defensive` (повышенная фильтрация шума)

Бот при старте прогоняет бэктест профилей и выбирает лучший по риск-скорингу
(`доходность - штраф за просадку + бонус за Sharpe/WR`), после чего торгует выбранным профилем.

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
| `AUTO_STRATEGY_OPT` | `true` = авто-оптимизация профиля при старте |
| `STRATEGY_PROFILE` | Профиль по умолчанию (`balanced`, `momentum`, `mean_reversion`, `defensive`) |

## Запуск

```bash
pip install -r requirements.txt
python bot.py
```

## Режимы

- **DEMO** — симуляция с реальными ценами (без API ключей)
- **LIVE** — реальная торговля через Bybit Testnet/Mainnet
