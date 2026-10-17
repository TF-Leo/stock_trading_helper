# 给 Agent 的接口契约

## 概述

本项目提供 `python cli.py <code> --json` 命令行接口，输出单一 JSON 文档。底层由可导入函数 `advise()` 产出同一 schema。

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
| --fast | bool | False | 快速模式：减少 bootstrap 次数、缩减候选池 |
| --start | str | None | 数据起始日期（YYYYMMDD） |
| --type | str | auto | 类型：auto/stock/fund |
| --jobs | int | 1 | 并行任务数 |
| --opt | str | grid | 优化方式：grid/bayes |

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
    "source": "sina",
    "from_cache": true,
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
- **type**: 标的类型（"stock" / "fund"）
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

### recommendation
- **status**: 状态枚举（见上表）
- **action**: 交易动作枚举（见上表）
- **action_reason**: 行动理由列表（字符串数组）
- **confidence**: 置信度（0-1 浮点数）
- **position_fraction**: 仓位比例（0-0.30 浮点数）
- **horizon_days**: 时间窗口 [最小天数，最大天数]
- **entry_zone**: 入场区间 [最低价，最高价] 或 null
- **stop_loss**: 止损价 或 null
- **take_profit**: 止盈价 或 null
- **distance_to_trigger**: 距离触发器百分比
- **invalidation**: 失效条件字符串
- **no_edge**: 是否无优势（布尔值）

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
  "generated_at": "2026-10-17T13:30:00",
  "request": { "code": "600519", "type": "stock", "start": "20200101", "opt": "grid", "jobs": 1, "fast": true },
  "instrument": { "code": "600519", "name": "贵州茅台", "board": "sh_main", "price_limit_pct": 0.1, "is_st": false },
  "data": { "bars": 1643, "start": "2020-01-02", "end": "2026-10-16", "last_close": 1520.0, "source": "sina", "from_cache": true, "window_note": null },
  "market_regime": { "label": "Bull", "confidence": 0.7, "as_of": "2026-10-16", "definition": "simple_moving_average" },
  "recommendation": {
    "status": "no_edge",
    "action": "hold",
    "action_reason": ["无统计显著优势：多重检验校正后不显著"],
    "confidence": 0.2,
    "position_fraction": 0.0,
    "horizon_days": [5, 20],
    "entry_zone": [1490.0, 1550.0],
    "stop_loss": 1444.0,
    "take_profit": 1672.0,
    "distance_to_trigger": { "buy_pct": 1.2, "sell_pct": 3.4 },
    "invalidation": "跌破 1444.0 或 20 个交易日内未触发",
    "no_edge": true
  },
  "evidence": {
    "consensus": { "family": "MACD", "agreement": 0.6, "members": ["MACD(12-26-9)"] },
    "dispersion": 0.12,
    "stability": { "action_stable": true, "family_stable": true, "windows_tested": 3, "top_k_overlap_min": 3 },
    "n_trials": 581,
    "n_survivors": 17,
    "deflated_sharpe": 0.31,
    "significant": false,
    "metrics": {
      "annual_return": { "point": 0.12, "ci_95": [0.02, 0.22] },
      "max_drawdown": { "point": 0.18, "ci_95": [0.10, 0.30] }
    },
    "excess_vs_buy_hold": { "point": 0.01, "ci_95": [-0.05, 0.07] },
    "triggers": [{ "name": "MACD(12-26-9)", "state": "watch", "message": "MACD 柱状体尚未上穿 0 轴" }]
  },
  "candidates": [{ "name": "MACD(12-26-9)", "family": "MACD", "score": 0.42, "trade_count": 32 }],
  "diagnostics": { "unaffordable_lot_events": 0, "blocked_by_limit_events": 0, "warnings": [], "elapsed_seconds": 48.7 },
  "disclaimer": "本结果不构成投资建议，市场有风险，投资需谨慎。"
}
```

## 幂等性保证

同一份缓存数据下连续两次运行，JSON 除 `generated_at` 与 `diagnostics.elapsed_seconds` 外逐字节相同。所有随机过程使用固定 seed。

## 兼容性

CLI 向后兼容所有旧参数：
- `--detail`, `--no-regime`, `--start`, `--jobs`, `--opt`, `--opt-iters`, `-t|--type`
- 新增 `--json`, `--fast`

语义变更已在本文档标注。
