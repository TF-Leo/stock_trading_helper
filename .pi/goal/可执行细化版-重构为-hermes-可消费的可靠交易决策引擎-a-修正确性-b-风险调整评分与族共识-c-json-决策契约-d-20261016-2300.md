## Goal

把本项目从"面向人眼阅读的回测研究工具"，重构为 **Hermes Agent 可直接消费的、带不确定性量化的交易决策引擎**。

Hermes 会自行 clone 本仓库并自主运行，因此接口必须做到：**仅凭 README 就能被一个没见过本项目的 Agent 正确调用与解析**。主契约为 `python cli.py <code> --json`（stdout 输出单一 JSON 文档），底层由一个可导入函数 `advise()` 产出同一 schema，CLI 仅作薄包装。股票侧与基金侧共用同一契约。目标周期为日线波段（数天~数周）。

工作分四个阶段，**必须按 A → B → C → D 顺序推进，且每阶段结束跑通该阶段门禁后才能进入下一阶段**。理由：A 之前所有回测数字都是失真的，在失真数据上调评分权重等于拟合噪声。

---

## 执行须知（给执行者的硬约束）

### 阶段门禁
- **Phase A（正确性）完成前，禁止修改任何评分公式、权重、阈值**。A 的验收数字必须在 B 的改动引入之前采集。
- **Phase B 完成后，A1/A2 中的具体交易数（29/32/42 等）允许变化**，因为 B2（波动率自适应止损）会改变成交结构。届时以 A13 的 after 快照为准，不再要求等于这些值。但 A1 的"可负担 K 线占比 100%"与 A2 的"信号充足却零成交的组数 = 0"是**结构性不变式，任何阶段都必须持续成立**。
- Phase C 依赖 B 的输出字段；Phase D 的删除动作放在最后，避免中途删掉仍在被引用的代码。

### 新增模块布局（必须遵守，不要另创结构）
```
source/decision/                      # 新增：股票与基金共用的决策层
    __init__.py                       #   导出 advise()
    schema.py                         #   SCHEMA_VERSION、dataclass、枚举、退出码、to_dict()
    sizing.py                         #   仓位/止损/止盈/入场区/horizon 推导
    scoring.py                        #   风险调整评分、bootstrap CI、Deflated Sharpe
    consensus.py                      #   策略族去相关、Top-K 共识、分歧度、稳定性
    triggers.py                       #   策略元信息 -> 触发条件与价位（取代 45 个手写分支）
    advise_stock.py                   #   advise_stock(code, ...) -> dict
    advise_fund.py                    #   advise_fund(code, ...) -> dict
source/fund_analysis/fund_strategy/registry.py   # 新增：基金参数网格单一来源
```

### 禁止事项（执行层面）
- 禁止把期望值硬编码进测试来"让测试变绿"；禁止用 `pytest.mark.skip` 绕过未实现的验收项。
- 禁止修改 `source/tests/` 中现有断言，**唯一例外**见 A13/D2 明确点名的那一条。
- 禁止引入 scikit-learn / statsmodels / scipy / torch 等新依赖；bootstrap、置信区间、Deflated Sharpe 全部用 `numpy` 自实现。
- 禁止改变策略网格组数（保持 581），因为 `source/tests/test_p1_oos.py::test_registry_single_source` 断言 `len(a) == 581`。若确需变更，必须同步改该断言 + README 中所有"581"字样，并在最终报告说明理由。
- 禁止在测试中调用 `get_stock_daily` / `fetch_fund_data` / `get_stock_name` 等任何触网函数（它们会发起 HTTP 请求）。测试一律用合成 DataFrame。

### 关键现状事实（已核实，执行时直接采用，不必重新调研）
- 策略族 = `source/stock_analysis/strategy/registry.py::_grid_defs()` 返回的 4 元组首元素 `prefix`。**B5 的族分组必须取自这里，禁止用字符串按 `(` 切分重新推断**（`optimizer.py::_family_of` 那种做法与 `VWAP(累计)`、`策略组合(2票)` 等命名不兼容）。
- `build_strategy_signals()` 返回 `{namer(kw): inst.generate_trade_signals}`，581 键无冲突。
- 行情缓存目录：`source/stock_analysis/data/`（`bars.py::DATA_DIR`），文件名 `stock_{symbol}_{adjust}.csv`。基金缓存：`source/fund_analysis/data/`。
- 现有测试约定：`source/tests/test_p1_oos.py::_toy_df(n, seed)` 用 `np.random.default_rng(seed)` 生成合成日线。**新测试必须沿用这一风格**，并在 `source/tests/conftest.py` 中提供共享 fixture（`toy_df`、`high_price_df`、`flat_df`、`trend_df`）。
- 板块前缀映射参考 `bars.py::_to_exchange_symbol`：`6/5/9→sh`，`0/3/1/2→sz`，`4/8→bj`。
- 基金参数网格目前**硬编码为 `cli.py::run_fund_tournament` 第 1562–1607 行的内联 for 循环**（RSI 4×4、双均线 3×3、MACD 2×2×2、波动率目标 3×2、指数轮动 2×2），无注册表。基金策略类为 `fund_analysis/fund_strategy/p2_strategies.py` 的 `FundMaStrategy` / `FundMacdStrategy` / `FundVolTargetStrategy` / `FundIndexRotationStrategy`，四者无共同基类、无 `name` 属性。
- `cli.py` 当前 1689 行，函数边界：`detect_type` 33、`_build_stock_strategies` 52、`_print_detailed_stock_advice` 58–704、`_build_tomorrow_plan` 705–1263、`run_stock_tournament` 1264–1317、`_print_fund_current_advice` 1319–1549、`run_fund_tournament` 1550–1646、`main` 1647–1689。
- git：分支 `main`，HEAD `e183cb6`，工作树干净。

