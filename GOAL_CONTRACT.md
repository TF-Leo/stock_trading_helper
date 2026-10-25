# Goal Contract v2.0 — 信号语义与可执行决策契约

> 版本：2.0（2026-10-25 重建）
> 上一版：v1.0（Phase A–D，已全部交付；全文见 `git show d4264a4:GOAL_CONTRACT.md`）
> 配套：执行清单 `GOAL_FIXLIST.md`（F22–F34）｜唯一台账 `PROGRESS.md`
> **完成定义不变**：`pwsh -File tools/check_gates.ps1 -Full` 退出码 = 0。

---

## 0. 本轮目标（一句话）

**让"模型算出来的东西"和"Hermes 读到的字面意思"是同一件事，并让回测评价的就是 Hermes 实际收到的那条规则。**

v1.0 解决的是"数字可不可信"——数据泄漏、资金基准、多重检验、错误路径同构。它做到了（47 项测试、39 项门禁、下游 Agent 真机复现全过）。

但它留下了一个更隐蔽的问题：**数字可信 ≠ 语义正确**。当前输出里同时存在"动作是 `hold`、理由写着卖出信号"、"卖点发生在两天前、文案却配今天的价格"、"策略大概率跑输基准、却报 `significant=true`"。下游 Agent 只能读文字，于是把文字当成了结论。

本轮不新增策略、不调参数、不改变量方向，只做三件事：

1. **Phase E 语义**：动作、状态、文案、数字四者由同一份事实生成，且带日期。
2. **Phase F 评价**：回测必须评价线上真正执行的那条规则，并给出信号级收益统计。
3. **Phase G 验证**：修掉已失效的门禁打桩，让"绿灯"重新等于"真的验过"。

---

## 1. 与 v1.0 的关系（**不要重开已完成项**）

| v1.0 阶段 | 状态 | 证据位置 |
|---|---|---|
| A 正确性（A1–A14） | 已交付 | `PROGRESS.md` §0.2 档案表 → §1.5 |
| B 可靠性（B1–B9） | 已交付 | 同上（G17/G18/G23/G26/G27/G28） |
| C 可消费性（C1–C8） | 已交付 | 同上（G02/G07/G35） |
| D 收敛与工程（D1–D6） | 已交付 | 同上（G10/G11/G29/G30） |

**以下 v1.0 条款继续有效，不得违反**：输出契约的顶层结构与字段名、五个退出码、五档 `status`、`position_fraction ≤ 0.30` 且只输出比例、错误路径与成功路径键集合同构、幂等确定性、门禁只加强不放宽、离线测试全绿、`cli.py ≤ 700` 行。

**v1.0 中被本轮显式替换的条款只有两条**，替换理由见 §3、§8：

- v1.0 B6 验证一"三窗口 `recommendation.action` 必须完全一致" → 由 §8 的新不变式替换（原判据正是 15 根过滤器的成因）。
- v1.0 输出规格中 `trigger.current` 的语义（未写明是价格还是指标值）→ 由 §6.1 明确为**指标值**，价格另设 `last_close`。

---

## 2. 事故陈述（已复现，非推测）

标的 002415 海康威视，本地前复权缓存 3928 根。按 `--as-of` 语义截断数据后重跑，实测：

| 日期 | 收盘 | 策略原始信号 | 输出 `action` | 输出 `action_reason` | `trigger.state` |
|---|---|---|---|---|---|
| 2026-10-20 | 32.82 | **MA3/MA30 死叉（sell=True）** | — | — | — |
| 2026-10-22 | 32.42 | `sell=False` | `hold` | `均线趋势(3-30-60): 卖出信号 (32.42)` | `near_sell` |
| 2026-10-23 | **33.78（+4.19%）** | `sell=False` | `hold` | `均线趋势(3-30-60): 卖出信号 (33.78)` | `near_sell` |

三处直接矛盾：

