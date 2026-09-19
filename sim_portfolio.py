#!/usr/bin/env python3
"""
模拟盘跟踪系统 v3 — 调用chanlun_strategy公共模块
入场：8层过滤（与回测/选股完全一致）
出场：止损-5% / 止盈+20% / 超时60天 / 卖出信号+破位
冷却：止损后5天不买

模式：
  --morning    早盘买入（9:30，只买入不卖出）
  --exit-only  尾盘检查（15:30，只卖出不买入）
  默认         完整模式（买入+卖出，向后兼容）
"""
import sys, os, json, time, argparse
sys.path.insert(0, os.path.expanduser('~/.hermes/scripts'))
from kline_db import get_klines
from chanlun_strategy import (
    WINDOW, COOLDOWN_DAYS,
    check_entry_signal, check_exit_signal, precompute,
)
from datetime import datetime

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

def supplement_klines(kl, today_klines, code):
    """如果K线最新数据不是今天，从today_klines补上"""
    today_str = datetime.now().strftime('%Y%m%d')
    if kl and kl[-1]['date'] != today_str and code in today_klines:
        kl = kl + [today_klines[code]]
    return kl

def is_yizi_limit(kl):
    """判断最新K线是否一字板（open==high==low==close，完全无波动）"""
    if not kl:
        return False
    last = kl[-1]
    return last['open'] == last['high'] == last['low'] == last['close']

CACHE_DIR = os.path.expanduser('~/.hermes/cache')
PORTFOLIO_FILE = os.path.join(CACHE_DIR, 'sim_portfolio.json')
NAV_FILE = os.path.join(CACHE_DIR, 'sim_nav.json')

INITIAL_CAPITAL = 1000000
MAX_POSITIONS = 10
POSITION_SIZE = 0.10

def load_portfolio():
    if os.path.exists(PORTFOLIO_FILE):
        try:
            with open(PORTFOLIO_FILE) as f:
                pf = json.load(f)
            # 验证必要字段
            if not isinstance(pf, dict):
                raise ValueError("格式错误：不是dict")
            for key in ('positions', 'cash', 'trades'):
                if key not in pf:
                    raise ValueError(f"缺少字段: {key}")
            if 'used_pivots' not in pf:
                pf['used_pivots'] = []
            if 'cooldown_until' not in pf:
                pf['cooldown_until'] = None
            return pf
        except (json.JSONDecodeError, Exception) as e:
            print(f"  ⚠️ 模拟盘数据损坏，使用默认值: {e}", file=sys.stderr)
    return {'positions': [], 'cash': INITIAL_CAPITAL, 'trades': [],
            'used_pivots': [], 'cooldown_until': None}

def save_portfolio(pf):
    import tempfile
    fd, tmp_path = tempfile.mkstemp(dir=os.path.dirname(PORTFOLIO_FILE), suffix='.tmp')
    try:
        with os.fdopen(fd, 'w') as f:
            json.dump(pf, f, ensure_ascii=False, indent=2)
        os.replace(tmp_path, PORTFOLIO_FILE)
    except Exception:
        try: os.unlink(tmp_path)
        except Exception: pass
        raise

def load_nav():
    if os.path.exists(NAV_FILE):
        try:
            with open(NAV_FILE) as f:
                return json.load(f)
        except (json.JSONDecodeError, Exception):
            pass
    return []

def save_nav(nav):
    import tempfile
    fd, tmp_path = tempfile.mkstemp(dir=os.path.dirname(NAV_FILE), suffix='.tmp')
    try:
        with os.fdopen(fd, 'w') as f:
            json.dump(nav, f, ensure_ascii=False)
        os.replace(tmp_path, NAV_FILE)
    except Exception:
        try: os.unlink(tmp_path)
        except Exception: pass
        raise

def load_backup_pool():
    path = os.path.join(CACHE_DIR, 'backup_pool.json')
    if not os.path.exists(path):
        return []
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return []

