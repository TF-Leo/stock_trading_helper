# P1 证据流水线设计与数据可用性探针（R2 / issue #21）

> 状态：**研究 / 设计文档，不含生产代码改动**。本文回答四件事：P1 候选所需数据在
> A5500 上到底能不能取到、每类数据怎么变成可复现证据、按什么顺序落地、哪些不做。
> 本文不替用户决定是否新增依赖，也不改 `action` 语义；所有行为变更都必须另开 PR。
>
> 上游固定版：a-stock-data **v3.10.4**，commit
> `f21534946c42be3ad048ac938eecb1bad84cb8ab`（`vendor/a-stock-data/VERSION`）。
> 本文所有上游 § 编号均以该 commit 为准；升级后必须重新对照。
>
> 本项目基线：`b82d2cb`（`TF-Leo/p1-evidence-plan`，2026-11-12）。
>
> 探针环境：Linux `samc-Precision-5820-Tower`（2× NVIDIA RTX **A5500**），
> Python **3.14.4**，2026-11-12 22:07–22:13（UTC+8）。探针只读，临时文件在
> `/tmp/p1probe_*`；**没有**写入项目 venv / `requirements.txt` / `source/`。

---

## 0. 结论先行

1. **P1 的第一项建议做「指数 regime，只展示不改 action」**（P1-3）。理由：不需要新依赖，
   腾讯指数行情、沪深 300 成分、中证指数估值全部实测可用；当前 `market_regime` 用的是
   **个股自身** MA20/MA60（`source/stock_analysis/advisor/analyzer.py:74-104`），而
   `get_market_index_daily()` 仍是空实现（`source/stock_analysis/data_loader/meta.py:820-822`），
   这是数据与语义都最清楚的一刀。
2. **baostock 在 Python 3.14.4 + A5500 上可用**：临时 venv 安装 `baostock==0.9.4` 成功
   （纯 Python，`py3-none-any`），登录 + 取 600519 一行耗时 **0.58 s**，返回
   `peTTM/pbMRQ/psTTM/pcfNcfTTM/turn/tradestatus/isST` 与上市日/退市日。
   它是 P1-1 历史估值、P1-2 真筹码、P1-6 历史 ST/停牌/退市的**唯一零鉴权来源**；
   但建议**只作为研究/预计算的可选依赖**引入，不进主 CLI 路径（见 §5）。
3. **东财数据本次全部可达**：分钟/日级资金流、两融、股东户数、业绩预告、增减持、
   回购、质押均返回 200 + 有效数据。但 push2/push2his 有 IP 级封禁先例
   （上游 §331-398），批量研究必须串行 ≥1 s；datacenter 与 push2 是不同 WAF，
   不能因一个端点失败判定「东财全灭」。
4. **腾讯 K 线第一个入口在本机被 WAF 返回 501**：`web.ifzq.gtimg.cn` 返回
   `waf.tencent.com/501page.html`，而 `proxy.finance.qq.com` 与 `ifzq.gtimg.cn`
   正常。现有适配器按三入口轮换（`source/stock_analysis/data_sources/astock.py:100-178`），
   因此主流程仍可用；但**不能把入口顺序当成保证**，必须保留冷却与换源。
5. **筹码分布没有公开端点**：`push2` / `push2his` 的 `cyq/get` 均实测 **HTTP 404**，
   只能按上游 §4.6 用「OHLC + 换手率」本地推演。换手率来自 baostock §6.5；
   若不引入 baostock，只能用「成交量 ÷ 流通股本」近似，精度与历史股本变化都不可控。
6. **本 PR 只新增本文件**。P1 的所有数据接入先做「只展示/不决策」，
   任何改 `action`、regime 标签、`untradeable`、回测成交口径的提案都必须单独 PR + 用户批准。

---

## 1. 探针方法与口径

### 1.1 环境与约束

| 项 | 值 | 说明 |
|---|---|---|
| 机器 | `samc-Precision-5820-Tower` | 2× NVIDIA RTX A5500；本文简称「A5500 本机」 |
| OS | Linux 7.0.0-38-generic x86_64 | Ubuntu 系 |
| Python | **3.14.4** | 系统 `/usr/bin/python3.14` |
| 项目基线 | `b82d2cb` | 全新 Orca worktree |
| 上游 | v3.10.4 / `f215349` | `vendor/a-stock-data/SKILL.md`，sha256 `09f7e8a3…` |
| 项目 venv | **未改动** | 探针依赖只装在 `/tmp/p1probe_bs` 临时 venv |
| 东财节流 | 串行，相邻请求 ≥1.1 s | 符合上游 `em_get()` 的防封铁律（§383-397） |

> 延迟是**单次取数的墙钟**，不是并发/压力测试；不能当成生产 SLA。
> 所有命令的 `EXIT` 是 shell/curl 退出码；HTTP 状态另列。`curl` 对 4xx 默认仍返回 0，
> 所以本文同时记录 `HTTP=`，不把「curl 退出 0」当成「数据可用」。

### 1.2 为什么探针不能只用「一次 200」

每类数据至少核对三件事：

1. **结构**：字段名/行数/单位是否符合上游文档；结构变了要报错，不能静默空表。
2. **语义**：日期口径、复权口径、公告日 vs 报告期、快照 vs 历史。
3. **失败模式**：限流、WAF、空数据、字段改名时，调用方能不能区分「没有」与「取不到」。

---

## 2. 数据可用性表

### 2.1 总表

> 「实测」列的数字来自 §2.2 的原始命令；「未验证」= 本次没有证据，不猜。
> 「延迟」= 单次调用耗时（秒，本机 2026-11-12）。

