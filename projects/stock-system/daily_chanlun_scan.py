#!/usr/bin/env python3
"""
每日缠论信号扫描 v2 — 调用chanlun_strategy公共模块
股票池 + 选股候选，输出缠论质量评级 + 三买信号
"""
import sys, os, json, time
from datetime import datetime, timedelta
sys.path.insert(0, os.path.expanduser('~/.hermes/scripts'))
from kline_cache import fetch_kline
from chanlun_strategy import (
    WINDOW, evaluate_chanlun_quality, scan_recent_signals,
)
from stock_screening import POOL_CODES

# Tushare批量K线（补充当天数据）
_TUSHARE_PRO = None
def _init_tushare():
    global _TUSHARE_PRO
    if _TUSHARE_PRO is not None:
        return
    try:
        import tushare as ts
        token_path = os.path.expanduser('~/.tushare/token.txt')
        if os.path.exists(token_path):
            with open(token_path) as f:
                token = f.read().strip()
            if token:
                _TUSHARE_PRO = ts.pro_api(token)
    except Exception:
        pass

def _tushare_code(code):
    """股票代码 → ts_code"""
    if code.startswith('8'):
        return f'{code}.BJ'
    if code.startswith(('6', '9')):
        return f'{code}.SH'
    return f'{code}.SZ'

def fetch_today_klines(codes):
    """从Tushare批量获取今天K线，返回 dict[code] -> dict"""
    _init_tushare()
    if _TUSHARE_PRO is None or not codes:
        return {}
    today = datetime.now().strftime('%Y%m%d')
    ts_codes = [_tushare_code(c) for c in codes]
    result = {}
    try:
        df = _TUSHARE_PRO.daily(ts_code=','.join(ts_codes), 
                                start_date=today, end_date=today,
                                fields='ts_code,trade_date,open,high,low,close,vol')
        if df is not None and len(df) > 0:
            for ts_c, code in zip(ts_codes, codes):
                row = df[df['ts_code'] == ts_c]
                if len(row) > 0:
                    r = row.iloc[0]
                    result[code] = {
                        'date': str(r['trade_date']),
                        'open': float(r['open']),
                        'high': float(r['high']),
                        'low': float(r['low']),
                        'close': float(r['close']),
                        'volume': float(r['vol']),
                    }
    except Exception as e:
        print(f"  ⚠️ Tushare批量拉取失败: {e}", file=sys.stderr)
    return result

# 股票池名称映射（从stock_config.json的stocks字段读取7只）
import json as _json
_cfg_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'stock_config.json')
try:
    with open(_cfg_path) as _f:
        _cfg = _json.load(_f)
    _STOCK_NAMES = {'600030': '中信证券', '688019': '安集科技', '688008': '澜起科技',
                    '600584': '长电科技', '300750': '宁德时代', '000977': '浪潮信息', '002156': '通富微电'}
    POOL = [(c, _STOCK_NAMES.get(c, c)) for c in _cfg.get('stocks', [])]
except Exception:
    POOL = [(c, c) for c in POOL_CODES]

def load_screening_results():
    """加载最近一次选股扫描的结果（检查日期是否为今天）"""
    cache_file = os.path.expanduser('~/.hermes/cache/screening_latest.json')
    if not os.path.exists(cache_file):
        return []
    try:
        with open(cache_file) as f:
            data = json.load(f)
        # 检查数据日期是否为今天（超过1天的数据视为过期）
        from datetime import datetime, timedelta
        data_date = data.get('date', '')
        if data_date:
            try:
                data_dt = datetime.strptime(data_date, '%Y-%m-%d')
                if (datetime.now() - data_dt).days > 1:
                    return []  # 数据过期
            except ValueError:
                pass
        return [(c, info['score']['name']) for c, info in data.get('passed', {}).items()]
    except Exception:
        return []

def scan_one(code, name, today_klines=None):
    """扫描单只票，返回结果字典"""
    try:
        kl = fetch_kline(code, 1000)
        if not kl or len(kl) < WINDOW + 100:
            return None

        # 如果数据库最新数据不是今天，补充当天K线
        today_str = datetime.now().strftime('%Y%m%d')
        if kl[-1]['date'] != today_str:
            if today_klines and code in today_klines:
                kl.append(today_klines[code])
            elif today_klines is None:
                # 首次调用时批量拉取
                today_klines = fetch_today_klines([code])
                if code in today_klines:
                    kl.append(today_klines[code])

        # 质量评估
        stats = evaluate_chanlun_quality(kl, name, code)
        quality = stats['quality'] if stats else '?'
        wr = stats['win_rate'] if stats else 0
        pf = stats['profit_factor'] if stats else 0

        # 最近信号（30天内）
        recent = scan_recent_signals(kl, name, code, lookback=30)

        latest = kl[-1]
        closes = [d['close'] for d in kl]
        ma20_now = sum(closes[-20:]) / 20 if len(closes) >= 20 else closes[-1]
        above_now = (latest['close'] - ma20_now) / ma20_now * 100 if ma20_now > 0 else 0

        return {
            'code': code, 'name': name,
            'price': latest['close'], 'above_ma20': above_now,
            'quality': quality, 'win_rate': wr, 'profit_factor': pf,
            'has_signal': bool(recent),
            'signal': recent[-1] if recent else None,
        }
    except Exception as e:
        print(f"  ⚠️ {name}({code}) 扫描失败: {e}", file=sys.stderr)
        return None