def do_exits(pf, today, now, report, today_klines=None):
    """检查持仓出场，返回 (sold_list, updated_positions)"""
    sold = []
    remaining = []
    stuck = []  # 一字跌停卖不出
    for pos in pf['positions']:
        # T+1：当天买入的票不检查出场
        if pos['entry_date'] == today:
            remaining.append(pos)
            continue

        if 'kline_fail_count' not in pos:
            pos['kline_fail_count'] = 0
        
        kl = get_klines(pos['code'], 500)

        # 补充当天K线（数据库18点才更新，15:30收盘后需要从Tushare拉）
        if today_klines is not None:
            kl = supplement_klines(kl, today_klines, pos['code'])
        
        if not kl or len(kl) < WINDOW + 10:
            pos['kline_fail_count'] += 1
            pos['current_price'] = pos['entry_price']
            pos['market_value'] = pos['shares'] * pos['current_price']
            if pos['kline_fail_count'] >= 5:
                pos['exit_date'] = today
                pos['exit_price'] = pos['entry_price']
                pos['exit_reason'] = '数据异常'
                pf['cash'] += pos['shares'] * pos['entry_price']
                pf['trades'].append({
                    'code': pos['code'], 'name': pos['name'],
                    'entry_date': pos['entry_date'], 'entry_price': pos['entry_price'],
                    'exit_date': today, 'exit_price': pos['entry_price'],
                    'pnl_pct': 0, 'reason': '数据异常',
                    'hold_days': pos['hold_days'],
                })
                sold.append(pos)
                continue
            remaining.append(pos)
            continue
        
        pos['kline_fail_count'] = 0

        if pos.get('last_update') != today:
            kline_dates = {d['date'] for d in kl}
            if today in kline_dates:
                pos['hold_days'] = pos.get('hold_days', 0) + 1
            pos['last_update'] = today

        pos['current_price'] = kl[-1]['close']
        pnl_pct = (pos['current_price'] - pos['entry_price']) / pos['entry_price']
        pos['pnl_pct'] = round(pnl_pct * 100, 2)
        pos['market_value'] = pos['shares'] * pos['current_price']

        _pc = precompute(kl)
        should_exit, reason = check_exit_signal(
            kl, pos['entry_price'], pos['hold_days'], pos['name'], pos['code'], _pc=_pc)

        if should_exit:
            # 一字跌停：实际无法卖出，继续持有
            if is_yizi_limit(kl):
                stuck.append({
                    'code': pos['code'], 'name': pos['name'],
                    'entry_date': pos['entry_date'], 'entry_price': pos['entry_price'],
                    'current_price': pos['current_price'], 'reason': reason,
                    'hold_days': pos['hold_days'],
                })
                remaining.append(pos)
                continue

            pos['exit_date'] = today
            if reason == '止损':
                from chanlun_strategy import STOP_LOSS
                pos['exit_price'] = pos['entry_price'] * (1 + STOP_LOSS)
            else:
                pos['exit_price'] = pos['current_price']
            pos['exit_reason'] = reason
            if reason == '止损':
                pf['cash'] += pos['shares'] * pos['exit_price']
            else:
                pf['cash'] += pos['market_value']
            pf['trades'].append({
                'code': pos['code'], 'name': pos['name'],
                'entry_date': pos['entry_date'], 'entry_price': pos['entry_price'],
                'exit_date': today, 'exit_price': pos['exit_price'],
                'pnl_pct': round((pos['exit_price'] - pos['entry_price']) / pos['entry_price'] * 100, 2),
                'reason': reason,
                'hold_days': pos['hold_days'],
            })
            sold.append(pos)
            if reason == '止损':
                today_idx = None
                for idx, bar in enumerate(kl):
                    if bar['date'] >= today:
                        today_idx = idx
                        break
                if today_idx is not None:
                    cd_idx = min(today_idx + COOLDOWN_DAYS, len(kl) - 1)
                    pf['cooldown_until'] = kl[cd_idx]['date']
                else:
                    from datetime import timedelta
                    pf['cooldown_until'] = (now + timedelta(days=COOLDOWN_DAYS + 2)).strftime('%Y%m%d')
            pivot_zg = pos.get('pivot_zg')
            if pivot_zg is not None:
                pivot_key = f"{pos['code']}_{round(pivot_zg, 1)}"
                if pivot_key in pf.get('used_pivots', []):
                    pf['used_pivots'].remove(pivot_key)
        else:
            remaining.append(pos)

    return sold, remaining, stuck

