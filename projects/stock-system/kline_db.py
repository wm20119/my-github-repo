#!/usr/bin/env python3
"""
K线数据库管理模块
SQLite数据库存储所有股票的全部K线数据

数据库位置: ~/.hermes/cache/kline.db
表结构:
  klines (code TEXT, date TEXT, open REAL, high REAL, low REAL, close REAL, volume REAL)
  主键: (code, date)
  索引: code
"""
import sqlite3
import os
import json
from datetime import datetime, timedelta

DB_PATH = os.path.expanduser('~/.hermes/cache/kline.db')


def get_conn():
    """获取数据库连接"""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    """初始化数据库表结构"""
    conn = get_conn()
    try:
        conn.execute('''
            CREATE TABLE IF NOT EXISTS klines (
                code TEXT NOT NULL,
                date TEXT NOT NULL,
                open REAL,
                high REAL,
                low REAL,
                close REAL,
                volume REAL,
                PRIMARY KEY (code, date)
            )
        ''')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_code ON klines(code)')
        conn.commit()
    finally:
        conn.close()


def get_klines(code, days=None):
    """
    从数据库读取K线数据
    
    Args:
        code: 股票代码
        days: 返回最近N根，None则返回全部
    
    Returns:
        list[dict]: K线数据列表
    """
    conn = get_conn()
    try:
        if days:
            rows = conn.execute(
                'SELECT * FROM klines WHERE code = ? ORDER BY date DESC LIMIT ?',
                (code, days)
            ).fetchall()
            rows = list(reversed(rows))  # 按时间正序
        else:
            rows = conn.execute(
                'SELECT * FROM klines WHERE code = ? ORDER BY date ASC',
                (code,)
            ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_latest_date(code):
    """获取某只股票的最新日期"""
    conn = get_conn()
    try:
        row = conn.execute(
            'SELECT MAX(date) as latest FROM klines WHERE code = ?',
            (code,)
        ).fetchone()
        return row['latest'] if row else None
    finally:
        conn.close()


def upsert_klines(code, klines_data):
    """
    插入或更新K线数据
    
    Args:
        code: 股票代码
        klines_data: list[dict], 每个dict包含date,open,high,low,close,volume
    """
    conn = get_conn()
    try:
        # 统一日期格式为 YYYY-MM-DD
        normalized = []
        for d in klines_data:
            date = d['date']
            if '-' not in date and len(date) == 8:
                date = f'{date[:4]}-{date[4:6]}-{date[6:8]}'
            normalized.append((code, date, d['open'], d['high'], d['low'], d['close'], d['volume']))
        
        conn.executemany(
            '''INSERT OR REPLACE INTO klines (code, date, open, high, low, close, volume)
               VALUES (?, ?, ?, ?, ?, ?, ?)''',
            normalized
        )
        conn.commit()
    finally:
        conn.close()


def get_all_codes():
    """获取数据库中所有股票代码"""
    conn = get_conn()
    try:
        rows = conn.execute('SELECT DISTINCT code FROM klines').fetchall()
        return [r['code'] for r in rows]
    finally:
        conn.close()


def get_stats():
    """获取数据库统计信息"""
    conn = get_conn()
    try:
        total_codes = conn.execute('SELECT COUNT(DISTINCT code) as cnt FROM klines').fetchone()['cnt']
        total_rows = conn.execute('SELECT COUNT(*) as cnt FROM klines').fetchone()['cnt']
        
        # 每只股票的K线数量分布
        dist = conn.execute('''
            SELECT 
                CASE 
                    WHEN cnt < 200 THEN '<200'
                    WHEN cnt < 500 THEN '200-500'
                    WHEN cnt < 1000 THEN '500-1000'
                    WHEN cnt < 2000 THEN '1000-2000'
                    ELSE '2000+'
                END as range,
                COUNT(*) as stocks
            FROM (SELECT code, COUNT(*) as cnt FROM klines GROUP BY code) t
            GROUP BY range
        ''').fetchall()
        
        return {
            'total_codes': total_codes,
            'total_rows': total_rows,
            'distribution': {r['range']: r['stocks'] for r in dist}
        }
    finally:
        conn.close()


def migrate_from_cache():
    """从现有缓存文件迁移到SQLite数据库"""
    cache_dir = os.path.expanduser('~/.hermes/cache/klines/')
    if not os.path.exists(cache_dir):
        print(f'缓存目录不存在: {cache_dir}')
        return
    
    init_db()
    
    files = [f for f in os.listdir(cache_dir) if f.endswith('.json')]
    print(f'找到{len(files)}个缓存文件，开始迁移...')
    
    for i, f in enumerate(files):
        code = f.replace('_daily.json', '')
        filepath = os.path.join(cache_dir, f)
        
        try:
            with open(filepath, 'r') as fh:
                data = json.load(fh)
            
            if data:
                upsert_klines(code, data)
            
            if (i + 1) % 100 == 0:
                print(f'进度: {i+1}/{len(files)}')
        except Exception as e:
            print(f'错误 {code}: {e}')
    
    stats = get_stats()
    print(f'迁移完成!')
    print(f'股票数: {stats["total_codes"]}')
    print(f'总行数: {stats["total_rows"]}')
    print(f'分布: {stats["distribution"]}')


if __name__ == '__main__':
    import sys
    
    if len(sys.argv) > 1 and sys.argv[1] == 'migrate':
        migrate_from_cache()
    elif len(sys.argv) > 1 and sys.argv[1] == 'stats':
        init_db()
        stats = get_stats()
        print(f'股票数: {stats["total_codes"]}')
        print(f'总行数: {stats["total_rows"]}')
        print(f'分布: {stats["distribution"]}')
    else:
        print('用法:')
        print('  python kline_db.py migrate  # 从缓存迁移')
        print('  python kline_db.py stats    # 查看统计')
