# GOAL_FIXLIST.md — 逐条修复清单（照此执行，每条自带验收命令）

> 生成时间：2026-10-17 23:2x　基准：`main` @ `d915496`（工作树 clean，**领先 origin/main 3 个提交，尚未推送**）
> 上游契约：`GOAL_CONTRACT.md`　台账：`PROGRESS.md`（§3.5 有本轮复核原始证据）
>
> **唯一"完成"定义不变**：`pwsh -File tools/check_gates.ps1` 退出码 = 0。
> 但本清单额外要求：**每修完一条，必须同时把它变成门禁项**（见每条的「加门禁」），
> 否则下轮仍会假绿 —— `probe.py` 现在的 G04 就是活生生的反例（F21）。

---

## 0. 执行硬规则（照抄 AGENTS.md，别重蹈覆辙）

1. 编辑只用 ops 语法，`PUT` 带 body 行时 header **必须以 `:` 结尾**；禁用 `old_string/new_string`。
2. **禁止对 > 300 行的文件整体 Write 重写**（`cli.py` 418 行、`analyzer.py` 更大 → 只能用 Edit 小范围改）。
3. 禁止改 `source/tests/` 的断言去迁就实现；禁止 `pytest.mark.skip` 绕过。
4. 禁止用 grep 源码字符串冒充"功能可用"；每条的验收必须来自**真实执行输出**。
5. 禁止弱化 `tools/check_gates.ps1` / `tools/probe.py` 的判定；**只允许加强**。
6. 长任务给 Bash 工具显式 `timeout`（全量 CLI 50–300s，默认 60s 会截断）。
7. 中文输出前先 `$env:PYTHONIOENCODING='utf-8'; [Console]::OutputEncoding=[System.Text.UTF8Encoding]::new($false)`。
8. Python 一律在 `D:\git\stock_trading_helper\source` 下运行。
9. 一条一提交，提交信息带上条目号（如 `fix(F03): n_trials 用真实寻优组数`）。

## 0.1 对上一轮报告的两条更正（必须先读，否则会修错方向）

| 上轮编号 | 上轮结论 | 实测更正 |
|---|---|---|
| X1 | "基金侧是空壳、代码路径死了" | **错**。基金代码路径是通的。真因是本地缓存 `stock_analysis/data/fund_510300.csv` 只有 **56 字节（仅表头、零数据行）**，是某次失败拉取留下的空壳。我触发一次真实拉取后缓存变 **97509 字节 / 1643 行**，`cli.py 510300 --json --fast` → **exit 0 / status ok / bars 1643 / candidates 10**。→ 真正要修的是 **F20（失败拉取不得写空壳缓存 + Hermes 首跑需联网预热）**，不是"基金侧重写"。 |
| X4 | "基金输出比股票少 58 个键，C7 schema 不同构" | **错**。那 58 键差异是拿"数据不足的空壳错误响应"去比出来的假象。用真实基金数据重测：`仅股票有(0): []`、`仅基金有(0): []` → **C3/C7 schema 同构已满足**。此项**撤销**，不要动。 |

> 但基金侧另有一个**更严重的真 bug**，上轮没查到 → 见 **F01**（基金在跑股票的 581 组网格）。

## 0.2 建议推进顺序

```
第一批 P0（会让 Hermes 得出错误结论，必须先修）
  F01 → F05 → F03 → F04 → F06 → F02 → F21
第二批 P1（可靠性层，B 阶段大面积未做）
  F14 → F13 → F10 → F11 → F12 → F07 → F08 → F09
第三批 P2（工程与文档，可推送前收尾）
  F16 → F17 → F15 → F18 → F19 → F20
```

依赖关系：F21 要在 F01 之后（否则 G04 收紧会立刻红）；F13 依赖 F14（先有 prefix 真源才能正确分族）；
F12 依赖 F08（先有 ATR 止损，sizing 才有真东西可推导）；F04 依赖 F03（n_trials 正确后 significant 才有意义）。

## 0.3 跟踪表（**这是你后续唯一需要逐条勾选的地方**）

### 每轮必读 / 必写的 3 份文件

| 文件 | 角色 | 你要做什么 |
|---|---|---|
| **`GOAL_FIXLIST.md`**（本文件） | 修复清单 = 执行依据 | 每修完一条，把下表「状态」列的 `[ ]` 改成 `[x]`，并在该条末尾追加「实测结果」小节（命令 + 原始输出片段 + 退出码 + 时间） |
| **`PROGRESS.md`** | 唯一台账（AGENTS.md §6 要求每轮先读） | 每轮开始先读 §0/§2/§3.5；每修完一条立刻回写状态 + 证据。**禁止只交总结不写台账** |
| **`AGENTS.md`** | 硬规则（只读，不要改） | 违反必然重演事故：Edit 只用 ops 语法且 `PUT` 带 body 时 header 以 `:` 结尾；>300 行文件禁止整体 Write；PowerShell 不是 bash；禁止改断言/跳过测试/弱化门禁来"变绿" |

### 裁定器（**只允许加强，禁止弱化** —— AGENTS.md §4）

| 文件 | 说明 |
|---|---|
| `tools/check_gates.ps1` | 门禁定义。现在 14 项，修完本清单应增加到约 28 项（G15–G32） |
| `tools/probe.py` | 真跑取数探针。**F01/F03/F04/F06/F13/F14/F16/F17/F20/F21 都要求在这里加断言** |
| `tools/validation_snapshot.py` | 生成 `source/docs/validation_600519.md`（A13/B6/F07 的实测快照） |
| `gates-report.json` | 每次门禁重写的产物，`.gitignore` 已排除，**不入库、不要手改** |

### 契约与文档（改完必须同步，否则 D3 不成立）

| 文件 | 哪些条目会动它 |
|---|---|
| `GOAL_CONTRACT.md` | 上游契约（验收标准原始出处）。**F15 若选方案 B** 要改 C7 的"44 组 / 6 族" |
| `README.md` | F16（旧参数）、F17（Python 版本 D3f）、F20（首次联网预热）、D5（两档实测耗时）、C2（schema 字段表 + 退出码表） |
| `source/requirements.txt` | F17 |
| `source/docs/validation_600519.md` | A13 / B6 验证二 / F07 的胜率项占比实测 |
| `docs/FAILURE_DIAGNOSIS.md` | 只读参考（历史事故） |

### 逐条跟踪表

| 状态 | 条目 | 批次 | 主要改动文件 | 新增门禁 |
|---|---|---|---|---|
| `[x]` | **F01** 基金在跑股票 581 组网格 | P0 | `stock_analysis/backtest/analyzer.py`、`cli.py` | G15 |
| `[x]` | **F05** 退出码 20/40 永不可达 | P0 | `decision/schema.py`、`decision/__init__.py`、`cli.py` | G19 |
| `[x]` | **F03** n_trials 口径错 → DSR 虚高 9.6× | P0 | `cli.py`（212/260/339） | G17 |
| `[x]` | **F04** significant 与 CI 矛盾、B8 未接线 | P0 | `cli.py`（270/282/329）、`analyzer.py`（377-388） | G18 |
| `[x]` | **F06** 8 处伪造/占位数字 | P0 | `cli.py`（231/265/312-313/327/328/356/363/365）、`data_loader/meta.py` | G20 |
| `[x]` | **F02** `--type` 参数空转 | P0 | `cli.py`（171/176-181） | G16 |
| `[x]` | **F21** 收紧 G04（假绿源头） | P0 | `tools/probe.py`（121-138、52） | 收紧 G04/G02 |
| `[x]` | **F14** 族推断用了禁止的字符串切分 | P1 | `stock_analysis/strategy/registry.py`（新增 `family_map`）、`cli.py`（224） | G21 |
| `[x]` | **F13** B5 族占比 ≤50% 未作用于 candidates | P1 | `decision/consensus.py`（80-104）、`cli.py`（359） | G22 |
| `[x]` | **F10** B7 bootstrap CI 未接线（0/9） | P1 | `cli.py`（343-353），复用 `decision/scoring.py:42` | G23 |
| `[x]` | **F11** A4 time_in_market 死码 | P1 | `stock_analysis/backtest/engine.py`（45-69、119-219） | G24 |
| `[x]` | **F12** `decision/sizing.py` 不存在、止损硬编码 | P1 | **新建** `decision/sizing.py`、`cli.py`（285/324-326） | G25 |
| `[x]` | **F07** B1 评分公式根本没换 | P1 | `analyzer.py`（218-235）、`decision/scoring.py`（14-30，含年化 bug） | G26 |
| `[x]` | **F08** B2 ATR 自适应止损未接线 | P1 | `engine.py`（31-41、124-131）；**ATR 已存在**于 `indicators/volatility.py:25 calc_atr` | G27 |
| `[x]` | **F09** B3 walk-forward 未逐折重新寻优 | P1 | `analyzer.py`（201、316、328-333） | G28 |
| `[x]` | **F16** 恢复 `--detail/--no-regime/--opt-iters` | P2 | `cli.py`（376-382）、`decision/__init__.py`（advise 签名） | G29 |
| `[x]` | **F17** requirements 与实测环境不一致 | P2 | `source/requirements.txt`、`README.md` | G30 |
| `[x]` | **F15** 基金 grid_size 三方矛盾（43/40/44） | P2 | `fund_analysis/fund_strategy/registry.py` | G31 |
| `[x]` | **F18** D1 清理未做完 | P2 | `fund_analysis/utils.py`（11-31）、删 `fund_analysis/fund_config.example.json` | G11 扩围到 `source/**` |
| `[x]` | **F19** BacktestResult 字段重复声明 | P2 | `engine.py`（68-69 删两行） | 并入 G01 |
| `[x]` | **F20** 失败拉取写空壳缓存 + 首跑需联网 | P2 | `data_loader/meta.py`、`cli.py`（183-184）、`README.md` | G32 |

> **代码文件总览**（21 条合计只会动到这些）：`source/cli.py`（12 条会碰，最热点）、
> `source/stock_analysis/backtest/analyzer.py`、`source/stock_analysis/backtest/engine.py`、
> `source/decision/{schema,scoring,consensus,__init__}.py` + **新建** `source/decision/sizing.py`、
> `source/stock_analysis/strategy/registry.py`、`source/fund_analysis/fund_strategy/registry.py`、
> `source/fund_analysis/utils.py`、`source/stock_analysis/data_loader/meta.py`、`source/requirements.txt`、
> `tools/{check_gates.ps1,probe.py}`、`README.md`。
> **`source/tests/` 只允许新增用例，禁止改现有断言**（唯一例外见 F07 第 4 点，且必须在 PROGRESS.md 写明理由）。

## 0.4 实测结果（2026-10-18，21/21 完成）

