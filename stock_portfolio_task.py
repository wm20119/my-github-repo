#!/usr/bin/env python3
"""
统一任务脚本 — 模拟盘操作 + 股票池信号扫描
所有盘中任务（09:30/11:30/14:50/15:30）统一入口

模式：
  --morning   09:30 早盘买入 + 股票池扫描
  --midday    14:50 盘中检查（出场+买入）+ 股票池扫描
  --summary   15:30 日报总结（只读）
"""
import sys, os, json, time, argparse, logging, shutil
from datetime import datetime
sys.path.insert(0, os.path.expanduser('~/.hermes/scripts'))

# 日志模块
from stock_log import log_trade

# 简单日志
LOG_DIR = os.path.expanduser('~/.hermes/cache/logs')
os.makedirs(LOG_DIR, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(message)s',
    handlers=[logging.FileHandler(os.path.join(LOG_DIR, 'stock_system.log'), encoding='utf-8')],
)
log = logging.getLogger('stock')


from kline_db import get_klines
from chanlun_strategy import (
    WINDOW, evaluate_chanlun_quality, scan_recent_signals,
)
from stock_screening import POOL_CODES
from sim_portfolio import (
    do_exits, do_buys,
    load_portfolio, save_portfolio, load_backup_pool,
    load_nav, save_nav,
    INITIAL_CAPITAL, MAX_POSITIONS,
)

# ========== Tushare 初始化 ==========
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

def _fetch_realtime_tencent(codes):
    """从腾讯行情API获取实时价格（盘中优先用这个）"""
    import urllib.request
    symbols = []
    for c in codes:
        if c.startswith('6') or c.startswith('9'):
            symbols.append(f'sh{c}')
        elif c.startswith('8'):
            symbols.append(f'bj{c}')
        else:
            symbols.append(f'sz{c}')
    url = f'http://qt.gtimg.cn/q={",".join(symbols)}'
    try:
        req = urllib.request.Request(url)
        resp = urllib.request.urlopen(req, timeout=10)
        data = resp.read().decode('gbk')
    except Exception:
        return {}
    result = {}
    for line in data.strip().split(';'):
        line = line.strip()
        if not line or '=' not in line:
            continue
        parts = line.split('"')[1].split('~')
        if len(parts) < 50:
            continue
        code = parts[2]
        result[code] = {
            'date': parts[30][:8],
            'open': float(parts[5]),
            'high': float(parts[33]),
            'low': float(parts[34]),
            'close': float(parts[3]),
            'volume': float(parts[6]),
        }
    return result

def fetch_today_klines(codes):
    """获取今天K线：盘中用腾讯实时API，收盘后用Tushare"""
    if not codes:
        return {}
    today = datetime.now().strftime('%Y%m%d')
    now_hour = datetime.now().hour

    # 盘中（9:30-15:00）优先用腾讯实时
    if 9 <= now_hour < 16:
        result = _fetch_realtime_tencent(codes)
        if result:
            # 只返回今天日期的数据
            return {c: d for c, d in result.items() if d['date'] == today}

    # 收盘后或腾讯失败，用Tushare
    _init_tushare()
    if _TUSHARE_PRO is None:
        return {}
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
        log.error(f"Tushare批量拉取失败: {e}")
    return result

# ========== 股票池 ==========
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
    """加载最近一次选股扫描的结果"""
    cache_file = os.path.expanduser('~/.hermes/cache/screening_latest.json')
    if not os.path.exists(cache_file):
        return []
    try:
        with open(cache_file) as f:
            data = json.load(f)
        # 结构校验
        if not isinstance(data, dict) or 'passed' not in data:
            log.error(f"screening_latest.json结构异常: {list(data.keys()) if isinstance(data, dict) else type(data)}")
            return []
        # 时间校验：超过1天的数据不用
        data_date = data.get('date', '')
        if data_date:
            try:
                data_dt = datetime.strptime(data_date, '%Y%m%d')
                if (datetime.now() - data_dt).days > 1:
                    log.info(f"screening数据过期: {data_date}")
                    return []
            except ValueError:
                pass
        return [(c, info['score']['name']) for c, info in data.get('passed', {}).items()]
    except Exception:
        return []

