# ST 名单收盘后预计算（PR #107 / P1）

> 实现：`tools/precompute_st_list.py`（收盘后取数）、
> `source/stock_analysis/data_loader/status.py`（`get_st_list_cached` / 冷却 /
> 444·FIN 探测）、`source/cli.py`（盘前只读）。

## 0. 一句话

**只在收盘后（18:30–18:40）联网取一次全市场 ST 名单，盘前只读本地缓存；
盘前对 push2/push2delay 的请求数为 0。** 缓存覆盖最近交易日 → 名单权威；
缓存过期 → 仍用但 `stale=true` + 日期 warning；无缓存 → `unavailable`（由
腾讯报价名称兜底）。坏结果（空表 / 重复代码 / 网络失败）绝不覆盖好缓存。

## 1. 为什么必须预计算（2026-11-15 A5500 实测）

* 东财 `push2` / `push2delay` 对本机出口 IP 触发 nginx **444** 反爬
  （3 分钟内约 10 次请求即触发）。
* ST 名单走 clist 全市场翻页；盘前对每只票各跑一次 CLI，等于每天把全市场
  名单反复拉 N 次 —— 这是封禁的直接原因。
* 旧缓存 `data/status/st_list.json` **从未落盘**，盘前只能按名称猜 ST。

## 2. 数据流（2026-11-15 起多源）

```
18:30–18:40  cron
  └─ tools/precompute_st_list.py
       ├─ 东财冷却中? ──是──> 跳过东财（0 请求），继续官方备胎
       ├─ StListFetchGuard(em_get) ── 计数 + 识别 444 / 零字节 FIN
       ├─ fetch_st_list()  # 三态：#106 空表/重复重试一次；网络失败不重试
       │                   # #109：重复重试会放开上游重复检查并真正清洗同内容重复行
       ├─ 串行取交易所官方备胎 data_sources/st_exchange.py
       │    ├─ 上交所 commonQuery（1 请求，COMPANY_STATUS=7）
       │    └─ 深交所 ShowReport/data（CATALOGID=fxjsb，按 pagecount 串行翻页）
       ├─ 合并：东财为基准 + 官方取并集；冲突取「是 ST」并告警
       ├─ 东财命中 444/FIN ──> 写 eastmoney_cooldown.json（仍可用官方落盘）
       ├─ 至少一源完整 ─────> 原子写 st_list.json，退出码 0
       └─ 所有可用源都失效 ─> 只告警，不覆盖旧缓存，退出码 4

09:15 盘前 cli.py
  └─ status.get_st_list_cached()  # 只读磁盘，绝不触网
       ├─ 覆盖最近交易日 ─> is_st_source=exchange_list
       ├─ 过期（含日期） ─> 仍可用，stale=true + warning
       └─ 无缓存/校验失败 ─> unavailable + warning（报价名称兜底）
```

### 2.1 两个官方源的实测取证（2026-11-16 复核；首测 2026-11-15）

| 交易所 | 端点 | 实测 | 字段 / 条数 |
|---|---|---|---|
| 上交所 | `https://query.sse.com.cn/sseQuery/commonQuery.do`（`sqlId=COMMON_SSE_CP_GPJCTPZ_GPLB_GP_L`，`STOCK_TYPE=1,8`，`COMPANY_STATUS=7`） | HTTP 200，JSON | `result[].A_STOCK_CODE` / `SEC_NAME_CN` / `STATE_CODE`；`STATE_CODE=7` 共 **80** 条（含 **12** 只 688 科创板）；`pageHelp`：`total=80`、`pageSize=2000`、`pageCount=1` |
| 深交所 | `https://www.szse.cn/api/report/ShowReport/data`（`SHOWTYPE=JSON&CATALOGID=fxjsb&TABKEY=tab1`） | HTTP 200，JSON | 响应含**两个块**：`tab1`=风险警示股票、`tab2`=退市整理股票；`tab1.metadata`：`pagesize=20`（`PAGESIZE` 被忽略）、`pagecount=6`、`recordcount=115`；行字段 `zqdm` / `gsjc`（值含 HTML）。115 条中含 **2 只 B 股**（`200056 *ST皇庭B`、`200488 ST晨鸣B`） |

