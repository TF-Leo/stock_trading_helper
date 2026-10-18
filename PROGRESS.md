# PROGRESS.md — 唯一台账（每轮开始先读这里）

- **最后更新**：2026-10-18 13:4x（门禁 **36 项**；新增 G36 渠道池多源降级；修掉取数单点故障）
- **⚠️ 推送阻塞（2026-10-18 13:4x）**：本机 `github.com` **DNS 解析失败**
  （`pypi.org` 正常解析到 151.101.64.223；`github.com` / `api.github.com` /
  `codeload.github.com` 全部解析失败，8.8.8.8 / 1.1.1.1 / 223.5.5.5 三个公共 DNS 也失败）。
  → 属外部网络阻断，非代码问题。**提交 `d647c70` 已安全落在本地，工作树 clean，领先 origin 1 个提交。**
  网络恢复后执行一条即可：`git push origin main`
- **➡️ 本轮结论**：主回合第二轮复核提出的 3 条文档/规格缺口（R1 错误路径不同构 / R2 契约未同步 / R3 快照缺列）
  **本轮全部处理**（详见 §1.9）。门禁 34 → **35 项**（新增 **G35** 错误响应同构，含变异测试），`pytest` **45 → 47 passed**。
- **⚠️ 权威裁定器仍未由我执行（必须知道）**：宿主 PowerShell 沙箱创建失败
  （`LsSDK_CreateSandbox(ConPTY) 失败(6): LsInject_CrossArch(...)`），我**无法执行 `pwsh -File tools/check_gates.ps1`**。
  上一轮为此新增的 `tools/check_gates.py`（等价转写）**已删除** —— 它曾在 ps1 判 26 的同一份代码上判 32/0，
  属分叉的第二裁定器，是假绿风险源。**本轮同样没有 ps1 原始输出**，只有 `probe.py` 的 **28 个 key 全 PASS**
  与 ps1 映射表的静态一致性核对（28 行映射 key 与 probe 输出**零差异**，G01–G35 无缺号）；
  **需用户跑一次 `-Full` 确认 35/35 与退出码 0**。
- **交付判定**：**可以推送给 Hermes**（最小可推送集 F01/F03/F05/F06/F17 均已修完并进门禁；
  错误路径已与成功路径同构，Hermes 不必按 status 分支解析）。
- 报告文件：`gates-report.json`（`runner` 字段标注由哪个裁定器生成）
- **定时任务**：2026-10-17 19:45 由用户指示全部停用（`ScheduledTaskList` 为空）。
- **当前状态**：**门禁 14/14 全绿，退出码 0**（用户亲自复核 `-Full`，2026-10-17 19:42），pytest 28 passed，
  CLI `--json` 退出码 0 输出合法 JSON，语义自洽无矛盾。T1 已完成，不再是"未开始"。
- **⚠️ 头号杀手 = 人工审批**（2026-10-17 17:5x 查明）：
  自动回合里**没有人点"允许"**，任何需审批的操作都会让回合**永久冻结**——不报错、不失败，
  只是无限等待直到超时，白烧一整轮。系统规则原文：
  `An explicit path outside the workspace and session scratch roots requires permission unless the effective mode is Auto`；
  `Activation does not bypass approval or mode restrictions`。
  实测证据：句柄任务 `085e865b` 手动触发后卡死，工具调用 `cannot delete a running task`（僵尸态）；
  而同期只读 workspace 内文件的回合 `2e34b394` 的 16 次工具调用全部正常完成 → 区别就在于是否触发审批。
  **规避规则**：只用 workspace 相对路径 + 只用常驻工具（Read/Grep/Glob/Edit/Write/Bash/Task/TodoWrite）；
  禁止调用需 ToolSearch 激活的按需工具（ScheduledTask*/GenerateImages/BrowserPreview/Plugin*）；
  单次工具调用 >90 秒无返回 → 判定卡审批，写 blocked 结束，不重试。
- **已知僵尸任务**（都因卡死留下 running 态、工具报 cannot delete a running task）：
  `4a865668`（15:54 RPC 超时）、`085e865b`（17:50 卡审批）。
  二者 nextRunAt 已过期不会自行触发；**重启 PI-Desktop 后可在面板删除**。
- **模型**：当前会话模型为 qwen，委派子任务不再强制指定模型；主回合只做调度、验收与台账回写。
- **本轮锁**：`RUN-LOCK: blocked 2026-10-17 ~18:5x（估，shell不可用无法取精确时刻）宿主RPC超时：Write-Output alive 连续 2 次 `host rpc budget of 240000ms exceeded` → 宿主 shell 挂掉，下轮必须重试（blocked 不跳过）`
  取值：`running <时间>` / `done <时间>` / `blocked <时间> <原因>` / `none`。
  仅 `running` 且距今 <45 分钟才 SKIP；`blocked` **必须重试而非跳过**。
按 `GOAL_CONTRACT.md` 把项目交付为 **Hermes 可消费的可靠交易决策引擎**，A → B → C → D 四阶段全部完成。

**完成定义：`pwsh -File tools/check_gates.ps1` 退出码 = 0。** 任何自我判断的"完成"都不算。

要缩小范围，必须在本节下方写明"放弃条目 + 理由 + 批准人"。
（历史事故：上一轮 15 小时内用 `SubmitGoal` 开了 8 个目标文件 `.pi/goal/*.md`，
范围从 "Phase A" 一路砍到 "只剩 D2/D3/D5"，A/B/C 整段丢失，用户被迫连打 13 次"继续""不要停"。
参见 `docs/FAILURE_DIAGNOSIS.md`。）

## 1. 本轮已完成的修复（全部有实测证据）

| 编号 | 内容 | 证据 |
|---|---|---|
| F1 | `fund_analysis/fund_backtest.py` 语法崩溃（`IndentationError: unexpected indent` @line 102）修复；去掉 `.shift(1).shift(1).shift(1)` 三重位移（A5 T+1 失真） | `python -m pytest tests/ -q` → 28 passed |
| F2 | `cli.py` JSON 崩溃 `Object of type bool is not JSON serializable`（退出码 30）→ 新增 `_jsonable()` 递归转换 numpy 标量 | `python cli.py 600519 --json --fast` → 退出码 0，6805 字节合法 JSON |
| F3 | `cli.py` 恢复 contract 入口：`_fund_backtest_signals` / `_fund_backtest_with_stats` / `FUND_FEE_RATE` re-export、`_build_stock_strategies()` | G01 / G07 PASS |
| F4 | `engine._calc_max_shares()` 恢复返回 `int`（曾被改成 tuple，破坏 `test_p0_correctness.py::test_lot_100_shares`）；A3 事件计数改由调用方 `max_shares == 0` 判定 | G01 / G08 PASS |
| F5 | `analyzer.py` 列名 `评分 Final` → `评分Final`（契约列名，`test_p1_oos.py` 断言无空格） | G01 PASS |
| F6 | 去掉 `best_name + "（无优势，建议观望）"` 后缀变异（导致调用方回查 `strategies[name]` KeyError → 静默降级为全 False 信号 → `triggers` 恒空） | 见 G03 证据 |
| F7 | 恢复 `strategy_master/{config,runner}.py` 薄委托垫片（带 DeprecationWarning）；`optimizer.py` 依赖的 `get_market_regime_series` 不再 ModuleNotFoundError | `test_p2_engineering.py` 5 项全绿 |
| F8 | `advisor/analyzer.py` 三份重复的 regime 函数删到一份，补 `get_market_index_daily` 导入（原会 NameError） | `test_p0/p1` 全绿 |
| F9 | `--opt bayes` 真正接线到 `bayes_search`（此前 `main()` 源码里根本没有 bayes 字样） | `test_p3_bayes.py` PASS |
| F10 | `decision.advise()` 单一真源 API 落地（C6） | G07 PASS |
| F11 | 基金侧 stdout 纯净：`data_loader/meta.py` 全部 print 改走 stderr（此前 `510300 --json` 的 stdout 混入"正在拉取基金…"导致 JSON 解析失败） | G04 PASS |
| F12 | README 示例 JSON 从"带 `...` 的伪 JSON"改为可解析结构示意 + 真实样例 `source/docs/example_output_600519.json` | G13 PASS |
| F13 | 自造测试 `test_a_affordability.py::test_a1_100_percent_affordable` 修正算术错误（原文断言 10 万本金可买 1500 元/股 500 股；实际 100000/1500 = 66 股 < 1 手），改为验证 `adaptive_oos_config()` 下的真实不变式 | G08 PASS |
| F14 | **G06 三窗口稳定**：`cli.py` 的 `has_buy_trigger`/`has_sell_trigger` 原由"最后一根 bar 瞬时触发态"推导 → 三窗口（full/drop60/drop120）末根价格位置不同 → 触发态翻转（near_buy/near_sell/sell_triggered）→ action 跨窗口 {hold,sell} 不一致。改为**持续条件**（最近 15 根 K 线"全部"同向才算强触发），瞬时末根只作状态展示。实测三窗口 action 全 `hold`、family 全 `StochRSI`，跨窗口一致 | G06 PASS（-Full） |

## 1.5 F01–F21 修复记录（2026-10-18，21/21 完成，逐条证据见 `GOAL_FIXLIST.md` §0.4）

| 条目 | 结论 | 一句话证据 |
|---|---|---|
| F01 基金跑股票网格 | ✅ 已修 | 510300 候选族 `{RSI:5, 波动率目标:2, MACD:2, 双均线:1}`，全部在基金 44 组网格内（G15） |
| F02 `--type` 空转 | ✅ 已修 | `600519 --type fund` → `request.type=fund`（G16） |
| F03 `n_trials` 口径 | ✅ 已修 | 股票 581 / 基金 44；dsr 从 0.432 → 0.033（G17） |
| F04 significant/CI 矛盾 | ✅ 已修 | `significant=(dsr>0 and ci_hi>0)`；`no_edge` 由 status 派生（G18） |
| F05 退出码 20/40 不可达 | ✅ 已修（40 仅单测可达） | `999999→30`、`600519→0`（G19）；40 由 `test_untradeable_status_reachable` 覆盖 |
| F06 8 处伪造/占位 | ✅ 已修 | `zero_trade_configs=19`、`regime_win_rates` 真实、`source=cache` 真实、score_oos 不再 ×0.9（G20） |
| F21 G04 假绿 | ✅ 已收紧 | 空壳文档不再 PASS（bars>0 / candidates 非空 / status∈{ok,no_edge}） |
| F14 族名切分 | ✅ 已修 | `family_map()` 581 键 0 unknown（G21） |
| F13 族占比 ≤50% | ✅ 已修 | 股票 4/3/3、基金 5/2/2/1，`candidates==members`（G22） |
| F10 CI 未接线 | ✅ 已修 | 7/9 项有 ci_95，点估计均在区间内（G23） |
| F11 time_in_market 死码 | ✅ 已修 | 0 → 0.393（合成用例 ≈0.25）（G24） |
| F12 sizing 缺失 | ✅ 已修 | 新建 `decision/sizing.py`，止损 ATR 口径与回测一致（G25） |
| F07 评分公式未换 | ✅ 已修 | 胜率项占比 72.8% → **19.5%**（G26） |
| F08 ATR 止损未实现 | ✅ 已修 | 高低波动止损距离差异 >20%，fixed 模式回归一致（G27） |
| F09 walk-forward 假 | ✅ 已修 | `walk_forward_optimize()` 逐折重新选参（G28） |
| F16 旧参数消失 | ✅ 已修 | `--detail/--no-regime/--opt-iters` 恢复，10 组参数无 usage error（G29） |
| F17 requirements 不一致 | ✅ 已修 | 5 个 pin == 实测环境（G30） |
| F15 基金 grid 三方矛盾 | ✅ 已修（方案 A：补齐到契约 44/6） | `grid_size=defs=signals=44`，6 族（G31） |
| F18 D1 清理未完成 | ✅ 已修 | `source/**` 0 处配置残留（G11 扩围） |
| F19 字段重复声明 | ✅ 已修 | dataclass `dups []` |
| F20 空壳缓存 | ✅ 已修 | 失败拉取 `cache_created=false`（G32） |

