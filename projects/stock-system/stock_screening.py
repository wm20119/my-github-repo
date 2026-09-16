#!/usr/bin/env python3
"""
选股系统 v3.1 — 全A股扫描
stock-scorer粗筛(≥50分) → 缠论历史胜率精筛(A/B级+三买信号)
输出：两道关都过的候选票
"""
import sys, os, json, time, tempfile
import argparse
sys.path.insert(0, os.path.expanduser('~/.hermes/scripts'))
from kline_cache import fetch_kline
from chanlun_strategy import (
    WINDOW, evaluate_chanlun_quality, scan_recent_signals,
)

def atomic_json_dump(data, path):
    """原子写入JSON文件：先写临时文件，再rename"""
    dir_name = os.path.dirname(path)
    os.makedirs(dir_name, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=dir_name, suffix='.tmp')
    try:
        with os.fdopen(fd, 'w') as f:
            json.dump(data, f, ensure_ascii=False)
        os.replace(tmp_path, path)
    except:
        try: os.unlink(tmp_path)
        except: pass
        raise

# 股票池：从stock_config.json统一读取
_CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'stock_config.json')

def load_pool_codes():
    """从stock_config.json读取股票池代码"""
    try:
        with open(_CONFIG_PATH, 'r', encoding='utf-8') as f:
            cfg = json.load(f)
        return cfg.get('stocks', [])
    except Exception:
        return ['600030', '688019', '688008', '600584', '300750', '000977', '002156']

POOL_CODES = load_pool_codes()

def get_all_a_stocks():
    """获取全A股列表（带缓存）"""
    cache_file = os.path.expanduser('~/.hermes/scripts/cache/all_a_stocks.json')
    if os.path.exists(cache_file):
        mtime = os.path.getmtime(cache_file)
        if time.time() - mtime < 86400:  # 缓存1天有效
            with open(cache_file) as f:
                return json.load(f)
    import akshare as ak
    df = ak.stock_info_a_code_name()
    stocks = [(row['code'], row['name']) for _, row in df.iterrows()]
    os.makedirs(os.path.dirname(cache_file), exist_ok=True)
    with open(cache_file, 'w') as f:
        json.dump(stocks, f, ensure_ascii=False)
    return stocks