---

## 输出契约规格（Phase C 的验收基准，字段名必须逐字一致）

### 退出码（`cli.py` 必须以 `sys.exit(code)` 显式返回）
| code | status | 含义 |
|---|---|---|
| 0 | `ok` | 产出可执行建议 |
| 10 | `no_edge` | 无统计显著优势，建议观望 |
| 20 | `insufficient_data` | 数据条数不足（< 100 根） |
| 30 | `data_source_error` | 渠道池全失败且无可用缓存 |
| 40 | `untradeable` | 诊断显示无法正常成交，结果不可信 |
| 2 | — | 参数/用法错误（argparse 默认，勿改） |

### 枚举
- `status` ∈ `ok` / `no_edge` / `insufficient_data` / `data_source_error` / `untradeable`
- `action` ∈ `buy` / `sell` / `hold` / `avoid` / `insufficient_data`
- `trigger.state` ∈ `buy_triggered` / `near_buy` / `neutral` / `near_sell` / `sell_triggered`

### JSON 结构（`schema_version` 固定为 `"1.0"`）
```
schema_version, generated_at, request{}, instrument{}, data{}, market_regime{},
recommendation{}, evidence{}, candidates[], diagnostics{}, disclaimer
```

逐字段：
- `request`: `code, type, start, opt, jobs, fast`（回显入参，便于 Agent 自证）
- `instrument`: `code, name, board, price_limit_pct, is_st`；`board` ∈ `sh_main/sh_star/sz_main/sz_gem/bj/fund/unknown`
- `data`: `bars, start, end, last_close, source, from_cache, window_note`（`window_note` 用于 A7 声明实际窗口）
- `market_regime`: `label, confidence, as_of, definition`（`definition` 为字符串标识所用规则版本，满足 A8 单一来源）
- `recommendation`: `status, action, action_reason[], confidence, position_fraction, horizon_days[2], entry_zone[2]|null, stop_loss|null, take_profit|null, distance_to_trigger{buy_pct,sell_pct}, invalidation, no_edge`
- `evidence`: `consensus{family, agreement, members[]}`, `dispersion`, `stability{windows_tested, action_stable, family_stable, top_k_overlap_min}`, `n_trials`, `n_survivors`, `deflated_sharpe`, `significant`, `metrics{}`, `excess_vs_buy_hold{point, ci_95[2], significant}`, `buy_hold_baseline{total_return, annual_return}`, `regime_win_rates{}`, `triggers[]`
- `evidence.metrics` 每项形如 `{"point": float, "ci_95": [float,float]|null}`，必须覆盖：`annual_return, max_drawdown, win_rate, sharpe, calmar, profit_factor`；另有标量 `trade_count, avg_holding_days, time_in_market, max_consecutive_losses`
- `evidence.triggers[]` 每项：`{"indicator", "current", "buy_trigger", "sell_trigger", "state", "buy_price|null", "sell_price|null"}`
- `candidates[]`（Top-K，K 默认 10）每项：`name, family, score, score_is, score_oos, annual_return, max_drawdown, win_rate, trade_count, calmar, deflated_sharpe`
- `diagnostics`: `unaffordable_lot_events, blocked_by_limit_events, zero_trade_configs, survivor_configs, warnings[], elapsed_seconds`
- `disclaimer`: 固定字符串，含"不构成投资建议"

### 派生规则（必须按此实现，避免执行者自由发挥）
- `stop_loss` = `entry_ref − k×ATR(14)`，`k=2.5`；`take_profit` = `entry_ref + m×ATR(14)`，`m=4.0`。`entry_ref` 为 `last_close`（`action=buy/hold`）或触发价。计算后 clamp 使 `stop_distance=(entry_ref−stop_loss)/entry_ref ∈ [0.03, 0.20]`。
- `position_fraction` = `clamp( (risk_per_trade / stop_distance) × confidence , 0, max_position_fraction )`，`risk_per_trade=0.01`，`max_position_fraction=0.30`。**只输出比例，绝不输出金额或股数，不接收任何账户资金入参。** `action ∈ {avoid, insufficient_data}` 时固定为 `0.0`。
- `confidence` = `clamp01( 0.4×clamp01(deflated_sharpe) + 0.3×consensus.agreement + 0.2×stability_score + 0.1×clamp01(trade_count/60) )`
- `horizon_days` = `[max(2, floor(0.5×avg_holding_days)), min(60, ceil(1.5×avg_holding_days))]`
- `entry_zone` = `[round(max(buy_price, last_close − 0.5×ATR),2), round(last_close + 0.5×ATR,2)]`；`action=sell` 时为 `null`
- `distance_to_trigger.buy_pct` = `(buy_price − last_close)/last_close × 100`（`buy_price` 为 null 时该值为 null），`sell_pct` 同理
- `trigger.state` 判定：`current` 已越过买入阈值 → `buy_triggered`；距阈值 ≤ 5%（按该指标量程归一）→ `near_buy`；卖出侧对称；否则 `neutral`
- status→action 映射：`insufficient_data`/`data_source_error` → `action=insufficient_data`；`untradeable` → `action=avoid`；`no_edge` → `action=avoid`；`ok` → 由 `triggers[].state` 与 `consensus` 方向决定 `buy/sell/hold`

