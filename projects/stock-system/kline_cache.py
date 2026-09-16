#!/usr/bin/env python3
"""
K线数据缓存模块
- 数据源：Tushare Pro API（稳定，不限流）
- 缓存路径：~/.hermes/cache/klines/{code}_daily.json
- 检查缓存是否有效（最后日期距今<=3天）
- 有效则直接用缓存，无效则拉全量并更新
- 增量模式：缓存有效但不足天数时，拉全量天数补全
- 原子写入：使用临时文件+rename防止数据损坏
"""
import os, json, tempfile
import urllib.request
from datetime import datetime, timedelta

CACHE_DIR = os.path.expanduser('~/.hermes/cache/klines')
os.makedirs(CACHE_DIR, exist_ok=True)

# Tushare token
_TUSHARE_TOKEN = None

def _get_tushare_token():
    global _TUSHARE_TOKEN
    if _TUSHARE_TOKEN:
        return _TUSHARE_TOKEN
    # 从文件读取
    token_path = os.path.expanduser('~/.tushare/token.txt')
    if os.path.exists(token_path):
        with open(token_path) as f:
            _TUSHARE_TOKEN = f.read().strip()
    return _TUSHARE_TOKEN

def _code_to_ts(code):
    """股票代码转Tushare格式：600030 -> 600030.SH"""
    code = str(code).zfill(6)
    if code.startswith(('6', '9')):
        return f'{code}.SH'
    elif code.startswith(('4', '8')):
        return f'{code}.BJ'
    else:
        return f'{code}.SZ'

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

def _save_cache(code, data):
    path = _cache_path(code)
    try:
        dir_name = os.path.dirname(path)
        fd, tmp_path = tempfile.mkstemp(dir=dir_name, suffix='.tmp')
        try:
            with os.fdopen(fd, 'w') as f:
                json.dump(data, f)
            os.replace(tmp_path, path)
        except Exception:
            try: os.close(fd)
            except: pass
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)
            raise
    except Exception:
        pass

def _fetch_from_api(code, days=500):
    """从Tushare API拉取日线K线"""
    token = _get_tushare_token()
    if not token:
        return []

    ts_code = _code_to_ts(code)
    end_date = datetime.now().strftime('%Y%m%d')
    start_date = (datetime.now() - timedelta(days=int(days * 1.6))).strftime('%Y%m%d')

    url = f'http://api.tushare.pro'
    payload = json.dumps({
        'api_name': 'daily',
        'token': token,
        'params': {
            'ts_code': ts_code,
            'start_date': start_date,
            'end_date': end_date,
        },
        'fields': 'trade_date,open,high,low,close,vol,amount'
    }).encode('utf-8')

    req = urllib.request.Request(url, data=payload, headers={
        'Content-Type': 'application/json'
    })
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            result = json.loads(resp.read().decode('utf-8'))
        if result.get('code') != 0:
            return []
        items = result.get('data', {}).get('items', [])
        if not items:
            return []
        # Tushare返回倒序（最新在前），转为正序
        data = []
        for item in items:
            # fields: trade_date,open,high,low,close,vol,amount
            trade_date = item[0]  # YYYYMMDD
            date_str = f'{trade_date[:4]}-{trade_date[4:6]}-{trade_date[6:8]}'
            data.append({
                'date': date_str,
                'open': float(item[1]),
                'high': float(item[2]),
                'low': float(item[3]),
                'close': float(item[4]),
                'volume': float(item[5]),
            })
        data.sort(key=lambda x: x['date'])
        return data[-days:] if len(data) > days else data
    except Exception:
        return []

def _is_cache_fresh(cache_data, max_age_days=1):
    if not cache_data:
        return False
    last_date = cache_data[-1]['date']
    try:
        last_dt = datetime.strptime(last_date, '%Y-%m-%d')
        return (datetime.now() - last_dt).days <= max_age_days
    except Exception:
        return False

def fetch_kline(code, days=500, use_cache=True):
    """
    获取K线数据（带缓存）
    - 缓存有效（最后日期距今<=3天）：直接返回缓存
    - 缓存无效：拉全量并更新缓存
    - 缓存有效但不足days天：拉全量天数补全
    """
    if not use_cache:
        return _fetch_from_api(code, days)

    cache = _load_cache(code)

    if _is_cache_fresh(cache, max_age_days=3):
        if len(cache) >= days:
            return cache[-days:]
        new_data = _fetch_from_api(code, days)
        if new_data:
            merged = list(cache)
            cache_dates = {d['date'] for d in merged}
            for d in new_data:
                if d['date'] not in cache_dates:
                    merged.append(d)
            merged.sort(key=lambda x: x['date'])
            if len(merged) > days:
                merged = merged[-days:]
            _save_cache(code, merged)
            return merged[-days:] if len(merged) >= days else merged
        return cache[-days:] if len(cache) >= days else cache

    data = _fetch_from_api(code, days)
    if data:
        _save_cache(code, data)
    return data

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
            raw = resp.read().decode('gbk')
        parts = raw.split('~')
        if len(parts) < 35: return None
        return {'price': float(parts[3]), 'name': parts[1]}
    except Exception:
        return None

if __name__ == '__main__':
    data = fetch_kline('600030', 100)
    print(f"拉取600030: {len(data)}根K线")
    if data:
        print(f"  最早: {data[0]['date']}")
        print(f"  最新: {data[-1]['date']}")
        print(f"  缓存文件: {_cache_path('600030')}")
