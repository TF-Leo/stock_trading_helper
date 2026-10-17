# AGENTS.md — 执行者硬规则

> 本文件不是建议，是**上一轮 15 小时、612 条消息、13 次人工"继续"**换来的对策清单。
> 每条规则后面都标注了它对应的历史事故。违反规则 = 必然重演事故。

## 0. 唯一"完成"定义

```
pwsh -File tools/check_gates.ps1        # 退出码必须是 0
```

**除这个退出码之外，任何自我判断的"已完成"都不算完成。** 禁止用文字总结替代门禁输出。

## 1. 运行模式（事故：整晚被迫 13 次人工救场）

- **要用子 Agent（Task）就必须在 Agent 模式。** 实测在 Goal 模式下调用 Task 会被直接拒绝：
  `Task is not allowed in goal mode. No action was performed.`
- **禁止反复 `SubmitGoal` 重开目标。** 历史事故：一夜之间产生 8 个 `.pi/goal/*.md`，
  且每次重开都把范围再砍一刀（`Phase A → Phase D → 只剩 D2/D3/D5`），
  结果 A/B/C 阶段整段被丢弃，用户被迫连打 13 次"继续""不要停"。
- **目标只能有一个**，写在 `PROGRESS.md` 顶部。要缩小范围必须显式列出"被放弃的条目 + 理由 + 谁批准"。
- **自驱节拍与模型**（2026-10-17 起）：单个每小时定时任务驱动；所有动代码的子任务必须走委派，
  当前会话模型为 qwen，委派时不再强制指定模型；主回合只调度 / 验收 / 回写台账。
- **验收权在主回合**：子 Agent 的自我报告不算证据，必须亲自跑门禁并读 `gates-report.json`。
- **验收权在主回合**：子 Agent 的自我报告不算证据，必须亲自跑门禁并读 `gates-report.json`。

## 2. 编辑工具（事故：25 次 Edit 语法失败，2 个子 Agent 因此阵亡）

- 只用 ops 语法。**PUT 带 body 行时 header 必须以 `:` 结尾**：
  ```
  PUT 48.=48:
  +新的一行
  ```
  漏掉冒号 → `EDIT_PARSE_FAILED: PUT with a colon requires at least one + body row`。
  历史上同一条 payload 连续重试 8 次，全部同样报错。
- **不要用 `old_string` / `new_string`**：本运行时报 `INVALID_ARGUMENT: ops required`。
- 行号必须是 Read / Grep / Write **刚刚展示过**的行；遇到 `EDIT_LINES_UNSEEN` 按提示用同一 tag 重试一次即可。
- **同一文件连续 3 次 Edit 失败就停手**：改小范围、重新 Read 精确取锚点，禁止原样重发。
- **禁止对 > 300 行的文件用 Write 整体重写。** 历史事故：重写 `cli.py` 后
  `main()` 整段消失、`_build_stock_strategies`/`FUND_FEE_RATE` 等契约入口被抹掉，测试网从 25 绿变 8 红。

## 3. Shell（事故：107 次 Bash 调用里 50 次非零退出）

- 本机是 **PowerShell 7，不是 bash**：`timeout 15 cmd`、`Add-Content -Value "...\n..."` 这类写法会直接报
  "找不到接受自变量…的位置参数" / "ScriptBlock 只能指定为 Command 参数的值"。
- 长任务必须显式设置 Bash 工具的 `timeout` 参数（全量 CLI 约 50–300s，默认 60s 会截断）。
- 中文输出先设 `$env:PYTHONIOENCODING='utf-8'` 或
  `[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)`，
  否则 GBK 乱码会让你误判"文件坏了"。
- 路径统一用 `D:\git\stock_trading_helper\source`（Python 代码在这里，且必须在该目录下运行）。

## 4. 禁止的"变绿"手段（事故：门禁全绿而产品是坏的）

- **禁止改 `source/tests/` 的断言去迁就实现。**
  唯一例外：断言本身有算术/逻辑错误 —— 必须在 `PROGRESS.md` 写清"错在哪、为什么、怎么算才对"。
  （真实案例：`test_a1_100_percent_affordable` 断言"10 万本金也能买 100000/2000=500 股"，
  实际 100000/2000=50 股 < 1 手，断言本身算错。）
- **禁止用 grep 源码/常量字符串证明功能可用。**
  历史事故：门禁项"JSON 输出 schema_version='1.0'" 的通过证据只是源文件里存在
  `SCHEMA_VERSION = '1.0'` 这个字符串，而当时 `python cli.py 600519 --json`
  **真实运行是崩的**（`Object of type bool is not JSON serializable`，退出码 30）。
  验收必须来自**真实执行结果**。
- **禁止跳过 / 删除 / 放宽 `tools/check_gates.ps1` 与 `tools/probe.py` 的判定。**
  它们是唯一裁定器。发现判定过松可以**加强**（必须单独说明理由）；
  任何弱化一律视为作弊。
- 声明完成时必须贴出**原始命令 + 原始输出片段**；没跑过的项写"未验证"，不要写 ✅。

## 5. 契约冲突的处理（已真实发生过）

- 契约 D1 要求"删除 `strategy_master`"，但 `tests/test_p2_engineering.py` 要求该模块存在。
  正确做法：保留**薄委托垫片**（`strategy_master/config.py`、`runner.py`，带 DeprecationWarning），
  既不删接口也不删测试。
- 契约 A1 要求"可负担 K 线占比 100%"。实现方式是 `adaptive_oos_config()` 按标的最高价
  **自适应放大资金**，而不是假设固定 10 万本金能买 1500 元/股的一手。

## 6. 每轮固定动作（抵抗 compaction 失忆）

1. 先读 `PROGRESS.md`（尤其"下一步"一节）。
2. `cd source; python -m pytest tests/ -q`。
3. 只推进**一个**台账条目；改完立刻 `pwsh tools/check_gates.ps1 -Quick`。
4. 立即更新 `PROGRESS.md`：状态 + 证据（命令、输出、退出码、时间）。
5. 台账全部条目完成后跑**完整门禁**（不带 `-Quick`，性能项加 `-Full`）。
6. 目标只有全绿才允许结束；未全绿就继续下一个条目，不要输出"阶段性总结"就停下。

## 7. 事实速查（省掉重复踩坑）

- 仓库：`D:\git\stock_trading_helper`；代码在 `source/`；测试 `source/tests/`（28 项）。
- 策略网格真源：`source/stock_analysis/strategy/registry.py::_grid_defs()`（581 组）。
  基金网格：`source/fund_analysis/fund_strategy/registry.py`（44 组）。
- 行情缓存：`source/stock_analysis/data/stock_{code}_qfq.csv`（600519 / 688981 等 10 只）。
- 基金回测执行层：`source/fund_analysis/fund_backtest.py`（`cli` 保留同名 re-export 供测试）。
- 假期/断网：`pytest` 必须离线全绿，数据一律走缓存，禁止在测试里触网。
- 退出码契约：0=ok，10=no_edge，20=insufficient_data，30=data_source_error，40=untradeable。