| # | 数据类 | 上游 § | 实测结果 | 延迟 | 新增依赖 | 失败模式 / 限制 |
|---|---|---|---|---|---|---|
| A1 | 腾讯个股快照（PE/PB/市值/涨跌停） | §1.1 | **可用**：600519 PE(TTM)=21.46、PB=6.50、总市值=17629.53 亿、涨停=1542.53、跌停=1262.07；3 次连续调用字段一致 | 0.22 | 无 | 停牌/废码会返回 200 + 定格报价，必须看 `is_stale`；指数 PB=0、涨跌停=-1 |
| A2 | 腾讯指数快照 | §1.1 | **可用但字段不全**：沪深 300 价格=4043.93、PE(TTM)=13.41、总市值=508626.81 亿；**PB=0、涨跌停=-1、PE(静) 为空** | 0.22 | 无 | 不能把指数 PB 当有效值；指数估值要用 §12.3 |
| A3 | 腾讯 K 线（个股/指数） | §1.2 | **可用（需换源）**：`proxy.finance.qq.com` / `ifzq.gtimg.cn` 正常；`web.ifzq.gtimg.cn` 返回 **HTTP 501 WAF 页** | 0.27 | 无 | 单入口约 600 次后空 JSON；三入口轮换 + 120 s 冷却；北交所不支持 |
| A4 | 新浪复权因子（个股/基金） | §1.6 | **可用**：600519 qfq 33 行；510300 hfq 15 行且含 `s/u`；161725 hfq 4 行 | 0.58 | 无 | 空 `data` 不能当「无需复权」；基金 qfq 舍入后可能非正 |
| B1 | 东财个股分钟资金流 | §3.4 | **可用**：600519 返回 100 条，最后 15:00 主力净流入 -322,878,512 元 | 0.49 | 无 | push2 有 IP 风控；ETF 不覆盖（上游实测） |
| B2 | 东财个股日级资金流 120 日 | §4.5 | **可用**：120 条，2026-05-20 → 2026-11-12 | 0.24 | 无 | push2his 有 IP 风控；返回空 ≠ 无数据，要重试/换时段 |
| B3 | 东财融资融券明细 | §4.1 | **可用**：600519 共 4017 行，最新 2026-11-11 融资余额 150.94 亿 | 0.34 | 无 | datacenter 与 push2 不同 WAF；字段 `SCODE` |
| B4 | 东财股东户数 | §4.3 | **可用**：600519 最新 2026-09-30 户数 278,364，环比 -6.09% | 0.12 | 无 | 季度级；注意用 `HOLD_NOTICE_DATE` 而非 `END_DATE` 做 as-of |
| B5 | 东财筹码分布 | §4.6 | **不可用（无端点）**：`push2`/`push2his` `cyq/get` 均 **HTTP 404**；只能本地推演 | 4.00（250 行含登录） | baostock（换手率） | 推演不是实测持仓；绝对数值不可断言，只看相对变化 |
| C1 | 沪深 300 成分 | §12.1 | **可用**：300 行 × 9 列，文件日期 **2026-11-11** | 0.56 | `xlrd`/`openpyxl`（读 .xls） | 只是最近快照，**不提供历史时点成分**；历史横截面有幸存者偏差 |
| C2 | 中证指数估值 | §12.3 | **可用**：000300 20 行，最新 **2026-11-11**，PE1=13.83、股息率1=3.03% | 0.07 | `xlrd`/`openpyxl` | 只有 PE/股息率，无 PB；不承诺全历史 |
| C3 | 指数日线（regime 用） | §1.2 | **可用**：sh000300 区间 2026-03-01→2026-11-12 共 173 根；MA20=4077.24、MA60=4362.91 | 0.27 | 无 | 同上 A3；指数代码必须带 `sh` 前缀 |
| D1 | 巨潮公告 | §7.1 | **可用**：600519 共 1689 条，最新为 2026 年三季报相关公告 | 0.71 | 无 | 必须先查 `orgId`；`announcementTime` 是 Unix 毫秒；`announcementTypeName` 可能为 null |
| D2 | 东财业绩预告 | §14.1 | **可用**：600519 33 行，最新 2025-01-03 对 2024 年报 | 0.10 | 无 | 一行可能按指标拆多行；金额单位元 |
| D3 | 东财机构调研 | §14.2 | **可用但逐票差异大**：002415 有 7 行（2026-10-17）；600519 返回 `code=9201 返回数据为空` | 0.11 | 无 | 必须带 `IS_SOURCE="1"`、`NUMBERNEW="1"`；空表是「确实没有」，不是接口坏 |
| D4 | 东财股东增减持 | §14.3 | **可用**：600519 13 行，最新 2025-12-30 控股股东增持 | 0.08 | 无 | 变动股数带符号；方向与符号要交叉校验 |
| D5 | 东财回购 | §14.4 | **可用**：600519 2 个方案，最新方案 2026-05-28 完成 | 0.07 | 无 | 代码字段是 **`DIM_SCODE`**，不是 `SECURITY_CODE`；用错返回 `9501` |
| D6 | 东财股权质押 | §14.5 | **可用**：600519 610 行，最新统计日 2026-11-06，质押比例 0.14% | 0.31 | 无 | 中国结算周度（通常周五）；只覆盖沪深，不含北交所 |
| E1 | 新浪基金复权因子 | §1.6 | **可用**：510300 hfq 15 行，`s=1.0,u=0.88` 结构正确 | 0.58 | 无 | 基金用 `s/u` 等差+折算口径；个股用 `f` 比例口径，不能混 |
| E2 | 上交所 ETF 份额 | §4.7 | **可用**：2026-11-11 共 924 条（自报 total=924）；2023-01-03 也可查 | 0.38 | 无 | 必须带 Referer；服务端每页压到 2000；条数必须等于 total |
| E3 | 深交所 ETF 份额 | §4.7 | **可用（仅当前快照）**：快照日 2026-11-12，772 条 / 39 页 | 0.41 | 无 | 只能取最新一天；历史必须自己按日留存 |
| F1 | 深交所交易日历 | §12.4 | **可用**：2026-11 共 30 天、21 个交易日；11-12=交易日 | 0.38 | 无 | 未发布月份抛错，不伪装休市 |
| G1 | baostock 估值历史 | §6.5 | **可用**：Python 3.14.4 登录成功；600519 2026-11-01→11-12 共 9 行，含 PE/PB/PS/PCF/换手/停牌/ST | 0.58 | **baostock 0.9.4**（纯 Python） | TCP 登录；不支持北交所；登录/登出会打印到 stdout |
| G2 | baostock 标的基本信息 | §6.6 | **可用**：600519 上市日 2001-08-27，`outDate` 为空（在市） | 0.58（同会话） | 同上 | 已退市标的的除权除息三源全缺（上游 §6.6） |
| H1 | 交易日历复用 | §12.4 | **已接入并验证**：`astock.get_month_calendar(2026,11)` 返回 30 天，`calendar_source=astock_calendar` | — | 无 | `ASTOCK_CALENDAR_OFFLINE=1` 可强制离线；失败回退周末推算 |

### 2.2 逐类原始证据

> 以下命令均在 A5500 本机执行；`EXIT` 为 shell 退出码。
> 完整原始输出在本次探针的 `/tmp/p1probe_run/out/`，不随仓库提交。

#### 2.2.1 估值

**腾讯 §1.1 个股快照（PE/PB/市值/涨跌停）**

```bash
$ curl -sS --max-time 10 'https://qt.gtimg.cn/q=sh600519,sh000300' \
    | iconv -f gbk -t utf-8 | cut -c1-260
v_sh600519="1~贵州茅台~600519~1410.27~1402.30~1395.13~32373~15499~16874~1410.27~17~…~0.26~21.46~…
v_sh000300="1~沪深300~000300~4043.93~4075.71~4063.25~175464628~0~0~…~20261112161…
EXIT=0
```

按上游字段索引解析（§1.1 字段表）：

| 字段 | 600519 | sh000300 | 结论 |
|---|---|---|---|
| 最新价 | 1410.27 | 4043.93 | 可用 |
| PE(TTM) | 21.46 | 13.41 | 可用 |
| PB | 6.50 | **0.00** | 指数 PB 不可用 |
| 总市值(亿) | 17629.53 | 508626.81 | 可用 |
| 涨停/跌停 | 1542.53 / 1262.07 | **-1 / -1** | 指数无涨跌停价 |
| 时间戳 | 20261112161452 | 20261112161409 | 收盘后已定格 |

连续 3 次调用（间隔 1.2 s）：600519、000001（上证）、399006（创业板）、510300（ETF）
**所有字段完全一致**；仅 sh000001 的总市值末位从 `642315.88` 变为 `642315.87`。
结论：个股 PE/PB/市值字段稳定；**指数的 PB/涨跌停字段不能参与计算**。

**baostock §6.5（Python 3.14.4 / A5500，临时 venv）**

```bash
$ uv venv /tmp/p1probe_bs --python 3.14
$ uv pip install --python /tmp/p1probe_bs/bin/python baostock
Resolved 5 packages in 1.60s
 + baostock==0.9.4
 + numpy==2.5.4
 + pandas==3.1.1
 + python-dateutil==4.0.0
 + tzdata==2026.6
EXIT=0
```

`baostock-0.9.4.dist-info/WHEEL`：`Root-Is-Purelib: true`、`Tag: py3-none-any`；
`Requires-Dist: pandas>=0.18.0`；安装后包体 **548 KB**。

```bash
$ cd /tmp/p1probe_run && /tmp/p1probe_bs/bin/python probe_baostock.py
python 3.14.4
baostock 00.9.40
login success!
login.error_code= 0 error_msg= success
query.error_code= 0 error_msg= success fields= ['date', 'code', 'close', 'peTTM', 'pbMRQ', 'psTTM', 'pcfNcfTTM', 'turn', 'tradestatus', 'isST']
rows= 9
['2026-11-11', 'sh.600519', '1402.3000', '21.336094', '6.463712', '10.049589', '48.042058', '0.318800', '1', '0']
['2026-11-12', 'sh.600519', '1410.2700', '21.457358', '6.500449', '10.106706', '48.315106', '0.259000', '1', '0']
basic: ['sh.600519', '贵州茅台', '2001-08-27', '', '1', '1']
logout success!
elapsed_sec=0.58
EXIT=0
```

结论：**可用**。baostock 的 PE(TTM)=21.457、PB=6.500 与腾讯快照 21.46 / 6.50 一致，
说明两者同日口径可交叉校验。注意：`baostock` 登录成功 / 登出成功会直接 print 到
stdout，若未来在 CLI JSON 路径使用，必须重定向或捕获，避免污染 JSON。

#### 2.2.2 资金面 / 筹码