1. **动作与文案相反**：`action=hold`，文案写"卖出信号"。
2. **文案里的数字是收盘价，不是指标值**：10/22 是 32.42、10/23 是 33.78，与 `data.last_close` 逐日相等；两条 `trigger.current` 也等于当日收盘价。**文案中没有任何一个数字来自指标。**
3. **大涨当天仍在报卖出**：10/23 涨 4.19% 后文案不变，只把价格换成了 33.78。用户观察到的"风险提示后次日大涨"，在系统里不是一次性误报，而是**持续误报**。

同时，当日统计标签为：`significant=true`、`deflated_sharpe=0.051`、超额收益 `point=-6.24e-4/日`、`ci_95=[-1.29e-3, +2.42e-5]`、`p_positive=0.035`。

即：**96.5% 的重抽样样本认为该策略跑输买入持有，系统仍标记 `significant=true`。**

### 代码事实（行号为 HEAD `d4264a4`）

| 位置 | 事实 |
|---|---|
| `source/cli.py:389` | `trigger_msg = format_trigger_message(triggers, 'hold')` —— 在 `action` 决定（441–446）**之前**生成，且 `'hold'` 是硬编码 |
| `source/decision/triggers.py:115` | `format_trigger_message(triggers, action)` 的 `action` 形参在函数体内**从未被使用**（死参数） |
| `source/decision/triggers.py:122-123` | `sell_triggered` 与 `near_sell` 合并渲染为"卖出信号"，不携带日期与年龄 |
| `source/decision/triggers.py:24,61` | `current` 直接取 `last_close` → 每条触发器的"当前值"都是收盘价 |
| `source/decision/triggers.py:38-41` | `near_sell` 的定义是"近 10 根内出现过卖点"，与"接近卖出阈值"无关 → 一次死叉在此后 10 个交易日内持续被描述为卖出 |
| `source/cli.py:419-423` | 要求"最近 15 根**全部**为真"才算强触发；均线死叉是**单根事件**，永远不可能满足 → 事件型策略的 `action` 被机制性钉死为 `hold` |
| `source/cli.py:418` | `significant = dsr>0 and ci_hi>0` —— 判据是"不能排除正收益"，名字却是"显著" |
| `source/stock_analysis/backtest/engine.py:155-156,259,277` | 回测执行的是**原始单根信号**，且成交价为 **T+1 收盘** |
| `tools/probe.py:1016-1030` | G06 三窗口打桩 `cli.get_stock_daily`，而实际取数走 `meta.resolve_and_fetch`（`cli.py:241-242`）→ 打桩不生效 |

---

## 3. 根因（R1–R5）

- **R1 语义层**：动作与文案由两条互不知情的路径生成。文案先算、动作后算，文案还不知道动作。这不是文案疏忽，是结构缺陷——**任何"文案与动作一致"的修复，只要还在两个地方各写一遍，就一定会再漂移**。
- **R2 时间层**：`near_*` 把"历史上发生过"当成"现在正在发生"，且不带日期。信号因此可以无限期地以"当前"的语气出现在大涨日。
- **R3 评价层**：三套口径并存——回测跑原始单根信号、线上动作叠了 15 根过滤、Hermes 读到的是近 10 日文案。**回测从未评价过 Hermes 实际收到的规则**，所以回测数字再好也不能证明这条提示有效。
- **R4 统计层**：`significant` 的判据（CI 上界 > 0）与名字（显著）不匹配，导致"大概率跑输"被判为"显著"。
- **R5 验证层**：G06 的打桩点在取数重构后失效，三个窗口跑的是同一份全量数据。**绿灯不再等于验过**，这也是 v1.0 末尾几轮"预测全绿、实跑见红"的同源问题。

---

## 4. 执行硬约束

