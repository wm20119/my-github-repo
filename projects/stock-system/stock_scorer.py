#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Serenity产业链卡点投资评分系统 v12
基本面25 + 估值10 + 卡点25 + 技术25 + 情绪15 = 100
"""

import urllib.request
import json
import time
import sys
import os
from datetime import datetime, timedelta

try:
    import akshare as ak
except ImportError:
    ak = None

try:
    import pandas as pd
except ImportError:
    pd = None

try:
    import tushare as ts
except ImportError:
    ts = None

# --- Tushare token ---
_TUSHARE_TOKEN = None
_TUSHARE_PRO = None
_TUSHARE_TOKEN_PATH = os.path.expanduser('~/.tushare/token.txt')
try:
    with open(_TUSHARE_TOKEN_PATH, 'r') as _f:
        _TUSHARE_TOKEN = _f.read().strip()
    if _TUSHARE_TOKEN and ts:
        ts.set_token(_TUSHARE_TOKEN)
        _TUSHARE_PRO = ts.pro_api()
except Exception:
    pass

# ============================================================
# 常量与配置
# ============================================================

# --- 从 stock_config.json 统一加载配置 ---
_CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'stock_config.json')
def _load_config():
    try:
        with open(_CONFIG_PATH, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception as e:
        print(f'  ⚠ 配置文件加载失败: {_CONFIG_PATH} - {e}', file=sys.stderr)
        sys.exit(1)
_cfg = _load_config()

CHOKEPOINT_DB = {k: tuple(v) for k, v in _cfg['chokepoint'].items()}
USER_DATA = _cfg['user_data']
CYCLE_STOCKS_OVERRIDE = {k: tuple(v) for k, v in _cfg['cycle_override'].items()}
INDUSTRY_PEERS = {k: (v[0], v[1]) for k, v in _cfg['industry_peers'].items()}
INDUSTRY_ETF = _cfg['industry_etf']


# ============================================================
# 数据获取: 腾讯行情
# ============================================================

def _market_prefix(code):
    """返回腾讯行情前缀 sh/sz/bj"""
    if code.startswith('8'):
        return 'bj'
    if code.startswith(('6', '9')):
        return 'sh'
    return 'sz'


def get_quote_tencent(code):
    """从腾讯获取单只股票行情, 返回 dict 或 None"""
    prefix = _market_prefix(code)
    url = f'https://qt.gtimg.cn/q={prefix}{code}'
    try:
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        resp = urllib.request.urlopen(req, timeout=8)
        raw = resp.read().decode('gbk', errors='ignore')
        parts = raw.split('~')
        if len(parts) < 47:
            return None
        name = parts[1]
        price = float(parts[3]) if parts[3] else 0.0
        change_pct = float(parts[32]) if parts[32] else 0.0
        turnover = float(parts[38]) if parts[38] else 0.0
        pe = float(parts[39]) if parts[39] else 0.0
        float_cap = float(parts[44]) if parts[44] else 0.0
        market_cap = float(parts[45]) if parts[45] else 0.0
        pb = float(parts[46]) if parts[46] else 0.0
        # 成交额: parts[37]是万元，转亿
        amount_raw = float(parts[37]) if parts[37] else 0.0
        amount_yi = amount_raw / 10000  # 万→亿
        return {
            'code': code, 'name': name, 'price': price,
            'change_pct': change_pct,
            'turnover_rate': turnover,
            'pe': pe, 'pb': pb,
            'market_cap': market_cap,
            'amount_yi': amount_yi,
        }
    except Exception:
        return None


def batch_quote_tencent(codes):
    """批量获取行情"""
    if not codes:
        return {}
    prefixes = [_market_prefix(c) + c for c in codes]
    url = 'https://qt.gtimg.cn/q=' + ','.join(prefixes)
    result = {}
    try:
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        resp = urllib.request.urlopen(req, timeout=12)
        raw = resp.read().decode('gbk', errors='ignore')
        for line in raw.strip().split('\n'):
            line = line.strip().rstrip(';')
            if '~' not in line:
                continue
            parts = line.split('~')
            if len(parts) < 47:
                continue
            try:
                code = parts[2]
                # 成交额: parts[37]是万元，转亿
                amount_raw = float(parts[37]) if parts[37] else 0.0
                amount_yi = amount_raw / 10000
                result[code] = {
                    'code': code,
                    'name': parts[1],
                    'price': float(parts[3]) if parts[3] else 0.0,
                    'change_pct': float(parts[32]) if parts[32] else 0.0,
                    'turnover_rate': float(parts[38]) if parts[38] else 0.0,
                    'pe': float(parts[39]) if parts[39] else 0.0,
                    'pb': float(parts[46]) if parts[46] else 0.0,
                    'market_cap': float(parts[45]) if parts[45] else 0.0,
                    'amount_yi': amount_yi,
                }
            except (ValueError, IndexError):
                continue
    except Exception:
        pass
    return result


# ============================================================
# Tushare 数据源 (替代腾讯行情+akshare利润表)
# ============================================================

def _tushare_code(code):
    """股票代码 → Tushare ts_code (600030 → 600030.SH)"""
    if code.startswith('8'):
        return f'{code}.BJ'
    if code.startswith(('6', '9')):
        return f'{code}.SH'
    return f'{code}.SZ'


def batch_quote_tushare(codes, trade_date=None):
    """从Tushare daily_basic获取PE/PB/换手率/总市值
    返回 dict[code] -> {pe, pb, turnover_rate, market_cap}
    """
    if _TUSHARE_PRO is None or not codes:
        return {}
    if trade_date is None:
        from datetime import datetime, timedelta
        today = datetime.now()
        # 尝试最近3个交易日(周末/节假日自动回退)
        ts_codes = [_tushare_code(c) for c in codes]
        for delta in range(0, 5):
            d = today - timedelta(days=delta)
            trade_date = d.strftime('%Y%m%d')
            try:
                df = _TUSHARE_PRO.daily_basic(ts_code=','.join(ts_codes),
                                              trade_date=trade_date,
                                              fields='ts_code,turnover_rate,pe_ttm,pb,total_mv')
                if df is not None and len(df) > 0:
                    break
            except Exception:
                df = None
                continue
        else:
            return {}
    else:
        try:
            ts_codes = [_tushare_code(c) for c in codes]
            df = _TUSHARE_PRO.daily_basic(ts_code=','.join(ts_codes),
                                          trade_date=trade_date,
                                          fields='ts_code,turnover_rate,pe_ttm,pb,total_mv')
        except Exception:
            return {}
    if df is None or len(df) == 0:
        return {}
    result = {}
    for _, row in df.iterrows():
        # ts_code: 600030.SH → 600030
        stock_code = row['ts_code'].split('.')[0]
        result[stock_code] = {
            'pe': float(row.get('pe_ttm', 0) or 0),
            'pb': float(row.get('pb', 0) or 0),
            'turnover_rate': float(row.get('turnover_rate', 0) or 0),
            'market_cap': float(row.get('total_mv', 0) or 0) / 10000,  # 万元→亿
        }
    return result


def batch_financial_tushare(codes):
    """批量获取财务数据(fina_indicator), 返回 dict[code] -> dict
    一次API调用拉所有股票，比逐只东方财富快10倍+
    """
    if _TUSHARE_PRO is None or not codes:
        return {}
    try:
        ts_codes = [_tushare_code(c) for c in codes]
        # fina_indicator不支持批量ts_code，需逐只但用最近日期减少数据量
        # 改用fina_indicator_vip或简化字段
        result = {}
        # 分批拉取，每批50只
        batch_size = 50
        for i in range(0, len(ts_codes), batch_size):
            batch = ts_codes[i:i+batch_size]
            batch_codes = codes[i:i+batch_size]
            for ts_c, code in zip(batch, batch_codes):
                try:
                    df = _TUSHARE_PRO.fina_indicator(ts_code=ts_c, limit=8,
                        fields='ts_code,end_date,roe,netprofit_margin,grossprofit_margin,'
                               'debt_to_assets,ocf_to_profit,netprofit_yoy,or_yoy,'
                               'dt_netprofit_yoy,deducted_profit')
                    if df is None or len(df) == 0:
                        continue
                    df = df.sort_values('end_date', ascending=False)
                    # 按报告期分类
                    annual = df[df['end_date'].astype(str).str.endswith('1231')]
                    non_annual = df[~df['end_date'].astype(str).str.endswith('1231')]
                    
                    def _parse_row(row):
                        return {
                            'roe_jq': row.get('roe'),
                            'net_margin': row.get('netprofit_margin'),
                            'gross_margin': row.get('grossprofit_margin'),
                            'debt_ratio': row.get('debt_to_assets'),
                            'ocf_to_profit': row.get('ocf_to_profit'),
                            'profit_growth_pct': row.get('netprofit_yoy'),
                            'deducted_profit_growth': row.get('dt_netprofit_yoy'),
                            'deducted_profit': row.get('deducted_profit'),
                            'report_date': str(row.get('end_date', '')),
                            'report_type': '年报' if str(row.get('end_date', '')).endswith('1231') else '季报',
                        }
                    
                    years = [_parse_row(r) for _, r in annual.head(4).iterrows()]
                    quarter = _parse_row(non_annual.iloc[0]) if len(non_annual) > 0 else None
                    
                    # 加权平均
                    weighted = {}
                    for field in ['roe_jq', 'debt_ratio', 'net_margin', 'ocf_to_profit']:
                        vals = [(y.get(field), 1.0) for y in years[:3] if y.get(field) is not None]
                        if quarter and quarter.get(field) is not None:
                            vals.append((quarter.get(field), 0.6))
                        if vals:
                            total_w = sum(w for _, w in vals)
                            weighted[field] = sum(v * w for v, w in vals) / total_w if total_w > 0 else None
                    
                    # 扣非增速
                    deducted_growth = None
                    if quarter and quarter.get('deducted_profit_growth') is not None:
                        deducted_growth = quarter['deducted_profit_growth']
                    elif len(years) >= 2:
                        this_d = years[0].get('deducted_profit')
                        last_d = years[1].get('deducted_profit')
                        if this_d is not None and last_d is not None and last_d != 0:
                            deducted_growth = (this_d - last_d) / abs(last_d) * 100
                    
                    result[code] = {
                        'years': years,
                        'quarter': quarter,
                        'weighted': weighted,
                        'deducted_growth_pct': deducted_growth,
                        'latest': years[0] if years else (quarter or {}),
                    }
                except Exception:
                    pass
            time.sleep(0.3)  # Tushare限流
        return result
    except Exception:
        return {}


def get_financial_data_tushare(code, years=3):
    """从Tushare income获取利润表, 返回东方财富F10同构格式的dict或None
    返回: {years: [...], quarter: {...}, weighted: {...}, deducted_growth_pct: ...}
    """
    if _TUSHARE_PRO is None:
        return None
    try:
        ts_code = _tushare_code(code)
        # 获取最近N+1年年报+最新季报
        df = _TUSHARE_PRO.income(ts_code=ts_code,
                                 fields='ts_code,ann_date,f_ann_date,end_date,'
                                        'revenue,n_income_attr_p,revenue_yoy,n_income_attr_p_yoy')
        if df is None or len(df) == 0:
            return None
        # 按end_date排序(降序)
        df['end_date'] = df['end_date'].astype(str)
        df = df.sort_values('end_date', ascending=False)
        # 筛选年报(1231)
        annual = df[df['end_date'].str.endswith('1231')]
        # 筛选最新一期(非年报)
        non_annual = df[~df['end_date'].str.endswith('1231')]

        def _parse(row):
            return {
                'net_profit': float(row.get('n_income_attr_p', 0) or 0),
                'revenue': float(row.get('revenue', 0) or 0),
                'profit_growth_pct': float(row.get('n_income_attr_p_yoy', 0) or 0),
                'revenue_growth_pct': float(row.get('revenue_yoy', 0) or 0),
                'report_date': str(row.get('end_date', '')),
            }

        annual_reports = [_parse(r) for _, r in annual.head(years + 1).iterrows()]
        latest_quarter = _parse(non_annual.iloc[0]) if len(non_annual) > 0 else None

        if not annual_reports:
            if latest_quarter:
                return {'years': [], 'quarter': latest_quarter,
                        'weighted': latest_quarter,
                        'deducted_growth_pct': latest_quarter.get('profit_growth_pct')}
            return None

        latest_annual = annual_reports[0]

        # 加权平均(简化: Tushare无扣非数据,用净利润代替)
        weighted = {}
        for field in ['net_profit']:
            vals = [y.get(field) for y in annual_reports[:years] if y.get(field) is not None]
            if vals:
                weighted[field] = sum(vals) / len(vals)
            else:
                weighted[field] = None

        # 增速(优先用季报,否则用年报同比)
        deducted_growth = None
        if latest_quarter and latest_quarter.get('profit_growth_pct'):
            deducted_growth = latest_quarter['profit_growth_pct']
        elif len(annual_reports) >= 2 and annual_reports[1].get('net_profit'):
            this = latest_annual.get('net_profit', 0)
            last = annual_reports[1].get('net_profit', 0)
            if last != 0:
                deducted_growth = (this - last) / abs(last) * 100

        return {
            'years': annual_reports[:years],
            'quarter': latest_quarter,
            'weighted': weighted,
            'deducted_growth_pct': deducted_growth,
            'latest': {
                'net_profit': latest_annual.get('net_profit'),
                'revenue': latest_annual.get('revenue'),
                'profit_growth_pct': latest_annual.get('profit_growth_pct'),
                'revenue_growth_pct': latest_annual.get('revenue_growth_pct'),
            },
        }
    except Exception:
        return None


# ============================================================
# Tushare 资金流向评分
# ============================================================

_SW_INDUSTRY_CACHE = {}       # code -> industry_name
_SW_ALL_INDUSTRIES = None     # {industry_name: [ts_code, ...]} 一次性缓存


def score_moneyflow(code):
    """资金流向评分 (0-5): 主力净流入方向与持续性
    主力净流入 = 超大单买入额 + 大单买入额 - 超大单卖出额 - 大单卖出额
    评分: 连续3天净流入→5, 整体净流入→4, 持平→3, 净流出→2, 大幅净流出→1
    """
    if _TUSHARE_PRO is None:
        return None
    try:
        ts_code = _tushare_code(code)
        end_date = datetime.now().strftime('%Y%m%d')
        start_date = (datetime.now() - timedelta(days=15)).strftime('%Y%m%d')
        df = _TUSHARE_PRO.moneyflow(ts_code=ts_code,
                                     start_date=start_date,
                                     end_date=end_date)
        if df is None or len(df) == 0:
            return None
        df = df.sort_values('trade_date', ascending=False).head(5)
        # 主力净流入 = 超大单+大单 买入-卖出 (单位: 元)
        df['main_net'] = (
            df['buy_elg_amount'].fillna(0) + df['buy_lg_amount'].fillna(0)
            - df['sell_elg_amount'].fillna(0) - df['sell_lg_amount'].fillna(0)
        )
        net_flows = df['main_net'].tolist()
        if not net_flows:
            return None
        # 连续净流入天数(从最近一天往前数)
        consecutive_positive = 0
        for n in net_flows:
            if n > 0:
                consecutive_positive += 1
            else:
                break
        total_net = sum(net_flows)
        avg_net = total_net / len(net_flows)
        # --- 按总市值比例调整阈值 (total_mv 单位: 万元) ---
        scale = 1.0  # 默认: 小盘股 (total_mv < 100亿)
        try:
            mv_df = _TUSHARE_PRO.daily_basic(
                ts_code=ts_code,
                trade_date=datetime.now().strftime('%Y%m%d'),
                fields='total_mv')
            if mv_df is None or len(mv_df) == 0:
                # 回退到前一个交易日
                prev = (datetime.now() - timedelta(days=1)).strftime('%Y%m%d')
                mv_df = _TUSHARE_PRO.daily_basic(
                    ts_code=ts_code, trade_date=prev, fields='total_mv')
            if mv_df is not None and len(mv_df) > 0:
                total_mv = float(mv_df.iloc[0].get('total_mv', 0) or 0)  # 万元
                if total_mv >= 5000000:    # >=500亿 (500万万元)
                    scale = 5.0
                elif total_mv >= 1000000:  # >=100亿
                    scale = 2.0
                else:
                    scale = 1.0
        except Exception:
            scale = 1.0
        base_flat = 5e6 * scale   # 接近持平阈值 (默认500万)
        base_out = 5e7 * scale    # 大幅净流出阈值 (默认5000万)

        if consecutive_positive >= 3:
            return 5   # 连续3天+主力净流入
        if total_net > 0:
            return 4   # 整体主力净流入
        if abs(avg_net) < base_flat:   # 接近持平
            return 3
        if total_net < -base_out:      # 大幅净流出
            return 1
        return 2               # 净流出
    except Exception:
        return None


# ============================================================
# Tushare 业绩预告+业绩快报
# ============================================================

def get_forecast_data(code):
    """获取最新业绩预告: type(预增/预减/略增/略减/预平/扭亏/续亏/首亏), p_change_min/max"""
    if _TUSHARE_PRO is None:
        return None
    try:
        ts_code = _tushare_code(code)
        df = _TUSHARE_PRO.forecast(
            ts_code=ts_code,
            fields='ts_code,ann_date,end_date,type,p_change_min,p_change_max,'
                   'net_profit_min,net_profit_max,summary')
        if df is None or len(df) == 0:
            return None
        df = df.sort_values('ann_date', ascending=False)
        row = df.iloc[0]
        return {
            'type': str(row.get('type', '')),
            'p_change_min': row.get('p_change_min'),
            'p_change_max': row.get('p_change_max'),
            'net_profit_min': row.get('net_profit_min'),
            'net_profit_max': row.get('net_profit_max'),
            'summary': str(row.get('summary', '')),
        }
    except Exception:
        return None


def get_express_data(code):
    """获取最新业绩快报: revenue, n_income_attr_p(归母净利润)等"""
    if _TUSHARE_PRO is None:
        return None
    try:
        ts_code = _tushare_code(code)
        df = _TUSHARE_PRO.express(
            ts_code=ts_code,
            fields='ts_code,ann_date,end_date,revenue,operate_profit,'
                   'total_profit,n_income,n_income_attr_p')
        if df is None or len(df) == 0:
            return None
        df = df.sort_values('ann_date', ascending=False)
        row = df.iloc[0]
        return {
            'revenue': row.get('revenue'),
            'n_income_attr_p': row.get('n_income_attr_p'),
            'ann_date': str(row.get('ann_date', '')),
        }
    except Exception:
        return None


# ============================================================
# Tushare 申万行业分类
# ============================================================

def get_sw_industry(code):
    """获取申万二级行业名称
    方法1: stock_basic直接查单只股票 (1次API)
    方法2: stock_basic批量获取全部股票的industry字段, 构建映射后反查 (仅首次1次API, 后续走缓存)
    """
    global _SW_INDUSTRY_CACHE, _SW_ALL_INDUSTRIES
    if _TUSHARE_PRO is None:
        return None
    if code in _SW_INDUSTRY_CACHE:
        return _SW_INDUSTRY_CACHE[code]
    try:
        ts_code = _tushare_code(code)
        # 方法1: stock_basic直接获取行业字段
        try:
            df = _TUSHARE_PRO.stock_basic(ts_code=ts_code,
                                           fields='ts_code,industry,list_status')
            if df is not None and len(df) > 0:
                industry = df.iloc[0].get('industry')
                if industry and str(industry).strip():
                    _SW_INDUSTRY_CACHE[code] = str(industry).strip()
                    return str(industry).strip()
        except Exception:
            # 方法1失败, 进入方法2批量查询
            pass
        # 方法2: 批量获取所有股票industry, 构建 {industry: [ts_code]} 映射 (仅1次API)
        if _SW_ALL_INDUSTRIES is None:
            try:
                all_df = _TUSHARE_PRO.stock_basic(
                    exchange='', list_status='L',
                    fields='ts_code,industry,list_status')
                if all_df is not None and len(all_df) > 0:
                    mapping = {}
                    for _, r in all_df.iterrows():
                        ind = str(r.get('industry', '')).strip()
                        tc = r.get('ts_code', '')
                        if ind and tc:
                            mapping.setdefault(ind, []).append(tc)
                    _SW_ALL_INDUSTRIES = mapping
                else:
                    _SW_ALL_INDUSTRIES = {}
            except Exception:
                _SW_ALL_INDUSTRIES = {}
        # 用映射反查
        for ind_name, members in _SW_ALL_INDUSTRIES.items():
            if ts_code in members:
                _SW_INDUSTRY_CACHE[code] = ind_name
                return ind_name
    except Exception:
        pass
    return None


# ============================================================
# K线数据: 新浪
# ============================================================

def get_kline_sina(code, days=120):
    """从新浪获取日K线, 返回 list of dict (date,open,high,low,close,volume)"""
    prefix = _market_prefix(code)
    url = (
        'https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/'
        f'CN_MarketData.getKLineData?symbol={prefix}{code}&scale=240&ma=no&datalen={days}'
    )
    try:
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        resp = urllib.request.urlopen(req, timeout=10)
        raw = resp.read().decode('utf-8', errors='ignore')
        data = json.loads(raw)
        result = []
        for item in data:
            result.append({
                'date': item.get('day', ''),
                'open': float(item.get('open', 0)),
                'high': float(item.get('high', 0)),
                'low': float(item.get('low', 0)),
                'close': float(item.get('close', 0)),
                'volume': float(item.get('volume', 0)),
            })
        return result
    except Exception:
        return []


# ============================================================
# 技术指标计算
# ============================================================

def calc_ma(closes, n):
    """计算N日均线"""
    if len(closes) < n:
        return None
    return sum(closes[-n:]) / n


def calc_macd(closes, fast=12, slow=26, signal=9):
    """计算MACD, 返回 (dif, dea, hist)"""
    if len(closes) < slow:
        return None, None, None
    # EMA
    ema_fast = [closes[0]]
    ema_slow = [closes[0]]
    k_fast = 2.0 / (fast + 1)
    k_slow = 2.0 / (slow + 1)
    for i in range(1, len(closes)):
        ema_fast.append(closes[i] * k_fast + ema_fast[-1] * (1 - k_fast))
        ema_slow.append(closes[i] * k_slow + ema_slow[-1] * (1 - k_slow))
    dif = [ema_fast[i] - ema_slow[i] for i in range(len(closes))]
    # DEA
    dea = [dif[0]]
    k_sig = 2.0 / (signal + 1)
    for i in range(1, len(dif)):
        dea.append(dif[i] * k_sig + dea[-1] * (1 - k_sig))
    hist = [(dif[i] - dea[i]) * 2 for i in range(len(dif))]
    return dif[-1], dea[-1], hist[-1]


def calc_volume_trend(volumes, n=5):
    """计算量能趋势, 返回 (recent_avg / prev_avg) 比值"""
    if len(volumes) < n * 2:
        return 1.0
    recent = sum(volumes[-n:]) / n
    prev = sum(volumes[-2 * n:-n]) / n
    if prev == 0:
        return 1.0
    return recent / prev


# ============================================================
# 行业PB对比
# ============================================================

# ============================================================
# Tushare 批量K线
# ============================================================

def batch_kline_tushare(codes, days=120):
    """从Tushare批量获取日K线, 返回 dict[code] -> list of dict
    一次API调用拉所有股票
    """
    if _TUSHARE_PRO is None or not codes:
        return {}
    ts_codes = [_tushare_code(c) for c in codes]
    end_date = datetime.now().strftime('%Y%m%d')
    start_date = (datetime.now() - timedelta(days=days+30)).strftime('%Y%m%d')
    
    result = {}
    batch_size = 50
    for i in range(0, len(ts_codes), batch_size):
        batch = ts_codes[i:i+batch_size]
        batch_codes = codes[i:i+batch_size]
        try:
            df = _TUSHARE_PRO.daily(ts_code=','.join(batch), start_date=start_date, end_date=end_date,
                                  fields='ts_code,trade_date,open,high,low,close,vol')
            if df is not None and len(df) > 0:
                for ts_c, code in zip(batch, batch_codes):
                    stock_df = df[df['ts_code'] == ts_c].sort_values('trade_date')
                    if len(stock_df) > 0:
                        result[code] = [{'date': str(row['trade_date']),
                                        'open': float(row['open']),
                                        'high': float(row['high']),
                                        'low': float(row['low']),
                                        'close': float(row['close']),
                                        'volume': float(row['vol'])} 
                                       for _, row in stock_df.iterrows()]
        except Exception:
            pass
        time.sleep(0.3)
    return result


# ============================================================
# Tushare 批量资金流向
# ============================================================

def batch_moneyflow_tushare(codes):
    """从Tushare批量获取资金流向, 返回 dict[code] -> score
    一次API调用拉所有股票
    """
    if _TUSHARE_PRO is None or not codes:
        return {}
    ts_codes = [_tushare_code(c) for c in codes]
    today = datetime.now().strftime('%Y%m%d')
    start_15d = (datetime.now() - timedelta(days=15)).strftime('%Y%m%d')
    
    result = {}
    try:
        df = _TUSHARE_PRO.moneyflow(ts_code=','.join(ts_codes), start_date=start_15d, end_date=today)
        if df is not None and len(df) > 0:
            for ts_c in ts_codes:
                code = ts_c.split('.')[0]
                stock_df = df[df['ts_code'] == ts_c].sort_values('trade_date', ascending=False).head(5)
                if len(stock_df) > 0:
                    stock_df['main_net'] = (
                        stock_df['buy_elg_amount'].fillna(0) + stock_df['buy_lg_amount'].fillna(0)
                        - stock_df['sell_elg_amount'].fillna(0) - stock_df['sell_lg_amount'].fillna(0)
                    )
                    net_flows = stock_df['main_net'].tolist()
                    if net_flows:
                        consecutive = 0
                        for n in net_flows:
                            if n > 0:
                                consecutive += 1
                            else:
                                break
                        total_net = sum(net_flows)
                        # 评分逻辑
                        if consecutive >= 3:
                            result[code] = 5
                        elif total_net > 0:
                            result[code] = 4
                        elif abs(total_net / len(net_flows)) < 5e6:
                            result[code] = 3
                        elif total_net < -5e7:
                            result[code] = 1
                        else:
                            result[code] = 2
    except Exception:
        pass
    return result


# ============================================================
# Tushare 批量风险因子
# ============================================================

def batch_risk_tushare(codes):
    """从Tushare批量获取风险因子, 返回 dict[code] -> {unlock, pledge}
    share_float按日期批量, pledge_stat逐只(不支持批量)
    """
    if _TUSHARE_PRO is None or not codes:
        return {}
    ts_codes = [_tushare_code(c) for c in codes]
    today = datetime.now().strftime('%Y%m%d')
    start_30d = (datetime.now() + timedelta(days=30)).strftime('%Y%m%d')
    
    result = {code: {'unlock': 0, 'pledge': 0} for code in codes}
    
    # 1. 限售解禁 - 按日期批量
    try:
        df = _TUSHARE_PRO.share_float(start_date=today, end_date=start_30d, fields='ts_code,float_ratio')
        if df is not None and len(df) > 0:
            for ts_c in ts_codes:
                code = ts_c.split('.')[0]
                stock_df = df[df['ts_code'] == ts_c]
                if len(stock_df) > 0:
                    max_ratio = stock_df['float_ratio'].max()
                    if float(max_ratio) > 5:
                        result[code]['unlock'] = -3
                    elif float(max_ratio) > 2:
                        result[code]['unlock'] = -1
    except Exception:
        pass
    
    # 2. 股权质押 - 逐只(不支持批量)
    for ts_c in ts_codes:
        code = ts_c.split('.')[0]
        try:
            df = _TUSHARE_PRO.pledge_stat(ts_code=ts_c, fields='ts_code,end_date,pledge_ratio')
            if df is not None and len(df) > 0:
                df = df.sort_values('end_date', ascending=False)
                ratio = float(df.iloc[0]['pledge_ratio'])
                if ratio > 50:
                    result[code]['pledge'] = -3
                elif ratio > 30:
                    result[code]['pledge'] = -1
        except Exception:
            pass
        time.sleep(0.2)
    
    return result


def get_industry_pb_stats(code):
    """获取行业PB统计: (industry_name, avg_pb, stock_pb, rank_ratio)
    优先用Tushare申万行业分类动态获取同行业股票, fallback到硬编码映射
    """
    # ---- 优先: Tushare申万行业动态获取 ----
    sw_industry = get_sw_industry(code)
    if sw_industry and _TUSHARE_PRO is not None:
        try:
            # 获取同行业上市股票
            df = _TUSHARE_PRO.stock_basic(industry=sw_industry,
                                           list_status='L',
                                           fields='ts_code,name')
            if df is not None and len(df) > 0:
                # ts_code '600030.SH' → '600030'
                peer_codes = [tc.split('.')[0] for tc in df['ts_code'].tolist()]
                # 排除自身, 取前15只做对比
                peer_codes = [c for c in peer_codes if c != code][:15]
                if peer_codes:
                    quotes = batch_quote_tencent(peer_codes[:8])
                    # 也把自身PB加进去
                    my_quote = batch_quote_tencent([code])
                    if code in my_quote and my_quote[code] and my_quote[code].get('pb', 0) > 0:
                        quotes[code] = my_quote[code]
                    pbs = []
                    stock_pb = None
                    for c, q in quotes.items():
                        if q and q.get('pb') and q['pb'] > 0:
                            pbs.append(q['pb'])
                            if c == code:
                                stock_pb = q['pb']
                    if pbs:
                        avg_pb = sum(pbs) / len(pbs)
                        if stock_pb is None:
                            return sw_industry, avg_pb, None, None
                        rank_ratio = sum(1 for p in pbs if p <= stock_pb) / len(pbs)
                        return sw_industry, avg_pb, stock_pb, rank_ratio
                    else:
                        return sw_industry, None, stock_pb, None
        except Exception:
            pass

    # ---- fallback: 硬编码行业映射 ----
    if code not in INDUSTRY_PEERS:
        return None, None, None, None
    industry_name, peers = INDUSTRY_PEERS[code]
    quotes = batch_quote_tencent(peers[:6])  # limit batch size
    pbs = []
    stock_pb = None
    for c, q in quotes.items():
        if q and q['pb'] > 0:
            pbs.append(q['pb'])
            if c == code:
                stock_pb = q['pb']
    if not pbs:
        return industry_name, None, stock_pb, None
    avg_pb = sum(pbs) / len(pbs)
    if stock_pb is None:
        return industry_name, avg_pb, None, None
    rank_ratio = sum(1 for p in pbs if p <= stock_pb) / len(pbs)
    return industry_name, avg_pb, stock_pb, rank_ratio


# ============================================================
# 净利率获取
# ============================================================

def get_financial_data(code, years=3):
    """从东方财富F10获取财务数据(年报+半年报+季报), 返回 dict 或 None
    返回: {
        'years': [...近N年年报],
        'quarter': {...最新一期季报/半年报},
        'weighted': {...加权平均值},
        'deducted_growth_pct': 扣非增速
    }
    """
    def _parse_item(item):
        """解析单条财务数据"""
        return {
            'net_margin': item.get('XSJLL'),  # 净利率
            'gross_margin': item.get('XSMLL'),  # 毛利率
            'debt_ratio': item.get('ZCFZL'),  # 资产负债率
            'ocf_to_profit': item.get('JYXJLYYSR'),  # 经营现金流/净利润
            'deducted_profit': item.get('KCFJCXSYJLR'),  # 扣非净利润
            'net_profit': item.get('PARENTNETPROFIT'),  # 净利润
            'goodwill_to_equity': item.get('ZYGDSYLZQJZB'),  # 商誉/净资产
            'roe_jq': item.get('ROEJQ'),  # ROE加权
            'profit_growth_pct': item.get('PARENTNETPROFITTZ'),  # 净利润增速%
            'deducted_profit_growth': item.get('KCFJCXSYJLRTBZZ'),  # 扣非净利润增速%
            'report_type': item.get('REPORT_TYPE', ''),
            'report_date': item.get('REPORT_DATE', ''),
        }
    
    def _fetch_reports(type_code):
        """获取指定类型的报告"""
        try:
            prefix = 'SZ' if code.startswith(('0', '3')) else 'BJ' if code.startswith('8') else 'SH'
            url = f'https://emweb.securities.eastmoney.com/PC_HSF10/NewFinanceAnalysis/ZYZBAjaxNew?code={prefix}{code}&type={type_code}&date_type=0'
            req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
            resp = urllib.request.urlopen(req, timeout=10)
            data = json.loads(resp.read().decode('utf-8'))
            reports = []
            for item in data.get('data', []):
                if item.get('REPORT_TYPE') in ('年报', '半年报', '第一季报告', '第三季报告'):
                    reports.append(_parse_item(item))
                    if len(reports) >= years + 1:  # 多取一条用于增速计算
                        break
            return reports
        except Exception:
            return []
    
    try:
        # 获取年报(最近3年)
        annual_reports = _fetch_reports(0)
        # 获取最新季报/半年报（只需type=1，包含半年报和最新季报）
        quarterly_reports = _fetch_reports(1)
        # 按报告日期排序，取最新的一条季报
        quarterly_reports.sort(key=lambda x: x.get('report_date', ''), reverse=True)
        latest_quarter = quarterly_reports[0] if quarterly_reports else None
        
        if not annual_reports:
            # 没有年报数据，用季报
            if latest_quarter:
                return {
                    'years': [],
                    'quarter': latest_quarter,
                    'weighted': latest_quarter,
                    'deducted_growth_pct': latest_quarter.get('deducted_profit_growth'),
                }
            return None
        
        latest_annual = annual_reports[0]
        
        # 计算加权平均值
        # 权重: 年报1.0, 半年报/三季报0.8, 一季报0.6
        def _get_weight(report):
            rt = report.get('report_type', '')
            if rt == '年报':
                return 1.0
            elif rt == '半年报':
                return 0.8
            elif rt == '第三季报告':
                return 0.8
            elif rt == '第一季报告':
                return 0.6
            return 0.5
        
        weighted = {}
        fields = ['roe_jq', 'debt_ratio', 'net_margin', 'ocf_to_profit']
        for field in fields:
            values = []
            # 年报数据
            for y in annual_reports[:years]:
                v = y.get(field)
                if v is not None:
                    values.append((v, 1.0))
            # 最新季报
            if latest_quarter:
                v = latest_quarter.get(field)
                if v is not None:
                    values.append((v, _get_weight(latest_quarter)))
            if values:
                total_weight = sum(w for _, w in values)
                weighted[field] = sum(v * w for v, w in values) / total_weight
            else:
                weighted[field] = None
        
        # 计算扣非净利润增速
        deducted_growth = None
        # 优先用季报的增速
        if latest_quarter and latest_quarter.get('deducted_profit_growth') is not None:
            deducted_growth = latest_quarter['deducted_profit_growth']
        elif len(annual_reports) >= 2:
            this_deducted = latest_annual.get('deducted_profit')
            last_deducted = annual_reports[1].get('deducted_profit')
            if this_deducted is not None and last_deducted is not None and last_deducted != 0:
                deducted_growth = (this_deducted - last_deducted) / abs(last_deducted) * 100
        
        return {
            'years': annual_reports[:years],
            'quarter': latest_quarter,
            'weighted': weighted,
            'deducted_growth_pct': deducted_growth,
            'latest': latest_annual,  # 保持向后兼容
        }
    except Exception:
        return None


# ============================================================
# 基本面评分


def _get_financial_trends(code):
    """合并akshare调用: 一次拉取近3年财务指标, 返回:
    {
        'std_dev': 增速标准差, 'growth_rates': [近3年增速],
        'revenue_growths': [近3年营收增速], 'profit_growths': [近3年利润增速]
    } 或 None
    """
    try:
        if ak is None:
            return None
        df = ak.stock_financial_analysis_indicator(symbol=code, start_year='2021')
        if df is None or len(df) < 2:
            return None
        annual = df[df['日期'].astype(str).str.contains('12-31')].sort_index()
        if len(annual) < 3:
            # 只使用年报数据，不混合季报
            return None
        # 找列名
        growth_col = rev_col = None
        for c in df.columns:
            if '净利润增长率' in c or '净利润增长' in c:
                growth_col = c
            if '主营业务收入增长率' in c or '营业收入增长率' in c:
                rev_col = c
        result = {}
        # 增速稳定性
        if growth_col:
            rates = annual[growth_col].dropna().tail(3).tolist() if len(annual) >= 3 else df[growth_col].dropna().tail(3).tolist()
            if len(rates) >= 2:
                rates = [float(r) for r in rates]
                mean = sum(rates) / len(rates)
                variance = sum((r - mean) ** 2 for r in rates) / len(rates)
                result['std_dev'] = variance ** 0.5
                result['growth_rates'] = rates
        # 营收+利润趋势
        if rev_col and growth_col:
            rev_rates = annual[rev_col].dropna().tail(3).tolist()
            profit_rates = annual[growth_col].dropna().tail(3).tolist()
            if len(rev_rates) >= 3 and len(profit_rates) >= 3:
                result['revenue_growths'] = [float(r) for r in rev_rates]
                result['profit_growths'] = [float(r) for r in profit_rates]
        return result if result else None
    except Exception:
        return None


def score_roe(quote):
    """ROE评分 (0-5) - 使用东方财富F10真实数据, 加权平均"""
    # 优先用3年中位数
    financial = quote.get('_financial', None)
    if financial and 'weighted' in financial:
        roe = financial['weighted'].get('roe_jq')
    else:
        roe = quote.get('roe_jq', None)
    if roe is not None:
        if roe >= 25:
            return 5
        if roe >= 18:
            return 4
        if roe >= 12:
            return 3
        if roe >= 8:
            return 2
        if roe > 0:
            return 1
        return 0
    # fallback到USER_DATA
    code = quote.get('code', '')
    roe = USER_DATA.get(code, {}).get('roe_ttm', 0)
    if roe >= 25:
        return 5
    if roe >= 18:
        return 4
    if roe >= 12:
        return 3
    if roe >= 8:
        return 2
    if roe > 0:
        return 1
    return 0


def score_growth(quote):
    """扣非净利润增速评分 (0-5) - 使用东方财富F10真实数据, 优先季报
    增强: 结合Tushare业绩预告/快报, 预增>30%加1分, 预减>30%扣1分
    """
    code = quote.get('code', '')
    # 优先用3年扣非增速中位数
    financial = quote.get('_financial', None)
    if financial:
        # 优先用季报增速
        g = financial.get('deducted_growth_pct')
    else:
        g = None
    if g is None:
        g = quote.get('profit_growth_pct', None)
    if g is not None:
        if g >= 60:
            base_score = 5
        elif g >= 30:
            base_score = 4
        elif g >= 10:
            base_score = 3
        elif g > 0:
            base_score = 2
        elif g > -20:
            base_score = 1
        else:
            base_score = 0
    else:
        # fallback到USER_DATA
        g = USER_DATA.get(code, {}).get('eps_growth_pct', 0)
        if g >= 60:
            base_score = 5
        elif g >= 30:
            base_score = 4
        elif g >= 10:
            base_score = 3
        elif g > 0:
            base_score = 2
        elif g > -20:
            base_score = 1
        else:
            base_score = 0

    # ---- Tushare业绩预告+快报增强 ----
    forecast_adj = 0
    try:
        forecast = get_forecast_data(code)
        if forecast:
            ftype = str(forecast.get('type', ''))
            p_max = forecast.get('p_change_max')
            p_min = forecast.get('p_change_min')
            # 预增且增幅>30% → +1分
            if ftype == '预增' and p_max is not None:
                try:
                    if float(p_max) > 30:
                        forecast_adj = 1
                except (ValueError, TypeError):
                    pass
            # 预减且降幅>30% → -1分
            elif ftype == '预减' and p_min is not None:
                try:
                    if float(p_min) < -30:
                        forecast_adj = -1
                except (ValueError, TypeError):
                    pass
            # 首亏 → -1分 (首次亏损风险)
            elif ftype == '首亏':
                forecast_adj = -1
        # 业绩快报: 归母净利润同比增速辅助验证
        if forecast_adj == 0:
            express = get_express_data(code)
            if express and express.get('n_income_attr_p') is not None:
                # 快报归母净利润为负(亏损)
                if float(express['n_income_attr_p']) < 0:
                    forecast_adj = -1
    except Exception:
        pass

    return max(0, min(5, base_score + forecast_adj))


def score_debt(quote):
    """资产负债率评分 (0-5) - 使用东方财富F10真实数据, 加权平均"""
    financial = quote.get('_financial', None)
    if financial and 'weighted' in financial:
        debt_ratio = financial['weighted'].get('debt_ratio')
    else:
        debt_ratio = quote.get('debt_ratio', None)
    if debt_ratio is not None:
        if debt_ratio < 30:
            return 5
        if debt_ratio < 50:
            return 4
        if debt_ratio < 65:
            return 3
        if debt_ratio < 75:
            return 2
        return 1
    # fallback到PE/PB推断
    pe = quote.get('pe', 0)
    if pe <= 0:
        return 0
    if 5 <= pe <= 30:
        return 5
    if 30 < pe <= 60:
        return 4
    if pe < 5:
        return 3
    if 60 < pe <= 100:
        return 3
    if 100 < pe <= 200:
        return 2
    return 1


def score_cashflow(quote):
    """经营现金流评分 (0-4) - 使用实际经营现金流/净利润数据, 加权平均"""
    financial = quote.get('_financial', None)
    if financial and 'weighted' in financial:
        ocf_to_profit = financial['weighted'].get('ocf_to_profit')
    else:
        ocf_to_profit = quote.get('ocf_to_profit', None)
    if ocf_to_profit is not None:
        # 经营现金流/净利润 > 1.0 表示利润质量高
        if ocf_to_profit > 1.0:
            return 4
        elif ocf_to_profit > 0.8:
            return 3
        elif ocf_to_profit > 0.5:
            return 2
        elif ocf_to_profit > 0.2:
            return 1
        else:
            return 0
    # 无数据时用原有逻辑
    pe = quote.get('pe', 0)
    turnover = quote.get('turnover_rate', 0)
    if pe <= 0:
        return 0
    if pe > 0 and turnover < 5:
        return 4
    if pe > 0 and turnover < 8:
        return 3
    if pe > 0 and turnover < 12:
        return 2
    if pe > 0:
        return 1
    return 0


# ============================================================
# 净利率评分 (0-6)
# ============================================================

def score_net_margin(quote):
    """净利率评分 (0-6) - 使用加权平均"""
    financial = quote.get('_financial', None)
    if financial and 'weighted' in financial:
        margin = financial['weighted'].get('net_margin')
        if margin is None:
            margin = quote.get('net_margin', 0) or 0
    else:
        margin = quote.get('net_margin', 0) or 0
    if margin >= 30:
        return 6
    if margin >= 20:
        return 5
    if margin >= 12:
        return 4
    if margin >= 6:
        return 3
    if margin >= 2:
        return 2
    if margin > 0:
        return 1
    return 0


# ============================================================
# RSI评分 (0-5)
# ============================================================

def score_rsi(closes):
    """RSI评分 (0-5), 基于14日RSI (Wilder平滑)"""
    if not closes or len(closes) < 15:
        return 2
    # 计算14日RSI (Wilder平滑)
    gains = []
    losses = []
    for i in range(1, len(closes)):
        diff = closes[i] - closes[i-1]
        gains.append(max(diff, 0))
        losses.append(max(-diff, 0))
    period = 14
    # 初始平均用简单平均
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    # Wilder平滑
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
    if avg_loss == 0:
        rsi = 100
    else:
        rs = avg_gain / avg_loss
        rsi = 100 - (100 / (1 + rs))
    # RSI评分: 趋势中40-70为健康区间
    if 40 <= rsi <= 60:
        return 5  # 中性，最佳
    if 30 <= rsi < 40 or 60 < rsi <= 70:
        return 4  # 偏弱/偏强
    if 20 <= rsi < 30:
        return 3  # 超卖，可能反弹
    if 70 < rsi <= 80:
        return 2  # 偏强但有风险
    if rsi < 20:
        return 2  # 深度超卖
    return 1  # >80 超买风险


# ============================================================
# 市场情绪评分 (0-15)
# ============================================================

def score_turnover_percentile(kline):
    """换手率分位 (0-5): 近期成交量相对历史水平, v11.2: 3日vs10日"""
    if not kline or len(kline) < 13:
        return 2
    recent = [d.get('volume', 0) for d in kline[-3:]]
    hist = [d.get('volume', 0) for d in kline[-13:-3]]
    avg_recent = sum(recent) / len(recent) if recent else 0
    avg_hist = sum(hist) / len(hist) if hist else 1
    if avg_hist == 0:
        return 2
    ratio = avg_recent / avg_hist
    if ratio > 2.0:
        return 5  # 显著放量
    if ratio > 1.5:
        return 4  # 温和放量
    if ratio > 0.8:
        return 3  # 正常
    if ratio > 0.5:
        return 2  # 缩量
    return 1  # 极度缩量


def score_momentum(kline):
    """涨跌幅动量 (0-5): 3/5/10日涨跌幅综合, v11.2: 短期化"""
    if not kline or len(kline) < 11:
        return 2
    closes = [d['close'] for d in kline]
    # 3日涨幅
    chg3 = (closes[-1] - closes[-4]) / closes[-4] * 100 if closes[-4] else 0
    # 5日涨幅
    chg5 = (closes[-1] - closes[-6]) / closes[-6] * 100 if closes[-6] else 0
    # 10日涨幅
    chg10 = (closes[-1] - closes[-11]) / closes[-11] * 100 if closes[-11] else 0
    # 综合: 短期动量+中期趋势
    score = 0
    # 3日动量 (0-2)
    if 0 < chg3 <= 5: score += 2
    elif -3 < chg3 <= 0 or 5 < chg3 <= 10: score += 1
    # 5日趋势 (0-2)
    if 5 < chg5 <= 20: score += 2
    elif 0 < chg5 <= 5: score += 1
    # 10日方向 (0-1)
    if chg10 > 0: score += 1
    # 强势动量加分
    if chg3 > 3 and chg5 > 5:
        score = min(5, score + 1)
    return min(5, score)


def score_volatility(kline):
    """波动率评分 (0-5): 低波动=稳定=高分, v11.2: 10日窗口"""
    if not kline or len(kline) < 10:
        return 2
    closes = [d['close'] for d in kline[-10:]]
    if not closes or any(c == 0 for c in closes):
        return 2
    returns = [(closes[i] - closes[i-1]) / closes[i-1] for i in range(1, len(closes))]
    if not returns:
        return 2
    avg_ret = sum(returns) / len(returns)
    variance = sum((r - avg_ret) ** 2 for r in returns) / len(returns)
    std = variance ** 0.5
    daily_vol = std * 100  # 百分比
    # 低波动=高分
    if daily_vol < 1.5:
        return 5
    if daily_vol < 2.5:
        return 4
    if daily_vol < 3.5:
        return 3
    if daily_vol < 5.0:
        return 2
    return 1


# ============================================================
# 估值评分
# ============================================================

def _get_pe_percentile(code, years=3, quote=None):
    """从akshare获取近N年PE数据, 计算当前PE在历史中的百分位
    返回 (percentile, current_pe) 或 (None, None)
    percentile: 0-1之间, 0=历史最低, 1=历史最高
    """
    try:
        if ak is None:
            return None, None
        # 尝试 stock_a_indicator_lg 接口获取历史PE
        df = ak.stock_a_indicator_lg(symbol=code)
        if df is not None and len(df) > 0:
            # 筛选近N年数据
            cutoff = datetime.now() - timedelta(days=years*365)
            if 'trade_date' in df.columns:
                date_col = 'trade_date'
            elif '日期' in df.columns:
                date_col = '日期'
            else:
                date_col = df.columns[0]
            # 转换日期
            df[date_col] = pd.to_datetime(df[date_col], errors='coerce')
            df = df[df[date_col] >= cutoff]
            # 找PE列
            pe_col = None
            for c in df.columns:
                if 'pe' in c.lower() and 'ttm' in c.lower():
                    pe_col = c
                    break
            if pe_col is None:
                for c in df.columns:
                    if c.lower() in ('pe', 'pe_ttm'):
                        pe_col = c
                        break
            if pe_col is None:
                return None, None
            pe_values = df[pe_col].dropna()
            pe_values = pe_values[pe_values > 0]  # 排除负PE
            if len(pe_values) < 20:
                return None, None
            current_pe = quote.get('pe', 0) if quote else float(pe_values.iloc[-1])
            if current_pe <= 0:
                return None, None
            # 计算百分位: 当前PE在历史中的位置
            below = sum(1 for v in pe_values if v <= current_pe)
            percentile = below / len(pe_values)
            return percentile, current_pe
    except Exception:
        pass
    return None, None


def score_pe_historical(quote, industry_stats):
    """PE历史分位评分 (0-5), 优先用历史百分位, 含成长股溢价"""
    pe = quote.get('pe', 0)
    if pe <= 0:
        return 1
    code = quote.get('code', '')
    # 获取扣非增速用于成长股溢价
    financial = quote.get('_financial', None)
    deducted_growth = financial.get('deducted_growth_pct') if financial else None
    if deducted_growth is None:
        deducted_growth = USER_DATA.get(code, {}).get('eps_growth_pct', 0)
    
    # 尝试PE百分位 (3年)
    percentile, _ = _get_pe_percentile(code, quote=quote)
    if percentile is not None:
        # 成长股溢价: 增速>30%时，PE容忍度提升一档
        growth_premium = 0
        if deducted_growth and deducted_growth > 30:
            growth_premium = 0.15  # 估值区间右移15%
        adjusted_percentile = max(0, min(1, percentile - growth_premium))
        if adjusted_percentile < 0.20:
            return 5  # 历史低估区
        if adjusted_percentile < 0.40:
            return 4
        if adjusted_percentile < 0.60:
            return 3
        if adjusted_percentile < 0.80:
            return 2
        return 1  # 历史高估区
    # fallback: 绝对PE阈值 (含成长股溢价)
    if pe < 20:
        return 5
    if pe < 40:
        return 4
    if pe < 70:
        return 3
    if pe < 100:
        return 2
    return 1


def score_pb_rationality(quote, industry_stats):
    """PB合理性评分 (0-3)"""
    stock_pb = industry_stats[2]
    avg_pb = industry_stats[1]
    if stock_pb is None or avg_pb is None or avg_pb == 0:
        # 无法比较时给中间分（1分）
        return 1
    ratio = stock_pb / avg_pb
    if ratio <= 0.5:
        return 3
    if ratio <= 0.8:
        return 2
    return 1


def score_peg(quote):
    """PEG评分 (0-2) - 使用扣非增速"""
    code = quote.get('code', '')
    pe = quote.get('pe', 0)
    # 优先用扣非增速
    financial = quote.get('_financial', None)
    if financial:
        g = financial.get('deducted_growth_pct')
    else:
        g = None
    if g is None:
        g = USER_DATA.get(code, {}).get('eps_growth_pct', 0)
    if pe <= 0 or g <= 0:
        return 1
    peg = pe / g
    if peg <= 1.0:
        return 2
    return 1


# ============================================================
# 技术面评分
# ============================================================

def score_macd(closes):
    """MACD评分 (0-7)"""
    dif, dea, hist = calc_macd(closes)
    if dif is None:
        return 2
    score = 0
    # DIF > DEA (金叉状态)
    if dif > dea:
        score += 2
    # MACD柱为正且增长
    if hist > 0:
        score += 2
    # DIF从负转正
    if dif > 0:
        score += 1
    # DIF>0且hist增长 (额外加分)
    if dif > 0 and hist > 0:
        score += 1
    # hist连续放大
    if len(closes) > 30:
        _, _, hist_prev = calc_macd(closes[:-1])
        if hist_prev is not None and hist > hist_prev:
            score += 1
    return min(score, 7)


def score_ma_trend(closes):
    """均线趋势评分 (0-7), 增加MA支撑/压力子逻辑"""
    if len(closes) < 20:
        return 2
    ma5 = calc_ma(closes, 5)
    ma10 = calc_ma(closes, 10)
    ma20 = calc_ma(closes, 20)
    if ma5 is None or ma10 is None or ma20 is None:
        return 2
    price = closes[-1]
    score = 0
    # 多头排列
    if ma5 > ma10 > ma20:
        score += 3
    elif ma5 > ma10:
        score += 2
    elif price > ma10:
        score += 1
    # 价格在均线上方
    if price > ma5:
        score += 1
    if price > ma20:
        score += 1
    # v11.2: MA支撑/压力微调 (短期化)
    if price > ma10:
        score += 1  # 强支撑
    elif price < ma20:
        score -= 1  # 跌破MA20, 压力
    # 路径: ma10~ma20之间不加不减
    # 多头+价格在MA5上方 (额外加分)
    if ma5 > ma10 > ma20 and price > ma5:
        score += 1
    return min(7, max(0, score))


def score_volume_price(kline_data):
    """量价配合评分 (0-6)"""
    if len(kline_data) < 10:
        return 2
    volumes = [d['volume'] for d in kline_data]
    closes = [d['close'] for d in kline_data]
    vol_ratio = calc_volume_trend(volumes, 3)
    price_change = (closes[-1] - closes[-6]) / closes[-6] if closes[-6] != 0 else 0
    score = 0
    # 放量上涨
    if price_change > 0 and vol_ratio > 1.2:
        score += 3
    elif price_change > 0 and vol_ratio > 0.8:
        score += 2
    elif price_change < 0 and vol_ratio < 0.8:
        # 缩量下跌, 抛压减轻
        score += 2
    elif price_change > 0 and vol_ratio < 0.6:
        # 缩量上涨, 持续性存疑
        score += 1
    else:
        score += 1
    # 量能稳定性
    if 0.8 < vol_ratio < 1.5:
        score += 1
    # 放量+上涨+稳定 (额外加分)
    if price_change > 0 and vol_ratio > 1.0 and 0.8 < vol_ratio < 1.5:
        score += 1
    return min(score, 6)


# ============================================================
# 卡点评分
# ============================================================

# 行业卡点默认评分（不在CHOKEPOINT_DB中的票根据行业给分）
INDUSTRY_CHOKEPOINT_DEFAULT = _cfg.get('industry_chokepoint_default', {
    '半导体': 15, '芯片': 15, '存储': 14, '封测': 13,
    '电子化学品': 14, '光刻': 16, '设备': 14, '材料': 13,
    '军工': 12, '新能源': 11, '医药': 10, '消费': 9,
    '券商': 8, '银行': 7, '地产': 6,
})

def score_chokepoint(code, industry_name=None, chokepoint_db=None):
    """卡点评分 (0-25, 含综合加分)"""
    db = chokepoint_db if chokepoint_db is not None else CHOKEPOINT_DB
    if code not in db:
        # 不在DB中：根据行业给默认分
        if industry_name:
            for key, score in INDUSTRY_CHOKEPOINT_DEFAULT.items():
                if key in industry_name:
                    return score
        return 10  # 完全未知行业给中等分（10/25）
    strength, subst, competition = db[code]
    # 卡点强度 (0-8)
    strength_score = strength * 8 / 10
    # 国产替代 (0-6)
    subst_score = subst * 6 / 10
    # 竞争格局 (0-6)
    competition_score = competition * 6 / 10
    base = strength_score + subst_score + competition_score
    # 综合卡点评级加分 (0-5)
    bonus = 0
    if strength >= 8 and subst >= 8 and competition >= 7:
        bonus = 5  # 顶级卡点
    elif strength >= 7 and subst >= 7:
        bonus = 3  # 高卡点
    elif strength >= 6 or subst >= 6:
        bonus = 1  # 中等卡点
    total = base + bonus
    return min(round(total), 25)


# ============================================================
# 周期评分
# ============================================================

def auto_detect_cycle_stage(quote):
    """自动检测周期阶段"""
    code = quote.get('code', '')
    roe = USER_DATA.get(code, {}).get('roe_ttm', 0)
    g = USER_DATA.get(code, {}).get('eps_growth_pct', 0)
    pe = quote.get('pe', 0)
    # 底部: ROE低 + 增速差
    if roe < 3 and g < 0:
        return 'bottom'
    if roe < 5 and g < -10:
        return 'bottom'
    # 复苏: ROE中等 + 增速改善
    if 5 <= roe <= 15 and g > 20:
        return 'recovery'
    if roe < 8 and g > 0:
        return 'recovery'
    # 顶部: ROE极高或PE极高
    if roe > 25 or pe > 150:
        return 'peak'
    # 衰退
    if roe > 15 and g < 0:
        return 'decline'
    return 'none'


def detect_cycle_stage(quote):
    """检测周期阶段, 优先使用override"""
    code = quote.get('code', '')
    if code in CYCLE_STOCKS_OVERRIDE:
        industry_name, stage = CYCLE_STOCKS_OVERRIDE[code]
        return industry_name, stage
    # 尝试匹配行业
    if code in INDUSTRY_PEERS:
        industry_name = INDUSTRY_PEERS[code][0]
    else:
        industry_name = '未知'
    stage = auto_detect_cycle_stage(quote)
    return industry_name, stage


# ============================================================
# 主分析函数
# ============================================================

def analyze(codes, skip_industry=False, chokepoint_overrides=None, skip_trends=False):
    """主分析: 对每只股票计算5维度评分 (v12)"""
    # 用本地副本避免污染全局CHOKEPOINT_DB
    local_chokepoint = dict(CHOKEPOINT_DB)
    if chokepoint_overrides:
        local_chokepoint.update(chokepoint_overrides)
    results = []
    # 大盘行情只需获取一次（循环外）
    market_quote = None
    try:
        market_quote = get_quote_tencent('000001')
    except Exception:
        pass
    # Tushare批量获取行情(PE/PB/换手率/总市值)
    tushare_quotes = batch_quote_tushare(codes)
    # Tushare批量获取财务数据(替代逐只东方财富F10)
    tushare_financials = batch_financial_tushare(codes)
    # Tushare批量获取K线(替代逐只新浪)
    tushare_klines = batch_kline_tushare(codes, 120)
    # Tushare批量获取资金流向
    tushare_moneyflows = batch_moneyflow_tushare(codes)
    # Tushare批量获取风险因子
    tushare_risks = batch_risk_tushare(codes)
    # 批量获取股票名称(stock_basic)
    stock_names = {}
    if _TUSHARE_PRO is not None:
        try:
            ts_codes = [_tushare_code(c) for c in codes]
            df = _TUSHARE_PRO.stock_basic(ts_code=','.join(ts_codes), fields='ts_code,name')
            if df is not None and len(df) > 0:
                for _, row in df.iterrows():
                    code = row['ts_code'].split('.')[0]
                    stock_names[code] = row['name']
        except Exception:
            pass
    for code in codes:
     try:
        print(f'  正在分析 {code}...', file=sys.stderr)
        # 行情: 优先用Tushare批量数据
        quote = tushare_quotes.get(code)
        if not quote or quote.get('price', 0) == 0:
            # fallback到腾讯
            quote = get_quote_tencent(code)
        if not quote or quote.get('price', 0) == 0:
            print(f'  ⚠ {code} 无法获取行情, 跳过', file=sys.stderr)
            continue
        # 补充股票名称
        if 'name' not in quote or not quote.get('name'):
            quote['name'] = stock_names.get(code, code)
        # K线: 优先用Tushare批量数据
        kline = tushare_klines.get(code, [])
        if not kline:
            kline = get_kline_sina(code, 120)  # fallback到新浪
        closes = [d['close'] for d in kline]
        # 行业PB
        if skip_industry:
            industry_stats = (None, None, None, None)
        else:
            industry_stats = get_industry_pb_stats(code)
        # 行业名称 (用于备注)
        cycle_info = detect_cycle_stage(quote)
        industry_name = cycle_info[0]
        stage_label = cycle_info[1]
        # 财务数据: 优先用Tushare批量数据，回退到东方财富F10
        financial = tushare_financials.get(code)
        if financial is None:
            financial = get_financial_data(code)  # 东方财富F10回退
        if financial:
            quote['_financial'] = financial  # 保存完整财务数据(含3年历史)
            # 将latest中的字段合并到quote(保持向后兼容)
            latest = financial.get('latest', {})
            for k, v in latest.items():
                if v is not None:
                    quote[k] = v

        # ---- 基本面 (25) ----
        fund_roe = score_roe(quote)
        fund_growth = score_growth(quote)
        fund_debt = score_debt(quote)
        fund_cashflow = score_cashflow(quote)
        fund_margin = score_net_margin(quote)
        # 增速稳定性 bonus/penalty (合并akshare调用)
        fin_trends = None if skip_trends else _get_financial_trends(code)
        growth_stability_adj = 0
        if fin_trends and 'std_dev' in fin_trends:
            std_dev = fin_trends['std_dev']
            if std_dev < 10:
                growth_stability_adj = 1  # 稳定加分
            elif std_dev > 25:
                growth_stability_adj = -1  # 波动大扣分
        fund_growth = max(0, min(5, fund_growth + growth_stability_adj))
        # v12: 连续3年扣非增速递增加分
        growth_trend_adj = 0
        if financial and 'years' in financial:
            years = financial['years']
            if len(years) >= 3:
                deducted_vals = [y.get('deducted_profit') for y in years[:3]]
                # 检查连续递增: 今年>去年>前年
                if all(v is not None for v in deducted_vals):
                    if deducted_vals[0] > deducted_vals[1] > deducted_vals[2] and deducted_vals[2] > 0:
                        growth_trend_adj = 1  # 连续3年递增, +1分
        fund_growth = max(0, min(5, fund_growth + growth_trend_adj))
        # 扣非净利润占比 → fund_margin调整
        deducted_adj = 0
        try:
            deducted_profit = float(quote.get('deducted_profit', 0) or 0)
            net_profit = float(quote.get('net_profit', 0) or 0)
        except (TypeError, ValueError):
            deducted_profit = net_profit = 0
        # 确保值有效
        if net_profit > 0 and deducted_profit > 0:
            deducted_ratio = deducted_profit / net_profit
            if deducted_ratio > 0.9:
                deducted_adj = 1  # 主业赚钱, +1分
            elif deducted_ratio < 0.5:
                deducted_adj = -1  # 靠非经常性损益, -1分
        fund_margin = max(0, min(6, fund_margin + deducted_adj))
        fundamental = min(25, fund_roe + fund_growth + fund_debt + fund_cashflow + fund_margin)

        # ---- 估值 (10) ----
        val_pe = score_pe_historical(quote, industry_stats)
        val_pb = score_pb_rationality(quote, industry_stats)
        val_peg = score_peg(quote)
        valuation = min(10, val_pe + val_pb + val_peg)

        # ---- 卡点 (25) ----
        chokepoint = score_chokepoint(code, industry_name, chokepoint_db=local_chokepoint)

        # ---- 技术面 (25) ----
        tech_macd = score_macd(closes) if closes else 2
        tech_ma = score_ma_trend(closes) if closes else 2
        tech_vp = score_volume_price(kline) if kline else 2
        tech_rsi = score_rsi(closes) if closes else 2
        technical = tech_macd + tech_ma + tech_vp + tech_rsi
        # ---- v12: 技术综合加分 (合并趋势+动量，避免重复) ----
        trend_bonus = 0
        if kline and len(kline) >= 20:
            closes_all = [d['close'] for d in kline]
            chg5d = (closes_all[-1] - closes_all[-6]) / closes_all[-6] * 100 if closes_all[-6] else 0
            chg20d = (closes_all[-1] - closes_all[-21]) / closes_all[-21] * 100 if len(closes_all) >= 21 and closes_all[-21] else 0
            # 趋势+动量综合评分 (0-5，不重复)
            if tech_macd >= 6 and tech_ma >= 6 and tech_vp >= 4:
                trend_bonus = 5  # 完美趋势
            elif tech_macd >= 6 and tech_ma >= 6:
                trend_bonus = 4  # 强势趋势
            elif tech_macd >= 5 and tech_ma >= 5 and chg5d > 5:
                trend_bonus = 3  # 趋势形成+动量确认
            elif tech_macd >= 5 or (tech_ma >= 5 and chg5d > 0):
                trend_bonus = 2  # 趋势初期
            elif tech_macd >= 4 or tech_ma >= 4:
                trend_bonus = 1  # 趋势萌芽
        
        # ---- v12: 跌破MA20扣分 ----
        ma20_penalty = 0
        if closes and len(closes) >= 20:
            ma20 = calc_ma(closes, 20)
            price = closes[-1]
            if ma20 and price < ma20 * 0.97:  # 跌破MA20超3%
                ma20_penalty = -2  # 扣2分
            elif ma20 and price < ma20:  # 刚跌破MA20
                ma20_penalty = -1  # 扣1分
        
        technical = min(25, max(0, technical + trend_bonus + ma20_penalty))

        # ---- 市场情绪 (15) ----
        sent_turnover = score_turnover_percentile(kline) if kline else 2
        sent_momentum = score_momentum(kline) if kline else 2
        sent_volatility = score_volatility(kline) if kline else 2
        sentiment = sent_turnover + sent_momentum + sent_volatility
        
        # ---- v12: 相对强度加分 (0-3) ----
        market_bonus = 0
        try:
            if market_quote and market_quote.get('change_pct') is not None:
                market_chg = market_quote['change_pct']
                stock_chg = quote.get('change_pct', 0)
                # 日内相对强度
                if market_chg < -1 and stock_chg > 0:
                    rs = stock_chg - market_chg
                    if rs > 5: market_bonus = 3
                    elif rs > 2: market_bonus = 2
                    else: market_bonus = 1
                elif market_chg > 0 and stock_chg > market_chg * 1.5:
                    market_bonus = 1
                # 多周期相对强度（用K线数据）
                if kline and len(kline) >= 20:
                    closes_rs = [d['close'] for d in kline]
                    stock_5d = (closes_rs[-1] - closes_rs[-6]) / closes_rs[-6] * 100 if closes_rs[-6] else 0
                    stock_20d = (closes_rs[-1] - closes_rs[-21]) / closes_rs[-21] * 100 if len(closes_rs) >= 21 and closes_rs[-21] else 0
                    # 5日跑赢大盘5%+
                    if stock_5d > 5 and stock_5d > market_chg * 2:
                        market_bonus = max(market_bonus, 2)
                    # 20日持续强势
                    if stock_20d > 10:
                        market_bonus = max(market_bonus, 1)
        except Exception:
            pass
        sentiment = min(15, sentiment + market_bonus)

        # ---- v12: 资金流向加分 (0-3) ----
        moneyflow_bonus = 0
        try:
            mf_score = tushare_moneyflows.get(code)
            if mf_score is None:
                mf_score = score_moneyflow(code)  # fallback到逐只
            if mf_score is not None:
                if mf_score >= 5:
                    moneyflow_bonus = 3   # 连续净流入, 强烈看多
                elif mf_score >= 4:
                    moneyflow_bonus = 2   # 整体净流入
                elif mf_score >= 3:
                    moneyflow_bonus = 1   # 持平
                # mf_score <= 2 不加分 (净流出)
        except Exception:
            pass
        sentiment = min(15, sentiment + moneyflow_bonus)

        total = fundamental + valuation + chokepoint + technical + sentiment
        
        # ---- 加减分项 ----
        pe = quote.get('pe', 0)
        # 亏损股扣分: PE为负(亏损) → -5分
        if pe <= 0:
            total -= 5
        # 商誉占比防爆雷
        goodwill = quote.get('goodwill_to_equity', None)
        if goodwill is not None:
            try:
                gw = float(goodwill)
                if gw > 50:
                    total -= 3  # 商誉占比极高
                elif gw > 30:
                    total -= 2  # 商誉占比偏高
            except (ValueError, TypeError):
                pass
        # 同步加速加分 + 增收不增利扣分 (复用fin_trends数据)
        if fin_trends:
            rev = fin_trends.get('revenue_growths', [])
            prof = fin_trends.get('profit_growths', [])
            # 同步加速: 近3年净利润增速连续递增
            if len(prof) >= 3 and prof[0] < prof[1] < prof[2]:
                total += 2  # 加速加分
            # 增收不增利: 营收增但利润降(最近一年)
            if len(rev) >= 2 and len(prof) >= 2:
                if rev[-1] > 0 and prof[-1] < 0:
                    total -= 3  # 增收不增利扣分
        
        # ---- 风险因子(限售解禁/大宗交易/股权质押) ----
        unlock_risk = 0
        block_risk = 0
        pledge_risk = 0
        if _TUSHARE_PRO is not None:
            # 使用批量数据
            risk_data = tushare_risks.get(code, {'unlock': 0, 'pledge': 0})
            unlock_risk = risk_data.get('unlock', 0)
            pledge_risk = risk_data.get('pledge', 0)
            if unlock_risk < 0:
                total += unlock_risk
            if pledge_risk < 0:
                total += pledge_risk
        
        total = max(0, min(100, total))

        # 评级
        if total >= 80:
            rating = 'S'
        elif total >= 70:
            rating = 'A'
        elif total >= 60:
            rating = 'B'
        elif total >= 45:
            rating = 'C'
        else:
            rating = 'D'

        stage_cn = {
            'bottom': '底部', 'recovery': '复苏',
            'peak': '顶部', 'decline': '衰退', 'none': '—'
        }.get(stage_label, '—')

        results.append({
            'code': code,
            'name': quote['name'],
            'total': total,
            'rating': rating,
            'fundamental': fundamental,
            'valuation': valuation,
            'chokepoint': chokepoint,
            'technical': technical,
            'sentiment': sentiment,
            'industry_name': industry_name,
            'stage_cn': stage_cn,
            # 子项明细
            'detail': {
                'fund_roe': fund_roe, 'fund_growth': fund_growth,
                'fund_debt': fund_debt, 'fund_cashflow': fund_cashflow,
                'fund_margin': fund_margin,
                'val_pe': val_pe, 'val_pb': val_pb, 'val_peg': val_peg,
                'tech_macd': tech_macd, 'tech_ma': tech_ma, 'tech_vp': tech_vp,
                'tech_rsi': tech_rsi,
                'sent_turnover': sent_turnover, 'sent_momentum': sent_momentum,
                'sent_volatility': sent_volatility,
                'fund_flow': moneyflow_bonus,
                'sw_industry': industry_stats[0] if industry_stats[0] else industry_name,
                'unlock_risk': unlock_risk,
                'block_risk': block_risk,
                'pledge_risk': pledge_risk,
            }
        })
     except Exception as e:
        print(f'  ✗ {code} 评分异常: {e}', file=sys.stderr)
        continue

    # 按总分降序
    results.sort(key=lambda x: x['total'], reverse=True)
    return results


# ============================================================
# 输出格式化
# ============================================================

def print_results(results):
    """格式化输出结果"""
    now = datetime.now().strftime('%Y-%m-%d %H:%M')
    print()
    print('=' * 90)
    print('  Serenity产业链卡点投资评分系统 v12 · 满分100')
    print('  基本面25 + 估值10 + 卡点25 + 技术25 + 情绪15')
    print(f'  评分时间: {now}')
    print('=' * 90)
    print()
    header = '{:<4}{:<10}{:<10}{:<6}{:<5}{:<8}{:<7}{:<7}{:<7}{:<7}{}'.format(
        '#', '代码', '名称', '总分', '评级', '基本面', '估值', '卡点', '技术', '情绪', '备注')
    print(header)
    print('-' * 90)
    for i, r in enumerate(results, 1):
        note = '[{}:{}]'.format(r['industry_name'], r['stage_cn'])
        fv = str(r['fundamental'])
        vv = str(r['valuation'])
        cv = str(r['chokepoint'])
        tv = str(r['technical'])
        sv = str(r['sentiment'])
        line = '{:<4}{:<10}{:<10}{:<6}{:<5}{}/25{}  {}/10{}  {}/25{}  {}/25{}  {}/15{} {}'.format(
            i, r['code'], r['name'], r['total'], r['rating'],
            fv, ' ' * max(0, 2 - len(fv)),
            vv, ' ' * max(0, 2 - len(vv)),
            cv, ' ' * max(0, 2 - len(cv)),
            tv, ' ' * max(0, 2 - len(tv)),
            sv, ' ' * max(0, 1 - len(sv)),
            note,
        )
        print(line)
    print('-' * 90)

    # 详细子项
    print()
    print('  📊 子项明细:')
    for r in results:
        d = r['detail']
        print(
            f'  {r["code"]} {r["name"]}: '
            f'ROE={d["fund_roe"]}/5 增速={d["fund_growth"]}/5 '
            f'负债={d["fund_debt"]}/5 现金={d["fund_cashflow"]}/4 净利率={d["fund_margin"]}/6 | '
            f'PE={d["val_pe"]}/5 PB={d["val_pb"]}/3 PEG={d["val_peg"]}/2 | '
            f'MACD={d["tech_macd"]}/7 均线={d["tech_ma"]}/7 量价={d["tech_vp"]}/6 RSI={d["tech_rsi"]}/5 | '
            f'放量={d["sent_turnover"]}/5 动量={d["sent_momentum"]}/5 波动={d["sent_volatility"]}/5',
        )
    print()


# ============================================================
# 回测验证
# ============================================================

def backtest_score(code, days=60):
    """
    回测评分有效性：用历史数据计算评分，对比后续涨幅
    返回：{score, forward_5d, forward_10d, forward_20d, win_rate}
    """
    kline = get_kline_sina(code, days + 30)
    if not kline or len(kline) < days + 20:
        return None

    closes = [d['close'] for d in kline]

    # 用前days天数据计算评分
    past_closes = closes[:days]
    past_kline = kline[:days]

    # 简化评分：只算技术面+情绪面（不需要API）
    tech_macd = score_macd(past_closes) if past_closes else 0
    tech_ma = score_ma_trend(past_closes) if past_closes else 0
    tech_rsi = score_rsi(past_closes) if past_closes else 0
    tech_score = tech_macd + tech_ma + tech_rsi

    sent_momentum = score_momentum(past_kline) if past_kline else 0
    sent_turnover = score_turnover_percentile(past_kline) if past_kline else 0
    sent_score = sent_momentum + sent_turnover

    total_approx = tech_score + sent_score  # 简化版总分

    # 计算后续涨幅
    current_price = closes[days - 1]
    if current_price <= 0:
        forward_5d = forward_10d = forward_20d = None
    else:
        forward_5d = (closes[days + 4] - current_price) / current_price * 100 if len(closes) > days + 4 else None
        forward_10d = (closes[days + 9] - current_price) / current_price * 100 if len(closes) > days + 9 else None
        forward_20d = (closes[days + 19] - current_price) / current_price * 100 if len(closes) > days + 19 else None

    return {
        'code': code,
        'score_approx': round(total_approx, 1),
        'tech': round(tech_score, 1),
        'sent': round(sent_score, 1),
        'forward_5d': round(forward_5d, 2) if forward_5d is not None else None,
        'forward_10d': round(forward_10d, 2) if forward_10d is not None else None,
        'forward_20d': round(forward_20d, 2) if forward_20d is not None else None,
    }


def backtest_batch(codes, days=60):
    """批量回测，输出评分vs涨幅对照表"""
    results = []
    for code in codes:
        r = backtest_score(code, days)
        if r:
            results.append(r)
    # 按评分排序
    results.sort(key=lambda x: x['score_approx'], reverse=True)
    # 输出
    print(f"{'代码':>8} {'评分':>6} {'技术':>5} {'情绪':>5} {'5日':>7} {'10日':>7} {'20日':>7}")
    print('-' * 55)
    for r in results:
        f5 = f"{r['forward_5d']:+.1f}%" if r['forward_5d'] is not None else '—'
        f10 = f"{r['forward_10d']:+.1f}%" if r['forward_10d'] is not None else '—'
        f20 = f"{r['forward_20d']:+.1f}%" if r['forward_20d'] is not None else '—'
        print(f"{r['code']:>8} {r['score_approx']:>6.1f} {r['tech']:>5.1f} {r['sent']:>5.1f} {f5:>7} {f10:>7} {f20:>7}")
    return results


# ============================================================
# CLI
# ============================================================

if __name__ == '__main__':
    default_codes = _cfg.get('stocks', ['688019', '688008', '600030', '600584'])
    codes = sys.argv[1:] if len(sys.argv) > 1 else default_codes
    skip_industry = '--skip-industry' in codes
    codes = [c for c in codes if c != '--skip-industry']
    
    # 解析 --chokepoint-json
    chokepoint_overrides = None
    for i, c in enumerate(codes):
        if c == '--chokepoint-json' and i + 1 < len(codes):
            json_path = codes[i + 1]
            codes = [x for x in codes if x not in ('--chokepoint-json', json_path)]
            try:
                with open(json_path) as f:
                    raw = json.load(f)
                chokepoint_overrides = {k: tuple(v) for k, v in raw.items()}
            except Exception as e:
                print(f'  ⚠ 读取chokepoint JSON失败: {e}', file=sys.stderr)
            break

    # 回测模式
    if '--backtest' in codes:
        codes = [c for c in codes if c != '--backtest']
        if not codes:
            codes = default_codes
        print(f'回测模式: {len(codes)} 只股票...', file=sys.stderr)
        backtest_batch(codes)
    else:
        print(f'Serenity评分系统 v12 启动, 分析 {len(codes)} 只股票...', file=sys.stderr)
        results = analyze(codes, skip_industry=skip_industry, chokepoint_overrides=chokepoint_overrides)
        print_results(results)