两源当次合计 **195** 条（沪 80 + 深 115，无重叠），请求量：上交所 1 次 +
深交所 6 次（串行翻页），官方合计 7 次/轮；东财仍按原有 clist 翻页。
上交所页面（`/disclosure/listedinfo/riskplate/`）与深交所页面
（`/disclosure/listed/warn/index.html`）本身不返回名单，数据由上述接口提供。

**SSE 完整性**：过滤后 80 条 < 单页上限 2000，`pageCount=1`，单页即全量，
**不存在截断**。首测注释里的「78 条 / 10 只 688」是旧快照误数，已按实测更正。
为防止将来风险警示数超过单页上限，`fetch_sse_st_list()` 额外校验
`total<=pageSize` 与 `pageCount==1`，任一不满足即显式失败（不静默截断）。

不完整/结构变化一律 **显式失败**：上交所要求 `len(result)==pageHelp.total`、
`total<=pageSize`、`pageCount==1` 且每行 `STATE_CODE=7`（否则会拿到
2000/2319 的截断全表）；深交所**每一页**都要求按 `metadata.tabkey=='tab1'`
命中风险警示块（`tab2` 是退市整理股票，**不匹配即失败**，没有「单块就吃
`payload[0]`」的回退），并满足 `sum(rows)==recordcount`（翻页期间
`pagecount/recordcount` 变化也失败）。空表**绝不**当成「今日没有 ST」。

## 3. cron 接线（实际接线由协调者安排）

```cron
# 工作日 18:35（收盘后、东财数据落定后）
35 18 * * 1-5  cd $REPO && /usr/bin/env python3 tools/precompute_st_list.py \
    >> $HOME/logs/st_list_precompute.log 2>&1
```

时段依据：15:00 收盘；18:30–18:40 为既有事件风险预计算的同一窗口，避开盘中
与盘前。一次运行只发 clist 翻页所需请求（沪深 + 北交所，通常各 1–N 页），
不是逐票请求。

## 4. 缓存结构（schema 2）

路径：`source/stock_analysis/data/state/st_list.json`（`source/…/data/` 已
gitignore）。原子写入（`.tmp` + `os.replace`）。

```json
{
  "schema": 2,
  "source": "eastmoney",
  "coverage": "沪深京",
  "fetched_at": "2026-11-13T18:35:00",
  "session_date": "2026-11-13",
  "count": 2,
  "sha256": "<codes 排序后以 \n 连接的 SHA-256>",
  "codes": ["000002", "600001"],
  "stocks": {"600001": {"name": "*ST示例", "st_type": "*ST", "market": "sh"}},
  "state": "ok",
  "merged_from": ["eastmoney", "sse", "szse"]
}
```

* `codes[]` 是**权威名单**；`stocks` 只留档名称/价格（#106 冲突保守判定复用）。
* `merged_from`（2026-11-15 additive）：本轮实际合并的源。`source` 记录**主源**
  （`eastmoney` / `exchange`），`merged_from` 记录明细（`eastmoney` / `sse` /
  `szse`）。官方备胎单独落盘时 `source="exchange"`、`coverage` 明确写
  「沪深A+B（官方，不含北交所）」（深市风险警示板含 B 股，见 §2.1）。
* `sha256` 只对代码集合取哈希；读盘方校验失败时**拒绝使用该缓存**
  （ST 假阴性会让人误买风险警示股），返回 `state='corrupt'` + warning。

## 5. 三态与「不覆盖好缓存」

沿用 #107：

