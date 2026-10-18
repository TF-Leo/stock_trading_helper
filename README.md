# 给 Agent 的接口契约

## 概述

本项目提供 `python cli.py <code> --json` 命令行接口，输出单一 JSON 文档。底层由可导入函数 `advise()` 产出同一 schema。

> **⚠️ 先说最重要的一条（读取方必读）**：`recommendation.status != "ok"` 时，
> `horizon_days` 可能是 `[null, null]`，`entry_zone` / `stop_loss` / `take_profit` / `stop_method` 可能是 `null`。
> **这些字段必须先判空再运算**（详见 [错误响应与成功响应同构（C3）](#错误响应与成功响应同构c3) 的实测清单）：
> 直接 `horizon_days[0] * 2`、`stop_loss * 0.95`、`entry_zone[0]` 都会抛 `TypeError`；
> 而 `position_fraction * 资金` 会**静默**得到 `0`、`f"{horizon_days[1]} 天内"` 会**静默**得到 `"None 天内"` ——
> 这两个不报错但结论是错的，更危险。

## 调用方式

```bash
# 基本用法
python cli.py 600519 --json

# 指定基金
python cli.py 510300 --type fund --json

# 快速模式（性能优化）
python cli.py 600519 --fast --json

# 指定数据起始日期
python cli.py 600519 --start 20200101 --json
```

## 参数说明

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| code | str | 必填 | 股票代码或基金代码 |
| --json | bool | False | 输出 JSON 格式（stdout），文本日志走 stderr |
| --fast | bool | False | 快速模式：减少 bootstrap 次数、缩减候选池、walk-forward 折数 3→2 |
| --start | str | None | 数据起始日期（YYYYMMDD） |
| --type / -t | str | auto | 类型：auto/stock/fund。`auto` 按代码前缀推断（5 开头视为基金） |
| --jobs | int | 1 | 并行任务数 |
| --opt | str | grid | 优化方式：grid（全网格）/ bayes（贝叶斯寻优） |
| --detail | bool | False | 文本模式追加详细内容（触发器 / 候选表 / 指标 / 告警） |
| --no-regime | bool | False | 跳过市场环境计算，输出 `Unknown` + `definition=disabled`（不假装 Bull） |
| --opt-iters | int | 30 | `--opt bayes` 的迭代次数；`--fast` 时上限 12 |

> 退出码：`0`=ok、`10`=no_edge、`20`=insufficient_data、`30`=data_source_error、`40`=untradeable。
> 进程退出码由 `recommendation.status` 唯一决定（`decision/schema.py::EXIT_CODES`），**不是**"ok 就 0、其他都 10"。

## JSON Schema

### 退出码

| code | status | 含义 |
|------|--------|------|
| 0 | ok | 产出可执行建议 |
| 10 | no_edge | 无统计显著优势，建议观望 |
| 20 | insufficient_data | 数据条数不足（< 100 根） |
| 30 | data_source_error | 渠道池全失败且无可用缓存 |
| 40 | untradeable | 诊断显示无法正常成交，结果不可信 |
| 2 | — | 参数/用法错误（argparse 默认） |

### 枚举值

- **status**: `ok` / `no_edge` / `insufficient_data` / `data_source_error` / `untradeable`
- **action**: `buy` / `sell` / `hold` / `avoid` / `insufficient_data`
- **trigger.state**: `buy_triggered` / `near_buy` / `neutral` / `near_sell` / `sell_triggered`

### 完整字段表

```json
{
  "schema_version": "1.0",
  "generated_at": "2024-XX-XXTXX:XX:XX.XXXXXX",
  "request": {
    "code": "600519",
    "type": "stock",
    "start": "20200101",
    "opt": "grid",
    "jobs": 1,
    "fast": false
  },
  "instrument": {
    "code": "600519",
    "name": "贵州茅台",
    "board": "sh_main",
    "price_limit_pct": 0.10,
    "is_st": false
  },
  "data": {
    "bars": 1643,
    "start": "2020-01-02",
    "end": "2026-10-16",
    "last_close": 1329.0,
    "source": "cache",
    "from_cache": true,
    "stale": false,
    "cache_last_date": "2026-10-16",
    "lag_days": 0,
    "expected_date": "2026-10-16",
    "requested_source": "sina",
    "window_note": null
  },
  "market_regime": {
    "label": "Bull",
    "confidence": 0.7,
    "as_of": "2026-10-16",
    "definition": "simple_moving_average"
  },
  "recommendation": {
    "status": "ok",
    "action": "buy",
    "action_reason": ["MA(5-20): 买入信号 (2.50)"],
    "confidence": 0.65,
    "position_fraction": 0.20,
    "horizon_days": [5, 20],
    "entry_zone": [1302.42, 1355.58],
    "stop_loss": 1262.55,
    "take_profit": 1461.90,
    "distance_to_trigger": {
      "buy_pct": 0.0,
      "sell_pct": 0.0
    },
    "invalidation": "跌破止损价 1262.55 或 10 日内未触发",
    "no_edge": false
  },
  "evidence": {
    "consensus": {
      "family": "StochRSI",
      "agreement": 0.75,
      "members": ["StochRSI(14,25/75)", "MACD(12-26-9)"]
    },
    "dispersion": 0.15,
    "stability": {
      "windows_tested": 3,
      "action_stable": true,
      "family_stable": true,
      "top_k_overlap_min": 5
    },
    "n_trials": 10,
    "n_survivors": 8,
    "deflated_sharpe": 0.85,
    "significant": true,
    "metrics": {
      "annual_return": {"point": 0.1014, "ci_95": [0.05, 0.15]},
      "max_drawdown": {"point": -0.0678, "ci_95": [-0.10, -0.03]},
      "win_rate": {"point": 0.75, "ci_95": [0.65, 0.85]},
      "sharpe": {"point": 1.10, "ci_95": [0.8, 1.4]},
      "calmar": {"point": 1.50, "ci_95": [1.0, 2.0]},
      "profit_factor": {"point": 2.5, "ci_95": [1.8, 3.2]},
      "trade_count": {"point": 32},
      "avg_holding_days": {"point": 11.1},
      "time_in_market": {"point": 0.25}
    },
    "excess_vs_buy_hold": {
      "point": 0.05,
      "ci_95": [0.02, 0.08]
    },
    "buy_hold_baseline": {
      "total_return": 0.58,
      "annual_return": 0.12
    },
    "regime_win_rates": {},
    "triggers": [
      {
        "indicator": "MA(5-20)",
        "current": 2.50,
        "buy_trigger": "20日均线上方",
        "sell_trigger": "20日均线下方",
        "state": "buy_triggered",
        "buy_price": null,
        "sell_price": null
      }
    ]
  },
  "candidates": [
    {
      "name": "StochRSI(14,25/75)",
      "family": "StochRSI",
      "score": 0.85,
      "score_is": 0.85,
      "score_oos": 0.77,
      "annual_return": 0.1014,
      "max_drawdown": -0.0678,
      "win_rate": 0.75,
      "trade_count": 32,
      "calmar": 1.50,
      "deflated_sharpe": 0.85
    }
  ],
  "diagnostics": {
    "unaffordable_lot_events": 0,
    "blocked_by_limit_events": 0,
    "zero_trade_configs": 0,
    "survivor_configs": 10,
    "warnings": [],
    "elapsed_seconds": 15.5
  },
  "disclaimer": "本结果不构成投资建议，市场有风险，投资需谨慎。"
}
```

## 字段详细说明

### request
- **code**: 股票代码或基金代码（字符串）
- **type**: **实际判定**的标的类型（"stock" / "fund"），`--type` 会真正生效
- **type_hint**: 用户入参回显（"auto" / "stock" / "fund"）
- **start**: 数据起始日期（可选）
- **opt**: 优化方式（"grid" / "bayes"）
- **jobs**: 并行任务数（整数）
- **fast**: 是否快速模式（布尔值）

### instrument
- **code**: 标的代码
- **name**: 标的名称
- **board**: 板块（"sh_main"/"sh_star"/"sz_main"/"sz_gem"/"bj"/"fund"/"unknown"）
- **price_limit_pct**: 涨跌停幅度（0.10 表示 10%）
- **is_st**: 是否 ST 股（布尔值）

### data
- **bars**: 实际参与回测的 K 线根数（受 `--start` 截取影响）
- **start / end**: 窗口首末日期
- **last_close**: 窗口末根收盘价
- **source**: **实际生效**的数据来源（`cache` / `akshare` / `sina`），不是请求参数
- **from_cache**: 是否来自本地缓存
- **stale**: **数据是否降级** —— `true` 表示本次"触网失败，回退到旧缓存"，结论基于陈旧数据
- **cache_last_date**: 缓存末根 K 线日期（ISO）
- **lag_days**: 缓存末日 → 期望最新交易日 之间的交易日数（仅排除周末，为下界估计）
- **expected_date**: 期望的最新交易日（已跳过周末与当日未收盘）
- **requested_source**: 调用方传入的渠道名（可能与 `source` 不同）
- **window_note**: `--start` 生效时的截取说明，否则 `null`

> `stale=true` 时 `diagnostics.warnings` 必定包含一条降级说明。
> 消费方应把 `stale=true` 视为"结论时效性下降"，而不是普通告警。

### recommendation
- **status**: 状态枚举（见上表）
- **action**: 交易动作枚举（见上表）。`no_edge` / `untradeable` 时固定为 `avoid`
- **action_reason**: 行动理由列表（字符串数组）
- **confidence**: 置信度（0-1 浮点数）= `0.4×DSR + 0.3×consensus.agreement + 0.2×stability + 0.1×min(trade_count/60,1)`
- **position_fraction**: 仓位比例（0-0.30 浮点数），`action ∈ {avoid, insufficient_data}` 时为 0
- **horizon_days**: 时间窗口 `[max(2,⌊0.5×平均持仓⌋), min(60,⌈1.5×平均持仓⌉)]`；无回测（错误路径）时为 `[null, null]`
- **entry_zone**: 入场区间 `[last−0.5ATR, last+0.5ATR]`（不低于最近买入触发价）；`action=sell` 时为 null
- **stop_loss**: 止损价 = `last − 2.5×ATR(14)`，`stop_distance` 被 clamp 到 `[0.03, 0.20]`；无 ATR 时用 `BacktestConfig.stop_loss_pct`（0.08）
- **take_profit**: 止盈价 = `last + 4.0×ATR(14)`；无 ATR 时用 `take_profit_pct`（0.15）
- **stop_method**: `"atr"` 或 `"fixed"` —— 明确告诉调用方止损是用哪种口径算的（**与回测引擎同一口径**）；无回测（错误路径）时为 null
- **distance_to_trigger**: `{buy_pct, sell_pct}`，由"最近一次该向信号所在 bar 的收盘价"与 `last_close` 实算；无该向信号时为 null
- **invalidation**: 失效条件，模板 `收盘跌破 {stop_loss}，或 {family} 族共识转为看空，或 {n} 个交易日内未触发入场`
- **no_edge**: 是否无优势（布尔值），恒等于 `status == "no_edge"`

### 错误响应与成功响应同构（C3）

`status ∈ {insufficient_data, data_source_error}` 时，响应的**键集合与成功响应一致**，
只是不适用的字段为显式 `null` —— 调用方**不需要按 status 分支写防御性解析**：

- `evidence.metrics` 仍有 9 项，每项 `{"point": null, "ci_95": null}`
- `evidence.n_trials = 0`、`significant = false`、`consensus.family = null`、`triggers = []`
- `recommendation.horizon_days = [null, null]`、`stop_loss/take_profit/stop_method = null`、`position_fraction = 0.0`
- `data.bars/start/end/last_close` 在"数据太短"时是**已知真实值**（如 `bars=23`），与 `invalidation` 文本一致；拉取彻底失败时才为 `0 / "" / null`
- `candidates = []`（没有候选才是正确语义，不是缺字段）

> 门禁 **G35** 逐路径比对成功响应与 `insufficient_data` 响应的结构（排除空列表的元素路径），
> 任一字段整块缺失即翻红。这条门禁是为了防止"契约写着显式 null、实现整块缺失"再次发生。

#### 判空清单（实测，2026-10-18）

下表是**真实运行**（`--start` 造成 `insufficient_data`）得到的字段值，以及**朴素写法会怎样**：

| 字段（错误路径取值） | 朴素写法 | 实测结果 |
|---|---|---|
| `horizon_days = [null, null]` | `horizon_days[0] * 2` | ❌ `TypeError: unsupported operand type(s) for *: 'NoneType' and 'int'` |
| `horizon_days = [null, null]` | `f"{horizon_days[1]} 天内"` | ⚠️ **不报错**，得到 `"None 天内"` —— 静默产出畸形结论 |
| `stop_loss = null` | `stop_loss * 0.95` | ❌ `TypeError: unsupported operand type(s) for *: 'NoneType' and 'float'` |
| `entry_zone = null` | `entry_zone[0]` | ❌ `TypeError: 'NoneType' object is not subscriptable` |
| `position_fraction = 0.0` | `position_fraction * 100000` | ⚠️ **不报错**，得到 `0.0` —— 静默得到"0 股/0 仓"，容易被当成真实结论 |
| `distance_to_trigger = {buy_pct: null, sell_pct: null}` | `distance_to_trigger["buy_pct"] < 0` | ❌ `TypeError: '<' not supported between instances of 'NoneType' and 'int'` |

**推荐的读取顺序**：

```python
r = doc["recommendation"]
if r["status"] != "ok":
    # 不要继续算价位与仓位；只向用户转述 r["invalidation"] 与 diagnostics["warnings"]
    return {"usable": False, "reason": r["invalidation"]}

# status == "ok" 之后，这些字段仍有可能是 null（例如无该向信号时的 distance_to_trigger）
stop = r["stop_loss"]
if stop is not None:
    ...
```

> 为什么保留"静默"而不是抛错：`position_fraction` 用 `0.0` 表达"不该建仓"是语义正确的，
> 但读取方**必须区分"0 表示不建仓"与"0 因为没算出来"**——判据是 `recommendation.status`。

### evidence.metrics
每个指标包含：
- **point**: 点估计值（浮点数）
- **ci_95**: 95% 置信区间 [下限，上限] 或 null

支持的指标：
- annual_return, max_drawdown, win_rate, sharpe, calmar, profit_factor
- trade_count, avg_holding_days, time_in_market

### disclaimer
固定字符串："本结果不构成投资建议，市场有风险，投资需谨慎。"

## 示例输出

以下为**结构示意**：字段名、层级与类型与真实输出完全一致，数值为示意值。
（历史问题：此处曾放一份带 `...` 的"伪 JSON"，既不可解析也不是真实输出。）
唯一完整真实样例由命令现跑生成，见 `source/docs/example_output_600519.json`：

```bash
cd source && python cli.py 600519 --json --fast > docs/example_output_600519.json
```

```json
{
  "schema_version": "1.0",
  "generated_at": "2026-10-18T10:34:33.471955",
  "request": { "code": "600519", "type": "stock", "type_hint": "auto", "start": null, "opt": "grid", "jobs": 1, "fast": true },
  "instrument": { "code": "600519", "name": "贵州茅台", "board": "sh_main", "price_limit_pct": 0.1, "is_st": false },
  "data": { "bars": 1643, "start": "2020-01-02 00:00:00", "end": "2026-10-16 00:00:00", "last_close": 1329.0, "source": "cache", "from_cache": true, "stale": false, "cache_last_date": "2026-10-16", "lag_days": 0, "expected_date": "2026-10-16", "requested_source": "sina", "window_note": null },
  "market_regime": { "label": "Bull", "confidence": 0.7, "as_of": "2026-10-16 00:00:00", "definition": "ma20_ma60_return_volatility" },
  "recommendation": {
    "status": "ok",
    "action": "hold",
    "action_reason": ["暂无明确交易信号，建议观望"],
    "confidence": 0.539,
    "position_fraction": 0.129,
    "horizon_days": [9, 29],
    "entry_zone": [1317.88, 1340.12],
    "stop_loss": 1273.42,
    "take_profit": 1417.92,
    "stop_method": "atr",
    "distance_to_trigger": { "buy_pct": -5.3, "sell_pct": -2.87 },
    "invalidation": "收盘跌破 1273.42，或 StochRSI 族共识转为看空，或 29 个交易日内未触发入场",
    "no_edge": false
  },
  "evidence": {
    "consensus": { "family": "StochRSI", "agreement": 0.89, "members": ["MACD(3-34-3)", "CCI(20,-150/150)", "StochRSI(14,25/75)"] },
    "dispersion": 0.11,
    "stability": { "action_stable": true, "family_stable": true, "windows_tested": 3, "top_k_overlap_min": 5 },
    "n_trials": 581,
    "n_survivors": 522,
    "deflated_sharpe": 0.033,
    "significant": true,
    "metrics": {
      "annual_return": { "point": 0.053762, "ci_95": [-0.040951, 0.156577] },
      "max_drawdown": { "point": 0.163564, "ci_95": [0.119227, 0.375632] },
      "win_rate": { "point": 0.528302, "ci_95": [0.377358, 0.660377] },
      "sharpe": { "point": 0.49233, "ci_95": [-0.313136, 1.195496] },
      "calmar": { "point": 0.328692, "ci_95": [-0.118693, 1.192933] },
      "profit_factor": { "point": 1.437227, "ci_95": [0.690927, 2.662773] },
      "trade_count": { "point": 53, "ci_95": null },
      "avg_holding_days": { "point": 18.849057, "ci_95": [15.787736, 22.378302] },
      "time_in_market": { "point": 0.393183, "ci_95": null }
    },
    "excess_vs_buy_hold": { "point": -0.000112, "ci_95": [-0.000754, 0.000701], "p_positive": 0.415, "n_obs": 1642, "benchmark": "buy_and_hold_close" },
    "buy_hold_baseline": { "total_return": 0.386, "annual_return": 0.0513 },
    "regime_win_rates": { "Bull": 0.5, "Bear": 0.5625, "Shock": 0.5143 },
    "triggers": [{
      "indicator": "MACD(3-34-3)",
      "current": 1329.0,
      "buy_trigger": "买入信号触发价 1258.62（最近一次买入信号收盘价）",
      "sell_trigger": "卖出信号触发价 1290.88（最近一次卖出信号收盘价）",
      "state": "near_buy",
      "buy_price": 1258.62,
      "sell_price": 1290.88
    }]
  },
  "candidates": [
    { "name": "MACD(3-34-3)", "family": "MACD", "score": 0.9655, "score_is": 0.4533, "score_oos": 1.185, "annual_return": 0.0538, "max_drawdown": 0.1636, "win_rate": 0.5283, "trade_count": 53, "calmar": 0.3289, "deflated_sharpe": 0.012 }
  ],
  "diagnostics": {
    "unaffordable_lot_events": 0,
    "blocked_by_limit_events": 0,
    "zero_trade_configs": 19,
    "survivor_configs": 484,
    "warnings": [],
    "elapsed_seconds": 9.3
  },
  "disclaimer": "本结果不构成投资建议，市场有风险，投资需谨慎。"
}
```

> 上表数值取自 2026-10-18 10:34 对 600519 的真实运行（`--fast`，1643 根），
> 为便于阅读删减了 `candidates` 其余 9 条；**完整未删减样例**见
> `source/docs/example_output_600519.json`（同样由命令现跑生成）。

## 幂等性保证

同一份缓存数据下连续两次运行，JSON 除 `generated_at` 与 `diagnostics.elapsed_seconds` 外逐字节相同。所有随机过程使用固定 seed。

## 兼容性

CLI 向后兼容所有旧参数（`probe.py::check_old_args` 与 `tools/check_gates` 的 G29 每次都会验证）：
- `--detail`, `--no-regime`, `--start`, `--jobs`, `--opt`, `--opt-iters`, `-t|--type`
- 新增 `--json`, `--fast`

语义变更：
- `--type` 以前被忽略（只按代码前缀猜），现在真正生效；`request.type` 回显**实际判定值**，`request.type_hint` 回显入参
- 退出码以前只有 0/10（所有非 ok 状态都被压成 10），现在严格按 `status` 映射到 0/10/20/30/40

## 性能（D5 两档实测）

| 模式 | 命令 | 实测墙钟 | 契约上限 |
|---|---|---|---|
| `--fast` | `python cli.py 600519 --json --fast` | **≈ 9.8s**（581 组网格 + 2 折 walk-forward） | ≤ 60s |
| 全量 | `python cli.py 600519 --json` | **≈ 12.3s**（3 折 walk-forward + 2000 次 bootstrap） | ≤ 300s |

> 2026-10-18 实测（600519，1643 根缓存）。回测主循环已从"逐 bar `df.iloc[i]` 生成 Series"
> 改为 numpy 数组下标取值，单次 `--fast` 由 58s 降至 9.8s；同一输入下 **218 个输出字段逐字节一致**。
> `--jobs 4` 实测 14.7s，**比单进程更慢**（进程池开销大于单策略回测耗时），不建议使用。
> 门禁 G12/G14 每次都会实测这两档并判定。
> 若机器上 `akshare` 首次探测超时，会额外增加约 10s（股票名称查询已加本地缓存 `data/.name_cache.json`）。

## 首次运行 / 离线预热（重要）

`.gitignore` 排除了 `source/stock_analysis/data/`，因此**刚 clone 下来时缓存为空，首次运行必须联网**。

### 预热命令（**逐票断言**，失败会立刻报错而不是静默跳过）

```bash
# 在仓库根目录执行（也可用绝对路径从任意目录执行）
python tools/warm_cache.py                    # 默认标的集
python tools/warm_cache.py 600519 510300      # 只预热指定标的
```

实测输出（成功）：

```
600519: 1643 根
688981: 1509 根
002371: 1643 根
300750: 1643 根
510300: 1643 根
OK 预热完成，可离线运行
```

失败时**退出码 1** 并逐条列出原因（实测）：

```
000000: 0 根

ERROR 预热失败：
  - 000000: 0 根 < 100

常见原因：出口 IP 被该行情源封锁 / 当前网络不可达 / 代码不存在。
请把以上完整输出（含 stderr）反馈，不要继续后续步骤。
```

> **为什么必须逐票断言**：`get_stock_daily` 取数失败时**返回 `None` 且只往 stderr 打一行、不抛异常**。
> 若写 `[get_stock_daily(c) for c in ...]`，失败会被静默吞掉 —— "命令成功退出但缓存是空的"，
> 下一步才以 `FileNotFoundError` / `insufficient_data` 爆炸，误导排查方向。
> `tools/warm_cache.py` 就是为这个场景写的：逐票检查行数、汇总失败、非零退出。

### 冷克隆自检（**不需要网络**）

```bash
cd source && python -m pytest tests/ -q     # 无缓存也必须是 47 passed
```

测试**不依赖** `stock_analysis/data/*.csv`（只用合成序列）。若在空缓存下出现
`FileNotFoundError: stock_analysis/data/stock_600519_qfq.csv`，说明回归了 —— 请报告。

### 行为约定

- 预热成功后会写 `stock_{code}_qfq.csv` / `fund_{code}.csv`，之后**可完全离线运行**。
- **无缓存 + 无网络 → 退出码 30（data_source_error）**，不是 20。
- 有缓存但不足 100 根 → 退出码 20（insufficient_data）。
- 拉取失败**不会**再写出空壳缓存文件（0 行 CSV）；若历史上已存在空壳，读取时会自动识别并删除后重拉。
- ⚠️ 已知坑：`600519 --type fund` 会从基金通道把**股票 K 线**拉到 `fund_600519.csv`。
  该文件只用于验证 `--type` 是否生效（G16），不参与任何结论计算，但会占缓存空间，可安全删除。

## 依赖与 Python 版本（D3f）

### 推荐：Python 3.12（唯一被门禁验证过的组合）

| 组件 | 版本 |
|---|---|
| Python | **3.12.10** |
| akshare | 1.18.29 |
| requests | 2.32.5 |
| numpy | 2.2.6 |
| pandas | 3.0.1 |
| pytest | 8.3.5 |

验证：`pwsh -File tools/check_gates.ps1 -Full` → **PASS 35 / 退出码 0**；`pytest` → **47 passed**。

### Python 3.14 的已知冲突（**已修正**）

`pandas 3.0.1` 的元数据是条件依赖：

```
numpy>=1.26.0 ; python_version <  "3.14"
numpy>=2.3.3 ; python_version >= "3.14"
```

因此对 numpy 用**单一硬 pin 必然在某一侧失败**：早期版本写死 `numpy==2.2.6`，在 3.14 上会
`ResolutionImpossible`。现在 `requirements.txt` 用 **environment marker** 分档：

```
numpy==2.2.6; python_version < "3.14"
numpy>=2.3.3,<3; python_version >= "3.14"
```

- Python 3.14 组合 = 按 PyPI 元数据推导的**可安装**组合，但**尚未跑过门禁**（本仓库无 3.14 环境）。
  在 3.14 上请务必跑一遍 `pytest` 与 `python cli.py 600519 --json --fast` 并把结果反馈。
- 门禁 G30 对比 `requirements.txt` 的 pin 与实际已安装版本；它按 `pkg==x.y.z` 提取，
  marker 后缀不影响解析（已实测 PASS）。

## 实现要点（与结论可靠性直接相关）

- **walk-forward 是逐折重新寻优**：`analyzer.walk_forward_optimize()` 在每折的**训练段**上重新给候选打分选优，
  再用该折测试段评估所选参数；不是"固定策略重跑子窗口"。
- **止损止盈与回测同源**：`decision/sizing.py` 是唯一实现（ATR 2.5×/4.0×，`stop_distance` clamp 到 [0.03,0.20]），
  回测引擎 `BacktestConfig.stop_mode="atr"` 默认启用同一套口径，`stop_method` 字段会告诉你实际用的是哪种。
- **多重检验校正**：`evidence.n_trials` 是本次**实际评分过的配置数**（股票 581 / 基金 44），
  不是候选个数；DSR 用它做分母，试验越多越保守。
- **显著性判定**：`significant = (deflated_sharpe > 0) and (excess_vs_buy_hold.ci_95[1] > 0)`；
  超额收益 CI 上界 ≤ 0 时 `status=no_edge`。
- **族去相关**：`candidates` 与 `evidence.consensus.members` 是同一份列表，任一族占比 ≤ `ceil(n/2)`；
  族名取自注册表 `_grid_defs()` 的 prefix（不做字符串切分）。
- **基金网格**：`fund_analysis/fund_strategy/registry.py::_grid_defs()`，44 组 / 6 族（RSI 16、双均线 9、MACD 8、
  波动率目标 6、指数轮动 4、风险平价 1），`grid_size()` 由该定义派生。
