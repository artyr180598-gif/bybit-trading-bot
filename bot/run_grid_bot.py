#!/usr/bin/env python
# -*- coding: utf-8 -*-
import os
import json
import time
import logging
import requests
import pandas as pd
import numpy as np
from datetime import datetime, timezone
from pathlib import Path
import threading
import sys

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

PAIRS = [
    {"symbol": "BTCUSDT", "yahoo": "BTC-USD", "name": "BTC", "grid_range": 2.0},
    {"symbol": "ETHUSDT", "yahoo": "ETH-USD", "name": "ETH", "grid_range": 2.5},
    {"symbol": "SOLUSDT", "yahoo": "SOL-USD", "name": "SOL", "grid_range": 3.0},
]

INITIAL_BALANCE = 10000.0
GRID_LEVELS = 20
POSITION_SIZE_PERCENT = 0.5
CHECK_INTERVAL = 300
PAPER_TRADE = True

logger.info("=" * 80)
logger.info("🤖 GRID TRADING BOT v3.0 - NEW DEPLOYMENT STARTED")
logger.info("=" * 80)

class DatabaseManager:
    def __init__(self, db_dir="data"):
        self.db_dir = Path(db_dir)
        self.db_dir.mkdir(parents=True, exist_ok=True)
        
    def save_trade(self, pair, trade_data):
        trades_file = self.db_dir / f"{pair}_trades.jsonl"
        with open(trades_file, "a") as f:
            f.write(json.dumps(trade_data, default=str) + "\n")
    
    def load_trades(self, pair):
        trades_file = self.db_dir / f"{pair}_trades.jsonl"
        trades = []
        if trades_file.exists():
            with open(trades_file, "r") as f:
                for line in f:
                    if line.strip():
                        try:
                            trades.append(json.loads(line))
                        except:
                            pass
        return trades
    
    def save_state(self, pair, state):
        state_file = self.db_dir / f"{pair}_state.json"
        with open(state_file, "w") as f:
            json.dump(state, f, indent=2, default=str)
    
    def load_state(self, pair):
        state_file = self.db_dir / f"{pair}_state.json"
        if state_file.exists():
            with open(state_file, "r") as f:
                return json.load(f)
        return None

class AnalyticsEngine:
    @staticmethod
    def calculate_metrics(trades):
        if not trades:
            return {}
        try:
            df = pd.DataFrame(trades)
            total_trades = len(df)
            winning_trades = len(df[df['pnl'] > 0])
            losing_trades = len(df[df['pnl'] < 0])
            win_rate = (winning_trades / total_trades * 100) if total_trades > 0 else 0
            total_pnl = df['pnl'].sum()
            total_pnl_percent = df['pnl_percent'].sum()
            
            return {
                'total_trades': int(total_trades),
                'winning_trades': int(winning_trades),
                'losing_trades': int(losing_trades),
                'win_rate': float(win_rate),
                'total_pnl': float(total_pnl),
                'total_pnl_percent': float(total_pnl_percent),
            }
        except:
            return {}

