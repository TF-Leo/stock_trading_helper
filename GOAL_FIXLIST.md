# GOAL_FIXLIST.md — 第二轮修复清单（F22–F34，每条自带验收命令）

> 生成：2026-10-25　基准：`main` @ `d4264a4`（工作树 clean，领先 origin/main 若干提交）
> 上游契约：`GOAL_CONTRACT.md` **v2.0**　台账：`PROGRESS.md`
>
> **唯一"完成"定义不变**：`pwsh -File tools/check_gates.ps1 -Full` 退出码 = 0。
> **本清单额外要求**：每修完一条，**必须同时把它变成门禁项**。上一轮的 G06 就是活反例——它至今仍是绿的，但打桩早已失效。
>
> **进度（2026-10-25 12:1x）**：**F22–F34 全部完成**（13/13）。
> 实测：**门禁 48/48 退出码 0**、pytest **67 passed**、probe **41 keys 全 PASS**。
> 提交：`954df2e` / `1ba98c8` / `090660b` / `1b9f567` / `12fafc0` / `213edee` / `cccb44d` + 本轮收尾。
> 实测证据见 `PROGRESS.md` §2.1 与 §7。

---

## 0. 执行硬规则

1. 编辑只用 Edit 小范围改；**禁止对 >300 行文件整体 Write**（`cli.py` 674 行、`probe.py` 1400+ 行）。
2. 禁止改 `source/tests/` 现有断言来迁就实现；禁止 `pytest.mark.skip` 绕过。
3. 禁止用 grep 源码字符串冒充"功能可用"；每条验收必须来自**真实执行输出**。
4. 禁止弱化 `tools/check_gates.ps1` / `tools/probe.py` 的判定；**只允许加强**。
5. **打桩必须自检**：patch 之后必须断言"被测代码真的拿到了打桩值"。只替换不校验 = 无效（G06 的教训）。
6. 长任务给显式 `timeout`；Python 一律在 `source/` 下运行；中文输出前设 `$env:PYTHONIOENCODING='utf-8'`。
7. 一条一提交，提交信息带条目号。
8. **禁止为了动作稳定重新引入"连续 N 根"过滤器**。这是本轮要拆的东西，不是可用工具。

## 0.1 本轮不再重复的项（A–D 已交付，勿返工）

A1–A14、B1–B9、C1–C8、D1–D6 已全部交付并由下游 Agent 真机复现。证据见 `PROGRESS.md` §0.2 档案表。
**唯一被本轮替换的判据**：B6 验证一（三窗口 `action` 必须一致）→ 见契约 §8。原判据正是 15 根过滤器的成因，不要"为了让它继续成立"去加过滤器。

## 0.2 推进顺序与依赖

```
第一批 P0（先修假绿，再修语义；顺序不能颠倒）
  F22 → F26 → F24 → F25 → F23
第二批 P1（统计、评价、打桩自检）
  F27 → F28 → F29 → F30 → F32
第三批 P2（复盘、文档、语料）
  F31 → F33 → F34
```

依赖关系：
- **F22 必须最先**。三窗口打桩失效时，任何语义改动都无法验证"是否更稳"，你会把噪声当信号。
- F23 依赖 F26（`action` 必须先由决策表产生，文案才有一致对象）。
- F24/F25 同属 `signals.py`，建议同一次提交内完成（都是"指标当前值 + 状态机"的产出）。
- F27 独立，但必须在 F28 之前（`signal_stats` 的 `hit_rate` 要与 `edge.verdict` 用同一套口径解释）。
- F30 依赖 F26（决策表）与 F29（成交假设）。
- F34 最后做（语料依赖前 12 条全部落地）。

## 0.3 跟踪表（**唯一需要逐条勾选的地方**）