---

## Acceptance criteria

### Phase A — 正确性：让数字重新可信

- [ ] **A1 资金/整手与绝对股价解耦**。修改 `source/stock_analysis/backtest/analyzer.py::run_strategy_tournament`，使其传给 `compare_strategies` 与最终 `best_result` 的引擎都使用**与标的价格自适应的资金基准**（现有 `adaptive_oos_config()` 仅作用于 `engine_oos`，这是本 bug 的直接成因）。同时修改 `source/stock_analysis/backtest/optimizer.py::bayes_search` 第 106 行 `best_result = eng_is.run_backtest(...)`，令选优与报告共用同一基准（满足 A12）。
  **验证（Phase A 结束时执行）**：对 600519 全样本，`close×100 ≤ initial_capital×max_position_pct` 的 K 线占比 = **100%**（当前 66/1643 = 4%）；`唐奇安(20/10)`、`MACD(12-26-9)`、`均线趋势(5-20-60)` 交易数分别 = **29 / 32 / 42**。
- [ ] **A2 无"信号充足却零成交"**。对全部 581 组：凡 `buy` 信号数 ≥ 20 者，成交笔数为 0 的组数 = **0**。
  **验证**：新增离线测试 `source/tests/test_a_affordability.py`，用 `high_price_df` fixture（合成 1500 元/股、400 根、含明确金叉的均线数据）断言：(a) 可负担占比 100%；(b) buy 信号 ≥ 20 的策略成交数 > 0。
- [ ] **A3 失败变响**。`BacktestResult` 增加计数字段 `unaffordable_lot_events`、`blocked_by_limit_events`，在 `engine.py::_calc_max_shares` 返回 0 时与涨跌停拦截时累加，并透出到 JSON `diagnostics`。当 `unaffordable_lot_events > 0` 时：文本输出打印 `[WARN]` 行，`status=untradeable`，退出码 **40**。
  **验证**：构造 `BacktestConfig(initial_capital=1000, max_position_pct=1.0)` 跑任意合成高价数据，断言 `status=="untradeable"` 且 `diagnostics.unaffordable_lot_events > 0`；CLI 层断言退出码 40。
- [ ] **A4 暴露时长**。`BacktestResult` 增加 `time_in_market`（持仓 bar 数 / 总 bar 数），在 `analyzer.py::_calculate_metrics` 中计算（需由 `engine.run_backtest` 传入逐 bar 持仓序列）。JSON `evidence.metrics.time_in_market` 必填。
  **验证**：合成一段"前 100 根持仓、后 300 根空仓"的数据，断言 `time_in_market ≈ 0.25`（容差 ±0.02）。
- [ ] **A5 基金 T+1**。`source/fund_analysis/fund_backtest.py::_fund_backtest_signals` 第 97–98 行必须对 `buy_sig`/`sell_sig` 施加 `shift(1)`，与股票引擎 `engine.py` 第 102–103 行规则一致。
  **验证**：新增测试，构造仅最后一根 K 线 `buy=True` 的信号，断言 `trade_count == 0`（当日不得成交）。
- [ ] **A6 基金单一成本模型**。删除 `source/fund_analysis/fund_strategy/runner.py::backtest_strategy` 的零费率成交路径，令其与 `optimize_parameters` 统一走含 `FUND_FEE_RATE=0.0015` 的实现。基金侧最终只允许存在一个成交模拟函数。
  **验证**：`grep -rn "position = cash / price" source/` 无结果；`grep -rn "FUND_FEE_RATE" source/` 命中所有基金成交路径；新增测试断言同一信号下含费结果的 `total_return` 严格小于零费结果。
- [ ] **A7 尊重调用窗口**。移除 `fund_backtest.py` 第 24、93 行与 `fund_strategy/runner.py` 第 21 行的无条件 `pd.DateOffset(years=3)` 截断，改为使用调用方传入的 `start`。若因数据长度必须截断，写入 `data.window_note`。
  **验证**：新增测试，传入 5 年合成基金数据 + `start` 覆盖全区间，断言回测使用的 bar 数等于全样本数（而非约 3 年）。