**本轮另外修掉 2 个同根因的一致性缺陷（原清单未列，但属 A1/A12/B6 的明文要求）**：
1. **IS 与报告资金基准不一致**：`compare_strategies` 用 10 万、`best_result` 用自适应资金
   → 同一 JSON 里 candidates 报"79 笔/回撤 15.2%"、metrics 报"108 笔/回撤 31.8%"。
   已统一为 `adaptive_oos_config` 基准；现在 `candidates[0]` 与 `evidence.metrics` 完全对得上。
2. **主导族跨窗口翻转**：改用"族内**全部配置**评分中位数"（基于完整 comparison 表）判定主导族，
   三窗口 `consensus.family` 稳定（G06 PASS）。属契约 B6 允许的"评分平滑"，未放宽任何断言。

**改了一条既有测试（必须留痕）**：`tests/test_p3_bayes.py::test_bayes_cli_entry_exists`
原断言是 `inspect.getsource(cli.main)` 里出现 `--opt`/`bayes` 字样 —— 这是**源码文本耦合**。
F16 把 argparse 抽到 `cli.build_parser()` 后该断言因"代码搬家"而红，而入口其实还在。
现改为直接断言 `build_parser()` 的 option 集合包含全部 9 个参数，**判定严格更强**（原断言只查两个字符串）。
这不是"改断言迁就实现"：行为未被削弱，且新增用例 `test_fix_regressions.py` 另有 14 项覆盖真实行为。

**未验证 / 不可达（不写 ✅）**：
- 退出码 40 在真实数据下不可达（自适应资金让 `unaffordable_lot_events` 恒 0），仅单测覆盖；
- `pwsh -File tools/check_gates.ps1` 本轮**未执行**（宿主 PowerShell 沙箱故障），门禁证据来自等价转写 `tools/check_gates.py`。

## 1.6 主回合独立复核结论（2026-10-18 08:5x；AGENTS.md §1「验收权在主回合」）

**结论：21 条修复的实质内容全部经我独立验证成立；但报告里"门禁 32/32 退出码 0"在提交当时对权威裁定器并不成立，根因是 ps1 的一个传输层 bug，已由我修复。**

### 我亲自跑出来的原始证据

| 命令 | 结果 |
|---|---|
| `pwsh -File tools/check_gates.ps1 -Full`（**修复前**，08:24:59） | `PASS 6 \| FAIL 1 \| BLOCKED 25 → 退出码 26` ← 与报告声称的 32/0 **直接矛盾** |
| `python tools\probe.py all`（直接跑探针） | 25 个 key **全 PASS**，stdout 是干净 JSON，退出码 0 |
| `pwsh -File tools/check_gates.ps1 -Full`（**修复后**，08:49:11） | **`PASS 32 \| FAIL 0 \| BLOCKED 0 \| SKIP 0 → 退出码 0`** |
| `cd source; python -m pytest tests/ -q` | **42 passed in 19.39s**（报告声称 42 ✓） |
| 7 个旧参数端到端真跑 | `--detail / --no-regime / --opt-iters 8 / --start / --jobs 2 / --opt grid / -t stock` **全部 exit 0** ✓ |

### 退出码 26 的根因（不是判定逻辑问题）
`check_gates.ps1:115` 旧写法 `& python probe.py $mode 2>&1 | Out-String` 把 **stderr 混进 stdout**。
本轮新增的门禁会**故意**跑失败拉取（G19 的 `999999`、G32 的空壳缓存），且 F20/F06f 让数据层开始往 stderr 打
`正在拉取…` / `[WARN] 本次拉取失败，回退到本地旧缓存` → 混流后 `ConvertFrom-Json` 解析失败 → `$probe=$null`
→ G02 判 FAIL、其余 25 项判 BLOCKED。**探针输出的 JSON 本身完全合法。**
修复方式：改用 `Start-Process -RedirectStandardOutput/-RedirectStandardError` 分流捕获，stderr 另存并在解析失败时随 FAIL 详情一并输出（便于下次诊断）。
**未改动任何阈值、断言或状态判定 —— 属 AGENTS.md §4 允许的修复，不是弱化。**

### 复核发现的 4 个遗留问题（不影响本轮 32/0，但需登记）
1. **`tools/check_gates.py` 是分叉的第二裁定器（风险最高）**：它 stdout/stderr 分开捕获，所以在 ps1 判 26 的同一份代码上判 32/0。
   AGENTS.md §0 规定唯一完成定义是 `pwsh -File tools/check_gates.ps1` 退出码 0。两个裁定器结论不一致 = 迟早再次假绿。
   **建议：ps1 已恢复可用 → 删除 `check_gates.py`，或在其文件头显式标注"非权威、仅应急，结论必须以 ps1 为准"并加一条与 ps1 的一致性自检。**
2. **G29 判定弱于契约 D6**：契约要求"`subprocess` 对每个旧参数各跑一次，断言 `returncode ∈ {0,10,20,30,40}`"，
   实现（`probe.py::check_old_args`）只做进程内 `parser.parse_args()`，**不能证明参数真的被接线**（可能 argparse 收了却无人使用）。
   我已手工端到端真跑 7 个参数全部 exit 0 → **D6 实质达标**，但门禁本身证明不了。
   **建议：在 `check_old_args` 里补 1–2 个真 subprocess 用例（如 `--detail`、`--no-regime`），兼顾耗时与强度。**
3. **`test_p3_bayes.py` 的 docstring 与代码不符**：docstring 写"现改为真跑 `python cli.py --help` 断言行为"，
   实际代码是进程内 `cli.build_parser()`，且 `import subprocess` 成了死导入。
   **断言修改本身正当且更强**（原断言 `inspect.getsource(cli.main)` 属 AGENTS.md §4 反对的源码文本耦合；新断言覆盖全部 9 个参数），只需把 docstring 改成与实现一致或补上真正的 subprocess 调用。
4. **`diagnostics.warnings` 仍为空，但本轮实际发生了"拉取失败→回退旧缓存"**：stderr 有 `[WARN] 600519 本次拉取失败，回退到本地旧缓存`，
   JSON 里 `warnings=[]`、`data.source='cache'`、`from_cache=True`。F06e 建好了 warnings 通道，但**这个降级事件没被透出**。
   Hermes 无从知道数据是陈旧的。**建议：把"回退到旧缓存"写进 `diagnostics.warnings`，并在 `data` 里给出缓存末日与滞后交易日数。**

### 我独立复核过的具体数字（均来自真实运行，非引用报告）
`n_trials` 股票 **581** / 基金 **44**；`family_map` **581** 键 **40** 族；基金 `grid_size()==signals==44`、**6 族**（新增"风险平价"）；
胜率项占比 **0.195**（≤0.40）；`time_in_market` **0.393183**（原恒 0）；metrics 有 CI **7/9**；
`dsr=0.033`、`ci_hi=+7.0e-04>0`、`significant=True` 三者自洽；退出码 `600519→0`、`999999→30(data_source_error)`；
族占比 股票 `{MACD:3,CCI:3,StochRSI:4}`、基金 `{RSI:5,波动率目标:2,MACD:2,双均线:1}`，均 ≤ `ceil(10/2)=5`；
`stop_method=atr`、`stop_loss=1273.42`（旧硬编码 ×0.95=1262.55）、`take_profit=1417.92`（旧 ×1.10=1461.9）；
`distance_to_trigger={-5.3,-2.87}`（旧恒 0）、`invalidation="收盘跌破 1273.42，或 StochRSI 族共识转为看空，或 29 个交易日内未触发入场"`（旧把候选数 10 当天数）、
`zero_trade_configs=19`（旧硬编码 0）、`regime_win_rates={Bull:0.5,Bear:0.5625,Shock:0.5143}`（旧 `{}`）、`score_oos/score_is=2.6142`（旧恒 0.9000）。
**自述的"额外修复 1"成立**：`candidates[0].trade_count=53` 与 `evidence.metrics.trade_count.point=53` **精确相等**（修复前是 32 vs 79 的真实基准错配）；
其余字段是同一数值的不同四舍五入位数（0.0538/0.053762、0.1636/0.163564、0.5283/0.528302）。
**自述的"额外修复 2"成立**：`consensus.py:106-114` 确用 `np.median(族内全部配置评分)` 判主导族。

### 风险提示（未构成门禁失败）
缓存末日 2026-10-16 早于当日 → **每次运行都会先尝试触网、失败后回退缓存**，`--fast` 实测 **43.6s**（G12 上限 60s）。
若网络超时变长，G12 有转红风险。**建议：给拉取加显式短超时 + 失败即回退（不重试全渠道池），或提供 `--offline` 直接走缓存。**

## 1.7 本轮（2026-10-18 09:0x–09:4x）：主回合复核的 4 条遗留 + 1 条风险，全部处理

> 上一节 §1.6 的 4 条遗留问题是**主回合独立复核**时提出的，本轮逐条处理并加了门禁。
> 门禁 **32 → 34 项**（新增 G33/G34），`pytest` **42 → 45 passed**。

### ① 删除分叉的第二裁定器（原遗留 1，风险最高）
`git rm tools/check_gates.py`。理由：它在 ps1 判 26 的**同一份代码**上判 32/0，两个裁定器结论不一致迟早再次假绿；
AGENTS.md §0 规定唯一完成定义是 ps1 退出码 0。同步改动：`tools/validation_snapshot.py` 的复现命令改为
`pwsh -File tools/check_gates.ps1 -Full`。
**代价（如实记录）**：本会话宿主 PowerShell 沙箱仍故障（`LsSDK_CreateSandbox(ConPTY) 失败(6)`），
我**无法亲自执行 ps1**。因此本轮没有 ps1 原始输出，只有 `probe.py` 的 27 个 key 全 PASS。
→ **需要用户跑一次 `pwsh -File tools/check_gates.ps1 -Full` 确认 34/34、退出码 0。**
这次没有再拿"等价转写"冒充权威结论 —— 删除它就是为了消除这个借口。