| 状态 | 条目 | 批次 | 主要改动文件 | 新增/加强门禁 |
|---|---|---|---|---|
| `[x]` | **F22** G06 三窗口打桩失效（假绿） | P0 | `tools/probe.py` | **加强 G06** |
| `[x]` | **F26** 15 根全真过滤使事件型策略永不触发 | P0 | **新建** `decision/actions.py`、`cli.py` | G40 |
| `[x]` | **F24** `near_sell` 混淆"曾发生"与"接近" | P0 | **新建** `decision/signals.py`、`decision/triggers.py` | G40 |
| `[x]` | **F25** `trigger.current` 恒为收盘价 | P0 | `decision/signals.py`、`decision/triggers.py` | G40 |
| `[x]` | **F23** 文案在动作之前生成 + 死参数 | P0 | **新建** `decision/render.py`、`cli.py` | G40 |
| `[x]` | **F27** `significant` 语义过宽（p=3.5% 仍为 true） | P1 | `cli.py`、`decision/scoring.py` | G41 |
| `[x]` | **F28** 无信号级评价（卖点后到底涨还是跌） | P1 | **新建** `decision/evaluate.py`、`cli.py` | G42 |
| `[x]` | **F29** 成交时点未声明且不可配 | P1 | `engine.py`、`cli.py`、`schema.py` | G43 |
| `[x]` | **F30** 回测不评价线上规则 | P1 | `decision/evaluate.py`、`cli.py` | G42 |
| `[x]` | **F31** 无 `--as-of`，无法复盘历史截面 | P2 | `cli.py`、`decision/__init__.py` | G40 |
| `[x]` | **F32** 打桩自检未通用化 | P1 | `tools/probe.py` | G44 |
| `[x]` | **F33** README 契约章节缺新字段 | P2 | `README.md` | —（C2 已覆盖） |
| `[x]` | **F34** 9 只标的语义回归语料缺失 | P2 | `tests/test_semantic_corpus.py` | G40 |

**代码文件总览**：`source/cli.py`（7 条会碰）、`source/decision/{signals,actions,render,evaluate}.py`（**4 个新文件**）、
`source/decision/{triggers,scoring,schema,__init__}.py`、`source/stock_analysis/backtest/engine.py`、
`tools/probe.py`、`README.md`、`source/tests/test_semantic_corpus.py`（新增）。
**`source/tests/` 只允许新增用例。**

---

# 第一批 P0

## F22　G06 三窗口门禁的打桩点已失效（**当前所有"稳定"结论都不可信**）

**缺陷**：`tools/probe.py:1011-1030::check_windows` 打桩 `cli.get_stock_daily`：

```python
orig = cli.get_stock_daily
for label, drop in (("full", 0), ("drop60", 60), ("drop120", 120)):
    cli.get_stock_daily = (lambda d: (lambda *a, **k: d))(sub)
```

但取数早在 2026-10-18 的重构里改走了 `meta.resolve_and_fetch`（`cli.py:241-242`）：

```python
from stock_analysis.data_loader.meta import resolve_and_fetch
df, data_type, type_err = resolve_and_fetch(code, type_hint=type_hint, start_date=start)
```

`cli.get_stock_daily` 只是 `cli.py:12` 遗留的 import，**没有任何调用点**。于是 patch 一个死符号，三个窗口实际都跑全量数据。

**实测证据**（002415，全量 3928 根，模拟 drop120 应得 3808）：

```
expected_bars_if_drop120_worked = 3808
actual_bars                     = 3928
actual_end                      = 2026-10-23
```

→ **G06 的 PASS 只证明了"同一份数据跑三次结果相同"**，与稳定性无关。这是本轮最先要修的东西。

**改法**
1. `check_windows` 改打桩 `stock_analysis.data_loader.meta.resolve_and_fetch`，签名保持
   `(code, type_hint="auto", start_date=None)` 返回 `(df, data_type, err)`。
2. **紧跟一条生效断言**（这是本条的实质）：
   ```python
   got = len(json.loads(run_cli_json(...))["data"]["bars"])
   assert got == len(full) - drop, f"打桩未生效：期望 {len(full)-drop}，实得 {got}"
   ```
3. 三窗口结果里增加 `bars` 字段，断言 `len(set(bars)) == 3`。
4. 判据按契约 §8 重写（**2026-10-25 已更正实验设计**）：
   - **固定决策日、从头砍训练窗口**（`full` / `drop_head60` / `drop_head120`），三者 `data.end` 必须相同。
     原实现从**末尾**砍，等于把决策日也往前挪了 60/120 天——不同日期的仓位本来就该不同，
     那不是稳定性问题。这是本轮发现的第二个"门禁本身错了"。
   - L1 `action` 必须一致；L2 `position_state`/`family` 允许变化但 `stability` 不得谎称稳定；
     L3 `bars` 必须互不相同。

**验收**（在 `source/` 下）
```powershell
python tools/probe.py windows
```
期望：`drop120` 的 `bars=3808`、`full=3928`、`drop60=3868`，三者互不相同；`stub_effective=true`。
当前实测是三个 3928。

**加门禁**：加强 **G06** —— 除上述外，加一条反向自证：把 `drop` 改成 0 后必须 FAIL（证明断言在咬人），跑完还原。

---

## F26　"最近 15 根全部为真"使事件型策略的 action 被机制性钉死为 hold

**缺陷**：`source/cli.py:419-423`

```python
_lookback = min(15, _n_sig)
has_buy_trigger  = (_n_sig >= 15) and bool(signals['buy'].iloc[-_lookback:].all())
has_sell_trigger = (_n_sig >= 15) and bool(signals['sell'].iloc[-_lookback:].all())
```

