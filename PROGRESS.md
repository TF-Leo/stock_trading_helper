# PROGRESS.md — 唯一台账（每轮开始先读这里）

## 2026-11-15 · PR #107 P1：ST 名单改为收盘后预计算 + 盘前只读缓存

- **分支**：`task/st-list-robust`（PR #107，父 = `origin/main` `ab7d435`）；
  未改 `source/decision/*`，未引入依赖，隐私口径不变。
- **根因（A5500 实测）**：东财 push2/push2delay 对本机出口 IP 触发 nginx 444
  （3 分钟内约 10 次请求即触发）；ST 名单走 clist 全市场翻页，盘前逐票各跑
  一次 CLI = 每天把全市场名单反复拉 N 次，且旧缓存从未落盘。
- **预计算**：新增 `tools/precompute_st_list.py`，18:30–18:40 运行一次，
  原子写 `source/stock_analysis/data/state/st_list.json`（schema 2：
  `fetched_at` / `session_date` / `count` / `sha256` / `codes[]` / `source` /
  `stocks` 留档）。空表 / 冲突重复按 #106 重试一次，仍异常**只告警、不写**；
  网络失败不重试；坏结果永不覆盖好缓存。
- **盘前只读**：`cli.py` 改用 `status.get_st_list_cached()`（只读磁盘，零联网）。
  覆盖最近交易日 → `is_st_source=exchange_list`；过期 → 仍用但 `stale=true`
  + 缓存日期/期望日期 warning；无缓存/校验失败 → `unavailable`（报价名称兜底）。
  `sha256` 校验失败按「拒绝使用」处理（防止 ST 假阴性）。
- **熔断**：`StListFetchGuard` 包 `_astock_generated.em_get`，命中 HTTP 444 或
  零字节 FIN 时抛 `StListBlocked`（不换域、不重试，请求数恰为 1），写
  `st_list_cooldown.json`（默认 1800s，`ST_LIST_COOLDOWN_SECONDS` 可覆盖）；
  冷却期内预计算 0 请求、退出码 3。旧 `get_st_list` 也尊重冷却，不再撞墙。
- **文档**：新增 `docs/ST_LIST_PRECOMPUTE.md`（cron `35 18 * * 1-5`、时段、
  缓存结构、退出码、验证口径）；README 的 ST 段落与 `status_source` 说明同步。
- **证据（本机 Linux，`~/.venv` Python 3.12）**：
  - `python -m pytest tests/ -q` → **1004 passed, 1 failed**；唯一失败
    `test_probe_requirements.py::test_current_environment_passes_via_probe`
    仍是父 commit 基线已有的解释器/依赖版本不符（akshare/requests/numpy/
    pandas/pytest），与本改动无关（#107 正文已记录同一失败）。
  - 新增 `source/tests/test_st_precompute.py` 13 项：缓存字段/一次全市场请求
    序列、空表与冲突重复不覆盖、网络失败不覆盖、444/零字节 FIN 请求数=1 +
    冷却期 0 请求、`main` 退出码 3/4、盘前 `advise_internal` 的
    `is_st_source=exchange_list` 且 `EM_SESSION.get` / `st_stock_list` /
    `fetch_st_list` / 旧 `get_st_list` 调用数均为 0、过期缓存 stale+日期、
    无缓存 unavailable、损坏缓存拒绝。
- **未验证**：本机无 `pwsh`，`tools/check_gates.ps1` 未执行；真实端点仍处于
  444 封禁窗口，未做联网复测（预计算用离线桩覆盖 444/FIN 行为）。
- **遗留风险**：cron 接线由协调者安排；若预计算未运行，盘前持续用旧缓存并
  `stale=true`；若东财改拦截形态（如 403 + HTML），按 `failed` 处理、保留
  旧缓存但不会自动冷却。

## 2026-11-15 · issue #106 ST 名单三态 / 重试 / 缓存回退 / 来源标注

- **分支**：`task/st-list-robust`（父 = `origin/main` `ab7d435`）；未改 `source/decision/*`
  的决策表（仅给 `Instrument` dataclass 补一个 additive 字段），未引入依赖。
- **三态**：`StListSnapshot.state` 新增 `ok` / `empty`（可疑空表）/ `duplicate`
  （可疑重复代码）/ `failed`（请求失败）/ `offline`。空表与失败各有明确
  warning；空表文案写明「接口返回 0 条，不能当成今日没有 ST」。
- **重试与备胎**：空表 / 重复代码先**重试一次**（网络失败不重试，避免放大限流）；
  仍异常则回退本地缓存，`stale=true` 且 warning 写明缓存日期与覆盖率。内容
  一致的重复代码在第二次尝试清洗（计数 + warning），内容冲突则继续报错。
- **来源标注**：`instrument.is_st_source`（additive）取值
  `exchange_list` / `quote_name` / `local_name` / `unavailable`；错误响应与成功
  响应键同构。渲染层 `is_st_source=unavailable` 输出「ST 状态未知」，不写「非 ST」。
- **冲突取保守**：名单说非 ST 而任一名称说 ST（含「报价名称说非 ST、本地名称说
  ST」这一旧实现会漏掉的组合）→ `is_st=true` + warning；名单说 ST 而报价名称
  非 ST → 以名单为准并 warning。
- **证据（本机 Linux，`~/.venv`）**：
  - `pytest tests/ -q` → **991 passed, 1 failed**；唯一失败
    `test_probe_requirements.py::test_current_environment_passes_via_probe`
    是父 commit 基线就存在的解释器版本不符（akshare/requests/numpy/pandas/pytest），
    与本改动无关。
  - 新增 16 项 `test_trading_status.py`（三态 / 重试 / 缓存日期+stale / 重复清洗 /
    四种来源 / 三向冲突 / 空报价名）与 4 项渲染措辞断言。
  - `tests/test_advice_render.py` 的 `cli_json_structure.golden.json` 同步
    additive 键 `instrument.is_st_source`。
- **真实端点实测（2026-11-15，本机，`force=True`）**：`push2.eastmoney.com`
  与 `push2delay.eastmoney.com` 均 `ConnectionError`（curl 亦 SSL EOF，而 github
  HTTP 200），真实观测到 `state=failed`、`available=false`、warning 为
  「ST 名单接口失败…且无本地缓存…」，**未**被当成「今日无 ST」，网络失败也
  **未**重试。`ok / empty / duplicate` 三态由离线桩逐项覆盖。
- **未验证**：本机无 `pwsh`，`tools/check_gates.ps1` 未执行。
- **跳过项**：官方风险警示名单备胎 —— 仓库内没有现成官方源（§6.8 已是东财风险
  警示板），按 brief「若仓库已有则复用」记为不新增。

## 2026-11-14 · issue #99 台账结算三项残留（basis 文案 / 无缓存钉 stock / fixture 措辞）

- **分支**：`task/ledger-residuals`（父 = `origin/main` `0722fec`）；**未改
  `source/decision/*`**，未引入依赖，schema 只做 additive。
- **1) basis 文案与「拒绝换源」统一**：新增两个固定措辞常量
  `BASIS_CONFIRMED_SUFFIX = "口径已确认：记录时确定，结算时不再更改"` /
  `BASIS_UNCERTAIN_SUFFIX = "口径未确认：首次结算成功前允许一次重判"`。
  `classify_instrument_detailed()` 的四种判定、`resolve_price_plan()` 的
  「记录价源」与「老记录」两条分支全部用这两句，不再出现「按 brief 默认」
  「并告警」这类与实现脱节的旧说法。
- **2) 记录时无缓存不再永久钉 stock**：`build_record()` 新增 additive 字段
  `basis_uncertain`。判 `true` 的三种情形：两份缓存都不存在 / 只有未复权
  交易所 OHLC（qfq 口径无法确定）/ `fund_*.csv` 读不出。`rejudge_record()`
  在**首次结算成功前**（`settled` 仍为 null）重判一次，**只允许
  uncertain → 确认**；已确认记录（`basis_uncertain=false`）、已结算记录、
  以及引入该字段之前的老记录（缺键）一律不重判。重判写盘并把 code 列进
  `settle_all()` 返回的 `rejudged`，`tools/forward_log.py evaluate` 打印
  「口径重判」一行；`build_report()` 新增 `basis_uncertain` 计数并单独输出。
- **3) fixture 匿名化措辞**：`tests/fixtures/fund/fund_999901.csv` 的说明改为
  「匿名化场内 ETF 日线切片，140 个交易日（2026-04-17~2026-11-12），文件无
  代码列、文件名是虚构代码，数值保留原样（公开行情）以便独立复核形态；不含
  持仓/成本/股数/权重」。新增 `test_fund_fixture_shape_matches_its_documented_
  provenance` 逐项核对说明与文件（行数、首末日、列集合、`open != close`、
  `amount ≈ volume × close`），说明与文件脱节即红。
- **证据（本机 Linux，`~/.venv`）**：
  - `pytest tests/ -q` → **875 passed, 1 failed**；唯一失败
    `test_probe_requirements.py::test_current_environment_passes_via_probe`
    是**基线就存在**的解释器版本不符（akshare 1.17.44 vs 1.18.29 等）。
    父 commit `0722fec` 基线 **861 passed / 同一条失败** → 净增 14 项。
  - `python tools/probe.py all` → **40 PASS / 1 FAIL**（仅 `requirements`），
    与父 commit 逐项相同。
  - 迁移对比：真实台账（2026-11-12/13，2 天 12 条）新旧实现结算结果
    **0 漂移**（settled/not_matured/pending/failed 逐项相同、`rejudged=[]`）；
    另用旧实现造一份含 2 条**已结算**记录（个股 qfq + 场外净值）的台账，
    新实现重算后 `settled` / `settle_reason` / `settle_status` / 三个口径字段
    **逐键相等，0 漂移**。
  - `tests/test_forward_ledger.py` 47 → **59**；`test_readme_consistency.py`
    新增 2 条（README「运行」行键集合 = `build_record` 实际键；重判语义必须
    写进 README）。