# ========== 股票池信号扫描 ==========
def scan_one(code, name, today_klines=None):
    """扫描单只票，返回结果字典"""
    try:
        kl = get_klines(code)
        if not kl or len(kl) < WINDOW + 100:
            return None
        today_str = datetime.now().strftime('%Y%m%d')
        if kl[-1]['date'] != today_str:
            if today_klines and code in today_klines:
                kl.append(today_klines[code])
        stats = evaluate_chanlun_quality(kl, name, code)
        quality = stats['quality'] if stats else '?'
        wr = stats['win_rate'] if stats else 0
        pf = stats['profit_factor'] if stats else 0
        trades = stats['total_trades'] if stats else 0
        # 三买信号：只看最后1根K线（与选股系统一致）
        today_signal = scan_recent_signals(kl, name, code, lookback=1)
        latest = kl[-1]
        closes = [d['close'] for d in kl]
        ma20_now = sum(closes[-20:]) / 20 if len(closes) >= 20 else closes[-1]
        above_now = (latest['close'] - ma20_now) / ma20_now * 100 if ma20_now > 0 else 0
        return {
            'code': code, 'name': name,
            'price': latest['close'], 'above_ma20': above_now,
            'quality': quality, 'win_rate': wr, 'profit_factor': pf,
            'total_trades': trades,
            'has_signal': bool(today_signal),
            'signal': today_signal[-1] if today_signal else None,
        }
    except Exception as e:
        log.error(f"{name}({code}) 扫描失败: {e}")
        return None

# ========== 模拟盘操作报告 ==========
def format_portfolio_ops(bought, sold, stuck, skipped):
    """格式化模拟盘操作结果"""
    lines = []
    if bought:
        lines.append(f"\n📥 买入 {len(bought)}只:")
        for b in bought:
            lines.append(f"  {b['name']}({b['code']}) ¥{b['entry_price']:.2f} {b['shares']}股 ZG={b.get('pivot_zg', '?')}")
    if sold:
        lines.append(f"\n📤 卖出 {len(sold)}只:")
        for s in sold:
            lines.append(f"  {s['name']}({s['code']}) {s['entry_date']}→{s['exit_date']} "
                         f"买{s['entry_price']:.2f}→卖{s['exit_price']:.2f} {s['pnl_pct']:+.1f}% [{s['exit_reason']}]")
    if stuck:
        lines.append(f"\n🔒 一字跌停无法卖出 {len(stuck)}只（继续持有）:")
        for s in stuck:
            pnl = (s['current_price'] - s['entry_price']) / s['entry_price'] * 100
            lines.append(f"  {s['name']}({s['code']}) 买{s['entry_price']:.2f}→现{s['current_price']:.2f} "
                         f"{pnl:+.1f}% 触发{s['reason']} 持{s['hold_days']}天")
    if skipped:
        lines.append(f"\n🚫 一字涨停无法买入 {len(skipped)}只:")
        for s in skipped:
            lines.append(f"  {s['name']}({s['code']}) ¥{s['price']:.2f} ZG={s['zg']:.1f} [{s['chanlun_quality']}级]")
    return lines

def format_pool_scan(results, pool_codes):
    """格式化股票池扫描结果"""
    lines = []
    has_signal = [r for r in results if r['has_signal']]
    pool_results = [r for r in results if r['code'] in pool_codes]
    screen_results = [r for r in results if r['code'] not in pool_codes]

    lines.append(f"\n{'=' * 50}")
    lines.append(f"📌 股票池 ({len(pool_results)}只)")
    lines.append("=" * 50)
    for r in pool_results:
        pf_str = 'N/A' if r['profit_factor'] == 0 else ('∞' if r['profit_factor'] >= 999.99 else f"{r['profit_factor']:.2f}")
        line = f"\n{r['name']}({r['code']}) {r['price']:.2f} 离MA20{r['above_ma20']:+.1f}%"
        line += f"\n  质量: {r['quality']}级 胜率{r['win_rate']:.0f}%({r['total_trades']}笔) 盈亏比{pf_str}"
        if r['has_signal']:
            sig = r['signal']
            line += f"\n  🔔 三买: {sig['date']} ¥{sig['price']:.2f} RSI={sig['rsi']:.0f} 量比={sig['vol_ratio']:.2f}"
        else:
            line += "\n  无三买信号"
        lines.append(line)

    if screen_results:
        lines.append(f"\n{'=' * 50}")
        lines.append(f"🎯 选股候选 ({len(screen_results)}只)")
        lines.append("=" * 50)
        for r in sorted(screen_results, key=lambda x: -x['win_rate']):
            pf_str = 'N/A' if r['profit_factor'] == 0 else ('∞' if r['profit_factor'] >= 999.99 else f"{r['profit_factor']:.2f}")
            line = f"\n{r['name']}({r['code']}) {r['price']:.2f} 离MA20{r['above_ma20']:+.1f}%"
            line += f"\n  质量: {r['quality']}级 胜率{r['win_rate']:.0f}%({r['total_trades']}笔) 盈亏比{pf_str}"
            if r['has_signal']:
                sig = r['signal']
                line += f"\n  🔔 三买: {sig['date']} ¥{sig['price']:.2f} RSI={sig['rsi']:.0f} 量比={sig['vol_ratio']:.2f}"
            else:
                line += "\n  无三买信号"
            lines.append(line)

    if has_signal:
        lines.append(f"\n{'=' * 50}")
        lines.append(f"🔔 有三买信号的票: {len(has_signal)}只")
        lines.append("=" * 50)
        for r in has_signal:
            sig = r['signal']
            lines.append(f"  {r['name']}({r['code']}) ¥{sig['price']:.2f} RSI={sig['rsi']:.0f} 量比={sig['vol_ratio']:.2f}")
    else:
        lines.append("\n当前无三买信号触发。")

    return lines

