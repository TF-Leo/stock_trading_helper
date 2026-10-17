# Validation Report: 600519 贵州茅台

## 运行信息
- **日期**: 2024-XX-XX
- **数据区间**: 2020-01-02 ~ 2026-10-16
- **K 线数**: 1643
- **Python 版本**: 3.12.10
- **依赖版本**: akshare 1.18.29, numpy 2.2.6, pandas 3.0.1

## 基线快照 (Before)

| 指标 | Before | After | Final |
|------|--------|-------|-------|
| 可负担占比 | 4% (66/1643) | 100% | 待验证 |
| 唐奇安 (20/10) 交易数 | 29 | 待验证 | 待验证 |
| MACD(12-26-9) 交易数 | 32 | 待验证 | 待验证 |
| 均线趋势 (5-20-60) 交易数 | 42 | 待验证 | 待验证 |

## Phase A 修复内容

### A1/A12: 资金/整手解耦
- OK analyzer.py 改用 adaptive_oos_config
- OK optimizer.py bayes_search 基准一致

### A3: 失败变响
- OK BacktestResult 新增 unaffordable_lot_events
- OK BacktestResult 新增 blocked_by_limit_events

### A4: 持仓时长
- OK BacktestResult 新增 time_in_market

### A9: 对比表卫生
- OK 移除 __买入持有基线__ 注入

### A10: 涨跌停按板块
- OK 创建 decision/instrument.py
- OK engine.py 使用动态阈值

### A11: 止损跳空
- OK 止损成交价 = min(止损价，开盘价)
- OK 止盈成交价 = max(止盈价，开盘价)

## 下一步
- 完成 A5/A6/A7 基金修复
- 完成 A8 regime 统一
- 完成 A14 测试修复
- 运行门禁验收测试
