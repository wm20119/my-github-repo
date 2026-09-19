#!/usr/bin/env python3
"""
stock_log.py — 股票系统日志模块
- 运行日志：写入 stock_system.log
- 交易日志：写入 stock_trade_log.db（SQLite，可查询）
"""
import os, sqlite3, json
from datetime import datetime

LOG_DIR = os.path.expanduser('~/.hermes/cache/logs')
os.makedirs(LOG_DIR, exist_ok=True)

# ========== 交易日志数据库 ==========
TRADE_DB = os.path.join(LOG_DIR, 'stock_trade_log.db')

def _init_trade_db():
    conn = sqlite3.connect(TRADE_DB)
    conn.execute('''CREATE TABLE IF NOT EXISTS trades (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts TEXT NOT NULL,
        code TEXT NOT NULL,
        name TEXT NOT NULL,
        action TEXT NOT NULL,
        price REAL,
        shares INTEGER,
        pnl_pct REAL,
        reason TEXT,
        hold_days INTEGER,
        pivot_zg REAL,
        extra TEXT
    )''')
    conn.commit()
    return conn

def log_trade(code, name, action, price=None, shares=None, pnl_pct=None,
              reason=None, hold_days=None, pivot_zg=None, extra=None):
    """记录一笔交易到SQLite"""
    conn = _init_trade_db()
    conn.execute(
        'INSERT INTO trades (ts, code, name, action, price, shares, pnl_pct, reason, hold_days, pivot_zg, extra) '
        'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
        (datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
         code, name, action, price, shares, pnl_pct, reason, hold_days, pivot_zg, extra)
    )
    conn.commit()
    conn.close()

def log_event(event_type, message):
    """记录系统事件到SQLite"""
    conn = _init_trade_db()
    conn.execute(
        'INSERT INTO trades (ts, code, name, action, extra) VALUES (?, ?, ?, ?, ?)',
        (datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
         '-', event_type, 'event', message)
    )
    conn.commit()
    conn.close()

def get_recent_trades(n=20):
    """查询最近N条交易记录"""
    conn = _init_trade_db()
    rows = conn.execute(
        'SELECT ts, code, name, action, price, shares, pnl_pct, reason, hold_days '
        'FROM trades WHERE action != "event" ORDER BY id DESC LIMIT ?', (n,)
    ).fetchall()
    conn.close()
    return rows

def get_recent_events(n=10):
    """查询最近N条系统事件"""
    conn = _init_trade_db()
    rows = conn.execute(
        'SELECT ts, name, extra FROM trades WHERE action = "event" ORDER BY id DESC LIMIT ?', (n,)
    ).fetchall()
    conn.close()
    return rows