def do_buys(pf, pool, today, now, report, today_klines=None):
    """从备选池买入，返回 bought_list"""
    bought = []
    skipped = []  # 一字涨停买不到
    total_mv = sum(p['market_value'] for p in pf['positions'])
    current_nav = pf['cash'] + total_mv
    if len(pf['positions']) >= MAX_POSITIONS:
        return bought

    in_cooldown = False
    if pf.get('cooldown_until') and today <= pf['cooldown_until']:
        in_cooldown = True
        report.append(f"\n⚠️ 冷却期中（止损后{COOLDOWN_DAYS}天），{pf['cooldown_until']}后恢复")

    if in_cooldown:
        return bought

    quality_order = {'A': 0, 'B': 1, 'C': 2, 'D': 3}
    sorted_pool = sorted(pool, key=lambda x: (
        quality_order.get(x.get('chanlun_quality', 'D'), 3),
        -x.get('chanlun_wr', 0),
        -x.get('score', 0),
    ))
    for stock in sorted_pool:
        if len(pf['positions']) >= MAX_POSITIONS:
            break
        code = stock['code']
        if any(p['code'] == code for p in pf['positions']):
            continue

        kl = get_klines(code, 500)
        if not kl:
            continue

        # 补充当天K线
        if today_klines is not None:
            kl = supplement_klines(kl, today_klines, code)

        ok, sig = check_entry_signal(kl, stock['name'], code)
        if not ok or not sig:
            continue

        # 一字涨停：实际无法买入，跳过
        if is_yizi_limit(kl):
            skipped.append({
                'code': code, 'name': stock['name'],
                'price': sig['price'], 'zg': sig['zg'],
                'chanlun_quality': stock.get('chanlun_quality', '?'),
            })
            continue

        zg = sig['zg']
        pivot_key = f"{code}_{round(zg, 1)}"
        if pivot_key in pf.get('used_pivots', []):
            continue

        position_value = current_nav * POSITION_SIZE
        shares = int(position_value / sig['price'] / 100) * 100
        if shares <= 0:
            continue
        cost = shares * sig['price']
        if cost > pf['cash']:
            continue

        pf['cash'] -= cost
        new_pos = {
            'code': code,
            'name': stock['name'],
            'entry_date': today,
            'entry_price': sig['price'],
            'current_price': sig['price'],
            'shares': shares,
            'cost': cost,
            'hold_days': 0,
            'pnl_pct': 0,
            'market_value': cost,
            'pivot_zg': zg,
            'last_update': today,
        }
        pf['positions'].append(new_pos)
        bought.append(new_pos)
        current_nav = pf['cash'] + sum(p['market_value'] for p in pf['positions'])
        if 'used_pivots' not in pf:
            pf['used_pivots'] = []
        pf['used_pivots'].append(pivot_key)

    return bought, skipped