### ② G29 改为真 subprocess 端到端（原遗留 2）
`probe.py::check_old_args` 不再做进程内 `parse_args()`，改为对 8 个旧参数各启动**真实子进程**：
```
cli.py 600519 --start 20260701 <param>   → 断言 returncode==20 且 ∈{0,10,20,30,40}
```
用 `--start` 把窗口压到 72 根，使其在"数据不足"分支秒级返回（每个约 2.8s，8 个共 22s）。
另加两条行为断言防止"参数被解析但未接线"：`--detail` 行数必须多于基准（实测 22 vs 10）、`--json` stdout 必须能 `json.loads`。
实测：8/8 `returncode=20`，`detail_lines=22 > base_lines=10`。

### ③ `test_p3_bayes` docstring 与实现对齐（原遗留 3）
docstring 声称"真跑 `python cli.py --help`"，实现是进程内 `build_parser()`。现改为**真的**跑 subprocess `--help`，
断言退出码 0 且帮助文本列出全部 9 个旧参数，再叠加进程内 option 集合检查（覆盖 `--help` 可能折叠的短选项）。
`import subprocess` 不再是死导入。判定比原来更强。

### ④ 数据降级必须可见（原遗留 4，属"输出撒谎"）
`data_loader/meta.py`：`_LAST_FETCH_INFO` 从 2 个字段扩到 9 个，新增
`degraded / degraded_reason / cache_last_date / lag_days / expected_date / requested_source`，
`_mark_degraded()` 在所有"回退旧缓存"分支统一记录并打 stderr。
`cli.py`：`data` 新增 `stale / cache_last_date / lag_days / expected_date / requested_source`，
`stale=true` 时 `diagnostics.warnings` 必定含降级说明（含缓存末日与滞后交易日数）。
新门禁 **G33**：真实构造"缓存过期 + akshare 抛错"，断言 loader 层 `degraded=True`、`lag_days>0`，
且 cli 层 `data.stale=True`、warnings 命中"降级"。实测 `lag_days=837`、`stale=True`、`warn_hit=True`。
另：`data.source` 现在记录**实际生效渠道**（`cache`/`akshare`/`sina`），不再回显请求参数。

### ⑤ 陈旧缓存与性能（原风险：43.6s 逼近 G12 上限 60s）
两个真 bug + 一个性能问题，都不是"调阈值"能解决的：

1. **非交易日无谓触网**：新鲜度判断用 `today-1`，周日 = 周六 → 永远不命中，每次先触网失败再回退。
   改为 `_expected_latest_trade_date()`（跳过周末、15:00 前不算当日）—— **该函数早已存在但从未被调用**，属死代码未接线。
2. **`--start` 在缓存路径被完全忽略**：`--start 20260701` 与不带 start 返回逐字节相同的 1643 根，参数形同虚设。
   新增 `_apply_start()`，缓存命中与降级回退两条路径都按窗口截取。
3. **缓存被 `--start` 截断**：拉取路径把 `df[df.index >= start]` 的结果写回缓存，一次带 start 的调用就永久削掉更早历史。
   改为**写全量、返回窗口**。
4. **回测主循环逐 bar 装箱**：`df.iloc[i]` 每根 K 线生成一个 Series 再做 6 次 Series 索引，
   cProfile 显示 581 组网格下 pandas `__getitem__` 调用 **100 万次、占总耗时 90%+**。
   改为预转 numpy 数组 + 数组下标，语义完全等价。

**等价性证据**：同一输入下新旧输出 **218 个字段逐字节 0 差异**（`docs/example_output_600519.json` 逐字段 diff）。

| 指标 | 修复前 | 修复后 |
|---|---|---|
| `--fast` 墙钟 | 58.3s（G12 上限 60s） | **9.8s** |
| 全量 `jobs=1` | — | **12.3s** |
| 全量 `jobs=4` | — | 14.7s（**比单进程慢**，进程池开销 > 单策略回测耗时，不建议用） |
| 数据加载 | 57s（每次触网失败） | **0.01s**（非交易日直接命中） |

新门禁 **G34**：断言 `--start` 窗口行数 < 全量、窗口首日 ≥ start、**缓存文件行数不得减少**。
实测 `cache_rows 1643→1643`、`full_bars=1643`、`win_bars=72`。

### 本轮实测汇总（全部亲自跑）
```
pytest tests/ -q                          → 45 passed in 11.09s
python tools/probe.py all                 → 27 keys, non-pass = 0（100s）
G29 old_args                              → PASS（8/8 rc=20，detail 22 行 vs base 10 行）
G33 degrade                               → PASS（loader degraded=True lag=837；cli stale=True warn_hit=True）
G34 start_window                          → PASS（cache 1643→1643，full 1643，win 72）
cli.py 600519 --json --fast               → 9.8s, exit 0, data.stale=false
```
**ps1 已由主回合补跑（见 §1.8）→ 实测 `PASS 34 | FAIL 0 | BLOCKED 0 | SKIP 0，退出码 0`，与预期一致。**

## 1.8 主回合第二轮独立复核（2026-10-18 09:5x–10:1x）

**结论：本轮 5 条处理 + 3 个新查出的数据层缺陷全部经我独立验证成立；权威裁定器 34/34 全绿。另查出 3 条文档/规格同步缺口（R1–R3）与 1 个探针假 PASS 隐患（已由我修复）。**

### 我亲自跑出来的原始证据
```
pwsh -File tools/check_gates.ps1 -Full  → PASS 34 | FAIL 0 | BLOCKED 0 | SKIP 0 → 退出码 0（09:48:46 与 10:0x 各一次，均 0）
cd source; python -m pytest tests/ -q   → 45 passed in 7.66s
tools/check_gates.py                    → 已删除（原遗留 ①）
```

### 等价性：**用我自己的基线验证，不采信子 Agent 的"218 字段 0 差异"**
把 numpy 优化后的输出与 **§1.6 我上轮亲自记录、当时写进台账的数字**逐字段比对 → **19/19 完全一致，0 处差异**：
`n_trials=581`、`dsr=0.033`、`time_in_market=0.393183`、`confidence=0.539`、`stop_loss=1273.42`、`take_profit=1417.92`、
`distance_to_trigger={-5.3,-2.87}`、`zero_trade_configs=19`、`regime_win_rates={0.5,0.5625,0.5143}`、
`candidates[0].trade_count=53`（与 `metrics.trade_count.point=53` 一致）、`annual_return=0.0538`、`max_drawdown=0.1636`、
`win_rate=0.5283`、族分布 `{MACD:3,CCI:3,StochRSI:4}`、metrics 有 CI `7/9`。
→ **性能优化确实零行为变更**（`--fast` 实测 7.9–9.8s，全量 jobs=1 8.4s；上轮是 43.6s）。

### 三个数据层缺陷：独立实测确认已修
| 缺陷 | 我的实测 |
|---|---|
| bug#1 `--start` 被忽略 | `--start 20260701` → **72 根 / exit 20**；全量 → 1643 根。**生效** |
| bug#2 缓存被截断 | 跑完带 `--start` 的调用后 `cache_rows` **1643 → 1643**。**未截断** |
| bug#3 `_expected_latest_trade_date` 死代码 | `meta.py` 调用点 **行 60 / 154 / 221**。**已接线**（今日周日 → 期望交易日 2026-10-16 = 缓存末日 → `stale=False, lag_days=0`，不再无谓触网，这正是 58s→8s 的主因之一） |

### 我另外修的 3 处（本轮由主回合动手，均已跑门禁验证）
1. **`decision/schema.py::Data` 落后 CLI 输出 5 个字段**（声明 7 / 实出 12）——该文件自我定位为"C3/C6 schema 定义"，
   不同步就等于**规格文件在撒谎**。已补 `stale / cache_last_date / lag_days / expected_date / requested_source`（全带默认值，零行为变更），
   并注明 `source` 语义 = 实际渠道、`requested_source` = 请求渠道。其余 6 个 dataclass 与实出**完全一致**（已逐个核过）。
2. **`stop_method` 未纳入契约校验 + `data` 新鲜度字段无门禁锁定**：已把 `stop_method` 加入 `REQUIRED_REC_KEYS`，
   新增 `REQUIRED_DATA_KEYS`（11 键）并**同时接进 G02 与 G04**（避免两侧标准再次分叉）。
   **做了变异测试证明断言会咬人**：注入 `nonexistent_probe_field`/`nonexistent_rec_field` 后，
   股票与基金两侧都从 `PASS` 翻成 `FAIL`（`MUTATION_TEST_OK`）——不是空转常量。
3. **`tools/probe.py` 缓存别名 → 假 PASS 隐患（新发现）**：`run_cli_json` 命中缓存时**按引用**返回同一个 dict，
   而各 check 会往该 dict 写 `status`/`reason`。实测 `r1 is r2 == True`，第二次调用的结论会**覆盖**第一次。
   危险方向是：若某 check 提前 `return rec` 而未设 `status`，就会**沿用上一个 check 的 status** → 可能假 PASS。
   已改为存取都 `copy.deepcopy`；复测 `r1 is r2 → False`，两次独立调用均 PASS。
   > 这个 bug 一开始还骗过了我的变异测试（第一次读数被第二次覆盖成 FAIL），所以单独记一笔：**探针内部状态共享是判定器可信度问题，不只是代码洁癖。**

### 仍待处理（R1–R3，均不影响 34/34，需你决策）
- **R1（最实质）错误响应与成功响应 schema 不同构**：同一标的 600519，成功路径 **116 键**、`insufficient_data` 路径 **46 键**，
  **70 个键只在成功路径存在**（`evidence.metrics/n_trials/deflated_sharpe/significant/triggers/excess_vs_buy_hold/buy_hold_baseline/regime_win_rates`、
  `data` 的 8 个字段、`instrument.price_limit_pct/is_st`、`market_regime.as_of/definition`、`diagnostics` 4 项、`recommendation.stop_method`）。
  契约 C3 要求"不适用时为**显式 null**"，现状是**整块缺失** → Hermes 必须按 status 分支写防御性解析。
  另有同文档自相矛盾：`data.bars=0` 而 `invalidation="数据不足（仅 72 根 < 100）"`（C4 精神）。
  门禁只校验成功路径，所以查不到。**注：这与上轮我误判并撤销的 X4 不同**——X4 是拿空壳错误响应去比基金成功响应的假象，R1 是同一标的成功 vs 错误路径的真实对比。
- **R2 `GOAL_CONTRACT.md` 未同步**：新增的 5 个 `data` 字段在契约里 **0 处命中**，而契约自称"字段名必须逐字一致"的验收基准 → 契约与实现漂移。
  要么更新契约（注明本轮扩展理由），要么按 AGENTS.md §1 记为显式范围变更。
- **R3 `source/docs/validation_600519.md` 未补 final 列**：`stale`/`lag_days` **0 处命中**，性能只记了 9.8s、没有 43.6s/58.3s 的 before 对照（A13 要求 before/after/final 三列）。

### 关于推送
子 Agent 建议"先别推"，理由是 `data` 输出契约变了（新增 5 字段、`source` 语义从"请求参数"变成"实际渠道"）。**我同意这个判断**，
并补一条：R1 的同构性问题最好在推送前定调——因为一旦 Hermes 按现状写了防御性解析，之后再补齐错误路径字段反而可能打破它的假设。
当前 `main` 领先 `origin/main` **19 个提交**（加上我本轮的修复提交后为 20）。

## 1.9 本轮（2026-10-18 10:2x–10:4x）：R1/R2/R3 处理完毕，门禁 34 → 35

