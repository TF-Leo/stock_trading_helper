# 事件风险预计算（T7 / issue #1 / R11 缺口表 #7–#10）

> 实现：`source/stock_analysis/data_loader/events.py`（只读缓存）、
> `tools/precompute_events.py`（收盘后取数）、`source/stock_analysis/advice_render.py`
> 的 `_events_*`（渲染）。
>
> 上游：`simonlin1212/a-stock-data` v3.10.4，commit
> `f21534946c42be3ad048ac938eecb1bad84cb8ab`（本地快照
> `vendor/a-stock-data/SKILL.md`）。§3.6 / §14.1 / §14.3 / §14.5。

## 0. 一句话

**只在收盘后联网预计算，盘前只读本地缓存；四类事件各自独立可用/过期；取数
坏了显示「事件数据过期（{D}）」，绝不显示成「没有事件」。** 每轮另发 **4 个
对照探针**（每报表 1 次，与票池大小无关）区分「确实没事件」与「接口静默降级」；
过期判定只用**官方交易日历**。

不改 `decision/*`，不改 `action` / `confidence` / 仓位；`evidence.events` 是
additive 字段。

## 1. 端点取证（2026-11-14 本机实测，不是凭记忆）

全部经 `_astock_generated.em_get()`：≥1s 最小间隔 + 随机抖动 + Keep-Alive
会话复用。以下请求均为**单次、串行**，公开代码，不涉及任何真实持仓。

| 子项 | 上游 § | `reportName` | 请求参数 | 实测 |
|---|---|---|---|---|
| 解禁 | §3.6 | `RPT_LIFT_STAGE` | `filter=(SECURITY_CODE="…")(FREE_DATE>=…)(FREE_DATE<=…)`，`sort=FREE_DATE asc` | 688512 → 2 条（2026-09-04 历史、2026-11-16 未来）；600000/002475/300750 → 0 条 |
| 股东增减持 | §14.3 | `RPT_SHARE_HOLDER_INCREASE` | `filter=(SECURITY_CODE=…)(NOTICE_DATE>=as_of-60d)(NOTICE_DATE<=as_of)`，`sort=NOTICE_DATE desc` | 688512 → 1 条（2026-09-18，减持 118.47 万股） |
| 业绩预告 | §14.1 | `RPT_PUBLIC_OP_NEWPREDICT` | 同上日期窗口，`sort=NOTICE_DATE desc` | 002475 → 3 条（2026-10-30，2026 年报预告，按指标拆行） |
| 股权质押 | §14.5 | `RPT_CSDC_LIST` | `filter=(SECURITY_CODE=…)`，`sort=TRADE_DATE desc`，`pageSize=1` | 600000/002475/300750 → 各 1 条（最新统计日 2026-11-13）；688512 → 0 条 |

**12 次请求 / 12.23s**（3 只 × 4 端点，`--interval 1.0 --retries 0`）：
实测墙钟 ≈ 1.02s/请求，与串行下限一致。

### 1.1 实测校正的两个字段口径

1. **解禁股数用 `ABLE_FREE_SHARES`，不是 `FREE_SHARES`。**
   2026-11-14 全市场样本（`FREE_DATE` 2026-11-14~12-04）里，`FREE_RATIO`
   与 `ABLE_FREE_SHARES` 对得上、与 `FREE_SHARES` 对不上：
   - 600050：`ABLE_FREE_SHARES=25150.2` 万股，`FREE_RATIO=0.008115…`
     → 0.8115% ≈ 25150 万股 / 总股本；`FREE_SHARES=3124191.1152` 万股
     （≈98% 总股本）明显不是本次解禁量。
   - 688512：`ABLE_FREE_SHARES=12374.8664` 万股，`FREE_RATIO=0.378127`
     → 37.81%，与本次「追加承诺限售股份上市流通」一致。
   因此缓存里的 `shares_10k` 取 `ABLE_FREE_SHARES`；`FREE_SHARES` 仅作为
   `free_shares_10k` 留档，**不参与阈值**。

2. **质押表按个股累积多年历史，必须只取第 1 页。**
   600000 的 `RPT_CSDC_LIST` 返回 **633 行 / 127 页**（每周统计，约 12 年）。
   本工具只要最新一个统计日，故 `pageSize=1` + 允许 `pages>1`，按
   `TRADE_DATE desc` 取第 1 行；**不声称**拿到了全部历史。其余三个端点
   若 `pages>1` 一律抛错，不把截断结果当完整结果。

## 2. 缓存

路径：`source/stock_analysis/data/state/events/events.json`（`source/…/data/`
已 gitignore）。原子写入（`.tmp` + `os.replace`）。