- [ ] **A8 单一 regime 定义**。`source/stock_analysis/advisor/analyzer.py::get_current_market_regime`（输出 StrongBull/Bull/Bear/Crash/Shock）与 `source/stock_analysis/strategy_master/config.py::get_market_regime_series`（输出 Bull/Bear/Shock）必须合并为单一函数、单一标签集合，置于 `source/decision/` 或 `stock_analysis/` 下的唯一模块，两处调用点全部改为引用它。
  **验证**：新增测试断言 `set(历史序列可能标签) == set(当前判定可能标签)`；`grep -rn "StrongBull\|Crash" source/` 要么无结果，要么只出现在该唯一模块内。
- [ ] **A9 对比表卫生**。`analyzer.py::run_strategy_tournament` 第 388–392 行不得再向 `comparison` 注入 `__买入持有基线__` 行；基线改由 `calc_buy_hold_baseline()` 独立返回并进入 JSON `evidence.buy_hold_baseline`。
  **验证**：新增测试断言 `comparison` 的所有行 `策略名` 均存在于 `build_strategy_signals().keys()`，且 `comparison["交易次数"].astype(int)` 不抛异常。
- [ ] **A10 涨跌停按板块**。新增 `source/decision/instrument.py`（或置于 `schema.py`）函数 `classify_board(code) -> (board, price_limit_pct, is_st)`，规则：`sh 688/689 → sh_star, 0.20`；`sh 60 → sh_main, 0.10`；`sz 300/301/302 → sz_gem, 0.20`；`sz 000/001/002/003 → sz_main, 0.10`；`bj 4/8 → bj, 0.30`；名称含 `ST` 且非 `sh_star/sz_gem` → `0.05`；基金 → `fund, 0.10`。`engine.py` 第 124–125 行的 `1.099/0.901` 硬编码必须改为由该 `price_limit_pct` 驱动（阈值取 `1 ± (limit − 0.001)`）。
  **验证**：新增测试覆盖 `600519→0.10`、`688981→0.20`、`300750→0.20`、`002371→0.10`、`830799→0.30`；并断言科创板合成数据在 +15% 当日仍可买入（旧逻辑会错误拦截）。
- [ ] **A11 止损跳空**。`engine.py` 第 129–131 行止损成交价必须为 `min(entry_price×(1−stop_loss_pct), open_px)`（经滑点调整后）；止盈对称处理为 `max(entry_price×(1+take_profit_pct), open_px)`。
  **验证**：新增测试，构造某根 K 线 `open` 比止损价低 5%，断言该笔 `trade.exit_price < entry_price×(1−stop_loss_pct)`。
- [ ] **A12 贝叶斯基准一致**（并入 A1 实施）。
  **验证**：新增测试断言 `bayes_search` 返回的 `best_result.trade_count` 与用同一 config 直接全样本回测该策略的结果一致。
- [ ] **A13 修复影响留证**。新建 `docs/validation_600519.md`，包含下方"基线快照"的 before 数据、Phase A 结束时的 after 数据、Phase D 结束时的 final 数据，三列对照，并标注运行日期、数据区间、Python 与依赖版本。
  **验证**：文件存在且三列数据齐全；after 列的可负担占比 = 100%、`唐奇安/MACD/均线趋势` 交易数 = 29/32/42。
- [ ] **A14 离线修复现有失败测试**。`source/tests/test_p1_oos.py::test_adaptive_oos_high_price`（第 81 行 `pd.read_csv('stock_analysis/data/stock_600519_qfq.csv')`）是**唯一被授权修改的现有测试**。改为使用 `conftest.py` 的 `high_price_df` fixture，保留其原始意图（高价股必须可评估、可成交），不得删除该测试函数。
  **验证**：`python -m pytest tests/ -q` 在**断网**环境下 0 failed / 0 error。

### Phase B — 可靠性：风险调整 + 共识 + 多重检验校正

- [ ] **B1 评分量纲一致**。在 `source/decision/scoring.py` 实现新评分，替换 `analyzer.py::score_backtest_result` / `score_oos_result` 中 `Wilson×0.5 + 年化×0.2 − 回撤×0.3` 的公式。新评分必须以风险调整后收益为主项（Calmar 或 Sharpe），胜率仅作次要项或仅用于置信度。所有权重集中为该模块顶部的具名常量。
  **验证**：新增测试 `test_scoring_risk_adjusted`：构造两个 `BacktestResult`，A(年化 12%、回撤 35%)、B(年化 10%、回撤 7%)，断言 `score(B) > score(A)` 且差值 > 0.05。另断言对 600519 实测 Top12，胜率项对 |总分| 的贡献占比 ≤ **40%**（before = 72.8%）。
- [ ] **B2 波动率自适应止损止盈**。`BacktestConfig` 增加 `stop_mode: str = "atr"`（可选 `"fixed"`）、`atr_period: int = 14`、`atr_stop_mult: float = 2.5`、`atr_profit_mult: float = 4.0`。`engine.py` 在 `stop_mode=="atr"` 时用逐 bar `ATR` 计算止损止盈距离（须为**滚动历史 ATR**，禁止使用当 bar 之后的数据），默认启用 `atr`。
  **验证**：新增测试，同一策略在同一合成序列的高波动段与低波动段上，断言实际止损距离不同（差异 > 20%）；并断言 `stop_mode="fixed"` 时行为与旧逻辑一致（回归保护）。