**东财 §3.4 分钟资金流**

```bash
$ curl -sS --max-time 15 -H 'Referer: https://quote.eastmoney.com/' \
  'https://push2.eastmoney.com/api/qt/stock/fflow/kline/get?secid=1.600519&klt=1&fields1=f1,f2,f3,f7&fields2=f51,f52,f53,f54,f55,f56,f57'
{"rc":0,…,"data":{"code":"600519","market":1,"name":"贵州茅台",…}}
EXIT=0
```

解析后：`rows=100`，首条 `2026-11-12 13:21`，末条 `2026-11-12 15:00,-322878512.0,…`。
单位是元（上游 §3.4 明确）。

**东财 §4.5 日级资金流 120 日**

```bash
$ curl -sS --max-time 15 -H 'Referer: https://quote.eastmoney.com/' \
  'https://push2his.eastmoney.com/api/qt/stock/fflow/daykline/get?secid=1.600519&fields1=f1,f2,f3,f7&fields2=f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61,f62,f63,f64,f65&lmt=120'
{"rc":0,…,"data":{"code":"600519","market":1,"name":"贵州茅台","klines":
["2026-05-20,-633552080.0,-773856.0,634325952.0,-150189040.0,-483363040.0,…"]}}
EXIT=0（解析后 rows=120）
```

**东财 §4.1 两融 / §4.3 股东户数**

```bash
$ curl -sS --max-time 20 -G 'https://datacenter-web.eastmoney.com/api/data/v1/get' \
  --data-urlencode 'reportName=RPTA_WEB_RZRQ_GGMX' \
  --data-urlencode 'columns=ALL' \
  --data-urlencode 'filter=(SCODE="600519")' \
  --data-urlencode 'pageNumber=1' --data-urlencode 'pageSize=1' \
  --data-urlencode 'sortColumns=DATE' --data-urlencode 'sortTypes=-1' \
  --data-urlencode 'source=WEB' --data-urlencode 'client=WEB'
{"result":{"pages":4017,"data":[{"DATE":"2026-11-11 00:00:00",…,"RZYE":15093509194,…}]}}
EXIT=0

$ curl … 'reportName=RPT_HOLDERNUMLATEST' … 'filter=(SECURITY_CODE="600519")' … 'sortColumns=END_DATE'
{"result":{"pages":1,"data":[{"SECURITY_CODE":"600519",…,"HOLDER_NUM":278364,
"PRE_HOLDER_NUM":296404,"HOLDER_NUM_RATIO":-6.086287634445,"END_DATE":"2026-09-30 00:00:00",…}]}}
EXIT=0
```

**东财 §4.6 筹码：端点不存在，只能本地推演**

```bash
$ curl -sS --max-time 15 -o /dev/null -w 'HTTP=%{http_code}' \
  'https://push2.eastmoney.com/api/qt/stock/cyq/get?secid=1.600519'
HTTP=404
EXIT=0
$ curl -sS --max-time 15 -o /dev/null -w 'HTTP=%{http_code}' \
  'https://push2his.eastmoney.com/api/qt/stock/cyq/get?secid=1.600519'
HTTP=404
EXIT=0
```

按上游 §4.6 原样代码（`vendor/a-stock-data/SKILL.md:3272-3366`，不改写）用 baostock
取 600519 2025-11-03→2026-11-12 的 250 个交易日，计算本地筹码：

```text
rows_raw=250 rows_trading=250 price=1410.27
profit_ratio=0.906239 avg_cost=1352.2287
cost_90=[1218.179, 1438.5923] cost_70=[1286.7349, 1386.281]
concentration_90=0.082963 peak_price=1377.8001
seconds=4.002（含 baostock 登录 + 250 行查询）
EXIT=0
```

结论：**算法可运行，但它是推演不是实测持仓**。P1 只能把「获利比例变化、筹码峰相对
现价的位置、集中度变化」当特征，不能把绝对值当真实成本分布。

#### 2.2.3 指数 regime

**腾讯 §1.2 指数日线：一个入口坏了，另两个可用**

```bash
# 坏入口（本机实测）
$ curl -sS --max-time 15 -H 'Referer: https://gu.qq.com/' \
  'https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param=sh000300,day,2026-09-01,2026-11-12,640,qfq' \
  -w '\nHTTP=%{http_code}'
<!DOCTYPE html><html><head><script>…window.location.href="https://waf.tencent.com/501page.html?…"
HTTP=501
EXIT=0

# 可用入口
$ curl -sS --max-time 15 -H 'Referer: https://gu.qq.com/' \
  'https://proxy.finance.qq.com/ifzqgtimg/appstock/app/fqkline/get?param=sh000300,day,2026-09-01,2026-11-12,640,qfq'
{"code":0,"msg":"","data":{"sh000300":{"day":[["2026-09-01","4618.730",…],…]}}}
EXIT=0
```

同理 `https://ifzq.gtimg.cn` 也正常。现有适配器 `astock.fetch_daily()` 端到端实测：
600519 qfq 2026-11-01→11-12 返回 **9 行**，最后一行
`open=1395.13, high=1411.00, low=1388.11, close=1410.27, volume=3237300`，
耗时 **0.70 s**（含三入口轮换与新浪复权因子）。

**指数 regime 最小实现路径**

当前空实现：

```python
# source/stock_analysis/data_loader/meta.py:820-822
def get_market_index_daily(symbol='000985', start_date='20200101', source='akshare'):
    """获取指数数据。"""
    return None
```

最小路径不需要新依赖：把 `sh000300` 交给 `_astock_generated.tencent_kline(adjust='')`
取日线，再复用 `analyzer._regime_from_row()`（`analyzer.py:74-91`）。实测：

```text
index=sh000300 rows=173 last_date=2026-11-12
close=4043.93 ma20=4077.24 ma60=4362.91
ret20_pct=-3.04 ret60_pct=-13.29 ann_vol=0.1689
EXIT=0
```

按当前 `_regime_from_row` 规则，`close < ma20`、`close < ma60`、`ret20=-3.04%`（未低于 -5%），
所以会落到 **Shock**；这正说明「指数 regime」与「个股 regime」可能给出不同标签，
必须先做同决策日 A/B，不能直接替换。

**沪深 300 成分 / 中证指数估值**

```bash
$ curl -sS --max-time 40 \
  'https://oss-ch.csindex.com.cn/static/html/csindex/public/uploads/file/autofile/cons/000300cons.xls' \
  -o /tmp/cscons.xls -w 'HTTP=%{http_code} bytes=%{size_download}'
HTTP=200 bytes=67072
EXIT=0
# 读取：300 行 × 9 列，日期 20261111，列名含空格（"指数代码 Index Code"）

$ curl -sS --max-time 40 \
  'https://oss-ch.csindex.com.cn/static/html/csindex/public/uploads/file/autofile/indicator/000300indicator.xls' \
  -o /tmp/csval.xls -w 'HTTP=%{http_code} bytes=%{size_download}'
HTTP=200 bytes=8192
EXIT=0
# 读取：20 行 × 10 列，最新 20261111：PE1=13.83、PE2=15.66、股息率1=3.03%、股息率2=2.6%
```

结论：**指数 regime 可用**；但成分是「最近快照」，不是历史时点成分。
历史横截面研究必须显式披露幸存者偏差，不能用当前 300 只冒充当年的 300 只。

#### 2.2.4 事件

**巨潮 §7.1 公告**

```bash
$ curl -sS --max-time 20 'http://www.cninfo.com.cn/new/data/szse_stock.json' \
  | head -c 120
{"stockList":[{"code":"000001","pinyin":"payh","category":"A股","orgId":"gssz0000001",…}]}
EXIT=0

$ curl -sS --max-time 25 -X POST 'https://www.cninfo.com.cn/new/hisAnnouncement/query' \
  -H 'Referer: https://www.cninfo.com.cn/new/disclosure' \
  -H 'Origin: https://www.cninfo.com.cn' \
  -H 'Content-Type: application/x-www-form-urlencoded' \
  --data 'stock=600519,gssh0600519&tabName=fulltext&pageSize=2&pageNum=1&column=&category=&plate=&seDate=&searchkey=&secid=&sortName=&sortType=&isHLtitle=true'
{"totalAnnouncement":1689,…,"announcements":[{"announcementId":"1225652625",
"announcementTitle":"贵州茅台关于召开2026年第三季度业绩说明会的公告",…}]}
EXIT=0
```

