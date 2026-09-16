#!/usr/bin/env python3
"""
缠论策略公共模块 v1.1
所有股票脚本共用的入场/出场/冷却/回测逻辑，改一处全生效。

常量：
  WINDOW=180, TAKE_PROFIT=+8%, STOP_LOSS=-5%, MAX_HOLD=60天, COOLDOWN=5天

函数：
  calc_rsi            - RSI计算
  precompute          - 预计算MACD/MA20等（回测/扫描用，避免重复计算）
  evaluate_chanlun_quality - 缠论历史胜率评估（回测）
  check_entry_signal  - 入场8条件判断
  check_exit_signal   - 出场4条件判断
  scan_recent_signals - 扫描最近N天信号
  backtest_single     - 单票回测
"""
import sys, os
sys.path.insert(0, os.path.expanduser('~/.hermes/scripts'))
import chanlun_engine as ce

# ============================================================
# 常量
# ============================================================
WINDOW = 180
TAKE_PROFIT = 0.08     # 止盈+8%
STOP_LOSS = -0.05      # 止损-5%
MAX_HOLD_DAYS = 60     # 最长持仓60天
COOLDOWN_DAYS = 5      # 止损后冷却5天

# ============================================================
# RSI计算
# ============================================================
def calc_rsi(closes, p=14):
    if len(closes) < p + 1:
        return [50] * len(closes)
    rsi = [50] * p
    gains = []; losses = []
    for i in range(1, len(closes)):
        c = closes[i] - closes[i - 1]
        gains.append(max(c, 0))
        losses.append(max(-c, 0))
    ag = sum(gains[:p]) / p
    al = sum(losses[:p]) / p
    for i in range(p, len(gains)):
        ag = (ag * (p - 1) + gains[i]) / p
        al = (al * (p - 1) + losses[i]) / p
        if al > 0:
            rs = ag / al
            rsi.append(100 - 100 / (1 + rs))
        elif ag > 0:
            rsi.append(100)  # 全涨无跌
        else:
            rsi.append(50)   # 无变化
    return [50] + rsi

# ============================================================
# 预计算（回测/扫描用，避免循环内重复计算）
# ============================================================
def precompute(klines):
    """
    预计算MACD、MA20等指标，返回dict供check_entry/exit_signal复用。
    """
    dates = [d['date'] for d in klines]
    closes = [d['close'] for d in klines]
    highs = [d['high'] for d in klines]
    lows = [d['low'] for d in klines]
    volumes = [d['volume'] for d in klines]
    _, _, macd_all = ce.calc_macd(closes)
    ma20 = [sum(closes[max(0, i - 19):i + 1]) / min(20, i + 1) for i in range(len(closes))]
    vol_ma10 = [sum(volumes[max(0, i - 9):i + 1]) / min(10, i + 1) for i in range(len(volumes))]
    return {
        'dates': dates, 'closes': closes, 'highs': highs,
        'lows': lows, 'volumes': volumes,
        'macd_all': macd_all, 'ma20': ma20, 'vol_ma10': vol_ma10,
    }

# ============================================================
# 入场条件（8层过滤）
# ============================================================
def check_entry_signal(klines, name, code, bar_index=None, _pc=None):
    """
    检查指定bar是否满足入场条件。
    bar_index: 默认最后一条K线。
    _pc: 预计算dict（可选，避免重复计算）。
    返回: (bool, signal_dict|None)
    """
    if bar_index is None:
        bar_index = len(klines) - 1
    i = bar_index

    if len(klines) < WINDOW + 10 or i < WINDOW or i >= len(klines):
        return False, None

    # 用预计算数据或现场计算
    if _pc is not None:
        closes = _pc['closes']
        macd_all = _pc['macd_all']
        ma20 = _pc['ma20']
        dates = _pc['dates']
    else:
        dates = [d['date'] for d in klines]
        closes = [d['close'] for d in klines]
        _, _, macd_all = ce.calc_macd(closes)
        ma20 = [sum(closes[max(0, idx - 19):idx + 1]) / min(20, idx + 1)
                for idx in range(len(closes))]

    window = klines[i - WINDOW:i]
    if len(window) < 30:
        return False, None

    try:
        r = ce.full_analysis(window, name, code)
    except Exception:
        return False, None
    if not r:
        return False, None

    bs = [bp['type'] for bp in r['buy_sell_points'] if bp['direction'] == 'buy']
    p = klines[i]['close']
    cl = [d['close'] for d in window]

    # 条件1: 第三类买点
    if '第三类买点' not in bs:
        return False, None
    # 条件2: score > 0
    if r['score'] <= 0:
        return False, None
    # 条件3: MACD diff > 0（统一用macd_all，与条件8一致）
    if macd_all[i] <= 0:
        return False, None
    # 条件4: 价格 >= MA20
    if p < ma20[i]:
        return False, None
    # 条件5: 有中枢
    if not r['pivots']:
        return False, None
    # 条件6: 价格 > ZG（中枢上沿）
    zg = r['pivots'][-1]['zg']
    if p <= zg:
        return False, None
    # 条件7: 价格 >= 近10天最高价
    if p < max(cl[-10:]):
        return False, None
    # 条件8: MACD柱连续3天上升
    if i < 3 or i >= len(macd_all):
        return False, None
    if not (macd_all[i] > macd_all[i - 1] > macd_all[i - 2]):
        return False, None

    return True, {
        'date': dates[i], 'price': p, 'zg': zg,
        'score': r['score'], 'diff': r['diff'], 'ma20': ma20[i],
    }