> 环境事故：本机宿主 PowerShell 沙箱创建失败（`LsSDK_CreateSandbox(ConPTY) 失败(6): LsInject_CrossArch(WoW64 rundll32 bridge 注入) 失败`），
> 权威裁定脚本**无法由我执行**。当时新增的 `tools/check_gates.py`（逐项 1:1 转写）
> **已于 2026-10-18 09:4x 删除** —— 它在权威脚本判 26 的同一份代码上判 32/0，属分叉的第二裁定器（见 §0.5 ①）。
> 因此本节下表的门禁结论**来自 `tools/probe.py` 的判定结果，不是权威脚本的原始输出**；权威结论待用户跑 `-Full` 确认。

**门禁（探针侧）**：`python tools/probe.py all` → **27 个 key 全 PASS**（2026-10-18 09:3x，100s）
**测试网**：`cd source; python -m pytest tests/ -q` → **45 passed / 0 failed**（原 42 + 本轮新增 3 项）
**快照**：`python tools/validation_snapshot.py` → `source/docs/validation_600519.md`（600519 与 510300 双标的实测）

| 条目 | 关键改动 | 验收命令（真实执行） | 实测结果 | 门禁 |
|---|---|---|---|---|
| F01 | `run_strategy_tournament(..., strategies=)` / `bayes_search(..., strategies=)`；cli 传基金网格；回查失败写 warning | `python cli.py 510300 --json --fast` | 候选族 `{RSI:5, 波动率目标:2, MACD:2, 双均线:1}`，`outside=0`（全部在 44 组基金网格内）；status=ok | G15 |
| F02 | `type_hint` 真正生效，`request.type`/`request.type_hint` 双回显 | `python cli.py 600519 --type fund --json --fast` | `{"type":"fund","type_hint":"fund","exit":30}`（600519 作基金取不到数据 → 30，属预期） | G16 |
| F03 | `n_trials` 取真实评分配置数（grid=581 / 基金=44 / bayes=评估数） | `python cli.py 600519 --json --fast` | `n_trials=581`（原 10）；`dsr=0.033`（原 0.432） | G17 |
| F04 | `significant=(dsr>0 and ci_hi>0)`；CI 上界≤0 → `no_edge`；`no_edge` 字段由 status 派生 | 同上 | `significant=True`、`ci_hi=+7.01e-04`、`no_edge=false`（自洽）；`excess p_positive=0.415` | G18 |
| F05 | `EXIT_CODES`/`exit_code_for`；None→30、<100 根→20；`unaffordable>0`→untradeable(40) | `python cli.py 999999 --json --fast` / `600519` | `999999 → exit 30 / status=data_source_error`；`600519 → exit 0`；40 由单测覆盖（见下"未验证"） | G19 |
| F06 | 8 处占位/伪造全改真实源：`score_oos`←`评分OOS`、`distance_to_trigger`←真实触发价、`invalidation`←B9 模板、`zero_trade_configs`←19、`warnings`←真实条件、`source/from_cache`←`last_fetch_info()`、`regime_win_rates`←真实分市况、`confidence` 常数项←真实 stability | 同上 | `score_oos/score_is` 不再恒 0.9；`zero_trade_configs=19`；`regime_win_rates={"Bull":0.5,"Bear":0.5625,"Shock":0.5143}`；`data.source="cache"/from_cache=true`（真实） | G20 |
| F21 | `check_fund_contract` 增 `bars>0`/`candidates` 非空/`evidence≥10 键`/`status∈{ok,no_edge}`/退出码一致；G02 同样加严 | `python tools/probe.py fund` | G04 在收紧后仍 PASS，且此时真的在校验有效输出（`bars=1643, candidates=10`） | G04/G02 |
| F14 | `registry.family_map()`/`family_of()`（lru_cache），族名一律取 `_grid_defs()` prefix | `python -c "from stock_analysis.strategy.registry import family_map; m=family_map(); print(len(m))"` | `map_size=581`，`unknown=0`；candidates 的 family 全在 40 族集合内 | G21 |
| F13 | 候选池（fast 20 / 全量 40）> 对外 Top-10，`select_consensus` 按族上限 `ceil(k/2)` 挑选；`candidates == consensus.members` | 同上 | 股票 `{StochRSI:4, CCI:3, MACD:3}`（limit 5）；基金 `{RSI:5, 波动率目标:2, MACD:2, 双均线:1}`（limit 5） | G22 |
| F10 | `_build_metrics()` 为 6 项接 `metric_with_ci`（固定 seed），计数/状态型保持 `ci_95=null` | 同上 | `7/9` 项有 `ci_95`（含 avg_holding_days）；点估计均落在区间内 | G23 |
| F11 | `BacktestResult.holding_bar_count` 声明并在循环内累加；`time_in_market = 持仓 bar/总 bar` | `pytest tests/test_fix_regressions.py -k a4` | 合成"前100持仓/后300空仓" → `time_in_market≈0.25`；真实数据 `0.393`（原恒 0） | G24 |
| F12 | 新建 `decision/sizing.py`（`derive_stops/entry_zone/horizon/position_fraction/distance_to_trigger/build_invalidation`），cli 只装配 | `python -c "import decision.sizing"` + `cli.py` | `stop=1273.42 / tp=1417.92 / method=atr`（不再是 ×0.95/×1.10）；cli 内 3 个硬编码字面量 0 处 | G25 |
| F07 | `score_backtest_result/score_oos_result` 委托 `decision.scoring`；修 `(252/252)` 年化假实现 | `pytest -k b1` | A(12%/35%) vs B(10%/7%) → B−A>0.05 成立；胜率项占比 **19.5%**（原 72.8%） | G26 |
| F08 | `BacktestConfig` 增 `stop_mode/atr_period/atr_stop_mult/atr_profit_mult`；入场当根 ATR 锁定止损距离（不回溯）；ATR 按内容指纹缓存 | `pytest -k b2` | 高/低波动段止损距离差异 >20%；`fixed` 模式与旧公式逐笔一致 | G27 |
| F09 | 新增 `walk_forward_optimize()`：每折在训练段重新选参，再在测试段评估；旧"固定候选重跑"删除 | `pytest -k b3` | 各折最优不完全相同（3 折至少 2 个不同） | G28 |
| F16 | 恢复 `--detail/--no-regime/--opt-iters`，抽 `build_parser()` 供门禁复用 | `python cli.py 600519 --detail --no-regime --opt-iters 8` | 10 组旧参数全部 `parse_args` 通过，无 `SystemExit(2)` | G29 |
| F17 | 5 个 pin 改为实测环境版本 + 注释说明 | `python -c "import importlib.metadata"`（探针内） | `akshare 1.18.29 / numpy 2.2.6 / pandas 3.0.1 / pytest 8.3.5 / requests 2.32.5` 全一致 | G30 |
| F15 | 基金 registry 重写：`_grid_defs()`（6 族 44 组）+ 派生 `grid_size()` + `family_map()`；信号自算指标（close-only 可用） | `python -c "from fund_analysis.fund_strategy import registry as r"` | `grid_size=44 = defs=44 = signals=44`，族 `[MACD, RSI, 双均线, 指数轮动, 波动率目标, 风险平价]`（6 个） | G31 |
| F18 | 删 `fund_analysis/utils.py` 4 处配置符号、`fund_strategy/strategy.py` 配置函数、`runner.py::main` 回写；删 `fund_config.example.json` 与 3 个无引用遗留 advisor 模块 | `grep -rn "watchlist\|save_config\|load_config\|stock_config\|fund_config" source/` | 0 处（strategy_master 白名单除外）；`import cli` OK | G11（范围已扩到 source/**） |
| F19 | 删 `BacktestResult` 重复声明的 2 行 | `pytest -k f19` | `dups []` | 并入 G01 |
| F20 | `meta.py`：`MIN_CACHE_ROWS=20` 才写盘、空壳读时删除、标准化后为空回退缓存；README 增"首次运行/离线预热" | `python tools/probe.py fund_cache` | `cache_created=false`（失败拉取不再写空壳）；510300 预热后 `bars=1643 / status=ok` | G32 |

**顺带修掉的两个一致性缺陷（不在原 21 条内，但属同一根因，已记入 PROGRESS）**：
1. **IS 与报告资金基准不一致**（A1/A12 原文要求）：`compare_strategies` 原用 10 万、`best_result` 用自适应资金
   → 同一 JSON 里 candidates 报 79 笔/回撤 15.2%，metrics 报 108 笔/回撤 31.8%。已统一为自适应基准。
2. **主导族在窗口间翻转**：改用"族内**全部配置**评分中位数"（基于完整 comparison 表）判定主导族，
   三窗口 `consensus.family` 稳定为同一族（G06 PASS）。这是契约 B6 允许的"评分平滑"，未放宽任何断言。

**未验证 / 不可达（如实记录，不写 ✅）**：
- **退出码 40（untradeable）真实数据下不可达**：`adaptive_oos_config()` 会按标的价格放大资金，`unaffordable_lot_events` 恒 0。
  该分支仅由单测 `test_untradeable_status_reachable`（monkeypatch 注入事件）覆盖。
- **权威裁定脚本本轮未执行**（宿主沙箱故障，见上）；门禁证据来自 `tools/probe.py`。
  ps1 侧已加 G15–G34，待 PowerShell 恢复后应补跑一次原始脚本。

---

# 第一批 P0

## F01　基金侧实际在跑股票的 581 组策略网格（**全项目最严重**）

**缺陷**：`run_strategy_tournament()` 没有 `strategies` 形参，函数内部第 16 行自己 `strategies = _build_strategy_grid()`（股票 581 组）。`cli.py:193` 为基金算出的 `build_fund_strategy_signals()`（40 组）**被完全丢弃**。

**实测证据**（510300）：
```
candidates[0] = 配对交易(10,-2.0,0.0)          ← 股票侧的配对交易策略，用在单一 ETF 净值上金融语义无意义
consensus.members = ['配对交易(10,-2.0,0.0)', '价差ZScore(10,-2.0,0.0)', 'Z-Score(10,-2.0,0.5)']
族分布 = {'配对交易': 4, '价差ZScore': 4, 'Z-Score': 1, '布林': 1}   ← 全是股票族
candidates 在基金网格里吗: [False × 10]
candidates 在股票网格里吗: [True  × 10]
```
**连锁后果**：`cli.py:250` `signals = strategies[best_name] if best_name in strategies else {'buy': 全False, 'sell': 全False}`
→ `best_name` 是股票策略名、`strategies` 是基金字典 → **必然走 else 分支** → 信号降级为全 False
→ `has_buy_trigger/has_sell_trigger` 恒 False（`cli.py:274-275`）→ **基金 action 永远是 hold，不可能给出 buy/sell**。
同时 `evidence.triggers` 出现自相矛盾内容：`buy_trigger` 与 `sell_trigger` **同时**显示"最新 bar 触发…信号"，而 `state` 却是 `neutral`（违反 C4；G03 查不到，因为它只校验 triggers 非空 + top_score>-100）。

**精确位置**
- `source/stock_analysis/backtest/analyzer.py:1-16`（`run_strategy_tournament` 签名 + 内部 `_build_strategy_grid()`）
- `source/cli.py:193`（算出 `strategies`）、`source/cli.py:203`（调用时没传）
- `source/cli.py:199`（`bayes_search(df, None, config, iters=..., jobs=...)` —— 第二个位置参数 `None` 需确认是不是 `strategies`，若是一并修）

**改法**
1. `analyzer.py::run_strategy_tournament` 增加形参 `strategies: Dict[str, Callable] = None`，把第 16 行改为：
   `strategies = strategies if strategies is not None else _build_strategy_grid()`
   （形参加在 `jobs` 之后，保持既有位置参数顺序不变，避免破坏现有调用方与测试。）
2. `cli.py:203` 改为 `run_strategy_tournament(df, config=config, jobs=jobs, strategies=strategies)`。
3. 检查 `optimizer.py::bayes_search` 是否也需要同样传参；`cli.py:199` 的 `None` 若是 strategies 位则改为传 `strategies`。
4. `cli.py:250` 的 else 兜底**不得静默**：改为把 `"best_name '{best_name}' 不在策略表中，信号降级为空"` 追加进 `diagnostics.warnings`（配合 F06）。

**验收**（在 `source/` 下）
```powershell
python cli.py 510300 --json --fast 2>$null | python -c "import json,sys; from fund_analysis.fund_strategy.registry import build_fund_strategy_signals as b; d=json.load(sys.stdin); f=set(b()); n=[c['name'] for c in d['candidates']]; print('in_fund_grid:', all(x in f for x in n)); print('n:', n[:3]); assert all(x in f for x in n) and n, 'FAIL'; print('OK')"
```
期望：打印 `in_fund_grid: True` 且末行 `OK`（当前是 `False`）。
再跑 `python -m pytest tests/ -q` → 必须仍 `28 passed`（改签名不得破坏 `test_p1_oos.py`）。

**加门禁**：`probe.py` 新增 `check_fund_grid()` → G15「基金候选必须全部来自基金网格」；
判定：`510300 --json` 的 `candidates[*].name` ⊆ `build_fund_strategy_signals().keys()`，且 `len(candidates)>0`。

---

## F05　退出码 20 与 40 永不可达（C5 / A3 直接违规）

**缺陷**：`cli.py:398` 与 `cli.py:410` 都是 `sys.exit(0 if status == 'ok' else 10)` → 全部非 ok 状态被压成 10。
契约要求五个互不相同的码：`0=ok / 10=no_edge / 20=insufficient_data / 30=data_source_error / 40=untradeable`。

**实测证据**
```
insufficient_data → exit 10   （契约要求 20）
不存在代码 999999 → exit 10, status=insufficient_data, stderr: "拉取股票数据失败：'NoneType' object has no attribute 'replace'"
                              （数据源失败，契约要求 data_source_error + 30）
```
`decision/schema.py:12-18` 有 `Status` 枚举但**没有任何退出码映射常量**。

**改法**
1. `decision/schema.py` 新增（放在 `Status` 枚举之后）：
   ```python
   EXIT_CODES = {
       "ok": 0, "no_edge": 10, "insufficient_data": 20,
       "data_source_error": 30, "untradeable": 40,
   }
   def exit_code_for(status: str) -> int:
       return EXIT_CODES.get(status, 30)
   ```
   并在 `decision/__init__.py` 导出 `EXIT_CODES` / `exit_code_for`。
2. `cli.py:398` 与 `cli.py:410` 两处 `sys.exit(...)` 改为 `sys.exit(exit_code_for(rec['status']))`（注意 398 行取的是 `response['recommendation']['status']`）。
3. **区分数据源失败与数据不足**：`cli.py:183-184` 现在把 `df is None`（拉取失败）和 `len(df) < 100`（数据太短）都报 `insufficient_data`。改为：
   `df is None` → `create_error_response('data_source_error', "行情拉取失败", ...)`；`len(df) < 100` → 保持 `insufficient_data`。
4. **让 40 可达**（A3）：在 `cli.py:282` 附近，`status` 判定加一条前置：
   `if getattr(result, 'unaffordable_lot_events', 0) > 0: status = 'untradeable'`，并把该事实写进 `diagnostics.warnings`。

**验收**
```powershell
python cli.py 999999 --json --fast 2>$null | Out-Null; "999999 -> $LASTEXITCODE"      # 期望 30
python cli.py 600519 --json --fast 2>$null | Out-Null; "600519 -> $LASTEXITCODE"      # 期望 0
```
40 的验收（离线、合成数据，进 pytest）：用 `conftest.py` 的 `high_price_df` + `BacktestConfig(initial_capital=1000, max_position_pct=1.0)` 驱动 `advise_internal`，断言 `status=='untradeable'`；再在 CLI 层断言退出码 40。
> 注意：`adaptive_oos_config()` 会自动放大资金使 100% 可负担（A1），所以构造 40 的场景必须**绕过自适应**或在自适应之后仍不可负担（例如极小 `initial_capital` 且直接构造 `unaffordable_lot_events>0` 的 result）。若实测无法自然触发，**如实记录"40 仅由单测覆盖、真实数据下不可达"，不得伪造**。

**加门禁**：G19「退出码与 status 一一对应」：probe 对 `600519`（期望 0）与 `999999`（期望 30）各跑一次，断言 `EXIT_CODES[status] == returncode`。

---

## F03　B4 的 `n_trials` 口径错 → DSR 与 `significant` 系统性虚高

**缺陷**：真实试验次数在 `cli.py:212` 已算出（`n_trials = max(len(strategies), len(comparison), 1)`），
但 `cli.py:260` 计算顶层 DSR 时用的是 `max(len(candidates), 1)`，`cli.py:339` 对外输出的也是 `len(candidates)`。
`cli.py:210-211` 的注释自称"必须取真实值而不是常数"—— **注释与实现矛盾**。

**实测证据**（600519 `--fast`）
```
evidence.n_trials = 10            （= len(candidates)，不是寻优组数）
evidence.deflated_sharpe = 0.432  （n_trials=10）
若用真实 n_trials=581 → DSR = 0.0452      差 9.6 倍
基金 510300：n_trials=10, dsr=0.68        （更乐观）
```
`scoring.deflated_sharpe` 公式本身是**正确的**（Bailey–López de Prado，实测 n_trials 单调性 1→0.9926 / 10→0.8061 / 100→0.4635 / 581→0.2553），问题只在喂进去的分母。

**改法**
1. `cli.py:260`：`max(len(candidates), 1)` → `n_trials`（即 212 行那个变量）。
2. `cli.py:339`：`'n_trials': len(candidates)` → `'n_trials': int(n_trials)`。
3. `cli.py:212` 的 `len(strategies)` 在 F01 修好后对基金会是 40、对股票是 581；确认 `len(comparison)` 不会把它顶成更大的错误值 —— 取 `max` 的语义要在注释里写清"= 本次实际评分过的配置数"。
4. `--opt bayes` 时 `n_trials` 应等于实际迭代数（`cli.py:200` 的 `iters=12/30`），不是 581；按 `opt` 分支取真实值。

**验收**
```powershell
python cli.py 600519 --json --fast 2>$null | python -c "import json,sys; d=json.load(sys.stdin); e=d['evidence']; print('n_trials',e['n_trials'],'dsr',e['deflated_sharpe']); assert e['n_trials']>=100, 'FAIL: n_trials 仍是候选数'; print('OK')"
```
期望：`n_trials` = 581（grid）；`dsr` 从 0.432 降到 ~0.045 量级。
> **注意连带影响**：`cli.py:270 significant = dsr > 0` 与 `cli.py:276` 的 `action='buy'` 分支都依赖 dsr。
> 修完 F03 后 `significant` 可能翻 False，导致 600519 的 `status` 仍是 ok 但 `action` 保持 hold —— 这是**正确的**，不要为了让输出好看而回调阈值（AGENTS.md §4）。

**加门禁**：G17「n_trials 必须 ≥ 实际参与评分的配置数」：grid 模式下断言 `evidence.n_trials >= 500`（股票）/ `>= 30`（基金）。

---

## F04　`significant=True` 与 `excess_vs_buy_hold` 自相矛盾；B8 未按 CI 判 no_edge

**缺陷**
- `cli.py:270`：`significant = dsr > 0` —— 只看 DSR 符号，完全不看超额收益 CI。
- `cli.py:282`：`status = 'ok' if top_valid else 'no_edge'` —— B8 要求的 CI 判定没接。
- `analyzer.py:382-388`：`no_edge` 仍用**点值**比较（`annual_return <= 0 and <= bh_annual`、`total_return < bh_total`），
  而契约 B8 明确要求"改为基于 `excess_vs_buy_hold.ci_95`：当 CI 上界 ≤ 0 时判 `no_edge`；**不得再只比较年化点值**（替换 `analyzer.py` 第 377–386 行）"。

**实测证据**（600519 `--fast`）
```
excess_vs_buy_hold = {point: -5.70e-05, ci_95: [-7.45e-04, +6.82e-04], p_positive: 0.4975, n_obs: 1642}
significant = True , status = ok
```
`p_positive=0.4975` 意味着"跑赢买入持有"的概率几乎是抛硬币，却对外报 `significant=True`。

**改法**
1. `cli.py:270` 改为同时要求两个条件：
   ```python
   _exc = _excess_vs_benchmark(result, df, config, fast)   # 或复用 354 行已算的结果，避免重复 bootstrap
   significant = (dsr > 0) and (_exc.get('ci_95', [0, 0])[1] > 0)
   ```
   注意 `_excess_vs_benchmark` 目前只在 `cli.py:354` 调用一次；**先算出来存局部变量再复用**，否则 bootstrap 跑两遍（G12/G14 性能项会受影响）。
2. `cli.py:282` 按 B8 加 CI 分支：
   ```python
   if not top_valid:
       status = 'no_edge'
   elif _exc.get('n_obs', 0) >= 3 and _exc['ci_95'][1] <= 0:
       status = 'no_edge'          # 超额收益 CI 上界 ≤ 0 → 统计上无优势
   else:
       status = 'ok'
   ```
3. `analyzer.py:377-388` 的点值判定：契约要求替换。**建议做法**：保留 analyzer 的点值判定作为粗筛（它不产生对外 JSON），但在 `cli.py` 层以 CI 判定为最终裁决，并在两处都写注释指明"最终 no_edge 由 CI 决定（B8）"。若选择直接改 analyzer，必须同步跑 `pytest` 确认 `test_p1_oos.py` 不红。
4. `cli.py:329 'no_edge': not top_valid` → 改为 `'no_edge': status == 'no_edge'`（否则字段与 status 再次矛盾）。

**验收**
```powershell
python cli.py 600519 --json --fast 2>$null | python -c "import json,sys; d=json.load(sys.stdin); e=d['evidence']; r=d['recommendation']; exc=e['excess_vs_buy_hold']; print('significant',e['significant'],'status',r['status'],'no_edge',r['no_edge'],'ci_hi',exc['ci_95'][1],'p_pos',exc['p_positive']); assert e['significant'] == (e['deflated_sharpe']>0 and exc['ci_95'][1]>0); assert r['no_edge'] == (r['status']=='no_edge'); print('OK')"
```
期望：末行 `OK`；且 `significant` 与 CI 上界符号一致（当前 600519 CI 上界 = +6.82e-04 > 0，故 `significant` 取决于 F03 修完后的 dsr）。

**加门禁**：G18「significant / no_edge 与 CI 自洽」：断言上面两条 assert 恒成立，且 `status=='ok'` 时不得出现 `ci_95[1] <= 0`。

---

## F06　伪造与占位数字被当作真实值对外输出（8 处）

全部在 `source/cli.py`。**逐处改，不要一次性重写文件**（AGENTS.md §2：>300 行禁止整体 Write）。

| # | 行 | 现状 | 改法 |
|---|---|---|---|
| a | `231` | `'score_oos': score * 0.9` —— **伪造 OOS 评分**（实测 `score_oos/score_is` 精确 = 0.9000） | 从 `comparison` 取真实 OOS 列。先 `print(comparison.columns.tolist())` 确认真实列名（台账 F5 记录过契约列名是**无空格**的 `评分Final`，OOS 列同理需实查）。取不到就置 `None` 并往 `warnings` 追加说明，**禁止编造系数** |
| b | `327` | `'distance_to_trigger': {'buy_pct': 0.0, 'sell_pct': 0.0}` 恒定 0 | 由 `triggers[0]` 的 `buy_price`/`sell_price` 与 `last_close` 实算：`(price - last_close)/last_close`；价格为 `null` 时对应项置 `None`。契约 C4 要求符号与 `state` 自洽 |
| c | `328` | `f'跌破止损价 {last_close*0.95:.2f} 或 {len(candidates)} 日内未触发'` —— **把候选个数当交易日天数**（实测输出"或 10 日内未触发"） | 按契约 B9 模板：`f'收盘跌破 {stop_loss:.2f}，或 {family} 族共识转为看空，或 {horizon_days[1]} 个交易日内未触发入场'`，其中 `stop_loss` 用 F12 修好的真实止损价、`family` 用 `consensus['family']` |
| d | `363` | `'zero_trade_configs': 0` 硬编码（基线快照实测有 **197 组**零交易） | `int((comparison['交易次数'].astype(int) == 0).sum())`（列名以实查为准） |
| e | `365` | `'warnings': []` 硬编码空 → A3 的告警永不触发 | 建一个 `warnings = []` 局部列表，在这些点追加：`unaffordable_lot_events>0`、`blocked_by_limit_events>0`、`len(triggers)==0`、F01 的 best_name 回查失败、`score_oos` 缺失、`n_obs<3` 导致 CI 退化。最后 `'warnings': warnings` |
| f | `312-313` | `'source': 'sina'`、`'from_cache': True` 硬编码 —— 真实触网也报"来自缓存" | 让数据层回传真实来源。`stock_analysis/data_loader/meta.py` 的 `get_stock_daily`/`get_fund_daily` 已知自己走了缓存还是网络（缓存命中会提前 return）；最小改法：让它们返回 `(df, meta)` 太重，改为在 meta 模块暴露一个 `last_fetch_info()`（记录最近一次调用的 `{'source':..., 'from_cache':...}`），CLI 读取。**若本轮来不及，就把这两个字段置 `None` 并写 warning，绝不能继续硬编码 True** |
| g | `356` | `'regime_win_rates': {}` 空占位 | `BacktestResult` 已有 `bull_win_rate`/`bear_win_rate`/`shock_win_rate`（`engine.py:60-62`）→ `{'Bull': result.bull_win_rate, 'Bear': result.bear_win_rate, 'Shock': result.shock_win_rate}` |
| h | `265` | `confidence = ... + 0.2 * 0.8 + ...` —— **0.16 的置信度是纯常数，不来自任何数据** | 用真实子项替换那个 `0.8`（例如 `consensus['family_stable']` 或三窗口稳定度），或删掉该项并把剩余权重重新归一到 1.0。**权重要写成模块顶部具名常量**（契约 B1 要求） |

**验收**（一条命令查完 a/b/c/d/g）
```powershell
python cli.py 600519 --json --fast 2>$null | python -c "
import json,sys
d=json.load(sys.stdin); r=d['recommendation']; e=d['evidence']; c=d['candidates'][0]; g=d['diagnostics']
print('score_oos/is =', c['score_oos']/c['score_is'] if c['score_is'] else None)
print('distance_to_trigger =', r['distance_to_trigger'])
print('invalidation =', r['invalidation'])
print('zero_trade_configs =', g['zero_trade_configs'])
print('regime_win_rates =', e['regime_win_rates'])
assert abs((c['score_oos']/c['score_is'])-0.9)>1e-9 or c['score_oos'] is None, 'FAIL: 仍是 x0.9 伪造'
assert g['zero_trade_configs']!=0 or True
print('OK')"
```
期望：比值不再是精确 0.9（或为 None）；`invalidation` 含族名与真实天数、不再出现"10 日"这种候选数；`regime_win_rates` 非空。

**加门禁**：G20「禁止占位常量」：probe 对 **600519 与 510300 两个标的**各跑一次，断言
`distance_to_trigger`、`regime_win_rates`、`zero_trade_configs`、`invalidation` **不全等**（占位常量会导致跨标的完全相同）；
并断言 `score_oos/score_is` 不等于 0.9。

---

## F02　`--type` 参数完全空转

**缺陷**：`cli.py:171` 签名收 `type_hint='auto'`，但函数体内**从未使用**；`cli.py:176` 用 `code.startswith('5')` 判类型。
`main()` 把 `args.type` 传进去（`cli.py:389`）等于白传。

**改法**：`cli.py:176-181` 改为
```python
if type_hint == 'fund':
    data_type = 'fund'
elif type_hint == 'stock':
    data_type = 'stock'
else:                                    # auto
    data_type = 'fund' if code.startswith('5') else 'stock'
df = get_fund_daily(code, start_date=start) if data_type == 'fund' \
     else get_stock_daily(code, start_date=start, source='sina')
```
并在 `--type` 的 help 里注明"auto 按代码前缀推断（5 开头视为基金）"。

**验收**
```powershell
python cli.py 600519 --type fund --json --fast 2>$null | python -c "import json,sys; d=json.load(sys.stdin); print('type=',d['request']['type'],'board=',d['instrument']['board'])"
```
期望：`type= fund`（当前会输出 `stock`，证明参数被忽略）。
`request.type` 需回显**用户请求的** type_hint 还是**实际判定的** data_type？→ 契约未明确，本清单规定：`request.type` 回显实际判定值，并新增 `request.type_hint` 回显用户入参（改完记得同步 F21 的键集合校验与 `test_c_schema.py` 若存在）。

**加门禁**：G16「--type 生效」：`600519 --type fund` 的 `request.type` 必须为 `fund`。

---

## F21　收紧 G04（**当前是假绿的源头**）

**缺陷**：`tools/probe.py:121-138 check_fund_contract` 只校验 `schema_version=='1.0'` + 11 个顶层键；
`probe.py:52 cli_json` 接受 `exit ∈ (0,10)`。于是"数据不足的空壳文档"也能判 PASS —— 与 X1 并存了整整一轮。

**改法**（**只允许加强**，AGENTS.md §4）：`check_fund_contract` 增加断言
```python
if data.get("data", {}).get("bars", 0) <= 0:      problems.append("基金 bars=0（无有效数据）")
if not data.get("candidates"):                    problems.append("基金 candidates 为空")
if len(data.get("evidence") or {}) < 10:          problems.append(f"基金 evidence 仅 {len(data.get('evidence') or {})} 键")
st = data.get("recommendation", {}).get("status")
if st not in ("ok", "no_edge"):                   problems.append(f"基金 status={st}（期望 ok/no_edge；缓存缺失请先联网预热，见 README）")
```
同时把 G02（股票侧）也加 `bars>0` 与 `candidates` 非空的断言，保持两侧同标准。

> **顺序提醒**：F21 必须在 **F01 之后**做。否则 G04 收紧的瞬间就会因为"基金候选来自股票网格"而红，
> 你会误以为是收紧改错了。

**验收**：`pwsh -File tools/check_gates.ps1` → G04 仍 PASS，但此时它是**真的**在校验有效输出。
反向自证（临时手改一个断言阈值让它 FAIL，确认门禁会红，然后改回）—— 这一步只做一次，确认判定器不是摆设。

---

# 第二批 P1（B 阶段大面积未做）

## F14　族推断用了契约**明令禁止**的字符串切分（先做这条，F13 依赖它）

**缺陷**：`cli.py:224` `family = idx.split('(')[0].split()[0]`。
契约「关键现状事实」原文：*"B5 的族分组**必须**取自 `_grid_defs()` 的 prefix，**禁止**用字符串按 `(` 切分重新推断
（`optimizer.py::_family_of` 那种做法与 `VWAP(累计)`、`策略组合 (2 票)` 等命名不兼容）"*。

**真源已核实**：`stock_analysis/strategy/registry.py::_grid_defs()` 返回 **40 个 4 元组**，
形如 `(prefix, StrategyClass, [params...], namer_lambda)`，`prefix` 即族名（实测 40 个 prefix，581 组）。

**改法**：在 `registry.py` 新增（不要放 cli.py）
```python
def family_of(name: str) -> str:
    """name -> 族前缀。真源 = _grid_defs() 的 prefix，禁止字符串切分推断。"""
    for prefix, _cls, params, namer in _grid_defs():
        for p in params:
            if namer(p) == name:
                return prefix
    return "unknown"

def family_map() -> Dict[str, str]:
    """一次性构建 name->prefix 映射（581 键），避免每次 O(n) 扫描。"""
    ...
```
（`family_map()` 建议模块级 `functools.lru_cache`，因为 581×40 的扫描在 CLI 里会被反复触发。）
然后 `cli.py:224` 改为 `family = family_map().get(idx, 'unknown')`；
基金侧同理，在 `fund_analysis/fund_strategy/registry.py` 提供同名 `family_map()`（见 F15）。

**验收**
```powershell
python -c "
from stock_analysis.strategy.registry import family_map, _grid_defs
m=family_map(); g=_grid_defs()
print('map size', len(m), 'grid combos', sum(len(x[2]) for x in g), 'families', len({x[0] for x in g}))
bad=[k for k,v in m.items() if v=='unknown']
print('unknown:', len(bad), bad[:5])
assert len(m)==581 and not bad
for n in ['VWAP(累计)','策略组合 (2 票)']:
    print(n, '->', m.get(n, '(不在网格)'))
print('OK')"
```
期望：`map size 581`、`unknown: 0`、末行 OK。

**加门禁**：G21「族名必须来自注册表」：断言 `family_map()` 无 `unknown` 且 `len==581`；
并断言 `--json` 输出里每个 `candidates[*].family` 都 ∈ `_grid_defs()` 的 prefix 集合。

---

## F13　B5「任一族 ≤ 50%」硬约束未作用于对外 `candidates`

**缺陷**：`select_consensus()` 内部有去相关（`consensus.py:64-104`），但 `cli.py:359` 对外输出的是
`candidates[:10]`（**原始评分前 10**），去相关结果只进了 `evidence.consensus.members`。

**实测证据**
```
600519: candidates 族分布 = {StochRSI: 6, CCI: 4}   → 6 > ceil(10/2)=5   契约 B5 验证条目 FAIL
        consensus.members = ['StochRSI(14,25/75)', 'CCI(14,-150/100)']   ← members 本身 1:1 合规
510300: {配对交易:4, 价差ZScore:4, Z-Score:1, 布林:1} → 恰好合规（但族本身是错的，见 F01）
```

**改法**：`cli.py:359` 改为输出经族去相关后的列表。两种可选实现，**选 (A)**：
- **(A)** 让 `select_consensus` 返回的 `members` 补齐到 `top_k`（每族至少 1 席后，剩余名额按"当前占比最小的族优先"填充），
  `cli.py:359` 改为 `'candidates': consensus['members'][:10]`。这样 `candidates` 与 `consensus.members` 天然一致，不会再漂移。
- (B) 在 cli.py 里对 `candidates` 单独做一次 ≤50% 过滤 —— 不推荐，会造成两处逻辑。

同时 `consensus.py:80-104` 现有的"第二轮填充"逻辑（`for c in sorted_cands: if c not in final_members`）**没有检查族占比**，
填完可能又超 50%；(A) 实现时必须在填充循环里加占比约束。

**验收**
```powershell
python cli.py 600519 --json --fast 2>$null | python -c "
import json,sys,collections,math
d=json.load(sys.stdin); c=d['candidates']
f=collections.Counter(x['family'] for x in c); n=len(c)
print('n=',n,'fam=',dict(f),'limit=',math.ceil(n/2))
assert c and all(v<=math.ceil(n/2) for v in f.values()), 'FAIL: 族占比超 50%'
print('members==candidates:', [m for m in d['evidence']['consensus']['members']]==[x['name'] for x in c])
print('OK')"
```
期望：`OK`，且 `members==candidates: True`。
另外补一条 pytest（契约 B5 原始验证）：构造 Top12 中某族占 7 席的评分表，断言 `select_consensus()` 输出该族占比 ≤ 50%。

**加门禁**：G22「B5 族占比」：对 600519 与 510300 都断言任一族计数 ≤ `ceil(len(candidates)/2)`。

---

## F10　B7 的 bootstrap CI 已实现但**未接线**到输出

**缺陷**：`decision/scoring.py:42 bootstrap_ci(values, stat_fn, n_boot=2000, seed=7) -> (lower, upper)` 已实现且确定性（固定 seed）；
`cli.py:343-353` 的 9 个 metric **全部只有 `point`，0 个有 `ci_95`**（实测 `含 ci_95 的 metric 数 = 0/9`）。
`decision/schema.py:116-119 MetricsItem` 早就声明了 `ci_95: Optional[List[float]]`，只是没人填。

**改法**：`cli.py:343-353` 为每个可从序列重算的指标接上 CI。数据源用 `result.equity_curve`（`engine.py:51/226` 已暴露）
与 `result.trades`：
```python
from decision.scoring import bootstrap_ci, equity_to_returns, point_stat
rets = equity_to_returns(list(getattr(result, 'equity_curve', []) or []))
nb = 400 if fast else 2000
def _ci(vals, fn):
    if not vals or len(vals) < 2: return None
    lo, hi = bootstrap_ci(vals, fn, n_boot=nb, seed=7)
    return [round(lo, 6), round(hi, 6)]
# annual_return / sharpe / max_drawdown 用 rets；win_rate / profit_factor / avg_holding_days 用逐笔 pnl_pct
pnl = [t.pnl_pct for t in getattr(result, 'trades', [])]
```
`trade_count` 是计数不是统计量 → `ci_95` 保持 `None` 并在 `warnings` 说明"计数型指标不适用 bootstrap"（**不要硬造**）。
`calmar` = annual/|mdd|，可用同一批 bootstrap 样本内的比值分布算。

**验收**
```powershell
python cli.py 600519 --json --fast 2>$null | python -c "
import json,sys
m=json.load(sys.stdin)['evidence']['metrics']
have=[k for k,v in m.items() if v.get('ci_95')]
print('有 ci_95:', len(have), '/', len(m), have)
assert len(have)>=5, 'FAIL: CI 未接线'
for k in have: assert m[k]['ci_95'][0] <= m[k]['point'] <= m[k]['ci_95'][1] or k in ('max_drawdown',), (k, m[k])
print('OK')"
```
期望：≥5 项有 CI，末行 OK。
再补契约 B7 的两条 pytest：(a) 同输入两次调用 CI 完全相同；(b) 交易数 10 的 `win_rate` CI 宽度 **严格大于** 交易数 200 的。

**加门禁**：G23「metrics CI 覆盖率 ≥ 5/9 且点估计落在 CI 内」。

---

## F11　A4 `time_in_market` 是死码（`holding_bar_count` 从未被赋值）

**缺陷**：`engine.py:218-219`
```python
if len(df) > 0 and hasattr(result, "holding_bar_count"):
    result.time_in_market = result.holding_bar_count / len(df)
```
`holding_bar_count` **全仓库只有这两处引用，从未被赋值**（实测 grep 仅命中 `engine.py:218`、`engine.py:219`），
且它不是 `BacktestResult` 的 dataclass 字段（实测 `time_in_market 字段: False`）。
→ `hasattr` 恒 False → `time_in_market` 永不设置 → `cli.py:352` 的 `hasattr(...) else 0` 兜底 → **输出恒 `{"point": 0}`**（股票与基金实测都是 0）。

**改法**
1. `engine.py:45-69` 的 `BacktestResult` 增加两个**声明字段**（注意 F19 的重复声明问题，加在 `calmar_ratio`/`sortino_ratio` 附近）：
   `holding_bar_count: int = 0` 与 `time_in_market: float = 0.0`。
2. 在 `run_backtest` 的逐 bar 循环里累加：持仓期间（`position > 0`）每根 bar `result.holding_bar_count += 1`。
   循环体在 `engine.py:119-214` 区间，注意 `equity_curve.append(cash + position*close)`（第 201 行）那一支是持仓分支。
3. `engine.py:218` 的 `hasattr` 守卫可以保留（向后兼容），但因为字段已声明，条件会真正生效。
4. `cli.py:352` 保持不变即可（此时 `hasattr` 为 True）。

**验收**（契约 A4 原始验证：前 100 根持仓、后 300 根空仓 → ≈0.25）
```powershell
python -c "
import numpy as np, pandas as pd
from stock_analysis.backtest.engine import BacktestEngine, BacktestConfig
# 构造一段必然在前段持仓、后段空仓的合成数据 + 信号
n=400
idx=pd.date_range('2020-01-01',periods=n,freq='B')
close=np.concatenate([np.linspace(10,12,100), np.linspace(12,10,300)])
df=pd.DataFrame({'open':close,'high':close*1.01,'low':close*0.99,'close':close,'volume':1e6},index=idx)
buy=pd.Series(False,index=idx); sell=pd.Series(False,index=idx); buy.iloc[0]=True; sell.iloc[100]=True
r=BacktestEngine(BacktestConfig(initial_capital=100000,max_position_pct=1.0)).run_backtest(df,lambda d:{'buy':buy,'sell':sell})
print('holding_bar_count=',r.holding_bar_count,'time_in_market=',round(r.time_in_market,4),'trades=',r.trade_count)
assert abs(r.time_in_market-0.25)<=0.02, 'FAIL'
print('OK')"
```
期望：`time_in_market ≈ 0.25`、末行 OK。若合成信号无法精确产生 100 根持仓，**调整合成数据而不是放宽容差**（AGENTS.md §4）。
再跑 `python cli.py 600519 --json --fast`，确认 `evidence.metrics.time_in_market.point` **不再是 0**。

**加门禁**：G24「time_in_market 非恒 0」：600519 实测值 > 0；且上面那条 pytest 进测试网。

---

## F12　`decision/sizing.py` 不存在；止损/止盈/入场区全硬编码，且与回测配置互相矛盾

**缺陷**
- 契约「新增模块布局」明列 `source/decision/sizing.py` 负责"仓位/止损/止盈/入场区/horizon 推导"。**实测该文件不存在**，`cli.py` 也从不引用 `sizing`。
- `cli.py:285` `stop_distance = 0.05` 硬编码 → `cli.py:286` 的仓位公式建立在假止损距离上。
- `cli.py:324-326`：`entry_zone = [last_close*0.98, last_close*1.02]`、`stop_loss = last_close*0.95`、`take_profit = last_close*1.10` —— 全是固定百分比。
- **矛盾点**：回测实际用的是 `BacktestConfig.stop_loss_pct=0.08` / `take_profit_pct=0.15`（`engine.py:38-39`，`cli.py:194` 未覆盖）。
  → 对外报 5% 止损 / 10% 止盈，回测里跑的是 8% / 15%。实测 510300：`last_close=4.236`，报 `stop_loss=4.02`（=×0.95）、`take_profit=4.66`（=×1.10）。
  → **Hermes 拿到的止损价与本项目的回测结论不是同一个东西。**

**改法**
1. 新建 `source/decision/sizing.py`，把 `cli.py:284-286` 与 `324-326` 的逻辑全部搬进去，提供纯函数：
   ```python
   def derive_stops(last_close, atr=None, config=None) -> dict   # {'stop_loss','take_profit','stop_distance','method'}
   def derive_entry_zone(last_close, atr=None) -> list           # [lower, upper]
   def derive_horizon(avg_holding_days) -> list                  # [lo, hi]
   def derive_position_fraction(confidence, stop_distance, cap=0.30) -> float
   ```
   顶部用具名常量（`RISK_PER_TRADE = 0.01`、`MAX_POSITION_FRACTION = 0.30`、`ENTRY_ZONE_ATR_MULT` 等）。
2. `stop_distance` 必须与**回测实际使用**的止损一致：无 ATR 时用 `config.stop_loss_pct`（0.08），**不是** 0.05。
3. F08 落地后，`derive_stops` 优先用 ATR（`method='atr'`），否则回落到 `config.stop_loss_pct`（`method='fixed'`），并把 `method` 写进输出便于 Hermes 判断。
4. `cli.py` 只做调用与装配，不得再留任何 `last_close * 0.95` 这类字面量。

**验收**
```powershell
python -c "import decision.sizing as s; print([n for n in dir(s) if n.startswith('derive_')])"
python cli.py 600519 --json --fast 2>$null | python -c "
import json,sys
d=json.load(sys.stdin); r=d['recommendation']; lc=d['data']['last_close']
print('last_close',lc,'stop',r['stop_loss'],'tp',r['take_profit'],'entry',r['entry_zone'],'horizon',r['horizon_days'])
assert abs(r['stop_loss']-lc*0.95)>1e-6, 'FAIL: 仍是 ×0.95 硬编码'
assert abs(r['take_profit']-lc*1.10)>1e-6, 'FAIL: 仍是 ×1.10 硬编码'
assert 0 <= r['position_fraction'] <= 0.30
print('OK')"
```
期望：`decision.sizing` 可导入且有 4 个 `derive_*`；止损/止盈不再等于 `×0.95`/`×1.10`；末行 OK。

**加门禁**：G25「sizing 单一真源」：`Select-String -Path source/cli.py -Pattern 'last_close \* 0\.9|last_close \* 1\.1|stop_distance = 0\.05'` 必须 **0 处匹配**；且 `decision/sizing.py` 存在。

---

## F07　B1 评分公式**根本没换**（Wilson 胜率项仍主导）

**缺陷**：`decision/scoring.py:14 risk_adjusted_score` 已实现（Calmar 主项 0.6 / Wilson 0.2 / 稳定性 0.2），
但 **`analyzer.py` 从未调用它** —— `cli.py:18` 以 `# noqa: F401`（未使用）导入，是死码。
真正在跑的仍是契约 B1 点名要替换的旧公式：
```
analyzer.py:218-223  score_backtest_result: adj*0.5 + annual_return*0.2 - max_drawdown*0.3   （docstring 原文就写着这个公式）
analyzer.py:226-230  score_oos_result:      adj*0.5 + annual_return*0.2 - max_drawdown*0.3
analyzer.py:284/296  同一套（注释 + _wilson_lower_bound）
```
基线快照记录的后果仍在：**Top12 中 Wilson 胜率项占 |总分| 72.8%**；
`StochRSI(9,25/80)` 年化 12.65%/回撤 34.96%/103 笔 排名**低于** `StochRSI(14,25/75)` 年化 10.14%/回撤 6.78%/32 笔。

**改法**
1. `analyzer.py:218-223` 与 `226-235` 改为委托 `decision.scoring.risk_adjusted_score`；
   **保留** `trade_count < MIN_TRADE_COUNT → -100.0` 与 `< OOS_MIN_TRADES → -100.0` 的地板判定（`consensus.py:44` 与 `cli.py:269` 都依赖 `-100` 这个约定，改了会连锁）。
2. 保留 Calmar 软惩罚（`analyzer.py:231-235`）如果它仍被 OOS 逻辑需要，但要在 `scoring.py` 顶部集中权重常量，**不得两地各写一套**。
3. 注意 `risk_adjusted_score` 第 20 行有可疑实现：`annual_return = (1+total_return)**(252/max(1,252)) - 1` —— `252/252` 恒等于 1，即 `annual_return = total_return`，**年化根本没做**。要么传入真实 bar 数做年化，要么把参数名改成 `annual_return` 直接接收年化值。这个 bug 会让 F07 改完后评分仍然失真，**必须一并修**。
4. `test_p1_oos.py` 可能断言了旧评分的相对顺序 → 先跑 pytest，若红，**判断是断言依赖了被契约明确要求替换的公式**（属 AGENTS.md §4 的例外情形），此时可改断言，但**必须在 PROGRESS.md 写清"错在哪、为什么、怎么算才对"**。

**验收**（契约 B1 原始验证）
```powershell
python -c "
from decision.scoring import risk_adjusted_score as s
A=s(total_return=0.12,max_drawdown=-0.35,win_rate=0.60,trade_count=100)
B=s(total_return=0.10,max_drawdown=-0.07,win_rate=0.55,trade_count=100)
print('A(年化12%%/回撤35%%)=%.4f  B(年化10%%/回撤7%%)=%.4f  B-A=%.4f'%(A,B,B-A))
assert B>A and (B-A)>0.05, 'FAIL'
print('OK')"
```
期望：`B > A` 且差值 > 0.05，末行 OK。
再加契约要求的第二条：对 600519 实测 Top12，**胜率项对 |总分| 的贡献占比 ≤ 40%**（before = 72.8%）—— 这条要写个脚本把新公式的三项分量拆出来算占比，结果记入 `docs/validation_600519.md`。

**加门禁**：G26「胜率项贡献 ≤ 40%」：probe 调 `risk_adjusted_score` 分量拆解，断言占比 ≤ 0.40。

---

## F08　B2 波动率自适应止损止盈**完全未实现**

**缺陷**：`Select-String -Path stock_analysis\backtest\*.py -Pattern 'atr|stop_mode'` → **0 处匹配**。
`BacktestConfig`（`engine.py:31-41`）只有 `stop_loss_pct=0.08` / `take_profit_pct=0.15` / `enable_stop_loss` / `enable_take_profit`，
**没有** 契约要求的 `stop_mode: str = "atr"`、`atr_period: int = 14`、`atr_stop_mult: float = 2.5`、`atr_profit_mult: float = 4.0`。
→ 契约 B2 整条未做，且它是 F12 的前置（sizing 要有真 ATR 才能推导）。

**改法**
1. `engine.py:31-41` `BacktestConfig` 增加 4 个字段，默认值按契约（`stop_mode="atr"` 即**默认启用**）。
2. **ATR 已有现成实现，不要重写**（本节初稿写错了，据实更正）：`source/stock_analysis/indicators/volatility.py:25`
   `calc_atr(high, low, close, period=14)`，并已在 `stock_analysis/indicators/__init__.py:31` 导出、`trend.py:130` 有使用先例。
   实测其实现：`tr2/tr3` 已正确用 `close.shift(1)`（无 look-ahead）；`atr = tr.rolling(period).mean()` **含当根 TR**。
   → 在"收盘后决策、次日成交"的引擎口径下含当根 TR 不算 look-ahead，但**你必须在接线时显式确认取值时点**：
   止损距离只能用**入场当根收盘时已知**的 ATR（即 `atr.iloc[entry_i]`），绝不能在持仓期间用未来根的 ATR 回溯改写止损。
   若改成盘中/当根即用，则必须补 `.shift(1)`。补一条单测锁定这个时点语义。
3. `run_backtest` 里止损/止盈判定（`engine.py:124-131` 附近，即 F-涨跌停与止损那块）改为：
   `stop_mode=='atr'` 时用 `entry_price - atr_at_entry*atr_stop_mult` / `entry_price + atr_at_entry*atr_profit_mult`；
   `=='fixed'` 时走原 `stop_loss_pct`/`take_profit_pct` 路径（**契约要求保留 fixed 作回归保护**）。
4. 与 A11 的跳空规则合并：止损成交价仍须 `min(stop_price, open_px)`（经滑点调整），止盈 `max(...)`。

**验收**（契约 B2 原始验证）
```powershell
python -m pytest tests/ -q     # 先确认没打破现有 28 项
```
再新增 pytest：同一策略在同一合成序列的**高波动段**与**低波动段**上，断言实际止损距离不同（差异 > 20%）；
并断言 `stop_mode="fixed"` 时行为与旧逻辑**逐笔一致**（回归保护）。
最后跑 `pwsh -File tools/check_gates.ps1 -Quick` 确认 G06（三窗口一致）没被 ATR 破坏 —— ATR 引入后末根值会随窗口变化，
**若 G06 变红，说明 F14/F13 的族级共识还不够稳，要回去加滞回，不得放宽 G06 断言**。

**加门禁**：G27「ATR 止损生效」：断言 `BacktestConfig().stop_mode == 'atr'`，且高/低波动段止损距离差异 > 20%。

---

## F09　B3 walk-forward 仍是"固定策略重跑子窗口"，没有逐折重新寻优

**缺陷**：`analyzer.py:201 def walk_forward_splits(df, n_splits=3)` —— 名称与语义都未改；
`analyzer.py:316` 取折，`analyzer.py:328-333` 对**已经在 IS 选定的固定候选**逐折重跑并 `score_oos_result(r_fte)`。
契约 B3 原文：*"每折必须在该折训练集上**独立重新选参**，再用该折测试集评估所选参数；**不得继续用固定策略重跑子窗口**。函数命名须反映语义（如 `walk_forward_optimize`）"*。
→ 这就是上轮诊断的"README 宣称 3 折 walk-forward 防过拟合名不副实"，**至今未修**，对参数选择的过拟合零防护。

**改法**
1. 新增 `walk_forward_optimize(df, strategies, config, n_splits=3, top_n=...)`：
   每折 → 在该折 **train 段**上对全网格（或 IS Top-N 子集，为性能考虑）评分选优 → 用选出的参数在该折 **test 段**回测 → 汇总各折 test 表现。
2. `analyzer.py:316/328-333` 的旧路径改为调用新函数；`walk_forward_splits` 可保留为纯切分工具（被新函数复用），但**对外语义由新函数承担**。
3. 性能：全网格 × 3 折会显著变慢，G14（≤300s）可能红。对策：`--fast` 下折数降到 2 或只在 IS Top-20 上逐折重选（契约 D5 明确允许 `--fast` "跳过部分 walk-forward 折"）。**必须实测两档耗时并写进 README**（D5 要求）。

**验收**（契约 B3 原始验证）
```powershell
python -m pytest tests/ -q
```
新增 pytest：用两段统计特性明显不同的合成序列拼接（前段趋势、后段均值回归），
断言**不同折选出的参数不完全相同**（至少一折的 best 与全局 best 不同）。
再跑 `pwsh -File tools/check_gates.ps1 -Full` 确认 G14 仍 ≤300s、G12 `--fast` 仍 ≤60s。

**加门禁**：G28「walk-forward 逐折重选」：上面那条 pytest 进测试网即算覆盖；另在 probe 里断言 `walk_forward_optimize` 可导入。

---

# 第三批 P2

## F16　D6 恢复三个消失的旧参数

**缺陷**：`cli.py:376-382` 的 argparse 只有 7 个参数。实测 `--detail` / `--no-regime` / `--opt-iters` **三个全部 MISSING**，旧调用方会直接 usage error（退出码 2）。

**改法**：补回三个 `add_argument`：
- `--detail`（`action='store_true'`）：非 JSON 模式下输出详细文本（触发器、候选表、指标）。
- `--no-regime`（`action='store_true'`）：跳过 `market_regime` 计算，输出 `{'label':'Unknown','confidence':0.0,'definition':'disabled'}`（**不要**假装 Bull）。
- `--opt-iters`（`type=int, default=30`）：接到 `cli.py:200` 的 `iters=`，替换现在硬编码的 `12 if fast else 30`（`--fast` 时可取 `min(args.opt_iters, 12)`）。
三者都要透传进 `advise_internal`（新增形参，保持 `advise()` 与 `--json` 键集合一致 → 会触发 G07，记得同步 `decision/__init__.py::advise` 的签名）。

**验收**（契约 D6 原始验证：每个旧参数各跑一次，退出码不得为 2）
```powershell
foreach($a in @('--detail','--no-regime','--opt-iters 8','--start 20220101','--jobs 2','--opt grid','-t stock','--json','--fast')){
  $arg=$a -split ' '
  python cli.py 600519 @arg --fast *> $null
  "$a -> exit $LASTEXITCODE"
}
```
期望：全部 ∈ {0,10,20,30,40}，**不得出现 2**。

**加门禁**：G29「旧参数不报 usage error」：把上面这个循环搬进 probe。

---

## F17　D3(e) `requirements.txt` 与实测环境不一致

**缺陷**（契约 D3(e) 点名的正是这一条，至今未做）
```
source/requirements.txt : akshare==1.17.44  numpy==2.3.5  pandas==3.0.6  pytest==9.1.1  requests==2.32.3
实测通过全部门禁的环境   : akshare  1.18.29  numpy  2.2.6  pandas  3.0.1  pytest  8.3.5  Python 3.12.10
```
`pip install -r requirements.txt --dry-run` **能成功解析并下载**（实测 `Would install ... akshare-1.17.44 numpy-2.3.5 pandas-3.0.6 pytest-9.1.1`），
所以不是装不上，而是 **Hermes 会装上一套从未跑过门禁的版本组合**。

**改法**：把 5 个 pin 改成实测值（`akshare==1.18.29 / numpy==2.2.6 / pandas==3.0.1 / pytest==8.3.5 / requests` 用 `pip show requests` 实查），
并在文件头加注释说明"这些版本 = 通过 `tools/check_gates.ps1 -Full` 14/14 的实测环境（Python 3.12.10，2026-10-17）"。
同步核对 README 里声明的 Python 版本（D3(f)）。

**验收**
```powershell
python -c "
import re,subprocess,sys,importlib
pins=dict(re.findall(r'^([a-zA-Z0-9_\-]+)==([\d\.]+)', open('requirements.txt',encoding='utf-8').read(), re.M))
import akshare,numpy,pandas,pytest,requests
act={'akshare':akshare.__version__,'numpy':numpy.__version__,'pandas':pandas.__version__,'pytest':pytest.__version__,'requests':requests.__version__}
bad={k:(v,act.get(k)) for k,v in pins.items() if act.get(k) and v!=act[k]}
print('pins',pins); print('actual',act); print('mismatch',bad); assert not bad, 'FAIL'; print('OK')"
```
期望：`mismatch {}` + `OK`。

**加门禁**：G30「requirements 与实测环境一致」：上面这段搬进 probe。

---

## F15　基金 `grid_size()` 是硬编码算式，三个数字互相矛盾

**缺陷**：`fund_analysis/fund_strategy/registry.py::grid_size()` 全文就是一行算式，**且注释自己承认对不上契约**：
```python
def grid_size() -> int:
    # RSI 4×4 + Double MA 3×3 + MACD 2×2×2 + Vol Target 3×2 + Index Rotation 2×2
    return 4*4 + 3*3 + 2*2*2 + 3*2 + 2*2  # = 16+9+8+6+4 = 43 (≈44 as per contract)
```
实测三方矛盾：`grid_size()` = **43**、`build_fund_strategy_signals()` 实际返回 **40**、契约/AGENTS.md §7 要求 **44**。
差额定位到 VolTarget：算式按 `3*2=6`，实际只生成 **3** 个。族数实测 **5**（RSI 16 / 双均线 9 / MACD 8 / VolTarget 3 / IndexRotation 4），契约要求 **6**。
另外 registry **没有 `_grid_defs()`**（只有 `build_fund_strategy_signals` + 5 个 `generate_*` + `grid_size`），
而契约 C7 明确要求"提供与股票侧**同构**的 `_grid_defs()`（含族前缀）与 `build_fund_strategy_signals()`，使 B5 共识逻辑可直接复用"。

**改法**（二选一，**必须在 PROGRESS.md 与最终报告里写明选了哪条及理由**）
- **方案 A（推荐，补齐到契约数字）**：把 VolTarget 补成 `3×2=6` 组 → 总 43；若还要凑 44 与 6 族，需确认契约那个"44/6 族"当初是怎么数出来的（可能是把某族多算了一组）。**不要为了凑 44 而硬塞无意义参数组合**。
- **方案 B（改文档对齐实现）**：以真实网格为准，`grid_size()` 改为 `len(build_fund_strategy_signals())`，然后同步修改 `GOAL_CONTRACT.md` C7 的 44/6、`AGENTS.md §7` 的"44 组"、README 中所有相关数字。
  契约 Boundaries 允许"不改变网格组数（基金 44）"，所以选 B **必须显式记录为范围变更 + 理由**（AGENTS.md §1：缩小范围要写明放弃条目/理由/批准人）。

无论选哪个，都要：
1. 新增 `_grid_defs()`，返回与股票侧同构的 4 元组 `(prefix, class_or_callable, [params...], namer)`；
2. `grid_size()` 改为 `sum(len(g[2]) for g in _grid_defs())`（**派生，不再是硬编码算式**）；
3. 新增 `family_map()`（供 F14 的基金侧使用）；
4. 删掉 `# = 43 (≈44 as per contract)` 这种"约等于契约"的注释 —— 它就是假绿的语言证据。

**验收**
```powershell
python -c "
from fund_analysis.fund_strategy import registry as r
g=r._grid_defs(); s=r.build_fund_strategy_signals()
print('grid_size()',r.grid_size(),'| _grid_defs 组合数',sum(len(x[2]) for x in g),'| signals',len(s),'| families',len({x[0] for x in g}))
assert r.grid_size()==sum(len(x[2]) for x in g)==len(s), 'FAIL: 三方仍不一致'
print('OK')"
```
期望：三个数字**完全相等**，末行 OK。
若 `test_*` 里有断言 44 的（先 `Select-String -Path tests\*.py -Pattern '44|grid_size'` 查），按所选方案同步处理，并在 PROGRESS.md 说明。

**加门禁**：G31「基金网格单一真源自洽」：上面那条 assert 搬进 probe。

---

## F18　D1 清理未做完

**缺陷**（实测 `Select-String source\**\*.py` 命中 4 处，全在同一个文件）
```
source/fund_analysis/utils.py:11  CONFIG_PATH = os.path.join(..., 'fund_config.json')
source/fund_analysis/utils.py:14  def load_config():
source/fund_analysis/utils.py:24  def save_config(config):
source/fund_analysis/utils.py:31  FUND_CONFIG = load_config()
```
另外 `source/fund_analysis/fund_config.example.json` **仍存在**（契约 D1 要求删除）。
门禁 G11 只扫 `cli.py`（`check_gates.ps1:87`），所以扫不到 → 判 PASS。

> **有意豁免，不要动**：`source/stock_analysis/strategy_master/{config,runner}.py` 的保留是 AGENTS.md §5 记录的
> 契约冲突处理（D1 要删 vs `tests/test_p2_engineering.py` 要求其存在），带 DeprecationWarning 的薄委托垫片是**正确做法**。

**改法**
1. 先查引用：`Select-String -Path source\**\*.py -Pattern 'FUND_CONFIG|from .utils import|from fund_analysis.utils import'`。
2. `utils.py` 里的 `CONFIG_PATH/load_config/save_config/FUND_CONFIG` 若**零引用** → 直接删除；若有引用 → 把引用方改成显式传参或常量，再删。
3. 删除 `source/fund_analysis/fund_config.example.json`（`git rm`）。
4. 确认 `.gitignore` 里 `source/fund_analysis/fund_config.json` 那条保留（那是本地真实配置，本就不该入库）。

**验收**（契约 D1 原始验证命令，注意范围是整个 `source/`）
```powershell
Select-String -Path source\*.py,source\**\*.py -Pattern 'watchlist|save_config|load_config|stock_config|fund_config' | Where-Object { $_.Path -notmatch 'strategy_master' } | ForEach-Object { "$($_.Filename):$($_.LineNumber): $($_.Line.Trim())" }
python -c "import cli; print('cli import OK')"
python -m pytest tests/ -q
```
期望：第一条**无输出**（strategy_master 豁免除外）；`cli import OK`；`28 passed`。

**加门禁**：把 G11 的扫描范围从 `$CliPy` 扩大到 `source/**/*.py`，并加 strategy_master 白名单。**这是加强，不是弱化。**

---

## F19　`BacktestResult` 有两个字段被重复声明

**缺陷**：`engine.py:66-69`
```
66:    unaffordable_lot_events: int = 0
67:    blocked_by_limit_events: int = 0
68:    unaffordable_lot_events: int = 0      ← 重复
69:    blocked_by_limit_events: int = 0      ← 重复
```
dataclass 里后者覆盖前者，功能上不致命，但属重构残留，且会让 `__init__` 签名出现重复参数名（Python 允许但极易埋坑）。

**改法**：删掉 `engine.py:68-69` 两行。**改完立刻跑 pytest** —— `test_p0_correctness.py` 里有 A3 事件计数相关断言。

**验收**
```powershell
python -c "
from stock_analysis.backtest.engine import BacktestResult as B
f=list(B.__dataclass_fields__)
dup=[x for x in set(f) if f.count(x)>1]
print('fields',len(f),'dups',dup); assert not dup; print('OK')"
python -m pytest tests/ -q
```
期望：`dups []` + OK + `28 passed`。

**加门禁**：并入 G01（pytest）即可，或加一条 dataclass 无重复字段的静态断言。

---

## F20　基金缓存策略：失败拉取会留下空壳文件，且 Hermes 首跑必须联网

**缺陷**（这就是上轮 X1 的真因）
1. `stock_analysis/data/fund_510300.csv` 曾是 **56 字节**（`date,open,high,low,close,volume,amount,postVol,postAmt` + 换行，**零数据行**）—— 某次失败拉取写下的空壳。它导致 `cli.py 510300 --json` 返回 `insufficient_data`。我触发一次成功拉取后变为 **97509 字节 / 1643 行**，CLI 立刻恢复 exit 0。
2. `stock_analysis/data_loader/meta.py::get_fund_daily` 的逻辑是：读缓存 → `if not df.empty` 且末日够新则直接返回 → 否则触网。**空壳文件不会造成永久卡死**（因为 `df.empty` 会触发重拉），这点是好的；但**失败时仍可能再次写下空壳**。
3. `.gitignore` 排除了 `source/stock_analysis/data/` 与 `source/fund_analysis/data/` → **Hermes 克隆后缓存全空，首次运行必须联网**。这是设计意图（契约 Boundaries 明确"沿用现有本地 CSV 缓存与 .gitignore 策略"），但 README 必须讲清楚，否则 Hermes 会以为项目坏了。

**改法**
1. `meta.py` 的两个写缓存点（`get_stock_daily` / `get_fund_daily` 里的 `to_csv`）加守卫：**只有 `df is not None and not df.empty and len(df) >= 某最小行数` 才写盘**；拉取失败时**不得创建/覆盖**缓存文件，并把失败原因打到 stderr。
2. 若已存在空壳文件，读取时视为"无缓存"并**删除该空壳**，避免反复读到 0 行。
3. `cli.py` 层：数据层返回 `None` → `data_source_error`（exit 30，配合 F05）；返回非 None 但 `len(df) < 100` → `insufficient_data`（exit 20）。
4. README 新增「首次运行 / 离线预热」小节，给出预热命令与所需标的清单，例如：
   ```
   # 首次运行需联网预热缓存（之后可完全离线）
   cd source
   python -c "from stock_analysis.data_loader import get_stock_daily, get_fund_daily; \
   [get_stock_daily(c) for c in ['600519','688981','002371','300750']]; get_fund_daily('510300')"
   ```
   并说明：**无缓存 + 无网络 → 退出码 30**；**有缓存 → 完全离线可用**（pytest 就是离线全绿的）。

**验收**
```powershell
# 1. 空壳不得再被写出
Remove-Item stock_analysis\data\fund_999999.csv -ErrorAction SilentlyContinue
python -c "from stock_analysis.data_loader import get_fund_daily; print(get_fund_daily('999999'))" 2>$null
Test-Path stock_analysis\data\fund_999999.csv      # 期望 False
# 2. 预热后离线可用（拔网或用不存在的 source 模拟）
python cli.py 510300 --json --fast 2>$null | python -c "import json,sys; d=json.load(sys.stdin); print('bars',d['data']['bars'],'status',d['recommendation']['status'])"
```
期望：`Test-Path` 为 `False`；预热后 `bars 1643 / status ok`。

**加门禁**：G32「失败拉取不写空壳缓存」：probe 对一个必然失败的代码跑一次，断言缓存文件未被创建。

---

# Definition of Done（全部修完后逐条跑，贴原始输出）

在 `D:\git\stock_trading_helper` 下：

```powershell
$env:PYTHONIOENCODING='utf-8'; [Console]::OutputEncoding=[System.Text.UTF8Encoding]::new($false)

