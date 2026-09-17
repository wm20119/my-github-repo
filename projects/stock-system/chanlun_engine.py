#!/usr/bin/env python3
"""
缠论完整分析引擎 v3.1
修复清单（v2→v3.1）：
1. 一买/一卖：必须有趋势结构（两个不重叠中枢）✅ v3
2. 三买：必须从中枢内突破+回踩确认 ✅ v3
3. 三卖：对称修复 ✅ v3
4. 背驰A段取中枢前完整走势（非仅第一个线段）✅ v3.1
5. 评分按趋势方向加权 ✅ v3.1
6. 加入成交量突破确认 ✅ v3.1
7. 卖出信号加趋势位置+力度检查 ✅ v3.1
"""

# ============================================================
# Part 1: K线包含关系处理
# ============================================================

def process_inclusion(klines):
    if len(klines) < 2:
        return klines[:]
    merged = [klines[0].copy()]
    for i in range(1, len(klines)):
        curr = klines[i]
        prev = merged[-1]
        prev_in_curr = prev['high'] <= curr['high'] and prev['low'] >= curr['low']
        curr_in_prev = curr['high'] <= prev['high'] and curr['low'] >= prev['low']
        if prev_in_curr or curr_in_prev:
            if len(merged) >= 2:
                direction = 'up' if merged[-1]['high'] >= merged[-2]['high'] else 'down'
            else:
                direction = 'up' if curr['close'] >= prev['close'] else 'down'
            if direction == 'up':
                new_high = max(prev['high'], curr['high'])
                new_low = max(prev['low'], curr['low'])
            else:
                new_high = min(prev['high'], curr['high'])
                new_low = min(prev['low'], curr['low'])
            merged[-1] = {'date': curr['date'], 'open': prev['open'], 'high': new_high,
                          'low': new_low, 'close': curr['close'], 'volume': prev['volume'] + curr['volume']}
        else:
            merged.append(curr.copy())
    return merged


# ============================================================
# Part 2: 分型识别
# ============================================================

def find_fractals(klines):
    fractals = []
    for i in range(1, len(klines) - 1):
        prev, curr, nxt = klines[i - 1], klines[i], klines[i + 1]
        if (curr['high'] > prev['high'] and curr['high'] > nxt['high'] and
                curr['low'] > prev['low'] and curr['low'] > nxt['low']):
            fractals.append({'type': 'top', 'date': curr['date'], 'price': curr['high'], 'idx': i, 'kline': curr})
        elif (curr['low'] < prev['low'] and curr['low'] < nxt['low'] and
              curr['high'] < prev['high'] and curr['high'] < nxt['high']):
            fractals.append({'type': 'bottom', 'date': curr['date'], 'price': curr['low'], 'idx': i, 'kline': curr})
    return fractals


# ============================================================
# Part 3: 严格笔构建
# ============================================================

def build_strokes(fractals, klines):
    if len(fractals) < 2:
        return []
    valid_fractals = [fractals[0]]
    for f in fractals[1:]:
        last = valid_fractals[-1]
        if f['type'] == last['type']:
            if f['type'] == 'top' and f['price'] > last['price']:
                valid_fractals[-1] = f
            elif f['type'] == 'bottom' and f['price'] < last['price']:
                valid_fractals[-1] = f
        else:
            gap = f['idx'] - last['idx']
            if gap >= 3:
                if f['type'] == 'top' and f['price'] > last['price']:
                    valid_fractals.append(f)
                elif f['type'] == 'bottom' and f['price'] < last['price']:
                    valid_fractals.append(f)
                else:
                    valid_fractals[-1] = f
    strokes = []
    for i in range(1, len(valid_fractals)):
        start = valid_fractals[i - 1]
        end = valid_fractals[i]
        if start['type'] == 'bottom' and end['type'] == 'top':
            stroke_type = 'up'
        elif start['type'] == 'top' and end['type'] == 'bottom':
            stroke_type = 'down'
        else:
            continue
        strokes.append({
            'type': stroke_type, 'start': start, 'end': end,
            'high': max(start['price'], end['price']),
            'low': min(start['price'], end['price']),
        })
    return strokes


# ============================================================
# Part 4: 线段构建（严格版）
# ============================================================