def run_morning():
    """早盘买入模式（9:30）— 只买入，不检查出场"""
    now = datetime.now()
    today = now.strftime('%Y%m%d')
    report = []
    report.append(f"📊 模拟盘·早盘买入 {today}")
    report.append(f"规则: 三买8层过滤入场 | 100万×10只×10%")
    report.append("=" * 50)

    pf = load_portfolio()
    nav_history = load_nav()
    pool = load_backup_pool()

    # 批量拉取当天K线（备选池+持仓），一次API调用搞定
    all_codes = list(set([s['code'] for s in pool] + [p['code'] for p in pf['positions']]))
    today_klines = fetch_today_klines(all_codes) if all_codes else {}
    if today_klines:
        report.append(f"  Tushare补充{len(today_klines)}只当天K线")

    if not pool:
        report.append("\n备选池为空（前夜选股无结果），无新买入。")
        # 仍输出账户状态
        total_mv = sum(p['market_value'] for p in pf['positions'])
        nav = pf['cash'] + total_mv
        nav_pct = (nav - INITIAL_CAPITAL) / INITIAL_CAPITAL * 100
        report.append(f"\n💰 账户:")
        report.append(f"  总资产: ¥{nav:,.0f} ({nav_pct:+.1f}%)")
        report.append(f"  持仓数: {len(pf['positions'])}/{MAX_POSITIONS}")
        return "\n".join(report)

    # 买入
    bought, skipped = do_buys(pf, pool, today, now, report, today_klines=today_klines)

    if bought:
        report.append(f"\n📥 早盘买入 {len(bought)}只:")
        for b in bought:
            report.append(f"  {b['name']}({b['code']}) ¥{b['entry_price']:.2f} {b['shares']}股 ZG={b.get('pivot_zg', '?')}")
    else:
        report.append("\n早盘无新买入（条件不满足或仓位已满）。")

    if skipped:
        report.append(f"\n🚫 一字涨停无法买入 {len(skipped)}只:")
        for s in skipped:
            report.append(f"  {s['name']}({s['code']}) ¥{s['price']:.2f} ZG={s['zg']:.1f} [{s['chanlun_quality']}级]")

    # 更新净值
    total_mv = sum(p['market_value'] for p in pf['positions'])
    nav = pf['cash'] + total_mv
    nav_pct = (nav - INITIAL_CAPITAL) / INITIAL_CAPITAL * 100

    nav_entry = {'date': today, 'nav': round(nav, 2), 'nav_pct': round(nav_pct, 2)}
    if nav_history and nav_history[-1]['date'] == today:
        nav_history[-1] = nav_entry
    else:
        nav_history.append(nav_entry)
    nav_history = nav_history[-120:]
    save_nav(nav_history)
    save_portfolio(pf)

    report.append(f"\n💰 账户:")
    report.append(f"  总资产: ¥{nav:,.0f} ({nav_pct:+.1f}%)")
    report.append(f"  现金: ¥{pf['cash']:,.0f}")
    report.append(f"  持仓数: {len(pf['positions'])}/{MAX_POSITIONS}")

    if pf['positions']:
        report.append(f"\n📋 持仓明细:")
        for p in pf['positions']:
            report.append(f"  {p['name']}({p['code']}) {p['shares']}股 "
                         f"买{p['entry_price']:.2f}→现{p['current_price']:.2f} "
                         f"{p['pnl_pct']:+.1f}% 持{p['hold_days']}天")

    return "\n".join(report)

def run_exit_only():
    """尾盘检查模式（15:30）— 只检查出场，不买入"""
    now = datetime.now()
    today = now.strftime('%Y%m%d')
    report = []
    report.append(f"📊 模拟盘·尾盘检查 {today}")
    report.append(f"规则: 止盈+20%/止损-5%/超时60天 | 100万×10只×10%")
    report.append("=" * 50)

    pf = load_portfolio()
    nav_history = load_nav()

    # 批量拉取当天K线（仅持仓票）
    all_codes = [p['code'] for p in pf['positions']]
    today_klines = fetch_today_klines(all_codes) if all_codes else {}
    if today_klines:
        report.append(f"  Tushare补充{len(today_klines)}只当天K线")

    # 1. 检查出场
    sold, remaining, stuck = do_exits(pf, today, now, report, today_klines=today_klines)
    pf['positions'] = remaining

    # 2. 计算净值
    total_mv = sum(p['market_value'] for p in pf['positions'])
    nav = pf['cash'] + total_mv
    nav_pct = (nav - INITIAL_CAPITAL) / INITIAL_CAPITAL * 100

    nav_entry = {'date': today, 'nav': round(nav, 2), 'nav_pct': round(nav_pct, 2)}
    if nav_history and nav_history[-1]['date'] == today:
        nav_history[-1] = nav_entry
    else:
        nav_history.append(nav_entry)
    nav_history = nav_history[-120:]
    save_nav(nav_history)
    save_portfolio(pf)

    # 3. 输出
    if sold:
        report.append(f"\n📤 卖出 {len(sold)}只:")
        for s in sold:
            report.append(f"  {s['name']}({s['code']}) {s['entry_date']}→{s['exit_date']} "
                         f"买{s['entry_price']:.2f}→卖{s['exit_price']:.2f} {s['pnl_pct']:+.1f}% [{s['exit_reason']}]")
    else:
        report.append("\n无卖出。")

    if stuck:
        report.append(f"\n🔒 一字跌停无法卖出 {len(stuck)}只（继续持有）:")
        for s in stuck:
            pnl = (s['current_price'] - s['entry_price']) / s['entry_price'] * 100
            report.append(f"  {s['name']}({s['code']}) 买{s['entry_price']:.2f}→现{s['current_price']:.2f} "
                         f"{pnl:+.1f}% 触发{s['reason']} 持{s['hold_days']}天")

    report.append(f"\n💰 账户:")
    report.append(f"  总资产: ¥{nav:,.0f} ({nav_pct:+.1f}%)")
    report.append(f"  现金: ¥{pf['cash']:,.0f}")
    report.append(f"  持仓市值: ¥{total_mv:,.0f}")
    report.append(f"  持仓数: {len(pf['positions'])}/{MAX_POSITIONS}")

    if pf['positions']:
        report.append(f"\n📋 持仓明细:")
        for p in pf['positions']:
            report.append(f"  {p['name']}({p['code']}) {p['shares']}股 "
                         f"买{p['entry_price']:.2f}→现{p['current_price']:.2f} "
                         f"{p['pnl_pct']:+.1f}% 持{p['hold_days']}天")

    if pf['trades']:
        wins = [t for t in pf['trades'] if t['pnl_pct'] > 0]
        report.append(f"\n📈 历史统计:")
        report.append(f"  总交易: {len(pf['trades'])}笔")
        report.append(f"  胜率: {len(wins)}/{len(pf['trades'])} = {len(wins) / len(pf['trades']) * 100:.0f}%")

    if len(nav_history) > 1:
        report.append(f"\n📉 净值趋势（近10天）:")
        for n in nav_history[-10:]:
            report.append(f"  {n['date']} {n['nav_pct']:+.1f}%")

    return "\n".join(report)

