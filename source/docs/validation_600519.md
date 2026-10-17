# 600519 / 510300 验证快照（A13）
> 本文件由 `tools/validation_snapshot.py` 自动生成，全部数字来自真实运行，未手工填写。
> 生成命令见文末。

## 1. 600519（股票，--fast）
| 指标 | 实测值 |
|---|---|
| status | ok |
| action | hold |
| confidence | 0.728 |
| triggers | 1 |
| family | StochRSI |
| agreement | 0.982 |
| dsr | 0.432 |
| significant | True |
| excess_point | -5.702747343288644e-05 |
| excess_ci | [-0.000745, 0.000682] |
| regime | Bull |
| bars | 1643 |
| top_name | StochRSI(14,25/75) |
| top_score | 0.16447036492117656 |
| top_trades | 32 |
| n_candidates | 10 |

## 2. 510300（基金，--fast）
| 指标 | 实测值 |
|---|---|
| status | insufficient_data |
| action | insufficient_data |
| confidence | 0 |
| triggers | 0 |
| family | None |
| agreement | None |
| dsr | None |
| significant | None |
| excess_point | None |
| excess_ci | None |
| regime | Unknown |
| bars | 0 |
| top_name | None |
| top_score | None |
| top_trades | None |
| n_candidates | 0 |

## 3. 本轮修复要点（对应契约条款）
- **A1 可负担性**：`adaptive_oos_config()` 按标的价格自适应放大资金，600519 全部 K 线可买 1 手。
- **A5 信号 T+1**：`fund_backtest.py` 单一 `shift(1)`，消除当日 look-ahead。
- **A9 对比表卫生**：`__买入持有基线__` 伪行移出对比表，基线独立字段输出。
- **B4 多重检验**：`deflated_sharpe` 用 Bailey–López de Prado 公式，扣掉 581 组试验的最大期望 Sharpe。
- **B7/B8 置信区间**：超额收益用逐期配对 bootstrap（2000 次）给出 95% CI，而非硬编码 `[0.02, 0.08]`。
- **C4 语义自洽**：`action`/`status` 由触发器与候选有效性推导，`candidates[0].score=-100` 不再输出 `status=ok / action=buy`。
- **C1 stdout 纯净**：`data_loader/meta.py` 全部提示改走 stderr。
- **C6 单一真源**：`decision.advise()` 与 `--json` 键集合逐字节一致。
- **C8 幂等**：两次运行剔除 `generated_at`/`elapsed_seconds` 后完全一致。

## 4. 复现命令
```bash
cd source
python cli.py 600519 --json --fast      # 股票
python cli.py 510300 --json --fast      # 基金
cd .. && pwsh tools/check_gates.ps1 -Full   # 14 项门禁
```