# ============================================================
# 出场条件
# ============================================================
def check_exit_signal(klines, entry_price, hold_days, name, code, bar_index=None, _pc=None):
    """
    检查是否满足出场条件。
    bar_index: 默认最后一条K线。
    _pc: 预计算dict（可选）。
    返回: (bool, reason_str|None)
    """
    if len(klines) < WINDOW + 10 or entry_price <= 0:
        return False, None
    if bar_index is not None and (bar_index < WINDOW or bar_index >= len(klines)):
        return False, None

    i = bar_index if bar_index is not None else len(klines) - 1
    p = klines[i]['close']
    lo = klines[i]['low']

    # 止损（用low价格）
    pnl = (p - entry_price) / entry_price
    low_pnl = (lo - entry_price) / entry_price
    if low_pnl <= STOP_LOSS or pnl <= STOP_LOSS:
        return True, '止损'
    # 止盈
    if pnl >= TAKE_PROFIT:
        return True, '止盈'
    # 超时
    if hold_days >= MAX_HOLD_DAYS:
        return True, '超时'

    # 卖出信号 + 破位
    window = klines[i - WINDOW:i]
    if len(window) >= 30:
        try:
            r = ce.full_analysis(window, name, code)
            if r:
                ss = [bp['type'] for bp in r['buy_sell_points'] if bp['direction'] == 'sell']
                for s in r['signals']:
                    if '上涨趋势背驰' in s:
                        ss.append('上涨背驰')
                if ss:
                    if _pc is not None:
                        ma20_val = _pc['ma20'][i]
                    else:
                        closes = [d['close'] for d in klines]
                        ma20_val = sum(closes[max(0, i - 19):i + 1]) / min(20, i + 1)
                    if p < ma20_val * 0.98:
                        return True, '信号'
        except Exception:
            pass

    return False, None

# ============================================================
# 缠论历史胜率评估（回测用）
# ============================================================
def evaluate_chanlun_quality(klines, name, code):
    """回测历史缠论信号质量，返回胜率/盈亏比/评级"""
    if len(klines) < WINDOW + 100:
        return {'win_rate': 0, 'avg_return': 0, 'profit_factor': 0,
                'total_trades': 0, 'quality': 'D', 'trades': []}

    # 预计算一次，循环内复用
    pc = precompute(klines)
    dates = pc['dates']
    closes = pc['closes']
    macd_all = pc['macd_all']
    ma20 = pc['ma20']

    trades = []; pos = None; cd = None
    for i in range(WINDOW, len(klines)):
        td = dates[i]
        window = klines[i - WINDOW:i]
        if len(window) < 30:
            continue

        if pos is None:
            if cd and td <= cd:
                continue
            ok, _ = check_entry_signal(klines, name, code, bar_index=i, _pc=pc)
            if ok:
                pos = {'entry_date': td, 'entry_price': klines[i]['close'],
                       'hold_days': 0, 'max_price': klines[i]['close']}
        else:
            pos['hold_days'] += 1
            h = klines[i]['high']
            pos['max_price'] = max(pos['max_price'], h)
            should_exit, reason = check_exit_signal(
                klines, pos['entry_price'], pos['hold_days'], name, code, bar_index=i, _pc=pc)
            if should_exit:
                if reason == '止损':
                    pnl = STOP_LOSS  # 止损价: entry_price*(1+STOP_LOSS)
                else:
                    p = klines[i]['close']
                    pnl = (p - pos['entry_price']) / pos['entry_price']
                trades.append({'pnl': pnl * 100, 'reason': reason, 'hold': pos['hold_days']})
                if reason == '止损':
                    cd_idx = min(i + COOLDOWN_DAYS, len(dates) - 1)  # 数据末尾冷却到末尾
                    cd = dates[cd_idx]
                pos = None

    if pos:
        lp = klines[-1]['close']
        pnl = (lp - pos['entry_price']) / pos['entry_price']
        trades.append({'pnl': pnl * 100, 'reason': '到期', 'hold': pos['hold_days']})

    if len(trades) < 2:
        return {'win_rate': 0, 'avg_return': 0, 'profit_factor': 0,
                'total_trades': len(trades), 'quality': 'D', 'trades': trades}

    wins = [t for t in trades if t['pnl'] > 0]
    losses = [t for t in trades if t['pnl'] <= 0]
    wr = len(wins) / len(trades) * 100
    avg_ret = sum(t['pnl'] for t in trades) / len(trades)
    aw = sum(t['pnl'] for t in wins) / max(1, len(wins))
    al = sum(t['pnl'] for t in losses) / max(1, len(losses))
    pf = abs(aw / al) if al != 0 else float('inf')

    if len(losses) == 0 and len(wins) >= 2:
        quality = 'A'
    elif len(wins) == 0 and len(losses) >= 2:
        quality = 'D'
    elif wr >= 60 and pf >= 1.5:
        quality = 'A'
    elif wr >= 50 and pf >= 1.2:
        quality = 'B'
    elif wr >= 40:
        quality = 'C'
    else:
        quality = 'D'

    return {
        'win_rate': round(wr, 1), 'avg_return': round(avg_ret, 1),
        'profit_factor': round(pf, 2) if pf != float('inf') else 999.99,
        'total_trades': len(trades), 'quality': quality, 'trades': trades,
    }