均线金叉/死叉是**单根事件**，`sell` 序列只会在交叉那一根为 True。要求连续 15 根全为真，等价于要求"同一根连续死叉 15 次"——**数学上不可能**。

后果：`action` 只能落在 `hold`（`cli.py:441-446` 的 `buy`/`sell` 分支都依赖这两个布尔）。
002415 在 10/22 的 `action=hold` 不是模型判断的结果，是**这个过滤器算出来的**。

同时它污染了回测口径：回测（`engine.py:155-156`）执行的是**原始单根信号**，线上执行的是**15 根过滤后的动作**。两者从未是同一个东西。

**改法**
1. 新建 `source/decision/actions.py`，实现契约 §7 决策表（纯函数，无 IO，可单测）。
2. 删除 `cli.py:419-423` 两个布尔量，`action` 改由 `decide(status, position_state, state, edge_verdict, confidence)` 产生。
3. `state` 来自 F24 的 `signals.py`（当根事件 + 仓位状态），不再用"近 N 根"近似。
4. 在 `actions.py` 顶部用注释写清设计原则（卖出靠纪律、买入靠证据、动作是增量），防止下轮被"优化"回去。

**验收**
```powershell
python -m pytest tests/test_semantic_actions.py -q
```
新增用例（合成数据，离线）：
- (a) 第 100 根单根金叉、之后 15 根全无信号 → `action == "buy"`（旧实现必为 `hold`）
- (b) 金叉后第 105 根死叉 → `action == "sell"`，且 `position_state` 由 `holding` 转 `flat`
- (c) `status="no_edge"` → `action == "avoid"`，与 `state` 无关
- (d) `edge_verdict="negative_significant"` + `buy_triggered` → `action != "buy"`

**加门禁**：**G40**（与 F23/F24/F25 共用）—— 断言 `action` 恒等于 `decide()` 的返回值（对 600519/002415/510300 三个标的实跑比对）。

---

## F24　`near_sell` 把"历史上发生过"当成"现在正在发生"

**缺陷**：`source/decision/triggers.py:24,38-41`

```python
lookback = max(1, min(10, n))
...
elif sell_recent > 0:
    state = 'near_sell'
```

`near_sell` 的定义是"**近 10 根内出现过卖点**"，与"接近卖出阈值"毫无关系。于是一次 10/20 的死叉，会让 10/21–11/3 的每一个交易日都被标记为 `near_sell`。

叠加 `triggers.py:122-123` 的渲染：

```python
elif t['state'] in ['sell_triggered', 'near_sell']:
    messages.append(f"{t['indicator']}: 卖出信号 ({t['current']:.2f})")
```

→ 10/23 大涨 4.19% 当天，Hermes 读到的仍是"卖出信号"。

**实测证据**（002415，两个截止日）：

```
2026-10-22: trigger_state=near_sell, action_reason="均线趋势(3-30-60): 卖出信号 (32.42)"
2026-10-23: trigger_state=near_sell, action_reason="均线趋势(3-30-60): 卖出信号 (33.78)"
```

**改法**
1. 新建 `source/decision/signals.py::build(df, buy, sell, indicator_values)`，产出契约 §6.1 的全部字段：
   `position_state / state / last_buy_date / last_buy_age / last_sell_date / last_sell_age / current_value / threshold`。
2. 状态机（严格按契约 §6.1）：
   ```
   position_state[t] = 1 if buy[t] else (0 if sell[t] else position_state[t-1])   # 初值 0，同根以 sell 优先
   buy_event[t]  = buy[t]  and position_state[t-1] == 0
   sell_event[t] = sell[t] and position_state[t-1] == 1
   near_buy  = position_state==0 and not buy_event and 距买入阈值 <= 5%
   near_sell = position_state==1 and not sell_event and 距卖出阈值 <= 5%
   ```
3. **硬约束**：`position_state == flat` 时 `state` 不得为 `near_sell`/`sell_triggered`。这条规则单独就能消灭本次事故。
4. `evaluate_triggers` 保留（D4 的指标级求值仍有用），但 `state` 字段改由 `signals.py` 提供，不再自行用 `lookback` 推断。

**验收**
```powershell
python cli.py 002415 --as-of 2026-10-22 --json --fast 2>$null | python -c "import json,sys; d=json.load(sys.stdin); s=d['recommendation']['signal']; print(s['position_state'], s['state'], s['last_sell_date'], s['last_sell_age']); assert s['position_state']=='flat'; assert s['state'] in ('neutral','near_buy'); assert s['last_sell_date']=='2026-10-20'; assert s['last_sell_age']==2; print('OK')"
```
期望：`flat neutral 2026-10-20 2` + `OK`。（`--as-of` 由 F31 提供；若 F31 未做，先用截断脚本等价验证，并在 PROGRESS 注明。）

