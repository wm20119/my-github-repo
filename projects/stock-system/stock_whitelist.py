#!/usr/bin/env python3
"""
股票初筛脚本
用腾讯实时API快速筛选，剔除垃圾股，保留约2000只

筛选规则：
1. 剔除ST/*ST
2. 剔除停牌（无实时价格）
3. 剔除北交所（8/4开头）
4. 剔除市值<10亿
5. 剔除日均成交额<500万

输出：~/.hermes/cache/stock_whitelist.json
"""
import sys, os, json, time, urllib.request
from datetime import datetime

CACHE_DIR = os.path.expanduser('~/.hermes/cache')
WHITELIST_FILE = os.path.join(CACHE_DIR, 'stock_whitelist.json')


def get_all_a_stocks():
    """获取全A股列表"""
    import tushare as ts
    token_path = os.path.expanduser('~/.tushare/token.txt')
    if not os.path.exists(token_path):
        return []
    
    with open(token_path) as f:
        token = f.read().strip()
    
    pro = ts.pro_api(token)
    df = pro.stock_basic(exchange='', list_status='L', fields='ts_code,symbol,name,area,industry,list_date')
    
    stocks = []
    for _, row in df.iterrows():
        code = row['symbol']  # 6位纯数字
        name = row['name']
        list_date = row['list_date']
        stocks.append({
            'code': code,
            'name': name,
            'ts_code': row['ts_code'],
            'list_date': list_date,
        })
    
    return stocks


def fetch_realtime_batch(codes):
    """批量获取实时价格（腾讯API）"""
    results = {}
    
    # 构建查询字符串
    queries = []
    for code in codes:
        if code.startswith(('6', '9')):
            prefix = 'sh'
        elif code.startswith(('4', '8')):
            prefix = 'bj'
        else:
            prefix = 'sz'
        queries.append(f'{prefix}{code}')
    
    # 一次最多查50只
    batch_size = 50
    for i in range(0, len(queries), batch_size):
        batch = queries[i:i+batch_size]
        query_str = ','.join(batch)
        url = f'https://qt.gtimg.cn/q={query_str}'
        
        try:
            req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
            with urllib.request.urlopen(req, timeout=10) as resp:
                raw = resp.read().decode('gbk', errors='ignore')
            
            # 解析返回数据
            for line in raw.split(';'):
                line = line.strip()
                if not line or '~' not in line:
                    continue
                
                parts = line.split('~')
                if len(parts) < 45:
                    continue
                
                # 提取代码（从变量名中提取）
                var_part = line.split('=')[0] if '=' in line else ''
                code = var_part.replace('v_', '').replace('sh', '').replace('sz', '').replace('bj', '')
                
                name = parts[1]
                price = float(parts[3]) if parts[3] else 0
                market_cap = float(parts[45]) if parts[45] else 0  # 总市值（亿元）
                turnover = float(parts[37]) if parts[37] else 0  # 成交额（万元）
                
                results[code] = {
                    'name': name,
                    'price': price,
                    'market_cap': market_cap,  # 万元
                    'turnover': turnover,  # 万元
                }
        except Exception as e:
            pass
        
        time.sleep(0.3)  # 限流
    
    return results


def screen_stocks(target_count=2000):
    """筛选股票"""
    print('正在获取全A股列表...')
    stocks = get_all_a_stocks()
    print(f'全A股: {len(stocks)}只')
    
    # 剔除北交所
    stocks = [s for s in stocks if not s['code'].startswith(('4', '8'))]
    print(f'剔除北交所后: {len(stocks)}只')
    
    # 剔除ST/*ST
    stocks = [s for s in stocks if 'ST' not in s['name'].upper()]
    print(f'剔除ST后: {len(stocks)}只')
    
    # 批量获取实时数据
    print('正在获取实时数据...')
    codes = [s['code'] for s in stocks]
    realtime = fetch_realtime_batch(codes)
    
    # 筛选
    passed = []
    for s in stocks:
        code = s['code']
        if code not in realtime:
            continue
        
        rt = realtime[code]
        
        # 剔除停牌（无价格）
        if rt['price'] <= 0:
            continue
        
        # 剔除市值<10亿（market_cap单位是亿元）
        if rt['market_cap'] < 10:
            continue
        
        # 剔除成交额<500万（turnover单位是万元，500万=500万元）
        if rt['turnover'] < 500:
            continue
        
        passed.append({
            'code': code,
            'name': rt['name'],
            'market_cap': rt['market_cap'],
            'turnover': rt['turnover'],
            'price': rt['price'],
        })
    
    print(f'筛选后: {len(passed)}只')
    
    # 按市值排序，保留前target_count只
    passed.sort(key=lambda x: x['market_cap'], reverse=True)
    if len(passed) > target_count:
        passed = passed[:target_count]
    
    print(f'最终保留: {len(passed)}只')
    
    # 保存白名单
    whitelist = {
        'date': datetime.now().strftime('%Y-%m-%d'),
        'count': len(passed),
        'stocks': {s['code']: s for s in passed},
    }
    
    with open(WHITELIST_FILE, 'w', encoding='utf-8') as f:
        json.dump(whitelist, f, ensure_ascii=False, indent=2)
    
    print(f'白名单已保存: {WHITELIST_FILE}')
    
    # 打印分布
    print(f'\n市值分布:')
    bins = [(10, 50), (50, 100), (100, 500), (500, 1000), (1000, 999999)]
    for low, high in bins:
        count = sum(1 for s in passed if low <= s['market_cap'] < high)
        print(f'  {low}-{high}亿: {count}只')
    
    return passed


def get_whitelist():
    """读取白名单"""
    if not os.path.exists(WHITELIST_FILE):
        return set()
    
    with open(WHITELIST_FILE, 'r', encoding='utf-8') as f:
        data = json.load(f)
    
    return set(data.get('stocks', {}).keys())


def is_in_whitelist(code):
    """检查股票是否在白名单中"""
    whitelist = get_whitelist()
    return code in whitelist


if __name__ == '__main__':
    if len(sys.argv) > 1 and sys.argv[1] == '--check':
        # 检查某只股票是否在白名单
        code = sys.argv[2]
        print(f'{code}: {"在白名单中" if is_in_whitelist(code) else "不在白名单中"}')
    elif len(sys.argv) > 1 and sys.argv[1] == '--count':
        # 显示白名单数量
        whitelist = get_whitelist()
        print(f'白名单: {len(whitelist)}只')
    else:
        # 执行筛选
        screen_stocks(target_count=3000)