注意：`announcementTime` 是 Unix 毫秒；本次响应里 `announcementTypeName` 为 `null`
（不能假设类型字段永远有值）。

**东财 §14.1–§14.5 事件**

```bash
# §14.1 业绩预告
$ curl … 'reportName=RPT_PUBLIC_OP_NEWPREDICT' … 'filter=(SECURITY_CODE="600519")' \
  … 'sortColumns=NOTICE_DATE' --data-urlencode 'sortTypes=-1'
{"result":{"pages":33,"data":[{"NOTICE_DATE":"2025-01-03 00:00:00","REPORT_DATE":"2024-12-31",
"PREDICT_FINANCE":"营业收入","PREDICT_AMT_LOWER":173800000000,"ADD_AMP_LOWER":15.44,…}]}}
EXIT=0

# §14.2 机构调研（必须带 IS_SOURCE / NUMBERNEW；600519 无记录，002415 有 7 条）
$ curl … 'reportName=RPT_ORG_SURVEYNEW' \
  --data-urlencode 'filter=(SECURITY_CODE="002415")(IS_SOURCE="1")(NUMBERNEW="1")' \
  … 'sortColumns=NOTICE_DATE,SECURITY_CODE,RECEIVE_START_DATE' --data-urlencode 'sortTypes=-1,1,-1'
{"result":{"pages":7,"data":[{"SECURITY_CODE":"002415","NOTICE_DATE":"2026-10-17",…,"SUM":202}]}}
EXIT=0
# 600519 同样参数：{"success":false,"code":9201,"message":"返回数据为空"}

# §14.3 股东增减持
$ curl … 'reportName=RPT_SHARE_HOLDER_INCREASE' … 'filter=(SECURITY_CODE="600519")'
{"result":{"pages":13,"data":[{"NOTICE_DATE":"2025-12-30","HOLDER_NAME":"中国贵州茅台酒厂(集团)有限责任公司",
"CHANGE_NUM_SYMBOL":127.4234,"DIRECTION":"增持",…}]}}
EXIT=0

# §14.4 回购（代码字段是 DIM_SCODE；用 SECURITY_CODE 会 9501）
$ curl … 'reportName=RPTA_WEB_GETHGLIST_NEW' --data-urlencode 'filter=(DIM_SCODE="600519")' \
  … 'sortColumns=UPD,DIM_SCODE,REPURCODE' --data-urlencode 'sortTypes=-1,1,1'
{"result":{"pages":2,"data":[{"DIM_SCODE":"600519","REPURPROGRESS":"006","REPURAMOUNT":2999933749.57,…}]}}
EXIT=0

# §14.5 股权质押
$ curl … 'reportName=RPT_CSDC_LIST' … 'filter=(SECURITY_CODE="600519")' … 'sortColumns=TRADE_DATE'
{"result":{"pages":610,"data":[{"TRADE_DATE":"2026-11-06","PLEDGE_RATIO":0.14,
"REPURCHASE_BALANCE":172.7,…}]}}
EXIT=0
```

结论：**事件数据可用，但字段陷阱多**：机构调研的额外过滤条件、回购的 `DIM_SCODE`、
两融的 `SCODE`、股东户数的 `HOLD_NOTICE_DATE`、质押的周度统计日。任何「HTTP 200」
都不等于「成功」，必须检查 `success` / `result.count`。

#### 2.2.5 基金 / ETF

**新浪 §1.6 基金复权因子**

```bash
$ curl -sS --max-time 10 -H 'Referer: https://finance.sina.com.cn/' \
  'https://finance.sina.com.cn/realstock/company/sh510300/hfq.js' | head -c 240
var sh510300hfq={"total":15,"data":[{"d":"2026-01-19","f":"1","s":"1.0000000000000000","u":"0.8800000000000000"},…]}
EXIT=0
```

`f` 是个股比例因子；基金的 `s`（份额折算）与 `u`（累计分红）不能与个股 `f` 混用。
项目现有 `astock.fetch_daily()` 已用「腾讯不复权 + 新浪比例因子」做个股复权；
基金路径需要单独适配（R1 §8.2 P1-5）。

**§4.7 ETF 份额**

```bash
# 上交所：可按历史日期查
$ curl -sS --max-time 30 -H 'Referer: https://www.sse.com.cn/' \
  'https://query.sse.com.cn/commonQuery.do?sqlId=COMMON_SSE_ZQPZ_ETFZL_XXPL_ETFGM_SEARCH_L&STAT_DATE=2026-11-11&isPagination=true&pageHelp.pageSize=10000&pageHelp.pageNo=1&pageHelp.beginPage=1&pageHelp.cacheSize=1&pageHelp.endPage=1'
{"pageHelp":{"data":[{"STAT_DATE":"2026-11-11","SEC_CODE":"510010","SEC_NAME":"治理ETF","TOT_VOL":"12752.44",…}], "total":924}}
EXIT=0
# 2023-01-03 也能查到（HTTP 200，约 110 KB）

# 深交所：只有当前快照
$ curl -sS --max-time 30 -H 'Referer: https://fund.szse.cn/' \
  'https://fund.szse.cn/api/report/ShowReport/data?SHOWTYPE=JSON&CATALOGID=1000_lf&TABKEY=tab1&selectJjlb=ETF&PAGENO=1'
[{"metadata":{"subname":"2026-11-12","pagecount":39,"recordcount":772,…}, "data":[…]}]
EXIT=0
```

结论：上交所历史可查；深交所只能当天留存。两所都是「万份」，但类别字段口径不同，
不能硬拼成一列。

#### 2.2.6 交易日历

```bash
$ curl -sS --max-time 30 \
  'https://www.szse.cn/api/report/exchange/onepersistenthour/monthList?month=2026-11'
{"data":[{"zrxh":1,"jybz":"0","jyrq":"2026-11-01"},{"zrxh":2,"jybz":"1","jyrq":"2026-11-02"},…]}
EXIT=0
```

项目已接入并验证：

```text
calendar 2026-11 rows=30 source=astock_calendar 11-12=True err=None
```

实现位置：`source/stock_analysis/data_sources/astock.py:255-339`；
`meta._expected_latest_trade_date()` 已优先调用它（`meta.py:770-788`）。
结论：**可复用**；本任务不需要重复实现，只需在 P1 的 as-of 校验里继续使用同一日历。

---

## 3. 证据方法

> 本节定义「怎么证明一个 P1 候选值得进入决策」。所有公式都按可复算口径写；
> 没有实测的地方写「待测」。**严禁前视**：任何特征只能用决策日 `d` 收盘后、
> 下一交易日开盘前已经可得的数据。

### 3.1 通用约定

#### 3.1.1 决策日、成交与标签

- 决策日 `d` = `data.end` 的最后一根日线。
- 成交口径沿用项目契约：`fill=next_open`、`slippage_bps=10`、`commission_bps=3`
  （`README.md:125-134`、`source/decision/schema.py:260-267`）。
- 标签收益：`r_{d,h} = open_{d+1+h} / open_{d+1} - 1`，`h=20` 个交易日。
  **不用 `close_d` 当日收盘价成交**，避免「看到收盘价再按收盘价买」的前视。
- 事件研究另用事件窗口：`[-5,+20]` 交易日，事件日 = 公告日后的第一个交易日
  （公告在收盘后发布的，事件日再顺延一天）。

#### 3.1.2 as-of 与发布滞后

每个特征必须带 `feature_date` 与 `pub_lag_days`；构建面板后先跑 as-of 校验：
`assert feature_date + pub_lag_days <= decision_date`。

