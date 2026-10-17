# 600519 / 510300 验证快照（A13 / B6 / B1）

> 本文件由 `tools/validation_snapshot.py` 自动生成，**全部数字来自真实运行**，未手工填写。
> 生成时间：2026-10-18 01:13:59　Python 3.12.10　数据区间见各表 `bars` 字段（600519 自 2020-01-02 起）

## 1. 600519（股票，`--json --fast`）

| 指标 | 实测值 |
|---|---|
| status | ok |
| action | hold |
| confidence | 0.539 |
| position_fraction | 0.129 |
| stop_loss | 1273.42 |
| take_profit | 1417.92 |
| stop_method | atr |
| entry_zone | [1317.88, 1340.12] |
| horizon_days | [9, 29] |
| distance_to_trigger | {"buy_pct": -5.3, "sell_pct": -2.87} |
| invalidation | 收盘跌破 1273.42，或 StochRSI 族共识转为看空，或 29 个交易日内未触发入场 |
| triggers | 1 |
| family | StochRSI |
| agreement | 0.89 |
| n_trials | 581 |
| n_survivors | 522 |
| dsr | 0.033 |
| significant | True |
| excess_point | -0.00011214 |
| excess_ci | [-0.000753971, 0.000700643] |
| excess_p_positive | 0.415 |
| regime | Bull |
| regime_win_rates | {"Bull": 0.5, "Bear": 0.5625, "Shock": 0.5143} |
| bars | 1643 |
| data_source | cache |
| from_cache | True |
| top_name | MACD(3-34-3) |
| top_score | 0.965486 |
| top_score_is | 0.453297 |
| top_score_oos | 1.185 |
| top_trades | 53 |
| n_candidates | 10 |
| family_counts | {"MACD": 3, "CCI": 3, "StochRSI": 4} |
| ci_metrics | [annual_return, avg_holding_days, calmar, max_drawdown, profit_factor, sharpe, win_rate] |
| time_in_market | 0.393183 |
| zero_trade_configs | 19 |
| warnings | 0 |

退出码：**0**（status=ok → 0）

## 2. 510300（基金，`--json --fast`）

| 指标 | 实测值 |
|---|---|
| status | ok |
| action | hold |
| confidence | 0.335 |
| position_fraction | 0.081 |
| stop_loss | 4.06 |
| take_profit | 4.52 |
| stop_method | atr |
| entry_zone | [4.34, 4.27] |
| horizon_days | [15, 46] |
| distance_to_trigger | {"buy_pct": 2.57, "sell_pct": 20.18} |
| invalidation | 收盘跌破 4.06，或 RSI 族共识转为看空，或 46 个交易日内未触发入场 |
| triggers | 1 |
| family | RSI |
| agreement | 0.868 |
| n_trials | 44 |
| n_survivors | 41 |
| dsr | 0.003 |
| significant | True |
| excess_point | -0.000181567 |
| excess_ci | [-0.000591813, 0.00025885] |
| excess_p_positive | 0.2125 |
| regime | Bear |
| regime_win_rates | {"Bull": 0.0, "Bear": 0.381, "Shock": 0.3913} |
| bars | 1643 |
| data_source | cache |
| from_cache | True |
| top_name | RSI(25-80) |
| top_score | 0.34126 |
| top_score_is | 0.148322 |
| top_score_oos | 0.4239 |
| top_trades | 44 |
| n_candidates | 10 |
| family_counts | {"RSI": 5, "波动率目标": 2, "MACD": 2, "双均线": 1} |
| ci_metrics | [annual_return, avg_holding_days, calmar, max_drawdown, profit_factor, sharpe, win_rate] |
| time_in_market | 0.530128 |
| zero_trade_configs | 3 |
| warnings | 0 |

退出码：**0**

## 3. B1 评分结构（before vs after）

| 项 | before（契约基线快照） | after（本次实测） |
|---|---|---|
| 胜率项对 \|总分\| 的贡献占比 | 72.8% | 19.5% |
| 评分主项 | `Wilson×0.5 + 年化×0.2 − 回撤×0.3` | `0.6×Calmar + 0.2×Wilson + 0.2×stability` |
| 年化是否真做 | 否（`252/252` 恒为 1） | 是（`(1+r)^(252/n_bars)−1`） |

## 4. B6 三窗口稳定性（验证二，真实数据）

| 窗口 | action | consensus.family | status |
|---|---|---|---|
| full | hold | StochRSI | ok |
| drop60 | hold | StochRSI | ok |
| drop120 | hold | StochRSI | ok |

结论：action 一致 = **True**，family 一致 = **True**

## 5. 本轮修复实测对照（F01–F21）

| 条目 | 修复前 | 修复后（本次实测） |
|---|---|---|
| F01 基金网格 | 510300 候选全部来自股票 581 组网格 | 候选族 {'RSI': 5, '波动率目标': 2, 'MACD': 2, '双均线': 1}（全部来自基金 44 组网格） |
| F03 n_trials | 10（= 候选数） | 股票 581 / 基金 44 |
| F04 significant | 只看 DSR>0 | significant=True，超额 CI 上界=0.000700643 |
| F06 score_oos | `score×0.9` 伪造 | score_is=0.453297 / score_oos=1.185（真实 OOS 列） |
| F06 zero_trade_configs | 硬编码 0 | 19 |
| F10 metrics CI | 0/9 有 ci_95 | 7/9 有 ci_95 |
| F11 time_in_market | 恒 0 | 0.393183 |
| F12 止损口径 | `last_close×0.95`（与回测 8% 矛盾） | 1273.42（method=atr，与回测 ATR 口径一致） |
| F13 族占比 | candidates 6/10 同族 | {'MACD': 3, 'CCI': 3, 'StochRSI': 4} |
| F14 族名来源 | `idx.split('(')[0]` | 注册表 prefix（family_map） |
| F05 退出码 | 非 ok 一律 10 | 600519=0 / 510300=0 / 999999=30 |

## 6. 复现命令

```bash
cd source
python cli.py 600519 --json --fast      # 股票
python cli.py 510300 --json --fast      # 基金
python -m pytest tests/ -q              # 测试网（离线）
cd .. && python tools/check_gates.py     # 32 项门禁（ps1 版的等价转写）
```