1. **门禁只允许加强**。禁止放宽阈值、删除断言、`pytest.mark.skip`、改现有断言来变绿。
2. **每修一条，必须同时把它变成门禁项**（写入 `tools/probe.py` 并挂进 `check_gates.ps1` 映射表），否则下轮必然复现。
3. **打桩必须自检**：任何 monkeypatch 之后，必须断言"被替换的入口确实被调用到、且返回值真的进入了被测代码"（R5 的直接教训）。只替换不校验的桩一律视为无效。
4. **禁止对 >300 行的文件整体重写**。`source/cli.py` 现 674 行（上限 700），本轮新增逻辑必须落到 `source/decision/` 下，**不得把 cli.py 撑破**。
5. **禁止用"连续 N 根同向"这类过滤器去换取动作稳定**。那正是 R1/R2 的成因；稳定性要靠 §8 重新定义的不变式达成。
6. **禁止反转买卖方向**。"卖点之后大涨"不足以证明应当反向操作；先建立度量（Phase F），再谈参数。任何方向性改动必须单独提案。
7. 一条一提交，提交信息带条目号（如 `fix(F24): 拆分 near_* 的历史与接近语义`）。
8. Python 一律在 `source/` 下运行；长任务给显式 timeout；中文输出前设 `PYTHONIOENCODING=utf-8`。

---

## 5. 模块布局（增量，不要另创结构）

```
source/decision/signals.py    # 新增：信号状态机（position_state / event / 历史 / 年龄 / 指标当前值）
source/decision/actions.py    # 新增：决策表（§7 的唯一实现），纯函数，无 IO
source/decision/render.py     # 新增：action_reason 唯一出口（§6.2 的规则实现）
source/decision/evaluate.py   # 新增：signal_stats（§6.4）与 decision replay（F30）
```

`decision/triggers.py` 降级为"指标级触发器求值"，**不再负责文案、不再负责动作**。
`cli.py` 只做装配：取数 → 选策略 → `signals.build()` → `actions.decide()` → `render.reason()` → 填 schema。

---

## 6. 输出契约增量（字段名逐字一致）

### 6.1 `recommendation.signal`（新增，必填）

```json
{
  "strategy": "均线趋势(3-30-60)",
  "position_state": "holding | flat",
  "state": "buy_triggered | near_buy | neutral | near_sell | sell_triggered",
  "as_of": "2026-10-22",
  "last_buy_date": "2026-10-15 | null",
  "last_buy_age": 5,
  "last_sell_date": "2026-10-20 | null",
  "last_sell_age": 2,
  "current_value": 0.9795,
  "last_close": 32.42,
  "threshold": {"buy": 1.0, "sell": 1.0}
}
```

**`state` 的语义被重新定义（这是本轮最核心的改动）**：

| 取值 | 含义（唯一解释） |
|---|---|
| `buy_triggered` | **本根**产生买入事件：`buy[t]=True` 且 `position_state[t-1]=flat` |
| `sell_triggered` | **本根**产生卖出事件：`sell[t]=True` 且 `position_state[t-1]=holding` |
| `near_buy` | 未触发，`position_state=flat`，且指标距买入阈值 ≤ 5%（按该指标量程归一） |
| `near_sell` | 未触发，`position_state=holding`，且指标距卖出阈值 ≤ 5% |
| `neutral` | 其余 |

**硬规则**：`position_state=flat` 时 `state` **不得**为 `near_sell` 或 `sell_triggered`。这条规则单独就能消灭本次事故——10/22 策略已是空仓，`near_sell` 不可能成立。

`position_state` 由策略自身信号回放得出（初值 `flat`；`buy` 置 1、`sell` 置 0；同根同时为真时以 `sell` 优先并记 `conflict`）。它表示**策略视角的隐含仓位，不是用户账户持仓**；`advise()` 仍不接收任何账户态入参。

### 6.2 `action_reason` 生成规则（强制，可机器校验）

`action_reason` 是 `list[str]`，1–3 条，**必须由 `actions.decide()` 的输出生成**，禁止在别处拼装：