def build_segments(strokes):
    if len(strokes) < 3:
        return []
    segments = []
    seg_start = 0
    while seg_start < len(strokes) - 2:
        seg_strokes = [strokes[seg_start]]
        break_idx = -1
        for j in range(seg_start + 1, len(strokes)):
            if j >= seg_start + 3:
                s1, s2, s3 = strokes[j - 2], strokes[j - 1], strokes[j]
                if s1['type'] == 'down' and s2['type'] == 'up' and s3['type'] == 'down':
                    overlap_h = min(s1['high'], s2['high'], s3['high'])
                    overlap_l = max(s1['low'], s2['low'], s3['low'])
                    if overlap_h > overlap_l:
                        if seg_strokes[-1]['end']['price'] > seg_strokes[0]['start']['price']:
                            break_idx = j - 2
                            break
                elif s1['type'] == 'up' and s2['type'] == 'down' and s3['type'] == 'up':
                    overlap_h = min(s1['high'], s2['high'], s3['high'])
                    overlap_l = max(s1['low'], s2['low'], s3['low'])
                    if overlap_h > overlap_l:
                        if seg_strokes[-1]['end']['price'] < seg_strokes[0]['start']['price']:
                            break_idx = j - 2
                            break
            seg_strokes.append(strokes[j])
        if len(seg_strokes) >= 3:
            start_p = seg_strokes[0]['start']['price']
            end_p = seg_strokes[-1]['end']['price']
            seg_high = max(s['high'] for s in seg_strokes)
            seg_low = min(s['low'] for s in seg_strokes)
            segments.append({
                'type': 'up' if end_p > start_p else 'down',
                'strokes': seg_strokes,
                'start_date': seg_strokes[0]['start']['date'],
                'end_date': seg_strokes[-1]['end']['date'],
                'high': seg_high,
                'low': seg_low,
                'start_price': start_p,
                'end_price': end_p,
            })
        if break_idx >= 0:
            seg_start = seg_start + len(seg_strokes) - 1
        else:
            break
    return segments


# ============================================================
# Part 5: 中枢构建
# ============================================================

def find_pivots(segments):
    if len(segments) < 3:
        return []
    pivots = []
    i = 0
    while i <= len(segments) - 3:
        s1, s2, s3 = segments[i], segments[i + 1], segments[i + 2]
        overlap_high = min(s1['high'], s2['high'], s3['high'])
        overlap_low = max(s1['low'], s2['low'], s3['low'])
        if overlap_high > overlap_low:
            pivot_zg = overlap_high
            pivot_zd = overlap_low
            end_idx = i + 2
            for j in range(i + 3, len(segments)):
                seg = segments[j]
                if seg['low'] < pivot_zg and seg['high'] > pivot_zd:
                    end_idx = j
                else:
                    break
            pivots.append({
                'zg': pivot_zg, 'zd': pivot_zd,
                'gg': max(s['high'] for s in segments[i:end_idx + 1]),
                'dd': min(s['low'] for s in segments[i:end_idx + 1]),
                'segments': segments[i:end_idx + 1],
                'start_date': segments[i]['start_date'],
                'end_date': segments[end_idx]['end_date'],
                'start_idx': i, 'end_idx': end_idx,
            })
            i = end_idx + 1
        else:
            i += 1
    return pivots


# ============================================================
# Part 6: MACD计算
# ============================================================

def calc_ema(data, period):
    if not data:
        return []
    ema = [data[0]]
    k = 2.0 / (period + 1)
    for i in range(1, len(data)):
        ema.append(data[i] * k + ema[-1] * (1 - k))
    return ema


def calc_macd(closes, fast=12, slow=26, signal=9):
    if not closes:
        return [], [], []
    ema_fast = calc_ema(closes, fast)
    ema_slow = calc_ema(closes, slow)
    diff = [f - s for f, s in zip(ema_fast, ema_slow)]
    dea = calc_ema(diff, signal)
    macd = [2 * (d - e) for d, e in zip(diff, dea)]
    return diff, dea, macd


# ============================================================
# Part 7: A-B-C趋势结构 + 严格背驰判定（v3.1修复A段范围）
# ============================================================