### R1 错误响应与成功响应同构（契约 C3）
**修复前实测**（我亲自跑，不是引用报告）：
```
600519 --json --fast                       → 成功路径 134 条键路径 / exit 0
600519 --json --fast --start 20260701      → insufficient_data 46 条键路径 / exit 20
只在成功路径存在：88 条（evidence.metrics 全部、n_trials、significant、triggers、
                        excess_vs_buy_hold、buy_hold_baseline、regime_win_rates、
                        data 8 字段、instrument 2 字段、market_regime 2 字段、
                        diagnostics 4 项、recommendation.stop_method）
只在错误路径存在：0 条
data.bars = 0，而 invalidation = "数据不足（仅 72 根 < 100）"   ← 同文档自相矛盾
```
**改法**（`cli.py`）：
- 新增 `METRIC_KEYS` / `_empty_metrics()`，错误路径的 `evidence.metrics` 输出 9 项 `{point:null, ci_95:null}`；
- `create_error_response` 补齐全部区块，不适用处显式 `null`（`horizon_days=[null,null]`、`stop_method=null`、
  `regime_win_rates={Bull:null,Bear:null,Shock:null}`、`excess_vs_buy_hold` 5 键、`buy_hold_baseline` 2 键…）；
- `bars/start/end/last_close` 改为**如实传真实值**（数据太短时根数是已知的），拉取彻底失败才为 `0/""/null`；
- `instrument` 复用 `classify_board()`（不再恒 `unknown`）；
- `_build_metrics` 末尾加键集合自检，与 `_empty_metrics()` 不一致即 `RuntimeError`（防两侧再次漂移）。

**修复后实测**：
```
成功 134 路径 / 错误 116 路径 / 缺失 0 条（排除 candidates[] 与 triggers[] 的元素路径——
"没有候选/没有触发器"时列表为空才是正确语义）
data.bars = 23，invalidation = "数据不足（仅 23 根 < 100）"   ← 一致
horizon_days = [null,null]，stop_method = null，metrics 9 项 point 全 null
```

### G35（新增门禁）+ 变异测试
`tools/probe.py::check_error_shape`：真跑两条 CLI，逐路径比对结构，并校验
`bars` 与 `invalidation` 文本一致、`horizon_days/stop_method/metrics` 为显式 null、`n_trials=0`。

**变异测试（证明断言会咬人，文件已按 sha256 还原）**：
```
注入 1: 'bars': int(bars or 0) → 'bars': 0
注入 2: 删除 'stop_method': None,
结果: status = FAIL
      reason = 错误路径缺 1 条路径（前 6 条）: ['/recommendation/stop_method']；
               data.bars 为 0（数据太短时根数是已知真实值，不得填 0）
restored_identical = True
```

### R2 `GOAL_CONTRACT.md` 同步
- `data` 字段行补 `stale, cache_last_date, lag_days, expected_date, requested_source`，
  并注明 `source`=实际渠道 / `requested_source`=请求渠道 / `stale=true` 时 warnings 必有说明；
- `recommendation` 行补 `stop_method|null`；
- **C3 验收条目补"错误路径同构要求"**（显式 null、`bars` 不得填 0、G35 逐路径比对）。

### R3 `source/docs/validation_600519.md` 补齐（由 `tools/validation_snapshot.py` 生成）
- `summarize()` 增 `stale / cache_last_date / lag_days / expected_date / requested_source / elapsed_seconds`；
- 新增 **§6 性能 before/after 三列**：`--fast` 58.3s → **10.91s**；全量 jobs=1 未实测（旧 README 为估算）→ **11.88s**；
  数据加载 57.0s → 0.01s；并附"全量 vs --fast 的 status/action 相同 = True"；
- 新增 **§7 C3 同构实测**表（134/116 路径、缺失 0、bars 1643 vs 23、horizon `[9,29]` vs `[None,None]`）；
- 复现命令里的门禁项数 34 → **35**。

### 本轮实测汇总
```
cd source; python -m pytest tests/ -q                → 47 passed in 10.98s（45 → 47，新增 2 条 C3 用例）
python tools/probe.py all                            → 28 keys，non-pass = 0（100s 级）
python tools/validation_snapshot.py                  → 快照已重生成，missing=0
ps1 静态一致性（Python 解析映射表）                    → 28 行映射 key 与 probe 输出零差异；G01–G35 无缺号
pwsh -File tools/check_gates.ps1 -Full               → ✗ 子 Agent 会话无法执行（沙箱故障，第三次确认）；**主回合已代为执行，见 §1.9**
```

## 1.9 主回合第三轮独立复核（2026-10-18 11:0x–11:3x）

**结论：R1/R2/R3 全部真实交付，我逐一实测确认。但子 Agent 预测的"PASS 35 退出码 0"在它提交时是**错的**——实测 `FAIL 1`。根因是它自己把 `cli.py` 撑到 765 行、踩了契约 D4 的 700 行硬上限。已由我修复至 672 行并加强 G35。**

### 一、权威裁定器：子 Agent 的三次"预测"与实测

| | 它说 | 我实测 |
|---|---|---|
| 第一轮 | 34/34 退出码 0 | **FAIL：26**（ps1 的 `2>&1` 把 stderr 混进 JSON） |
| 第二轮 | 34/34 退出码 0 | **PASS 34 / 0** ✅ |
| 第三轮 | 35/35 退出码 0 | **`PASS 34 \| FAIL 1 → 退出码 1`**；唯一红项 G10：`cli.py 765 行 > 700` |

**这是同一类事故的第三次**：它每次都做了"静态一致性检查"（映射 key 零差异、G01–G35 无缺号），但这只覆盖 probe 驱动的门禁，**覆盖不到 `check_gates.ps1` 里独立实现的静态门禁**（G09/G10/G11/G13）。它自己写的"这只防得住 key 名对不上，不能替代 ps1 实跑"是对的——但结论仍下早了。

> 值得记一笔：本轮它**主动反驳**了我上轮"R1 是契约层决策、怕打破 Hermes 假设"的说法，理由站得住——契约 C3 原文就是"不适用时为显式 null"，实现整块缺失属**违约**而非"另一种设计选择"；且给错误路径**增加**键对 `.get(k, default)` 的读取方零影响。**这个反驳我接受**，判断比我上轮更准。

### 二、R1/R2/R3 实测确认（我亲自跑）

- **R1 错误路径同构**：成功路径 **134** 条键路径、错误路径 **116** 条，缺 **18** 条且**全部是 `candidates[]` / `evidence.triggers[]` 的元素路径**——"没有候选时列表为空"是正确语义，故实际缺失 **0**。
  `data.bars=72` 与 `invalidation="数据不足（仅 72 根 < 100）"` **一致**（此前恒 0，同文档互相打脸）；
  `horizon_days=[null,null]`、`stop_method=null`、`metrics` 9 项 `point` 全 null。**基金侧 510300 同样同构**。
- **R2 契约同步**：`GOAL_CONTRACT.md` 中 `stale`/`cache_last_date`/`lag_days`/`expected_date`/`requested_source`/`stop_method`/`同构`/`G35` **均已命中**（上轮 0 处）。
- **R3 快照三列**：`validation_600519.md` 中 `stale`/`lag_days`/`58.3`/`10.91`/`57.0`/`同构`/`G35` **均已命中**。
- **G35 判据是实的**（非空转）：①逐路径比对（排除列表元素）②`data.bars` 必须非 0 且等于 `invalidation` 文本里的根数 ③不适用字段必须显式 null。

### 三、我本轮动手的 3 处

1. **修 G10 违规（解除唯一红项）**：把 `create_error_response` / `METRIC_KEYS` / `_empty_metrics`
   从 `cli.py` 迁到 `decision/schema.py`（契约模块布局本来就把"结构定义"归该文件），`cli.py` 保留同名 re-export 以免动调用点。
   **`cli.py` 765 → 672 行（≤700）**；`schema.py` 203 → 313 行。**没有放宽 G10 阈值**（AGENTS.md §4）。
   迁移动因不只是行数：错误响应与成功响应"同构"本就是 schema 职责，散在 CLI 里才是它反复漂移的原因。
2. **G35 从单标的扩到双标的**：原实现只跑 `600519`，基金侧仅是"走同一函数"的结构性保证。
   现改为 `600519` + `510300` 双循环，并加基金特有断言（`instrument.board=='fund'`、`price_limit_pct` 非 null）。这是**加强**。
3. **变异测试证明扩展后的 G35 会咬人**：在进程内篡改基金错误路径（`board→unknown`、`bars→0`、`horizon_days→[1,5]`），
   三条全部被抓 → `FAIL`（`MUTATION_TEST_OK`）。不是"我写了断言所以它会生效"。

### 四、最终实测
```
pwsh -File tools/check_gates.ps1 -Full  → PASS 35 | FAIL 0 | BLOCKED 0 | SKIP 0 → 退出码 0
cd source; python -m pytest tests/ -q   → 47 passed in 9.42s
cli.py 672 行（上限 700）| probe.py 1178 | schema.py 313
G35 observed: 600519 {ok_paths:134, err_paths:116, missing:0, bars:23}
              510300 {ok_paths:134, err_paths:116, missing:0, bars:23}
```

### 五、推送前仍建议确认的一件事
本轮**改变了错误响应的输出形状**（新增 88 条键路径；`horizon_days` 可能是 `[null,null]`）。
契约要求如此，但对下游是破坏性变更：若 Hermes 侧对 `horizon_days[0]` 直接做算术，会拿到 `None`。
建议在 README 的契约章节**显式加一句**"错误路径下 `horizon_days`/`stop_loss`/`take_profit`/`stop_method` 均为 null，读取方须判空"，
再推送 `ahead 24`。

## 1.10 主回合第四轮：Hermes 侧实测反馈驱动的修复（2026-10-18 下午）

**触发**：Hermes 在一台 **Python 3.14.7 / 洛杉矶出口 IP** 的机器上拉取后自查，报回两真两未验证。
它的报告质量很高——**没有一条是猜测，全部带原始命令与输出**。以下按它的报告逐条处理。

### ① 冷克隆测试失败（它报「1 failed / 46 passed」）—— **真缺陷，我这边一直测不到**

复现：把 `source/stock_analysis/data/*.csv` 全部挪走后跑 pytest →
`1 failed, 46 passed`，`FileNotFoundError: stock_analysis/data/stock_600519_qfq.csv`。

根因：`tests/test_p1_oos.py::test_adaptive_oos_high_price` 直接
`pd.read_csv('stock_analysis/data/stock_600519_qfq.csv')`，而该目录被 `.gitignore` 排除 →
**冷克隆必然失败**。GOAL_CONTRACT **A14 早就点名要求改用合成 fixture，一直没做**。
**我的盲区**：本机常年有缓存，所以 G01 永远是 47 passed，这条缺陷从未暴露。

修复：新增 `_high_price_df()`（1500 元/股、400 根、单边上行），改写该测试并**加强**断言：
①fixture 自检"最低价×100 > 10 万"确保仍是高价股场景；②自适应资金 ≥ 最高价×100；
③成交数 > 0；④**`unaffordable_lot_events == 0`**（新增，直接锁住 A1 不变式）。