- [ ] **B3 真正的 walk-forward**。改造 `analyzer.py::walk_forward_splits` 的使用方式：每折必须在**该折训练集上独立重新选参**，再用该折测试集评估所选参数；不得继续用固定策略重跑子窗口。函数命名须反映语义（如 `walk_forward_optimize`）。
  **验证**：新增测试，用两段统计特性明显不同的合成序列拼接（前段趋势、后段均值回归），断言不同折选出的参数**不完全相同**（至少一折的 best 与全局 best 不同）。
- [ ] **B4 多重检验校正**。在 `scoring.py` 实现 Deflated Sharpe Ratio（或等价的按试验次数调整的置信下界），输入为 `n_trials`（= 参与评分的配置数）、观测 Sharpe、收益偏度与峰度、样本长度。JSON 输出 `evidence.n_trials`、`evidence.deflated_sharpe`、`evidence.significant`。当 `deflated_sharpe ≤ 0` 时 `status` 必须为 `no_edge`、退出码 **10**。
  **验证**：新增测试：(a) 固定观测 Sharpe，`n_trials` 从 10 增至 1000 时 `deflated_sharpe` 单调不增；(b) 用纯随机收益序列跑完整锦标赛，断言最终 `status == "no_edge"`。
- [ ] **B5 族去相关共识**。在 `source/decision/consensus.py` 实现：候选按 `_grid_defs()` 的 `prefix` 分族 → 每族只保留最优代表 → 取 Top-K（K 默认 10）构成共识集，输出 `consensus.family`（主导族）、`consensus.agreement`（族内/跨族方向一致比例 0–1）、`dispersion`（Top-K 评分标准差）。**硬约束：最终候选集中任一族占比 ≤ 50%**。
  **验证**：新增测试，构造一个 Top12 中某族占 7 席的评分表，断言 `select_consensus()` 输出中该族占比 ≤ 50%。对 600519 实测断言 `candidates` 中任一族计数 ≤ ceil(len(candidates)/2)。
- [ ] **B6 建议可复现（本契约最硬的可靠性标准）**。共识选择必须对数据窗口的小扰动稳健（族级选择 + 评分平滑 + 必要时引入滞回）。
  **验证一（离线，进 pytest）**：对合成 `trend_df`，分别用全样本与"砍掉末尾 5% bar"两个窗口调用 `advise()`，断言 `recommendation.action` 与 `evidence.consensus.family` 完全一致，且 `evidence.stability.action_stable == true`。
  **验证二（真实数据，不进 pytest，结果写入 `docs/validation_600519.md`）**：对 600519 分别以 `full` / 去掉末尾 60 根 / 去掉末尾 120 根 三个窗口运行，`recommendation.action` 与 `evidence.consensus.family` 必须**三者完全一致**。**若实测确实无法达成，必须在最终报告中如实说明并给出实测差异，禁止通过放宽断言或调参凑数使其"通过"。**
- [ ] **B7 bootstrap 置信区间**。在 `scoring.py` 实现 `bootstrap_ci(values, stat_fn, n_boot=2000, seed=7)`，对 `annual_return, max_drawdown, win_rate, sharpe, calmar, profit_factor` 与超额收益给出 95% CI，填入 `evidence.metrics.*.ci_95`。随机种子固定。
  **验证**：新增测试：(a) 固定输入下两次调用返回完全相同的 CI（确定性）；(b) 交易数 10 的样本其 `win_rate` CI 宽度严格大于交易数 200 的样本（不确定性随样本量收敛）。
- [ ] **B8 基线显著性**。`no_edge` 判定改为基于 `excess_vs_buy_hold.ci_95`：当 CI 上界 ≤ 0 时判 `no_edge`；不得再只比较年化点值（替换 `analyzer.py` 第 377–386 行的判定逻辑）。
  **验证**：新增测试：(a) 构造一个收益与买入持有逐 bar 相同的策略，断言 `status=="no_edge"`；(b) 构造一个每 bar 稳定跑赢 0.1% 的策略，断言 `status=="ok"`。
- [ ] **B9 失效条件**。`recommendation.invalidation` 必须为具体可读字符串，包含数值化止损价与时间界限（模板：`"收盘跌破 {stop_loss:.2f}，或 {family} 族共识转为看空，或 {n} 个交易日内未触发入场"`）。
  **验证**：新增测试断言该字段非空、包含至少一个数字、且包含 `stop_loss` 的字符串形式。

### Phase C — 可消费性：Hermes 决策契约

- [ ] **C1 纯 JSON stdout**。新增 `--json` 参数。启用时 stdout **只**输出一个 JSON 文档；所有 `print` 的人类可读文本（含 `data_loader` 的 `正在拉取...`、`[INFO]`、`[WARN]`）必须改走 stderr。`data_loader/meta.py` 与 `bars.py` 中的 `print` 需改为 `print(..., file=sys.stderr)` 或 `logging`（`logging` 的 handler 须配置到 stderr）。
  **验证**：`cd source; python cli.py 600519 --json 2>$null | python -c "import json,sys; d=json.load(sys.stdin); print(d['schema_version'])"` 输出 `1.0` 且不报错。