- **跳过项**：无。

## 2026-11-14 · PR #101 最后一轮：1×P2（可选日期）+ 2×P3（文档）

- **基线**：`origin/task/event-risk` = `90d24c2`；工作区
  `fix-pr101-2`，未合并。
- **P2 真实数据崩溃（已修）**：东财增减持 `RPT_SHARE_HOLDER_INCREASE` 存在
  `START_DATE=None` 的合法行（员工持股计划尚未开始）。修前整只票的
  `holder_trades` 子项抛 `EventsFetchError: START_DATE 不是 YYYY-MM-DD：None`。
  修法：新增 `_optional_day()`，`START_DATE` / `END_DATE` / 预告 `REPORT_DATE`
  缺失或不可解析时**保留该行**、字段置 `null`，并在子项 warnings 计数
  `可选日期缺失 N 处（…），已保留该行并置 null`；窗口判定所必需的
  `FREE_DATE` / `NOTICE_DATE` / `TRADE_DATE` 与代码仍丢弃坏行并告警。
- **真机复验（公开代码，非持仓）**：全 14 只默认票池逐票扫描，唯一命中
  `300476` 的 1 行员工持股计划（`START_DATE=None`，`END_DATE=2026-10-30`）。
  - 修前（`90d24c2` 原文件，真实端点）：`FAILED EventsFetchError:
    START_DATE 不是 YYYY-MM-DD：None`。
  - 修后：`fetch_holder_trades("300476")` → `OK 1 条`，`start_date=null`；
    端到端 `precompute 300476` → `requests=8 ok_sections=4 failed=0 wall=8.82s`；
    缓存 `holder_trades.available=true, items=1` + 上述 warning。
  - 默认 14 只全量：`requests=60 ok_sections=56 failed_sections=0 wall=64.83s`。
- **P3 文档**：
  - `docs/EVENT_RISK.md` §7 cron 改为**单行**（crontab 不支持 `\` 续行），
    加 `mkdir -p logs`，解释器改仓库 venv 绝对路径
    `/path/to/stock_trading_helper/source/.venv/bin/python`；票池/请求改为
    实测 `14 只 / 60 请求`（§5 同步，实测 wall=64.83s）。
  - `README.md`：`600000,000001` 位置参数实测报
    `代码无法解析：'600000,000001'`（EXIT=1），改为
    `--codes 600000,000001 --json`，实测 `EXIT=0, requests=12,
    ok_sections=8, failed_sections=0`。
  - 测试数量：`test_events_context` **69 passed**；全量 **930 passed,
    1 failed**（唯一失败仍是基线既存的 `test_probe_requirements` 解释器
    版本断言）。
- **约束**：未改 `source/decision/*`；盘前仍零联网（本次真机请求只在预计算
  侧）；未引入依赖；隐私口径不变（只用公开代码）。

## 2026-11-14 · PR #101 复验修复（2×P2 + 5×P3）

- **基线**：`origin/task/event-risk` = `3e23874`（PR 头），Hermes 复验结论
  2×P2 + 5×P3。全部修在 PR 分支上，未合并。
- **P2-1 行级归属校验**：`tools/precompute_events.py` 的四个 `fetch_*` 逐行
  核对 `SECURITY_CODE` + 日期窗口（解禁 `FREE_DATE`、增减持/预告
  `NOTICE_DATE`、质押 `TRADE_DATE<=as_of`）；他票 / 窗口外 / 日期不可解析 /
  重复行**丢弃**并写进子项 `warnings`，CLI 转 `diagnostics.warnings`。
  测试覆盖三种坏行，端到端断言不入缓存。
- **P2-2 探针**：每轮先跑 4 个对照探针（CSDC 无条件 `pageSize=1`；
  解禁 `FREE_DATE>=as_of`；预告/增减持 `NOTICE_DATE>=as_of-30d`）。探针
  抛错或 0 行 → 全票池该子项 `available=false, reason=interface_suspected`，
  跳过该子项逐票请求；零成本兜底（全票池皆空且上一轮有数据）→
  `empty_pool_suspected` + warning。新增 `--probes-only`。
  实测 4 请求 / **3.86s**；3 只票实测 16 请求 / 17.39s，1 只 8 请求 / 8.57s。
- **P3 全部落地**：北交所 `not_supported` → 渲染「ℹ️ 股权质押：未覆盖」；
  缓存未生成 → 「事件数据未生成（预计算未运行）」，不写「过期（未知）」；
  过期判定只用官方磁盘交易日历（缺月向前翻，日历不可用则跳过日期判定）；
  未知 `PREDICT_TYPE` → warning + 保留原值；`docs/EVENT_RISK.md` §7 写入
  18:35 cron 示例（实际接线另行安排）。
- **验证**：`pytest tests/test_events_context.py` 63 passed；
  全量 `pytest tests/` **924 passed, 1 failed**（唯一失败是基线既存的
  `test_probe_requirements` 解释器版本断言，与本改动无关）。
- **未改**：`source/decision/*`、`action` / `confidence` / 仓位；未引入依赖；
  盘前路径仍零联网（探针只在预计算侧跑）。

## 2026-11-14 · issue #1 / R11 T7：事件风险预计算（解禁/增减持/预告/质押）

- **分支**：`task/event-risk`（父 `origin/main` = `0722fec`）；未改
  `source/decision/*`，未引入依赖，`--json` 只做 additive（新增
  `evidence.events`）。
- **设计**：`tools/precompute_events.py` 只在收盘后（18:30–18:50 时段）串行
  取数，写 `source/stock_analysis/data/state/events/events.json`（`data/` 已
  gitignore）；CLI 主路径只读缓存（`data_loader/events.py`），**盘前零请求**
  （socket + `_http_get` 双桩测试）。四类子项独立 `available/stale/as_of/
  items[]`；失败只影响该子项，保留上一轮数据并标 `stale=true`。
- **端点取证（实测，不是凭记忆）**：钉死 `f215349` 的 SKILL.md §3.6/§14.1/
  §14.3/§14.5，再本机真跑：
  - `600000/002475/300750` × 4 端点 → **12 请求 / 12.23s**（≈1.02s/请求，
    与串行下限一致）；`002475` 预告 3 行、三者质押各 1 行（最新统计日
    2026-11-13）。
  - `688512` → 解禁 2 条（2026-11-16，占总股本 37.81%）、减持 1 条；
    渲染实测 `⚠️ 解禁：2026-11-16 解禁 12374.87 万股（占总股本 37.81%）`。
  - **实测校正**：解禁股数取 `ABLE_FREE_SHARES`（与 `FREE_RATIO` 对齐），
    不是 `FREE_SHARES`（后者是更大的流通股口径，600050 实测对不上）；
    质押表按个股累积多年历史（600000 实测 633 行/127 页），只取
    `TRADE_DATE desc` 第 1 页 1 行，允许 `pages>1`。
- **阈值（写死常量，理由见 `docs/EVENT_RISK.md`）**：20 自然日窗口；
  解禁 ≥1.0% 或 ≥500 万股；减持合计 ≥0.5% 或 ≥500 万股；预告类型黑名单
  （首亏/续亏/预减/增亏/略减）或同比上限 ≤-20%；质押 ≥20%。每类至多一行；
  无事件整段消失；过期写「事件数据过期（{D}）」，不写「无事件」。
- **测试**：新增 `source/tests/test_events_context.py` **47 项**（四类命中/
  未命中/过期/接口失败、第 20 日与阈值边界、盘前零请求、10 只×4 端点串行
  估算、失败隔离、保留旧子项、端点 9201/错误码/多页/非列表/北交所质押）。
  全量 `pytest tests/ -q` → **906 passed, 1 failed**；唯一失败
  `test_probe_requirements.py::test_current_environment_passes_via_probe`
  是基线就存在的解释器版本不符（缺口盘点 U7），与本轮无关。
- **门禁说明**：本机 Linux 无 `pwsh`，`tools/check_gates.ps1` 无法执行；
  以 `pytest` 全量 + `tools/probe.py` 可跑项为准。`cli.py` **687 行**（≤700）。
- **风险**：①限流——44 请求（含 4 探针）串行 ≥43s，绝不可放盘前；
  ②「无事件 vs 坏了」已由对照探针 + 行级归属校验补齐（见本文件顶部
  2026-11-14 复验修复）；③阈值可能带来 warning 疲劳，已按
  「20 日内 + 重大额度 + 每类一行」收敛。

## 2026-11-14 · PR #97 复验非阻塞项收口（2×P2 + 3×P3）

- **分支**：`TF-Leo/fix-pr97`（父 = PR #97 head `c3756af`）；**未改
  `source/decision/*`**，未引入依赖，`--json` 只做 additive（键集合不变）。
- **P2-1 `null` ≠ 空池**：`fetch_pool()` 原先 `payload is None → return []`，
  与 README「非列表一律抛错」矛盾，且 HTTP 200 + body `null`（上游降级页的
  常见形态）会被读成「今天没人被监控」。现 `null` 抛 `MarketWatchError`，
  **只有 `[]` 是真空池**；`test_empty_list_and_null_differ_end_to_end` 逐项
  断言两条路径的 `available` / warnings 不同。
- **P2-2 docstring 对齐实现**：`stale=true` 且有命中时**实现会出行**（日期取
  条目生效窗口），旧 docstring 写「stale 不出行」。以实现为准（命中是风险
  提示，宁可多报），改 `advice_render` 模块 docstring + `_market_watch_line`
  docstring + README 两处，并补两条断言：`stale+hit` 出行、
  `render(stale)==render(fresh)` 逐字节一致。**渲染行为零改动**。
- **P3-1 缓存损坏 ≠ 请求失败**：新增 `read_cache_checked()` 三态
  （可用 / `CACHE_MISSING` / `corrupt: <原因>`），校验严格到单条字段与
  `count` 一致性；`load_pool` 遇坏缓存**忽略并重新请求一次**（旧实现只
  「视为缺失」且不重取，warning 报成「接口请求失败」）。`PoolSnapshot`
  新增 `cache_problem` 字段（dataclass 内部字段，**不进 CLI JSON**），
  `market_watch_evidence` 单独输出一条「监控池缓存损坏…已忽略坏缓存并重新
  请求」。同时修掉 `_snapshot_from_cache` 的 `count == 0` 兜底 —— 那会把
  「entries 全垃圾 + count=0」读成合法空池。
- **P3-2 死参数**：`market_watch_evidence(code, data_type=None, ...)` 的
  `data_type` 从未被读取（监控池是全市场名单，与标类型无关）。选择**删除**
  而非硬用：`cli.py` 调用点改为 `_market_watch_evidence(code, as_of=as_of)`，
  docstring 写明「没有 `data_type` 参数」的理由。
- **P3-3 `cli.py` 超限**：基线 761 行（G10 的 ≤700 在基线即红），PR #97 后
  772。**做了最小拆分而非跳过**：纯函数块（`_jsonable` / `parse_pct` /
  `build_parser` / `_parse_cli_date` / `_date_arg`）迁至新文件
  `source/cli_args.py`，`cli.py` 保留同名 re-export，故
  `tests/test_cli_date_args.py`、`tests/test_p3_bayes.py`、
  `tools/probe.py`（含 `python cli.py --help` 真跑）零改动。
  **`cli.py` 772 → 678 行，G10 由红转绿。** 模块**不放在 `decision/`**：
  本任务约束「不改 `decision/*`」。
- **证据（本机 Linux，无 pwsh；门禁脚本以 probe 为裁定器，probe 直接跑）**：
  - `pytest tests/ -q` → **833 passed, 1 failed**；唯一失败
    `test_probe_requirements.py::test_current_environment_passes_via_probe`
    是**基线就存在**的解释器版本不符（akshare 1.17.44 vs 1.18.29 等）。
    基线为 818 passed / 同一条失败 → 净增 15 项（`test_market_watch.py`
    38 → 53）。
  - `python tools/probe.py all` → **39 PASS / 2 FAIL**：
    `requirements`（同上，基线红）；`error_shape`（
    `/instrument/one_word_limit` 缺失，**由父 commit #93 引入**，修复需改
    `decision/*`，超出本次约束，**未处理**）。两者在改动前后逐字相同。
  - 非回退项：`BJ` 映射、一次请求服务全池（`len(calls)==1`）、同日二次
    0 请求、9 类失败构造、渲染行位置（闸门之后 / 价格行之前）全部沿用
    原断言并继续通过。
- **跳过项**：无。

## 2026-11-14 · issue #1 / R11 T6：全市场重点监控池（硬风险告警）

- **分支**：`TF-Leo/market-watch`（父 `origin/main` = `c357b28`）；未改
  `source/decision/*`，未引入新依赖。
- **端点取证（不是凭记忆）**：`curl` 钉死 commit `f215349` 的 SKILL.md §8.4
  （4525–4583 行）核对 URL/字段，再本机实测
  `GET https://mobappconfig.securities.eastmoney.com/emcfg/stock_monitor.json`
  → **HTTP 200、0.20s、19 条**；字段恰为
  `MARKET/STKNAME/STKCODE/VALIDATESTARTDATE/VALIDATEENDDATE/LINK_URL`。
  实测样本里 **3 条 `MARKET="B"`**（920575/920090/920023，均 `*ST`），
  与上游「三值坑」记录一致。
- **实现**：
  - 新增 `source/stock_analysis/data_sources/market_watch.py`：一次请求 →
    原始条目 → `{code: {code,name,market,start,end,reason,link}}`；
    `_MARKET_MAP = {"1":"SH","0":"SZ","B":"BJ"}`，未知值原样带出 `?<值>`；
    3s 超时；非 2xx / 非 JSON / 非列表 / 行字段不合法一律抛
    `MarketWatchError`（**不返回空 dict**）；`[]` 视为合法空结果。
  - 缓存 `source/stock_analysis/data/state/market_watch.json`（`data/` 已
    gitignore），含 `fetched_at` / `session_date` / 原始 `entries`；当日有效，
    同进程 memo + 磁盘双层。失败回退旧缓存标 `stale=true`；无缓存
    `available=false`。
  - `cli.py`：`evidence.market_watch = {available,stale,as_of,hit}`（additive）。
    G35 错误路径用 `with_market_watch_block()` 包一层补同构块 —— **不改
    `decision/*`**（第一版曾改 `decision/context.py`，按约束已撤回）。
  - `advice_render.py`：命中时输出 `🚫 交易所重点监控（{start}~{end}）`，
    位置在可执行性闸门之后、价格行之前；未命中/不可用/无命中时过期整行消失。
  - `--as-of` 回放**抑制命中**（只有当前快照，套到过去等于前视），并在
    warnings 显式说明「已抑制」，不写成「那天没被监控」。
- **测试**：新增 `source/tests/test_market_watch.py`（38 项）：解析（命中/未命中/
  **北交所 B→BJ 非 SZ**/未知市场/4 类非法行）、空结果 vs 接口坏了（4 类）、
  缓存（1 次请求服务 10 票、当日二次 0 请求、失败回退 stale、坏缓存）、
  窗口过滤、as-of 抑制、渲染位置/消失/不改 action、G35 同构、端到端注入命中。
