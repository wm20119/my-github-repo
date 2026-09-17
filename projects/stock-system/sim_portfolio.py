#!/usr/bin/env python3
"""
模拟盘跟踪系统 v3 — 调用chanlun_strategy公共模块
入场：8层过滤（与回测/选股完全一致）
出场：止损-5% / 止盈+20% / 超时60天 / 卖出信号+破位
冷却：止损后5天不买
"""
import sys, os, json, time
sys.path.insert(0, os.path.expanduser('~/.hermes/scripts'))
from kline_cache import fetch_kline
from chanlun_strategy import (
    WINDOW, COOLDOWN_DAYS,
    check_entry_signal, check_exit_signal, precompute,
)
from datetime import datetime

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
                return json.load(f)
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

def run():
    now = datetime.now()
    today = now.strftime('%Y-%m-%d')
    report = []
    report.append(f"📊 模拟盘 {today}")
    report.append(f"规则: 三买8层过滤入场 | 止盈+20%/止损-5%/超时60天 | 100万×10只×10%")
    report.append("=" * 50)

    pf = load_portfolio()
    nav_history = load_nav()
    pool = load_backup_pool()

    # 1. 更新持仓现价，检查出场
    sold = []
    remaining = []
    for pos in pf['positions']:
        # K线获取失败计数
        if 'kline_fail_count' not in pos:
            pos['kline_fail_count'] = 0
        
        kl = fetch_kline(pos['code'], 500)
        time.sleep(0.3)
        
        if not kl or len(kl) < WINDOW + 10:
            pos['kline_fail_count'] += 1
            pos['current_price'] = pos['entry_price']  # 保持旧价格
            pos['market_value'] = pos['shares'] * pos['current_price']  # 同步更新市值
            # 连续失败5次强制退出（约5天无法获取数据）
            if pos['kline_fail_count'] >= 5:
                pos['exit_date'] = today
                pos['exit_price'] = pos['entry_price']
                pos['exit_reason'] = '数据异常'
                # 用 shares * entry_price 计算退还资金，与 exit_price 一致
                # 不能用 market_value，因为那是上次成功获取K线时的过时市值
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
        
        pos['kline_fail_count'] = 0  # 成功获取，重置失败计数

        # 计算持仓天数（仅交易日递增，用K线判断是否交易日，兼容节假日）
        if pos.get('last_update') != today:
            kline_dates = {d['date'] for d in kl}
            if today in kline_dates:
                pos['hold_days'] = pos.get('hold_days', 0) + 1
            pos['last_update'] = today

        pos['current_price'] = kl[-1]['close']
        pnl_pct = (pos['current_price'] - pos['entry_price']) / pos['entry_price']
        pos['pnl_pct'] = round(pnl_pct * 100, 2)
        pos['market_value'] = pos['shares'] * pos['current_price']

        # 预计算指标，复用避免重复计算
        _pc = precompute(kl)
        should_exit, reason = check_exit_signal(
            kl, pos['entry_price'], pos['hold_days'], pos['name'], pos['code'], _pc=_pc)

        if should_exit:
            pos['exit_date'] = today
            # 止损用触发价（entry*1.05），其他用收盘价
            if reason == '止损':
                from chanlun_strategy import STOP_LOSS
                pos['exit_price'] = pos['entry_price'] * (1 + STOP_LOSS)
            else:
                pos['exit_price'] = pos['current_price']
            pos['exit_reason'] = reason
            # 止损用触发价计算回收资金，其他用市价
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
            # 止损冷却（使用交易日，与回测一致）
            if reason == '止损':
                # 复用已拉取的K线数据计算冷却到期日（避免冗余API调用）
                today_idx = None
                for idx, bar in enumerate(kl):
                    if bar['date'] >= today:
                        today_idx = idx
                        break
                if today_idx is not None:
                    cd_idx = min(today_idx + COOLDOWN_DAYS, len(kl) - 1)
                    pf['cooldown_until'] = kl[cd_idx]['date']
                else:
                    # 找不到日期时fallback到日历天
                    from datetime import timedelta
                    pf['cooldown_until'] = (now + timedelta(days=COOLDOWN_DAYS + 2)).strftime('%Y-%m-%d')
            # 清除去重记录（检查pivot_zg是否存在）
            pivot_zg = pos.get('pivot_zg')
            if pivot_zg is not None:
                pivot_key = f"{pos['code']}_{round(pivot_zg, 1)}"
                if pivot_key in pf.get('used_pivots', []):
                    pf['used_pivots'].remove(pivot_key)
        else:
            remaining.append(pos)

    pf['positions'] = remaining

    # 2. 检查买入
    bought = []
    # 计算当前总资产用于仓位计算
    total_mv = sum(p['market_value'] for p in pf['positions'])
    current_nav = pf['cash'] + total_mv
    if len(pf['positions']) < MAX_POSITIONS:
        in_cooldown = False
        if pf.get('cooldown_until') and today <= pf['cooldown_until']:
            in_cooldown = True
            report.append(f"\n⚠️ 冷却期中（止损后{COOLDOWN_DAYS}天），{pf['cooldown_until']}后恢复")

        if not in_cooldown:
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

                kl = fetch_kline(code, 500)
                time.sleep(0.3)
                if not kl:
                    continue

                ok, sig = check_entry_signal(kl, stock['name'], code)
                if not ok or not sig:
                    continue

                # 同中枢去重
                zg = sig['zg']
                pivot_key = f"{code}_{round(zg, 1)}"
                if pivot_key in pf.get('used_pivots', []):
                    continue

                # 计算买入（基于当前净值）
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
                # 买入后重新计算NAV，确保后续仓位大小基于递减的净值
                current_nav = pf['cash'] + sum(p['market_value'] for p in pf['positions'])
                if 'used_pivots' not in pf:
                    pf['used_pivots'] = []
                pf['used_pivots'].append(pivot_key)

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
    print(run())