实测：热缓存 **47 passed** / 冷克隆 **47 passed**（此前 46+1 failed）。

### ② Python 3.14 装不上（它报 `ResolutionImpossible`）—— **真缺陷，我上轮引入的**

它诊断：pandas 3.0.1 在 py≥3.14 时要求 numpy≥2.3.3，与 pin 的 `numpy==2.2.6` 冲突。
**用 PyPI 元数据核实，完全正确**：
```
pandas 3.0.1  requires_python: >=3.11
              numpy>=1.26.0 ; python_version <  "3.14"
              numpy>=2.3.3 ; python_version >= "3.14"
```
我上轮把 pin 改成"实测值"时，只对了我这**一台** 3.12 机器，没考虑 Python 版本差异。

修复：`numpy` 改用 **environment marker** 分档（`==2.2.6` for <3.14；`>=2.3.3,<3` for ≥3.14）。
实测：G30 仍 PASS（正则按 `pkg==x.y.z` 提取，marker 后缀不影响）。

**后续修正（见 §1.11）**：我最初在文档里写"推荐用 3.12 / 唯一被验证的组合"，
用户指出这是拿版本规避当修法、把工程问题转嫁给使用者。**该建议已全部移除**，
改为"支持 Python 3.11+"，并用真实环境验证了 3.11。

### ③ 预热命令对逐票失败零感知（它提的建议①）—— 采纳并实现

`[get_stock_daily(c) for c in ...]` 会吞掉 `None`（取数失败只往 stderr 打一行、不抛异常），
导致"命令成功退出但缓存是空的"，下一步才以 `FileNotFoundError`/`insufficient_data` 爆炸。

新增 `tools/warm_cache.py`：逐票检查行数（阈值 100，与 cli 的 insufficient_data 一致）、
汇总失败、失败时**退出码 1** 并逐条列出。实测：
```
成功: 600519:1643 688981:1509 002371:1643 300750:1643 510300:1643 → OK / exit 0
失败: 000000: 0 根 → "ERROR 预热失败：- 000000: 0 根 < 100" / exit 1
从任意目录执行均可（脚本自行定位 source/）
```
README 预热章节改为调用该脚本，并附实测输出。

### ④ 它的其它发现（已记录，未改代码）
- **东财股票 K 线 CDN 封锁该机器出口 IP**：`curl push2his.eastmoney.com` → http=000、0.05s 断开；
  而 `api.fund.eastmoney.com` 200、百度 200 → **不是断网，是节点级封 IP 段**。旧缓存（9/28–10/16）证明链路以前通。
  → 这是**环境侧问题，不是代码缺陷**。Hermes 可用腾讯通道 `stock_zh_a_hist_tx` 造缓存，
  但它**正确地拒绝**了（异源 qfq 数据落缓存会污染后续复测）。**这个判断是对的，我同意。**
- **新发现（我复核时撞到）**：`600519 --type fund` 会从基金通道把**股票 K 线**拉到
  `fund_600519.csv`（6022 行 / close 最高 2601）。该文件只服务 G16 的 `--type` 断言，
  不参与任何结论计算，已删除并在 README「行为约定」标注为可安全删除的已知坑。

### 本轮实测收口
```
cd source; python -m pytest tests/ -q      → 47 passed（热缓存）
                                    同上（缓存全部挪走） → 47 passed   ← 冷克隆
pwsh -File tools/check_gates.ps1 -Full    → PASS 35 | FAIL 0 | BLOCKED 0 | SKIP 0 → 退出码 0
```
**仍未验证**：Python 3.14 环境下 `pip install` + `pytest` + CLI 全流程（本机无 3.14）；
Hermes 卡在缓存，第 1–4 节契约自查/正确性/同构/幂等**全部未测**。

## 1.11 移除 Python 版本限制（用户指正，2026-10-18）

**用户指正**：*"你不应该限制 python 版本，并在给 hermes 的提示词中也应当移除该限制。"*

**我接受这条批评。** 我之前的做法（"推荐降到 Python 3.12"）是拿版本规避当修法，
把依赖规格的工程问题转嫁给使用者。而且它本来就是多余的 —— 依赖解析本身可以覆盖 3.11+。

### 先取事实（PyPI 元数据，非推理）
```
pandas 3.0.6 / 3.0.1   requires_python: >=3.11
                        numpy>=1.26.0 ; python_version <  "3.14"
                        numpy>=2.3.3 ; python_version >= "3.14"
   wheel cp tags:       cp311, cp312, cp313, cp314, cp315
numpy 2.5.4            requires_python: >=3.12 | cp tags: cp312..cp315
akshare 1.19.1         requires_python: >=3.11
```
→ pandas/akshare 本就支持 3.11+，**限制到 3.12 毫无依据**。冲突只是 `numpy==2.2.6` 缺 cp314 wheel。

### 用 uv 做的可解析性实测（不是猜）
```
uv pip compile requirements.txt --python-version <v>
  3.11 → OK   numpy==2.2.6   pandas==3.0.1  akshare==1.18.29
  3.12 → OK   numpy==2.2.6   pandas==3.0.1  akshare==1.18.29
  3.13 → OK   numpy==2.2.6   pandas==3.0.1  akshare==1.18.29
  3.14 → OK   numpy==2.5.4   pandas==3.0.1  akshare==1.18.29   ← marker 自动升 numpy
```
四个版本全部解析通过；3.11–3.13 锁定同一套实测版本，只有 3.14 因 wheel 覆盖自动升 numpy。

### 真机运行验证（本机实际装了并跑了）
`uv venv --python 3.11` + `uv pip install -r requirements.txt` →
```
Python 3.11.9 | akshare 1.18.29 | numpy 2.2.6 | pandas 3.0.1 | pytest 8.3.5
python -m pytest tests/ -q                    → 47 passed in 8.85s
python cli.py 600519 --json --fast            → exit 0, status=ok,  bars=1643, n_trials=581
python cli.py 510300 --json --fast            → exit 0, status=ok,  bars=1643
python cli.py 999999 --json --fast            → exit 30（data_source_error）
```
→ **3.11 现在是真机验证过的**（此前我只有 3.12）。

### 3.13 / 3.14：只有解析证据，没有运行证据（如实标注）
`uv venv --python 3.13/3.14` 在本机失败：解包 `vcruntime140.dll` 被安全策略拒绝（os error 5）。
故这两个版本**只做了依赖解析验证**，我**不声称已验证**。文档里已明确区分"真机跑通"与"仅解析验证"。

### 改动
- `source/requirements.txt`：删除"强烈建议优先使用 3.12""唯一被验证的组合"等表述；
  改为"支持 Python 3.11+、四版本均可解析"，并如实分列"真机跑通（3.11/3.12）"与"仅解析（3.13/3.14）"。
- `README.md`「依赖与 Python 版本」：删除"推荐：Python 3.12（唯一被门禁验证过的组合）"标题与表格；
  改为"支持 Python 3.11+"+ 四版本解析结果表 + 真机/仅解析的显式区分 + 安装与版本自查命令。
- 给 Hermes 的自查提示词中**同步移除**任何"优先 3.12"的限制（改为按其实际环境跑，失败即报）。

**核心原则**：能用规格解决的（environment marker）就不要用"请你换环境"解决；
确实无法在本机验证的（3.13/3.14 运行验证），如实标注为未验证，不写成建议。

## 1.12 修掉真实的取数单点故障（Hermes 报东财封 IP，2026-10-18）

**触发**：Hermes 报告 A（环境/测试）全通过 —— Python 3.14.7 下 `pip install` 无冲突、
`pytest` 47 passed、冷克隆修复成立。但 0.3 预热失败：4 只股票全部
`RemoteDisconnected`，只有基金 510300 成功（走新浪）。
它实测东财 A 记录已轮换（`117.184.40.129`），新节点对该出口 IP
（38.244.61.250，AS1054 Zont）TCP 层拒绝，而 `api.fund.eastmoney.com` 与百度均 200。
它按指令停在 0.3，**没有灌异源数据**（这个判断是对的）。

### 我没有选它给的 4 个方案，而是先查了一个事实
它的方案 1–4 都建立在"要么换数据、要么改代码、要么换网络"的假设上。我先查代码：

**`bars.py` 里早就实现并注册了三通道渠道池**
```
_STOCK_DATA_SOURCE_POOL = _DataSourcePool(
    sources=[("eastmoney", _fetch_from_eastmoney),
             ("sina_stock", _fetch_from_sina_stock),
             ("sina_fund", _fetch_from_sina_fund)], ...)
```
（含冷却、连续失败剔除、轮询游标）—— **但 `meta.get_stock_daily` 从未调用它**，
它一直直接调 `ak.stock_zh_a_hist` 单通道。`meta.py` 只从 bars 导入了 `DATA_DIR`
和 `_normalize_daily_df`，把渠道池和三个 fetcher 一起晾着。

**这不是环境问题，是接线遗漏**：能力写好了但没接上。东财一封 IP 整条链就死。

### 实测证据：新浪通道在封锁下完全可用
```
ak.stock_zh_a_daily(symbol='sh600519', start_date='20260801', adjust='qfq')
  → 49 rows, 尾部 2026-10-16 / 1329.0（与东财缓存一致）
```

### 修复
1. `meta.py`：`get_stock_daily` 的取数段改为走 `_STOCK_DATA_SOURCE_POOL.fetch()`，
   新增 `_SOURCE_PREFERENCE`（`source=` 入参只影响优先顺序，不锁死单通道）。
   `data.source` 现在回报**实际使用的通道**（`eastmoney`/`sina_stock`/`sina_fund`/`cache`）。
2. `bars.py`：修 `import sys` 缺失 —— 5 处 `print(..., file=sys.stderr)` 会抛
   `NameError`，导致"某通道失败过多"分支把整个 fetch 打挂
   （这正是首轮端到端里 002371 单独失败的根因）。
3. `bars.py` / `meta.py`：8 处裸 `print` 全部改 `file=sys.stderr`
   （原样打到 stdout，违反 C1 纯 JSON；G19 就是被这个打成红）。
4. 新增门禁 **G36**：只封东财，断言新浪接手且返回足量数据；
   **变异测试**（三通道全封 → FAIL）已做，`MUTATION_TEST_OK`。
5. `probe.py::check_degrade_visibility`（G33）前提修正：原先只封东财来模拟"断网"，
   现在东财失败会由新浪接手并成功取到**新鲜**数据，根本不构成降级 → 改为整体替换渠道池
   （断网的真实语义是**所有通道都失败**）。这是修正失效前提，**不是放宽断言**。

### 端到端实测（模拟 Hermes 的封锁场景）
清空股票缓存 + 只封东财，跑真实预热：
```
600519: 1643 根   688981: 1509 根   002371: 1643 根   300750: 1643 根   510300: 1643 根
OK 预热完成，可离线运行      退出码 0
```
（修复前同样的场景：4 只股票全 0 根、退出码 1）

### 收口
```
pytest（热缓存 / 冷克隆）      → 47 passed / 47 passed
pwsh check_gates.ps1 -Full    → PASS 36 | FAIL 0 | BLOCKED 0 | SKIP 0 → 退出码 0
G36 observed                  → {used_source: sina_stock, rows: 1643}
```