- **证据（本机 Linux，无 pwsh）**：
  - `pytest source/tests -q` → **818 passed, 1 failed**；唯一失败
    `test_probe_requirements.py::test_current_environment_passes_via_probe`
    是**基线就存在**的解释器版本不符（akshare 1.17.44 vs 1.18.29 等），与本次无关。
  - 真实取数：`load_pool()` → `available=True stale=False as_of=2026-11-14
    n=19 sec=0.196`，markets `{SH,SZ,BJ}`；**第二次 0 请求、0.0004s**。
  - 真实 CLI（公开基准 600519）：`market_watch.available=true, hit=null`，
    `evidence` 18 键，`--fast` 墙钟 23.1s、退出码 10（no_edge，与基线一致）。
  - 真实命中（公开标的 605255，2026-11-13~11-26 在监控窗口内）：CLI 输出
    `hit={listed:true,start:2026-11-13,end:2026-11-26,reason:monitor}`；
    渲染输出 `【今日不建议执行】收盘涨停…` → `🚫 交易所重点监控
    （2026-11-13~2026-11-26）` → `605255 天普股份 现价 73.82`，位置符合契约。
  - `source/docs/example_output_600519.json` 只补 `market_watch` 键（沿用
    #93 的最小改动做法，不重跑整份样例以免行情数据无谓漂移）。
- **隐私**：测试与本文只用公开/虚构代码（600000/920999/605255/600519）；
  未出现真实持仓代码、名称、金额、股数、权重。
- **风险（如实记录）**：
  1. `reason` 是**本模块派生**（上游只给名单不给原因），仅按名称 ST 前缀分
     `risk_warning` / `monitor` 两值；它不影响命中判定。
  2. 监控池只有**当前快照**，无历史版本 → `--as-of` 回放不提供历史监控状态。
  3. 接口当日不可达时，若缓存日期已旧，`stale=true` 仍会展示旧名单并报命中；
     文案已标注过期，但**不阻止**渲染该行（命中是风险提示，宁可多报）。
  4. 池子通常十几只，命中率低 → 已用端到端注入命中的用例证明链路不是死代码。
  5. `cli.py` 行数基线 761（G10 的 ≤700 在基线即为红）；本次 +17 行至 778，
     未修复该既有超限。

## 2026-11-14 · issue #11（P1）冷启动数据完整性：腾讯日线缺根导致策略漂移

- **分支**：`TF-Leo/cold-start-gap`（基线 `1c86274`）；未改 `source/decision/*`，
  未引入新依赖。
- **根因（Hermes review 后修正为「精度是主因、缺根是次因」）**：
  1. **主因：报价精度**。腾讯通道返回「原始价 × 新浪比例因子」的未取整结果
     （4 位小数），新浪/东财返回 0.01 元报价单位取整后的价格。逐项隔离
     （6041/6042 根、581 组网格）：
     - 腾讯全量（4 位小数）→ CMF(30,0.1/-0.1) 0.4897
     - 腾讯 + 补 2007-09-04 → 仍是 CMF 0.4897
     - 腾讯 + 补根 + 12 个价格异常日换新浪 → CMF 0.4879
     - 腾讯 + 全部成交量换新浪 → CMF
     - 新浪全量 → 唐奇安(30/20) 0.3810
     - **腾讯 OHLC 归一到 0.01 元 → 唐奇安(30/20) 0.3809**
  2. **次因：缺根**（Hermes 复现）。同一 vintage、数据已归一的条件下，**删掉
     任意一根**，top-1 就从 0.3810 翻到 0.4133。因此本 PR 不再写「缺口不是
     触发点」这种绝对说法：精度是主因、缺根是次因，两者都属「数据集状态改变
     结论」，都需要显式检测/标注。
  3. 源端缺口事实：腾讯 `fqkline` 2007-09-04 缺根，且不是分页边界
     （700 天窗口第 9 页 2007-04-02→2009-03-01 的中段）；窄区间单请求
     （2007-08-20~10-10）同样没有该根，09-03/09-05 都在。