**加门禁**：**G40** —— 断言"`flat` 时不存在 `near_sell`/`sell_triggered`"，对 9 只标的 × 3 窗口全量扫描。

---

## F25　`trigger.current` 恒等于当日收盘价，文案里没有任何指标值

**缺陷**：`source/decision/triggers.py:23,61`

```python
last_close = float(df['close'].iloc[-1])
...
'current': last_close,
```

于是每条触发器的"当前值"都是收盘价。文案 `f"{indicator}: 卖出信号 ({current:.2f})"` 里的数字因此**逐日跟着价格变**，与指标无关。

**实测证据**：002415 `trigger.current` 与 `data.last_close` 在 10/22、10/23 **精确相等**（32.42 / 33.78）。
这正是用户看到"卖出信号 (32.42)"时的困惑来源——32.42 是当天收盘价，不是任何指标读数。

**改法**
1. `signals.py` 计算 `current_value`：由策略提供（`BaseStrategy.describe_triggers` 若已有则复用，否则按策略类型给默认口径：均线类给 `MA_short/MA_long` 比值，阈值类给指标原值）。
2. `trigger` 字典增加 `last_close` 字段；`current` 改为指标值。
3. 文案改为 `"MA3/MA30 = 0.980（阈值 1.000）"` 这类**可解释**表述，禁止再把价格写成指标值。

**验收**
```powershell
python cli.py 600519 --json --fast 2>$null | python -c "import json,sys; d=json.load(sys.stdin); t=d['evidence']['triggers'][0]; print('current',t['current'],'last_close',t['last_close'],'ratio',t['current']/t['last_close']); assert abs(t['current']-t['last_close'])>1e-9, 'FAIL: current 仍是收盘价'; print('OK')"
```
期望：`current` 与 `last_close` 不相等（比值显著偏离 1）+ `OK`。
再对 002415 两个截止日断言 `current` 各自不同、且不等于各自 `last_close`。

**加门禁**：**G40** —— `abs(trigger.current - trigger.last_close) > 1e-9`，9 只标的逐一断言（允许最多 1 只因指标口径恰好相等而豁免，需在 reason 里写明）。

---

## F23　`action_reason` 在 `action` 之前生成，且函数签名里的 `action` 是死参数

**缺陷**：`source/cli.py:389`

```python
trigger_msg = format_trigger_message(triggers, 'hold')     # ← action 还没算（441-446 才算）
```

而 `source/decision/triggers.py:115`

```python
def format_trigger_message(triggers, action):
    ...
    # 函数体内从未使用 action
```

于是：**文案永远不知道动作是什么**，只能按触发器状态自行表述。这是"动作 hold、文案写卖出"的结构性成因——不是措辞问题，是数据流方向错了。

**改法**
1. 新建 `source/decision/render.py::build_reasons(action, signal, edge, warnings)`，实现契约 §6.2 的五条规则。
2. 删除 `cli.py:389` 的提前生成；`action_reason` 在 `action` 确定之后构建。
3. 删除 `format_trigger_message` 的 `action` 死参数（若仍有其他调用点，一并改）。
4. 文案模板（示例，须包含日期与仓位）：
   - `hold` + flat：`"策略当前空仓（2026-10-20 死叉离场，距今 2 个交易日）；今日无新信号"`
   - `hold` + holding：`"策略当前持有（2026-10-15 金叉入场，距今 5 个交易日）；今日无新信号"`
   - `sell`：`"今日（2026-10-20）触发死叉，策略由持有转为空仓"`
   - `buy`：`"今日（2026-10-15）触发金叉，证据等级 inconclusive，建议轻仓"`

**验收**
```powershell
python cli.py 002415 --as-of 2026-10-22 --json --fast 2>$null | python -c "import json,sys,re; d=json.load(sys.stdin); r=d['recommendation']; print(r['action'], r['action_reason']); assert r['action']=='hold'; assert not re.search('卖出信号|买入信号', r['action_reason'][0]), 'FAIL: hold 仍出现信号字样'; assert '2026-10-20' in r['action_reason'][0]; print('OK')"
```
期望：打印 `hold ['策略当前空仓（2026-10-20 死叉离场，距今 2 个交易日）；今日无新信号']` + `OK`。

**加门禁**：**G40** —— 对 3 个标的：`action=hold` ⇒ reason 不含 `卖出信号|买入信号`；`action=sell` ⇒ reason 含当日日期且 `position_state=holding`；reason 中出现的日期必须存在于 `signal.last_*_date`。

