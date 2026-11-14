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
| --type / -t | str | auto | 类型：auto/stock/fund。**`auto` 双向探测**：5/15/16/18 开头按基金试，其余按股票试；取不到数据再试另一侧（见下方"场外基金与 auto 判定"）。不确定时请显式指定 |
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

### v3.0 语义（**消费方必读**）

2026-10-25 架构复审（`ARCHITECTURE_REVIEW.md`）后，输出的含义有三处根本变化：

1. **每条建议都有有效期。** `recommendation.horizon_days` 固定为 `[20, 20]`，
   `review_date` 给出复审日。**这不是"今天买、明天看"的信号** —— 实测 1–5 日尺度
   IC 不显著（|t|<2），20 日尺度才有边际。请不要用它做次日交易判断。

2. **`action` 是"相对当前仓位的增量动作"，且买入与卖出用不同机制。**
   - `buy`：当根买点 + 该侧 20 日条件期望达标（`side_expectancy.ok=true`）。
     **不再要求**"策略全样本 alpha 显著"——那是"策略值不值得信"的检验，不是单笔门槛。
   - `sell`：**只由风控触发**（止损 / 止盈 / 时间止损），文案会说明是哪一种。
     "跌破均线/死叉"**不再产生卖出**：实测卖点后 20 日平均 +1.04%、仅 5/14 只为负。
   - `hold`：不动作。`action_reason` 里可能出现"买点"二字，那是在描述**历史**（带日期）。
   - `target_position`（0/0.5/1.0）给出动作后的目标仓位，`signal.position_state` 给出当前仓位。

3. **`near_buy` / `near_sell` 表示"接近阈值"，不表示"曾经发生过"。**
   历史信号看 `signal.last_buy_date` / `last_sell_date` 与 `last_*_age`。
   硬规则：`position_state=flat` 时不会出现 `near_sell` 或 `sell_triggered`。

