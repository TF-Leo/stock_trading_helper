# 持有天数：`holding_days` 与 `last_buy_age` 的语义差异（issue #144）

## 两个字段回答的不是同一个问题

| 字段 | 位置 | 含义 | 何时会被重置 |
|---|---|---|---|
| `recommendation.holding_days` | `recommendation`（整数字/`null`） | **实际持有交易日数**：策略引擎自入场以来已经持有的 bar 数 | 只有真正离场（止损 / 止盈 / 时间止损）才归零 |
| `recommendation.signal.last_buy_age` | `signal`（整数字/`null`） | **距最近一次买点信号的交易日数** | 持仓期间**再次出现买点信号**就重新从 0 起算 |

两者只有在「入场后没有新的买点信号」时才恰好相等。实测 600519 的 5 只
持仓样本中有 3 只不一致：`21 vs 2`、`16 vs 0`、`4 vs 1`（前者是实际持有
交易日数，后者是 `last_buy_age`）。**任何「已经持有了多久」的展示都必须
使用 `holding_days`。**

## 数据来源（不重新推导）

`recommendation.holding_days` 由 `source/cli.py` 从决策层已有的
`decision.policy.run_policy(...)['bars_held']` **原值透传**：

- 决策层的取值逻辑未改动；
- 渲染层不再解析 `action_reason` 文本（旧实现的正则
  `第\s*(\d+)\s*/\s*\d+\s*个交易日` 已删除）；
- 也不再用 `signal.last_buy_age` 兜底。

`bars_held` 仍保留在 `evidence.decision_replay` 的内部完整回放结果中，但
`public_view` 的白名单只输出 `POLICY_KEYS`，因此 JSON 的公开入口是
`recommendation.holding_days`。

## 缺失时的行为

`holding_days` 是 **additive** 字段：

- 成功路径：0 表示当前空仓；正整数表示实际持有交易日数；
- 错误路径（数据源失败 / 数据不足）：显式 `null`，与成功路径键同构（G35）；
- 渲染层遇到缺失或 `null` 时**省略**「（第 N 个交易日）」整段，主行只写
  「继续持有」，**绝不**回退到文本解析或 `last_buy_age`。

这样文案改版（例如 #138 的「今日」→「数据截至」、#140 的超期措辞）不会再
静默改变持有天数。

## 相关测试

- `source/tests/test_holding_days_144.py`：字段来源与 `bars_held` 逐值一致
  （多样本、600519 / 600000）、`build_reasons` 文案整体改写后主线不变、
  缺失时省略且不回退；
- `source/tests/test_plain_row_142.py`：主行的持有天数 / 超期措辞契约；
- `source/tests/test_advice_render.py::test_cli_json_structure_golden`：
  `--json` 键结构（含新增的 `holding_days`）。