1. 第一条必含**当前策略仓位**与**依据日期**：`"策略当前空仓（2026-10-20 死叉离场，距今 2 个交易日）"`。
2. `action == hold` 且 `state ∈ {neutral, near_buy, near_sell}` 时，**禁止**出现 `"卖出信号"` / `"买入信号"` 字样；历史信号只能表述为 `"曾于 YYYY-MM-DD 出现卖点"`。
3. `action == sell` 时必须写明 `"今日（YYYY-MM-DD）触发"`，且 `position_state` 必须为 `holding`。
4. `action == buy` 时必须写明今日触发与证据等级（`edge.verdict`）。
5. 文案中的任何数字必须可回溯到 `signal.current_value` / `threshold` / `last_close` / `edge` 之一，**不得出现无出处的数字**。

> 判据不是"读起来顺"，而是正则可查：`action=hold` 时 `reason` 不含 `卖出信号|买入信号`；含日期的句子必须能对上 `last_*_date`。

### 6.3 `evidence.edge`（新增，替换 `significant` 的模糊语义）

```json
{
  "verdict": "positive_significant | inconclusive | negative_significant",
  "point": -0.000624,
  "ci_95": [-0.001290, 0.0000242],
  "p_positive": 0.035,
  "n_obs": 3926,
  "dsr": 0.051,
  "dsr_ok": true
}
```

- `verdict`：`ci_95[0] > 0` → `positive_significant`；`ci_95[1] < 0` → `negative_significant`；否则 `inconclusive`。
- `evidence.significant = (verdict == "positive_significant") and dsr_ok`。**语义收紧为"统计上可证明有超额优势"**，而不是"不能排除正收益"。
- `p_positive` 必须与 CI 自洽：`p_positive >= 0.975 ⟺ ci_95[0] > 0`，`p_positive <= 0.025 ⟺ ci_95[1] < 0`（2000 次重抽样下的分位数等价关系）。
- 当 `verdict == "inconclusive"` 且 `p_positive < 0.5` 时，`diagnostics.warnings` **必须**含一条：`"超额收益点估计为负（p_positive=3.5%），该策略不应作为择时依据"`。

**为什么 `inconclusive` 不直接判 `no_edge`**：`no_edge` 保留给"统计上可证明为负"（`negative_significant`）。把"无证据"也判成 `no_edge` 会让系统对所有标的都输出 `avoid`，反而丢失信息。区别在于 `action` 被证据门控（§7），Hermes 可以自己按 `verdict` 决定要不要听。

### 6.4 `evidence.signal_stats`（新增，必填）

对**被选中的那一个策略**，统计其历史上每次买卖点之后的实际走势（这是用户问题的直接答案）：

```json
{
  "strategy": "<被选中策略名>",
  "horizons": [1, 3, 5, 10, 20],
  "n_buy": "<int>", "n_sell": "<int>",
  "buy":  {"mean_fwd": {"<h>": "<float>"}, "hit_rate": {"<h>": "<float>"}},
  "sell": {"mean_fwd": {"<h>": "<float>"}, "hit_rate": {"<h>": "<float>"},
           "missed_rally_mean_5d": "<float>"},
  "sell_false_alarm_rate_5d": "<float>",
  "note": null
}
```

> 上表是**结构示意**（尖括号为占位类型说明），真实数值由 F28 计算后写入。禁止在实现里回填示例值。

- `hit_rate`：买点后收益 > 0 的比例；卖点后收益 < 0 的比例。
- `sell_false_alarm_rate_5d`：卖点后 5 日收益 > 0 的比例（即"卖错了"的比例）。
- `missed_rally_mean_5d`：卖点后 5 日收益为正的那些样本的均值（踏空成本）。
- 样本不足（`n_sell < 5`）时 `note` 写明原因，字段保留、值为 `null`，**不得整块缺失**（C3 同构要求）。

### 6.5 `data.execution`（新增，必填）

```json
{"signal_bar": "close", "fill": "next_open | next_close", "slippage_bps": 5, "commission_bps": 3}
```

声明本次结论对应的**成交假设**。`evidence.metrics` 必须由同一假设下的回测产生。当前实现为 `next_close`；改为 `next_open` 是 F29 的内容。

---

## 7. 决策表（唯一真源，`decision/actions.py`）

