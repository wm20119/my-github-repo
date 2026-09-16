# 📈 A股缠论选股系统 v5.1

基于缠论（Chan Theory）的自动化选股、回测、模拟盘交易系统。

## 架构

```
chanlun_strategy.py    ← 唯一逻辑源（入场8条件/出场4条件/冷却5天/回测/RSI/precompute）
├── stock_screening.py     ← 全A股选股扫描（~5500只）
├── sim_portfolio.py       ← 模拟盘交易（100万×10只×10%）
├── daily_chanlun_scan.py  ← 盘中实时扫描
├── combined_system_v5_1.py ← 独立回测验证
├── stock_scorer.py        ← 多因子评分系统（满分100）
├── chanlun_engine.py       ← 缠论底层引擎
└── stock_config.json      ← 统一配置（股票池/参数）
```

## 核心参数

| 参数 | 值 |
|------|-----|
| WINDOW | 180（K线窗口） |
| TP | +8%（止盈） |
| SL | -5%（止损） |
| HOLD | 60天（最大持仓） |
| CD | 5天（止损冷却） |

## 入场条件（8项）

1. 缠论三买信号
2. stock-scorer评分 > 0
3. MACD diff > 0
4. 价格 > MA20
5. 存在中枢
6. 价格 > 中枢ZG
7. 价格 ≥ 近10天最高
8. MACD柱3天上升

## 出场条件（4项）

1. 止损 -5%
2. 止盈 +8%
3. 超时 60天
4. 卖出信号 + 破位MA20×0.98

## 股票池

600030 中信 | 688019 安集 | 688008 澜起 | 600584 长电 | 300750 宁德 | 000977 浪潮 | 002156 通富

## 运行

```bash
# 选股扫描
python3 stock_screening.py

# 模拟盘日报
python3 sim_portfolio.py

# 盘中扫描
python3 daily_chanlun_scan.py

# 回测验证
python3 combined_system_v5_1.py
```

## 依赖

- Python 3.12+
- 无需第三方包（纯标准库）
- K线数据：Sina API + 腾讯API

---

*v5.1 | 2026-09-16*