# 1. 测试网离线全绿
cd source; python -m pytest tests/ -q                      # 期望 0 failed / 0 error，通过数 ≥ 28（新增用例后应更多）

# 2. 完整门禁（含性能项）
cd ..; pwsh -File tools/check_gates.ps1 -Full              # 期望 PASS ≥ 14+新增项 | FAIL 0 | BLOCKED 0 | SKIP 0 → 退出码 0
Get-Content gates-report.json | python -c "import json,sys; d=json.load(sys.stdin); print(d['summary'])"

# 3. 股票侧真实输出自洽
cd source; python cli.py 600519 --json --fast 2>$null | python -c "import json,sys,collections,math; d=json.load(sys.stdin); r=d['recommendation']; e=d['evidence']; c=d['candidates']; f=collections.Counter(x['family'] for x in c); print('status',r['status'],'action',r['action'],'n_trials',e['n_trials'],'dsr',e['deflated_sharpe'],'sig',e['significant'],'fam',dict(f),'ci_metrics',sum(1 for v in e['metrics'].values() if v.get('ci_95')),'tim',e['metrics']['time_in_market']['point']); assert e['n_trials']>=500; assert all(v<=math.ceil(len(c)/2) for v in f.values()); assert sum(1 for v in e['metrics'].values() if v.get('ci_95'))>=5; assert e['metrics']['time_in_market']['point']>0; assert r['no_edge']==(r['status']=='no_edge'); print('OK')"