| 数据 | 字段日期 | 建议 `pub_lag_days` | 理由 |
|---|---|---|---|
| 腾讯/新浪日线、PE/PB 快照 | 交易日 `d` | 0（下一交易日开盘前可用） | 收盘后即可取；用于 `d+1` 开盘决策 |
| baostock PE/PB/换手/停牌/ST | 交易日 `d` | 0 | 同上；但必须缓存，不能回测时逐日联网 |
| 东财日级资金流 | 交易日 `d` | 1 | 收盘后发布，保守按 T+1 可用 |
| 两融 | `DATE` | 1 | 交易所 T+1 披露 |
| 股东户数 | `HOLD_NOTICE_DATE` | 1 | **不能用 `END_DATE`**（报告期早于公告日） |
| 业绩预告 | `NOTICE_DATE` | 1 | 公告日收盘后发布 |
| 机构调研 | `NOTICE_DATE` | 1 | 同上 |
| 增减持 | `NOTICE_DATE` | 1 | 同上 |
| 回购 | `UPDATEDATE` / `NOTICEDATE` | 1 | 同上 |
| 质押 | `TRADE_DATE`（中国结算统计日） | 1 | 周度，通常周五 |
| 巨潮公告 | `announcementTime` 的日期 | 1 | 公告时间可能在收盘后 |
| 中证成分/估值 | 文件 `date` | 0 | 官方文件日期 ≤ 决策日才可用 |
| ETF 份额（沪） | `STAT_DATE` | 1 | T 日盘后/T+1 确认 |
| ETF 份额（深） | 快照日 | 1 | 只有当前快照，历史需自己留存 |

#### 3.1.3 样本池

- **主池（横截面）**：2026-11-11 的沪深 300 成分（300 只）+ 14 只自选
  （`tools/forward_log.py:38-40`），按代码去重。
- **历史可用性过滤**：用 baostock `ipoDate` 剔除决策日尚未上市的标的；
  用 `tradestatus` 剔除停牌日；用 `isST` 记录当时 ST 状态。
- **幸存者偏差声明**：中证成分是当前快照，不是历史时点成分。主结果必须标注
  「current-constituent backtest」，并另跑一版**自选 14 只**的结果；不得把
  300 只结果说成「历史沪深 300 的真实成分」。
- **最小样本**：
  - 横截面主结果：≥250 个决策日、每日 ≥100 只、≥60 个互不重叠的 20 日区间
    （约 5 年数据）。
  - 事件：每类 ≥100 个事件、≥30 个不同公告日；不足只做描述性展示。
  - 基金/ETF：44 组基金网格 + ≥100 只 ETF、≥250 个交易日。
  - 自选 14 只：只做案例与前瞻台账，不做统计显著性结论。

#### 3.1.4 指标与验收阈值（预注册）

| 指标 | 定义 | 记录方式 | 进入「只展示」 | 进入「改 action」 |
|---|---|---|---|---|
| 秩 IC | 每日 `Spearman(feature_d, r_{d,20})` 的均值；t 值用非重叠样本或 Newey-West(lag=19) | 均值、t、n_eff、ICIR | `abs(IC) ≥ 0.02` 且 `abs(t) ≥ 2.0` | 样本外（最后 30% 时间）`abs(IC) ≥ 0.02` 且 `abs(t) ≥ 2.0` |
| IC 稳定性 | 按年分桶，IC 同号占比 | 年度表 | ≥2/3 年份同号 | 连续 2 年同号 + 样本外同号 |
| 买点命中率 | 买点后 20 日上涨占比；与基线两比例检验 | 命中率、Δ、p | Δ ≥ 0 即可展示 | Δ ≥ 5pp 且 p<0.05 |
| 置信度校准 | 5 分箱：箱内实际命中率 vs 置信度；ECE / Brier | 可靠性表 | 只报告，不设门槛 | ECE ≤ 0.05 且 Brier 改善 ≥0.01 |
| 最大回撤 | 按 `policy.py` 口径回放，block bootstrap | max DD、95% CI | 只报告 | 不劣于基线（容差 +1pp），且年化不降 |
| 数据源错误率 | `status=data_source_error` 次数 ÷ 请求数 | 计数、退出码 | 新通道错误率 ≤ 现有通道 | — |

**多重检验**：每个 P1 候选只允许一个「主指标 + 主因子」预注册；其余为探索性，
必须用 `evidence.n_trials` / DSR 校正。**禁止**先跑 50 个因子再挑最好看的那个宣称显著。

#### 3.1.5 复现命令契约

现有工具可直接复现基线：

```bash
# 价格类因子基线（完全离线，读 source/stock_analysis/data/*.csv）
python tools/ic_report.py --min-bars 500 --horizons 1,5,10,20,60

# 前瞻台账（今天记录，20 个交易日后结算）
python tools/forward_log.py record
python tools/forward_log.py evaluate

# 跨源一致性
python tools/check_source_parity.py 600519 --source sina --tol 1e-6
```

P1 新增证据运行器（**本 PR 不实现**，后续 PR 落地）统一 CLI：

```bash
# 1) 构建 as-of 面板（离线；输入为已缓存的 CSV/快照，不联网）
python tools/p1_evidence.py panel \
  --start 2019-01-01 --end 2026-11-11 \
  --pool csi300,watchlist14 \
  --sources price,valuation,flow,margin,holder,chip,index,events \
  --out evidence/p1/panel.parquet

# 2) 校验无前视（任何 feature_date + pub_lag_days > decision_date 即失败）
python tools/p1_evidence.py check-asof --panel evidence/p1/panel.parquet

# 3) 单因子 IC（非重叠 + Newey-West）
python tools/p1_evidence.py ic --panel evidence/p1/panel.parquet \
  --factor pb_pct_3y --horizon 20 --nonoverlap --newey-west 19

# 4) 买点分层（有/无某特征）
python tools/p1_evidence.py stratify --panel evidence/p1/panel.parquet \
  --factor flow_main_20d --on buy_trigger --horizon 20

# 5) 回放与回撤（复用项目 policy 口径）
python tools/p1_evidence.py replay --panel evidence/p1/panel.parquet \
  --policy source/decision/policy.py --fill next_open --slippage-bps 10 --commission-bps 3
```

产物固定为 `evidence/p1/<run_id>/{panel.parquet,ic.csv,stratify.csv,replay.json,manifest.json}`；
`manifest.json` 记录输入文件 sha256、git commit、命令、Python 版本，保证同输入可复算。

### 3.2 P1-1 估值上下文

**假设**：估值分位（PE/PB/PS 在自身历史与横截面中的位置）对 20 日收益有增量信息，
或者至少能降低「高位追买」的尾部风险。

| 项 | 定义 |
|---|---|
| 特征 | `pe_ttm`, `pb_mrq`, `ps_ttm`, `pcf_ttm`；`pe_pct_3y`/`pb_pct_3y` = 过去 756 个交易日分位；`log_mcap`；`pe_ttm` 为负时单独标记，不参与排序 |
| 数据 | baostock §6.5（历史日频）+ 腾讯 §1.1（当日快照交叉校验）；前向 EPS/PEG **暂不做**（§2.2 同花顺一致预期另议） |
| as-of | `date ≤ d`，`pub_lag_days=0`；不得使用任何 `date > d` 的行；前复权价用于收益率，估值字段本身不复权 |
| 样本池 | 沪深 300 + 自选 14；剔除 `pe_ttm<=0` 的亏损样本（单独统计），剔除停牌日 |
| 指标 | 秩 IC（20 日）、分 5 组的 Top-Bottom 价差、买点命中率分层、置信度校准 |
| 最小样本 | ≥250 日 × ≥100 只 × ≥60 个非重叠 20 日区间 |
| 验收 | 只展示：`abs(IC)≥0.02` 且 `abs(t)≥2`；改 action：样本外同号 + 命中率 Δ≥5pp + 回撤不劣化 |
| 复现 | `python tools/p1_evidence.py panel --sources price,valuation …`；`ic --factor pb_pct_3y --horizon 20` |
| 依赖 | **baostock**（历史估值）；若拒绝引入，只能从今往后每日落腾讯快照，**无法做历史证据** |
| 风险 | 20 日尺度估值可能无预测力；亏损股 PE 为负；当前成分幸存者偏差；北交所不支持 |

**先做什么**：只新增 `evidence.valuation`（PE/PB/分位/数据日期）作为上下文，
`action` 完全不变；A/B golden JSON 必须证明 14 只自选标的的 `action/target_position`
逐字节一致。

### 3.3 P1-2 资金面 / 筹码上下文