---

# 第二批 P1

## F27　`significant` 的判据是"不能排除正收益"，名字却是"显著"

**缺陷**：`source/cli.py:418`

```python
significant = bool(dsr > 0 and exc_ci_hi > 0)
```

`ci_hi > 0` 的含义是"95% 区间上界为正"，即**不能排除正收益**。它不等于"有显著正收益"——后者要求 `ci_lo > 0`。

**实测证据**（002415 @2026-10-22）：

```
significant = True
excess.point = -6.24e-4 /日      （策略跑输买入持有）
excess.ci_95 = [-1.29e-3, +2.42e-5]
excess.p_positive = 0.035        ← 96.5% 的重抽样样本认为跑输
dsr = 0.051
```

**输出对 Hermes 的含义是"这个结论统计显著"，而事实是"这个策略大概率跑输基准"。** 这是本轮第二严重的问题（仅次于语义矛盾）。

**改法**
1. 在 `decision/scoring.py` 增加 `edge_verdict(point, ci_lo, ci_hi) -> str`，按契约 §6.3 判定三档。
2. `cli.py` 输出 `evidence.edge = {verdict, point, ci_95, p_positive, n_obs, dsr, dsr_ok}`。
3. `significant = (verdict == "positive_significant") and dsr_ok`。
4. `verdict == "inconclusive"` 且 `p_positive < 0.5` → 追加 warning（契约 §6.3 强制）。
5. `status` 规则不变（`no_edge` 仍只在 `ci_hi <= 0` 时触发），但**在 reason/warnings 里必须说清"无统计优势"**。

**验收**
```powershell
python cli.py 002415 --as-of 2026-10-22 --json --fast 2>$null | python -c "import json,sys; d=json.load(sys.stdin); e=d['evidence']; g=e['edge']; print(g); assert g['verdict']=='inconclusive'; assert e['significant'] is False, 'FAIL: p=3.5% 仍判显著'; assert any('p_positive' in w for w in d['diagnostics']['warnings']); print('OK')"
```
期望：`verdict=inconclusive`、`significant=False`、warnings 含 p_positive 说明 + `OK`。
再跑 600519 确认 `significant` 与 `ci_lo>0` 严格一致。

**加门禁**：**G41** —— `significant == (ci_lo>0 and dsr>0)`；`verdict` 与 CI 符号一致；`p_positive>=0.975 ⟺ ci_lo>0`、`p_positive<=0.025 ⟺ ci_hi<0`。

---

## F28　没有任何"卖点之后到底涨了还是跌了"的统计

**缺陷**：当前输出只有策略级指标（年化、回撤、胜率），**没有信号级评价**。于是"这个卖点是不是误报"无法回答，只能靠人工看图——用户这次正是这么发现的。

现有 `metrics` 回答的是"这套策略整体赚不赚钱"，不是"它给出的这条提示准不准"。二者是不同的东西。

**改法**
1. 新建 `source/decision/evaluate.py::signal_stats(df, buy, sell, horizons=(1,3,5,10,20))`，产出契约 §6.4。
2. 纯向量化实现（`close.shift(-h)/close - 1`），**不得引入循环遍历全历史**（3928 根 × 5 个 horizon 必须 < 50ms）。
3. 接入 `evidence.signal_stats`，错误路径保留键、值为 `null`（C3 同构）。
4. 顺带回答本次事故：002415 的 10/20 卖点，`fwd_1=-1.46%`（对）、`fwd_3=+2.93%`（错）、`fwd_5` 待算。这类样本积累起来，才能判断"卖点是否系统性偏晚"。

**验收**
```powershell
python cli.py 002415 --as-of 2026-10-22 --json --fast 2>$null | python -c "import json,sys; d=json.load(sys.stdin); s=d['evidence']['signal_stats']; print(s['n_sell'], s['sell']['hit_rate'], s['sell_false_alarm_rate_5d']); assert s['n_sell']>0; assert s['horizons']==[1,3,5,10,20]; assert 0<=s['sell_false_alarm_rate_5d']<=1; print('OK')"
```
另加离线单测：给定已知的 10 根序列与手工计算的 5 日收益，断言 `mean_fwd["5"]` 精确相等（1e-12）。

**加门禁**：**G42** —— `signal_stats` 十键齐备、`hit_rate ∈ [0,1]`、`n_sell` 与 `sell.sum()` 一致、`horizons` 为 `[1,3,5,10,20]`。

---

## F29　成交时点是 T+1 收盘，且从不声明

**缺陷**：`engine.py:155-156,259,277`

