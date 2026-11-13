# R10 无偏池上的策略候选复验（R6 复验）

> **issue #86**。性质：**研究任务**。本 PR 只新增 `tools/strategy_lab_unbiased.py`
> 与本文件；**未改** `source/` 生产逻辑、`actions.py`、`requirements.txt`、`README.md` 契约。
>
> 前置：
> - `docs/STRATEGY_REDESIGN_V1.md`（R6 / #70）—— 在**沪深 300 当前成分**上跑
>   13 个预注册候选，无一达标；该池有 **14.4pp/年**幸存者偏差。
> - `docs/UNIVERSE_BUILD_LOG.md`（R9 / #83）—— 无偏日线池（全 A + 退市，5,471 票）。
>
> **状态：数据集重建中，本文件尚无绩效数字。**
> 按协调者要求，数据 md5 校验完成前不产出任何绩效结论。
> 下方各表的数字栏在分析跑完后一次性填入，`TODO(数据)` 表示待填。

---

## 0. 结论先行

> TODO(数据)：分析完成后填写。必须明确回答 issue #86 的两个问题：
> 1. R6 的结论有多少来自幸存者偏差？
> 2. 是否有候选能在无偏池上通过 R4 §3.6 的 X/Y/Z 门槛？
>
> 若全部候选仍不达标，必须原句写出「**选股层到此为止**」。

---

## 1. 问题与方法冻结

### 1.1 为什么要复验

R6 的票池是 **2026-11 的沪深 300 当前成分**（300 只）。该池的等权买入持有年化
14.26%，而真实沪深 300 价格指数同期 −0.13% —— 相差 **14.4pp/年**。
这意味着 R6 里所有绝对收益数字都**不可当作可投资业绩**；更重要的是，
**相对比较也可能被污染**：如果候选策略系统性地偏好「活下来并做大」的票，
它的超额就会部分来自幸存者偏差，而不是选股能力。

R9 已建成无偏池（5,471 票 = 在市 5,224 + 退市 247，2018-01-02~2026-11-12，
9.69M 行，0 失败），本轮在同一套预注册候选上重跑。

### 1.2 候选与方法**完全不改**

| 项 | 值 | 来源 |
|---|---|---|
| 候选 | R6 的 13 个「持有期 × 策略族」 | `tools/strategy_lab.py::CANDIDATES` |
| 组合 | long-only、等权 top-30、按 H 非重叠再平衡 | R6 §2.2 |
| 成本 | 买入 13bp / 卖出 23bp + 10bp 滑点（含印花税） | `BacktestConfig` |
| 成交 | T+1；d 收盘算分 → d+1 开盘买；涨停不买、跌停不卖 | R6 §4.1 |
| 随机对照 | 等暴露度 `iid` 与 `turnover` 两种，各 ≥200 次 | P1 计划 §3.6.4 |
| 统计 | 扣费年化 / maxDD / Sharpe / 换手 / DSR / Bonferroni；OOS 最后 30% | R6 §4.4 |

**代码层面的防漂移**：`strategy_lab_unbiased.py::_assert_r6_parity()` 在每次
`main()` 入口断言候选表、组合参数（`TOP_N` / `DONCHIAN_WINDOW` /
`LOWVOL_WINDOW` / `RSI_PERIOD` / `BOLL_WINDOW` / `BOLL_STD`）与成本口径
（`BUY_COST` / `SELL_COST`）与 R6 **逐项相同**；不一致直接抛错，不静默继续。

> 本轮**没有**因结果增删候选或调参。若未来要改候选表，必须在新报告里登记新的
> `n_trials` 并说明旧候选为何作废。

---

## 2. 数据

### 2.1 数据集丢失与重建（必须如实记录）

任务书指定的数据集路径是
`~/orca/workspaces/stock_trading_helper/universe-build/evidence/universe/`（866MB）。
执行时该路径**已不存在**：整个 `universe-build` worktree 已从 `git worktree list`
消失，`find / -name 'sh.600000.csv'` 无结果，全盘无任何目录含 >100 个 CSV，
无 >400MB 归档。

**唯一幸存物**是 R9 验收会话留下的元数据副本：

| 文件 | 内容 | 位置 |
|---|---|---|
| `manifest.json` | 5,471 条，含 `csv_md5` / `rows` / `first` / `last` / `ipo_date` / `out_date` / `delisted` | `~/.hermes/cache/scratch/v85/val/` |
| `universe.json` | 主表快照与 5,471 只的 `code/ipo/out/name` | 同上 |
| `validate-report.json` | R9 的离线校验结果 | 同上 |