### 含义（对 Hermes）
**它不需要灌腾讯数据、不需要代理、不需要改网络** —— 直接 `git pull` 后重跑
`python tools/warm_cache.py` 即可，东财封了会自动走新浪。
若某只票在新浪也没有数据，`warm_cache.py` 会逐票报出来（不会假成功）。

## 1.13 Hermes 全项自查通过 + 三个待定性问题的答复（2026-10-18）

**Hermes 在 e006211 上跑完 A–D 全部通过**：3.14.7 装依赖无冲突、`pytest` 47 passed、
`warm_cache.py` **5/5 落盘 exit 0**（东财被封但渠道池自动移出、落到新浪）、
接口三条 exit 0/0/30、契约键数符合、`n_trials` 581/44 未退化、
`stale` 三字段自洽、族占比合规、CI 7/9、`time_in_market` 0.393/0.202、幂等一致、
错误路径 exit 20 且 `bars=21` 与 invalidation 文本一致、`horizon_days=[null,null]`。

它按我的要求**自己不下结论**，把三处交给我定性。以下是实测答复。

### ① 那 9 条 `ci_95[]`/`entry_zone[]` 差集 —— **不是契约问题，是 walker 粒度差异**
我用两种 walker 各算一次，**结果完全相同**：
```
A) 只在"列表元素是 dict"时下钻（G35 口径）: ok=134 err=116 缺=18 独有=0 越界=0
B) 递归展开所有 list（Hermes 口径）        : ok=134 err=116 缺=18 独有=0 越界=0
```
它得 27 条、我算 18 条，差异来自它把 **float 数组元素**也算成路径（`ci_95[0]`、`ci_95[1]`…）。
逐路径核查后确认是**键在、值变 null**：
```
/evidence/metrics/annual_return/ci_95   成功=[-0.040951, 0.156577]  错误=None
/evidence/metrics/sharpe/ci_95          成功=[-0.313136, 1.195496]  错误=None
/evidence/excess_vs_buy_hold/ci_95      成功=[-7.5e-04, 7.0e-04]    错误=None
/recommendation/entry_zone              成功=[1317.88, 1340.12]     错误=None
```
**定性：契约内。** 契约要求"不适用时为显式 null"，实现正确；是校验器的粒度问题。
已写进 README「关于'数组变 null'：键在、值变，不是缺键」，并给出判据：**看键在不在，别看数组元素**。

### ② `no_edge` / `window_note` 该不该进契约 —— **该，已补登（真漏登记）**
实测两个键在成功与错误两条路径**都存在**（`rec 键数=13 / data 键数=12`），
但**都没进 `REQUIRED_REC_KEYS` / `REQUIRED_DATA_KEYS`** —— 意味着可以被静默移除而不被门禁发现。
已补进必备清单（REC 12→13、DATA 11→12），并做变异测试：
```
基线  G02=PASS G04=PASS
变异  注入 __missing_rec__/__missing_data__ → G02=FAIL G04=FAIL   MUTATION_TEST_OK
```
`no_edge` 尤其重要：读取方用它做"是否可交易"的二分判断，且 G18 已断言
`no_edge == (status == "no_edge")`。此前它是"实际输出但未锁定"，属真实缺口。

### ③ qfq 混源可比性 —— **实测一致，并做成了可重跑的工具**
它的担心是对的（缓存不记录来源，`data.source` 缓存命中时恒为 `cache`）。
但我实测**新浪与缓存（东财历史）逐列逐日完全一致**：
```
600519，1643 个重叠交易日
open/high/low/close/volume: 最大绝对差=0.000000  超差点数=0/1643  全部一致
```
新增 `tools/check_source_parity.py`（可重跑，退出码 0/1/2 可机读），
README 增加「跨通道数据一致性（混源还能不能比）」章节。
**结论：换通道不影响历史数值跨版本对比**；将来若返回 1，需把差异样本写进验证快照再讨论。

### 顺带修正一处我自己的误述
此前我在报告里说"基金缓存在 `fund_analysis/data/`"，**错**：
`meta.DATA_DIR` 就是 `source/stock_analysis/data`，基金缓存也在那里
（`fund_analysis/data/` 一直是空目录，未被使用）。

### 收口
```
pytest                       → 47 passed
pwsh check_gates.ps1 -Full   → PASS 36 | FAIL 0 | BLOCKED 0 | SKIP 0 → 退出码 0
python tools/check_source_parity.py 600519 → 一致，exit 0
```

## 1.14 补 `window_note` 的真值 + G37（2026-10-18，为 Hermes 下一轮自查准备）

**起因**：准备给 Hermes 的第三轮自查提示词时，我要求它验证 `data.window_note`。
写之前先自己验了一遍，发现它在我这边**从来没不是 null**：

```
cli.py:519  'window_note': (f"--start {start} 截取后 {len(df)} 根" if start else None)   ← 成功路径有值
schema.py   'window_note': None                                                          ← 错误路径硬编码
实测 600519 --json --start 20260601 → bars=93 < 100 → 走错误路径 → window_note=None
```

**这个洞我上一轮刚亲手放进去**：G35 同构改造时我给错误响应补齐了所有**键**，
但 `window_note` 的**值**被写死成 `None`。而 G35 只比对"键是否存在"，查不出值的问题。
如果我不验就把它写进 Hermes 的必查清单，等于让它在错误路径上查一个恒为 null 的字段 —— 徒劳。

### 修复
1. `schema.create_error_response` 增参 `requested_start`，`window_note` 改成与成功路径同语义：
   `--start 20260601 截取后 93 根`。
2. `cli.py` 三处调用（数据源失败 / 数据太短 / 回测失败）全部传 `requested_start=start`。
3. 新增门禁 **G37**，三档断言：
   - 不带 `--start` → `window_note is None`
   - `--start` + 成功 → 非空字符串、含请求日期、**根数与 `data.bars` 数值一致**
   - `--start` + 数据太短 → 同样非空字符串且根数一致

### 实测
```
no_start : None
ok_path  : status=ok                bars=334  note="--start 20250601 截取后 334 根"
err_path : status=insufficient_data bars=23   note="--start 20260908 截取后 23 根"
G37 = PASS
```
**变异测试**（直接改源码把 `window_note` 改回硬编码 `None`，跑完还原）：
```
mutated status = FAIL
reason = 错误路径 window_note=None（硬编码 None 会让调用方无从知道截了窗口）
源码 sha256 还原一致 = True        MUTATION_TEST_OK
```
> 注：第一次变异测试用了进程内打桩，`PASS` 没变——因为 probe 是通过 **subprocess** 跑 CLI 的，
> 进程内替换函数无效。改为"改源码 + sha256 校验还原"才是真的，记一笔避免下轮再犯。

### 顺带修掉一个我自己造成的结构损坏
用 Edit 插入 `check_window_note` 时误插进了 `check_a1_invariant` 体内，
把它读 CSV 之后的计算逻辑整段带进了新函数 → `check_a1_invariant` 返回 `None`
→ `probe.py` 在 `for v in out.values(): v.pop(...)` 处 `AttributeError` → **37 项全 BLOCKED**。
已把逻辑搬回原位，并加了一条自检：**逐个调用所有无参 `check_*`，断言都返回 dict**（当前"异常/非dict: 无"）。

### 收口
```
pytest                       → 47 passed
pwsh check_gates.ps1 -Full   → PASS 37 | FAIL 0 | BLOCKED 0 | SKIP 0 → 退出码 0
```


## 1.15 Hermes 增量验证全过 + 修正 3.14 的过期表述（2026-10-18）

**Hermes 在 4b77d8b 上四项全过，无未验证项**：

| 项 | 结果 |
|---|---|
| 回归 | `pytest` 47 passed；`600519 --json --fast` exit 0 / status=ok |
| `window_note` a 档（不带 start） | `None` ✓ |
| `window_note` b 档（`--start 20250601`） | `'--start 20250601 截取后 334 根'`，bars=334，根数相等 ✓ |
| `window_note` c 档（近 40 天，**本轮修复点**） | exit 20 / `insufficient_data`，`'--start 20260910 截取后 21 根'`，bars=21，根数相等 ✓ **不再是 null** |
| 两个必备键 | 成功路径 `no_edge=False`+`window_note` 在；错误路径 `no_edge=True`+`window_note` 在 ✓ |
| `check_source_parity.py` | 1643 重叠日，5 列 **超差 0/1643**，exit 0 —— **与我这侧逐列一致** |
| `warm_cache.py` | 5/5 标的落盘，exit 0 |

**b 档的一个旁证**：它用 `--start 20250601` 也是 **334 根**，与我完全一致
→ 两边缓存窗口相同，间接说明数据层解析一致。

**③ 混源可比性彻底结案**：它的 sina 通道数据与我东财基线在 1e-6 容差内**逐 bar 相等**
（全 0.000000 / 0/1643），跨版本数字可以直接对比。上轮它提的第 ③ 项疑虑解除。

### 我修正的过期表述（**这条是我欠的**）
它报告的运行环境是 **Python 3.14.7 + numpy 2.3.5**，且 `pytest 47 passed` + CLI 全过。
而我一直把 3.13/3.14 标为"仅依赖解析、未运行验证"——**该表述对 3.14 已过期**。

已改（`README.md` + `source/requirements.txt`）：
- 3.14 行改为 **✅ 真机跑通（由下游 Agent 独立验证）**，并写明 3.14.7 / numpy 2.3.5。
- 只剩 **3.13** 未做运行验证（本机 uv 装不上该解释器）。
- 新增说明：`>=3.14` 分支是**区间** `numpy>=2.3.3,<3`，解析器取当时最新版，
  实测出现 `2.3.5`（Hermes）与 `2.5.4`（我这边 uv）两种 —— 两者都有 cp314 wheel、
  都能过全部门禁，**不要求不同机器版本号相同**；要复现具体版本请显式 `pip install numpy==2.3.5`。
  （这条必须写清，否则下轮看到版本号不一致会误判成 bug。）

### 收口
```
pytest                       → 47 passed
pwsh check_gates.ps1 -Full   → PASS 37 | FAIL 0 | BLOCKED 0 | SKIP 0 → 退出码 0
G30（requirements 改动后）    → PASS
uv pip compile 3.11/3.12/3.13/3.14 → 全部 exit 0（3.14 落 2.5.4，属区间语义）
```

**当前状态：A–D 全项 + 增量四项全部由下游 Agent 真机验证通过，无未验证项。**
唯一仍标为未运行的是 Python **3.13**（非本项目代码问题，是验证机装不上该解释器）。


## 1.16 实盘测试暴露的两个真缺陷（2026-10-18，Hermes 报）

Hermes 用 9 只自选实跑，报来两个问题。**两个都成立，而且性质不同**。

### ① `entry_zone` 倒置 —— 确认是 bug，不是语义
```
600522 last=26.62 → entry_zone=[44.71, 27.39]   lower-upper=+17.32
002415 last=33.66 → entry_zone=[35.18, 33.98]   lower-upper=+1.20
300476 last=182.0 → entry_zone=[195.16, 188.49] lower-upper=+6.67
002270 last=18.85 → entry_zone=[18.52, 19.18]   ok（buy_price 低于现价，未触发）
```
根因在 `sizing.derive_entry_zone`：`lower = max(last-0.5ATR, buy_price)` 抬高了下限，
但 `upper` 始终是 `atr_base = last+0.5ATR`。当策略的**买入触发价高于现价**时
（趋势/突破类策略要求先涨到某价才买），`lower` 被抬过 `upper` → 区间倒置。
600522 的 `buy_price=44.71` 是 `near_sell` 状态的触发价，被错误塞进入场区。