def format_account(pf, nav_history):
    """格式化账户状态"""
    lines = []
    total_mv = sum(p['market_value'] for p in pf['positions'])
    nav = pf['cash'] + total_mv
    nav_pct = (nav - INITIAL_CAPITAL) / INITIAL_CAPITAL * 100

    lines.append(f"\n💰 账户:")
    lines.append(f"  总资产: ¥{nav:,.0f} ({nav_pct:+.1f}%)")
    lines.append(f"  现金: ¥{pf['cash']:,.0f}")
    lines.append(f"  持仓市值: ¥{total_mv:,.0f}")
    lines.append(f"  持仓数: {len(pf['positions'])}/{MAX_POSITIONS}")

    if pf['positions']:
        lines.append(f"\n📋 持仓明细:")
        for p in pf['positions']:
            lines.append(f"  {p['name']}({p['code']}) {p['shares']}股 "
                         f"买{p['entry_price']:.2f}→现{p['current_price']:.2f} "
                         f"{p['pnl_pct']:+.1f}% 持{p['hold_days']}天")

    if pf['trades']:
        wins = [t for t in pf['trades'] if t['pnl_pct'] > 0]
        lines.append(f"\n📈 历史统计:")
        lines.append(f"  总交易: {len(pf['trades'])}笔")
        lines.append(f"  胜率: {len(wins)}/{len(pf['trades'])} = {len(wins) / len(pf['trades']) * 100:.0f}%")

    if len(nav_history) > 1:
        lines.append(f"\n📉 净值趋势（近10天）:")
        for n in nav_history[-10:]:
            lines.append(f"  {n['date']} {n['nav_pct']:+.1f}%")

    return lines

# ========== 核心：拉K线 + 操作 + 扫描 ==========
def run_core(do_portfolio_ops=True):
    """
    统一核心逻辑：
    1. 拉当天K线（股票池+持仓+备选池）
    2. 模拟盘操作（可选）
    3. 股票池扫描
    4. 输出报告
    """
    now = datetime.now()
    today = now.strftime('%Y%m%d')
    report = []

    # 收集所有代码
    all_stocks = [(c, n) for c, n in POOL]
    screening = load_screening_results()
    pool_codes = [c for c, _ in POOL]
    for c, n in screening:
        if c not in pool_codes:
            all_stocks.append((c, n))

    pf = load_portfolio()
    pool = load_backup_pool()
    nav_history = load_nav()

    all_codes = list(set(
        [c for c, _ in all_stocks]
        + [p['code'] for p in pf['positions']]
        + [s['code'] for s in pool]
    ))
    today_klines = fetch_today_klines(all_codes) if all_codes else {}
    if today_klines:
        report.append(f"  Tushare补充{len(today_klines)}只当天K线")

    # 模拟盘操作
    bought, sold, stuck, skipped = [], [], [], []
    if do_portfolio_ops and (pf['positions'] or pool):
        if pf['positions']:
            sold, remaining, stuck = do_exits(pf, today, now, report, today_klines=today_klines)
            pf['positions'] = remaining
            save_portfolio(pf)  # 出场后立即保存，防止买入崩溃丢数据
        if pool:
            bought, skipped = do_buys(pf, pool, today, now, report, today_klines=today_klines)
        save_portfolio(pf)

    # 模拟盘操作报告
    report.extend(format_portfolio_ops(bought, sold, stuck, skipped))

    # 交易日志写入SQLite
    for b in bought:
        log_trade(b['code'], b['name'], 'buy', b['entry_price'], b['shares'],
                  pivot_zg=b.get('pivot_zg'), extra=b.get('chanlun_quality'))
    for s in sold:
        log_trade(s['code'], s['name'], 'sell', s['exit_price'], None,
                  pnl_pct=s['pnl_pct'], reason=s['exit_reason'], hold_days=s['hold_days'])

    # 股票池扫描
    results = []
    scan_errors = []
    for i, (code, name) in enumerate(all_stocks):
        if i % 5 == 0:
            report.append(f"\n  扫描进度: {i + 1}/{len(all_stocks)}")
        r = scan_one(code, name, today_klines)
        if r:
            results.append(r)
        else:
            scan_errors.append(f"{name}({code})")

    # 扫描错误汇总记日志
    if scan_errors:
        log.error(f"扫描失败{len(scan_errors)}只: {', '.join(scan_errors[:5])}")

    report.extend(format_pool_scan(results, pool_codes))

    # 账户状态
    report.extend(format_account(pf, nav_history))

    # 更新净值（每次都保存，不只是有交易时）
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

    return "\n".join(report)

