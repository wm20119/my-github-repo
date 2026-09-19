#!/usr/bin/env python3
"""
模拟盘核心模块 — 持仓管理 + 买卖逻辑
入场：8层过滤（与回测/选股完全一致）
出场：止损-5% / 止盈+20% / 超时60天 / 卖出信号+破位
冷却：止损后5天不买

调用方：stock_portfolio_task.py
"""
import os, json, sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'strategy'))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'data'))
from kline_db import get_klines
from chanlun_strategy import (
    WINDOW, COOLDOWN_DAYS,
    check_entry_signal, check_exit_signal, precompute,
)
from datetime import datetime

# ============================================================
# 常量
# ============================================================
CACHE_DIR = os.path.expanduser('~/.hermes/cache')
PORTFOLIO_FILE = os.path.join(CACHE_DIR, 'sim_portfolio.json')
NAV_FILE = os.path.join(CACHE_DIR, 'sim_nav.json')

INITIAL_CAPITAL = 1000000
MAX_POSITIONS = 10
POSITION_SIZE = 0.10

# ============================================================
# 工具函数
# ============================================================
def is_yizi_limit(kl):
    """判断最新K线是否一字板（open==high==low==close，完全无波动）"""
    if not kl:
        return False
    last = kl[-1]
    return last['open'] == last['high'] == last['low'] == last['close']

def supplement_klines(kl, today_klines, code):
    """如果K线最新数据不是今天，从today_klines补上"""
    today_str = datetime.now().strftime('%Y%m%d')
    if kl and kl[-1]['date'] != today_str and code in today_klines:
        kl = kl + [today_klines[code]]
    return kl

# ============================================================
# 持仓读写
# ============================================================
def load_portfolio():
    if os.path.exists(PORTFOLIO_FILE):
        try:
            with open(PORTFOLIO_FILE) as f:
                pf = json.load(f)
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

# ============================================================
# 买卖逻辑
# ============================================================
def do_exits(pf, today, now, report, today_klines=None):
    """检查持仓出场，返回 (sold_list, remaining_positions, stuck_list)"""
    sold = []
    remaining = []
    stuck = []
    for pos in pf['positions']:
        if pos['entry_date'] == today:
            remaining.append(pos)
            continue

        if 'kline_fail_count' not in pos:
            pos['kline_fail_count'] = 0

        kl = get_klines(pos['code'], 500)

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
    """从备选池买入，返回 (bought_list, skipped_list)"""
    bought = []
    skipped = []
    total_mv = sum(p['market_value'] for p in pf['positions'])
    current_nav = pf['cash'] + total_mv
    if len(pf['positions']) >= MAX_POSITIONS:
        return bought, skipped

    in_cooldown = False
    if pf.get('cooldown_until') and today <= pf['cooldown_until']:
        in_cooldown = True
        report.append(f"\n⚠️ 冷却期中（止损后{COOLDOWN_DAYS}天），{pf['cooldown_until']}后恢复")

    if in_cooldown:
        return bought, skipped

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

        if today_klines is not None:
            kl = supplement_klines(kl, today_klines, code)

        ok, sig = check_entry_signal(kl, stock['name'], code)
        if not ok or not sig:
            continue

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
