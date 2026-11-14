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
坏了显示「事件数据过期（{D}）」，绝不显示成「没有事件」。**

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
                      "error": null, "items": []},
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

## 3. `evidence.events`（CLI additive）

成功、不可用、错误路径键完全一致：

```
available, stale, as_of, fetched_at, reason, code,
lockup, holder_trades, forecast, pledge
```

每个子项：

```
available, stale, as_of, fetched_at, reason, error, items[]
```

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

渲染位置：可执行性闸门 + 重点监控之后、价格/建议行之前。

过期行：`⚠️ 事件数据过期（{D}）：{受影响的子项}`。`D` 取受影响子项中最早的
`as_of`；无日期写「未知」。**绝不**把过期写成「无事件」。

## 5. 限流与串行耗时

- 所有东财请求走 `em_get()`（≥1s、会话复用）。
- 预计算侧再加一层「相邻请求（含重试）≥ `--interval`」节流，默认 1.0s，
  不得 <1.0。两层时间戳都取「上一次请求返回之后」，不会叠加成 2s。
- 默认每票 4 请求：`10 只 × 4 = 40`。串行下限 `(40-1)×1.0 = 39s`；含网络
  时延接近 40s。实测 12 请求 / 12.23s（≈1.02s/请求）。
- `python tools/precompute_events.py --estimate` 不触网，直接打印估算。
- **不放在 09:15 盘前**：CLI 主路径只读缓存，零请求（测试用 socket +
  `_http_get` 双桩验证）。

## 6. 「无事件」vs「接口坏了」

| 情况 | 缓存 | CLI / 渲染 |
|---|---|---|
| 带个股条件的接口返回 0 行 | `available=true, items=[]` | 该子项无行（确实没有） |
| 请求失败 / 非 2xx / 非 JSON / `code≠0` / 结构改变 | 有旧数据：保留 + `stale=true, error=…`；无旧数据：`available=false, stale=true` | `⚠️ 事件数据过期（{D}）`，不写「无事件」 |
| 全市场无过滤查询 0 行 | 上游视为接口坏了 | 本工具不发全市场查询；带个股条件的 0 行是合法空结果。**残余风险**：若接口对所有个股都静默返回 0 行、且 code=0，本工具无法区分「全市场真的无事件」与「接口静默降级」。错误码/结构异常仍会被抓住。 |

**不得声称**：接入后建议更准/胜率更高。本层只做风险事实展示。

## 7. 隐私

- 文档、提交、测试只出现公开代码（600000/002475/300750/688512 等）或
  虚构代码/名称/金额。
- 不出现任何真实持仓的代码、名称、金额、股数、权重。
- 缓存文件在 `source/…/data/`（gitignore），不进入提交。

## 8. 测试与复验

```
cd source
/home/samc/.venv/bin/python -m pytest tests/test_events_context.py -q
# 47 passed

/home/samc/.venv/bin/python -m pytest tests/ -q
# 906 passed, 1 failed
# 唯一失败 test_probe_requirements.py::test_current_environment_passes_via_probe
# 是基线就存在的解释器版本不符（缺口盘点 U7），与本改动无关。
```

真实端点（公开代码，非持仓）：

```
python tools/precompute_events.py 600000 002475 300750 \
  --out-dir /tmp/events_verify2 --interval 1.0 --retries 0
# requests=12 ok_sections=12 failed_sections=0 wall=12.23s

python tools/precompute_events.py 688512 \
  --out-dir /tmp/events_verify3 --interval 1.0 --retries 0
# requests=4 ok_sections=4 failed_sections=0 wall=3.67s
# 渲染：⚠️ 解禁：2026-11-16 解禁 12374.87 万股（占总股本 37.81%）
```