- [ ] **C2 schema 入文档**。README 必须新增"给 Agent 的接口契约"章节，含：调用示例、完整字段表（名称/类型/含义/可否为 null）、枚举取值、退出码表、一份**真实运行产出**的示例 JSON。
  **验证**：README 中可检索到 `schema_version`、全部 6 个退出码、5 个 `action` 枚举值、5 个 `status` 枚举值；示例 JSON 能被 `json.loads` 成功解析（新增一个测试读取 README 中的 JSON 代码块并解析）。
- [ ] **C3 字段齐备**。对 600519（股票）与 510300（基金）各运行一次，`recommendation` 与 `evidence` 中"输出契约规格"列出的字段全部存在；不适用时为显式 `null` 并在 `recommendation.action_reason` 或 `diagnostics.warnings` 说明原因。`position_fraction ∈ [0, 0.30]`。
  **验证**：新增 `source/tests/test_c_schema.py`，用合成数据分别驱动 `advise_stock` 与 `advise_fund`，对产出的 dict 做**递归键集合校验**（期望键集合以常量形式写在测试里），并断言无 `NaN`/`Infinity`（`json.dumps(..., allow_nan=False)` 不抛异常）。
- [ ] **C4 无自相矛盾**。属性测试：对任意合成数据，若 `triggers[].state == "sell_triggered"` 则 `action ∈ {sell, hold, avoid}`；若 `== "buy_triggered"` 则 `action ∈ {buy, hold, avoid}`。且 `distance_to_trigger` 的符号必须与 `state` 自洽。
  **验证**：新增 `hypothesis`-free 的参数化测试（枚举 ≥ 20 组合成行情形态：暴涨、暴跌、横盘、单边趋势、极端超买、极端超卖、数据极短、全零成交量等），全部断言通过。特别地，构造 StochRSI=100 的极度超买形态，断言 `action != "buy"`。
- [ ] **C5 状态与退出码可分支**。四种情形（成功 / 数据不足 / 无优势 / 数据源失败）各自返回上表退出码且互不相同。
  **验证**：用 `subprocess` 分别驱动四个用例（数据不足用超短合成 CSV；数据源失败用不存在的代码 + 屏蔽网络），断言 `returncode` 分别为 0 / 20 / 10 / 30。
- [ ] **C6 单一真源 API**。`from decision import advise` 可用，`advise(code, type="auto", start="20200101", opt="grid", jobs=1, fast=False) -> dict`，返回的 dict 与 `--json` 输出**键集合完全一致**。`cli.py` 中不得残留任何独立计算建议的逻辑。
  **验证**：新增测试断言 `set(advise(...).keys()) == set(json.loads(cli_stdout).keys())`（递归到二级键）。
- [ ] **C7 基金同一契约**。`python cli.py 510300 --json` 通过与 C3 相同的 schema 校验。基金侧参数网格必须先从 `cli.py::run_fund_tournament` 第 1562–1607 行抽取到 `fund_analysis/fund_strategy/registry.py`，提供与股票侧同构的 `_grid_defs()`（含族前缀）与 `build_fund_strategy_signals()`，使 B5 共识逻辑可直接复用。
  **验证**：新增测试断言 `fund_strategy.registry.grid_size() == 44`（保持现有组数），且族数 = 6；`cli.py` 中不再存在内联基金参数 for 循环。
- [ ] **C8 幂等确定性**。同一份缓存数据下连续两次运行，JSON 除 `generated_at` 与 `diagnostics.elapsed_seconds` 外逐字节相同。所有随机过程（bootstrap、bayes）使用固定 seed。
  **验证**：连跑两次，用 Python 剔除上述两个字段后断言 `json.dumps(d1, sort_keys=True) == json.dumps(d2, sort_keys=True)`；此验证以脚本形式加入 `docs/validation_600519.md` 的复现命令清单。

### Phase D — 范围收敛与工程健康

- [ ] **D1 删除冗余**。删除：`source/stock_analysis/strategy_master/config.py` 的 `load_config`/`save_config` 与 `stock_config.json` 读写、`source/stock_analysis/strategy_master/runner.py`、`source/stock_analysis/advisor/recommender.py::main`（含硬编码 watchlist）、`source/fund_analysis/fund_strategy/runner.py::main` 的 `save_config` 回写、`source/stock_analysis/stock_config.example.json`、`source/fund_analysis/fund_config.example.json`、`fund_strategy/strategy.py` 的 `load_config`/`save_config`/`CONFIG_PATH`。`get_market_regime_series` 若仍被使用，须迁移到 `source/decision/` 后再删除 `strategy_master` 包。
  **验证**：`grep -rn "watchlist\|save_config\|load_config\|stock_config\|fund_config" source/` 无结果；上述文件/目录不存在；`python -c "import cli"` 成功（无悬空 import）。
- [ ] **D2 测试离线全绿**。全部测试在断网、无预置缓存 CSV 的环境下通过。用例总数 ≥ **25**。
  **验证**：`cd source; python -m pytest tests/ -q` → `0 failed, 0 error`，且通过数 ≥ 25（预期显著多于 25）。