- **修复**：
  - 新增 `source/stock_analysis/data_loader/gapfill.py`：缺失工作日 → 官方日历
    确认 → 非主源日期集比对 → 邻近日口径校验（价 ≤0.01 元、量 ≤100 股）→
    补入并标注来源；补不到/口径不一致/日历不可用均显式 warning。
  - **日历 fail-closed**（Hermes 第 2 条）：只有官方日历明确判为交易日（True）
    才补；日历取不到 → 一根都不补，仅输出「数据完整性：… 未做缺口核验
    （日历不可用）」。旧实现会按参照源日期集补入节假日/周末（实测 7 根）。
  - `meta.py`：`_LAST_FETCH_INFO` 增加 `gap_filled_dates` / `gap_fill_sources`
    / `gap_warnings`（additive，schema_version 未改）。**报价精度归一改为
    唯一入口**：`_prepare_indexed_frame`（冷启动 / 增量 / 全量重取 / 参照源
    共用）归一到 0.01 元，`_merge_with_cache` 的合并结果再归一一次（兼容旧
    4 位缓存）；新增测试锁住「同标的同一天，冷启动与增量路径逐值相等」。
  - `cli.py`：`gap_warnings` → `diagnostics.warnings`（前缀「数据完整性：」）。
  - 新增 `source/tests/test_gap_fill.py`（14 项离线 fixture：检测/补齐/失败告警/
    邻近日口径/日历 fail-closed/无网络早退/冷增量一致性/meta 集成）。
  - **近端重抓语义**（Hermes 第 5 条）：渠道池 `fetch()` 只有起始日、没有结束
    日，实际请求是「缺口日前 5 天 → 最新交易日」；对 2007 年的缺口等于**多一次
    全历史请求**（腾讯分页约 19 次 HTTP）。代码注释与 PR 已如实标注，不再
    自称「窄区间」。
- **影响面（Hermes 复验，12 只样本）**：**3 只 top-1 翻转**；`000001` 归一后
  最大相对误差 **4.38%**。仍接受该方案：CMF 对亚分噪声过敏，归一到 A 股报价
  单位后与新浪/东财口径一致、排名更稳；代价是低价/高分红老券的复权价相对误差
  变大（600519 最低复权价 2.89 元时 <0.35%，000001 最坏 4.38%），两项均已如实
  写入 PR，不只报有利数字。
- **证据**（本机 Linux，无 pwsh，门禁以 `tools/probe.py` 等价执行）：
  - `pytest tests/ -q` → **632 passed**（原 618 + 新增 14）。
  - `tools/probe.py all` → 41 keys，除 `as_of` BLOCKED（本 worktree 无
    `stock_600519_qfq.csv` 缓存）外全部 PASS；该 BLOCKED 与本次改动无关。
  - 真实数据离线端到端（腾讯 6041 + 新浪参照）：补 2007-09-04 → **6042 根**，
    `gap_filled_dates=['2007-09-04']`，锦标赛 **唐奇安(30/20)**。
  - 真实冷启动 CLI（清缓存，2026-11-14 14:56）：`bars=6042`、`source=astock_tencent`、
    `status=no_edge`、top-1 **`唐奇安(30/20)` 0.3809**；
    `gap_filled_dates=['2007-09-04']`、来源 `sina_stock`。
    紧随其后的增量路径：`bars=6042`、`source=cache`、同 `status`、同 top-1 0.3809。
  - 腾讯 WAF 冷却期间按 `tencent_throttle` 规则等待到期后复测，未绕过。
- **隐私（Hermes 第 6 条）**：原分支中间提交的 `PROGRESS.md` blob 含真实持仓
  代码。已把分支压成**一个提交**后 force-push；本次新增段落一律使用公开基准
  代码（600519），历史行按既定口径不动，commit message 不含该代码。
- **遗留风险**：见 PR 描述。

---

## 2026-11-13 · issue #13（P0）腾讯限流：跨进程冷却 / 请求预算 / 快速切备胎

- **分支**：`task/p0-tencent-throttle`（基线 `d440c94`）
- **范围**：`source/stock_analysis/data_sources/tencent_throttle.py`（新增）、
  `data_sources/astock.py`、`data_loader/bars.py`、`tools/context_ic.py`、
  `source/tests/conftest.py`、`source/tests/test_tencent_throttle.py`（新增）、
  `docs/ASTOCK_INTEGRATION.md` §5.1.1。
  **未改** `source/decision/actions.py`（`git diff origin/main...HEAD -- source/decision/actions.py` 为空）。
- **未改上游生成物**：`_astock_generated.py` 仍是快照可复验产物（`tools/astock_extract.py --check` 通过）；
  冷却能力全部实现在适配层，通过 `_tencent_host_down_until` 这个既有注入点生效。
- **证据**（本机 Linux，无 `pwsh`，门禁脚本以 `tools/probe.py` 等价执行）：
  - `pytest tests/ -q` → **596 passed**（原 578 + 新增 18）。
  - `tools/probe.py all` → 41 keys，除 `windows` 外全 PASS；`windows` 在基线
    `d440c94` 上同样 FAIL（同一 reason `action 跨训练窗口不一致`），属既有数据依赖问题。
  - 20 只冷启动演练：**100 次请求 / 32.4s / 0 失败 / 无冷却 / 最长连败 0**。
  - 打桩 429 批量演练：**腾讯 HTTP 仅 3 次**（主源连败 3 只即停用）→ 20 只全部 baostock 补齐，
    **0 失败 / 最长连败 0**。
  - 跨进程：进程 A 3 请求触发冷却 → 进程 B **0 请求**（`TencentThrottled`）→ 冷却过期后恢复 4 请求成功。

---

## 2026-11-13 · issue #67（D2 验收退回）：单通道阻塞也受单票预算约束

V19 在 PR #66（`4f98bfb`）上实测：3s 预算 + 单通道阻塞 12s → 墙钟 **12.01s**
（预算 4 倍，`interrupted=false`）。根因：`bars.py` 的**单通道调用**没有套
`run_with_deadline`，预算只约束「通道之间」；且单次 HTTP 仍是 `(5, 15)`，与
小预算错配。

- **修复**（分支 `task/p0-tencent-throttle`，HEAD `4f98bfb` 之上）：
  1. `_DataSourcePool.fetch` 对**每个通道**调用 `run_with_deadline(…, 剩余预算)`；
     超时记 `TencentBudgetExceeded` attempt、停止剩余通道、调用方回退缓存并 `degraded`。
  2. 新增 `tencent_throttle.http_timeout_for_budget()`：预算充足保持 `(5, 15)`；
     收紧时 connect/read 收敛到预算内（3s → `(1.0, 2.0)`）。
     `astock.fetch_daily(..., budget=…)` 在取数窗口内应用、退出还原默认值；
     渠道池把剩余预算透传给腾讯通道（akshare 通道不接受 per-call 超时，靠墙钟兜底）。
  3. **未改** `_astock_generated.py`（仍是快照可复验产物）、**未改** `decision/actions.py`。
- **证据**（命令原始输出见 PR #66 正文）：
  - D2 生产路径（`meta.get_stock_daily` + 旧缓存）：3s 预算 / 12s 阻塞通道 →
    **3.01s**，`degraded=true`，`source=cache`（修复前 12.01s）。
  - 20s 预算 / 45s 挂死 → **20.01s**（修复前 45.0s）。
  - `fetch_daily(budget=3)` 三次入口 HTTP 实际 timeout 均为 `(1.0, 2.0)`，退出还原 `(5, 15)`。
  - `pytest tests/ -q` → **603 passed**（596 + 新增 7）。
- **遗留风险**：Python 无法强杀线程，上限是**协作式**——调用线程按时返回，
  被放弃的 daemon 线程自行结束；其单次 HTTP 超时已压到预算量级，最坏情况
  仍靠 HTTP timeout 兜底。

---

- **最后更新**：2026-10-25 14:20（**F22–F34 全部完成 13/13**；门禁 48/48；外部验证 V2–V6 已执行）
- **当前 HEAD**：`01ee33b`（V2–V5 验证记录已推送）｜工作树：V6 台账记录待提交
- **当前基线**：**门禁 48/48 退出码 0**｜`pytest` **67 passed**｜probe **41 keys 全 PASS**｜`cli.py` 665 行
- **本轮状态**：v3.0 决策层重写 + F32 打桩自检 + F34 语义语料，全部完成。
- **门禁已由本机自证**：`check_gates.ps1 -Full -Python <py3.12>` → `PASS 48 / FAIL 0 / BLOCKED 0 / SKIP 0`，退出码 0。
  首次失败的原因是门禁调用了 PATH 上的 `python`（3.13，无 pandas），**属环境问题**；已加 `-Python` / `PYTHON_BIN` 覆盖。