# ========== 三个模式 ==========
def run_morning():
    """09:30 早盘买入 + 股票池扫描"""
    now = datetime.now()
    report = [f"📊 模拟盘·早盘 {now.strftime('%Y%m%d %H:%M')}",
              f"规则: 三买8层过滤入场 | 100万×10只×10%",
              "=" * 50]
    report.extend(run_core(do_portfolio_ops=True).split("\n"))
    return "\n".join(report)

def run_midday():
    """14:50 盘中检查 + 股票池扫描"""
    now = datetime.now()
    report = [f"📊 模拟盘·盘中 {now.strftime('%Y%m%d %H:%M')}",
              f"规则: 止盈+20%/止损-5%/超时60天 | 100万×10只×10%",
              "=" * 50]
    report.extend(run_core(do_portfolio_ops=True).split("\n"))
    return "\n".join(report)

def run_summary():
    """15:30 日报总结（只读）"""
    now = datetime.now()
    pf = load_portfolio()
    nav_history = load_nav()
    report = [f"📊 模拟盘·日报 {now.strftime('%Y%m%d %H:%M')}",
              "=" * 50]
    report.extend(format_account(pf, nav_history))
    return "\n".join(report)


def run_status():
    """系统状态一览"""
    lines = []
    now = datetime.now()
    lines.append("📊 股票系统状态 " + now.strftime('%Y-%m-%d %H:%M'))
    lines.append("=" * 50)

    from kline_db import init_db, get_stats
    init_db()
    stats = get_stats()
    lines.append("")
    lines.append("💾 K线数据库:")
    lines.append("  股票数: " + str(stats.get('total_codes', '?')))
    lines.append("  总行数: " + str(stats.get('total_rows', '?')))

    pf = load_portfolio()
    total_mv = sum(p['market_value'] for p in pf['positions'])
    nav = pf['cash'] + total_mv
    nav_pct = (nav - INITIAL_CAPITAL) / INITIAL_CAPITAL * 100
    lines.append("")
    lines.append("💰 模拟盘:")
    lines.append("  总资产: ¥{:,.0f} ({:+.1f}%)".format(nav, nav_pct))
    lines.append("  现金: ¥{:,.0f}".format(pf['cash']))
    lines.append("  持仓: {}/{}只".format(len(pf['positions']), MAX_POSITIONS))
    for p in pf['positions']:
        lines.append("    {}({}) {}股 {:+.1f}% 持{}天".format(
            p['name'], p['code'], p['shares'], p['pnl_pct'], p['hold_days']))
    if pf.get('cooldown_until'):
        lines.append("  冷却期: 至" + pf['cooldown_until'])
    if pf['trades']:
        wins = len([t for t in pf['trades'] if t['pnl_pct'] > 0])
        lines.append("  历史交易: {}笔 胜率{:.0f}%".format(
            len(pf['trades']), wins/len(pf['trades'])*100))
    nav_history = load_nav()
    if nav_history:
        lines.append("  净值: {}天 最新{:+.1f}%".format(
            len(nav_history), nav_history[-1]['nav_pct']))

    cache_file = os.path.expanduser('~/.hermes/cache/screening_latest.json')
    if os.path.exists(cache_file):
        try:
            with open(cache_file) as f:
                sc = json.load(f)
            passed = sc.get('passed', {})
            if passed:
                lines.append("")
                lines.append("📌 选股({}): {}只候选".format(sc.get('date',''), len(passed)))
                for code, info in list(passed.items())[:3]:
                    s = info.get('score', {})
                    c = info.get('chanlun', {})
                    lines.append("  {}({}) {}分 {}级".format(
                        s.get('name',code), code, s.get('total',0), c.get('quality','?')))
            else:
                lines.append("")
                lines.append("📌 选股({}): 无候选".format(sc.get('date','')))
        except:
            pass

    # 交易日志
    from stock_log import get_recent_trades
    trades = get_recent_trades(10)
    if trades:
        lines.append("")
        lines.append("📝 最近交易:")
        for t in trades:
            ts, code, name, action, price, shares, pnl, reason, hold = t
            act = '📥买' if action == 'buy' else '📤卖'
            detail = f"¥{price:.2f}" if price else ""
            if action == 'sell' and pnl is not None:
                detail += f" {pnl:+.1f}% [{reason}] 持{hold}天"
            lines.append(f"  {ts} {act} {name}({code}) {detail}")

    # 错误日志
    log_file = os.path.join(LOG_DIR, 'stock_system.log')
    if os.path.exists(log_file):
        size = os.path.getsize(log_file)
        lines.append("")
        lines.append("📝 日志: {:.1f}KB".format(size/1024))
        try:
            with open(log_file, encoding='utf-8') as f:
                all_lines = f.readlines()
            errors = [l.strip() for l in all_lines if 'ERROR' in l][-5:]
            if errors:
                lines.append("  最近错误:")
                for e in errors:
                    lines.append("    " + e[:80])
        except:
            pass
    return lines