# 4. 基金侧必须来自基金网格
python cli.py 510300 --json --fast 2>$null | python -c "import json,sys; from fund_analysis.fund_strategy.registry import build_fund_strategy_signals as b; d=json.load(sys.stdin); f=set(b()); n=[x['name'] for x in d['candidates']]; print('bars',d['data']['bars'],'n',len(n)); assert n and all(x in f for x in n); print('OK')"

# 5. 退出码分支互不相同
foreach($c in @('600519','999999')){ python cli.py $c --json --fast *> $null; "$c -> $LASTEXITCODE" }   # 期望 0 与 30

# 6. 幂等（G05 已覆盖，人工再确认一次）
# 7. 三窗口一致（G06 已覆盖）
# 8. 旧参数不报 usage error → 见 F16 验收循环
# 9. requirements 与实测环境一致 → 见 F17 验收
# 10. 文档同步：README 的 schema 字段表 / 退出码表 / 两档实测耗时（D5）/ 首次联网预热说明（F20）/ Python 版本（D3f）
```

**最终报告要求**：逐条列出 F01–F21 的实际执行结果（命令 + 原始输出片段 + 退出码 + 时间）。
没跑过的写"**未验证**"，**不要写 ✅**（AGENTS.md §4）。
任何被放弃的条目必须在 `PROGRESS.md` 写明"放弃条目 + 理由 + 批准人"。

---

# 附：本轮已确认**真实通过**的项（不要重复劳动）

| 项 | 证据 |
|---|---|
| G01 测试网 | `pytest tests/ -q` → **28 passed in 16.49s**，离线 |
| 完整门禁 | `check_gates.ps1 -Full` → **PASS 14 / FAIL 0 / BLOCKED 0 / SKIP 0，退出码 0**（2026-10-17 22:56:28） |
| A1/G08 可负担不变式 | `unaffordable_lot_events=0`；`position_fraction=0.146 ∈ [0,0.30]` |
| A6 基金单一成本模型 | `Select-String 'position = cash / price' source/` → **0 处匹配** |
| A8 单一 regime 定义 | `StrongBull|Crash` 仅出现在测试函数名 `test_advisor_no_crash`，非 regime 标签 |
| C1 纯 JSON stdout（股票） | 7200 字节合法 JSON，`schema_version=1.0`，无 NaN/Inf |
| C3/C7 schema 同构 | 真实基金数据下重测：`仅股票有(0) / 仅基金有(0)` → **完全一致** |
| C6 单一真源 API | `from decision import advise`，键集合与 `--json` 一致（G07 PASS） |
| C8 幂等 | G05 PASS（两次运行剔除时间戳后逐字节相同） |
| B6 三窗口稳定 | G06 PASS（full/drop60/drop120 的 action 与 family 一致） |
| D3(b) README 无不存在的组合 | `Select-String '5-13-34' README.md` → 0 处 |
| D4 输出数据驱动 | `if best_name` **0 处**；`cli.py` **418 行** ≤ 700 |
| D5 性能分档 | G12 `--fast` ≤60s、G14 全量 ≤300s 均 PASS |
| B4 的 DSR **公式本身** | Bailey–López de Prado 完整实现，无 scipy；实测单调性 1→0.9926 / 10→0.8061 / 100→0.4635 / 581→0.2553 正确（问题只在 F03 的分母口径） |
| B5 的无效候选过滤（T4） | `consensus.py:44` 按 `score > -100` 过滤，`cli.py:247/269` 已接线（台账曾误记为未完成，已更正） |
| B8 的超额收益计算 | `_excess_vs_benchmark` 是真实配对 bootstrap（`point=-5.70e-05, ci_95=[-7.45e-04,6.82e-04], p_positive=0.4975, n_obs=1642`），非硬编码（问题只在 F04 的判定未接线） |

---

# 0.5 复核后追加修复（2026-10-18 09:0x–09:4x，门禁 32 → 34）

主回合独立复核提出的 4 条遗留 + 1 条风险，本轮逐条处理。**全部只加强，未放宽任何阈值、未删除任何断言。**

| # | 问题（复核提出） | 处理 | 门禁 | 实测 |
|---|---|---|---|---|
| ① | `tools/check_gates.py` 是分叉的第二裁定器，在权威脚本判 26 的同一份代码上判 32/0 | **删除该文件**；`validation_snapshot.py` 复现命令改回权威脚本 | — | `git rm` 完成；无残留引用 |
| ② | G29 只做进程内 `parse_args()`，弱于契约 D6 的"subprocess 真跑 + returncode ∈ {0,10,20,30,40}" | 改为 8 个旧参数各启动**真实子进程**（`--start 20260701` 压到 72 根秒级返回），并加 `--detail` 行数、`--json` 纯 JSON 两条行为断言 | **G29** | 8/8 `returncode=20`；`detail 22 行 > base 10 行` |
| ③ | `test_p3_bayes` docstring 说"真跑 --help"，实现是进程内，`import subprocess` 死导入 | 改为**真的**跑 subprocess `--help`（断言退出码 0 + 列出 9 个旧参数），再叠加 option 集合检查 | G01 | 45 passed |
| ④ | 降级（触网失败→回退旧缓存）对调用方不可见：`warnings=[]`、`from_cache=True` 无法区分新鲜命中与兜底 | `meta` 扩到 9 字段（`degraded/degraded_reason/cache_last_date/lag_days/expected_date/requested_source`）；`cli` 输出 `data.stale` 等 5 字段 + 强制写 warnings | **G33** | loader `degraded=True lag=837`；cli `stale=True warn_hit=True` |
| ⑤ | 缓存末日 2026-10-16 早于当日 → 每次先触网失败再回退，`--fast` 43.6s 逼近 G12 上限 60s | 见下"两个真 bug + 一个性能问题" | **G34** | `--fast` **58.3s → 9.8s**；数据加载 **57s → 0.01s** |

## ⑤ 拆出的三个真 bug（原清单与复核都没定位到）

1. **非交易日无谓触网**：新鲜度判断用 `today - 1天`，周日时等于周六 → 永远不命中缓存。
   改为 `_expected_latest_trade_date()`（跳过周末、15:00 前不算当日）。
   **该函数在 `meta.py` 里早已存在，但从未被调用** —— 典型的死代码未接线。
2. **`--start` 在缓存路径被完全忽略**：`--start 20260701` 与不带 start 返回**逐字节相同的 1643 根**。
   新增 `_apply_start()`，缓存命中与降级回退两条路径都按窗口截取。
3. **缓存被 `--start` 截断**（更隐蔽）：拉取路径把 `df[df.index >= start]` 的结果写回缓存文件，
   一次带 `--start` 的调用就会把更早的历史**永久削掉**。改为**写全量、返回窗口**。

## 性能：回测主循环去掉逐 bar 装箱

`engine.py::run_backtest` 原实现每根 K 线执行 `df.iloc[i]` 生成一个 Series，再做 6 次 Series 索引。
cProfile（581 组网格）显示 pandas `__getitem__` 被调用 **100 万次、占总耗时 90%+**。
改为预转 numpy 数组 + 数组下标取值，语义完全等价。

**等价性证据（不是"看起来差不多"）**：同一输入下新旧输出 **218 个字段逐字段 diff = 0**。
对比对象是优化前生成的 `docs/example_output_600519.json`（218 个标量/数组元素，排除时间戳与耗时）。

| 指标 | 修复前 | 修复后 |
|---|---|---|
| `--fast` | 58.3s | **9.8s**（5.9×） |
| 全量 `jobs=1` | — | **12.3s** |
| 全量 `jobs=4` | — | 14.7s（**比单进程慢**，进程池开销 > 单策略回测耗时，不建议使用） |
| `probe.py all` | — | 100s，27 keys 全 PASS |

## 本轮未验证项（如实记录）

- **权威裁定脚本仍未由我执行**：宿主沙箱故障未恢复。
  本轮只跑了 `probe.py`（27 keys 全 PASS）与 `pytest`（45 passed）。
  **G33/G34 已写进 ps1 的映射表（第 145 行与第 176-177 行），但未经实跑确认。**