```python
df['buy_signal']  = raw_buy.shift(1).fillna(False).astype(bool)     # T 日信号，T+1 执行
...
exit_price = self._apply_slippage(close, OrderType.SELL)            # 成交价 = T+1 收盘
entry_price = self._apply_slippage(close, OrderType.BUY)
```

信号在 T 日收盘算出，**成交在 T+1 日收盘**。而 Hermes 拿到建议后，最早只能在 T+1 开盘动作。两者差了一整个交易日。

更麻烦的是：输出里**没有任何字段声明这件事**。Hermes 无法知道回测数字对应哪种执行假设，也无法判断自己按 T+1 开盘执行会不会偏离。

**改法**
1. `BacktestConfig` 增 `exec_price: str = "next_open"`（可选 `"next_close"`，后者为旧行为，用于回归对照）。
2. `run_backtest` 在 `exec_price=="next_open"` 时用 `open_arr[i]` 成交，`"next_close"` 时维持 `close_arr[i]`。
3. `data.execution = {signal_bar, fill, slippage_bps, commission_bps}` 如实回报，值与 `BacktestConfig` 同源（不得各写一份）。
4. README 说明：`fill` 改变会改变 `evidence.metrics` 的绝对值，但不应改变信号本身。

**验收**
```powershell
python -c "
from stock_analysis.backtest.engine import BacktestConfig
import pandas as pd, numpy as np
from stock_analysis.backtest.engine import BacktestEngine
n=300; idx=pd.date_range('2020-01-01',periods=n,freq='B')
c=np.linspace(10,20,n); df=pd.DataFrame({'open':c*0.99,'high':c*1.01,'low':c*0.98,'close':c,'volume':1e6},index=idx)
b=pd.Series(False,index=idx); b.iloc[10]=True; s=pd.Series(False,index=idx); s.iloc[100]=True
r1=BacktestEngine(BacktestConfig(exec_price='next_open')).run_backtest(df,lambda d:{'buy':b,'sell':s})
r2=BacktestEngine(BacktestConfig(exec_price='next_close')).run_backtest(df,lambda d:{'buy':b,'sell':s})
print(r1.trades[0].entry_price, r2.trades[0].entry_price)
assert abs(r1.trades[0].entry_price-r2.trades[0].entry_price)>1e-9, 'FAIL: 两种假设成交价相同'
print('OK')"
```
期望：两个入场价不同 + `OK`。
再断言 `cli.py ... --json` 的 `data.execution.fill == BacktestConfig().exec_price`。

**加门禁**：**G43** —— `data.execution` 五键齐备；`fill ∈ {next_open,next_close}`；`fill` 与 `BacktestConfig().exec_price` 一致。

---

## F30　回测评价的不是 Hermes 收到的那条规则

**缺陷**：三套口径并存。

| 层 | 规则 | 谁在用 |
|---|---|---|
| 回测 | 原始单根信号，T+1 收盘成交 | `evidence.metrics` 全部数字 |
| 线上动作 | 15 根全真过滤 + `significant` + `confidence` | `recommendation.action` |
| 下游文案 | 近 10 日"曾有信号" | Hermes 实际读到的东西 |

结论：**回测再好，也不能证明 Hermes 收到的提示有效**。这是契约 R3，也是"回测绿灯但实盘打脸"的结构性原因。

**改法**
1. 新建 `decision/evaluate.py::decision_replay(df, buy, sell, edge_series, config)`：
   逐日按契约 §7 决策表产出 `action`，按 `exec_price` 成交，统计 `trade_count / annual_return / max_drawdown / exposure / vs_buy_hold`。
2. `edge_series` 用 **250 根滚动窗口的超额收益点估计**（不做逐日 bootstrap，性能不可接受），并在 `method` 字段写明：
   `"trailing_250_point_estimate"`。
3. 输出 `evidence.decision_replay = {method, trade_count, annual_return, max_drawdown, exposure, vs_buy_hold, note}`。
4. **禁止**把该结果称为 walk-forward（它不是：参数仍来自全样本选择）。

**验收**
```powershell
python cli.py 600519 --json --fast 2>$null | python -c "import json,sys; d=json.load(sys.stdin); r=d['evidence']['decision_replay']; print(r); assert r['method']=='trailing_250_point_estimate'; assert 0<=r['exposure']<=1; assert r['trade_count']!=d['evidence']['metrics']['trade_count']['point'], 'FAIL: 回放与原始信号回测相同，说明没在跑决策表'; print('OK')"
```
期望：`trade_count` 与原始回测不同（决策表加证据门控后交易必然更少）+ `OK`。

**加门禁**：**G42** —— `decision_replay` 七键齐备、`exposure ∈ [0,1]`、`trade_count < 原始回测 trade_count`（证据门控只会减少交易）。

---