def run_screening(skip_step1=False):
    from datetime import datetime
    now = datetime.now()
    report = []
    report.append(f"📊 全A股选股扫描 {now.strftime('%Y-%m-%d %H:%M')}")
    report.append("流程: 全A股 → stock-scorer粗筛(≥50分) → 缠论精筛(A/B级+三买信号)")
    report.append("=" * 50)

    # 1. 获取全A股
    try:
        all_stocks = get_all_a_stocks()
        # 过滤ST、退市、北交所(8/9开头)
        candidates = [(c, n) for c, n in all_stocks
                      if c not in POOL_CODES
                      and not c.startswith('8') and not c.startswith('9')
                      and 'ST' not in n and '退' not in n]
        report.append(f"\n全A股: {len(all_stocks)}只 (排除股票池/ST/退市/北交所后{len(candidates)}只)")
    except Exception as e:
        report.append(f"获取股票列表失败: {e}")
        return "\n".join(report)

    # 2. stock-scorer评分
    import stock_scorer as ss
    report.append(f"\n【Step 1】stock-scorer粗筛...")
    scores = {}
    fail_count = 0
    step1_cache = os.path.expanduser('~/.hermes/cache/screening_step1_scores.json')
    cache_fresh = False
    cache_age = 0
    if os.path.exists(step1_cache):
        cache_age = time.time() - os.path.getmtime(step1_cache)
        cache_fresh = cache_age < 7 * 86400  # 7天有效
    if (skip_step1 or cache_fresh) and os.path.exists(step1_cache):
        try:
            with open(step1_cache) as f:
                scores = json.load(f)
            age_days = int(cache_age / 86400)
            report.append(f"  从缓存加载: {len(scores)}只 (缓存{age_days}天前, {'有效' if cache_fresh else '已过期但强制使用'})")
            fail_count = None  # 缓存模式不统计失败
        except (json.JSONDecodeError, ValueError) as e:
            report.append(f"  Step1缓存损坏，重新评分: {e}")
            scores = {}
            cache_fresh = False
    else:
        for i, (code, name) in enumerate(candidates):
            if i % 50 == 0:
                report.append(f"  评分进度: {i + 1}/{len(candidates)} (通过{len(scores)}只)")
            try:
                results = ss.analyze([code], skip_industry=True)
                if results and results[0]['total'] >= 50:
                    scores[code] = results[0]
            except Exception:
                fail_count += 1
            time.sleep(0.3)
        atomic_json_dump(scores, step1_cache)
        report.append(f"  Step1结果已缓存")
    if fail_count is not None:
        report.append(f"  评分完成: {len(scores)}只通过(≥50分), {fail_count}只失败")
    else:
        report.append(f"  评分完成: {len(scores)}只通过(≥50分)")

    # 3. 缠论质量精筛
    report.append(f"\n【Step 2】缠论历史胜率精筛...")
    chanlun_results = {}
    for i, (code, score_info) in enumerate(scores.items()):
        name = score_info.get('name', code)
        if i % 10 == 0:
            report.append(f"  缠论进度: {i + 1}/{len(scores)} (通过{len(chanlun_results)}只)")
        kl = fetch_kline(code, 1000)
        if not kl or not isinstance(kl, list) or len(kl) < WINDOW + 100:
            continue
        stats = evaluate_chanlun_quality(kl, name, code)
        if stats and stats['quality'] in ('A', 'B') and stats['total_trades'] >= 5:
            # 检查最近2天是否有三买信号
            recent_sigs = scan_recent_signals(kl, name, code, lookback=2)
            if not recent_sigs:
                continue
            chanlun_results[code] = {
                'score': score_info,
                'chanlun': stats,
                'has_signal': True,
                'kl': kl,
            }
        time.sleep(0.1)

    report.append(f"  精筛完成: {len(chanlun_results)}只通过(A/B级)")

    # 4. 输出结果
    if not chanlun_results:
        report.append("\n无候选票。")

    # 复合分值排序: 胜率×盈亏比×0.6 + 评分×0.4
    def _rank_key(x):
        wr = x[1]['chanlun']['win_rate']
        pf = x[1]['chanlun']['profit_factor']
        if pf >= 999:  # evaluate_chanlun_quality将inf转为999.99
            pf = 5
        score = x[1]['score']['total']
        return -(wr * pf * 0.6 + score * 0.4)
    sorted_results = sorted(chanlun_results.items(), key=_rank_key)

    report.append(f"\n{'=' * 50}")
    report.append(f"🎯 候选股（两道关都过）: {len(sorted_results)}只")
    report.append(f"{'=' * 50}")

    for code, info in sorted_results:
        s = info['score']
        c = info['chanlun']
        kl = info['kl']
        latest = kl[-1]
        closes = [d['close'] for d in kl]
        ma20_now = sum(closes[-20:]) / 20
        above = (latest['close'] - ma20_now) / ma20_now * 100 if ma20_now > 0 else 0

        pf_str = '∞' if c['profit_factor'] == float('inf') else f"{c['profit_factor']:.2f}"
        report.append(f"\n{s['name']}({code}) ¥{latest['close']:.2f} 离MA20{above:+.1f}%")
        report.append(f"  评分: {s['total']:.0f}({s['rating']}) 基{s['fundamental']}+估{s['valuation']}+卡{s['chokepoint']}+技{s['technical']}+情{s['sentiment']}")
        report.append(f"  缠论: {c['quality']}级 胜率{c['win_rate']:.0f}% 盈亏比{pf_str}")
        trades_n = c['total_trades']
        reliable = '✅可靠' if trades_n >= 5 else '⚠️数据偏少'
        report.append(f"  交易次数: {trades_n}笔 {reliable}")
        if info['has_signal']:
            report.append(f"  🔔 有三买信号！")

    # 保存结果供盘中扫描使用（即使为空也要更新，避免下游读到过期数据）
    cache_file = os.path.expanduser('~/.hermes/cache/screening_latest.json')
    cache_data = {
        'date': now.strftime('%Y-%m-%d'),
        'passed': {code: {'score': info['score'], 'chanlun': info['chanlun']}
                   for code, info in chanlun_results.items()},
    }
    atomic_json_dump(cache_data, cache_file)

    # 单独存备选池（含价格，供模拟盘用）
    backup_file = os.path.expanduser('~/.hermes/cache/backup_pool.json')
    backup_data = []
    for code, info in chanlun_results.items():
        kl = info['kl']
        latest = kl[-1]
        backup_data.append({
            'code': code,
            'name': info['score']['name'],
            'date': now.strftime('%Y-%m-%d'),
            'price': latest['close'],
            'score': info['score']['total'],
            'rating': info['score']['rating'],
            'chanlun_quality': info['chanlun']['quality'],
            'chanlun_wr': info['chanlun']['win_rate'],
            'chanlun_pf': info['chanlun']['profit_factor'],
            'has_signal': info['has_signal'],
        })
    atomic_json_dump(backup_data, backup_file)

    # 追加到历史记录
    history_file = os.path.expanduser('~/.hermes/cache/backup_pool_history.json')
    history = []
    if os.path.exists(history_file):
        try:
            with open(history_file) as f:
                history = json.load(f)
        except Exception:
            pass
    history.append({'date': now.strftime('%Y-%m-%d'), 'stocks': backup_data})
    history = history[-60:]  # 只保留最近60天
    atomic_json_dump(history, history_file)

    return "\n".join(report)

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--skip-step1', action='store_true', help='跳过Step1评分（用缓存）')
    args = parser.parse_args()
    print(run_screening(skip_step1=args.skip_step1))