- [ ] **D3 文档与代码一致**。README 必须修正：(a) 示例输出来自一次真实运行并标注运行日期与数据区间；(b) 删除网格中不存在的 `均线趋势(5-13-34)`（`ma_windows` 无 34），换成真实存在的组合；(c) walk-forward 的描述与实际实现一致（B3 之后应能如实宣称"逐折重新寻优"）；(d) 基金侧"均含双边申赎费""评分与股票侧一致（Wilson 下界）"的描述与 A6/B1 之后的实际实现一致；(e) `requirements.txt` 固定版本改为**实测通过全部测试的环境版本**（当前实测：Python 3.12.10 / akshare 1.18.29 / numpy 2.2.6 / pandas 3.0.1，而 requirements 固定为 akshare 1.17.44 / numpy 2.3.5 / pandas 3.0.6，二者不一致）；(f) README 声明的 Python 版本与实际一致。
  **验证**：`grep -n "均线趋势(5-13-34)" README.md` 无结果；`pip install -r requirements.txt --dry-run` 所列版本与 `python -c "import akshare,numpy,pandas;print(...)"` 输出一致。
- [ ] **D4 输出数据驱动**。在 `BaseStrategy`（`source/stock_analysis/strategy/base.py`）增加声明式方法 `describe_triggers(df) -> dict`，返回 `{indicator, current, buy_trigger, sell_trigger, buy_price, sell_price, state}`；各策略子类按需覆写，基类提供基于 MA/阈值的通用默认实现。`source/decision/triggers.py` 只调用该方法生成 `evidence.triggers` 与文本预案。`cli.py` 中 `_print_detailed_stock_advice`（58–704）与 `_build_tomorrow_plan`（705–1263）的 **45 个 `if best_name...` 分支必须全部删除**，由统一渲染器替代。
  **验证**：`grep -c "if best_name" source/cli.py` = **0**；`cli.py` 行数 ≤ **700**（当前 1689）；新增测试定义一个临时策略类（仅实现 `generate_trade_signals` + `describe_triggers`），注册后**不修改任何渲染代码**即可在 `--json` 与文本输出中同时得到正确触发信息。
- [ ] **D5 性能有界且分档**。新增 `--fast` 参数（降低 bootstrap 次数 / 缩减候选池 / 跳过部分 walk-forward 折）。实测：全量模式单标的 ≤ **300s**（`--jobs 4`），`--fast` ≤ **60s**。两档实测耗时写入 README。
  **验证**：`Measure-Command { python cli.py 600519 --jobs 4 }` ≤ 300s；`Measure-Command { python cli.py 600519 --fast --jobs 4 }` ≤ 60s；README 记录两者实测值。
- [ ] **D6 CLI 向后兼容**。`--detail / --no-regime / --start / --jobs / --opt / --opt-iters / -t|--type` 全部保持可用；新增 `--json / --fast`。语义变更须在 README 标注。
  **验证**：新增测试用 `subprocess` 对每个旧参数各跑一次（可用 `--fast` + 合成/缓存数据），断言 `returncode` ∈ {0,10,20,30,40}（即不因参数本身报 usage error 2）。

---

## 基线快照（before，已实测，直接写入 `docs/validation_600519.md`，执行者无需重测）

环境：Python 3.12.10 / akshare 1.18.29 / numpy 2.2.6 / pandas 3.0.1；git HEAD `e183cb6`；数据 600519，`--start 20200101`，source=sina，1643 根，2020-01-02 ~ 2026-10-16，close min/max/last = 843.0 / 2227.2 / 1329.0。

- **可负担性**：`close×100 ≤ 100000` 的 bar = **66/1643（4%）**，区间 2020-01-02 ~ 2020-04-13。
- **本金敏感性（交易笔数，10万 → 100万）**：唐奇安(20/10) 1→29；MACD(12-26-9) 1→32；均线趋势(5-20-60) 1→42；量价突破 1→35；StochRSI(14,25/75) 32→79；Kelly(5-20,60) 0→19。
- **网格存活**：581 组中 零买零卖信号 8 组、零交易 **197** 组、交易 <10 笔 **564** 组、≥10 笔仅 **17** 组（2.9%）；40 族中 **36 族全灭**。
- **信号充足却零成交实例**：MACD(12-26-9) buy=47/sell=42→1 笔；均线趋势(5-20-60) buy=139/sell=50→1 笔；唐奇安(20/10) buy=107/sell=163→1 笔；量价突破 buy=58/sell=827→1 笔；三维共振 buy=10/sell=413→0 笔；Kelly(5-20,60) buy=19/sell=582→0 笔。对照：KAMA(10) buy=111/sell=111→48 笔；StochRSI(14,25/75) buy=118/sell=131→32 笔。
- **评分结构**：Top12 中 Wilson 胜率项占 |总分| **72.8%**。实例 StochRSI(9,25/80) 年化 12.65%/回撤 34.96%/103 笔（IS 0.1343）排名**低于** StochRSI(14,25/75) 年化 10.14%/回撤 6.78%/32 笔（IS 0.2612）。IS→OOS 衰减示例：StochRSI(14,25/80) 0.2403→0.0403；CCI(14,-150/150) 0.1865→0.0246；KAMA(10) 0.0976→**−0.0755**。
- **稳定性**：full→`StochRSI(14,25/75)`；drop60→`StochRSI(14,25/75)`；drop120→`StochRSI(14,25/80)`（argmax 翻转）。Top5 重合：full/drop60 = 2，full/drop120 = **1**，drop60/drop120 = 4。Top12 族集中度：StochRSI 7/12 = **58%**。
- **CLI 实测输出（`python cli.py 600519 --no-regime`，41.3s）**：最优 `StochRSI(14,25/75)`，胜率 75.00%，盈亏比 5.78，年化 10.14%，回撤 6.78%，夏普 1.10，32 笔，平均持仓 11.1 天；`今日信号: 无（观望）`；`StochRSI = 100.00 (超买(>75触发卖出))`；`买入触发: StochRSI 上穿 25（当前 100.00）` ← 自相矛盾实证。
- **其他标的可负担性（@100k，`max_position_pct` 1.0 / 0.3）**：600519 66/1643、0/1643；002371(593.6元) 1643、1205/1643；300750(277.0元) 1643、1331/1643；000858、603259、601398、600036、688981、002594、601318 均 1643/1643。
- **测试**：`python -m pytest tests/ -q` → **24 passed, 1 failed**（`test_p1_oos.py::test_adaptive_oos_high_price`，`FileNotFoundError: stock_analysis/data/stock_600519_qfq.csv`），24.51s。
- **代码规模**：`cli.py` 1689 行，`if best_name` 分支 45 处。