| 取数结果 | 预计算行为 | 盘前读取 |
|---|---|---|
| 东财 `ok` 且名单非空 | 以东财为基准，合并官方并集后原子覆盖，退出码 0 | `exchange_list`（`merged_from` 可审计） |
| 东财失败 + 两所官方都 `ok` | 用官方名单落盘（`source=exchange`），退出码 0；warning 写明不含北交所（深市含 B 股） | `exchange_list` |
| 东财失败 + 官方仅一所成功 | 官方名单**不完整**，只告警，**不写**，退出码 4 | 旧缓存（可能 `stale=true`）或 `unavailable` |
| `empty`（0 条，可疑） | 重试 1 次；仍空 → 只告警，**不写** | 旧缓存（可能 `stale=true`） |
| `duplicate`（同码冲突） | 重试 1 次；仍冲突 → 只告警，**不写** | 旧缓存（可能 `stale=true`） |
| `failed`（网络/结构异常） | 不重试，只告警，**不写**（官方若完整仍可落盘） | 旧缓存（`stale=true`）或 `unavailable` |

**两源冲突**：东财与官方在沪深重叠范围内一边有一边无、或同码名称不一致时，
**取并集（任一源说是 ST 就是 ST）**并把冲突写进 `stats.conflicts` / 缓存
warning / stderr（`CONFLICT ...`）。北交所不在官方覆盖内，**不算冲突**。

`stats.conflicts`（issue #123 起）是**结构化条目列表**，不是整段 warning：

```json
[
  {"kind": "east_only", "count": 1, "codes": ["600003"],
   "message": "东财独有 1（样例 ['600003']）"},
  {"kind": "name_mismatch", "count": 1, "codes": ["600001"],
   "message": "名称不一致 1（样例 ['600001']）"}
]
```

`kind` 取值：`east_only`（东财有、官方覆盖范围内没有）/ `official_only`
（官方有、东财没有）/ `name_mismatch`（同码名称不一致）。没有冲突时为
`[]`（旧版为 `null`）。给人看的整段文案仍在 `stats.conflict_summary`、
`stats.reason` 与缓存 warning 中（additive，字段语义与命名一致）。

**空表绝不等同于「今日没有 ST」。**

## 6. 反爬熔断（444 / 零字节 FIN）

`StListFetchGuard` 包一层 `_astock_generated.em_get`，在 HTTP 入口记录状态码
与异常原文 —— 因为上游 `_em_clist_all` 会把 444 吞成异常类型后换域重试，
上层拿不到状态码。

* 命中 HTTP 444 或零字节 FIN → 抛 `StListBlocked`（刻意不继承
  `requests.RequestException`）：**不换域、不重试，一次即止**；
* 写 `source/stock_analysis/data/state/st_list_cooldown.json`
  （`reason` / `detail` / `blocked_at` / `blocked_until`，默认 1800s，
  可用 `ST_LIST_COOLDOWN_SECONDS` 覆盖）；
* 冷却期内**不再请求东财**（东财 0 请求）；2026-11-15 起**仍会尝试交易所
  官方备胎**（不同主机，不受东财冷却影响）。若官方两所都成功，本轮照常
  落盘、退出码 0；若 `--no-official` 且冷却中，才是 0 请求跳过（退出码 3）；
* 人工确认封锁解除后可 `--force` 或 `--clear-cooldown`。只有**东财成功**
  才清冷却文件；官方备胎成功不清（东财可能仍在封禁）。

## 7. 命令与退出码

```bash
python3 tools/precompute_st_list.py                 # 正常预计算
python3 tools/precompute_st_list.py --json          # JSON 统计
python3 tools/precompute_st_list.py --dry-run       # 只说明计划，不触网
python3 tools/precompute_st_list.py --state-dir /tmp/st-state  # 临时目录
python3 tools/precompute_st_list.py --force         # 忽略东财冷却
python3 tools/precompute_st_list.py --clear-cooldown
python3 tools/precompute_st_list.py --no-official   # 只用东财（维护/对照）
```

| 退出码 | 含义 |
|---|---|
| 0 | 缓存已更新（东财或官方两所至少一源完整） |
| 2 | 参数错误 |
| 3 | 东财冷却中且官方备胎被 `--no-official` 禁用（本轮 0 请求） |
| 4 | 所有可用源都失效 / 坏结果 / 写盘失败（缓存未被覆盖） |