# ============================================================
# 扫描最近N天信号
# ============================================================
def scan_recent_signals(klines, name, code, lookback=30):
    """
    扫描最近lookback根K线，返回所有三买信号。
    返回: [{'date','price','rsi','above_ma20','vol_ratio'}, ...]
    """
    if len(klines) < WINDOW + 10:
        return []

    # 预计算一次
    pc = precompute(klines)
    dates = pc['dates']
    closes = pc['closes']
    rsi_all = calc_rsi(closes)
    ma20 = pc['ma20']
    vol_ma10 = pc['vol_ma10']

    # v3.6优化：只做一次full_analysis，然后逐bar检查买卖点是否在lookback窗口内
    start_idx = max(WINDOW, len(klines) - lookback)
    recent = klines[start_idx - WINDOW:]  # 包含分析所需历史
    try:
        r = ce.full_analysis(recent, name, code)
    except Exception:
        return []
    if not r or not r['pivots'] or r['score'] <= 0 or r['diff'] <= 0:
        return []

    bs = [bp for bp in r['buy_sell_points']
          if bp['type'] == '第三类买点' and bp['direction'] == 'buy']
    if not bs:
        return []

    zg = r['pivots'][-1]['zg']
    macd_all = pc['macd_all']
    signals = []
    for i in range(start_idx, len(klines)):
        if i < 3 or i >= len(macd_all):
            continue
        p = klines[i]['close']
        cl_window = [d['close'] for d in klines[i - WINDOW:i]]
        if (len(cl_window) >= 10
                and p < ma20[i]
                and p <= zg
                and p < max(cl_window[-10:])
                and not (macd_all[i] > macd_all[i - 1] > macd_all[i - 2])):
            continue
        # 检查是否有第三类买点在此bar或之前出现
        for bp in bs:
            if p > zg and p >= max(cl_window[-10:]) and macd_all[i] > macd_all[i - 1] > macd_all[i - 2]:
                above = (p - ma20[i]) / ma20[i] * 100 if ma20[i] > 0 else 0
                vr = klines[i]['volume'] / vol_ma10[i] if vol_ma10[i] > 0 else 1
                signals.append({
                    'date': dates[i], 'price': p,
                    'rsi': rsi_all[i], 'above_ma20': above, 'vol_ratio': vr,
                })
                break
    return signals

# ============================================================
# 单票回测
# ============================================================
def backtest_single(klines, name, code, score_info=None):
    """对一只票跑缠论回测，返回交易列表"""
    if len(klines) < WINDOW + 100:
        return []

    # 预计算一次
    pc = precompute(klines)
    dates = pc['dates']

    trades = []; pos = None; cd = None
    for i in range(WINDOW, len(klines)):
        td = dates[i]
        if pos is None:
            if cd and td <= cd:
                continue
            ok, sig = check_entry_signal(klines, name, code, bar_index=i, _pc=pc)
            if ok:
                pos = {'entry_date': td, 'entry_price': sig['price'],
                       'hold_days': 0, 'max_price': sig['price']}
        else:
            pos['hold_days'] += 1
            h = klines[i]['high']
            pos['max_price'] = max(pos['max_price'], h)
            should_exit, reason = check_exit_signal(
                klines, pos['entry_price'], pos['hold_days'], name, code, bar_index=i, _pc=pc)
            if should_exit:
                if reason == '止损':
                    pnl = STOP_LOSS  # 止损价: entry_price*(1+STOP_LOSS)
                else:
                    p = klines[i]['close']
                    pnl = (p - pos['entry_price']) / pos['entry_price']
                trades.append({
                    'pnl': round(pnl * 100, 2), 'reason': reason,
                    'code': code, 'name': name,
                    'entry': pos['entry_date'], 'exit': td,
                    'hold': pos['hold_days'],
                })
                if reason == '止损':
                    cd_idx = min(i + COOLDOWN_DAYS, len(dates) - 1)  # 数据末尾冷却到末尾
                    cd = dates[cd_idx]
                pos = None

    if pos:
        lp = klines[-1]['close']
        pnl = (lp - pos['entry_price']) / pos['entry_price']
        trades.append({
            'pnl': round(pnl * 100, 2), 'reason': '到期',
            'code': code, 'name': name,
            'entry': pos['entry_date'], 'exit': dates[-1],
            'hold': pos['hold_days'],
        })
    return trades