```python
def decide(status, state, edge_verdict, confidence, significant) -> str:
    if status in ("insufficient_data", "data_source_error"): return "insufficient_data"
    if status in ("untradeable", "no_edge"):                 return "avoid"
    if state == "sell_triggered":                            return "sell"
    if state == "buy_triggered" and edge_verdict == "positive_significant" \
       and significant and confidence >= 0.30:               return "buy"
    return "hold"
```

> **不要传 `position_state`**。`state` 的定义已包含"事件前"的仓位
> （`buy_triggered` ⇔ 此前空仓，`sell_triggered` ⇔ 此前持仓）。
> 首版实现曾传事件**后**的仓位来判断事前前提，导致 `buy` 永不触发。

设计原则（写进代码注释，防止下轮又被"优化"掉）：

- **卖出靠纪律，买入靠证据**：风控动作不等统计显著性；建仓动作必须有证据下限。
- **动作是增量，不是判断**：`buy`/`sell` 表示"相对策略当前仓位要做什么"，`hold` 表示"不动作"。`position_state` 单独给出，Hermes 不必从文案猜。
- **不再有"连续 N 根"**。事件型信号按事件处理，状态型策略由 `position_state` 自然承载。

---

## 8. 稳定性不变式（替换 v1.0 B6 验证一）

v1.0 要求三窗口（full / drop60 / drop120）的 `recommendation.action` 完全一致。为满足它，实现里加了"15 根全真"过滤器——**这是用语义错误换来的稳定**，本轮必须撤掉。

新不变式（对同一标的、同一份缓存，只改窗口）：

| 层级 | 要求 | 说明 |
|---|---|---|
| L1 必须一致 | `recommendation.signal.position_state` | 决策的真正输入，路径依赖但鲁棒 |
| L2 必须如实上报 | `evidence.stability.action_stable` / `family_stable` | `action` 允许因窗口不同而变化，但**必须如实报告**，禁止用过滤器凑 `true`；单次运行只跑一个窗口时一律 `null` |
| L3 必须不同 | 三窗口 `data.bars` **互不相同** | 这是"打桩真的生效"的证据（R5） |
| 观察项（不判失败） | `evidence.consensus.family` | 它是 40 个族"评分中位数"的 argmax，窗口小幅变化即可翻转；只影响 `invalidation` 文案与 `agreement`，不参与 `action` 计算 |

**2026-10-25 实测（600519，full/drop60/drop120）**：`bars = 6027 / 5967 / 5907`（互不相同 ✓）、
`position_state` 三窗口均为 `flat` ✓、`action` 均为 `avoid` ✓、`consensus.family` 在
`唐奇安` / `ATR通道` 之间漂移（`family_drift=true`，已如实上报，不再声称稳定）。

> 原 v2.0 草稿曾把 `family` 也列为 L1 硬不变式。实测表明这会把"两个近似趋势通道族之间
> 的 argmax 抖动"判成失败，从而逼着实现重新引入某种平滑/过滤器——那正是本轮要拆的东西。
> 因此改为观察项：**允许漂移，但禁止谎称稳定**。若将来要让 family 真正稳定，应单独提案
> 族级滞回，不得靠收紧门禁解决。

若 L1 实测不稳，允许的处理顺序是：族级共识 → 评分平滑 → 滞回（带明确状态转移条件）。**不允许**重新引入"连续 N 根"或"末根瞬时态"之类的口径把差异藏起来。

---

## 9. Phase E — 语义（P0，先做）

- [ ] **E1 决策表落地**（F26）。新建 `decision/actions.py`，实现 §7；`cli.py:419-423` 的 15 根过滤器删除；`action` 只由 `decide()` 产生。
  **验证**：合成"单根金叉 + 后续 15 根无信号"的序列，断言 `action=buy`（旧实现必然 `hold`）；再构造"金叉后一根死叉"，断言 `action=sell`。