**假设**：主力资金净流入、两融余额变化、股东户数下降、筹码集中度上升，
对买点确认或回撤控制有增量信息。

| 项 | 定义 |
|---|---|
| 特征 | `flow_main_5d/20d = Σ main_net / float_mcap`；`margin_chg_5d/20d = RZYE_d/RZYE_{d-5/20}-1`；`holder_chg_qoq = HOLDER_NUM_RATIO`；`chip_profit_ratio`、`chip_peak_dist = (price-peak)/price`、`chip_conc_90` |
| 数据 | 东财 §3.4（分钟，仅当日，不用于 20 日证据）、§4.5（日级 120 日）、§4.1、§4.3；筹码 §4.6 本地推演，输入 baostock §6.5 |
| as-of | 资金流 `pub_lag_days=1`；两融 1；股东户数用 `HOLD_NOTICE_DATE`；筹码用 `date ≤ d` 的 OHLC+turn，`pub_lag_days=0`（收盘后算，次日开盘用） |
| 样本池 | 沪深 300 + 自选 14；只统计有资金流数据的沪深标的（ETF 不覆盖） |
| 指标 | 秩 IC；**买点分层**（有/无正向资金流）的 20 日均值、命中率、t；扣费净收益/max DD 对照；校准 |
| 最小样本 | 资金流 ≥250 日 × ≥100 只；事件类（户数/两融）≥4 个季度 × ≥100 只；筹码 ≥250 日 × ≥100 只 |
| 验收 | 只展示：IC `abs(t)≥2`；改 action：命中率 Δ≥5pp + 回撤不劣化 + 样本外同号 |
| 复现 | `panel --sources flow,margin,holder,chip`；`ic --factor flow_main_20d`；`stratify --on buy_trigger` |
| 依赖 | 日级资金流/两融/户数无新依赖；**筹码依赖 baostock 换手率** |
| 风险 | 东财 IP 封禁；字段口径变化；20 日尺度衰减；筹码是推演；ETF 无个股资金流 |

**降级方案（不引入 baostock 时）**：换手率用 `volume / 流通股本` 近似，其中
流通股本由腾讯 §1.1 的流通市值 ÷ 现价反推。该近似会因送转/增发/解禁而漂移，
必须标记为 `chip_source=approx_float_shares`，且不得用于「改 action」的证据。

### 3.4 P1-3 指数 regime

**假设**：市场指数 regime 比个股自身 MA20/MA60 更能代表「大盘环境」，
能改善 `regime_win_rates` 的稳定性或最大回撤。

| 项 | 定义 |
|---|---|
| 特征 | `index_close`、`ma20`、`ma60`、`ret20`、`ret60`、`ann_vol20`、`index_pe`、`index_dy` |
| 数据 | 腾讯 §1.2 指数日线（`sh000300`）+ §12.3 中证估值；成分 §12.1 仅用于横截面研究，不用于 regime 标签 |
| as-of | 指数日线 `date ≤ d`，`pub_lag_days=0`；中证估值文件 `date ≤ d` |
| 样本池 | 指数本身（时间序列）+ 沪深 300 成分（横截面）；不要求成分历史，因为标签只来自指数价格 |
| 指标 | 与现有个股 regime 的标签一致率；`regime_win_rates` 的跨年稳定性；校准（Bull/Bear/Shock 分箱命中率）；回放 max DD / 年化 |
| 最小样本 | ≥250 个决策日；每年 ≥30 个决策日；不重叠 20 日区间 ≥60 |
| 验收 | 只展示：`evidence.index_regime` 与现有 `market_regime` 并列；改行为：校准 Brier 改善 ≥0.02 或 max DD 改善 ≥2pp，且命中率不降 >2pp |
| 复现 | `panel --sources price,index`；`replay --compare-regime stock_vs_index` |
| 依赖 | 无新依赖；腾讯指数 K 线 + 中证估值文件 |
| 风险 | 当前成分快照幸存者偏差（横截面）；regime 标签改变历史输出，属**行为变更**；指数 PB 不可用 |

**最小实现**：把 `get_market_index_daily()` 从 `return None` 改为
「腾讯 `sh000300` 不复权日线 → DataFrame」，再在 `analyzer` 增加
`get_index_regime()`；**先只展示，不替换** `get_current_market_regime()`。
实测 173 根日线足以算 MA60（`close=4043.93, ma20=4077.24, ma60=4362.91`）。

### 3.5 P1-4 事件黑名单 / 风险提示

**假设**：未来事件窗口（业绩预告、解禁/减持、质押、回购、调研）能提示「先别买/降仓」，
降低建议的最大回撤，或提高建议可执行率。

| 项 | 定义 |
|---|---|
| 特征 | `days_to_forecast`、`forecast_type`、`days_to_pledge`、`pledge_ratio_pct`、`holder_trade_20d`、`buyback_progress`、`survey_count_20d`、`announcement_count_5d` |
| 数据 | §7.1 巨潮；§14.1–§14.5 东财（本批不做 §3.6 解禁，见 §6） |
| as-of | 全部按公告日 `NOTICE_DATE`/`announcementTime` + 1 个交易日；不得用报告期 |
| 样本池 | 沪深 300 + 自选 14；事件组与无事件组按同一决策日配对 |
| 指标 | 事件窗口 `[-5,+20]` 异常收益；买点命中率（有/无 warning）；max DD；warning 精确率（被标记事件中后续 20 日跌幅 >5% 的占比） |
| 最小样本 | 每类 ≥100 个事件、≥30 个不同公告日；不足只展示 |
| 验收 | 只展示：warning 出现/不出现符合 fixture；改 action：max DD 改善 ≥2pp 且 warning 精确率 ≥60%，命中率不降 >2pp |
| 复现 | `panel --sources events`；`stratify --factor event_warning --on buy_trigger` |
| 依赖 | 无新依赖；东财多端点（必须串行 ≥1s） |
| 风险 | 事件重复/缺失；公告时间与生效时间不同；warning 疲劳；调研 600519 为空表 |

**先做什么**：只在 `diagnostics.warnings` 增加可读事件提示，不改 `action`；
warning 必须带来源、事件日、字段原文，避免「一句空泛的风险提示」。

### 3.6 P1-5 基金 / ETF 数据增强

**假设**：基金复权因子、ETF 份额变化能提高基金建议的数值正确性，或提供申赎/规模信号。

| 项 | 定义 |
|---|---|
| 特征 | `fund_adj_factor`、`etf_shares_chg_5d/20d`、`etf_premium_discount`、`tracking_diff_20d` |
| 数据 | 新浪 §1.6（基金 `s/u`）；上交所 §4.7（历史）；深交所 §4.7（当前快照，自行留存） |
| as-of | 份额 `STAT_DATE + 1`；深交所快照日 + 1；复权因子按生效日 `date ≤ d` |
| 样本池 | 现有基金网格 44 组 + ≥100 只 ETF |
| 指标 | 跨源一致性（最大绝对差）；份额变化与未来 20 日收益的 IC；折溢价与回撤；净值序列差异可解释 |
| 最小样本 | 44 组 × ≥250 日；ETF ≥100 只 × ≥250 日 |
| 验收 | 复权 parity ≤1e-6；份额信号 `abs(t)≥2`；现有 44 组基金网格不回归 |
| 复现 | `panel --sources fund,etf`；`python tools/check_source_parity.py` 同思路 |
| 依赖 | 无新依赖（xlrd/openpyxl 仅读官方文件时用） |
| 风险 | 场外基金无 OHLCV/份额；基金代码段与股票重叠；复权口径变化会改历史净值 |

**先做什么**：只把复权因子/份额作为基金上下文展示；不改变「场外 OTC 净值 = close」
路径（R1 §8.2 P1-5）。

### 3.7 P1-6 交易制度完整性

**假设**：回测/建议若在停牌日成交、用错历史 ST 涨跌停、或把已退市标的当活票，
会系统性高估收益；修正后应降低回撤/提高可执行率。