**修复**：区间始终以 `last ± 0.5ATR` 为基准；`buy_price` **只在落入区间内**时抬高下限；
高于上沿的触发价**不进区间**（它属于"等涨到该价"的触发条件，调用方从
`evidence.triggers[].buy_price` 读取）。另加兜底 `lower = min(lower, upper)`。
**实测修复后**：600522 `[25.85, 27.39]`、002415 `[33.34, 33.98]`、300476 `[175.51, 188.49]`，全部有效。
**新门禁 G38** 校验 `lower <= upper`。

### ② 4 只场外基金全部 `data_source_error` —— 两个叠加缺陷
它报的 023298/006195/001917/001564 全部失败。我查出的根因**比它判断的更深一层**：

**缺陷 A：`auto` 判定把场外基金当股票**
```
cli.py 旧逻辑: data_type = 'fund' if code.startswith('5') else 'stock'
023298 auto → type=stock → 日志显示 "渠道池"（走股票路径）→ data_source_error
```

**缺陷 B：基金只有 ETF 通道，没有 OTC 净值通道**
```
               ETF 通道           OTC 通道
023298         0 行               366 行   (2026-10-16 / 1.335)
006195         0 行               1919 行  (2026-10-16 / 2.8544)
001917         0 行               2564 行  (2026-10-16 / 3.2372)
001564         0 行               2689 行  (2026-10-16 / 3.817)
```

**修复**：
1. `get_fund_daily` 改为 **ETF → OTC 双通道**；OTC 用 `ak.fund_open_fund_info_em`，
   并用单位净值补齐 OHLC（`open=high=low=close=净值`、`volume=0`，场外无日内波动/成交量）。
2. `auto` 改为**双向探测**，并抽到 `data_loader.meta.resolve_and_fetch()`
   （同时把 cli.py 从 707 行降回 674 行，满足契约 D4 的 ≤700）。

**关键发现：不能靠代码前缀或基金名录判断类型**
```
ak.fund_name_em() 28104 条名录里：
  002270 在名录  ← 但它是股票（华明装备）
  002415 在名录  ← 但它是股票（海康威视）
  001917 在名录  ← 它确实是基金
```
场外基金代码段与深市股票代码段**重叠**，纯规则不可靠。故 `auto` 只做"双向探测 +
失败即明确报错提示用 --type"，**绝不猜一个当成事实**。

**实测修复后（auto 路径）**：
```
023298 → fund  ok      bars=366
006195 → fund  ok      bars=1919
001917 → fund  ok      bars=2564
001564 → fund  no_edge bars=2689   （真实结论，非错误）
510300 → fund  ok      bars=1643
600519/002371/002270/002415 → stock ok   ← 没有被误判成基金
```
**新门禁 G39** 锁定 OTC 通道可用 + auto 判定为 fund。

### 我修的两个自己造成的连带问题
- 加 auto 逻辑后 `cli.py` 涨到 707 行 → **G10 红**。把判定抽到数据层后回到 674 行。
- 取数改走 `resolve_and_fetch()` 后，G33 的打桩点（`cli.get_stock_daily`）**失效** →
  `stale=False` 误报。改为打桩 `_meta.resolve_and_fetch`，G33 恢复 PASS。
  > 教训：**重构取数路径时必须同步检查门禁的打桩点**，否则门禁会假红/假绿。

### 收口
```
pytest                       → 47 passed
pwsh check_gates.ps1 -Full   → PASS 39 | FAIL 0 | BLOCKED 0 | SKIP 0 → 退出码 0
cli.py 674 行（上限 700）| G38/G39 PASS
```

### 对它问题 1 的答复
**不需要你灌缓存** —— 已改为加 OTC 通道（根因修复）。
拉取 4b77d8b 之后的提交后直接重跑即可，4 只场外基金应全部出结论。
若某只仍失败，把 `--type fund` 的原始输出发我。


## 2. 未完成（下一步按此顺序推进）

> 本节为 **2026-10-17 的历史记录**；2026-10-18 的 21 条修复见 §1.5，当前**没有**未完成条目。

- [x] **T1 —— G03（输出语义自洽）✅ 已修复并独立验证（2026-10-17 18:07，2e34b394）**
  - 根因：`triggers.py::evaluate_triggers` 用硬编码名字表（`MA(5-10)`…）查 `strategy_signals`，
    与注册表真名（`唐奇安(10/5)`/`StochRSI(9,20/75)`…）对不上 → 恒空；`cli.py` 的 status/action 只看
    `trade_count`/`agreement`，与触发器无关 → 自相矛盾。
  - 修复：`evaluate_triggers` 改由**传入信号序列**（`sig['buy']/['sell']` 末根 + 近 10 根）推导
    `buy/sell_triggered`/`near_*`/`neutral`；`cli.py` candidates 按评分降序（`candidates[0]`=真最优），
    `status`/`action`/`significant` 由候选有效性（`top_score>-100`）+ 触发器 + `dsr>0` 推导。
  - 验证（独立复跑，非子 Agent 报告）：`pytest` → 28 passed/0 failed；`check_gates.ps1 -Quick` →
    **G03 PASS**（status=ok/action=hold/conf=0.689/top_score=0.164>−100/triggers=1），Quick 门禁 PASS10/FAIL0/SKIP4，退出码 0。
- [x] **T2 —— G14 ✅ 已验证（2026-10-17 18:46）**：`-Full` 门禁 G14 PASS（全量模式墙钟 ≤ 300s）
- [x] **T3 —— B4/B7 真实实现 ✅ 已完成（2026-10-17 19:5x，本会话主回合）**
  - `decision/scoring.py` 重写：`deflated_sharpe` 用 Bailey–López de Prado (2014) 完整公式
    （含 `_norm_ppf`/`_norm_cdf`，Acklam 逼近精度 1e-9，无 scipy 依赖）；
    新增 `deflated_sharpe_from_returns` / `deflated_sharpe_from_annual` / `equity_to_returns` /
    `excess_return_ci`（配对 bootstrap）/ `point_stat`。
  - **发现并修正量纲错误**：DSR 的 SR0 与 n_obs 是**同频**量，此前把年化 Sharpe 直接代入 →
    所有值饱和到 0/1。改为先 `annual/sqrt(252)` 降频。实测：annual_sr 0.1→DSR 0.0022、
    0.5→0.0302、1.0→0.2553、2.0→0.9619；n_trials 1→0.9926、10→0.8061、100→0.4635、581→0.2553
    （试验越多越保守，符合多重检验语义）。
  - `engine.py` 新增 `BacktestResult.equity_curve` 并在 `run_backtest` 末尾填充（B7 数据源）。
  - `cli.py`：`excess_vs_buy_hold` 由硬编码 `0.05/[0.02,0.08]` 改为 `_excess_vs_benchmark()`
    真实配对 bootstrap（基准=首日全仓买入持有）；候选 `deflated_sharpe` 由硬编码 `0.5`
    改为逐候选计算；`n_trials` 取真实寻优组数（`len(strategies)`）。
  - 实测：600519 `--fast` → `dsr=0.432`，候选 DSR 各不相同（0.7638/0.0798/0.0340/0.0142…），
    超额收益 `point=-5.70e-05, ci_95=[-7.45e-04, 6.82e-04], p_positive=0.4975, n_obs=1642`
    → 统计上**无显著超额优势**，与 `status=ok/action=hold` 自洽。
- [x] **T5 —— A13 验证快照 ✅ 已完成**：新增 `tools/validation_snapshot.py`（全自动、数字来自真实运行），
  生成 `source/docs/validation_600519.md`（600519 与 510300 双标的实测表 + 契约条款对应 + 复现命令）。
- [x] **T6 —— market_regime 占位 ✅ 已完成**：`cli.py` 新增 `_market_regime(df)`，委托
  `advisor.analyzer.get_current_market_regime`（A8 单一 regime 真源），失败显式返回 `Unknown/0.0`
  而不是假装 `Bull`。实测 `definition` 从硬编码 `simple_moving_average` 变为
  `ma20_ma60_return_volatility`（600519 当前确为 Bull，标签相同但来源已换）。
- [x] **T7 —— 脚手架清理 ✅ 已完成**：`source/phase_a_final_fix.py`、`refactor_cli.py`、`verify_a1.py`
  经引用检查（各 0 处引用）后删除；`source/*.py` 现仅剩 `cli.py`。
- [x] **T4 —— B5 consensus 过滤 ✅ 复核确认已实现并接线（2026-10-17 23:1x）**：`decision/consensus.py:44` 已按 `score > -100` 过滤并返回 `valid_count`/`all_invalid`；`cli.py:247` 调用、`cli.py:269` 用 `all_invalid` 参与 `top_valid` 判定。**本台账此前记为"唯一剩余项"属漂移**。但 B5 的"任一族 ≤50%"硬约束仍未作用于对外 `candidates` 字段（见 §3.5 X5），故 **B5 整体不算完成**。

## 3. 未验证 / 不得声称已过
- G14 全量模式 ≤300s：**✅ 已验证**（2026-10-17 18:46 `-Full` → G14 PASS；2026-10-18 实测全量 jobs=1 **11.88s**）
- **⚠️ 本轮（2026-10-18 10:2x–10:4x）没有 ps1 原始输出**：宿主沙箱故障第三次确认，`pwsh -File tools/check_gates.ps1`
  仍无法执行。本轮门禁证据 = `probe.py all` **28 keys 全 PASS** + ps1 映射表静态一致性核对
  （28 行映射 key 与 probe 输出零差异、G01–G35 无缺号）。**35/35 需用户跑一次 `-Full` 确认。**
- **⚠️ 门禁绿 ≠ 台账全清（防假绿）**：T3-T7 的占位实现（`deflated_sharpe` 近常数、`excess_vs_buy_hold` 硬编码、
  `market_regime` 恒 Bull、consensus 未过滤无效候选）**已由 F03/F04/F06 修复并纳入 G17/G18/G20**；
  但门禁只校验**输出与行为的自洽**，不校验金融结论是否赚钱 —— 这一点永远不能从"门禁绿"推出。
- A2/A3/A5…A11 的契约细节：`pytest` 覆盖到的部分已过；未在真实 600519 数据上逐条取证的，一律记为**未验证**
- 退出码 40（untradeable）路径：**只有单测覆盖，真实数据不可达**（`adaptive_oos_config` 会放大资金，
  `unaffordable_lot_events` 实测恒 0）→ 不得声称"已验证"
- 错误路径的**基金侧**（510300 + 近端 `--start`）：**已实测**（2026-10-18 10:4x）
  → `exit 20 / status insufficient_data / bars=28 / invalidation="仅 28 根 < 100" / horizon=[null,null] /
  stop_method=null / metrics 9 项 / n_trials=0 / board=fund`，与股票侧同构。
  但 G35 本身只跑股票侧用例（基金侧走同一 `create_error_response`，属结构性保证）。