- **本轮结论（一句话）**：v1.0 解决了"数字可不可信"，没解决"字面意思对不对"。002415 的事故不是模型看错一天，而是**动作、文案、数字、统计标签四者互不一致**，且验证这三者的门禁已经失效。
- **完成定义不变**：`pwsh -File tools/check_gates.ps1 -Full` 退出码 = 0。任何自我判断的"完成"都不算。

---

## 0. 当前状态

| 项 | 值 | 备注 |
|---|---|---|
| 契约 | `GOAL_CONTRACT.md` **v3.0** | §7 决策表、§8 稳定性实验、§9 验收均已按实测修订 |
| 清单 | `GOAL_FIXLIST.md` **F22–F34** | **13/13 全部完成** |
| 门禁 | **48 项**（probe 41 keys） | 新增 G40 语义 / G41 证据 / G42 信号统计 / G43 成交口径 / G44 打桩自检 / G45 as-of / G46 回放 / G47 有效期 / G48 语料 |
| 测试 | **67 passed**（原 47） | 新增 `tests/test_semantic_actions.py` 20 项 |
| 待做 | 无（本清单内） | 外部验证见 `VERIFICATION_HANDOFF.md` |

### 0.1 本轮为何开新一轮（不是返工）

v1.0 的验收标准全部围绕"数字是否可信"：数据泄漏、资金基准、多重检验、schema 同构、退出码可达。这些**已经做到**，下游 Agent 也独立复现过（`pytest` 47 passed、门禁 39/39、冷克隆可跑、场外基金可用）。

但用户拿实盘结果一对，就发现了 v1.0 从未检验的一层：**输出的字面意思与内部事实不一致**。

这不是"再加几个断言"能解决的，因为：
- 动作与文案由两条互不知情的代码路径生成（`cli.py:389` 在 `action` 之前就写好了文案）；
- `near_sell` 的真实含义是"近 10 根内出现过卖点"，却被渲染成"卖出信号"；
- 文案里的数字是收盘价，不是任何指标读数；
- `significant` 的判据是"不能排除正收益"，名字却是"显著"。

所以本轮的目标不是"让输出更好看"，而是**让语义有唯一真源、让评价对准下游实际收到的规则、让验证重新可信**。

---

## 1. 本轮诊断证据（可复现）

### 1.1 事故标的：002415 海康威视

数据：本地前复权缓存 `stock_analysis/data/stock_002415_qfq.csv`，**3928 根**。
复现方式：截断 `meta.resolve_and_fetch` 到指定日期后跑 `cli.advise_internal()`（`--as-of` 尚未实现，F31 要做）。

| 日期 | 收盘 | 原始信号 | `action` | `action_reason` | `trigger.state` |
|---|---|---|---|---|---|
| 2026-10-15 | 33.86 | MA3/MA30 **金叉** | — | — | — |
| 2026-10-20 | 32.82 | MA3/MA30 **死叉**（`sell=True`） | — | — | — |
| 2026-10-22 | 32.42 | `sell=False` | `hold` | `均线趋势(3-30-60): 卖出信号 (32.42)` | `near_sell` |
| 2026-10-23 | **33.78（+4.19%）** | `sell=False` | `hold` | `均线趋势(3-30-60): 卖出信号 (33.78)` | `near_sell` |

**关键点**：10/23 是大涨日，文案仍然写"卖出信号"，只把括号里的数字从 32.42 换成了 33.78。
→ 用户看到的不是一次性误报，而是**持续误报**：一次死叉会在其后 10 个交易日里天天被描述为"卖出信号"。

### 1.2 同一天的四组数字，互相矛盾

```
action                 = hold
action_reason          = "均线趋势(3-30-60): 卖出信号 (32.42)"
trigger.state          = near_sell
trigger.current        = 32.42      ← 与 data.last_close 精确相等
signal.sell（原始）    = False      ← 当日根本没有卖点
last_sell_date         = 2026-10-20（死叉日，收盘 32.82）
```

四句话描述同一天，两句说"卖"，两句说"没卖"。

### 1.3 统计标签同样在撒谎

002415 @2026-10-22：

```
significant       = true
deflated_sharpe   = 0.051
excess.point      = -6.24e-4 / 日      ← 策略跑输买入持有
excess.ci_95      = [-1.29e-3, +2.42e-5]
excess.p_positive = 0.035              ← 96.5% 的重抽样样本认为跑输
excess.n_obs      = 3926
```

`significant=true` 的判据是 `ci_hi > 0`（`cli.py:418`），含义只是"**不能排除**正收益"。
但字段名与下游解读都是"统计显著"。**Hermes 读到 `significant=true` + `status=ok`，会把这个策略当成有依据的。**

### 1.4 五条根因（代码位置已核）

| 编号 | 根因 | 位置 |
|---|---|---|
| R1 | 动作与文案由两条互不知情的路径生成；文案先算、动作后算，`format_trigger_message` 的 `action` 形参是死参数 | `cli.py:389`、`triggers.py:115` |
| R2 | `near_*` 把"历史上发生过"当成"现在正在发生"，不携带日期与年龄 | `triggers.py:24,38-41,122-123` |
| R3 | 回测跑原始单根信号、线上叠 15 根过滤、下游读近 10 日文案——三套口径 | `engine.py:155-156`、`cli.py:419-423` |
| R4 | `significant` 判据（CI 上界>0）与名字（显著）不匹配 | `cli.py:418` |
| R5 | G06 三窗口门禁打桩点失效，三窗口跑的是同一份数据 | `probe.py:1016-1030` vs `cli.py:241-242` |

### 1.5 R5 的实测证据（**本轮最需要先修的东西**）

`probe.py::check_windows` 打桩 `cli.get_stock_daily`，而取数早已改走 `meta.resolve_and_fetch`。
`cli.get_stock_daily` 只剩 `cli.py:12` 的 import，**零调用点**。

```
expected_bars_if_drop120_worked = 3808
actual_bars                     = 3928
actual_end                      = 2026-10-23
```

→ G06 的 PASS 只能证明"同一份数据跑三次结果相同"。**它现在是假绿。**
这也解释了 v1.0 末尾反复出现的"预测全绿、实跑见红"——验证层本身没有被验证。

### 1.6 15 根过滤器的来源（为什么它必须被拆掉）

`cli.py:419-423` 要求"最近 15 根全部为真"才算强触发。它的注释写明动机：

> 避免瞬时单根信号在三窗口（full/drop60/drop120）间翻转 —— G06 稳定的前提

也就是说：**为了满足 v1.0 B6 的"三窗口 action 必须一致"，实现里加了一个让事件型信号永远无法触发的过滤器。**
均线死叉是单根事件，`sell.iloc[-15:].all()` 恒为 False → `action` 被机制性钉死为 `hold`。

这是"用语义错误换稳定"的典型。v2.0 §8 已把不变式改为"`position_state` 与 `family` 一致 + `action_stable` 如实上报 + 三窗口 `bars` 必须不同"，并明确禁止重新引入此类过滤器。

---

## 2. 本轮跟踪表（**唯一需要逐条勾选的地方**）

| 状态 | 条目 | 批次 | 一句话 | 门禁 |
|---|---|---|---|---|
| `[x]` | F22 | P0 | 修 G06 打桩失效（三窗口假绿） | 加强 G06 |
| `[x]` | F26 | P0 | 拆 15 根过滤，落地决策表 | G40 |
| `[x]` | F24 | P0 | `near_sell` 拆分"曾发生"与"接近" | G40 |
| `[x]` | F25 | P0 | `trigger.current` 改指标值 | G40 |
| `[x]` | F23 | P0 | 文案单出口，动作后置 | G40 |
| `[x]` | F27 | P1 | `significant` 收紧 + `edge.verdict` | G41 |
| `[x]` | F28 | P1 | 信号级统计（卖点后 1/3/5/10/20 日） | G42 |
| `[x]` | F29 | P1 | 成交时点可配 + 声明 | G43 |
| `[x]` | F30 | P1 | 决策回放（评价线上规则） | G42 |
| `[x]` | F31 | P2 | `--as-of` 历史复盘 | G40 |
| `[x]` | F32 | P1 | 打桩自检通用化 | G44 |
| `[x]` | F33 | P2 | README 契约章节同步 | C2 |
| `[x]` | F34 | P2 | 9 只标的语义回归语料 | G40 |

**每修完一条，必须在本表把 `[ ]` 改 `[x]`，并在 §2.1 追加一行实测证据（命令 + 输出片段 + 退出码 + 时间）。**

### 2.1 逐条实测记录（修完一条写一行，禁止预填）