```json
{
  "600000": {
    "lockup":        {"available": true, "stale": false, "as_of": "2026-11-14",
                      "fetched_at": "2026-11-14T18:35:00", "reason": null,
                      "error": null, "warnings": [], "items": []},
    "holder_trades": {"available": true, "stale": false, "...": "...", "items": []},
    "forecast":      {"available": true, "stale": false, "...": "...", "items": []},
    "pledge":        {"available": true, "stale": false, "...": "...", "items": [
      {"date": "2026-11-13", "ratio_pct": 0.1, "pledged_shares_10k": 3241.45,
       "pledged_mktcap_10k": 33711.08, "pledge_count": 4}
    ]}
  },
  "schema": 1, "source": "eastmoney_datacenter",
  "upstream": "a-stock-data v3.10.4 §3.6 / §14.1 / §14.3 / §14.5",
  "fetched_at": "2026-11-14T18:35:00", "stale": false
}
```

- 顶层只有 6 位数字键是标的；其余是保留元数据键。
- 每个子项独立成功/失败。失败时**保留上一轮同名子项**并标
  `stale=true` / `error=<原因>`；其余三个子项继续。没有旧数据则
  `available=false, stale=true`，CLI 报「事件数据过期（{D}）」。
- `as_of` 是**快照日**（预计算运行日；回退时是上一轮运行日）。事件自身
  日期在 `items[].date` / `items[].notice_date`。
- `warnings`（additive）：P2-1 丢弃的行、P3-4 未知 `PREDICT_TYPE` 等。
  空子项也带 `[]`，成功/失败/不可用路径键同构。

### 2.1 行级归属校验（P2-1）

`_datacenter()` 只负责发请求；**返回的每一行**都要在 `fetch_*` 里再核对一次
（上游 `_em_event_rows()` 的同口径防线）：

| 子项 | 代码字段 | 日期字段 | 允许窗口 |
|---|---|---|---|
| 解禁 | `SECURITY_CODE` | `FREE_DATE` | `as_of-365d ~ as_of+90d` |
| 增减持 | `SECURITY_CODE` | `NOTICE_DATE` | `as_of-60d ~ as_of` |
| 预告 | `SECURITY_CODE` | `NOTICE_DATE` | `as_of-60d ~ as_of` |
| 质押 | `SECURITY_CODE` | `TRADE_DATE` | `<= as_of`（最新一个统计日） |

不符合的行**丢弃**（不整批抛错，避免一行坏数据丢掉整页好数据），并按原因
累计成一条 warning 落盘：`丢弃 N 行（他票行 a、窗口外行 b、日期不可解析 c、
重复行 d）`。重复行按「解析后的完整事件对象」去重。CLI 会把这些 warning
转成 `diagnostics.warnings`，因此库外的行**不会**被当成该标的的事件。

**可选日期缺失不丢行**：`START_DATE`、`END_DATE`（增减持）与 `REPORT_DATE`
（预告）不是窗口判定所必需，缺失或不可解析时**保留该行**、字段置 `null`，
并单独计数成一条 warning
`可选日期缺失 N 处（START_DATE a、END_DATE b、REPORT_DATE c），已保留该行并置 null`。
实测东财增减持存在 `START_DATE=None` 的合法行（员工持股计划尚未开始），
严格解析会让整只票的「股东增减持」子项失败。窗口判定所必需的
`FREE_DATE` / `NOTICE_DATE` / `TRADE_DATE` 以及代码仍按上表校验，缺失时
丢弃该行并计入 warning。

> 选「丢弃 + 告警」而不是整批报错：东财偶尔回混合页，整批抛错会让一个子项
> 全天不可用；而把别的标的的行入库是**更坏**的错误。两者都不接受，但丢弃
> 能把影响限制在坏行本身，且 warning 让运维可见。

## 3. `evidence.events`（CLI additive）

成功、不可用、错误路径键完全一致：

```
available, stale, as_of, fetched_at, reason, code,
lockup, holder_trades, forecast, pledge
```

每个子项：

```
available, stale, as_of, fetched_at, reason, error, warnings[], items[]
```

`reason` 新增（additive）：`interface_suspected`（探针失败/0 行）、
`empty_pool_suspected`（全票池皆空且上一轮有数据）、`not_supported`（该市场
无此口径，例如北交所质押）。

- 基金：`available=false, reason=fund_not_supported`，不告警、不渲染。
- `--as-of` 回放：`reason=replay_not_supported`；事件只有当前快照，套到过去
  等于前视，故不报命中、不渲染，只给一条说明。