def find_trend_structure(pivots, segments, diff, dea, macd_hist, closes, daily_data):
    """
    严格背驰判定（v3.1）：
    1. 必须是趋势中（至少两个同向中枢，中枢间无重叠）
    2. A段：第一个中枢之前的完整走势（取p1起始日期之前的全部K线）
    3. B段：中枢区域（MACD回拉0轴）
    4. C段：第二个中枢之后的走势
    5. 背驰条件：C段MACD面积 < A段MACD面积 × 70%
    """
    results = []
    if len(pivots) < 2:
        return results

    date_to_idx = {}
    for i, d in enumerate(daily_data):
        date_to_idx[d['date']] = i

    for i in range(len(pivots) - 1):
        p1 = pivots[i]
        p2 = pivots[i + 1]

        if p2['zd'] > p1['zg']:
            trend_dir = 'up'
        elif p2['zg'] < p1['zd']:
            trend_dir = 'down'
        else:
            continue

        # v3.1修复：A段取p1起始日期之前的全部走势（不只是第一个线段）
        a_end_idx = date_to_idx.get(p1['start_date'], 0)
        a_start_idx = 0  # 从数据开头到p1开始

        # C段：p2之后到数据结尾
        c_start_idx = date_to_idx.get(p2['end_date'], len(closes) - 1)
        c_end_idx = len(closes) - 1

        # B段：p1结束到p2开始之间的所有走势段
        p1_end_date = p1['end_date']
        p2_start_date = p2['start_date']
        b_segments = [seg for seg in segments
                      if seg['end_date'] >= p1_end_date and seg['start_date'] <= p2_start_date]
        if b_segments:
            b_start_idx = date_to_idx.get(b_segments[0]['start_date'], a_end_idx)
            b_end_idx = date_to_idx.get(b_segments[-1]['end_date'], c_start_idx)
        else:
            b_start_idx = a_end_idx
            b_end_idx = c_start_idx

        a_start_idx = max(0, min(a_start_idx, len(closes) - 1))
        a_end_idx = max(0, min(a_end_idx, len(closes) - 1))
        b_start_idx = max(0, min(b_start_idx, len(closes) - 1))
        b_end_idx = max(0, min(b_end_idx, len(closes) - 1))
        c_start_idx = max(0, min(c_start_idx, len(closes) - 1))
        c_end_idx = max(0, min(c_end_idx, len(closes) - 1))

        if a_start_idx >= a_end_idx - 4 or c_start_idx >= c_end_idx - 4:
            continue

        if trend_dir == 'up':
            a_area = sum(m for m in macd_hist[a_start_idx:a_end_idx + 1] if m > 0)
            c_area = sum(m for m in macd_hist[c_start_idx:c_end_idx + 1] if m > 0)
        else:
            a_area = sum(abs(m) for m in macd_hist[a_start_idx:a_end_idx + 1] if m < 0)
            c_area = sum(abs(m) for m in macd_hist[c_start_idx:c_end_idx + 1] if m < 0)

        # B段：跳过EMA热身期不可靠的数据（b_start_idx>=b_end_idx时直接判否）
        if b_start_idx < b_end_idx:
            b_diff = diff[b_start_idx:b_end_idx + 1]
            if b_diff:
                max_abs_diff = max(abs(d) for d in diff) if diff else 1
                b_near_zero = any(abs(d) < max_abs_diff * 0.2 for d in b_diff)
            else:
                b_near_zero = False
        else:
            b_near_zero = False

        if a_area > 1:
            ratio = c_area / a_area
            is_divergence = ratio < 0.7 and b_near_zero
            if c_area < 1:
                continue
            if is_divergence:
                results.append({
                    'trend': trend_dir,
                    'pivot1': p1, 'pivot2': p2,
                    'a_area': round(a_area, 2),
                    'c_area': round(c_area, 2),
                    'ratio': round(ratio, 3),
                    'b_near_zero': b_near_zero,
                })

    return results


# ============================================================
# Part 8: 三类买卖点识别（v3.1 严格版）
# ============================================================