| 条目 | 命令 / 提交 | 实测输出 | 时间 |
|---|---|---|---|
| F22 | `python tools/probe.py windows` · `1ba98c8` | `bars=6027/5967/5907`（互不相同）；`position_state` 三窗口均 `flat`；`action` 均 `avoid`；`family_drift=true` 如实上报 → **PASS** | 2026-10-25 |
| F24/F25 | 截断 `meta.resolve_and_fetch` 到 2026-10-22 · `954df2e` | `position_state=flat`、`state=near_buy`、`last_sell_date=2026-10-20`、`last_sell_age=2`、`MA3/MA30=0.9795`、`last_close=32.42` | 同上 |
| F23 | 同上 | `action_reason = ["策略当前空仓（2026-10-20 卖点离场，距今 2 个交易日）；今日无新信号，维持当前仓位；指标已接近买点阈值，但尚未触发"]`；**不含"卖出信号"** | 同上 |
| F26 | `pytest tests/test_semantic_actions.py` | 13 passed；单根金叉 → `buy`，单根死叉 → `sell`（旧实现恒为 `hold`） | 同上 |
| F27 | 同上截断脚本 · `090660b` | `edge.verdict=inconclusive`、`significant=false`、`p_positive=0.035`、warning 含"不应作为择时依据" | 同上 |
| F28 | 同上 | `n_buy=199 / n_sell=108`；卖点后 5 日 `hit_rate=0.514`、误报率 `0.4766`、踏空均值 `+4.19%`；买点后 5 日 `hit_rate=0.538`、均值 `+1.06%` | 同上 |
| 全量 | `pytest tests/ -q`；`python tools/probe.py all` | **60 passed / 9.16s**；**37 keys 全 PASS**（G35 `missing_paths=0`） | 同上 |
| F29 | 合成序列 `exec_price` 对照 · `1b9f567` | 同一信号：`next_open` 入场 **10.1707** vs `next_close` **10.3783**；`data.execution.fill=next_open` | 2026-10-25 |
| F31 | `cli.py 002415 --as-of 2026-10-22 --json --fast` | `bars=3927`、`end=2026-10-22`、`last_close=32.42`、`request.as_of` 回显、`window_note="--as-of 2026-10-22 截取后 3927 根"`；`--as-of 2026-10-23` → 3928 / 33.78 | 同上 |
| G06 设计更正 | `python tools/probe.py windows` | 固定决策日、从头砍训练窗口：600519 `action=avoid` ×3、`position_drift=true`（唐奇安 30/20→20/10）；002415 `hold/flat/near_buy` ×3 完全一致 | 同上 |
| F30 | `cli.py 002415 --as-of 2026-10-22 --json --fast` · `12fafc0` | `decision_replay.trade_count=0`、`exposure=0`、`vs_buy_hold=-8.79`、`note="决策表未放行买入（significant=False…）"`、`method=trailing_250_point_estimate`；600519 同类，note 为 `status=no_edge` | 2026-10-25 |
| F33 | `grep` README + JSON 解析 | 两个 ```json 块均可 `json.loads`；新增「v2.0 语义变更（消费方必读）」与 signal/edge/signal_stats/decision_replay/execution 字段说明 | 同上 |

**关键实测（002415 两日对照，修复前后）**

| 日期 | 修复前 | 修复后 |
|---|---|---|
| 10-22 | `hold` + `卖出信号 (32.42)`；`current=32.42`（=收盘价） | `hold` + `策略当前空仓（2026-10-20 卖点离场，距今 2 个交易日）`；`current=0.9795`（MA3/MA30） |
| 10-23（+4.19%） | `hold` + `卖出信号 (33.78)` | `hold` + `…；指标已接近买点阈值，但尚未触发`；`current=0.9903` |

---

## 3. 未验证 / 不得声称

- **F32/F34 未验证**：打桩自检通用化、9 只标的语义语料**尚未实现**。
- **决策回放是近似口径**：`method=trailing_250_point_estimate`，`status`/`confidence`/`significant` 用全样本值，`edge` 用 250 根滚动点估计。**不是 walk-forward**，不得如此宣称。
- **`action=buy` 当前几乎不可达**：需 `edge.verdict=positive_significant` 且 `significant=True`，实测多数标的为 `inconclusive`，因此 `decision_replay` 常为零交易（已在 `note` 说明）。这是"买入靠证据"的直接后果；若要提高可交易性，需单独提案调整证据门槛，**不得**偷偷放宽。
- **`check_gates.ps1` 本轮未执行**（宿主 pwsh 沙箱创建失败：`LsSDK_CreateSandbox(ConPTY) 0x6`）。只有 `probe.py` 的 37 keys 全 PASS，**不能据此声称 44/44**；需用户跑一次 `-Full`。
- **G06 的实验设计本轮被更正**：原实现从**末尾**砍 60/120 根，等于把决策日一起往前挪，测到的是"仓位随时间变化"而非稳定性。改为固定决策日、从头砍训练窗口；判据 L1 改为 `action`。实测 600519 `action` 三窗口一致、`position_state` 因胜出变体变化而漂移（已如实上报 `position_drift=true`）。
- **`position_state` 在 600519 上不跨训练窗口稳定**：丢最老 120 根后唐奇安参数从 (30/20) 变 (20/10)。这是真实性质，**未修**；因该标的 `status=no_edge`，不影响建议。若将来要让仓位稳定，应做族级共识/滞回，禁止加过滤器。
- **`significant` 已收紧**为"CI 下界>0 且 DSR>0"。历史输出（含 Hermes 侧缓存）里的 `significant=true` 不可直接沿用。
- **指标值覆盖不完整**：`current_value` 只对 10 个可复算族（均线趋势/MACD/KDJ/唐奇安/StochRSI/CCI/WilliamsR/KAMA/布林）有值，其余族返回 `null` 并在 `diagnostics.warnings` 说明。**不得**为了"看起来完整"回填收盘价。
- 退出码 40（untradeable）真实数据下不可达（自适应资金使 `unaffordable_lot_events` 恒 0），仅单测覆盖——此结论仍然有效。
- **门禁绿 ≠ 台账全清 ≠ 结论正确**：门禁只校验输出与行为的自洽，不校验金融结论是否赚钱。
- 上一轮记录的 `github.com` DNS 解析失败（推送阻塞）**本轮未复核**，推送前需重新确认。
- **Python 3.13** 仍只有依赖解析证据，无运行证据（3.11/3.12/3.14 已真机跑通）。

---

## 4. 每轮固定动作

1. 读本文件（§0、§1、§3）→ 读 `GOAL_CONTRACT.md` §6–§8 → 读 `GOAL_FIXLIST.md` §0.2 的顺序。
2. `cd source; python -m pytest tests/ -q`，确认基线未退化。
3. **只做一个条目**，做完立刻跑对应门禁。
4. 立刻回写 §2 跟踪表 + §2.1 实测记录。**禁止只交总结不写台账。**
5. 全部条目完成后跑 `pwsh -File tools/check_gates.ps1 -Full`（不带 `-Quick`）。
6. 未全绿就继续下一个条目，不要输出"阶段性总结"就停。

**打桩纪律（新增，来自 R5）**：任何 monkeypatch 之后必须紧跟一条生效断言，否则该门禁视为无效。

---

## 5. 历史档案索引（v1.0 全过程，全文见 `git show d4264a4:PROGRESS.md`）

> 压缩为索引是为了让每轮读的台账保持可读。**原始证据（命令、原始输出、退出码、时间）全部在 git 历史里**，需要时按提交号取。

| 原节 | 主题 | 结果 | 关键数字 |
|---|---|---|---|
| §1 | 早期 14 项修复（语法崩溃、JSON 序列化、regime 去重、单一真源 API、stdout 纯净） | 完成 | 28 passed |
| §1.5 | **F01–F21 二十一条**（基金跑股票网格、退出码 20/40 不可达、n_trials 口径、significant 矛盾、8 处伪造数字、族名切分、族占比、CI 未接线、time_in_market 死码、sizing 缺失、评分未换、ATR 止损、walk-forward 假、旧参数消失、requirements 漂移、基金网格三方矛盾、D1 清理、字段重复、空壳缓存） | 21/21 完成 | 门禁 14 → 32 |
| §1.6 | 主回合复核 1：ps1 退出码 26 的根因是 `2>&1` 把 stderr 混进 JSON | 修复 | PASS 32 / 0 |
| §1.7 | 4 条遗留（删分叉裁定器、G29 改真 subprocess、docstring 对齐、降级可见）+ 3 个数据层 bug + numpy 主循环优化 | 完成 | `--fast` 58.3s → 9.8s |
| §1.8 | 复核 2：numpy 优化等价性（19/19 字段一致）；探针 deepcopy 防假 PASS | 完成 | 34/34 |
| §1.9 | R1 错误路径同构（88 条键缺失 → 0）、R2 契约同步、R3 快照三列；新增 G35 | 完成 | 134 / 116 路径 |
| §1.9b | 复核 3：`cli.py` 765 行超 D4 上限 → 迁 `create_error_response` 到 schema，降至 672；G35 扩双标的 | 完成 | 35/35 |
| §1.10 | 冷克隆测试失败（A14 欠账）、py3.14 依赖冲突、`warm_cache.py` | 完成 | 47 passed（冷/热一致） |
| §1.11 | 移除 Python 版本限制（用户指正：不得拿换环境当修法） | 完成 | 3.11/3.12 真机 |
| §1.12 | 渠道池接线（东财封 IP 致取数全死；`bars.py` 早已实现却未调用） | 完成 | G36 |
| §1.13 | Hermes 全项自查通过；三处待定性问题答复（CI 数组粒度、`no_edge`/`window_note` 补登、qfq 混源一致性） | 完成 | 36/36 |
| §1.14 | `window_note` 错误路径硬编码 null → 真值；G37 + 变异测试 | 完成 | 37/37 |
| §1.15 | Hermes 增量验证全过；修正 3.14 的过期表述 | 完成 | 37/37 |
| §1.16 | 实盘暴露：`entry_zone` 倒置（G38）、场外基金 OTC 通道 + `auto` 双向探测（G39） | 完成 | 39/39 |

**v1.0 已确认通过的项（不要重复劳动）**：测试网 47 passed、门禁 39/39、错误路径同构、`window_note` 三档、OTC 基金、`entry_zone` 区间有效、DSR 公式本身正确、渠道池降级、幂等、`cli.py` ≤ 700。

---

## 6. 历史失败时间线（规避依据，别重复踩）

### 2026-10-16 15:10 → 10-17 04:28（13 小时，612 条消息）

用户 14 条消息里 13 条是"继续/不要停"。七类根因：
反复重开目标导致范围从 Phase A 砍到只剩 D2/D3/D5；Edit 语法 25 次失败；Bash 里写 PowerShell 语法；**用源码常量字符串冒充门禁通过**；整文件 Write 重写 `cli.py` 导致 `main()` 消失；上下文压缩后失忆只交总结不干活。

### 2026-10-17 15:54 首个自驱回合

跑了 37 分钟、17 次工具调用，仓库零改动。真因是宿主 PowerShell RPC 全线超时（7 次连续 `host rpc budget exceeded`），连"写 blocked 标记"都要 shell，所以标记也没写成。

### 2026-10-18 全天：三轮"预测全绿、实跑见红"

同一个错误犯了三次：子 Agent 每次都做"静态一致性检查"（映射 key 零差异、G01–G35 无缺号），但那只覆盖 probe 驱动的门禁，**覆盖不到 ps1 里独立实现的静态门禁**（G09/G10/G11/G13）。第三次直接踩了 `cli.py` 765 行 > 700。

### 2026-10-25（本轮触发）：实盘对不上

用户在 Hermes 上看到 002415 在 10/22 收到卖出风险提示，10/23 大涨 4.19%，且其他多只股票也有类似现象。
诊断结论：**不是模型预测错了一天，而是输出的字面意思与内部事实长期不一致**，而验证这件事的门禁（G06）已经失效。

**共同模式**：每次事故都不是"某个函数写错了"，而是**"验证某件事的手段本身没有被验证"**。
本轮把这一条写成了硬规则：打桩必须自检（F32 / G44）。

---

## 7. v3.0 架构重写（2026-10-25 11:00–11:20）

**触发**：用户指出"系统几乎不会给 buy"是设计失败的症状，判断正确。复审确认 v2.0 方向错了，
证据与推理见 `ARCHITECTURE_REVIEW.md`。

### 7.1 改了什么

| 模块 | 变化 |
|---|---|
| `decision/expectancy.py`（新） | 该侧条件期望（n / 均值 / 命中 / t 值 / ok）+ 无前视的滚动版本 |
| `decision/policy.py`（新） | 策略引擎：买点+期望入场；止损 / 止盈 / 时间止损出场；给出当前仓位与待执行动作 |
| `decision/actions.py`（重写） | `decide(status, pending, expectancy_ok, holding)`；**删除 significant 门控**；卖出只认风控 |
| `decision/signals.py` | `build(..., policy=)`：仓位与事件由策略引擎给出，趋势卖点只作描述 |
| `decision/render.py` | 文案带有效期、期望数值、风控类型；`print_detail` 从 cli 迁入 |
| `decision/evaluate.py` | 新增 `build_metrics`（从 cli 迁入） |
| `cli.py` | 665 行；输出 `target_position` / `review_date` / `side_expectancy` |
| 门禁 | G46 改为策略引擎口径；新增 **G47** 有效期契约；G03 删除"buy 必须显著" |

### 7.2 实测（002415）

| 日期 | 动作 | 说明 |
|---|---|---|
| 2026-10-16 | **buy**，目标 100%，复审 11-13 | 买点 + 20 日期望 +2.87%（n=231，命中 59.3%，t=4.2） |
| 2026-10-19 | hold（持有第 1/20 日，入场 33.33） | 已按次日开盘成交 |
| 2026-10-20 | **hold**（死叉日） | **不再卖出**：趋势卖出已废弃 |
| 2026-10-22 | hold（第 4/20 日，止损 31.65 / 止盈 36.03） | 10-23 大涨时仍在持有 |
| 2026-10-23 | hold（第 5/20 日） | — |

**这正是用户最初抱怨的场景：死叉不再触发卖出，因此不会在低点离场、错过次日 4.19%。**

策略引擎回放（002415 全历史）：121 笔，年化 +4.77%，最大回撤 45.2%，持仓占比 25.4%。
**显著跑输买入持有（-772 个百分点）**——因为该股长期上涨，而策略只有 25% 时间在场。
这是诚实的代价，不是 bug；是否要提高暴露或延长持有，属下一轮参数议题。

### 7.3 已知局限（不得声称）

- 有效期固定 20 日，未按标的自适应；14 只票的 IC 可能高估。
- 止损用 ATR(14)×2.5、clamp [3%, 20%]；未单独处理跳空（成交在次日开盘）。
- `action=buy` 只在"买点当日"出现（年均约 14 次/标的），其余日子是 hold。
- 回放使用**全样本选出的策略参数**，不是逐日重新选优；`method=policy_h20_risk_exit` 已声明。

### 7.4 权威门禁自证 46/46（推翻"跑不了"的旧结论）

此前多轮都记录"宿主 pwsh 沙箱创建失败，无法执行门禁"。本轮发现：**用 Python 子进程
调用 `pwsh` 是可以的**，真正的失败原因是门禁内部调用 PATH 上的 `python`（本机为 3.13.12，
未装 pandas/numpy），导致所有依赖 CLI 的门禁集体 `ModuleNotFoundError` 翻红。

修复：`check_gates.ps1` 增加 `-Python` 参数与 `PYTHON_BIN` 环境变量（默认仍是 `python`，
不改变判定逻辑）。指定 3.12 解释器后：

```
PASS 46 | FAIL 0 | BLOCKED 0 | SKIP 0 → 退出码 0     （370.2s）
```

**教训**：环境差异会伪装成代码失败。门禁必须允许指定解释器，否则换台机器就会误判。
这条已写入 `VERIFICATION_HANDOFF.md` §1。

### 7.5 新增验证工具（`tools/`）

| 工具 | 用途 |
|---|---|
| `collect_evidence.py` | 一键取证：门禁 / pytest / probe / 幂等 / as-of / 耗时，产出 `evidence/<ts>/` + `REPORT.md` |
| `forward_log.py` | 前向台账：`record` 冻结今日建议，`evaluate` 在 20 个交易日后用真实行情结算 |
| `ic_report.py` | 横截面 IC 报告：按因子 × horizon 输出 IC/t/n 与分组价差 |

### 7.6 横截面证据（下一版方向的依据，尚未实现）

用 14 只票做横截面排序（`0.7×mom60_rank + 0.3×vol20_rank`，只做多 Top2），
**不重叠采样**（每 h 个交易日取一次，避免重叠窗口放大 t）：

| 持有期 | Top2 | 等权池 | 超额 | t | 胜率 | 样本 |
|---|---|---|---|---|---|---|
| 5 日 | +0.97% | +0.50% | +0.41% | 1.69 | 49.0% | 414 |
| 10 日 | +1.95% | +0.97% | +1.10% | **2.32** | 52.2% | 207 |
| 20 日 | +3.99% | +1.88% | **+2.55%** | **2.82** | 56.3% | 103 |
| 60 日 | +10.97% | +5.84% | +7.11% | **2.11** | 61.8% | 34 |

对比单标的择时（v3.0 现状：买点 20 日 +1.72%），**横截面排序的超额更强、更稳**。

**但这只是假设，不是结论**：
- 14 只票、8 年、单一市场，且权重 0.7/0.3 是我临时定的（有过拟合风险）；
- 未扣交易成本与换手（20 日换仓一次，双边约 0.13%）；
- 必须先用 `warm_cache.py` 扩到 200+ 只、`ic_report.py` 复验后才能立项。

**若复验成立**，下一版应是"排名 + 定期换仓"的组合层，而不是继续在单标的择时上加参数。

### 2026-10-25 10:00（执行第一批：P0 + F27/F28）

3 个提交：`954df2e`（语义单一真源）、`1ba98c8`（修 G06 假绿 + 移除伪造 stability）、`090660b`（significant 收紧 + 信号级统计）。
测试 47 → **60 passed**；probe **35 keys 全 PASS**；`cli.py` 694 → **693 行**（上限 700）。

三条值得记下的过程教训：

1. **首版 `decide()` 传了事件"后"的仓位去判断"此前是否空仓"，导致 `buy` 永不触发。**
   在写单测时才被发现。教训：状态机的"事件前/事件后"必须在函数签名上区分，不能靠调用方记住顺序。
2. **修好 G06 后，第二个假值立刻现形**：`consensus.stability` 一直硬编码
   `action_stable/family_stable=True`、`windows_tested=3`、`top_k_overlap_min=top_k//2`。
   假绿修好后，它掩盖的问题会自动暴露——这是"先修验证层"的直接收益。