- 错误响应（取数阶段失败）：`available=false, stale=true,
  reason=data_source_error`，由 `with_events_block()` 补键同构（G35），
  **不改 `decision/schema.py`**。

## 4. 渲染规则与阈值

只报**距快照日 20 个自然日内**、达到阈值的重大事件；每类**至多一行**；
无事件则整类消失。阈值是模块常量，见
`advice_render.EVENT_THRESHOLDS`。

| 类型 | 窗口 | 阈值 | 理由 |
|---|---|---|---|
| 解禁 | 未来 0–20 日 | 占总股本 **≥1.0%** 或 **≥500 万股** | 1% 总股本在 20 日持有期里是显著稀释/抛压；500 万股≈中盘股数日成交，低于此通常被市场吸收 |
| 股东减持 | 过去 0–20 日公告，合计 | 占总股本 **≥0.5%** 或 **≥500 万股** | 0.5% 是「重要股东」级变动；按公告聚合，避免单笔小额刷屏 |
| 业绩预告 | 过去 0–20 日公告 | 类型 ∈ `首亏/续亏/预减/增亏/略减`，或净利同比上限 **≤ -20%** | 黑名单只报负面；-20% 以下才可能改变 20 日持有判断 |
| 股权质押 | 最新统计日 | 质押比例 **≥20%** | 20% 以上为大股东高质押，平仓/补仓风险进入可解释区间；中国结算周度，日期在行内显示 |

阈值边界一律用 `>=`（刚好达到即报）。阈值不参与 `action` / `confidence`。

### 4.1 过期判定：官方交易日历（P3-3）

日期过期判定只用 `astock.calendar_days_from_disk()`（深交所官方整月日历，
**只读、不触网**），**不是**「缓存文件是否当天生成」：

- 当前月日历缺失时继续向前翻月，取最近一个官方交易日；
- 连一份官方日历都没有 → 跳过日期过期判定（宁可晚报，不在长假首日误报）。
  显式的 `stale` / `error` / 探针结论仍照常生效。

实测场景：`now=2026-10-08 09:00`、缓存 `2026-09-30`，10 月日历缺失但 9 月
日历说明上一交易日是 9-30 → **不报过期**；`now=2026-10-09 09:00`、10 月
日历说明 10-08 已开市 → 9-30 的缓存**确实过期**。

> 残余风险：若本机连一份官方日历都没有（全新部署、从未跑过行情链路），
> 日期过期判定会被跳过，极旧的缓存可能不报「过期」。这是「宁可晚报、
> 不误报」的取舍；预计算/行情链路正常运行时磁盘日历总会有文件。

> 15:00–18:30 仍会判过期（最新交易日已是当日、而预计算 18:30 才跑）。这是
> 可辩护的保守：数据确实是上一交易日的，别把它当「新鲜」。

### 4.2 未知 `PREDICT_TYPE`（P3-4）

预告类型使用封闭词表：`首亏/续亏/预减/增亏/略减`（负面）+ `预增/略增/扭亏/
续盈/减亏/预盈/不确定`。不在表内的类型**不静默映射**：事件照常保留，同时写
warning `未知 PREDICT_TYPE：…（未按负面类型处理，请核对上游词表）`，随子项
落盘并进 CLI warnings。

渲染位置：可执行性闸门 + 重点监控之后、价格/建议行之前。

过期行：`⚠️ 事件数据过期（{D}）：{受影响的子项}`。`D` 取受影响子项中最早的
`as_of`；无日期写「未知」。**绝不**把过期写成「无事件」。

专用文案（互斥，不重复出条）：

| 情形 | 渲染 |
|---|---|
| 缓存从未生成（`reason=cache_missing`） | `⚠️ 事件数据未生成：…（预计算未运行，请先运行 tools/precompute_events.py）` —— **不写**「过期（未知）」 |
| 缓存存在但无该标的（`code_missing`） | `⚠️ 事件数据未生成：…（缓存存在但该标的未包含在内，请重跑…）` |
| 对照探针失败 / 0 行（`interface_suspected`） | `⚠️ 事件数据过期：{子项}（接口疑似异常，本轮空结果不采信）` |
| 全票池皆空且上一轮有数据（`empty_pool_suspected`） | 同上，并在 warnings 里点名「上一轮有数据」 |
| 该市场无此口径（`not_supported`，北交所质押） | `ℹ️ 股权质押：未覆盖（该市场无此统计口径）` —— **不得**渲染成「无事件」 |

## 5. 限流与串行耗时