def run_scan():
    from datetime import datetime
    now = datetime.now()
    report = []
    report.append(f"📊 缠论信号扫描 {now.strftime('%Y-%m-%d %H:%M')}")
    report.append("=" * 50)

    # 合并股票池 + 选股候选（去重）
    all_stocks = [(c, n) for c, n in POOL]
    screening = load_screening_results()
    pool_codes = [c for c, _ in POOL]
    for c, n in screening:
        if c not in pool_codes:
            all_stocks.append((c, n))

    report.append(f"扫描范围: 股票池{len(POOL)}只 + 选股候选{len(screening)}只 = 共{len(all_stocks)}只")

    # 批量拉取今天K线（如果数据库没有）
    today_str = now.strftime('%Y%m%d')
    all_codes = [c for c, _ in all_stocks]
    today_klines = fetch_today_klines(all_codes)
    if today_klines:
        report.append(f"  从Tushare补充{len(today_klines)}只当天K线")

    results = []
    for i, (code, name) in enumerate(all_stocks):
        if i % 5 == 0:
            report.append(f"  进度: {i + 1}/{len(all_stocks)}")
        r = scan_one(code, name, today_klines)
        if r:
            results.append(r)
        time.sleep(0.3)

    # 分类输出
    has_signal = [r for r in results if r['has_signal']]
    pool_results = [r for r in results if r['code'] in pool_codes]
    screen_results = [r for r in results if r['code'] not in pool_codes]

    # 股票池部分
    report.append(f"\n{'=' * 50}")
    report.append(f"📌 股票池 ({len(pool_results)}只)")
    report.append(f"{'=' * 50}")
    for r in pool_results:
        if r['profit_factor'] == 0:
            pf_str = 'N/A'
        elif r['profit_factor'] >= 999.99:
            pf_str = '∞'
        else:
            pf_str = f"{r['profit_factor']:.2f}"
        line = f"\n{r['name']}({r['code']}) {r['price']:.2f} 离MA20{r['above_ma20']:+.1f}%"
        line += f"\n  质量: {r['quality']}级 胜率{r['win_rate']:.0f}% 盈亏比{pf_str}"
        if r['has_signal']:
            sig = r['signal']
            line += f"\n  🔔 三买: {sig['date']} ¥{sig['price']:.2f} RSI={sig['rsi']:.0f} 量比={sig['vol_ratio']:.2f}"
        else:
            line += f"\n  无三买信号"
        report.append(line)

    # 选股候选部分
    if screen_results:
        report.append(f"\n{'=' * 50}")
        report.append(f"🎯 选股候选 ({len(screen_results)}只)")
        report.append(f"{'=' * 50}")
        for r in sorted(screen_results, key=lambda x: -x['win_rate']):
            if r['profit_factor'] == 0:
                pf_str = 'N/A'
            elif r['profit_factor'] >= 999.99:
                pf_str = '∞'
            else:
                pf_str = f"{r['profit_factor']:.2f}"
            line = f"\n{r['name']}({r['code']}) {r['price']:.2f} 离MA20{r['above_ma20']:+.1f}%"
            line += f"\n  质量: {r['quality']}级 胜率{r['win_rate']:.0f}% 盈亏比{pf_str}"
            if r['has_signal']:
                sig = r['signal']
                line += f"\n  🔔 三买: {sig['date']} ¥{sig['price']:.2f} RSI={sig['rsi']:.0f} 量比={sig['vol_ratio']:.2f}"
            else:
                line += f"\n  无三买信号"
            report.append(line)

    # 有信号的汇总
    if has_signal:
        report.append(f"\n{'=' * 50}")
        report.append(f"🔔 有三买信号的票: {len(has_signal)}只")
        report.append(f"{'=' * 50}")
        for r in has_signal:
            sig = r['signal']
            report.append(f"  {r['name']}({r['code']}) ¥{sig['price']:.2f} RSI={sig['rsi']:.0f} 量比={sig['vol_ratio']:.2f}")
    else:
        report.append(f"\n当前无三买信号触发。")

    return "\n".join(report)

if __name__ == '__main__':
    print(run_scan())