3. **给错误路径加新字段时漏了 horizon 子键，G35 立刻报"缺 20 条路径"。**
   同构门禁确实在咬人，不是摆设。
4. **三窗口门禁本身测错了对象**：从末尾砍 60/120 根，等于把决策日也一起往前挪，
   不同日期的仓位本来就该不同。这个实验设计从 v1.0 就错了，而 15 根过滤器正是为它服务的。
   修好打桩后它立刻暴露——**验证手段错了，比没有验证更危险**，因为它会持续产出绿灯。

### 7.7 F32 / F34 收尾（13/13）

| 条目 | 做法 | 门禁 |
|---|---|---|
| **F32 打桩自检** | 新增 `_check_stub(label, expected, actual)`；给 probe 内**全部 5 处** monkeypatch（`check_windows` / `check_degrade_visibility` / `check_walk_forward` / `check_fund_cache_guard` / `check_source_failover`）补生效断言；新增 `check_stub_selfcheck()` 用 **AST** 扫描：凡有 `orig* = ...` 打桩的函数，必须出现 `_check_stub(` 调用 | **G44** |
| **F34 语义语料** | 新增 `check_corpus()`：4 只真实标的（002415 / 300750 / 601398 / 688981）取同一历史截面（2026-10-22），断言"flat 不得 near_sell、hold 不得含信号字样、sell 必须说明风控、current 不得等于 last_close"；缓存末日早于截面的标的记 `skipped` 并写明原因 | **G48** |