- 所有东财请求走 `em_get()`（≥1s、会话复用）。
- 预计算侧再加一层「相邻请求（含重试）≥ `--interval`」节流，默认 1.0s，
  不得 <1.0。两层时间戳都取「上一次请求返回之后」，不会叠加成 2s。
- 默认每票 4 请求 + **每轮 4 个对照探针**：`14 只 × 4 + 4 = 60`。串行下限
  `(60-1)×1.0 = 59s`；2026-11-14 本机实测默认票池 60 请求
  `wall=64.83s`（`--retries 0`，56 个子项成功 / 0 失败）。探针与票池大小无关，
  4 次实测（本机，公开代码）见下。
- `python tools/precompute_events.py --estimate` 不触网，直接打印估算（已含
  4 个探针）。
- `python tools/precompute_events.py --probes-only` 只跑 4 个探针、不写缓存，
  用于体检接口与实测探针耗时。

2026-11-14 本机实测（`--probes-only --interval 1.0`，4 次串行公开请求）：

```
PROBE lockup: 1 行 / holder_trades: 1 行 / forecast: 1 行 / pledge: 1 行
probe_requests=4 suspected=- wall=3.86s
```

含网络时延后 4 个探针约 **+3.9s**，与任务里的 +4 请求 ≈ +4s 吻合。
- **不放在 09:15 盘前**：CLI 主路径只读缓存，零请求（测试用 socket +
  `_http_get` 双桩验证）。

## 6. 「无事件」vs「接口坏了」

| 情况 | 缓存 | CLI / 渲染 |
|---|---|---|
| 带个股条件的接口返回 0 行，且**对照探针正常** | `available=true, items=[]` | 该子项无行（确实没有） |
| 请求失败 / 非 2xx / 非 JSON / `code≠0` / 结构改变 | 有旧数据：保留 + `stale=true, error=…`；无旧数据：`available=false, stale=true` | `⚠️ 事件数据过期（{D}）`，不写「无事件」 |
| 对照探针抛错或 0 行 | 该子项对**所有代码**：有旧数据保留 + `stale=true, reason=interface_suspected`；无旧数据 `available=false` | `⚠️ 事件数据过期：{子项}（接口疑似异常，本轮空结果不采信）` |
| 全票池该子项皆空，且上一轮有数据 | `stale=true, reason=empty_pool_suspected` + warnings | 同上，warnings 点名「上一轮有数据」 |

### 6.1 对照探针设计（P2-2，每轮 4 请求）

| 报表 | 探针 filter | 为什么「恒应非空」 |
|---|---|---|
| `RPT_CSDC_LIST` | **无条件**，`TRADE_DATE desc`, `pageSize=1` | 中国结算周度全市场统计恒有最新一行 |
| `RPT_LIFT_STAGE` | `(FREE_DATE>='{as_of}')`, `pageSize=1` | 全市场未来解禁恒有排期 |
| `RPT_PUBLIC_OP_NEWPREDICT` | `(NOTICE_DATE>='{as_of-30d}')`, `pageSize=1` | 全市场 30 天窗口恒有公告 |
| `RPT_SHARE_HOLDER_INCREASE` | `(NOTICE_DATE>='{as_of-30d}')`, `pageSize=1` | 全市场 30 天窗口恒有公告 |

- 探针在逐票取数**之前**串行跑一次；失败/0 行的子项跳过本轮逐票请求
  （接口已疑似异常，省请求），并对所有代码标 `interface_suspected`。
- 探针只关心「有没有至少 1 行」，所以允许 `pages>1`（`pageSize=1` 时
  必然 `pages>1`），不把截断当完整结果。
- 零成本兜底：某子项全票池皆空且**上一轮同票池有数据** → 记 warning 并标
  `empty_pool_suspected`，不得静默当作「无事件」。该兜底不需要额外请求，
  只覆盖「曾经有过数据」的场景，因此只作补充、不作主判据。

**残余风险**：若服务端静默降级到「连全市场无过滤探针也返回 0 行」，探针
能抓住；若服务端返回**别的市场的行**，P2-1 的代码/日期归属校验会丢弃并告警。
两者叠加后，本层不再有已知的「空结果冒充无事件」路径。

**不得声称**：接入后建议更准/胜率更高。本层只做风险事实展示。

## 7. 调度接线（cron）

预计算必须在**收盘后、下一交易日盘前**完成，且每天只跑一次。推荐时段
**18:30–18:50（Asia/Shanghai）**：此时行情已定、东财数据中心当日数据已落，
又远离 09:15 盘前窗口。

cron 命令（路径按部署机调整；实际接线由运维/Hermes 安排，本仓库不改
`~/.hermes/`）：