**重建可行性**：最初测 `baostock.com:10030` 不通，一度判断无法重建。实测
baostock 数据 API 的真实域名是 **`public-api.baostock.com:10030`**
（`baostock/common/contants.py::BAOSTOCK_SERVER_IP`；`baostock.com` 只是门户站点，
10030 端口封禁）。该域名可达，单票 2018-01-01~2026-11-12 取 2,149 行约 4.9s。

**落盘位置改为持久路径** `~/data/stock-universe/`（worktree 会被清理，不再把
866MB 数据集放进 worktree）；worktree 内 `evidence/universe` 只是指向它的软链。

### 2.2 一次请求落两份（以及为什么它不改变数据）

R9 的取数字段是 `date,code,open,high,low,close,volume,amount,tradestatus,pctChg,isST`，
**不含估值**；而候选 `value_trend_h20` 需要 `pbMRQ` 的历史分位。

若先建 R9 池再单独补估值，等于把 6.8 小时的串行取数做两遍。因此 `fetch` 用
**超集字段**（`BASE_FIELDS + turn,peTTM,pbMRQ,psTTM`）取一次，然后：

- 投影回 `BASE_FIELDS` → `csv/<code>.csv`（应与 R9 **逐字节相同**）；
- 全字段 → `val/<code>.csv`（供 pb 分位使用）。

**已验证投影不改变任何数值**：

```text
$ python -c "...超集取一次→投影回 BASE_FIELDS→算 md5..."
md5 superset-derived 2688e63092947b61bdfce91b27a88b28
md5 reference         2688e63092947b61bdfce91b27a88b28
IDENTICAL True
```

### 2.3 md5 逐票校验（数据可信度的唯一证据）

> TODO(数据)：`verify` 跑完后填入。必须给出「一致票数 / 不一致票数」，
> 以及不一致票的处理（逐只重抓后的最终数字）。

```text
$ .venv/bin/python tools/strategy_lab_unbiased.py verify \
    --data-dir ~/data/stock-universe \
    --reference ~/data/stock-universe/reference-manifest-v85.json
TODO(数据)
```

### 2.4 无偏池规模与结构

> TODO(数据)：动态池日均/最小/最大票数、排除的 24 只 merger、退市结算票数。

| 项 | 值 |
|---|---|
| 全池 | TODO(数据) |
| 排除 `exit_reason=merger` | 24 只（预注册规则，见 §2.5） |
| 实际参与回测 | TODO(数据) |
| 其中退市票（`price` 类） | TODO(数据) |
| 动态池规模（日均 / min / max） | TODO(数据) |
| 交易日 | 2,149（2018-01-02 ~ 2026-11-12） |

### 2.5 无偏池三条使用规则（issue #86）

| # | 规则 | 实现 |
|---:|---|---|
| 1 | 退市票用 `last_tradestatus1_close` 结算；`merger` 的 24 只先排除 | `apply_delisting_settlement()`：退出日起 `open`/`close` 一并**冻结**在退出价，之后禁止买入；`load_unbiased()` 在加载阶段剔除 `exit_reason=merger` |
| 2 | 按日动态池：某日只含当日已上市且未退市的票 | `mat["listing"]`：`ipo_date <= d <= out_date`，并入 `feats["elig"]` 的每一族与随机对照池 |
| 3 | 停牌日不可成交 | `tradestatus > 0.5` 且 `open` 有效才可买/卖；跌停/停牌卖出顺延（上限 20 日） |

**为什么「冻结价格」是必要的（实测 bug）**：最初只把 `close` 冻结、`open` 留空。
当一笔持仓跨越了退市日（窗口内退市）时，`entry_basis` 取到 `NaN`，整段净值被污染。
合成数据实测产生 21 个 `NaN` 因子，等权基线的年化直接变成 `nan`。
修复后等权基线恢复正常（合成数据 6.24%）。

### 2.6 24 只 merger 排除的影响

> TODO(数据)：列出 24 只代码/名称/退出日，并说明排除的方向性影响
> （这些票多为换股吸收合并，价格序列不反映真实退出价值；若按价格序列结算
> 会**低估**收益，排除它们是保守选择）。

---

## 3. 幸存者偏差量级（issue #86 问题 1）

### 3.1 三个口径的定义