**为什么 F34 做成 probe 而不是 pytest**：9 只 × 3 窗口约需 9 分钟，会把 11 秒的测试套件拖到十分钟；
放进门禁只跑 4 只 × 1 截面（约 70s），既覆盖真实数据的不变式，又不拖慢日常迭代。
若要更广覆盖，用 `tools/ic_report.py` 扩票池，而不是加长 pytest。

最终验收：**门禁 48/48 退出码 0**、pytest **67 passed**、probe **41 keys 全 PASS**。

### 7.8 外部验证执行（2026-10-25 13:40–13:44，按 `VERIFICATION_HANDOFF.md`）

| 项 | 命令 | 实测结果 | 证据 |
|---|---|---|---|
| V2 冷克隆（本地） | `git clone --no-hardlinks D:/git/stock_trading_helper <tmp>` | HEAD `a726203`；缓存文件 **0**；`pytest` **67 passed / 10.43s**；`import cli` OK | `evidence/handoff-20261025/cold_clone.txt` |
| V2 冷克隆（GitHub，推送后） | `git clone https://github.com/TF-Leo/stock_trading_helper.git <tmp>` | HEAD `a726203`；缓存文件 **0**；`pytest` **67 passed / 11.52s**；`import cli` OK | 同上 |
| V3 CLI 语义 | `cli.py 002415 --json --fast --as-of 2026-10-22/23` | 两日均 `action=hold`、`target_position=1.0`、`position_state=holding`；`review_date=11-19/11-20`；全文无“卖出信号/风险提示/减仓” | `evidence/handoff-20261025/hermes.txt` |
| V4 推送 | `git push origin main` | `d4264a4..a726203`，`push_exit=0`；推送后工作树 clean | `evidence/handoff-20261025/push.txt` |
| V5 前向台账 | `python tools/forward_log.py record` | 新增 **12 行**，累计 **14 只**；`as_of=2026-10-23`；13 只 `flat/hold` + 002415 `holding` | `evidence/forward_log.csv` |
| V6 IC 复验 | `warm_cache.py` 沪深300 ×300 + `ic_report.py --min-bars 500` | 298 只可用（297 成分 + 002270）；默认区间 2018-01~2026-10：**mom60 20d IC -0.0171 (t=-2.95)**；2020+ 区间 -0.0261 (t=-4.73)；不重叠 20d **-0.0239 (t=-0.93)**、价差 **+0.499% (t=0.62)** | `evidence/handoff-20261025/V6_IC_REPORT.md` |

**V3 残留风险**：JSON 仍保留 `recommendation.signal.last_sell_date/last_sell_age`、
`evidence.signal_stats.sell`、`evidence.triggers[].sell_trigger` 等描述字段
（v3.0 趋势卖点不参与动作）。**Hermes 必须读 `recommendation.action` / `target_position` /
`position_state`，禁止按 "sell"/“卖点”关键词扫描**；否则仍会误报。
**V3 未完成**：Hermes 侧提示词原文、渲染输出、版本与取动作方式需用户补录（本机无 Hermes 入口，未伪造）。
**V5 尚未到期**：自 2026-10-23 起需 20 个交易日，`evaluate` 目前不会有 `matured=true` 行。
**V6 结论（2026-10-25 14:20）**：同一 2020+ 区间（1612 个交易日）下，14 只票 mom60 20 日
不重叠价差 **+4.17%（t=2.68, n=79）**；297 只沪深300 仅 **+0.499%（t=0.62, n=79）**，
20 日 IC 符号反转为 **-0.0239（t=-0.93）**。→ **14 只票的横截面动量是选样偏差，
不作为下一版主线；维持 v3.0 单标的买点+风控路线。** `lowvol20` 在大池 IC 为正、
Top-Bottom 为负（符号冲突），同样不可用。
局限：成分取 2026-10-23 名单（幸存者偏差）、新标的仅 2020+、未扣换手成本、非 walk-forward。

---

## 8. P1-2 日级资金面上下文（issue #36，2026-11-13，分支 `task/p1-flow-context`）

- **目标**：只展示、不改 action。预计算 + 缓存 + 只读展示；CLI / 9:15 cron 主路径**不触网**。
- **实现**：
  - `tools/astock_extract.py` 新增上游 §4.5 `stock_fund_flow_120d` 与 `em_market_code`
    （固定 commit `f215349`），生成物与 manifest 已重生成，`--check` EXIT=0（43 symbols）。
  - `tools/precompute_flow.py`：逐票串行、相邻请求 ≥1s、失败重试后继续，写
    `source/stock_analysis/data/flow/<code>.json`（gitignore）。
  - `source/stock_analysis/data_loader/flow.py`：只读缓存；摘要 `main_net_5d/20d`（元）、
    `direction`、`as_of`、`source`、`pub_lag_days=1`、`stale`（只读磁盘日历）；缺失返回 None。
  - CLI `evidence.flow` additive（15 键），错误路径同构；`recommendation` A/B 逐键一致。
  - README 新增 `evidence.flow` 字段表。
- **测试**：`pytest` **203 passed**（基线 181 + 新增 22）；新增、修改的实现文件均离线可测。
- **真实预计算（本机）**：东财 `push2his` 被出口 IP RST —
  `curl` HTTP 000 / `SSL routines::unexpected eof`；`precompute_flow.py 600519 002415`
  6 次请求、墙钟 10.19s、0 成功 2 失败（`RemoteDisconnected`）。**降级行为已按设计生效**：
  CLI 输出 `evidence.flow.available=false, reason="cache_missing"` + warning，`action/status` 不变。
  换可达网络后重跑同一条命令即可生成缓存，无需改代码。

---

## 9. P1-2 新浪资金流备胎 + 口径隔离（issue #39，2026-11-13，分支 `task/p1-flow-sina-backup`）

- **目标**：东财整域不可达时仍展示资金面，但**两种口径绝不混字段**；CLI 仍只读缓存、不触网。
- **实现**：
  - `tools/astock_extract.py` 新增上游「备用源速查」`fund_flow_backup`（固定 commit `f215349`），
    生成物/manifest 重生成，`--check` EXIT=0（44 symbols）。
  - `tools/precompute_flow.py`：东财优先；**首票单次探测**失败即 `eastmoney_down=True`，
    后续票不再请求东财（不逐票轰炸）→ 新浪备胎；缓存 JSON 增加 `source` / `caliber` /
    `attempts`；所有请求（含重试与备胎）仍强制相邻 ≥1s；失败不覆盖旧缓存。
  - `flow.py`：按 `source/caliber` 分支。东财只写 `main_net_5d/20d`（`net_*` 为 null）；
    新浪只写 `net_5d/20d`（总净流入，`main_net_*` 为 null），`direction_basis` 标
    `net_5d_sign` / `net_20d_sign`；缺 `caliber` 的 P1-2 旧缓存按东财口径兼容；
    未知/自相矛盾口径判 `cache_invalid`。`FLOW_KEYS` 15 → 18 键，三条路径保持同构。
  - CLI `evidence.flow` 新增 `caliber` / `net_5d` / `net_20d`；README 增加口径对照表并写明
    **两种口径不可直接比较**；`docs/P1_EVIDENCE_PLAN.md` §3.3 补充同一条约束。
- **测试**：`pytest source/tests -q` → **212 passed**（基线 203 + 新增 9）；新增/修改测试全部离线。
- **真实预计算（本机，默认 14 只）**：东财首票探测即 `ValueError: 空响应`（底层
  `RemoteDisconnected` / TLS RST）→ 自动新浪；
  `requests=15`（东财探测 1 + 新浪 14）、`ok=14 failed=0`、`wall=14.27s`、`interval=1.0s`，
  `eastmoney_ok=0 sina_ok=14`。缓存 600519：`source=sina_fund_flow_backup`、
  `caliber=sina_total_net`、`attempts={eastmoney:0,sina:1}`、120 行、
  `2026-05-20..2026-11-12`。
- **CLI 验收（600519）**：`available=true`、`source=sina_fund_flow_backup`、
  `caliber=sina_total_net`、`net_5d=-100186129.18`、`net_20d=12707604317.54`
  （与独立手算原始 `netamount` 逐位一致）、`main_net_5d/20d=null`、
  `direction=outflow/direction_basis=net_5d_sign`；全网封禁（socket 101）下同样出值，
  stderr 中东财请求 **0** 次。`--as-of 2026-11-12` → `as_of=2026-11-11`、
  `net_5d=841858947.53`、`net_20d=14105076066.16`（只用 `date < as-of` 的行，与手算一致）。
- **A/B**：同行情缓存、同 venv 下对比基线 `b189389`，`recommendation` / `market_regime` /
  `instrument` / `data` / `candidates` / `evidence`（除 flow）逐键一致，`action=hold` 不变。
- **门禁**：`pwsh` 本机未安装，`check_gates.ps1` 未运行（如实记录）；`pytest` 与
  `astock_extract.py --check` 为真实执行证据。