| 项 | 定义 |
|---|---|
| 特征 | `tradestatus`、`isST`、`outDate`、`price_limit_pct`、`days_to_delist` |
| 数据 | baostock §6.5/§6.6；当前 ST 名单 §6.8（P0-3 已接入）；交易日历 §12.4 |
| as-of | `date ≤ d`；`outDate <= d` 视为已退市；ST 用当日 `isST`，不能用今天的名单回填历史 |
| 样本池 | 回测全样本 + 沪深 300；历史 ST/停牌/退市 fixture |
| 指标 | 停牌日成交次数（必须 0）；退市标的建议数（必须 0）；ST 涨跌停幅度正确率；历史回测差异逐项解释 |
| 最小样本 | 至少 20 个停牌事件、20 个 ST 区间、10 个退市样本（若可得） |
| 验收 | 0 次不可成交 bar 成交；0 条退市建议；ST 限价 100% 正确；回测差异表完整 |
| 复现 | `python tools/p1_evidence.py replay --respect-tradestatus --respect-st` |
| 依赖 | **baostock**（历史 ST/停牌/退市）；§6.8 只能给当日名单 |
| 风险 | 已退市标的除权除息三源全缺；改变历史回测数字，属**行为变更** |

**先做什么**：只做「读取 + warning/标记」，不直接改 `action`；`untradeable` 只有在
停牌/一字板/ST 规则冲突有明确证据时才用（与 P0-3 一致）。

---

## 4. 实施顺序与 PR 边界

### 4.1 三条硬边界

1. **只展示不改 action**：新增 `evidence.*` / `diagnostics.*` 字段，`action`、
   `position_fraction`、`status` 必须与改动前逐字节一致；用 14 只自选的 golden JSON
   A/B 对照。
2. **行为变更单独 PR**：改 `action`、改 `market_regime` 标签、改
   `untradeable`、改回测成交口径，全部单独 PR + 大池证据 + 用户批准。
3. **离线可测**：所有新增适配器必须有 fixture 单测；`import` 不联网；
   联网只发生在显式的缓存/预计算任务里。

### 4.2 推荐顺序

| 阶段 | 内容 | 依赖 | 是否改行为 | 验收 |
|---|---|---|---|---|
| **S0（本 PR）** | `docs/P1_EVIDENCE_PLAN.md`：数据可用性、证据方法、PR 边界 | 无 | 否 | 本文 |
| **S1** | `tools/p1_evidence.py` 骨架 + 离线面板 + `check-asof` + fixture | 现有缓存 | 否 | 同输入同输出；as-of 校验 0 违例 |
| **S2** | P1-3 指数 regime **只展示**：`get_market_index_daily` 最小实现 + `evidence.index_regime` | 腾讯指数日线、中证估值 | 否（并列展示） | golden JSON action 不变；指数 regime 标签可复算 |
| **S3** | P1-2 日级资金面 **只展示**（先不做筹码） | 东财 §4.5/§4.1/§4.3 | 否 | IC 报告；东财串行断言；离线 fixture |
| **S4** | P1-4 事件 warning **只展示**（业绩预告/增减持/回购/质押/公告） | 东财 §14.x、巨潮 §7.1 | 否 | fixture 事件窗口；warning 可读；action 不变 |
| **S5** | baostock 可选依赖 PR（**需用户批准**） | baostock 0.9.4 | 否（研究/预计算） | Python 3.14 + A5500 登录/取数；lazy import；JSON 不被 print 污染 |
| **S6** | P1-1 估值 / P1-2 筹码 / P1-6 制度完整性（按批准范围） | S5 | 否（先展示） | as-of 无前视；IC/分层/回放报告 |
| **S7** | P1-5 基金/ETF **只展示** | 新浪 §1.6、两所 §4.7 | 否 | parity；44 组不回归 |
| **S8** | 行为变更 PR（regime 替换 / 因子过滤 / untradeable） | S2–S7 的证据 + 用户批准 | **是** | 命中率/回撤/校准达标；历史差异逐项解释 |

> 如果用户**不批准 baostock**：S5–S6 的 P1-1/P1-6 改为「前向快照累积」，
> 当前只能做 P1-3 → P1-2（不含筹码）→ P1-4 → P1-5；不得用近似筹码出「改 action」结论。

### 4.3 依赖图

```text
S0 文档
  └─ S1 证据运行器（离线面板 + as-of 校验）
       ├─ S2 指数 regime（展示）───┐
       ├─ S3 日级资金面（展示）     │
       ├─ S4 事件 warning（展示）   │
       ├─ S5 baostock（可选依赖）   │
       │    ├─ S6a 估值（展示）     │
       │    ├─ S6b 筹码（展示）     │
       │    └─ S6c 制度完整性（展示）
       └─ S7 基金/ETF（展示）       │
                                    ▼
                         S8 行为变更（单独 PR + 用户批准）
```

### 4.4 PR 边界清单

| PR | 允许改 | 禁止改 | 必须证明 |
|---|---|---|---|
| 本 PR | `docs/P1_EVIDENCE_PLAN.md` | `source/`、`requirements.txt`、`README.md`、`vendor/`、测试 | 文档自检 |
| S1 | `tools/p1_evidence.py`、`docs/`、fixture | 生产决策链 | 离线复现、as-of 0 违例 |
| S2 | `meta.get_market_index_daily`、`analyzer` 新函数、`schema.evidence` 新块、README、测试 | 替换现有 regime | golden JSON action 不变 |
| S3/S4/S7 | 数据适配器、schema additive、render 文案、fixture | 改 action | action 不变、离线 |
| S5 | 可选依赖文件、adapter、fixture | 主 CLI 强制 import | lazy import、Python 3.14 |
| S8 | 决策/回测行为 | 无证据的改动 | 大池证据 + 用户批准 |

---

## 5. baostock 决策建议

### 5.1 可用性结论

**可用（本机 A5500 / Python 3.14.4 实测）**：

| 项 | 实测 |
|---|---|
| 安装 | `uv pip install baostock` → `baostock==0.9.4`，纯 Python `py3-none-any`，包体 548 KB |
| 依赖 | 仅 `pandas>=0.18.0`（项目已有 pandas 3.0.1） |
| 登录 | `bs.login()` → `error_code=0 success`，0.58 s（含查询） |
| 取数 | 600519 2026-11-01→11-12 共 9 行；`peTTM/pbMRQ/psTTM/pcfNcfTTM/turn/tradestatus/isST` 全有值 |
| 基本信息 | 600519 上市日 2001-08-27，`outDate` 空；`status=1` |
| 交叉校验 | baostock PE=21.457 / PB=6.500 与腾讯快照 21.46 / 6.50 一致 |
| 退出 | `bs.logout()` 正常；登录/登出会 print 到 stdout |

**限制**：TCP 长连接；不支持北交所（4/8/92/920）；已退市标的的除权除息三源全缺；
没有官方 SLA；版本 0.9.4 较老，但接口稳定。

### 5.2 成本

- **磁盘/依赖**：548 KB + 一个 pandas 依赖（已存在），可忽略。
- **网络**：一次 `login` + 一次查询约 0.58 s；批量回补应按会话复用，不要每只票登录。
- **工程**：需要新增 adapter、缓存 CSV、错误路径、fixture；必须 lazy import，
  不能让主 CLI 在没装 baostock 时崩。
- **维护**：TCP 协议与第三方库；上游已多年未大改，但仍是外部依赖。
- **合规/许可**：baostock 是公开数据接口；引入前确认其使用条款（本任务未做法务判断）。

### 5.3 结论

**有条件引入：作为「研究/预计算」可选依赖，不进主 `requirements.txt`、不进主 CLI 路径。**

理由：

1. 它是 P1-1 历史估值、P1-2 真筹码、P1-6 历史 ST/停牌/退市的**唯一零鉴权来源**；
   没有它，这三项只能做「从今天起的前向快照」，无法在合理时间内产出历史证据。
2. 成本低：纯 Python、548 KB、Python 3.14 实测可用、登录+查询 <1 s。
3. 风险可控：lazy import + 独立 adapter + 缓存 + 明确错误路径；主路径不依赖它。
4. 引入动作必须单独 PR（S5），由用户批准；本 PR 不改 `requirements.txt`。

### 5.4 如果拒绝引入：替代方案与缺口

