#!/usr/bin/env python3
"""
K线数据库更新脚本
从Tushare拉取新数据，更新SQLite数据库

用法:
  python update_kline_db.py              # 更新所有股票
  python update_kline_db.py 600030       # 更新指定股票
  python update_kline_db.py --codes 600030 000001  # 更新多只
"""
import sys, os, json, time
sys.path.insert(0, os.path.expanduser('~/.hermes/scripts'))

import urllib.request
from datetime import datetime, timedelta
from kline_db import init_db, get_latest_date, upsert_klines, get_all_codes, get_stats

# Tushare token
_TUSHARE_TOKEN = None

def _get_tushare_token():
    global _TUSHARE_TOKEN
    if _TUSHARE_TOKEN:
        return _TUSHARE_TOKEN
    token_path = os.path.expanduser('~/.tushare/token.txt')
    if os.path.exists(token_path):
        with open(token_path) as f:
            _TUSHARE_TOKEN = f.read().strip()
    return _TUSHARE_TOKEN


def _code_to_ts(code):
    """股票代码转Tushare格式"""
    code = str(code).zfill(6)
    if code.startswith(('6', '9')):
        return f'{code}.SH'
    elif code.startswith(('4', '8')):
        return f'{code}.BJ'
    else:
        return f'{code}.SZ'


def fetch_from_tushare(code, start_date=None, end_date=None, retries=3):
    """从Tushare API拉取日线K线"""
    token = _get_tushare_token()
    if not token:
        print(f'错误: Tushare token未配置')
        return []

    ts_code = _code_to_ts(code)
    if not end_date:
        end_date = datetime.now().strftime('%Y%m%d')
    if not start_date:
        # 默认拉取最近1年的数据
        start_date = (datetime.now() - timedelta(days=365)).strftime('%Y%m%d')

    url = 'https://api.tushare.pro'
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

    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, data=payload, headers={
                'Content-Type': 'application/json'
            })
            with urllib.request.urlopen(req, timeout=15) as resp:
                result = json.loads(resp.read().decode('utf-8'))
            if result.get('code') != 0:
                continue
            items = result.get('data', {}).get('items', [])
            if not items:
                return []  # 无新数据
            # 转换格式
            data = []
            for item in items:
                trade_date = item[0]
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
            return data
        except Exception as e:
            if attempt < retries - 1:
                time.sleep(1)
            else:
                print(f"  ⚠️ {code} Tushare拉取失败: {e}", file=sys.stderr)
    return []


def update_stock(code):
    """更新单只股票的K线数据"""
    latest = get_latest_date(code)
    
    if latest:
        # 兼容两种日期格式：2026-09-17 或 20260917
        try:
            if '-' in latest:
                start_date = (datetime.strptime(latest, '%Y-%m-%d') + timedelta(days=1)).strftime('%Y%m%d')
            else:
                start_date = (datetime.strptime(latest, '%Y%m%d') + timedelta(days=1)).strftime('%Y%m%d')
        except ValueError:
            start_date = '20100101'
    else:
        # 没有历史数据，拉取全部（从2010年开始）
        start_date = '20100101'
    
    end_date = datetime.now().strftime('%Y%m%d')
    
    # 如果start_date > end_date，说明已经是最新
    if start_date > end_date:
        return 0
    
    data = fetch_from_tushare(code, start_date, end_date)
    if data:
        upsert_klines(code, data)
        return len(data)
    return 0


def update_all():
    """更新所有股票"""
    init_db()
    
    # 从白名单获取股票列表
    try:
        from stock_whitelist import get_whitelist
        whitelist = get_whitelist()
        if whitelist:
            codes = list(whitelist)
        else:
            codes = get_all_codes()
    except Exception:
        codes = get_all_codes()
    
    print(f'需要更新: {len(codes)}只股票')
    
    updated = 0
    errors = []
    
    for i, code in enumerate(codes):
        try:
            count = update_stock(code)
            if count > 0:
                updated += 1
            
            if (i + 1) % 100 == 0:
                print(f'进度: {i+1}/{len(codes)} (已更新{updated}只)')
            
            # 限流：每只股票间隔0.1秒
            time.sleep(0.1)
        except Exception as e:
            errors.append(f'{code}: {str(e)[:50]}')
    
    print(f'\n完成! 更新了{updated}只股票')
    if errors:
        print(f'错误: {len(errors)}只')
        for e in errors[:5]:
            print(f'  {e}')
    
    # 打印统计
    stats = get_stats()
    print(f'\n数据库统计:')
    print(f'  股票数: {stats["total_codes"]}')
    print(f'  总行数: {stats["total_rows"]}')
    print(f'  分布: {stats["distribution"]}')


if __name__ == '__main__':
    init_db()
    
    if len(sys.argv) > 1:
        if sys.argv[1] == '--codes':
            # 更新指定股票
            codes = sys.argv[2:]
            for code in codes:
                count = update_stock(code)
                print(f'{code}: 更新{count}根K线')
                time.sleep(0.1)
        else:
            # 更新单只股票
            code = sys.argv[1]
            count = update_stock(code)
            print(f'{code}: 更新{count}根K线')
    else:
        # 更新所有
        update_all()
