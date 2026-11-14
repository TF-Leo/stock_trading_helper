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

## 2. 数据流

```
18:30–18:40  cron
  └─ tools/precompute_st_list.py
       ├─ 冷却中? ──是──> 退出码 3（0 请求）
       ├─ StListFetchGuard(em_get) ── 计数 + 识别 444 / 零字节 FIN
       ├─ fetch_st_list()  # 三态：#106 空表/重复重试一次；网络失败不重试
       │                   # #109：重复重试会放开上游重复检查并真正清洗同内容重复行
       ├─ 命中 444/FIN ──> 写 st_list_cooldown.json，退出码 4（不写缓存）
       ├─ state=ok ─────> 原子写 st_list.json，退出码 0
       └─ 空表/重复/失败 ─> 只告警，不覆盖旧缓存，退出码 4

09:15 盘前 cli.py
  └─ status.get_st_list_cached()  # 只读磁盘，绝不触网
       ├─ 覆盖最近交易日 ─> is_st_source=exchange_list
       ├─ 过期（含日期） ─> 仍可用，stale=true + warning
       └─ 无缓存/校验失败 ─> unavailable + warning（报价名称兜底）
```

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
  "state": "ok"
}
```

* `codes[]` 是**权威名单**；`stocks` 只留档名称/价格（#106 冲突保守判定复用）。
* `sha256` 只对代码集合取哈希；读盘方校验失败时**拒绝使用该缓存**
  （ST 假阴性会让人误买风险警示股），返回 `state='corrupt'` + warning。

## 5. 三态与「不覆盖好缓存」

沿用 #107：

| 取数结果 | 预计算行为 | 盘前读取 |
|---|---|---|
| `ok` 且名单非空 | 原子覆盖缓存，退出码 0 | `exchange_list` |
| `empty`（0 条，可疑） | 重试 1 次；仍空 → 只告警，**不写** | 旧缓存（可能 `stale=true`） |
| `duplicate`（同码冲突） | 重试 1 次；仍冲突 → 只告警，**不写** | 旧缓存（可能 `stale=true`） |
| `failed`（网络/结构异常） | 不重试，只告警，**不写** | 旧缓存（`stale=true`）或 `unavailable` |

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
* 冷却期内预计算直接退出码 3，**0 请求**；`get_st_list_cached` 的警告里也会
  带上冷却原因；
* 人工确认封锁解除后可 `--force` 或 `--clear-cooldown`。

## 7. 命令与退出码

```bash
python3 tools/precompute_st_list.py                 # 正常预计算
python3 tools/precompute_st_list.py --json          # JSON 统计
python3 tools/precompute_st_list.py --dry-run       # 只说明计划，不触网
python3 tools/precompute_st_list.py --state-dir /tmp/st-state  # 临时目录
python3 tools/precompute_st_list.py --force         # 忽略冷却
python3 tools/precompute_st_list.py --clear-cooldown
```

| 退出码 | 含义 |
|---|---|
| 0 | 缓存已更新 |
| 2 | 参数错误 |
| 3 | 冷却中跳过（未发请求） |
| 4 | 取数失败 / 坏结果 / 写盘失败（缓存未被覆盖） |

## 8. 验证口径

* `source/tests/test_st_precompute.py`：
  - 缓存字段结构、`sha256`、`session_date`；
  - 空表 / 冲突重复 / 网络失败**不覆盖**已有缓存；
  - 444 与零字节 FIN 后请求数恰为 1，冷却期内 0 请求；
  - 盘前 `cli.advise_internal` 的 `is_st_source=exchange_list` 且
    `EM_SESSION.get` / `st_stock_list` / `fetch_st_list` 调用数均为 0。

## 9. 遗留风险

* 预计算依赖 cron 实际接线；若 18:30 任务未运行，盘前会持续用旧缓存并
  `stale=true`（不会误报成「非 ST」）。
* 444/FIN 判定依赖异常文案/状态码特征；若东财改变拦截形态（如 403 + 普通
  HTML），预计算仍会按 `failed` 处理并保留旧缓存，但不会自动写冷却。
* 缓存只覆盖名单代码；名称/价格以预计算时刻为准（盘前报价名称仍可用于冲突
  保守判定）。