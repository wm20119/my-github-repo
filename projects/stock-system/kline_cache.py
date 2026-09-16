#!/usr/bin/env python3
"""
K线数据缓存模块
- 缓存路径：~/.hermes/cache/klines/{code}_daily.json
- 检查缓存是否有效（最后日期距今<=3天）
- 有效则直接用缓存，无效则拉全量并更新
- 增量模式：缓存有效但不足天数时，拉全量天数补全
- 原子写入：使用临时文件+rename防止数据损坏
"""
import os, json, tempfile
import urllib.request
from datetime import datetime

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

def _save_cache(code, data):
    path = _cache_path(code)
    try:
        # 原子写入：先写临时文件再重命名
        dir_name = os.path.dirname(path)
        fd, tmp_path = tempfile.mkstemp(dir=dir_name, suffix='.tmp')
        try:
            with os.fdopen(fd, 'w') as f:
                json.dump(data, f)
            os.replace(tmp_path, path)  # 原子操作
        except Exception:
            try: os.close(fd)
            except: pass
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)
            raise
    except Exception:
        pass  # 写入失败不影响程序运行，但可加logging.warning

def _fetch_from_api(code, days=500):
    """从Sina API拉取K线"""
    if code.startswith('8'):
        prefix = 'bj'
    elif code.startswith(('6','9')):
        prefix = 'sh'
    else:
        prefix = 'sz'
    url = f'https://quotes.sina.cn/cn/api/jsonp_v2.php/var%20_k=/CN_MarketDataService.getKLineData?symbol={prefix}{code}&scale=240&ma=no&datalen={days}'
    req = urllib.request.Request(url, headers={
        'User-Agent': 'Mozilla/5.0',
        'Referer': 'https://finance.sina.com.cn'
    })
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            text = resp.read().decode('utf-8')
        s = text.find('['); e = text.rfind(']') + 1
        if s < 0: return []
        data = json.loads(text[s:e])
        return [{'date':d['day'][:10],'open':float(d['open']),'high':float(d['high']),
                 'low':float(d['low']),'close':float(d['close']),
                 'volume':float(d['volume'])} for d in data]
    except Exception:
        return []

def _is_cache_fresh(cache_data, max_age_days=1):
    """检查缓存是否有效（最后日期距今<=max_age_days天）"""
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
    - 缓存有效但不足days天：拉全量天数补全（向后扩展）
    """
    if not use_cache:
        return _fetch_from_api(code, days)

    cache = _load_cache(code)

    # 缓存有效，直接返回
    if _is_cache_fresh(cache, max_age_days=3):
        # 检查缓存是否够长
        if len(cache) >= days:
            return cache[-days:]
        # 不够长，直接拉全量天数以向后扩展
        new_data = _fetch_from_api(code, days)
        if new_data:
            # 合并：缓存 + 新数据（去重）— 操作副本避免污染原始数据
            merged = list(cache)
            cache_dates = {d['date'] for d in merged}
            for d in new_data:
                if d['date'] not in cache_dates:
                    merged.append(d)
            merged.sort(key=lambda x: x['date'])
            # 只保留最近days天
            if len(merged) > days:
                merged = merged[-days:]
            _save_cache(code, merged)
            return merged[-days:] if len(merged) >= days else merged
        return cache[-days:] if len(cache) >= days else cache

    # 缓存无效，拉全量
    data = _fetch_from_api(code, days)
    if data:
        _save_cache(code, data)
    return data

def fetch_realtime(code):
    """获取实时价格"""
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
    # 测试
    data = fetch_kline('600030', 100)
    print(f"拉取600030: {len(data)}根K线")
    if data:
        print(f"  最早: {data[0]['date']}")
        print(f"  最新: {data[-1]['date']}")
        print(f"  缓存文件: {_cache_path('600030')}")