def find_buy_sell_points(pivots, segments, fractals, diff, dea, macd_hist, closes,
                         trend_structures=None, daily_data=None):
    """
    三类买卖点识别（v3.1）
    修复：一买/一卖依赖趋势结构，三买/三卖验证突破来源，卖出加趋势位置检查
    """
    points = []
    if not pivots:
        return points

    current_price = closes[-1]
    current_diff = diff[-1]
    current_dea = dea[-1]
    last_pivot = pivots[-1]

    bottom_fractals = [f for f in fractals if f['type'] == 'bottom']
    top_fractals = [f for f in fractals if f['type'] == 'top']

    # --- 成交量辅助 ---
    volumes = [d['volume'] for d in daily_data] if daily_data else []
    vol_ma10 = sum(volumes[-10:]) / 10 if len(volumes) >= 10 else 0
    vol_now = volumes[-1] if volumes else 0
    vol_ratio = vol_now / vol_ma10 if vol_ma10 > 0 else 1.0
    vol_breakout = vol_ratio > 1.5  # 放量突破

    # ================================================================
    # 第一类买点：下跌趋势背驰（第29/37课）
    # 前提：存在下跌趋势（两个不重叠中枢），C段背驰
    # ================================================================
    if trend_structures:
        for ts in trend_structures:
            if ts['trend'] == 'down':
                ratio = ts['ratio']
                strength = 'strong' if ratio < 0.5 else 'medium'
                points.append({
                    'type': '第一类买点', 'direction': 'buy',
                    'price': current_price,
                    'signal': f'下跌趋势背驰(A/C={ratio:.2f})',
                    'strength': strength,
                })
                break

    # v3.3: 单中枢底背驰弱信号（放宽条件）
    # 没有两个中枢的完整趋势，但至少有一个中枢+MACD底背驰
    if not any(bp['type'] == '第一类买点' for bp in points):
        if pivots and len(macd_hist) >= 20 and len(closes) >= 10:
            # 检查MACD底背驰：价格在中枢下方，近10天绿柱面积<前10天
            if current_price < last_pivot['zd'] and current_diff < 0:
                recent_neg = sum(abs(m) for m in macd_hist[-10:] if m < 0)
                prev_neg = sum(abs(m) for m in macd_hist[-20:-10] if m < 0)
                if prev_neg > 1 and recent_neg < prev_neg * 0.7 and recent_neg > 0.5:
                    points.append({
                        'type': '第一类买点', 'direction': 'buy',
                        'price': current_price,
                        'signal': f'单中枢底背驰(绿柱{recent_neg:.1f}/{prev_neg:.1f}={recent_neg/prev_neg:.0%})',
                        'strength': 'medium',
                    })

    # ================================================================
    # 第二类买点：一买之后回调确认（第37/40课）
    # 前提：有下跌趋势背驰结构 + 回调不破前低 + MACD金叉
    # v3.3: 二买也放宽条件——只要当前有买入信号（包括弱一买），且回调不破前低
    has_buy_signal = any(bp['type'] == '第一类买点' for bp in points)
    if has_buy_signal and bottom_fractals:
        last_bot = bottom_fractals[-1]
        # 回调验证：从一买到当前，价格曾回落到前低1.05倍以内
        pullback_confirmed = False
        if points:
            first_buy = next((bp for bp in points if bp['type'] == '第一类买点'), None)
            if first_buy:
                buy_idx = None
                for idx in range(len(closes) - 1, -1, -1):
                    if abs(closes[idx] - first_buy['price']) / first_buy['price'] < 0.01:
                        buy_idx = idx
                        break
                if buy_idx is not None:
                    high_after_buy = max(closes[buy_idx:])
                    low_after_buy = min(closes[buy_idx:])
                    # 从高点回调至少5%，且回落点在前低1.05倍以内
                    if high_after_buy > 0:
                        pullback_pct = (high_after_buy - low_after_buy) / high_after_buy
                        if pullback_pct >= 0.05 and low_after_buy <= last_bot['price'] * 1.05:
                            pullback_confirmed = True
        if (pullback_confirmed and
            current_price > last_bot['price'] * 1.02 and
            current_diff > current_dea):
            points.append({
                'type': '第二类买点', 'direction': 'buy',
                'price': current_price,
                'signal': f'回调不破底{last_bot["price"]:.2f}，MACD确认',
                'strength': 'medium',
            })

    # ================================================================
    # 第三类买点：中枢突破+回踩（第37/65课）
    # 前提：近20天曾有价格在中枢内 → 突破ZG → 回踩不破ZG
    # ================================================================
    if len(pivots) >= 1 and len(closes) >= 10:
        pivot_in_range = any(last_pivot['zd'] <= c <= last_pivot['zg'] for c in closes[-20:])
        if pivot_in_range and current_price > last_pivot['zg']:
            recent_lows = min(closes[-5:])
            if recent_lows >= last_pivot['zg']:
                # v3.3: 放量突破=strong，缩量突破=medium（仍有效但权重低）
                if vol_breakout:
                    points.append({
                        'type': '第三类买点', 'direction': 'buy',
                        'price': current_price,
                        'signal': f'放量突破中枢[{last_pivot["zd"]:.1f},{last_pivot["zg"]:.1f}]后回踩不破ZG',
                        'strength': 'strong',
                    })
                elif len(closes) >= 10:
                    # 缩量突破也给信号，但降级为medium
                    points.append({
                        'type': '第三类买点', 'direction': 'buy',
                        'price': current_price,
                        'signal': f'突破中枢[{last_pivot["zd"]:.1f},{last_pivot["zg"]:.1f}]后回踩不破ZG(缩量)',
                        'strength': 'medium',
                    })

    # ================================================================
    # 第一类卖点：上涨趋势背驰（第29/37课）
    # 前提：存在上涨趋势（两个不重叠中枢），C段背驰
    # v3.1修复：加入趋势位置检查（价格必须在中枢上方）
    # ================================================================
    # v3.5: 一卖放宽——不要求完整趋势结构，只要MACD背驰+价格在中枢上方
    if current_price > last_pivot['zg'] and current_diff > 0 and len(macd_hist) >= 20:
        recent_pos = sum(m for m in macd_hist[-10:] if m > 0)
        prev_pos = sum(m for m in macd_hist[-20:-10] if m > 0)
        if prev_pos > 1 and recent_pos < prev_pos * 0.7:
            strength = 'strong' if recent_pos < prev_pos * 0.5 else 'medium'
            points.append({
                'type': '第一类卖点', 'direction': 'sell',
                'price': current_price,
                'signal': f'上涨背驰(红柱{recent_pos:.0f}/{prev_pos:.0f}={recent_pos/prev_pos:.0%})',
                'strength': strength,
            })

    # ================================================================
    # 第三类卖点：中枢跌破+回抽（第37/65课）
    # 前提：近20天曾有价格在中枢内 → 跌破ZD → 回抽不破ZD
    # ================================================================
    if len(pivots) >= 1 and len(closes) >= 10:
        pivot_in_range = any(last_pivot['zd'] <= c <= last_pivot['zg'] for c in closes[-20:])
        if pivot_in_range and current_price < last_pivot['zd']:
            recent_highs = max(closes[-5:])
            if recent_highs <= last_pivot['zd']:
                points.append({
                    'type': '第三类卖点', 'direction': 'sell',
                    'price': current_price,
                    'signal': f'跌破中枢[{last_pivot["zd"]:.1f},{last_pivot["zg"]:.1f}]后回抽不破ZD',
                    'strength': 'strong',
                })

    return points