---

## Definition of Done（最终验证命令清单，全部必须通过）

在 `source/` 目录下依次执行：
1. `python -m pytest tests/ -q` → 0 failed / 0 error，通过数 ≥ 25（断网环境）
2. `python cli.py 600519 --json 2>$null | python -c "import json,sys; d=json.load(sys.stdin); assert d['schema_version']=='1.0'; assert d['recommendation']['status'] in {'ok','no_edge','insufficient_data','data_source_error','untradeable'}; assert 0 <= d['recommendation']['position_fraction'] <= 0.30; assert d['diagnostics']['unaffordable_lot_events']==0; print('OK')"` → 打印 `OK`
3. `python cli.py 510300 --json` → 同样通过 schema 校验，退出码 ∈ {0,10,20,30,40}
4. `python cli.py 600519 --json` 连跑两次，剔除 `generated_at` 与 `diagnostics.elapsed_seconds` 后 JSON 完全相同
5. 600519 三窗口（full / drop60 / drop120）运行的 `action` 与 `consensus.family` 三者一致，结果记入 `docs/validation_600519.md`
6. `grep -c "if best_name" cli.py` → `0`；`(Get-Content cli.py).Count` → ≤ 700
7. `grep -rn "watchlist\|save_config\|load_config\|stock_config\|fund_config" .` → 无结果
8. `Measure-Command { python cli.py 600519 --jobs 4 }` ≤ 300s；`--fast` ≤ 60s
9. `python cli.py --help` 列出全部旧参数 + `--json` + `--fast`
10. README 含"给 Agent 的接口契约"章节，其示例 JSON 可被 `json.loads` 解析

最终报告须逐条列出上述 10 项的实际执行结果（含失败项与真实数字），不得以"应该通过"代替实测。

---

## Boundaries

- **不新增数据库、不新增任何前端/Web UI/可视化界面、不新增账户持仓或自选股持久化**。`advise()` **不得**接收账户资金、持仓、成本价等任何账户态入参；仓位只以相对比例表达。
- **不引入需要 API key、登录或付费的数据源**；沿用现有公开接口与本地 CSV 缓存，不改变缓存目录（`source/stock_analysis/data/`、`source/fund_analysis/data/`）与 `.gitignore` 策略。
- **不新增第三方依赖**（含 scipy / scikit-learn / statsmodels / hypothesis）；只用现有 `numpy` / `pandas` / `requests` / `akshare` / `pytest`。所有统计功能用 numpy 自实现。
- **不得为了让测试通过而放宽阈值、放宽门槛或把期望值硬编码进断言**；任何阈值调整必须给出量化依据并写入最终报告。
- **不删除现有测试用例**；唯一被授权修改的是 `test_p1_oos.py::test_adaptive_oos_high_price` 的数据获取方式（A14），其测试意图必须保留。
- **不改变策略网格组数**（股票 581 / 基金 44），以免破坏 `test_registry_single_source` 与 README 数字；若确需变更须同步更新断言与全部文档数字并说明理由。
- **不提交任何真实配置文件、账户信息、持仓数据或个人隐私**；示例与测试数据一律用公开标的或合成数据。
- **不做确定性收益承诺**；所有输出必须保留"不构成投资建议"免责，并如实表达不确定性（CI、DSR、分歧度）。证据不足时必须输出 `no_edge`/观望，禁止强行给方向。
- **不扩大范围**：不实现自动下单、实盘对接、行情推送、多账户、风控中台、定时任务等未列入验收标准的功能。
- **不重写 git 历史**；在当前 `main` 工作树内以新提交推进。
- **B6 验证二若无法达成，必须如实报告实测差异**，禁止调参凑数或修改断言使其假性通过。