## F32　打桩自检没有通用化（F22 会再次发生）

**缺陷**：`probe.py` 里有多处 monkeypatch（G06 的 `cli.get_stock_daily`、G33 的 `_meta.resolve_and_fetch`、G32 的缓存守卫等），但**只有 F22 这一处被发现失效**。同类问题没有任何机制防止复发。

上一轮已经写过一次教训（`PROGRESS.md` 归档 §1.16 末尾："重构取数路径时必须同步检查门禁的打桩点"），但它只是**一句注释**，不是门禁。

**改法**
1. `probe.py` 增 `_assert_stub_effective(label, expected, actual)`：不一致时抛错并写明"打桩未生效"。
2. 所有 monkeypatch 之后必须调用一次，且 `expected` 必须是"只有打桩生效才可能得到的值"（如 `len(full)-drop`）。
3. 新增 `check_stub_selfcheck()`：静态扫描 `probe.py` 自身，统计 `orig_* = ` 赋值次数与 `_assert_stub_effective` 调用次数，后者 < 前者即 FAIL。

**验收**
```powershell
python tools/probe.py stub_selfcheck
```
期望：`{"patches": N, "selfchecks": N, "status": "PASS"}`。
变异测试：删掉 F22 里那条自检 → `status=FAIL`（跑完按 sha256 还原）。

**加门禁**：**G44** —— 把 `check_stub_selfcheck` 挂进映射表。

---

# 第三批 P2

## F31　没有 `--as-of`，历史截面无法复盘

**缺陷**：当前只能跑"最新数据"。本次事故的复现是靠临时 monkeypatch 截断 `meta.resolve_and_fetch` 完成的（见 `PROGRESS.md` §1 的脚本），**不可由用户或 Hermes 自行复现**。

没有这个能力，"10/22 到底输出了什么"永远只能靠翻旧报告；而报告可能被后续改动覆盖。

**改法**
1. `cli.py` 增 `--as-of YYYY-MM-DD`，透传进 `advise()`。
2. 数据层截断（`df.loc[:as_of]`），`data.window_note` 写明 `--as-of 2026-10-22 截断至 3927 根`。
3. 与 `--start` 共存时先 `--start` 后 `--as-of`，`window_note` 同时记录两者。
4. 幂等性保持（同一 `--as-of` 两次运行结果相同）。

**验收**
```powershell
python cli.py 002415 --as-of 2026-10-22 --json --fast 2>$null | python -c "import json,sys; d=json.load(sys.stdin); print(d['data']['bars'], d['data']['end'], d['data']['last_close'], d['data']['window_note']); assert d['data']['bars']==3927; assert d['data']['last_close']==32.42; print('OK')"
python cli.py 002415 --as-of 2026-10-23 --json --fast 2>$null | python -c "import json,sys; d=json.load(sys.stdin); assert d['data']['bars']==3928 and d['data']['last_close']==33.78; print('OK')"
```
期望：`3927 2026-10-22 32.42` 与 `3928 2026-10-23 33.78`。

**加门禁**：**G40** —— 把上面两条断言搬进 probe（作为 002415 语义回归的固定用例）。

---

## F33　README 契约章节缺本轮全部新字段

**缺陷**：契约 §6 新增/变更 5 组字段（`signal`、`action_reason` 规则、`edge`、`signal_stats`、`execution`），README 的"给 Agent 的接口契约"章节仍是 v1.0 内容。C2 要求"字段名逐字一致"，不同步就等于**规格文件在撒谎**（上一轮 R2 已经踩过一次同样的坑）。

**改法**
1. README 增/改五处，每处给出：字段表（名称/类型/可否为 null/含义）、枚举取值、002415 的真实示例。
2. 明确写出**读取方必须遵守的两条**：
   - `action` 是**增量动作**（相对 `signal.position_state`），不是对行情的判断；
   - `near_*` 表示"接近阈值"，不表示"曾经发生过"；历史信号请看 `last_*_date`。
3. 加一段"常见误读"，直接写本次事故：`hold` + 旧版 `action_reason` 的"卖出信号"字样不代表建议卖出（v2.0 起已不可能出现）。

**验收**
```powershell
python -c "
t=open('../README.md',encoding='utf-8').read()
for k in ['position_state','last_sell_age','edge','verdict','signal_stats','execution','fill']:
    assert k in t, k
print('OK')"
```

**加门禁**：C2 已覆盖（README 示例 JSON 可解析）；本条不新增门禁，但需在 PROGRESS 记录命中结果。

---

## F34　9 只实盘标的的语义回归语料缺失