def run_full():
    """完整模式 — 买入+卖出（向后兼容）"""
    now = datetime.now()
    today = now.strftime('%Y%m%d')
    report = []
    report.append(f"📊 模拟盘 {today}")
    report.append(f"规则: 三买8层过滤入场 | 止盈+20%/止损-5%/超时60天 | 100万×10只×10%")
    report.append("=" * 50)

    pf = load_portfolio()
    nav_history = load_nav()
    pool = load_backup_pool()

    # 批量拉取当天K线（持仓+备选池）
    all_codes = list(set([p['code'] for p in pf['positions']] + [s['code'] for s in pool]))
    today_klines = fetch_today_klines(all_codes) if all_codes else {}
    if today_klines:
        report.append(f"  Tushare补充{len(today_klines)}只当天K线")

    # 1. 检查出场
    sold, remaining, stuck = do_exits(pf, today, now, report, today_klines=today_klines)
    pf['positions'] = remaining

    # 2. 检查买入
    bought, skipped = do_buys(pf, pool, today, now, report, today_klines=today_klines)

    # 3. 计算净值
    total_mv = sum(p['market_value'] for p in pf['positions'])
    nav = pf['cash'] + total_mv
    nav_pct = (nav - INITIAL_CAPITAL) / INITIAL_CAPITAL * 100

    nav_entry = {'date': today, 'nav': round(nav, 2), 'nav_pct': round(nav_pct, 2)}
    if nav_history and nav_history[-1]['date'] == today:
        nav_history[-1] = nav_entry
    else:
        nav_history.append(nav_entry)
    nav_history = nav_history[-120:]
    save_nav(nav_history)
    save_portfolio(pf)

    # 4. 输出
    if sold:
        report.append(f"\n📤 卖出 {len(sold)}只:")
        for s in sold:
            report.append(f"  {s['name']}({s['code']}) {s['entry_date']}→{s['exit_date']} "
                         f"买{s['entry_price']:.2f}→卖{s['exit_price']:.2f} {s['pnl_pct']:+.1f}% [{s['exit_reason']}]")

    if bought:
        report.append(f"\n📥 买入 {len(bought)}只:")
        for b in bought:
            report.append(f"  {b['name']}({b['code']}) ¥{b['entry_price']:.2f} {b['shares']}股 ZG={b.get('pivot_zg', '?')}")

    if stuck:
        report.append(f"\n🔒 一字跌停无法卖出 {len(stuck)}只（继续持有）:")
        for s in stuck:
            pnl = (s['current_price'] - s['entry_price']) / s['entry_price'] * 100
            report.append(f"  {s['name']}({s['code']}) 买{s['entry_price']:.2f}→现{s['current_price']:.2f} "
                         f"{pnl:+.1f}% 触发{s['reason']} 持{s['hold_days']}天")

    if skipped:
        report.append(f"\n🚫 一字涨停无法买入 {len(skipped)}只:")
        for s in skipped:
            report.append(f"  {s['name']}({s['code']}) ¥{s['price']:.2f} ZG={s['zg']:.1f} [{s['chanlun_quality']}级]")

    report.append(f"\n💰 账户:")
    report.append(f"  总资产: ¥{nav:,.0f} ({nav_pct:+.1f}%)")
    report.append(f"  现金: ¥{pf['cash']:,.0f}")
    report.append(f"  持仓市值: ¥{total_mv:,.0f}")
    report.append(f"  持仓数: {len(pf['positions'])}/{MAX_POSITIONS}")

    if pf['positions']:
        report.append(f"\n📋 持仓明细:")
        for p in pf['positions']:
            report.append(f"  {p['name']}({p['code']}) {p['shares']}股 "
                         f"买{p['entry_price']:.2f}→现{p['current_price']:.2f} "
                         f"{p['pnl_pct']:+.1f}% 持{p['hold_days']}天")

    if pf['trades']:
        wins = [t for t in pf['trades'] if t['pnl_pct'] > 0]
        report.append(f"\n📈 历史统计:")
        report.append(f"  总交易: {len(pf['trades'])}笔")
        report.append(f"  胜率: {len(wins)}/{len(pf['trades'])} = {len(wins) / len(pf['trades']) * 100:.0f}%")

    if len(nav_history) > 1:
        report.append(f"\n📉 净值趋势（近10天）:")
        for n in nav_history[-10:]:
            report.append(f"  {n['date']} {n['nav_pct']:+.1f}%")

    return "\n".join(report)

