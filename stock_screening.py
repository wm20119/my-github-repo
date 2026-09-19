#!/usr/bin/env python3
"""
选股系统 v3.1 — 全A股扫描
缠论历史胜率先筛(A/B级+三买信号) → stock-scorer评分把关
输出：两道关都过的候选票
"""
import sys, os, json, time, tempfile
sys.path.insert(0, os.path.expanduser('~/.hermes/scripts'))
from kline_db import init_db, get_latest_date, upsert_klines, get_klines
from chanlun_strategy import (
    WINDOW, evaluate_chanlun_quality, scan_recent_signals,
)
from stock_whitelist import get_whitelist

def atomic_json_dump(data, path):
    """原子写入JSON文件：先写临时文件，再rename"""
    dir_name = os.path.dirname(path)
    os.makedirs(dir_name, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=dir_name, suffix='.tmp')
    try:
        with os.fdopen(fd, 'w') as f:
            json.dump(data, f, ensure_ascii=False)
        os.replace(tmp_path, path)
    except Exception:
        try: os.unlink(tmp_path)
        except Exception: pass
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

def run_screening():
    from datetime import datetime
    import sys
    now = datetime.now()
    report = []
    report.append(f"📊 全A股选股扫描 {now.strftime('%Y%m%d %H:%M')}")
    report.append("流程: 全A股 → 缠论快筛(A/B级+三买信号) → stock-scorer评分(≥50分)")
    report.append("=" * 50)

    # 1. 获取股票列表（从白名单）
    try:
        whitelist = get_whitelist()
        if whitelist:
            candidates = [(c, '') for c in whitelist]
            report.append(f"\n白名单: {len(whitelist)}只")
            print(f"白名单: {len(whitelist)}只", flush=True)
        else:
            report.append(f"\n白名单为空，请先运行stock_whitelist.py")
            return "\n".join(report)
    except Exception as e:
        report.append(f"获取股票列表失败: {e}")
        return "\n".join(report)

    # 2. 缠论快筛（只读K线，速度快）
    report.append(f"\n【Step 1】缠论快筛（A/B级+三买信号）...")
    
    # 初始化K线数据库
    init_db()
    
    chanlun_candidates = {}
    chanlun_errors = 0
    for i, (code, name) in enumerate(candidates):
        if i % 100 == 0:
            report.append(f"  缠论进度: {i + 1}/{len(candidates)} (通过{len(chanlun_candidates)}只)")
        try:
            # 从数据库读取K线数据（不更新，直接读）
            kl = get_klines(code)
            if not kl or not isinstance(kl, list) or len(kl) < WINDOW + 100:
                continue
            
            stats = evaluate_chanlun_quality(kl, name, code)
            if stats and stats['quality'] in ('A', 'B') and stats['total_trades'] >= 5:
                # 检查最近1天是否有三买信号
                recent_sigs = scan_recent_signals(kl, name, code, lookback=1)
                if recent_sigs:
                    chanlun_candidates[code] = {
                        'name': name,
                        'chanlun': stats,
                        'latest': kl[-1],
                        'ma20': sum(d['close'] for d in kl[-20:]) / min(20, len(kl)),
                    }
        except Exception as e:
            chanlun_errors += 1
            if chanlun_errors <= 5:
                print(f"  ⚠️ {name}({code}) 缠论分析异常: {e}", file=sys.stderr)
    
    if chanlun_errors > 5:
        report.append(f"  ⚠️ {chanlun_errors}只票缠论分析异常（仅显示前5条）")
    
    report.append(f"  缠论快筛完成: {len(chanlun_candidates)}只通过(A/B级+三买信号)")
    
    if not chanlun_candidates:
        report.append("\n无候选票。")
        report.append("\n" + "=" * 50)
        report.append(f"🎯 候选股（两道关都过）: 0只")
        report.append("=" * 50)
        return "\n".join(report)
    
    # 3. 对A/B级股票进行stock-scorer评分
    report.append(f"\n【Step 2】stock-scorer评分（仅{len(chanlun_candidates)}只）...")
    
    import stock_scorer as ss
    scores = {}
    
    # 批量调用analyze，每50只一批
    chanlun_codes = list(chanlun_candidates.keys())
    batch_size = 50
    
    for batch_start in range(0, len(chanlun_codes), batch_size):
        batch_codes = chanlun_codes[batch_start:batch_start + batch_size]
        report.append(f"  评分进度: {batch_start + len(batch_codes)}/{len(chanlun_codes)}")
        
        try:
            results = ss.analyze(batch_codes, skip_industry=True, skip_trends=True)
            for r in results:
                if r['total'] >= 50:
                    scores[r['code']] = r
        except Exception as e:
            report.append(f"  批次错误: {e}")

    report.append(f"  评分完成: {len(scores)}只通过(≥50分)")
    
    # 4. 合并结果
    chanlun_results = {}
    for code in scores:
        if code in chanlun_candidates:
            chanlun_results[code] = {
                'score': scores[code],
                'chanlun': chanlun_candidates[code]['chanlun'],
                'has_signal': True,
                'latest': chanlun_candidates[code]['latest'],
                'ma20': chanlun_candidates[code]['ma20'],
            }

    # 4. 输出结果
    if not chanlun_results:
        report.append("\n无候选票。")

    # 复合分值排序: 胜率×盈亏比×0.6 + 评分×0.4
    def _rank_key(x):
        wr = x[1]['chanlun']['win_rate']
        pf = x[1]['chanlun']['profit_factor']
        if pf >= 999.9:  # evaluate_chanlun_quality将inf转为999.99，避免误截正常值
            pf = 5.0
        score = x[1]['score']['total']
        return -(wr * pf * 0.6 + score * 0.4)
    sorted_results = sorted(chanlun_results.items(), key=_rank_key)

    report.append(f"\n{'=' * 50}")
    report.append(f"🎯 候选股（两道关都过）: {len(sorted_results)}只")
    report.append(f"{'=' * 50}")

    for code, info in sorted_results:
        s = info['score']
        c = info['chanlun']
        latest = info['latest']
        ma20_now = info['ma20']
        above = (latest['close'] - ma20_now) / ma20_now * 100 if ma20_now > 0 else 0

        pf_str = '∞' if c['profit_factor'] >= 999 else f"{c['profit_factor']:.2f}"
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
        'date': now.strftime('%Y%m%d'),
        'passed': {code: {'score': info['score'], 'chanlun': info['chanlun']}
                   for code, info in chanlun_results.items()},
    }
    atomic_json_dump(cache_data, cache_file)

    # 单独存备选池（含价格，供模拟盘用）
    backup_file = os.path.expanduser('~/.hermes/cache/backup_pool.json')
    backup_data = []
    for code, info in chanlun_results.items():
        latest = info['latest']
        backup_data.append({
            'code': code,
            'name': info['score']['name'],
            'date': now.strftime('%Y%m%d'),
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
    history.append({'date': now.strftime('%Y%m%d'), 'stocks': backup_data})
    history = history[-60:]  # 只保留最近60天
    atomic_json_dump(history, history_file)

    return "\n".join(report)

if __name__ == '__main__':
    print(run_screening())
