# Grid Trading Bot Configuration

# Trading Pairs
trading_pairs = [
    'BTC',
    'ETH',
    'SOL',
]

# Grid Parameters
levels = 10  # Number of grid levels
range = {
    'min': 10000,  # Minimum price
    'max': 50000   # Maximum price
}

# Balance Settings
balance_settings = {
    'base_currency': 'USDT',
    'investment_amount': 1000,  # Amount to invest per grid
    'max_investment': 10000   # Maximum amount to invest
}

# Telegram Configuration
telegram_config = {
    'bot_token': 'YOUR_TELEGRAM_BOT_TOKEN',
    'chat_id': 'YOUR_CHAT_ID',
}

# Analytics Settings
analytics_settings = {
    'enabled': True,
    'log_file': 'analytics.log',  # Log file path
}

# Other Important Parameters
other_settings = {
    'trading_fee': 0.001,  # Trading fee as a decimal
    'timeframe': '1h',  # Chart timeframe
}