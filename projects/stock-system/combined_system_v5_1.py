#!/usr/bin/env python3
"""
综合选股系统 v5.1 — 保留main()用于独立回测验证
入场/出场/回测逻辑已移至 chanlun_strategy.py 公共模块
"""
import sys, os, json, time, random
sys.path.insert(0, os.path.expanduser('~/.hermes/scripts'))
from kline_cache import fetch_kline
from chanlun_strategy import (
    WINDOW, evaluate_chanlun_quality, backtest_single,
)

DATALEN = 500

def main():
    print("综合选股系统 v5.1 — 缠论先筛→评分再把关")
    print("=" * 70)

    # 获取全A列表
    print("获取股票列表...")
    import stock_scorer as ss
    try:
        import akshare as ak
        df = ak.stock_info_a_code_name()
        valid = [(c, n) for c, n in zip(df['code'].tolist(), df['name'].tolist())
                 if not c.startswith('8') and not c.startswith('9') and 'ST' not in n and '退' not in n]
    except Exception:
        print("akshare获取失败,用备选列表")
        valid = []

    # 随机选300只
    random.seed(777)
    if len(valid) > 300:
        sample = random.sample(valid, 300)
    else:
        sample = valid[:300] if valid else []

    # 股票池也加上
    pool = [('600030', '中信证券'), ('688019', '安集科技'), ('688008', '澜起科技'),
            ('600584', '长电科技'), ('300750', '宁德时代'), ('000977', '浪潮信息'),
            ('002156', '通富微电')]
    all_codes = pool + [(c, n) for c, n in sample if c not in [p[0] for p in pool]]
    print(f"共{len(all_codes)}只")

    # Step 1: 拉K线
    print(f"\n【Step 1】拉取K线...")
    klines_data = {}
    fail = 0
    for i, (code, name) in enumerate(all_codes):
        if i % 50 == 0 and i > 0:
            print(f"  进度: {i}/{len(all_codes)} (成功{len(klines_data)} 失败{fail})")
        kl = fetch_kline(code, DATALEN)
        if kl and len(kl) >= WINDOW + 100:
            klines_data[code] = (kl, name)
        else:
            fail += 1
        time.sleep(0.25)
    print(f"  完成: {len(klines_data)}只有效 (失败{fail})")

    # Step 2: 缠论历史胜率评估（第一关）
    print(f"\n【Step 2】缠论历史胜率评估（第一关）...")
    chanlun_stats = {}
    for code, (kl, name) in klines_data.items():
        stats = evaluate_chanlun_quality(kl, name, code)
        if stats:
            chanlun_stats[code] = stats
    # 统计
    grades = {}
    for code, cs in chanlun_stats.items():
        g = cs['quality']
        if g not in grades:
            grades[g] = {'count': 0, 'codes': []}
        grades[g]['count'] += 1
        grades[g]['codes'].append(code)
    print(f"  A级: {grades.get('A', {}).get('count', 0)}只  B级: {grades.get('B', {}).get('count', 0)}只  "
          f"C级: {grades.get('C', {}).get('count', 0)}只  D级: {grades.get('D', {}).get('count', 0)}只")

    MIN_QUALITY = ('A', 'B')
    passed_ch = {c: cs for c, cs in chanlun_stats.items() if cs['quality'] in MIN_QUALITY}
    print(f"  第一关通过(缠论{MIN_QUALITY}): {len(passed_ch)}只")

    # 显示通过的票
    print(f"\n  缠论A/B级票:")
    for code, cs in sorted(passed_ch.items(), key=lambda x: -x[1]['win_rate']):
        name = klines_data[code][1]
        pf_str = '∞' if cs['profit_factor'] == float('inf') else f"{cs['profit_factor']:>5.2f}"
        print(f"    {name:<8} {code} {cs['total_trades']}笔 胜率{cs['win_rate']:>5.1f}% "
              f"均收{cs['avg_return']:>+6.1f}% 盈亏比{pf_str}")

    # Step 3: stock-scorer评分（第二关）
    print(f"\n【Step 3】stock-scorer评分（第二关）...")
    scores = {}
    for i, code in enumerate(passed_ch):
        name = klines_data[code][1]
        if i % 10 == 0:
            print(f"  评分进度: {i + 1}/{len(passed_ch)}")
        try:
            r = ss.analyze([code], skip_industry=True)
            if r:
                scores[code] = r[0]
        except Exception:
            pass
        time.sleep(0.3)
    print(f"  评分完成: {len(scores)}只")

    MIN_SCORE = 50
    passed_both = {c: scores[c] for c in scores if scores[c]['total'] >= MIN_SCORE}
    print(f"  第二关通过(评分≥{MIN_SCORE}): {len(passed_both)}只")

    # 显示最终名单
    print(f"\n{'=' * 70}")
    print(f"两道关都通过: {len(passed_both)}只")
    print(f"{'=' * 70}")
    for code, s in sorted(passed_both.items(), key=lambda x: -x[1]['total']):
        cs = chanlun_stats[code]
        print(f"  {s['name']:<8} {code} 评分{s['total']:>5}({s['rating']}) "
              f"缠论{cs['total_trades']}笔/胜率{cs['win_rate']:.0f}%/{cs['quality']}级")

    # Step 4: 组合回测
    print(f"\n{'=' * 70}")
    print("回测对比: 不同筛选组合")
    print(f"{'=' * 70}")

    def run_backtest(target_codes, label):
        all_trades = []
        for code in target_codes:
            kl, name = klines_data[code]
            trades = backtest_single(kl, name, code)
            all_trades.extend(trades)
        if not all_trades:
            return None
        wins = [t for t in all_trades if t['pnl'] > 0]
        losses = [t for t in all_trades if t['pnl'] <= 0]
        wr = len(wins) / len(all_trades) * 100
        aw = sum(t['pnl'] for t in wins) / max(1, len(wins))
        al = sum(t['pnl'] for t in losses) / max(1, len(losses))
        total = sum(t['pnl'] for t in all_trades)
        pf = abs(aw / al) if al != 0 else float('inf')
        return {'label': label, 'n': len(all_trades), 'wr': round(wr, 1), 'aw': round(aw, 1),
                'al': round(al, 1), 'total': round(total, 1),
                'pf': '∞' if pf == float('inf') else round(pf, 2),
                'stocks': len(target_codes)}

    all_codes_list = list(klines_data.keys())
    ch_a = [c for c, s in chanlun_stats.items() if s['quality'] == 'A']
    ch_ab = [c for c, s in chanlun_stats.items() if s['quality'] in ('A', 'B')]
    ch_abc = [c for c, s in chanlun_stats.items() if s['quality'] in ('A', 'B', 'C')]
    score_pass = [c for c in scores if scores[c]['total'] >= MIN_SCORE]
    both_pass = list(passed_both.keys())

    configs = [
        ("全部(不过滤)", all_codes_list),
        ("缠论A级", ch_a),
        ("缠论A/B级", ch_ab),
        ("缠论A/B/C级", ch_abc),
        (f"评分≥{MIN_SCORE}", score_pass),
        ("缠论A/B+评分≥50", both_pass),
    ]

    print(f"\n{'配置':<25} {'票数':>4} {'笔数':>4} {'胜率':>5} {'盈均':>6} {'亏均':>6} {'盈亏比':>5} {'总盈亏':>7}")
    print("-" * 70)
    for label, codes in configs:
        r = run_backtest(codes, label)
        if r:
            print(f"  {r['label']:<23} {r['stocks']:>4} {r['n']:>4} {r['wr']:>4.0f}% {r['aw']:>+5.1f} {r['al']:>+5.1f} {str(r['pf']):>5} {r['total']:>+6.1f}%")
        else:
            print(f"  {label:<23}  无交易")

    # Step 5: 缠论先筛 vs 评分先筛
    print(f"\n{'=' * 70}")
    print("缠论先筛 vs 评分先筛 对比")
    print(f"{'=' * 70}")

    r1 = run_backtest(both_pass, "缠论先→评分后")
    score_first = [c for c in score_pass if chanlun_stats.get(c, {}).get('quality') in MIN_QUALITY]
    r2 = run_backtest(score_first, "评分先→缠论后")
    r3 = run_backtest(ch_ab, "纯缠论A/B")
    r4 = run_backtest(score_pass, "纯评分≥50")

    print(f"\n{'方案':<25} {'票数':>4} {'笔数':>4} {'胜率':>5} {'盈均':>6} {'亏均':>6} {'盈亏比':>5} {'总盈亏':>7}")
    print("-" * 70)
    for r in [r1, r2, r3, r4]:
        if r:
            print(f"  {r['label']:<23} {r['stocks']:>4} {r['n']:>4} {r['wr']:>4.0f}% {r['aw']:>+5.1f} {r['al']:>+5.1f} {str(r['pf']):>5} {r['total']:>+6.1f}%")

if __name__ == '__main__':
    main()