- [ ] **E2 信号状态机**（F24/F25）。新建 `decision/signals.py`，产出 §6.1 全部字段；`position_state=flat` 时禁止 `near_sell`。
  **验证**：用 002415 截至 2026-10-22 的数据，断言 `position_state=flat`、`state ∈ {neutral, near_buy}`（实测为 `near_buy`：MA3/MA30=0.9795，距金叉 2%）、`last_sell_date=2026-10-20`、`last_sell_age=2`、`current_value != last_close`。
- [ ] **E3 文案单出口**（F23）。新建 `decision/render.py`；`format_trigger_message` 的 `action` 死参数删除；`cli.py:389` 的提前生成删除。
  **验证**：`action=hold` 时 `action_reason` 不含 `卖出信号|买入信号`；`action=sell` 时含当日日期；`action_reason` 与 `action` 的一致性对 9 只标的 × 3 窗口全部成立。
- [ ] **E4 `trigger.current` 改指标值**（F25）。`evidence.triggers[].current` 为指标当前值，新增 `last_close` 字段；`buy_trigger`/`sell_trigger` 文案不得再把收盘价当指标值。
  **验证**：`current != last_close` 至少在 8/9 标的成立（允许极少数恰好相等）；同一标的在两个不同截止日的 `current` 不相等，而 `last_close` 各自等于当日收盘。
- [ ] **E5 契约与 README 同步**（F33）。§6 全部字段写入 README「给 Agent 的接口契约」，并给出 002415 的真实示例。
  **验证**：README 可检索到 `position_state`、`last_sell_age`、`edge.verdict`、`signal_stats`、`execution.fill`；示例 JSON 可被 `json.loads` 解析。

---

## 10. Phase F — 评价（P1，E 之后）

- [ ] **F1 信号级统计**（F28）。新建 `decision/evaluate.py::signal_stats()`，产出 §6.4。
  **验证**：对 002415 断言 `n_sell > 0`、`horizons == [1,3,5,10,20]`、`sell.hit_rate["1"]` 与手算一致（容差 1e-9）；`sell_false_alarm_rate_5d ∈ [0,1]`。
- [ ] **F2 成交时点声明**（F29）。`BacktestConfig` 增 `exec_price: str = "next_open"`；`data.execution.fill` 如实回报。
  **验证**：同一信号序列在 `next_open` 与 `next_close` 下 `trade_count` 相同但成交价不同；`data.execution.fill` 与配置一致。
- [ ] **F3 决策回放**（F30）。`decision/evaluate.py::decision_replay()`：用**被选中策略**的日频信号 + §7 决策表 + 250 根滚动超额收益点估计，逐日回放，产出 `evidence.decision_replay`。
  **验证**：`method` 字段必须写明近似方式；断言 `trade_count` 与原始信号回测**不同**（证明它真的在跑另一套规则），且 `exposure ∈ [0,1]`。
  > 允许的近似：不逐日重跑 581 组网格。**禁止**把该结果称为 walk-forward。
- [ ] **F4 `--as-of` 复盘**（F31）。新增参数，把数据截断到指定日期后完整运行，`data.window_note` 注明。
  **验证**：`002415 --as-of 2026-10-22` 与 `--as-of 2026-10-23` 的 `data.bars` 分别为 3927 / 3928；后者 `last_close=33.78`。

---

## 11. Phase G — 验证完整性（G1 最先，必须与 F22 同批完成；G2–G7 随各阶段落地）

- [ ] **G1 修 G06 打桩**（F22）。`probe.py::check_windows` 改为打桩 `meta.resolve_and_fetch`；并断言三窗口 `bars` 互不相同。
  **验证**：`drop120` 下 `bars` 必须等于 `len(full)-120`（002415 应为 3808，当前实测 3928）；三值互不相等。
- [ ] **G2 打桩自检通用化**（F32）。probe 内所有 monkeypatch 必须紧跟一条"生效断言"；新增 `check_stub_selfcheck()`，任一处缺自检即 FAIL。
  **验证**：临时移除某处自检 → **G44** FAIL（变异测试）；恢复后 PASS。