class TelegramNotifier:
    def __init__(self, token, chat_id):
        self.token = token
        self.chat_id = chat_id
        self.api_url = f"https://api.telegram.org/bot{token}"
    
    def send_message(self, text):
        if not self.token or not self.chat_id:
            return False
        try:
            requests.post(
                f"{self.api_url}/sendMessage",
                json={"chat_id": self.chat_id, "text": text, "parse_mode": "HTML"},
                timeout=10
            )
            return True
        except Exception as e:
            logger.error(f"Telegram error: {e}")
            return False
    
    def send_trade_notification(self, pair, trade_data):
        side = trade_data['side']
        emoji = "🟢" if side == "BUY" else "🔴"
        pnl_text = ""
        if side == "SELL":
            pnl = trade_data.get('pnl', 0)
            pnl_pct = trade_data.get('pnl_percent', 0)
            pnl_text = f"\n✅ <b>PnL: ${pnl:.2f} ({pnl_pct:.2f}%)</b>"
        
        message = f"""{emoji} <b>{side} - {pair}</b>
━━━━━━━━━━━━━━━━━
💰 ${trade_data['price']:.4f}{pnl_text}
🎫 {trade_data.get('trade_id', 'N/A')}"""
        self.send_message(message)
    
    def send_daily_report(self, pair, metrics):
        message = f"""📊 <b>Daily Report - {pair}</b>
━━━━━━━━━━━━━━━━━
📈 Trades: {metrics.get('total_trades', 0)}
✅ Wins: {metrics.get('winning_trades', 0)} ❌ Losses: {metrics.get('losing_trades', 0)}
📊 Win Rate: {metrics.get('win_rate', 0):.1f}%
💰 PnL: ${metrics.get('total_pnl', 0):.2f} ({metrics.get('total_pnl_percent', 0):.2f}%)"""
        self.send_message(message)

def get_price(sym):
    try:
        url = "https://query1.finance.yahoo.com/v8/finance/chart/" + sym
        r = requests.get(url, params={"interval": "1m", "range": "1d"}, headers={"User-Agent": "Mozilla/5.0"}, timeout=15)
        closes = r.json()["chart"]["result"][0]["indicators"]["quote"][0]["close"]
        p = next((c for c in reversed(closes) if c is not None), None)
        return float(p) if p else None
    except:
        return None