def run_backup():
    """备份关键文件"""
    backup_dir = os.path.expanduser('~/.hermes/cache/backups/' + datetime.now().strftime('%Y%m%d'))
    os.makedirs(backup_dir, exist_ok=True)
    files = {
        'sim_portfolio.json': os.path.expanduser('~/.hermes/cache/sim_portfolio.json'),
        'sim_nav.json': os.path.expanduser('~/.hermes/cache/sim_nav.json'),
        'screening_latest.json': os.path.expanduser('~/.hermes/cache/screening_latest.json'),
        'stock_config.json': os.path.expanduser('~/.hermes/scripts/stock_config.json'),
    }
    copied = []
    for name, src in files.items():
        if os.path.exists(src):
            shutil.copy2(src, os.path.join(backup_dir, name))
            copied.append(name)
    db_src = os.path.expanduser('~/.hermes/cache/kline.db')
    if os.path.exists(db_src):
        shutil.copy2(db_src, os.path.join(backup_dir, 'kline.db'))
        copied.append('kline.db')
    return ["✅ 备份完成: " + backup_dir, "  文件: " + ', '.join(copied)]

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='统一任务：模拟盘+股票池')
    group = parser.add_mutually_exclusive_group()
    group.add_argument('--morning', action='store_true', help='09:30 早盘买入+扫描')
    group.add_argument('--midday', action='store_true', help='14:50 盘中检查+扫描')
    group.add_argument('--summary', action='store_true', help='15:30 日报总结（只读）')
    group.add_argument('--status', action='store_true', help='系统状态一览')
    group.add_argument('--backup', action='store_true', help='备份关键文件')
    group.add_argument('--trades', action='store_true', help='查看最近交易记录')
    args = parser.parse_args()

    try:
        if args.morning:
            print(run_morning())
        elif args.midday:
            print(run_midday())
        elif args.summary:
            print(run_summary())
        elif args.status:
            print('\n'.join(run_status()))
        elif args.backup:
            print('\n'.join(run_backup()))
        elif args.trades:
            from stock_log import get_recent_trades
            trades = get_recent_trades(20)
            if not trades:
                print("暂无交易记录")
            else:
                print(f"最近{len(trades)}笔交易:")
                print("-" * 60)
                for t in trades:
                    ts, code, name, action, price, shares, pnl, reason, hold = t
                    act = '📥买入' if action == 'buy' else '📤卖出'
                    detail = f"¥{price:.2f}" if price else ""
                    if action == 'sell' and pnl is not None:
                        detail += f" {pnl:+.1f}% [{reason}] 持{hold}天"
                    print(f"  {ts} {act} {name}({code}) {detail}")
        else:
            print(run_morning())
    except Exception as e:
        error_msg = f'❌ 任务异常: {e}'
        log.error(error_msg)
        # 输出到stdout，cron会推微信
        print(error_msg)