- [ ] **G3 语义门禁 G40**（F23/F24）。断言 `action_reason` 与 `action` 一致、`flat` 时无 `near_sell`、日期与 `age` 自洽。
- [ ] **G4 统计门禁 G41**（F27）。断言 `significant == (ci_lo>0 and dsr>0)`、`verdict` 与 CI 一致、`p_positive` 与 CI 分位数一致。
- [ ] **G5 统计门禁 G42**（F28）。断言 `signal_stats` 齐备且 `hit_rate ∈ [0,1]`、`n_sell` 与信号序列一致。
- [ ] **G6 成交声明门禁 G43**（F29）。断言 `data.execution` 五键齐备且 `fill` 与 `BacktestConfig.exec_price` 一致。
- [ ] **G7 回归语料**（F34）。9 只实盘标的 × 3 窗口的语义不变式固化为测试（断言不变式，不锁死数值）。
  **验证**：`pytest -k semantic_corpus` 全绿；任一标的出现"flat + near_sell"即红。

---

## 12. Definition of Done（逐条跑，贴原始输出）

在 `source/` 下：

1. `python -m pytest tests/ -q` → `0 failed / 0 error`，通过数 ≥ **60**（2026-10-25 实测 60）。
2. `python tools/probe.py all` → **35 个 key 全 PASS**（2026-10-25 实测 35/35）。
3. `pwsh -File tools/check_gates.ps1 -Full` → `FAIL 0 / BLOCKED 0 / SKIP 0`，退出码 **0**，PASS = **42**。
4. `python cli.py 002415 --as-of 2026-10-22 --json --fast` → `action=hold`；`signal.position_state=flat`；`signal.last_sell_date=2026-10-20`；`action_reason` **不含** `卖出信号`；`trigger.current != last_close`。
5. `python cli.py 002415 --as-of 2026-10-23 --json --fast` → 同上，且 `data.last_close=33.78`；`action_reason` 说明"空仓 + 卖点距今 3 个交易日"。
6. `python cli.py 600519 --json --fast` → `evidence.edge.verdict` 与 `significant` 自洽；`evidence.signal_stats.n_sell > 0`；`data.execution.fill` 非空。
7. 三窗口（full/drop60/drop120）→ `bars` 三者互不相同，`position_state` 与 `consensus.family` 一致，`stability.action_stable` 如实。
8. 连跑两次（剔除 `generated_at`/`elapsed_seconds`）JSON 完全相同。
9. `(Get-Content cli.py).Count` → ≤ **700**。
10. README 含 §6 全部新字段与 002415 示例；示例 JSON 可解析。

最终报告必须逐条列出实测结果（命令 + 原始输出片段 + 退出码）。**没跑过的写"未验证"，不要写 ✅。**

> **截至 2026-10-25 的实际进度**：第 1–3 项已实测（60 passed / 35 keys 全 PASS / ps1 待用户执行）；
> 第 4–6 项用截断脚本等价验证通过（`--as-of` 属 F31，尚未实现）；第 7–10 项见 `PROGRESS.md` §2.1。

---

## 13. Boundaries

- **不新增策略、不改网格组数、不调评分权重**。本轮只改语义、评价与验证。
- **不反转买卖方向**。"低点应买"是一个假设，必须先用 `signal_stats` 量化（F1）再单独提案。
- **不引入新依赖**（含 scipy / sklearn / hypothesis）；统计一律 numpy 自实现。
- **不接收账户态**：无资金、无持仓、无成本价；仓位只输出比例。
- **不新增前端 / 数据库 / 定时任务 / 自动下单**。
- **不放宽任何现有门禁**；`check_gates.ps1` 与 `probe.py` 只允许加强。
- **不删除现有测试**；新增用例优先，确需修改必须在 `PROGRESS.md` 写明"错在哪、为什么、怎么算才对"。
- **如实表达不确定性**：证据不足时 `action` 只能是 `hold`/`avoid`，禁止给方向；`inconclusive` 必须在 warnings 里说清。
- **不重写 git 历史**；在当前 `main` 工作树内以新提交推进。