class GridTradingEngine:
    def __init__(self, pair_config):
        self.pair = pair_config['name']
        self.symbol = pair_config['symbol']
        self.grid_range = pair_config.get('grid_range', 2.0)
        self.grid_levels = GRID_LEVELS
        self.db = DatabaseManager()
        self.notifier = TelegramNotifier(TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID)
        self.balance = INITIAL_BALANCE
        self.open_positions = []
        self.grid = []
        self._load_state()
    
    def _load_state(self):
        state = self.db.load_state(self.symbol)
        if state:
            self.balance = state.get('balance', INITIAL_BALANCE)
            self.open_positions = state.get('open_positions', [])
            self.grid = state.get('grid', [])
            logger.info(f"📂 {self.pair}: Loaded - Balance: ${self.balance:.2f}")
        else:
            logger.info(f"✨ {self.pair}: Fresh - Balance: ${self.balance:.2f}")
    
    def _save_state(self):
        state = {
            'timestamp': datetime.now(timezone.utc).isoformat(),
            'balance': self.balance,
            'open_positions': self.open_positions,
            'grid': self.grid,
        }
        self.db.save_state(self.symbol, state)
    
    def create_grid(self, price):
        if price <= 0:
            return []
        lower = price * (1 - self.grid_range / 100)
        upper = price * (1 + self.grid_range / 100)
        grid = []
        for i in range(self.grid_levels):
            level = lower + (upper - lower) * i / (self.grid_levels - 1)
            grid.append({'level': i, 'price': round(level, 4), 'status': 'pending'})
        self.grid = grid
        logger.info(f"🔲 {self.pair}: Grid ${lower:.2f} - ${upper:.2f}")
        return grid
    
    def process_price(self, price):
        signals = []
        for grid_point in self.grid:
            if price <= grid_point['price'] and grid_point['status'] == 'pending':
                signal = self._create_buy_signal(price, grid_point)
                if signal:
                    signals.append(signal)
                grid_point['status'] = 'bought'
            elif grid_point['status'] == 'bought' and price >= grid_point['price']:
                signal = self._create_sell_signal(price, grid_point)
                if signal:
                    signals.append(signal)
                grid_point['status'] = 'sold'
        return signals
    
    def _create_buy_signal(self, price, grid_point):
        position_size = (self.balance * POSITION_SIZE_PERCENT) / 100 / price
        cost = position_size * price
        if cost > self.balance:
            return None
        trade = {
            'timestamp': datetime.now(timezone.utc).isoformat(),
            'side': 'BUY',
            'price': price,
            'quantity': position_size,
            'grid_level': grid_point['level'],
            'pnl': 0,
            'pnl_percent': 0,
            'status': 'open',
            'trade_id': f"GRID-{self.pair}-{int(time.time())}",
        }
        self.balance -= cost
        self.open_positions.append(trade)
        self.db.save_trade(self.symbol, trade)
        return trade
    
    def _create_sell_signal(self, price, grid_point):
        buy_position = None
        for pos in self.open_positions:
            if pos['grid_level'] == grid_point['level'] and pos['status'] == 'open':
                buy_position = pos
                break
        if not buy_position:
            return None
        pnl = (price - buy_position['price']) * buy_position['quantity']
        pnl_percent = (price - buy_position['price']) / buy_position['price'] * 100
        trade = {
            'timestamp': datetime.now(timezone.utc).isoformat(),
            'side': 'SELL',
            'price': price,
            'quantity': buy_position['quantity'],
            'grid_level': grid_point['level'],
            'pnl': pnl,
            'pnl_percent': pnl_percent,
            'status': 'closed',
            'paired_with': buy_position['trade_id'],
            'trade_id': f"GRID-{self.pair}-{int(time.time())}",
        }
        self.balance += buy_position['quantity'] * price
        buy_position['status'] = 'closed'
        self.db.save_trade(self.symbol, trade)
        return trade
    
    def run(self):
        logger.info(f"🚀 {self.pair}: Engine STARTED")
        last_daily_report = datetime.now(timezone.utc)
        while True:
            try:
                price = get_price(f"{self.pair}-USD")
                if price is None:
                    time.sleep(CHECK_INTERVAL)
                    continue
                if not self.grid:
                    self.create_grid(price)
                signals = self.process_price(price)
                for signal in signals:
                    if signal['side'] == 'BUY':
                        logger.info(f"🟢 {self.pair} BUY @ ${signal['price']:.4f}")
                        self.notifier.send_trade_notification(self.pair, signal)
                    else:
                        logger.info(f"🔴 {self.pair} SELL @ ${signal['price']:.4f} | PnL: ${signal['pnl']:.2f}")
                        self.notifier.send_trade_notification(self.pair, signal)
                self._save_state()
                now = datetime.now(timezone.utc)
                if (now - last_daily_report).days >= 1:
                    trades = self.db.load_trades(self.symbol)
                    metrics = AnalyticsEngine.calculate_metrics(trades)
                    self.notifier.send_daily_report(self.pair, metrics)
                    last_daily_report = now
                time.sleep(CHECK_INTERVAL)
            except Exception as e:
                logger.error(f"❌ {self.pair}: Error: {e}", exc_info=True)
                time.sleep(CHECK_INTERVAL)

def main():
    logger.info("=" * 80)
    logger.info("🤖 GRID TRADING BOT v3.0 INITIALIZATION")
    logger.info("=" * 80)
    
    msg = f"""🤖 <b>Grid Trading Bot v3.0 - NEW VERSION</b>
━━━━━━━━━━━━━━━━━━━━━━━━━
✅ STARTED - Railway Deployment v3.0
📊 Pairs: {', '.join([p['name'] for p in PAIRS])}
💰 Balance: ${INITIAL_BALANCE}/pair
🎯 Target: 100% ROI/year
⏰ Time: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}"""
    
    notifier = TelegramNotifier(TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID)
    notifier.send_message(msg)
    
    threads = []
    for pair_config in PAIRS:
        engine = GridTradingEngine(pair_config)
        thread = threading.Thread(target=engine.run, daemon=False)
        thread.start()
        threads.append(thread)
        logger.info(f"✅ Thread started for {pair_config['name']}")
    
    logger.info("=" * 80)
    logger.info("🎉 ALL THREADS RUNNING - BOT ACTIVE 24/7")
    logger.info("=" * 80)
    
    for thread in threads:
        thread.join()

if __name__ == "__main__":
    main()