```cron
# 周一至周五 18:35 跑事件风险预计算；串行 ~60s（14 只 / 60 请求）+ 重试余量
35 18 * * 1-5 cd /path/to/stock_trading_helper && mkdir -p logs && /path/to/stock_trading_helper/source/.venv/bin/python tools/precompute_events.py >> logs/precompute_events.log 2>&1
```

- crontab 每条命令必须写成**单行**：`\` 续行不被支持（`/bin/sh` 会把反斜杠
  和换行原样传给 shell，行为不可预期）。
- 首次部署先建日志目录：`mkdir -p logs`（已并入上面的 cron 命令，重复执行
  无副作用）。
- 解释器用仓库 venv 的**绝对路径**（`source/.venv/bin/python`），不用
  `/usr/bin/python3`：系统 Python 缺 `requests` 等依赖，任务会静默失败。
  路径按部署机调整。

- 遇法定节假日可照跑（东财返回上一交易日数据），但**不要**在 09:00–15:00
  之间跑：既挤盘前，又拿到未收盘数据。
- 退出码：`0` 全部子项成功；`1` 有子项失败 / 探针疑似异常（日志里
  `FAIL` / `WARN` 行 + `--json` 可用于告警）；`2` 参数错误。
- 健康检查：`python tools/precompute_events.py --probes-only`（4 请求，
  ~4s，不写缓存）。
- 未配置定时任务时，CLI 会显示「事件数据未生成（预计算未运行）」，这是
  预期行为，不是接口故障。

## 8. 隐私

- 文档、提交、测试只出现公开代码（600000/002475/300750/688512 等）或
  虚构代码/名称/金额。
- 不出现任何真实持仓的代码、名称、金额、股数、权重。
- 缓存文件在 `source/…/data/`（gitignore），不进入提交。

## 9. 测试与复验

```
cd source
/home/samc/.venv/bin/python -m pytest tests/test_events_context.py -q
# 69 passed

/home/samc/.venv/bin/python -m pytest tests/ -q
# 930 passed, 1 failed
# 唯一失败 test_probe_requirements.py::test_current_environment_passes_via_probe
# 是基线就存在的解释器版本不符（缺口盘点 U7），与本改动无关。
```

新增覆盖（P2/P3）：

- P2-1：他票行 / 窗口外行 / 重复行（解禁、增减持、预告、质押）逐类丢弃
  + warning；端到端断言坏行不入缓存且 CLI notes 可见。
- P2（本轮新增）：`START_DATE=None`、`END_DATE=None`、两者均 `None` 三种行
  → 子项整体不失败、行保留且字段为 `null`、其他行不受影响；关键日期
  （`NOTICE_DATE=None`）仍只丢该行；预告 `REPORT_DATE=None` 同规则。
- P2-2：探针 0 行 / 探针抛错 → `interface_suspected`；探针正常 + 票池空 →
  `available=true, items=[]`（真空）；上一轮有数据 → `empty_pool_suspected`。
- P3：北交所 `not_supported` 渲染「未覆盖」；缓存未生成渲染「未生成」；
  长假首日不误报过期（官方日历）；未知 `PREDICT_TYPE` 告警。

真实端点（公开代码，非持仓）：

```
python tools/precompute_events.py --probes-only --interval 1.0
# probe_requests=4 suspected=- wall=3.86s

# 2026-11-14 复验：300476 的增减持存在 START_DATE=None 的员工持股计划行
# 修复前：FAILED EventsFetchError: START_DATE 不是 YYYY-MM-DD：None（整子项失败）
# 修复后：OK 1 条，start_date=null，warnings=可选日期缺失 1 处
python tools/precompute_events.py 300476 \
  --out-dir /tmp/events_300476 --interval 1.0 --retries 0
# requests=8 (probe=4) ok_sections=4 failed_sections=0 wall=8.82s

python tools/precompute_events.py --out-dir /tmp/events_full \
  --interval 1.0 --retries 0
# requests=60 (probe=4) ok_sections=56 failed_sections=0 wall=64.83s

python tools/precompute_events.py 600000 002475 300750 \
  --out-dir /tmp/events_verify2 --interval 1.0 --retries 0
# requests=16 (probe=4) ok_sections=12 failed_sections=0 wall=17.39s

python tools/precompute_events.py 688512 \
  --out-dir /tmp/events_verify3 --interval 1.0 --retries 0
# requests=8 (probe=4) ok_sections=4 failed_sections=0 wall=8.57s
# 渲染：⚠️ 解禁：2026-11-16 解禁 12374.87 万股（占总股本 37.81%）
```