| 口径 | 定义 | 用途 |
|---|---|---|
| **无偏池等权** | 全部 5,471 票（动态池），等权，H=20 再平衡 | 本轮基线 |
| **幸存者子集等权** | 只含「到窗口末仍未退市」的票，等权 | 隔离「退市票」这一项 |
| **当前成分等权** | R6 用的同一份沪深 300 快照（2026-11-09，300 只），等权 | 复现 R6 的 14.4pp 口径 |
| 沪深 300 指数 | `sh.000300` 价格指数 | 真实市场基准 |

### 3.2 结果

> TODO(数据)

| 口径 | 年化 | Sharpe | maxDD | 与无偏池等权之差 |
|---|---:|---:|---:|---:|
| 无偏池等权 | TODO | TODO | TODO | — |
| 幸存者子集等权（N=TODO） | TODO | TODO | TODO | TODO pp |
| 当前成分等权（300 只，2026-11-09） | TODO | TODO | TODO | TODO pp |
| 沪深 300 指数 | TODO | TODO | TODO | TODO pp |

**检验目标**：R6 报告「当前成分等权 14.26% vs 指数 −0.13%，差 14.4pp/年」。
本轮若给出接近 14pp 的「当前成分 − 无偏池」差，说明 R9 池确实消除了该量级的偏差。

---

## 4. 结果：13 个候选（issue #86 问题 2）

### 4.1 候选表现（扣费、组合层面）

> TODO(数据)：`<data-dir>/out/candidates.csv`

| 候选 | 族 | H | 年化 | Sharpe | maxDD | 换手/年 | 命中率 | K |
|---|---|---:|---:|---:|---:|---:|---:|---:|

### 4.2 基线

> TODO(数据)

### 4.3 等暴露度随机组

> TODO(数据)

### 4.4 推断：Bonferroni / DSR / OOS / 功效

> TODO(数据)

### 4.5 年度稳定性

> TODO(数据)

### 4.6 Walk-forward（3 年训练 → 1 年测试）

> TODO(数据)

### 4.7 与 R6 逐候选对比

> TODO(数据)：这是本报告的核心表之一。每个候选给出 R6 与 R10 的年化 / maxDD /
> Sharpe / 换手 / DSR，以及差值。必须解释差值的来源：
> ① 票池从 300 只当前成分 → 5,471 只无偏池；② 退市票结算；
> ③ 按日动态池；④ 估值列来自同一次取数（`value_trend_h20`）。

| 候选 | R6 年化 | R10 年化 | Δ | R6 maxDD | R10 maxDD | Δ | R6 Sharpe | R10 Sharpe | Δ | R6 DSR | R10 DSR |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|

### 4.8 预注册判定逐项

> TODO(数据)：9 条判定（R6 §2.3）逐候选列出 ✓/✗。

---

## 5. X / Y / Z 判定（R4 §3.6）

### 5.1 门槛回顾

| 路径 | 门槛 | 附加条件 |
|---|---|---|
| **X** 买点分层 | Δ命中率 ≥ 3pp | 聚类稳健 p<0.05；OOS 同号；n_clusters ≥ 2,500 |
| **Y** 组合收益 | 扣费年化改善 ≥ +1.5pp | 同口径；**必须过等暴露度对照** |
| **Z** 组合回撤 | maxDD 改善 ≥ 3pp | 年化劣化 ≤ 0.5pp；**必须过等暴露度对照** |
| **C** 校准 | Brier 改善 ≥ 0.02 | 需前向台账 ≥200 条已到期记录 |

### 5.2 本轮的判定边界（必须先说清）

**X 路径本轮不可判定。** 本工具是**组合层**回测，输出的是组合净值；
X 要求的是「买点分层命中率（票 × 季度聚类）」，需要按 R3 口径重建买点并做
聚类检验。因此本报告对 X 一律写「未测」，**不写「未通过」** ——
两者含义不同，混写会误导。

**C 路径本轮不可判定。** 需要前向台账的真实记录，回测无法替代（R3 §8.2/§10.3）。

**Y / Z 可以判定**，因为它们的单位就是组合层的年化与 maxDD。判定用：

- Y：`Δ年化(vs 无偏等权) ≥ +1.5pp` 且 `Δ年化(vs 沪深300) ≥ +1.5pp`
  且 `p(随机 ≥ 策略) ≤ 0.05`（两种对照取更严格者）；
- Z：`ΔmaxDD(vs 无偏等权) ≥ 3pp` 且 `Δ年化 ≥ −0.5pp` 且 `p(随机 ≥ 策略) ≤ 0.05`。

> 代码：`_x_y_z_verdict()`。X 固定返回 `not_testable` 并附说明，避免把
> 「没测」误报成「没通过」。

### 5.3 逐候选判定

> TODO(数据)