# ============================================================
# Part 9: 综合分析（v3.1 评分加权）
# ============================================================

def full_analysis(daily_data, name, code):
    if not daily_data or len(daily_data) < 30:
        return None

    closes = [d['close'] for d in daily_data]

    merged = process_inclusion(daily_data)
    fractals = find_fractals(merged)
    strokes = build_strokes(fractals, merged)
    segments = build_segments(strokes)
    pivots = find_pivots(segments)
    diff, dea, macd_hist = calc_macd(closes)
    trend_structures = find_trend_structure(pivots, segments, diff, dea, macd_hist, closes, daily_data)
    buy_sell_points = find_buy_sell_points(pivots, segments, fractals, diff, dea, macd_hist, closes,
                                           trend_structures, daily_data)

    def ma(data, n):
        return sum(data[-n:]) / n if len(data) >= n else None
    ma5, ma10, ma20, ma60 = ma(closes, 5), ma(closes, 10), ma(closes, 20), ma(closes, 60)
    ma120 = ma(closes, 120) if len(closes) >= 120 else None

    if ma5 and ma10 and ma20:
        if ma5 > ma10 > ma20: ma_arr = "多头排列"
        elif ma5 < ma10 < ma20: ma_arr = "空头排列"
        else: ma_arr = "交叉缠绕"
    else:
        ma_arr = "N/A"

    diff_now, dea_now, macd_now = diff[-1], dea[-1], macd_hist[-1]
    if diff_now > 0 and dea_now > 0: macd_zone = "0轴上"
    elif diff_now < 0 and dea_now < 0: macd_zone = "0轴下"
    else: macd_zone = "0轴附近"

    if diff[-1] > dea[-1] and diff[-2] <= dea[-2]: macd_sig = "刚金叉"
    elif diff[-1] < dea[-1] and diff[-2] >= dea[-2]: macd_sig = "刚死叉"
    elif diff[-1] > dea[-1]: macd_sig = "金叉中"
    else: macd_sig = "死叉中"

    # v3.1: 判断当前趋势方向（用于评分加权）
    has_uptrend = any(ts['trend'] == 'up' for ts in trend_structures)
    has_downtrend = any(ts['trend'] == 'down' for ts in trend_structures)

    # 评分（v3.1: 趋势方向加权）
    score = 0
    signals = []

    if ma_arr == "多头排列": signals.append("✅ 日线多头排列"); score += 2
    elif ma_arr == "空头排列": signals.append("❌ 日线空头排列"); score -= 2
    else: signals.append("⚠️ 均线交叉缠绕")

    if macd_sig == "刚金叉" and diff_now > 0: signals.append("✅ MACD刚金叉+0轴上"); score += 3
    elif macd_sig == "金叉中" and diff_now > 0: signals.append("✅ MACD金叉+0轴上"); score += 2
    elif macd_sig == "刚金叉" and diff_now < 0: signals.append("⚠️ MACD刚金叉但0轴下"); score += 1
    elif macd_sig == "刚死叉" and diff_now > 0: signals.append("⚠️ MACD刚死叉但0轴上"); score -= 1
    elif macd_sig == "死叉中" and diff_now < 0: signals.append("❌ MACD死叉+0轴下"); score -= 2
    else: signals.append(f"📌 MACD {macd_sig}")

    if pivots:
        last_p = pivots[-1]
        if closes[-1] > last_p['zg']: signals.append(f"✅ 价格突破中枢上沿{last_p['zg']:.2f}"); score += 2
        elif closes[-1] < last_p['zd']: signals.append(f"❌ 价格跌破中枢下沿{last_p['zd']:.2f}"); score -= 2
        else: signals.append(f"📌 价格在中枢[{last_p['zd']:.2f},{last_p['zg']:.2f}]内")

    # v3.1: 趋势背驰信号（来自trend_structures，已经过严格验证）
    for ts in trend_structures:
        if ts['trend'] == 'down':
            signals.append(f"⚡ 下跌趋势背驰(A/C比{ts['ratio']:.2f})→底部信号"); score += 2
        else:
            signals.append(f"⚡ 上涨趋势背驰(A/C比{ts['ratio']:.2f})→顶部信号"); score -= 2

    for bp in buy_sell_points:
        if bp['direction'] == 'buy':
            signals.append(f"🎯 {bp['type']}: {bp['signal']}")
            score += 3 if bp['strength'] == 'strong' else 1
        else:
            signals.append(f"🎯 {bp['type']}: {bp['signal']}")
            score -= 3 if bp['strength'] == 'strong' else 1

    if score >= 5: rating = "🟢 强烈看多（缠论买点区间）"
    elif score >= 2: rating = "🟡 偏多（关注买点确认）"
    elif score >= -1: rating = "⚪ 中性（震荡区间）"
    elif score >= -3: rating = "🟠 偏空（谨慎观望）"
    else: rating = "🔴 看空（缠论卖点区间）"

    return {
        'code': code, 'name': name, 'current': closes[-1],
        'ma_arr': ma_arr, 'ma5': ma5, 'ma10': ma10, 'ma20': ma20, 'ma60': ma60, 'ma120': ma120,
        'macd_zone': macd_zone, 'macd_sig': macd_sig,
        'diff': diff_now, 'dea': dea_now, 'macd': macd_now,
        'pivots': pivots, 'fractals': fractals,
        'strokes': strokes, 'segments': segments,
        'trend_structures': trend_structures, 'buy_sell_points': buy_sell_points,
        'signals': signals, 'score': score, 'rating': rating,
        'merged_count': len(merged), 'fractal_count': len(fractals),
        'stroke_count': len(strokes), 'segment_count': len(segments),
        'pivot_count': len(pivots),
    }