**缺陷**：现有 47 项测试几乎全部基于合成数据或 600519/510300 两个标的。用户的实盘观察横跨 9 只股票，而**语义类缺陷（flat + near_sell）恰恰只在真实数据上出现**——合成数据太干净，构造不出"死叉后第 2 天"这种状态。

**改法**
1. 新增 `source/tests/test_semantic_corpus.py`，参数化覆盖：`600519, 002415, 002371, 300750, 000858, 603259, 601398, 600036, 688981`（缺缓存的标的自动 skip 并打印原因，**不得静默通过**）。
2. 每只标的断言**不变式**（不锁死数值）：
   - `position_state == flat` ⇒ `state not in {near_sell, sell_triggered}`
   - `action == hold` ⇒ `action_reason` 不含 `卖出信号|买入信号`
   - `trigger.current != trigger.last_close`
   - `action == sell` ⇒ `position_state == holding`
   - `edge.verdict` 与 `ci_95` 符号一致
3. 每个标的跑 3 个窗口（full/drop60/drop120），断言 `bars` 互不相同、`position_state` 一致。

**验收**
```powershell
python -m pytest tests/test_semantic_corpus.py -q
```
期望：`9 passed`（或 `N passed, M skipped` 且 skip 原因逐条打印）。
反向自证：临时把 `signals.py` 的 `near_sell` 判断改回旧口径（不看 `position_state`）→ 必须 FAIL。

**加门禁**：**G40** —— 语料进 pytest 即算覆盖；G40 另对 3 个主标的做 CLI 层复核。

---

# Definition of Done（逐条跑，贴原始输出）

```powershell
$env:PYTHONIOENCODING='utf-8'; [Console]::OutputEncoding=[System.Text.UTF8Encoding]::new($false)

# 1. 测试网
cd source; python -m pytest tests/ -q          # 期望 0 failed / 0 error，通过数 >= 55

# 2. 探针 + 权威门禁
cd ..; python tools/probe.py all               # 期望 44 keys 全 PASS
pwsh -File tools/check_gates.ps1 -Full         # 期望 FAIL 0 / BLOCKED 0 / SKIP 0，退出码 0

# 3. 事故标的语义（本轮核心）
cd source
python cli.py 002415 --as-of 2026-10-22 --json --fast 2>$null | python -c "import json,sys,re; d=json.load(sys.stdin); r=d['recommendation']; s=r['signal']; print(r['action'], s['position_state'], s['state'], s['last_sell_date'], r['action_reason']); assert r['action']=='hold'; assert s['position_state']=='flat'; assert s['state'] in ('neutral','near_buy'); assert not re.search('卖出信号|买入信号', r['action_reason'][0]); print('OK')"

# 4. 统计标签
python cli.py 002415 --as-of 2026-10-22 --json --fast 2>$null | python -c "import json,sys; d=json.load(sys.stdin); e=d['evidence']; print(e['edge']['verdict'], e['significant'], e['edge']['p_positive']); assert e['significant']==False; print('OK')"

# 5. 三窗口（必须真的不同）
python tools/probe.py windows                  # 期望 full=3928 drop60=3868 drop120=3808

# 6. 幂等 / 行数 / 文档
python cli.py 600519 --json --fast 2>$null | python -c "import json,sys; d=json.load(sys.stdin); print(len(d['evidence']['signal_stats']), d['data']['execution']['fill'])"
(Get-Content cli.py).Count                     # 期望 <= 700
```

**最终报告要求**：逐条列出 F22–F34 的实测结果（命令 + 原始输出片段 + 退出码 + 时间）。
没跑过的写"**未验证**"，**不要写 ✅**。任何被放弃的条目必须在 `PROGRESS.md` 写明"放弃条目 + 理由 + 批准人"。

---

# 附：本轮**不应重复劳动**的项（已确认通过）

| 项 | 证据 |
|---|---|
| 测试网 | `pytest tests/ -q` → **47 passed in 9.42s**（2026-10-25 复跑） |
| 权威门禁 | HEAD `d4264a4` → `PASS 39 / FAIL 0 / BLOCKED 0 / SKIP 0`，退出码 0 |
| 数据层多通道 | 东财封 IP 时自动落新浪，G36 PASS；`warm_cache.py` 5/5 落盘 |
| 错误路径同构 | G35 PASS（成功 134 路径 / 错误 116 路径，缺失 0，均为列表元素路径） |
| `window_note` 真值 | G37 PASS（三档，含错误路径） |
| 场外基金 | G39 PASS（023298/006195/001917/001564 全部出结论） |
| `entry_zone` 区间有效 | G38 PASS（600522/002415/300476 修复后 lower<=upper） |
| DSR 公式本身 | Bailey–López de Prado 实现正确，n_trials 单调性成立；问题只在 `significant` 的判据 |