`evidence.significant` 仍然输出，但**只描述"该策略全样本超额收益是否显著"**，
不参与 `action` 判定；判断"这笔该不该做"请看 `recommendation.side_expectancy`。

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
    "fast": false,
    "as_of": null
  },
  "instrument": {
    "code": "600519",
    "name": "贵州茅台",
    "board": "sh_main",
    "price_limit_pct": 0.10,
    "is_st": false,
    "is_suspended": false,
    "limit_up": 1542.53,
    "limit_down": 1262.07,
    "quote_stale": false,
    "one_word_limit": null,
    "status_source": "eastmoney_st_list+tencent_quote"
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
    "window_note": null,
    "execution": {
      "signal_bar": "close",
      "fill": "next_open",
      "slippage_bps": 10,
      "commission_bps": 3
    }
  },
  "market_regime": {
    "label": "Bull",
    "confidence": 0.7,
    "as_of": "2026-10-16",
    "definition": "ma20_ma60_return_volatility",
    "index": {
      "label": "Shock",
      "confidence": 0.5,
      "as_of": "2026-10-16",
      "definition": "ma20_ma60_return_volatility",
      "source": "tencent_index:sh000300",
      "bars": 1662
    }
  },
  "recommendation": {
    "status": "ok",
    "action": "hold",
    "action_reason": ["持有中（第 4/20 个交易日，入场 2026-10-19 @ 33.3333）；未触发风控", "止损 31.6494、止盈 36.0276；2026-11-19 复审"],
    "signal": {
      "strategy": "均线趋势(5-30-120)",
      "indicator": "MA5/MA30",
      "position_state": "holding",
      "state": "near_sell",
      "as_of": "2026-10-22",
      "last_buy_date": "2026-10-19",
      "last_buy_age": 3,
      "last_sell_date": "2026-10-21",
      "last_sell_age": 1,
      "current_value": 0.9903,
      "last_close": 32.42,
      "threshold": {"buy": 1.0, "sell": 1.0},
      "conflict": false
    },
    "target_position": 1.0,
    "review_date": "2026-11-19",
    "side_expectancy": {"side": "buy", "horizon": 20, "mean": 0.02874,
                        "hit_rate": 0.5931, "n": 231, "t_stat": 4.2, "ok": true},
    "confidence": 0.65,
    "position_fraction": 0.20,
    "horizon_days": [20, 20],
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
      "windows_tested": 1,
      "action_stable": null,
      "family_stable": null,
      "top_k_overlap_min": null
    },
    "n_trials": 10,
    "n_survivors": 8,
    "deflated_sharpe": 0.2434,
    "significant": false,
    "edge": {
      "verdict": "inconclusive",
      "point": -0.00049,
      "ci_95": [-0.001215, 0.000181],
      "p_positive": 0.07,
      "n_obs": 3926,
      "dsr": 0.2434,
      "dsr_ok": true
    },
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
    "signal_stats": {
      "strategy": "均线趋势(5-30-120)",
      "horizons": [1, 3, 5, 10, 20],
      "n_buy": 234,
      "n_sell": 87,
      "buy": {"mean_fwd": {"1": 0.001469, "3": 0.008705, "5": 0.013653, "10": 0.017301, "20": 0.02874},
              "hit_rate": {"1": 0.5043, "3": 0.5279, "5": 0.5325, "10": 0.5541, "20": 0.5931}},
      "sell": {"mean_fwd": {"1": -0.000969, "3": 0.005473, "5": -0.000444, "10": -0.008096, "20": -0.006663},
               "hit_rate": {"1": 0.5402, "3": 0.5465, "5": 0.5233, "10": 0.593, "20": 0.5581},
               "missed_rally_mean_5d": 0.038323},
      "sell_false_alarm_rate_5d": 0.4535,
      "note": null
    },
    "decision_replay": {
      "method": "trailing_250_point_estimate",
      "trade_count": 0,
      "annual_return": 0.0,
      "max_drawdown": 0.0,
      "exposure": 0.0,
      "vs_buy_hold": -8.794562,
      "note": "决策表未放行买入（significant=False 或 edge 未达 positive_significant），回放全程空仓"
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
    "elapsed_seconds": 15.5,
    "source_attempts": []
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
- **price_limit_pct**: 涨跌停幅度（0.10 表示 10%）。有实时报价时以腾讯 `limit_up/last_close` 推算为准；否则按板块静态规则（主板 10%、创业板/科创板 20%、北交所 30%）。**2026-07-06 起沪深主板 ST/*ST 已由 5% 并轨为 10%**（交易所《交易规则（2026 年修订）》），故不再对主板 ST 做 5% 修正；创业板/科创板 ST 仍 20%，北交所 ST 30%
- **is_st**: 是否 ST/*ST（**真实值**：东财 §6.8 当日名单优先，其次腾讯报价名称前缀；两者都不可用时，若本地名称带 ST 前缀则判 `true` 并在 `diagnostics.warnings` 注明「仅依据名称」，否则为 `null`，不再默认 `false`）
- **is_suspended**: 是否停牌。判据是「交易日 10:00 后 / 已收盘 / 非交易日」腾讯报价定格（`is_stale=true` 且当日成交量为 0）；**盘前（<10:00）返回 `null`**，不把集合竞价前的正常定格误判为停牌
- **limit_up / limit_down**: 腾讯实时快照给出的当日涨停价 / 跌停价（元）；报价不可用时为 `null`
- **quote_stale**: 腾讯报价是否定格（`is_stale`）。**它本身不等于停牌**，只是"报价非当日真实成交"的信号
- **one_word_limit**: 一字板方向（`"up"` / `"down"` / `null`）。腾讯实时快照满足 `最高=最低=涨停价`（或跌停价）时给出；`null` 表示当日不是一字板或报价不可用。渲染层优先读取本字段，`diagnostics.warnings` 里的「一字涨停/一字跌停」文案仅作兜底。**仅成功路径存在**：错误响应由 `decision/*` 构造，本次改动未触碰该目录，故错误响应缺此键，消费方须按「缺失」处理
- **status_source**: 本次状态的数据来源，`+` 分隔的组合值：
  - `tencent_quote`：实时报价（涨跌停价 / 名称 / is_stale）
  - `eastmoney_st_list`：东财 §6.8 当日 ST 名单（新鲜）
  - `st_cache`：ST 名单接口失败后回退的旧缓存（同时 `diagnostics.warnings` 有说明）
  - `quote_unavailable`：腾讯报价取数失败或字段异常
  - `offline`：显式离线（`ASTOCK_STATUS_OFFLINE=1` 或 `ASTOCK_DISABLE_NETWORK=1`）
  - `fund_static`：基金不查询交易状态，仅保留静态板块字段
  - `unavailable`：错误响应（取数阶段已失败，状态不可得）

> P0-3（issue #6）新增字段全部是 **additive**，`schema_version` 保持 `1.0`：
> 旧字段含义不变，错误响应与成功响应键集合同构（G35）。无法获取时显式 `null`。
>
> 例外（issue #92）：`instrument.one_word_limit` 只在成功路径存在；错误响应由
> `decision/*` 构造且本次未触碰该目录，故缺此键。消费者须按「键缺失 = 无该
> 事实」处理。
>
> **可执行性硬提示**：`is_suspended=true`、疑似退市（报价不可用且行情连续滞后
> ≥20 个交易日，或报价定格且名称含退市标记）、一字涨停遇买入、一字跌停遇卖出时，
> `recommendation.status` 升为 `untradeable`（退出码 40）、`action=avoid`，
> 并在 `diagnostics.warnings` 写明原因。**不修改 `decision/actions.py` 的决策表**——
> 只拦截"今天这笔成交不了"的场景。
>
> ST 名单由东财 §6.8 提供，经上游 `em_get()` 串行限流（最小间隔 1s + 抖动）；
> 本地短缓存 1 个交易日（`source/stock_analysis/data/status/`，已 gitignore），
> 接口失败（含 429）回退旧缓存并 warning，不阻断主流程。本项目不引入 baostock。

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

### market_regime

`label / confidence / as_of / definition` 四个字段的含义与历史版本**完全一致**
（输入是**个股自身**的日线），未做任何改动。个股 `definition` 的实际取值是
`ma20_ma60_return_volatility`（取不到时为 `unavailable`，`--no-regime` 时为
`disabled`）。早期文档曾写成一个已废弃的均线口径名，与真实输出不符，现已按
`cli.py <code> --json` 的实际结果更正。

`index`（P1-3，additive，`schema_version` 仍为 `1.0`）是**沪深 300 指数**的
市场环境判定，只展示、**不参与 action / 仓位 / `regime_win_rates`**：

| 键 | 含义 |
|---|---|
| `label` | `Bull` / `Bear` / `Shock`（与个股 regime 同一套标签） |
| `confidence` | 置信度（0–1） |
| `as_of` | 指数日线最后一根的日期 |
| `definition` | 恒为 `ma20_ma60_return_volatility`：与个股 regime **同一判定规则**、不同输入 |
| `source` | 数据来源，当前为 `tencent_index:sh000300` |
| `bars` | 参与判定的指数日线根数（< 60 时不给标签） |

> 指数日线取不到（断网 / WAF / 数据不足 60 根）时，`index` 六个键**全部为显式
> `null`**，并在 `diagnostics.warnings` 追加一条说明；**CLI 正常返回**，`action`
> 与原有 `market_regime` 不受任何影响。`--no-regime` 时 `index.definition = "disabled"`。
>
> **关于 WAF 的措辞**：指数日线的三个腾讯入口（`web.ifzq.gtimg.cn` /
> `proxy.finance.qq.com` / `ifzq.gtimg.cn`）**并非永久同等可用**。
> **2026-11-12 观测**：只有 `web.ifzq.gtimg.cn` 返回 HTTP 501 WAF 页
> （`waf.tencent.com/501page.html`），另外两个正常 —— 即 **1/3 入口不可用**，
> 而不是"三入口一律 501"。这是**某一时点的观测**，不是永久结论：入口可用性
> 会随 WAF 策略变化，适配器因此保留三入口轮换 + 冷却，不依赖固定顺序。
> **2026-11-13 观测（issue #69）**：个股 K 线 `fqkline` 的三个入口**同时**
> 返回 HTTP 501 + JS 挑战页（`location.href` / `btoa`），而指数 `newfqkline`
> 与腾讯报价仍正常。**WAF 与 429/空响应限流是两种不同失败**：WAF 写 1800s
> 长冷却、冷却期内个股腾讯通道一个请求都不发；限流仍是 120s。详见
> `docs/ASTOCK_INTEGRATION.md` §5.1.2 与运维排查命令。
> 复现方式见 `docs/P1_EVIDENCE_PLAN.md` §2.2.3。
>
> 指数与个股标签**允许不同**（指数走弱而个股强势是正常现象）。本字段目前只用于
> 展示与后续研究；任何"用指数 regime 替换个股 regime / 改 action"的提案都需要
> 单独 PR 与 A/B 证据（见 `docs/P1_EVIDENCE_PLAN.md` §3.4 的验收门槛）。

### diagnostics

- **warnings**: 可读中文告警数组。P0-4 起逐通道失败也会写入，例如
  `astock_tencent 失败：HTTPError 429（本轮已冷却 120s）`、
  `eastmoney 失败：Timeout read timed out`、`sina_stock 冷却中，本轮跳过`。
  **首试即成功时不新增条目**（不制造噪声）。
- **source_attempts**（P0-4 / issue #7，**additive**，`schema_version` 仍为 `1.0`）：
  本轮渠道池的逐通道取数明细，数组元素为

  ```json
  {"source": "astock_tencent", "ok": false, "error_class": "HTTPError",
   "error_text": "429 Client Error: Too Many Requests for url: <url>",
   "cooldown": true, "cooldown_seconds": 120, "removed": false}
  ```

  - `ok=false`：本次**真实调用失败**，`error_class` / `error_text` 必有值；
  - `ok=null`：本轮**未调用**，因为该通道在 `cooldown` 中或已被 `removed`（从池中剔除）；
  - `ok=true`：本次成功；全通道首试成功时数组只有这一条，`warnings` 不新增。
  - `error_text` 经脱敏（长 URL → `<url>`、token/key → `<redacted>`）并截断到 **≤200 字**；
    `source` 只允许池内登记的标识符，避免把任意异常文本带进对外 JSON。
  - **WAF（issue #69，additive）**：腾讯个股 K 线命中 HTTP 501 + JS 挑战页时，
    该条目额外带 `"waf_blocked": true`，`cooldown_seconds` 是 WAF 长冷却剩余
    （默认 1800s）；冷却期内后续票记 `ok=null, cooldown=true` 且同样带
    `waf_blocked`（该通道本轮**不再发请求**）。普通限流/成功/失败条目不新增键。
  - 错误响应与成功响应键同构：无触网时为空数组 `[]`（G35）。
  - **基金（issue #24，additive）**：ETF / OTC 通道同样逐次记录，来源名为
    `fund_etf_sina`（`ak.fund_etf_hist_sina`）与 `eastmoney_otc`
    （`ak.fund_open_fund_info_em`）。场外基金走场内 ETF 通道**常态就是 0 行**，
    记为 `error_class="EmptyDataError"`；**同轮 OTC 成功时该条目不写入 `warnings`**
    （避免 023299/001917 这类自选标的的正常路径被无意义失败刷屏），但
    `source_attempts` 中仍可见。基金没有渠道池，故 `cooldown` / `removed` 恒为 `false`。

### recommendation
- **status**: 状态枚举（见上表）
- **action**: 交易动作枚举（见上表）。`no_edge` / `untradeable` 时固定为 `avoid`
- **action_reason**: 行动理由列表（字符串数组）
- **confidence**: 置信度（0-1 浮点数）= `0.4×DSR + 0.3×consensus.agreement + 0.2×stability + 0.1×min(trade_count/60,1)`
- **position_fraction**: 仓位比例（0-0.30 浮点数），`action ∈ {avoid, insufficient_data}` 时为 0
- **horizon_days**: 时间窗口 `[max(2,⌊0.5×平均持仓⌋), min(60,⌈1.5×平均持仓⌉)]`；无回测（错误路径）时为 `[null, null]`
- **entry_zone**: 入场区间 `[last−0.5ATR, last+0.5ATR]`，**保证 lower ≤ upper**；
  买入触发价仅在落入该区间内时才用于抬高下限（高于上沿的触发价属于"等涨到该价"的条件，
  从 `evidence.triggers[].buy_price` 读取，不混进入场区）。`action=sell` 时为 null。
  > 历史 bug（2026-10-18 修）：旧实现做 `lower = max(last−0.5ATR, buy_price)` 却不调整上沿，
  > 于是 `buy_price > last+0.5ATR` 时输出**倒置区间**（实测 600522 `[44.71, 27.39]`、
  > 002415 `[35.18, 33.98]`、300476 `[195.16, 188.49]`）。门禁 **G38** 现在校验 lower ≤ upper。
- **stop_loss**: 止损价 = `last − 2.5×ATR(14)`，`stop_distance` 被 clamp 到 `[0.03, 0.20]`；无 ATR 时用 `BacktestConfig.stop_loss_pct`（0.08）
- **take_profit**: 止盈价 = `last + 4.0×ATR(14)`；无 ATR 时用 `take_profit_pct`（0.15）
- **stop_method**: `"atr"` 或 `"fixed"` —— 明确告诉调用方止损是用哪种口径算的（**与回测引擎同一口径**）；无回测（错误路径）时为 null
- **distance_to_trigger**: `{buy_pct, sell_pct}`，由"最近一次该向信号所在 bar 的收盘价"与 `last_close` 实算；无该向信号时为 null
- **invalidation**: 失效条件，模板 `收盘跌破 {stop_loss}，或 {family} 族共识转为看空，或 {n} 个交易日内未触发入场`
- **signal**: 信号状态块（v2.0 新增）——
  `strategy`（策略名）、`indicator`（指标口径，如 `MA5/MA30`）、`position_state`（`flat`/`holding`）、
  `state`（五档，见枚举）、`as_of`、`last_buy_date`/`last_buy_age`、`last_sell_date`/`last_sell_age`、
  `current_value`（**指标当前值，不是价格**；不可复算的族为 `null`）、`last_close`、
  `threshold{buy,sell}`、`conflict`（同根买卖同时为真）
- **no_edge**: 是否无优势（布尔值），恒等于 `status == "no_edge"`

### evidence（v2.0 新增字段）

- **edge**: 证据等级 `{verdict, point, ci_95, p_positive, n_obs, dsr, dsr_ok}`；
  `verdict ∈ positive_significant / inconclusive / negative_significant`，
  判据是 CI **下界**>0（正）/ 上界<0（负）/ 其余（不确定）
- **signal_stats**: 信号级统计 `{strategy, horizons[1,3,5,10,20], n_buy, n_sell, buy{mean_fwd,hit_rate},
  sell{mean_fwd,hit_rate,missed_rally_mean_5d}, sell_false_alarm_rate_5d, note}`；
  回答"每次买点/卖点之后实际涨了还是跌了"。样本不足时 `note` 说明原因
- **decision_replay**: 按线上决策表逐日回放 `{method, trade_count, annual_return, max_drawdown,
  exposure, vs_buy_hold, note}`。`method` 如实标注近似口径（当前 `trailing_250_point_estimate`），
  **不是 walk-forward**。`trade_count=0` 时 `note` 必说明原因（如 `status=no_edge`）

### evidence.flow（P1-2，issue #36；#39 增加新浪备胎）

日级资金流**上下文**，`schema_version` 仍为 `1.0`（additive）。
**CLI 主路径绝不触网**，只读预计算缓存：

1. 收盘后运行 `python tools/precompute_flow.py`（逐票串行、相邻请求 ≥1s、失败重试后继续）：
   - **东财优先**（§4.5，`source=eastmoney_push2his`）；首票只发一次探测请求，
     整域 TLS RST / 空响应时立即标记本轮东财不可用，后续票不再请求东财
     （不逐票重试轰炸），改走新浪「备用源速查」`fund_flow_backup`
     （`source=sina_fund_flow_backup`）；
   - 两个源都失败才记失败，**不覆盖**旧缓存；缓存 JSON 记录
     `source` / `caliber` / `attempts`（各源实际请求次数）。
   写入 `source/stock_analysis/data/flow/<code>.json`（`data/` 已在 `.gitignore`）。
2. CLI / 盘前 cron 只读这份缓存。缓存缺失或过期时 `flow.available=false` + warning，
   CLI 正常返回，**不会为了资金流多发一个请求**。

**两种口径不可直接比较（#39 硬约束）**：

| `caliber` | `source` | 可用字段 | 显式 `null` 的字段 |
|---|---|---|---|
| `eastmoney_main_net` | `eastmoney_push2his` | `main_net_5d/20d`（主力净流入 = 超大单 + 大单） | `net_5d/20d` |
| `sina_total_net` | `sina_fund_flow_backup` | `net_5d/20d`（**总净流入**，新浪 `netamount`） | `main_net_5d/20d` |

新浪没有「大单/主力」拆分，因此**绝不会**把总净流入写进 `main_net`；两种口径
不是同一个量，不能直接相减、排序或跨票比较，调用方必须先看 `caliber`。
没有 `caliber` 的 P1-2 旧缓存按东财口径读取（向后兼容）。

**只展示、不改决策**：`evidence.flow` 不参与 `action`、`status`、`position_fraction`、
`entry_zone`、`target_position` 的任何计算；A/B 对照中 `recommendation` 逐键一致。

| 键 | 含义 |
|---|---|
| `available` | 缓存可用且至少有一行；不可用时其余数值键为 `null` |
| `code` | 6 位纯数字代码。**成功与错误路径一致**：`sh600519` / `600519.SH` 等写法都会被归一成 `600519`（`data_source_error` 等不可用块同样归一，不保留调用方原样写法） |
| `source` | 写入缓存的**数据来源**，当前可能取值：`eastmoney_push2his`（东财优先，见 §4.5）或 `sina_fund_flow_backup`（东财整域不可用时的新浪备胎）。不可用（`available=false`）时为 `null`。**该字段是开放集合**：后续新增来源（如新的备胎通道）只会增加取值，读取方应按「未知来源 → 不猜口径」处理，不要写死枚举 |
| `caliber` | **口径**，与 `source` 成对出现：`eastmoney_main_net`（主力净流入 = 超大单 + 大单）或 `sina_total_net`（总净流入）。两者**不是同一个量**，不可直接相减 / 排序 / 跨票比较；不可用时为 `null`。与 `source` 一样是开放集合，新口径会新增取值，读取方必须先看 `caliber` 再解读数值键 |
| `as_of` | 摘要实际用到的**最后一根资金流日期**（不是文件时间） |
| `fetched_at` | 预计算写入缓存的时间 |
| `rows` | 本次摘要用到的行数（`--as-of` 时可能小于缓存总行数） |
| `pub_lag_days` | 恒为 `1`：当日资金流收盘后发布；`--as-of d` 只使用 `date < d` 的行 |
| `main_net_5d` | 最近 5 个交易日**主力**净流入之和，**单位元**（仅东财口径；新浪口径为 `null`） |
| `main_net_20d` | 最近 20 个交易日主力净流入之和；仅东财口径，不足 20 行时为 `null` |
| `net_5d` | 最近 5 个交易日**总**净流入之和，单位元（仅新浪口径；东财口径为 `null`） |
| `net_20d` | 最近 20 个交易日总净流入之和；仅新浪口径，不足 20 行时为 `null` |
| `direction` | `inflow` / `outflow` / `flat`（由当前口径可用窗口的净额符号判定） |
| `direction_basis` | `main_net_5d_sign` / `main_net_20d_sign` / `net_5d_sign` / `net_20d_sign` |
| `stale` | 缓存末日早于「期望最新交易日」；判定只读本地磁盘交易日历，缺失时退回周末推算 |
| `expected_date` / `expected_source` | 期望交易日及其口径（`calendar_cache` / `estimate`） |
| `reason` | 不可用原因：`cache_missing` / `cache_invalid` / `no_rows_as_of` / `fund_not_supported` / `data_source_error` |

> 基金（含 ETF）没有个股资金流（上游 §4.5 不覆盖），固定返回
> `available=false, reason="fund_not_supported"`，且**不追加 warning**，避免每次基金运行刷屏。
> 股票缓存缺失/过期会在 `diagnostics.warnings` 给出一条可操作的提示，但不改变任何决策字段。

### evidence.valuation（P1-1，issue #46）

估值历史**上下文**，`schema_version` 仍为 `1.0`（additive）。
**CLI / 9:15 主路径绝不触网，`source/` 也不 import baostock** —— 只读预计算缓存：

1. 可选依赖（**不进主 `source/requirements.txt`**）：
   ```bash
   pip install -r requirements-research.txt      # baostock==0.9.4，仅研究/预计算
   ```
2. 收盘后运行预计算（登录一次、逐票串行、失败重试后继续）：
   ```bash
   python tools/precompute_valuation.py 600519 002415
   # 默认 14 只自选；--start 2016-01-04 --end 2026-11-12；--json 输出机读统计
   ```
   写入 `source/stock_analysis/data/valuation/<code>.json`（`data/` 已在 `.gitignore`）。
   baostock 的登录/查询日志会被重定向，`--json` 的 stdout 不被 `login success` 污染。
3. CLI 只读这份缓存。缓存缺失/无效/过期时 `valuation.available=false` + warning，
   CLI 正常返回，**不会为了估值多发一个请求**。

取数口径为上游 a-stock-data v3.10.4 §6.5（`adjustflag="3"`，不复权）：
`peTTM` / `pbMRQ` / `psTTM` / `pcfNcfTTM` + 换手率 + 停牌 + ST。baostock 不支持北交所，
预计算会在登录前拒绝 4/8/92/920 号段并继续下一票。**与本轮东财/同花顺是否可达无关**：
估值来源是 baostock，不经过东财 push2/datacenter。

**只展示、不改决策**：`evidence.valuation` 不参与 `action`、`status`、
`position_fraction`、`entry_zone`、`target_position` 的任何计算；A/B 对照中
`recommendation` 逐键一致。前向 PE / PEG / 一致预期**本任务不做**（东财/同花顺当前不可达），
相关字段不出现、也不猜测。

| 键 | 含义 |
|---|---|
| `available` | 缓存可用且至少有一行不晚于 `as_of`；不可用时其余数值键为 `null` |
| `code` | 6 位纯数字代码 |
| `source` | 恒为 `baostock_valuation` |
| `as_of` | 摘要实际用到的**最后一根估值日期**（不是文件时间/运行日） |
| `fetched_at` | 预计算写入缓存的时间 |
| `rows` | 本次摘要用到的行数（`--as-of` 时只统计不晚于该日的行） |
| `pub_lag_days` | 恒为 `0`：估值基于当日收盘价，`--as-of d` 使用 `date <= d` 的行 |
| `pe_ttm` / `pb` / `ps` | 截至 `as_of` 的最新值；缺失为 `null`。亏损股 PE 可为负，**不参与排序** |
| `pe_percentile_3y` / `pb_percentile_3y` / `ps_percentile_3y` | 0–100 分位；见下方定义 |
| `*_percentile_reason` | 分位为 `null` 的原因：`missing` / `non_positive` / `insufficient_samples` |
| `percentile_window_days` | 窗口长度，恒为 `756`（≈3 年交易日） |
| `percentile_samples` | `{pe, pb, ps}` 各自实际进入分位的有效样本数 |
| `turnover_latest` | 最新换手率，**百分数**（0.31 = 0.31%），与上游一致 |
| `is_st_hist` | 截至 `as_of` 当日的**历史 ST 标记**（baostock `isST`）；回答「当时是不是 ST」 |
| `stale` | 缓存末日早于「期望最新交易日」；判定只读本地磁盘交易日历，缺失时退回周末推算 |
| `expected_date` / `expected_source` | 期望交易日及其口径（`calendar_cache` / `estimate`） |
| `reason` | 不可用原因：`cache_missing` / `cache_invalid` / `no_rows_as_of` / `fund_not_supported` / `data_source_error` |

**3 年分位定义**（写死，README/测试/实现共用）：

```
100 × (窗口内严格小于当前值的样本数) / (窗口样本数 − 1)
```

窗口 = 截至 `as_of` 的最近 **756 个交易日**；**停牌日**（`tradestatus=0`）与
**指标非正**的行不进入样本（亏损股的 PE 不可排序）。当前值缺失/非正，或有效样本
少于 **250** 时，该分位为 `null` 并在 `*_percentile_reason` 写明原因 —— 不拿几十行
冒充「3 年」。并列值不计入「严格小于」，因此窗口最大值恒为 100、最小值恒为 0。

> 基金（含 ETF）固定返回 `available=false, reason="fund_not_supported"`，
> 且**不追加 warning**（上游 §6.5 只覆盖沪深个股）。股票缓存缺失/过期会给出
> 一条可操作提示，但不改变任何决策字段。

### evidence.market_watch（T6，issue #1 / R11 缺口表 #6）

交易所**重点监控池**，`schema_version` 仍为 `1.0`（additive）。
上游 a-stock-data v3.10.4 §8.4 `em_stock_monitor()`：东财 App「重点监控」名单
（被交易所风险警示 / 重点监控的标的 + 生效时间窗）。**零鉴权静态 JSON、全量返回、
不分页**，因此**一次请求服务全池** —— 不是逐票请求。

```
GET https://mobappconfig.securities.eastmoney.com/emcfg/stock_monitor.json
Referer: https://vipmoney.eastmoney.com/
```

上游字段（逐字，不改名）：`STKCODE` / `STKNAME` / `MARKET` /
`VALIDATESTARTDATE` / `VALIDATEENDDATE` / `LINK_URL`。
**`MARKET` 是三值且含字母**：`"1"`=沪、`"0"`=深、**`"B"`=北交所**。
写成 `"SH" if MARKET=="1" else "SZ"` 会把北交所整片错标成深市；未知取值不猜，
原样带出 `?<原值>`。

| 键 | 含义 |
|---|---|
| `available` | 当日请求成功，或成功回退到历史缓存；`false` 表示无缓存且取数失败 |
| `stale` | `true` = 本次用的是**旧缓存**（当日请求失败后回退），`as_of` 是那份缓存的日期 |
| `as_of` | 这份名单对应的**北京日期**（不是文件时间，也不是运行时刻） |
| `hit` | 命中：`{listed: true, start, end, reason}`；未命中 / 不可用 / 过期无命中时为 `null` |

`reason` 是**本模块派生**的封闭两值（上游只给名单，不给原因）：`risk_warning`
（名称带 `ST` / `*ST` 前缀）或 `monitor`（其余）。它只影响文案归类，不影响
「是否命中」这一事实（命中只看代码）。`hit` 只暴露这四个键 —— 名称与东财链接
**不进入 CLI JSON**。

**行为约束**：

1. **一次请求**：同一北京自然日 + 同一状态目录，最多 1 次 HTTP；第二次起读缓存（0 请求）。
   盘前主路径超时上限 **3s**。
2. **当日有效**：缓存 `source/stock_analysis/data/state/market_watch.json`（`data/` 已 gitignore），
   记录 `fetched_at` 与原始条目。
3. **失败 ≠ 无监控**：请求/解析失败 → 回退上一份缓存并标 `stale=true`；**无可用缓存**才
   `available=false`。两种情况都在 `diagnostics.warnings` 给原因。
4. **空结果与接口坏了分开**：接口返回 `[]` 是**唯一合法**的「全市场 0 条」；
   `null`（HTTP 200 但 body 为空）/ 非列表 / 非 2xx / 非 JSON / 行字段不合法
   一律抛 `MarketWatchError`，绝不返回空 dict。
   **本地缓存损坏 ≠ 接口请求失败**：JSON 坏 / schema 不符 / `entries` 非列表 /
   单条字段不合法 / `count` 与条数不符都视为「没有缓存」，忽略坏缓存并**重新请求
   一次**，warnings 里单独写明「缓存损坏」而不是「接口请求失败」。
5. **`--as-of` 回放不给命中**：监控池只有当前快照、没有历史版本，把今天的名单套到
   过去某天等于引入前视。回放时 `hit` 恒为 `null`，若当前确实命中会在 warnings 里
   显式说明「已抑制」，避免被误读成「那天没被监控」。
6. **只展示、不改决策**：`evidence.market_watch` 不参与 `action`、`confidence`、
   `status`、`position_fraction`、`entry_zone`、`target_position` 的任何计算；
   A/B 对照中 `recommendation` 逐键一致。

建议渲染层（`stock_analysis/advice_render.py`）在命中时输出一行

```
🚫 交易所重点监控（2026-11-13~2026-11-26）
```

位置固定在**可执行性闸门之后、价格/建议行之前**；未命中 / `available=false` /
无命中时过期，整行消失（不把「没取到」写成「没被监控」）。
**`stale=true` 不抑制该行**：命中是风险提示，宁可多报；「名单截至哪天」看
`as_of` 与 warnings，行内日期仍是该条目的生效窗口。
离线开关：`ASTOCK_MARKET_WATCH_OFFLINE=1`（测试默认开启）；
状态目录覆盖：`MARKET_WATCH_STATE_DIR`。

### evidence.events（T7，issue #1 / R11 缺口表 #7–#10）

四类**事件风险**：限售解禁 / 股东增减持 / 业绩预告 / 股权质押。上游
a-stock-data v3.10.4 §3.6、§14.1、§14.3、§14.5（东财 `datacenter-web`）。
`schema_version` 仍为 `1.0`（additive）；**不参与** `action` / `confidence` /
仓位 / 触发器。

**只在收盘后预计算，盘前零联网**：

```bash
# 18:30–18:50 时段；默认 14 只自选，10 只 × 4 端点 + 4 探针 = 44 请求，串行 ≥43s
python tools/precompute_events.py
python tools/precompute_events.py 600000,000001 --json
python tools/precompute_events.py --estimate        # 不触网，只算串行耗时
python tools/precompute_events.py --probes-only     # 4 个对照探针，~4s，不写缓存

# CLI 主路径只读如下缓存（data/ 已 gitignore），不发起任何请求
source/stock_analysis/data/state/events/events.json
```

```
evidence.events = {
  available, stale, as_of, fetched_at, reason, code,
  lockup:        {available, stale, as_of, fetched_at, reason, error, warnings[], items[]},
  holder_trades: {...}, forecast: {...}, pledge: {...}
}
```

- 每个子项**独立**成功/失败：任一子端点坏掉只影响该子项，其余三项与主建议
  不受影响；失败时保留上一轮数据并标 `stale=true`，没有旧数据则
  `available=false`。
- **行级归属校验**：返回行必须同时匹配 `SECURITY_CODE` 与日期窗口（解禁
  `FREE_DATE`、增减持/预告 `NOTICE_DATE`、质押 `TRADE_DATE<=as_of`）；
  他票行 / 窗口外行 / 重复行一律丢弃并写入 `warnings[]`，不会张冠李戴入库。
- **「无事件」与「接口坏了」分开**：带个股条件的 0 行且**对照探针正常**
  = 确实没有；请求失败 / `code≠0` / 结构改变 / 非 JSON / **探针 0 行**
  = 坏了。每轮 4 个探针（每报表 1 次，与票池大小无关）；探针失败时该子项
  对全票池标 `interface_suspected`。另有零成本兜底：全票池皆空且上一轮有
  数据 → `empty_pool_suspected`。渲染层对后者输出
  `⚠️ 事件数据过期：…（接口疑似异常，本轮空结果不采信）`，**绝不**写成「无事件」。
- **过期判定只用官方交易日历**（只读磁盘日历），不是「缓存文件是否当天
  生成」；日历缺失时跳过日期判定，长假后首日不误报过期。缓存从未生成时
  写「事件数据未生成（预计算未运行）」，不写「过期（未知）」。
- 北交所质押 `reason=not_supported`，渲染「ℹ️ 股权质押：未覆盖」，不冒充
  「无事件」；未知 `PREDICT_TYPE` 保留事件并告警，不静默映射。
- **只报 20 日内 + 重大额度**，每类至多一行：解禁 ≥1.0% 或 ≥500 万股；
  减持合计 ≥0.5% 或 ≥500 万股；预告类型黑名单（首亏/续亏/预减/增亏/略减）
  或同比上限 ≤-20%；质押 ≥20%。阈值常量见
  `advice_render.EVENT_THRESHOLDS`，理由见 `docs/EVENT_RISK.md`。
- 基金：`available=false, reason=fund_not_supported`，不告警、不渲染。
  `--as-of` 回放：`reason=replay_not_supported`（事件只有当前快照，套到
  过去等于前视）。
- 状态目录覆盖：`EVENTS_STATE_DIR`。调度 cron 示例见
  `docs/EVENT_RISK.md` §7。

### 前向台账（P1，issue #56）：每日记录 + 20 交易日结算

回放历史得不出资金流方向与置信度校准的结论（R3 §8.2：资金流只有 79 个决策日、
置信度校准回放成本约 8s/票/日）。唯一出路是**从今天起把建议冻住**，等 20 个
交易日后用真实行情结算。台账由 `tools/forward_log.py` 驱动，逻辑在
`source/stock_analysis/forward_ledger.py`：

```bash
python tools/forward_log.py record                 # 收盘后跑；默认 14 只自选
python tools/forward_log.py record --codes 002415,601066 --ledger-only
python tools/forward_log.py evaluate               # 结算已到期记录（每日跑）
python tools/forward_log.py report                 # 汇总；--json 输出机读
```

写入 `source/stock_analysis/data/forward/<YYYY-MM-DD>.json`（`data/` 已在
`.gitignore`）。**同一天重复跑覆盖同 code，不追加**；按 CLI 返回的
`data.end`（最后一根行情日）分文件，因此周末/节假日跑会落在上一个交易日。

每条记录的键集合固定，取不到就写 `null` 并在 `missing` 里记原因
（`cache_missing` / `cli_failed` / `field_absent_in_cli_output` 等），
**不省略键、不猜**：

| 组 | 键 |
|---|---|
| 决策 | `as_of` `code` `action` `status` `confidence` `position_fraction` `entry_zone` `stop_loss` `take_profit` `horizon_days` `target_position` |
| 信号 | `signal.position_state` |
| 资金面 | `flow.{available,source,caliber,net_5d,net_20d,as_of,main_net_5d,main_net_20d}` |
| 估值 | `valuation.{available,pe_percentile_3y,pb_percentile_3y,ps_percentile_3y,as_of}` |
| 市况 | `market_regime.{label,confidence}` / `index_regime.{label,confidence}` |
| 运行 | `cli_exit` `elapsed_s` `recorded_at` `missing` `instrument_type` `price_source` `price_caliber` `instrument_basis` `basis_uncertain` |
| 结算 | `settled.{ret_20d,hit,settle_date,entry_date,entry_open,exit_open,horizon_days,price_source}` 或 `null` + `settle_reason`，外加 `settle_status` |

结算口径：`open_{d+1+20} / open_{d+1} − 1`。**个股与场内 ETF** 用前复权开盘价
（`qfq_open_to_open`，分红送转在比值里自然抵消）；**场外基金**只有单位净值，
`open` 由净值补齐，因此是**净值对净值**（`nav_to_nav`，OTC 无开盘价）。
「交易日」以该票自己的行情索引为准（停牌日无开盘价，不能成交）。
`buy` 记 `ret > 0`、`sell` 记 `ret < 0`；`hold` / `avoid` / `insufficient_data`
没有方向，**不参与命中率与校准**。价格取不到时 `settled=null` + `settle_reason`，
**不缩短持有期、不猜**。

价源按**记录里写下的** `price_source` 走，结算时**绝不换源**（PR #96 复验 P1-1）：

| 标的 | 数据文件 | `price_source` | `price_caliber` |
|---|---|---|---|
| 个股 | `stock_*_qfq.csv` | `local_qfq_cache` | `qfq_open_to_open` |
| 场内 ETF / LOF | `stock_*_qfq.csv` | `local_qfq_cache` | `qfq_open_to_open` |
| 场外基金 | `fund_*.csv`（单位净值） | `fund_nav` | `nav_to_nav` |

`instrument_type` 在**记录时**判定，依据是数据文件类型 + 代码段静态信息
（判定依据写进 `instrument_basis`），**不取 CLI 的 `request.type`** —— 那是取数
通道，`auto` 失败会回落到另一侧，按它结算会把个股/ETF 永久钉在错误口径上。
**口径记录时确定、结算时不再更改**：`instrument_basis` 的固定措辞就是
「口径已确认：记录时确定，结算时不再更改」。记录里指定的文件缺失、损坏、为空
或没有 `open` 列时，直接 `no_price_data`（`failed`），**不会滑到另一个口径的缓存**。

#### 口径未确认与一次重判（issue #99）

记录当日**没有任何可用缓存**（`stock_*_qfq.csv` 与 `fund_*.csv` 都不存在）、
`fund_*.csv` 读不出、或只有未复权交易所 OHLC 因而无法确定 qfq 口径时，口径
只能按代码段静态兜底。这类记录写 `basis_uncertain=true`，并在 `instrument_basis`
里写明「口径未确认：首次结算成功前允许一次重判」。

重判规则（`settle_all()` 执行，返回的 `rejudged` 列出本次重判的 code）：

- **只在首次结算成功前**发生（`settled` 仍为 `null`），且**只允许
  uncertain → 确认**：缓存出现且能确定口径时，四个口径字段改成确认值、
  `basis_uncertain` 置 `false`，`instrument_basis` 前缀「首次结算前重判：」；
- 仍无法确认（缓存仍是未复权 OHLC）→ 记录原样保留，下次结算再试；
- **已确认记录永不重判**（`basis_uncertain=false`），**已结算记录永不重判**
  （`settled` 非 null），引入该字段之前的老记录（缺键）同样不重判 ——
  反向改动已确认口径正是这条规则要防的事。

例：记录当日无缓存（按代码段判 stock/qfq，`basis_uncertain=true`）→ 之后
`fund_*.csv` 净值到位 → 首次 `evaluate` 重判为 `fund` / `fund_nav` /
`nav_to_nav` 并正常结算，`ret_20d` 来自净值序列。

老记录没有 `price_source` 时按 brief 默认 **stock/qfq**；只有当 `fund_*.csv`
存在、内容确认为**单位净值**（`open == close`、无交易所 `amount`/`postVol` 列）
且没有 `stock_*_qfq.csv` 时才判场外基金；判定依据由 `settle_all()` 返回的
`basis`（`{code: 依据}`）与 `warnings` 给出。

`settle_status` 把「没到期 / 数据未到 / 从未结算」和「真失败」分开：

| `settle_status` | 触发条件 | 是否计入失败率 |
|---|---|---|
| `settled` | 已算出收益 | 否 |
| `not_matured` | `not_matured`（还没攒够 20 根） | 否 |
| `pending` | `no_bars_after_as_of`（as_of 之后还没有行情，通常是缓存未更新）；或**从未跑过 `evaluate`** | 否 |
| `failed` | `no_price_data` / `open_missing` / `price_parse_error` | 是 |

`report` 输出里 `pending` 与 `failed` 分开计数（`settle_status` 逐态计数，
`pending` / `failed` 两个顶层键）。老台账没有 `settle_status` 时按 `settled` /
`settle_reason` 反推：两者都为空（从未结算）归 `pending`，**不再算成 `settled`**。
`evaluate` 打印时 pending 与 failed 的原因也分开列出，不会出现「失败 0 条」后面
挂一串 pending 原因。

`report` 给出：已到期条数、方向命中率、按 `confidence` 分箱的校准（含 Brier）、
按 `action` / `price.caliber` / `price.source` / `flow.caliber` / `flow.available` /
`valuation.pb` 分位的分层命中率，以及口径未确认（`basis_uncertain`）的条数 ——
供 `docs/P1_EVIDENCE_PLAN.md` §3.6.3 的 C 路径（Brier 改善 ≥0.02，需 ≥200 条已到期记录）使用。
`confidence` 在这里的操作性定义是「该方向判断正确的概率」。

> 旧 `evidence/forward_log.csv` / `forward_eval.csv` 仍照常写（同一份 CLI 输出派生，
> 不重复跑 CLI）；JSON 台账是 issue #56 的正式口径。

### data.execution（v2.0 新增字段）

`{signal_bar, fill, slippage_bps, commission_bps}` —— 本次结论对应的成交假设。
`fill` 当前为 `next_open`（信号次日开盘成交，可执行）；旧版是次日收盘成交，晚一整天。

### 错误响应与成功响应同构（C3）

`status ∈ {insufficient_data, data_source_error}` 时，响应的**键集合与成功响应一致**，
只是不适用的字段为显式 `null` —— 调用方**不需要按 status 分支写防御性解析**：

- `evidence.metrics` 仍有 9 项，每项 `{"point": null, "ci_95": null}`
- `evidence.n_trials = 0`、`significant = false`、`consensus.family = null`、`triggers = []`
- `evidence.edge` 七键齐全（值全 `null`）、`evidence.signal_stats` 展开到 5 个 horizon 子键（值 `null`）、
  `evidence.decision_replay` 七键齐全、`recommendation.signal` 十三键齐全、`data.execution` 四键齐全
- `evidence.flow` 十八键齐全（`available=false, reason="data_source_error"`，数值键全 `null`）
- `evidence.valuation` 二十四键齐全（`available=false, reason="data_source_error"`，数值键全 `null`；
  `pub_lag_days=0`，`percentile_samples={pe: null, pb: null, ps: null}`）
- `evidence.market_watch` 四键齐全（`available=false, stale=false, as_of=null, hit=null`；
  取数阶段失败时**不读缓存、不触网**）
- `recommendation.horizon_days = [null, null]`、`stop_loss/take_profit/stop_method = null`、`position_fraction = 0.0`
- `data.bars/start/end/last_close` 在"数据太短"时是**已知真实值**（如 `bars=23`），与 `invalidation` 文本一致；拉取彻底失败时才为 `0 / "" / null`
- `candidates = []`（没有候选才是正确语义，不是缺字段）

> 门禁 **G35** 逐路径比对成功响应与 `insufficient_data` 响应的结构（排除空列表的元素路径），
> 任一字段整块缺失即翻红。这条门禁是为了防止"契约写着显式 null、实现整块缺失"再次发生。

#### `data.window_note`：`--start` 时必须给出截取说明

`window_note` 的口径（门禁 **G37** 锁定三档）：

| 场景 | 取值 |
|---|---|
| 不带 `--start` | `null`（没有截取，不编造说明） |
| `--start` + 成功 | `"--start 20250601 截取后 334 根"` |
| `--start` + 数据太短 | `"--start 20260908 截取后 23 根"`（与 `data.bars` 一致） |

> 这条曾是漏洞：`create_error_response` 把它**硬编码成 None**，即使带 `--start` 也不说明窗口。
> G35 只比对"键是否存在"，值被写死时查不出来 —— 所以另加 **G37** 专查这个值，
> 并要求 `window_note` 里的根数与 `data.bars` 数值一致。

#### 关于"数组变 null"：键在、值变，不是缺键

一个常见误判：用"递归展开所有 list"的 walker 去比对两条路径，会多出一批
`.../ci_95[0]`、`.../ci_95[1]`、`.../entry_zone[0]` 之类的**元素路径**，
看起来像"成功路径有、错误路径没有"，进而被当成契约违规。

**实际是类型变化，不是缺键**（实测 2026-10-18）：

| 路径 | 成功路径 | 错误路径 | 结论 |
|---|---|---|---|
| `/evidence/metrics/annual_return/ci_95` | `[-0.040951, 0.156577]` | `null` | 键在，值为 null ✓ |
| `/evidence/metrics/sharpe/ci_95` | `[-0.313136, 1.195496]` | `null` | 键在，值为 null ✓ |
| `/evidence/excess_vs_buy_hold/ci_95` | `[-7.5e-04, 7.0e-04]` | `null` | 键在，值为 null ✓ |
| `/recommendation/entry_zone` | `[1317.88, 1340.12]` | `null` | 键在，值为 null ✓ |

判据：**G35 的 walker 只在"列表元素是 dict"时下钻**，float 数组不被展开成路径。
因此 G35 的差集口径是——`成功 134 条 / 错误 116 条 / 缺 18 条，且 18 条全部是
`candidates[]` 与 `evidence.triggers[]` 的元素路径，超出允许范围 **0 条**。

> 调用方要做的判断很简单：**看键在不在，别看数组元素**。
> 若你的校验器把 `ci_95: null` 当成错误，那是校验器过严 —— 契约明确要求"不适用时为显式 null"。

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
  "instrument": { "code": "600519", "name": "贵州茅台", "board": "sh_main", "price_limit_pct": 0.1, "is_st": false, "is_suspended": false, "limit_up": 1542.53, "limit_down": 1262.07, "quote_stale": false, "one_word_limit": null, "status_source": "tencent_quote" },
  "data": { "bars": 1643, "start": "2020-01-02 00:00:00", "end": "2026-10-16 00:00:00", "last_close": 1329.0, "source": "cache", "from_cache": true, "stale": false, "cache_last_date": "2026-10-16", "lag_days": 0, "expected_date": "2026-10-16", "requested_source": "sina", "window_note": null },
  "market_regime": { "label": "Bull", "confidence": 0.7, "as_of": "2026-10-16 00:00:00", "definition": "ma20_ma60_return_volatility", "index": { "label": "Shock", "confidence": 0.5, "as_of": "2026-10-16 00:00:00", "definition": "ma20_ma60_return_volatility", "source": "tencent_index:sh000300", "bars": 1662 } },
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
    "elapsed_seconds": 9.3,
    "source_attempts": []
  },
  "disclaimer": "本结果不构成投资建议，市场有风险，投资需谨慎。"
}
```

> 上表数值取自 2026-10-18 10:34 对 600519 的真实运行（`--fast`，1643 根），
> 为便于阅读删减了 `candidates` 其余 9 条；**完整未删减样例**见
> `source/docs/example_output_600519.json`（同样由命令现跑生成）。

## 建议渲染（`tools/render_advice.py`）

把 `cli.py --json` 的完整响应渲染成可直接投递的每日建议：

```bash
source/.venv/bin/python cli.py 600519 --json --fast \
  | source/.venv/bin/python tools/render_advice.py --compact
```

渲染层是纯函数（不改入参、不读写文件、不触网），字段口径见
`source/stock_analysis/advice_render.py` 的模块 docstring。`--compact` 只压缩
`diagnostics.warnings`，其余行与默认输出逐字节一致。

渲染的上下文行：仓位/持有期/复审日（T1）、可执行性闸门（T2）、regime 冲突（T3）、
估值分位（T4）、资金流方向（T5），以及**交易所重点监控命中（T6）**——
`evidence.market_watch.hit.listed is True` 时输出
`🚫 交易所重点监控（{start}~{end}）`，位置在可执行性闸门之后、价格/建议行之前。
未命中 / `available=false` / 无命中时过期，整行消失；`stale=true` 时**照常输出**
（命中宁可多报，名单时点看 `as_of` 与 warnings）；该行**只陈述事实**，
不改写 `action` / `confidence`。

### `--compact` 契约：封闭词表替换 + 整段删除

issue #92 要求 compact 输出是原文的「删除子序列」。实现需要把少量内部术语换成
用户可读表述，协调者裁定后的口径如下（**封闭集合 + 可验证**）：

1. **删除**只允许三类：URL、堆栈/回溯段、**配对完整**的内部引用括号。括号
   不配对时不删；删除后括号失衡则整条退回原文。不截句、不重排、不删斜杠枚举
   中的单项。
2. **替换**只允许模块级封闭常量
   `stock_analysis.advice_render.COMPACT_GLOSSARY` 里的条目（当前 7 项）：

   | 原文 token | 替换为 | 说明 |
   | --- | --- | --- |
   | `p_positive` | 买侧期望胜率 | 买侧期望 |
   | `trigger.current` | 触发参考价 | 触发价字段 |
   | `is_st` | ST 标记 | ST 状态字段 |
   | `ProxyError` | 代理异常 | 网络代理错误 |
   | `no_edge` | 无统计优势 | 统计判定，不是「没有机会」 |
   | `95% CI` | 置信区间 | no_edge 告警里的统计术语 |
   | `统计上无优势，判 no_edge` | 统计上无优势 | 短语优先匹配，去掉同义重复分句（N4） |

   词表之外的任何内部字段名/术语都不替换，仍只允许整段删除。增删词表项必须
   同步修改 `tests/test_advice_render.py::test_compact_glossary_is_closed_set`
   与本表。
3. **词边界**：token 前后不得粘连字母/数字/下划线/中文或 `.`，例如 `is_st`
   不会命中 `is_status`、`仅is_st推断`、`x.is_st`。
4. **空格**：替换时若标签与相邻字符都是中文，吸收其间的原空格；删除段残留的
   多余空格同样折叠。输出不得出现中文夹空格（`is_st 只能` → `ST 标记只能`）
   或连续两个空格（N1）。
5. **不变量**：不含词表 token 的告警，输出必为原文的删除子序列；含 token 的，
   除被替换 token（及为避免中文夹空格而吸收的 ASCII 空格）外逐字保持原序。
   词表命中只对**原文位置**判定：删除段把两侧文本拼成 token 时（例如
   `is_（…）st` → `is_st`），该拼接产物不触发替换（N2）。
6. **安全红线**：含「停牌/退市/ST/一字 + 无法区分/无法判断/无法成交」的告警，
   压缩前后语义必须一致；`test_compact_preserves_safety_qualifiers` 用参数化
   用例锁住。

> 与 issue #92 字面表述的差异（替换 vs 纯删除）已显式记录在此：只接受上面这个
> 封闭、可验证的词表，不接受任何自由改写；`--json` 仅允许 additive 变更。

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

### 数据源与降级（**多通道自动切换**）

股票日线走**三通道渠道池**，任一成功即返回，全部失败才回退旧缓存：

```
eastmoney (ak.stock_zh_a_hist)  →  sina_stock (ak.stock_zh_a_daily)  →  sina_fund (ak.fund_etf_hist_sina)
```

- 每个通道有**冷却 / 连续失败剔除**策略（避免反复撞已封的节点）。
- `source=` 入参只影响**优先顺序**：`akshare`/`eastmoney` 先试东财；`sina`/`sina_stock` 先试新浪；不给则按池内轮询。
- 实际使用的通道会写进 `data.source`（`eastmoney` / `sina_stock` / `sina_fund` / `cache`）。

> 这条能力是 2026-10-18 修好的：此前 `get_stock_daily` **只调东财单通道**，
> 而 `bars.py` 里的三通道池早已实现却从未被调用。后果是——一旦东财封锁出口 IP，
> 整条取数链就死掉，尽管新浪通道完全可用（实测：东财 TCP 层被拒时，
> `ak.stock_zh_a_daily` 正常返回 1643 根）。门禁 **G36** 现在锁定这个降级能力。

**实测（模拟东财全封，4 只股票）**：

```
600519: 1643 根   688981: 1509 根   002371: 1643 根   300750: 1643 根
OK 预热完成，可离线运行          ← 全部由 sina_stock 接手
```

### 场外基金与 `auto` 判定（**重要**）

基金取数走**两个通道**：

| 通道 | 接口 | 覆盖 |
|---|---|---|
| 场内 ETF | `ak.fund_etf_hist_sina` | 510300 这类场内 ETF（有 OHLCV） |
| 场外 OTC | `ak.fund_open_fund_info_em` | 023298 / 001917 这类场外基金（**只有单位净值**） |

场外基金**没有 OHLCV**，数据层用单位净值补齐：`open=high=low=close=净值`、`volume=0`。
回测基于 close，信号与收益不受影响。

**实测（2026-10-18）**：

```
023298   ETF=0 行   OTC=366 行    尾部 2026-10-16 / 1.335
006195   ETF=0 行   OTC=1919 行   尾部 2026-10-16 / 2.8544
001917   ETF=0 行   OTC=2564 行   尾部 2026-10-16 / 3.2372
001564   ETF=0 行   OTC=2689 行   尾部 2026-10-16 / 3.817
```

> 历史缺口：`get_fund_daily` 曾**只有 ETF 通道**，这 4 只场外基金全部 0 行 →
> `data_source_error`。门禁 **G39** 现在锁定 OTC 通道可用。

#### `auto` 为什么不能只看代码前缀

场外基金代码段与深市股票代码段**重叠**，纯代码规则无法可靠区分。实测
`ak.fund_name_em()` 名录（28104 条）里：

```
002270 在基金名录   ← 但它是股票（华明装备）
002415 在基金名录   ← 但它是股票（海康威视）
001917 在基金名录   ← 它是基金
```

所以 `auto` 改为**双向探测**：先按前缀猜一侧，取不到足量数据再试另一侧，
两侧都失败则明确报错并提示用 `--type`，**绝不猜一个当成事实**。

**建议：对 0/1 段的代码，显式传 `--type stock` 或 `--type fund`**，避免歧义。
`5/15/16/18` 开头的场内基金可以安全依赖 `auto`。

### 跨通道数据一致性（**混源还能不能比**）

多通道带来一个真问题：上一版基线可能是东财拉的，这一版可能落到新浪。
**数字还能直接对比吗？** 不要靠感觉，用工具查：

```bash
python tools/check_source_parity.py 600519              # 缓存 vs 新浪
python tools/check_source_parity.py 600519 --source eastmoney
```

退出码：`0` 一致 / `1` 存在超差（会打印最大差异样本）/ `2` 前置条件不满足（缓存缺失、通道不可用、无重叠）。

**实测结果（2026-10-18，600519，1643 个交易日全量比对）**：

```
open    : 最大绝对差=0.000000  超差点数=0/1643  一致
high    : 最大绝对差=0.000000  超差点数=0/1643  一致
low     : 最大绝对差=0.000000  超差点数=0/1643  一致
close   : 最大绝对差=0.000000  超差点数=0/1643  一致
volume  : 最大绝对差=0.000000  超差点数=0/1643  一致

一致（容差 1e-06）→ 跨通道/跨版本数字可以对比
```

即：**新浪与东财对同一段历史给出的 qfq 完全一致**，换通道不影响历史数值比对。
若将来某次比对返回 `1`，请把差异样本写进 `source/docs/validation_600519.md`
再讨论跨版本对比，不要默认"应该差不多"。

> 注意：`data.source` 只在**当次触网**时反映真实通道；缓存命中时恒为 `cache`
> （缓存文件本身不记录来源）。所以"这份缓存是谁拉的"只能靠上面的工具实测判定。

### 行为约定

- 预热成功后会写 `stock_{code}_qfq.csv` / `fund_{code}.csv`，之后**可完全离线运行**。
- **无缓存 + 所有通道都不可达 → 退出码 30（data_source_error）**，不是 20。
- 有缓存但不足 100 根 → 退出码 20（insufficient_data）。
- 有旧缓存但已过期且全部通道失败 → **降级回退旧缓存**，并在
  `data.stale=true` / `diagnostics.warnings` / `data.lag_days` 里如实暴露（G33 锁定）。
- 拉取失败**不会**写出空壳缓存文件（0 行 CSV）；历史上已存在的空壳会被自动识别并删除后重拉。
- ⚠️ 已知坑：`600519 --type fund` 会从基金通道把**股票 K 线**拉到 `fund_600519.csv`。
  该文件只用于验证 `--type` 是否生效（G16），不参与任何结论计算，可安全删除。

## 依赖与 Python 版本（D3f）

**支持 Python 3.11+**，不限定小版本。`source/requirements.txt` 在 3.11 / 3.12 / 3.13 / 3.14 上均可解析安装。

### 为什么会有版本差异

差异**只来自 numpy 的 wheel 覆盖范围**，与本项目代码无关。`pandas 3.0.x` 的元数据是条件依赖：

```
numpy>=1.26.0 ; python_version <  "3.14"
numpy>=2.3.3 ; python_version >= "3.14"
```

而 `numpy 2.2.x` 没有 cp314 wheel。所以对 numpy 用单一硬 pin 必然在某一侧失败。
`requirements.txt` 用 **environment marker** 分档解决：

```
numpy==2.2.6; python_version < "3.14"
numpy>=2.3.3,<3; python_version >= "3.14"
```

`uv pip compile` 实测：四个版本全部解析成功。**运行验证**的覆盖面如下——

| Python | numpy | pandas | akshare | 验证方式 |
|---|---|---|---|---|
| 3.11 | 2.2.6 | 3.0.1 | 1.18.29 | ✅ **真机跑通**：pytest 47 passed，CLI exit 0/0/30 |
| 3.12 | 2.2.6 | 3.0.1 | 1.18.29 | ✅ **真机跑通**：pytest 47 passed，门禁 37/37 |
| 3.13 | 2.2.6 | 3.0.1 | 1.18.29 | ⚠️ 仅依赖解析验证（本仓库未运行） |
| **3.14** | **2.3.5** | 3.0.1 | 1.18.29 | ✅ **真机跑通**（由下游 Agent 独立验证）：pytest 47 passed、CLI exit 0、预热 5/5、错误路径同构、跨通道一致性全过 |

> **3.14 已被真机验证**：Python 3.14.7 + numpy 2.3.5 环境下 `pip install -r requirements.txt`
> 无解析冲突（marker 生效、自动离开 `numpy==2.2.6` 分支），47 项测试全过，CLI 三条退出码正确。
> 只剩 **3.13 未做运行验证**（本仓库验证机无法安装该解释器：写 `vcruntime140.dll` 被安全策略拒绝）。
> 若你在 3.13 上遇到问题，把 `pytest` 与 `python cli.py 600519 --json --fast` 的原始报错发过来 —— 那会是真 bug。

> **关于 numpy 版本号不一致（正常，不是 bug）**：`>=3.14` 分支写的是 `numpy>=2.3.3,<3` 这个**区间**，
> 解析器会取当时可用的最新版 —— 实测中出现过 `2.3.5` 与 `2.5.4` 两种结果。
> 两者都有 cp314 wheel、都能通过全部门禁，所以**不要求两边版本号相同**。
> 需要复现某一位具体数字时，请用 `pip install numpy==2.3.5` 显式锁定。

### 安装

```bash
cd source
pip install -r requirements.txt
```

> **可选：估值预计算依赖**。`requirements-research.txt` 里的 `baostock==0.9.4` **不属于**
> 主依赖，只有 `tools/precompute_valuation.py`（P1-1 估值历史）需要它；主 CLI 与测试
> 不安装也能完整运行。安装方式：`pip install -r requirements-research.txt`。

装完自查（一条命令确认版本组合）：

```bash
python -c "import sys,akshare,numpy,pandas,pytest;print(sys.version.split()[0],'| akshare',akshare.__version__,'| numpy',numpy.__version__,'| pandas',pandas.__version__,'| pytest',pytest.__version__)"
```

门禁 G30 会对比 `requirements.txt` 的 pin 与实际已安装版本；它按 `pkg==x.y.z` 提取，
marker 后缀不影响解析（在 3.11 与 3.12 上均实测 PASS）。

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