| 需求 | 替代 | 缺口 |
|---|---|---|
| 历史 PE/PB/PS | 腾讯每日快照 **从今往后** 落盘 | 没有历史，无法回测；至少等 6–12 个月 |
| 指数估值 | 中证 §12.3 文件 | 只有指数级 PE/股息率，无个股、无 PB |
| 筹码换手率 | `volume / 流通股本`（腾讯流通市值 ÷ 现价） | 送转/增发/解禁导致股本漂移；只能标记为近似 |
| 历史 ST | 无 | §6.8 只有当日名单；不能还原「当时是不是 ST」 |
| 停牌历史 | 无 | 无法排除「停牌日成交」 |
| 退市日 | 无 | 无法剔除已退市标的，幸存者偏差 |
| 北交所估值 | 腾讯当日快照 | 无历史；腾讯 K 线不支持北交所 |

**建议**：如果 P1 的第一批只做「指数 regime + 事件 + 日级资金面 + 基金/ETF」，
可以**暂不引入** baostock；一旦要做 P1-1 估值证据或 P1-6 制度完整性，
就必须先批准 S5。

---

## 6. 不做清单

### 6.1 P1 范围内不做（本批）

| 项 | 理由 |
|---|---|
| 前向 EPS / PEG / 分析师一致预期（§2.2） | 接口稳定性与分析师偏差未验证；先把 PE/PB 历史基线做出来，再谈前向估值 |
| 龙虎榜 / 北向 / 大宗交易 / 板块资金流（§3.2/§3.5/§3.8/§3.9） | 与 20 日持有期尺度不匹配；东财风控成本高；先做日级个股资金流 |
| 解禁日历（§3.6） | 本任务探针清单未覆盖；且与 §14 事件重复度高，下一批再评估 |
| 新闻 / 快讯 / 研报全文 / 互动易（§5.x/§2.1/§10.x） | 需 NLP、噪声大、不可离线；结构化事件优先 |
| 打板四池 / 炸板率 / 热榜（§8.x/§10.2） | 短线情绪，20 日尺度错配 |
| 期权 / 可转债 / 期货 / 商品（§9.1/§15.1/§13.x） | 当前无持仓、无账户态；映射链太长 |
| iwencai 语义搜索（§2.3） | 需 API Key、输出非确定性、无法离线测试 |
| 新股申购日历（§14.6） | 与持仓建议无关 |
| mootdx 行情（§1.7） | 上游自述 2026-09 起返回空；不引入 |
| 北交所全链路 | 腾讯 K 线不支持、baostock 不支持；当前无北交所持仓 |
| 分钟级资金流用于 20 日证据 | 只有当日 100 条，无法回溯；只做实时展示 |
| 用当前沪深 300 成分冒充历史成分 | 幸存者偏差；所有横截面结果必须标注 current-constituent |
| 用筹码绝对值当真实持仓成本 | §4.6 是推演；只看相对变化 |
| 用「一次 HTTP 200」证明数据可用 | 本次已见 9201/9501/404；必须校验 `success`/`result` |

### 6.2 明确需要用户决策（本文不替用户拍板）

1. 是否批准 **baostock** 作为研究/预计算可选依赖（S5）。
2. 是否允许 P1-3 指数 regime 从「只展示」升级为「替换现有 regime」（S8，行为变更）。
3. 是否允许把当前沪深 300 成分用于历史横截面（必须接受幸存者偏差声明）。
4. 是否为 P1 的 `evidence.*` 新增字段 bump `schema_version`（当前 1.0；R1 建议只增不改）。
5. 深交所 ETF 份额从哪天开始按日留存（越早越好；历史不可补）。

---

## 7. 未验证 / 待补证据

| # | 项 | 状态 | 补证方式 |
|---|---|---|---|
| U1 | 腾讯 K 线在 9:15 盘前的语义（昨收/集合竞价） | **未验证** | 交易日 9:10–9:25 实测 `price/last_close/is_stale` |
| U2 | 东财 push2 在批量/长时间运行下的封禁阈值 | **未验证** | 串行 ≥1 s 压测 1 小时，记录 403/429/空响应 |
| U3 | ST 名单 §6.8 的当日准确性 | **未验证**（P0-3 已接入，本探针未重复） | 连续 5 个交易日与交易所公告对账 |
| U4 | 估值/资金面因子在 20 日尺度有增量 IC | **未验证** | 按 §3 方法跑大池 IC；本文只给设计 |
| U5 | 指数 regime 优于个股 regime | **未验证** | 同决策日 A/B；比较校准/回撤/`regime_win_rates` |
| U6 | 事件 warning 能降低回撤 | **未验证** | 事件窗口分层回放；比较 max DD / 命中率 |
| U7 | 深交所 ETF 份额历史 | **不可得** | 只能从今天起按日留存 |
| U8 | 已退市标的的复权/除权 | **源侧缺失** | 上游明确三源全缺；不做无根据的补值 |
| U9 | 生产盘前 10 只票在新增端点后的总墙钟 | **未验证** | 记录端到端耗时；必要时收盘后预计算 |
| U10 | Hermes 是否按 `action` 而非关键词消费 | **未验证**（`PROGRESS.md:377-380`） | 用新 JSON 跑一轮并回帖原文 |
| U11 | 本次探针的延迟在生产网络/交易时段的代表性 | **未验证** | 交易时段重跑 §2.2 命令；本文数字只代表 22:07–22:13 单次 |

> 本文没有跑完整回测、没有改实盘克隆、没有写入项目 venv；
> 所有「可用」仅指**单次取数可用**，不等于「生产稳定」或「有预测力」。

---

## 8. 自检清单（对应 issue #21 交付物）

- [x] 数据可用性表：来源、实测结果、延迟、依赖、失败模式；不可用写「不可用/未验证」。
- [x] 每类数据给原始命令 + 输出片段 + 退出码；东财 9201/9501/404 如实记录。
- [x] 证据方法：as-of 特征、样本池、指标、最小样本、验收阈值、复现命令；明确禁止前视。
- [x] 实施顺序与 PR 边界：先「只展示不改 action」，行为变更单独 PR。
- [x] baostock 决策建议：可用/成本/结论/替代方案/缺口。
- [x] 不做清单与理由。
- [x] 本文只新增 `docs/P1_EVIDENCE_PLAN.md`；未改 `source/`、`requirements.txt`、`README.md`、`vendor/`、测试。

---

## 9. 参考

### 9.1 上游（固定 commit）

- `vendor/a-stock-data/SKILL.md` §1.1（腾讯快照）、§1.2（腾讯 K 线）、§1.6（新浪复权因子）、
  §3.4（分钟资金流）、§4.1/§4.3/§4.5/§4.6/§4.7（资金面/筹码/ETF）、
  §6.5/§6.6（baostock）、§7.1（巨潮）、§12.1–§12.4（指数/日历）、§14.1–§14.5（事件）。
- 上游防封：`SKILL.md:331-398`；估值公式：`SKILL.md:7052-7112`。

### 9.2 本项目

- `docs/ASTOCK_INTEGRATION.md` §4.1 指标字典、§8.2 P1 队列、§9 未验证假设。
- `source/decision/schema.py:154-168`（`Evidence`）、`:67-76`（`Instrument`）、`:84-95`（`Data`）。
- `source/cli.py:581`（evidence 组装）。
- `source/stock_analysis/data_loader/meta.py:820-822`（指数空实现）、`:770-788`（官方日历）。
- `source/stock_analysis/advisor/analyzer.py:74-104`（个股 regime）。
- `source/stock_analysis/data_sources/astock.py:100-178`（日线）、`:184-201`（快照）、
  `:255-339`（日历）。
- `tools/ic_report.py:38-129`、`tools/forward_log.py:38-149`、
  `tools/check_source_parity.py:54-146`、`tools/collect_evidence.py:115-137`。
- `README.md:369-395`（evidence 契约）、`:125-134`（执行口径）、`:747-780`（Python 支持）。
- `PROGRESS.md:375-388`（V6 横截面动量未复现）、`:377-380`（Hermes 消费风险）。

---

## 10. 变更记录

| 日期 | 版本 | 变更 | 作者 |
|---|---|---|---|
| 2026-11-12 | v1.0 | 初稿：6 类数据探针、P1 证据方法、PR 边界、baostock 决策、不做清单 | TF-Leo（Orca worker） |