| 候选 | X | Y (Δ年化 vs EW) | Y 判定 | Z (ΔmaxDD vs EW) | Z 判定 | 随机对照 p |
|---|---|---:|---:|---:|---:|---:|

---

## 6. 结论与建议

> TODO(数据)。必须回答：
> 1. 是否有候选值得**纸面前向**？若没有，原句写「**选股层到此为止**」。
> 2. 若有，给出与 R6 相同的试点规格（票池/信号/组合/成本/监控），并明确
>    证据状态（K、MDE、DSR、Bonferroni）。
> 3. 与 R6 的结论差异是否可归因于幸存者偏差。

---

## 7. 局限（如实列出）

| 风险 | 说明 |
|---|---|
| **24 只 merger 排除** | 换股/吸收合并的价格序列不反映真实退出价值；排除是保守处理，但会让池子少 24 只（占 0.44%），且这些票多为大盘国企，可能轻微低估「合并收益」 |
| **qfq 快照** | 前复权是 2026-11-14 的快照；后续除权会使整段前复权价格整体平移。**不影响**同一时点内的相对比较与策略排序，但绝对价格会变 |
| **不含北交所/B股** | 池子是 `type=1`（5,566 只）中的 5,471 只；北交所约 355 只与 B 股不在内。**不能**把这 5,471 只称作「全市场」 |
| **X/C 路径未测** | 见 §5.2。本报告的 X/C 一律是「未测」 |
| **样本长度** | H=120 只有 17 个独立窗；MDE 仍会 >10pp。不能据此判定「有/无 edge」 |
| **单一价格源** | 全部 baostock qfq；未做跨源对照 |
| **基准口径** | 沪深 300 为价格指数（不含分红）；等权为 qfq 总收益 |
| **成本假设** | 印花税 10bp（实际 5bp，偏保守）；滑点固定 10bp，未建模冲击 |
| **ST 历史** | 依赖 baostock `isST`；早期缺失按非 ST 处理 |
| **退市冻结期** | 退出日之后按退出价冻结，不产生额外盈亏；实际冻结期内可能仍有流动性风险 |
| **数据集重建** | 本轮数据是**重建**的（原始 866MB 已丢失）。可信度完全依赖 §2.3 的 md5 逐票校验 |

---

## 8. 复现

```bash
export PATH="$HOME/.local/bin:$HOME/.npm-global/bin:$PATH"
uv venv --python 3.14 .venv
uv pip install --python .venv/bin/python \
    -r source/requirements.txt -r requirements-research.txt

# 0) 离线自检（必须全 PASS；含与 R6 候选表的一致性断言）
.venv/bin/python tools/strategy_lab_unbiased.py selftest

# 1) 重建无偏池（联网，约 6–7 小时；串行、可断点续跑）
.venv/bin/python -u tools/strategy_lab_unbiased.py fetch \
    --data-dir ~/data/stock-universe --watchdog-seconds 600

# 2) 离线校验（必须 md5_mismatch=0）
.venv/bin/python tools/strategy_lab_unbiased.py verify \
    --data-dir ~/data/stock-universe \
    --reference ~/data/stock-universe/reference-manifest-v85.json

# 3) 离线分析（一条命令复现）
.venv/bin/python tools/strategy_lab_unbiased.py analyze \
    --data-dir ~/data/stock-universe --random-reps 500
```

产物：`~/data/stock-universe/out/{summary.json,candidates.csv,yearly.csv}`；
校验报告：`~/data/stock-universe/verify-report.json`。

**生产测试**：`source/` 未改动。本轮在 Linux，**未运行** `tools/check_gates.ps1`
（本机无 `pwsh`）；不把「未运行」写成通过。

---

## 9. 引用索引

| 内容 | 位置 |
|---|---|
| 候选表 / 组合引擎 / DSR / 随机组（R6） | `tools/strategy_lab.py` |
| 本轮的取数 / 校验 / 分析 | `tools/strategy_lab_unbiased.py` |
| R9 建池工具（会话/超时/校验机制被复用） | `tools/build_universe.py` |
| 无偏池结构与退市规则 | `docs/UNIVERSE_BUILD_LOG.md` |
| 幸存者偏差可行性 | `docs/UNIVERSE_FEASIBILITY.md` |
| X/Y/Z 门槛与等暴露度对照 | `docs/P1_EVIDENCE_PLAN.md` §3.6 |
| 生产成本口径 | `source/stock_analysis/backtest/engine.py::BacktestConfig` |
| 涨跌停阈值 | `source/decision/instrument.py::get_price_limits` |