def run_summary():
    """只读总结模式 — 不操作，只输出当前状态"""
    now = datetime.now()
    today = now.strftime('%Y%m%d')
    report = []
    report.append(f"📊 模拟盘·日报总结 {today}")
    report.append("=" * 50)

    pf = load_portfolio()
    nav_history = load_nav()

    total_mv = sum(p['market_value'] for p in pf['positions'])
    nav = pf['cash'] + total_mv
    nav_pct = (nav - INITIAL_CAPITAL) / INITIAL_CAPITAL * 100

    report.append(f"\n💰 账户:")
    report.append(f"  总资产: ¥{nav:,.0f} ({nav_pct:+.1f}%)")
    report.append(f"  现金: ¥{pf['cash']:,.0f}")
    report.append(f"  持仓市值: ¥{total_mv:,.0f}")
    report.append(f"  持仓数: {len(pf['positions'])}/{MAX_POSITIONS}")

    if pf['positions']:
        report.append(f"\n📋 持仓明细:")
        for p in pf['positions']:
            report.append(f"  {p['name']}({p['code']}) {p['shares']}股 "
                         f"买{p['entry_price']:.2f}→现{p['current_price']:.2f} "
                         f"{p['pnl_pct']:+.1f}% 持{p['hold_days']}天")

    if pf['trades']:
        wins = [t for t in pf['trades'] if t['pnl_pct'] > 0]
        report.append(f"\n📈 历史统计:")
        report.append(f"  总交易: {len(pf['trades'])}笔")
        report.append(f"  胜率: {len(wins)}/{len(pf['trades'])} = {len(wins) / len(pf['trades']) * 100:.0f}%")

    if len(nav_history) > 1:
        report.append(f"\n📉 净值趋势（近10天）:")
        for n in nav_history[-10:]:
            report.append(f"  {n['date']} {n['nav_pct']:+.1f}%")

    return "\n".join(report)

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='模拟盘跟踪系统')
    group = parser.add_mutually_exclusive_group()
    group.add_argument('--morning', action='store_true', help='早盘买入模式（只买入不卖出）')
    group.add_argument('--exit-only', action='store_true', help='尾盘检查模式（只卖出不买入）')
    group.add_argument('--summary', action='store_true', help='只读总结模式（不操作，只输出当前状态）')
    args = parser.parse_args()

    if args.morning:
        print(run_morning())
    elif args.exit_only:
        print(run_exit_only())
    elif args.summary:
        print(run_summary())
    else:
        print(run_full())