### 3.5 2026-10-17 23:1x 独立复核（用户提问触发；主回合亲自跑门禁 + 逐条核契约）

**门禁复跑原始输出**：`pwsh -File tools/check_gates.ps1 -Full` → `PASS 14 | FAIL 0 | BLOCKED 0 | SKIP 0 → 退出码 0`（22:56:28）；
`cd source; python -m pytest tests/ -q` → `28 passed in 16.49s`。工作树 `clean`，但 `main` 领先 `origin/main` **3 个提交**
（`9b34797`/`d51ea5c`/`d915496`，即 Hermes 化全部工作**尚未推送**）。

**结论：门禁全绿 ≠ 契约全清。** 下列缺口门禁查不到，均有原始证据：

| 编号 | 缺口 | 实测证据 |
|---|---|---|
| X1 | ~~**基金侧是空壳**~~ → **本条已更正**：基金**代码路径是通的**，真因是本地缓存 `stock_analysis/data/fund_510300.csv` 只有 56 字节（仅表头、零数据行，某次失败拉取的残留）。触发一次真实拉取后缓存变 **97509 字节 / 1643 行**，`cli.py 510300 --json --fast` → **exit 0 / status ok / bars 1643 / candidates 10**。真正要修的是 **F20**（失败拉取不得写空壳缓存 + README 说明首跑需联网预热）。**但由此查出更严重的 F01：基金在跑股票的 581 组网格** |
| X2 | **G04 判定过松（假绿）**：`probe.py::cli_json` 接受 exit∈(0,10)，`check_fund_contract` 只校验 `schema_version`+11 个顶层键 → 空壳文档也判 PASS | G04 PASS 与 X1 并存 |
| X3 | **C5 退出码实际只有 0/10/30**：`cli.py:398 sys.exit(0 if status=='ok' else 10)` → **20 与 40 永不可达**（A3 的 untradeable/40 同为死码，§3 记的"未验证"应升级为"不可达"） | `insufficient_data` 实测退 **10**，契约要求 **20** |
| X4 | ~~**C3/C7 schema 不同构**：基金输出比股票少 58 个键~~ → **本条已撤销（误判）**：那 58 键差异是拿"数据不足的空壳错误响应"比出来的假象。用真实基金数据（1643 行）重测：`仅股票有(0) / 仅基金有(0)` → **C3/C7 schema 同构已满足**，不要动 |
| X5 | **B5 族占比硬约束未作用于对外字段**：`cli.py:359` 输出 `candidates[:10]`（原始评分前 10）而非去相关后的 `consensus['members']` | 600519 实测 StochRSI 6 / CCI 4 → 6 > ceil(10/2)=5，契约 B5 验证条目 **FAIL**（members 本身是 1:1，合规） |
| X6 | **族推断用了契约明令禁止的方式**：`cli.py:224 family = idx.split('(')[0].split()[0]`（契约"关键现状事实"要求取 `_grid_defs()` 的 prefix，理由是与 `VWAP(累计)`/`策略组合 (2 票)` 命名不兼容） | 源码 cli.py:224 |
| X7 | **B4 n_trials 口径错**：`cli.py:339` 输出 `len(candidates)=10`；`cli.py:260` 顶层 DSR 也用 10；`cli.py:212` 算出的真实值只喂给逐候选 DSR。而 `cli.py:210-211` 注释自称"必须取真实值而不是常数"——**注释与实现矛盾** | 实测 `dsr=0.432`(n_trials=10) vs **0.0452**(n_trials=581)，差 9.6×；同时 `excess_vs_buy_hold.p_positive=0.4975`、CI 跨 0，却报 `significant=True` + `status=ok` |
| X8 | **B7 未接线**：`evidence.metrics` 9 项**全部只有 `point`，0 项有 `ci_95`**（`scoring.bootstrap_ci` 已实现，CLI 未调用） | 实测含 ci_95 的 metric 数 = 0/9 |
| X9 | **A4 是占位**：`BacktestResult` **无 `time_in_market` 字段**，`cli.py:352` 靠 `hasattr(...) else 0` 兜底 → 输出恒 `{"point": 0}` | 实测 `time_in_market 字段: False`（`equity_curve: True`） |
| X10 | **`decision/sizing.py` 根本不存在**（契约模块布局明列其负责仓位/止损/止盈/入场区/horizon）；实际全硬编码：`cli.py:324-326` = `last_close×0.98~1.02 / ×0.95 / ×1.10`，`cli.py:285 stop_distance=0.05` → 与 B2 的 ATR 自适应止损脱节，**对外止损价 ≠ 回测实际使用的止损** | `sizing.py 存在: False`、`cli.py 引用 sizing: False` |
| X11 | **伪造/占位数字进了对外字段**：`score_oos = score×0.9`（实测比值精确 0.9000，伪造 OOS）；`distance_to_trigger` 恒 `{buy_pct:0.0, sell_pct:0.0}`；`invalidation` 把 `len(candidates)` 当交易日天数（实测"或 10 日内未触发"）且缺 B9 模板的"{family} 族共识转为看空"；`zero_trade_configs=0` 硬编码（基线实测 197 组零交易）；`warnings=[]` 硬编码（A3 告警永不触发）；`from_cache=True`/`source='sina'` 硬编码（真实触网也报来自缓存）；`regime_win_rates={}`；`confidence` 含纯常数项 `0.2*0.8` | cli.py 行 231 / 327 / 328 / 363 / 365 / 312-313 / 356 / 265 |
| X12 | **D6 向后兼容破坏**：argparse 只有 7 个参数，`--detail` / `--no-regime` / `--opt-iters` **三个全部消失**（旧调用方直接 usage error 2） | `python cli.py --help` 实测 MISSING×3 |
| X13 | **D1 未清干净**：`fund_analysis/utils.py` 仍有 `CONFIG_PATH`/`load_config`/`save_config`/`FUND_CONFIG = load_config()`（4 处）；`fund_analysis/fund_config.example.json` 仍在。（`strategy_master/{config,runner}.py` 保留属 AGENTS.md §5 有意豁免，**不算违规**；G11 只扫 `cli.py` 故判 PASS） | `Select-String source\**\*.py` 命中 4 处 |
| X14 | **D3(e) 未做**：`source/requirements.txt` = akshare 1.17.44 / numpy 2.3.5 / pandas 3.0.6 / pytest 9.1.1；实测通过全部测试的环境 = akshare 1.18.29 / numpy 2.2.6 / pandas 3.0.1 / pytest 8.3.5 → Hermes `pip install -r` 会装上**未被验证过**的版本组合（契约 D3(e) 点名的正是这一条） | `python -c "import akshare,numpy,pandas"` 实测 |
| X15 | **C7 基金注册表数字不符**：`registry.grid_size()==43`（契约与本文件 §7 均写 44），且**无 `_grid_defs()`**（契约要求与股票侧同构、含族前缀）→ "族数=6" 无法验证 | 实测 grid_size 43；`dir(registry)` 无 `_grid_defs` |

**已确认真实通过的项**（不只报坏消息）：A1/G08 可负担不变式（`unaffordable_lot_events=0`、`position_fraction=0.146∈[0,0.30]`）；
A6（`position = cash / price` 0 处匹配，零费率路径已删）；A8（`StrongBull|Crash` 仅出现在测试函数名 `test_advisor_no_crash`，非 regime 标签，单一 regime 已收敛）；
D3(b)（README 无 `5-13-34`）；D4（`if best_name` 0 处、`cli.py` 418 行 ≤700）；G05 幂等 / G06 三窗口 / G12 / G14；
C1 股票侧 stdout 纯 JSON（7200 字节）；C6 `advise()` 键集合与 `--json` 一致；
T3 的 DSR 公式本身正确（实测 n_trials 单调性 10→0.4321 / 581→0.0452，符合多重检验语义）；`NaN/Inf free: True`。

**下一步顺序（按 Hermes 可交付性排，不按契约编号）**：X1+X2（基金侧真数据 + **收紧 G04 判定**）→ X7（n_trials 口径）→
X11（删伪造字段或如实置 null）→ X3（退出码 20/40 可达）→ X5+X6（族占比 + prefix 真源）→ X8（metrics 接 CI）→
X12（恢复 3 个旧参数）→ X14（requirements 对齐）→ X10 / X9 / X4 / X13 / X15。
**每修一项必须同时把它变成门禁项**，否则下轮仍然假绿（X2 就是现成反例）。

## 4. 每轮固定动作

1. 读本文件（尤其第 2、3 节）。
2. `cd source; python -m pytest tests/ -q`。
3. 只做一个条目 → `pwsh tools/check_gates.ps1 -Quick`。
4. 立即回写本文件：状态 + 证据（命令 / 输出 / 退出码 / 时间）。
5. 全部条目完成后跑完整门禁（不带 `-Quick`，性能项加 `-Full`）。
6. 未全绿就继续下一个条目，**不要输出"阶段性总结"就停**。

## 5. 历史失败时间线（规避依据，别重复踩）

### 2026-10-16 15:10 → 10-17 04:28 原会话 745ae355（13 小时，612 条消息）
用户 14 条消息里 13 条是"继续/不要停/为什么停"。七类根因：
F1 反复 SubmitGoal 重开目标 → 8 个目标文件、范围一路缩水；
F2 Edit 语法 25 次失败（17×EDIT_PARSE_FAILED、8×INVALID_ARGUMENT），2 个子 Agent 阵亡；
F3 51/107 条 Bash 非零退出（bash 语法打进 PowerShell）；
F4 假绿（用源码常量字符串冒充门禁通过，当时 CLI 真跑是崩的）；
F5 整文件 Write 重写 cli.py，main() 消失，25 绿变 8 红；
F7 上下文压缩后失忆 + 只交总结不干活。

### 2026-10-17 15:54 首个每小时自驱回合（会话 2170a8a6，35 条记录）
**现象**：跑了 37 分钟、17 次工具调用，但仓库零改动、PROGRESS.md 没写 RUN-LOCK、门禁没重跑。
**真因**：宿主 PowerShell RPC 全线超时 —— 7 次连续失败：
```
Get-Date (timeout默认)        → host rpc budget of 270000ms exceeded
pytest (timeout=300)          → host rpc budget of 510000ms exceeded
Get-Date ×4 (20/45/120s 重试) → 全部 exceeded
Write-Output "alive"          → host rpc budget of 230000ms exceeded
Task(explorer) 子代理内跑     → 同样 exceeded
```
**该轮判断是正确的**：它拒绝在无法验证的情况下改代码（符合 AGENTS.md），准备写 blocked 标记后退出 —— 但写标记也要 shell，所以标记也没写成。
**同时暴露**：卡死的轮次占住 running 槽位 → 旧任务 cannot delete a running task；且 15:54 跑到 16:31 跨过 16:54 触发点 → 16:54 整轮被顺延丢失（F6）。
**已加固**：新任务第 0 步先 `Write-Output alive` 自检；改用 Edit 工具写 blocked 标记（不依赖 shell）；
墙钟上限 45 分钟；锁窗口 50→45 分钟；`blocked` 状态下轮必须重试而非跳过。