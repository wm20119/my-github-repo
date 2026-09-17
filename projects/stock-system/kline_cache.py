#!/usr/bin/env python3
"""
K线数据模块
- 数据源：SQLite数据库（~/.hermes/cache/kline.db）
- 更新方式：手动运行 update_kline_db.py
- 实时价格：腾讯API
"""
import os, json, tempfile
import urllib.request
from datetime import datetime, timedelta

# 数据库模块
try:
    from kline_db import get_klines as _db_get_klines, get_latest_date as _db_get_latest_date
    _HAS_DB = True
except ImportError:
    _HAS_DB = False

# 兼容旧缓存
CACHE_DIR = os.path.expanduser('~/.hermes/cache/klines')
os.makedirs(CACHE_DIR, exist_ok=True)


def _cache_path(code):
    return os.path.join(CACHE_DIR, f'{code}_daily.json')


def _load_cache(code):
    path = _cache_path(code)
    if os.path.exists(path):
        try:
            with open(path, 'r') as f:
                return json.load(f)
        except Exception:
            pass
    return []


def fetch_kline(code, days=None, use_cache=True):
    """
    获取K线数据
    
    优先从SQLite数据库读取，如果数据库不存在则从旧缓存读取。
    
    Args:
        code: 股票代码
        days: 返回最近N根，None返回全部
        use_cache: 是否使用缓存（保留参数，现在只从数据库/旧缓存读）
    
    Returns:
        list[dict]: K线数据列表，每个dict包含date,open,high,low,close,volume
    """
    # 优先从SQLite数据库读取
    if _HAS_DB:
        try:
            data = _db_get_klines(code, days)
            if data:
                return data
        except Exception:
            pass
    
    # 回退到旧缓存文件
    cache = _load_cache(code)
    if cache:
        if days and len(cache) > days:
            return cache[-days:]
        return cache
    
    return []


def fetch_realtime(code):
    """获取实时价格（用腾讯API，Tushare无实时接口）"""
    if code.startswith('8'):
        prefix = 'bj'
    elif code.startswith(('6','9')):
        prefix = 'sh'
    else:
        prefix = 'sz'
    url = f'https://qt.gtimg.cn/q={prefix}{code}'
    try:
        req = urllib.request.Request(url, headers={'User-Agent':'Mozilla/5.0'})
        with urllib.request.urlopen(req, timeout=10) as resp:
            raw = resp.read().decode('gbk', errors='ignore')
        parts = raw.split('~')
        if len(parts) < 35: return None
        return {'price': float(parts[3]), 'name': parts[1]}
    except Exception:
        return None


if __name__ == '__main__':
    data = fetch_kline('600030')
    print(f"600030: {len(data)}根K线")
    if data:
        print(f"  最早: {data[0]['date']}")
        print(f"  最新: {data[-1]['date']}")