## 8. 验证口径

* `source/tests/test_st_precompute.py`：
  - 缓存字段结构、`sha256`、`session_date`、`source` / `merged_from`；
  - 空表 / 冲突重复 / 网络失败**不覆盖**已有缓存；
  - 444 与零字节 FIN 后请求数恰为 1，冷却期内东财 0 请求；
  - 官方备胎：东财 444 时两所官方落盘；official 仅一所成功/空表时**不覆盖**；
    东财 + 官方并集与冲突告警（取「是 ST」）；
  - 盘前 `cli.advise_internal` 的 `is_st_source=exchange_list` 且
    `EM_SESSION.get` / `st_stock_list` / `fetch_st_list` /
    `st_exchange.fetch_official_st_lists` 调用数均为 0。
* `source/tests/test_st_exchange.py`（23 项，全离线）：
  - 上交所正常 1 请求；截断（2000/2319）、`total>pageSize` / `pageCount>1`
    （需分页）、过滤未生效（混合 `STATE_CODE`）、空表、HTTP 非 2xx、非 JSON
    都显式失败；
  - 深交所按 `pagecount` 串行翻页、HTML 名称清洗、`recordcount` 不符 /
    翻页期间变化 / 缺 metadata / 空表都显式失败；**每页 tabkey 必须是 tab1**
    （只有 tab2 的单块响应、缺 tabkey、翻页途中出现 tab2 一律失败；tab1+tab2
    同时返回时只吃 tab1）；
  - 两所串行汇总（一所失败不吞另一所）、合并并集、同码名称冲突取 `*ST`、
    结构化冲突条目（`merge_lists_detailed`）。
* `source/tests/test_st_precompute.py`：`stats.conflicts` 是结构化条目列表
  （`kind` / `count` / `codes` / `message`），无冲突时为 `[]`；整段文案在
  `stats.conflict_summary`。

## 9. 遗留风险

* 预计算依赖 cron 实际接线；若 18:30 任务未运行，盘前会持续用旧缓存并
  `stale=true`（不会误报成「非 ST」）。
* 444/FIN 判定依赖异常文案/状态码特征；若东财改变拦截形态（如 403 + 普通
  HTML），预计算仍会按 `failed` 处理并保留旧缓存，但不会自动写冷却。
* 缓存只覆盖名单代码；名称/价格以预计算时刻为准（盘前报价名称仍可用于冲突
  保守判定）。
* **官方备胎的覆盖缺口**：上交所只取 A 股（主板 + 科创板）；深交所取风险
  警示板全部股票，**含 B 股**（2026-11-16 实测 115 条中有 `200056`、
  `200488` 两只 B 股）；**不含北交所**。B 股保留在名单里的影响：这些代码
  的 `is_st` 会为 `True`，但本项目只交易 A 股，B 股不会进入候选池；若将来
  纳入 B 股，风险警示语义对 B 股同样成立，判定依然正确。东财长期封禁期间，
  北交所标的的 ST 判定会退化为报价名称（`is_st_source=quote_name`），
  warning 已写明。
* **官方端点可能变**：上交所 `COMPANY_STATUS=7` 是状态过滤参数（2026-11-16
  复核返回 80 条风险警示、全部 `STATE_CODE=7`，`pageCount=1` 未截断）；
  深交所 `CATALOGID=fxjsb` 来自风险警示板页面本身，响应含 `tab1`（风险
  警示股票）与 `tab2`（退市整理股票）两块。任一源结构变化都会**显式失败**
  （不静默返回空、也不把 tab2 当 tab1），此时只剩东财/旧缓存兜底；需要人工
  复核端点后更新模块。
* **请求量**：官方每轮约 7 个 GET（沪 1 + 深 6，深交所固定 20/页，`PAGESIZE`
  被忽略）；东财仍按 clist 翻页。全部串行、不重试。