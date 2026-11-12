# a-stock-data 能力 → 决策映射与集成路线图（R1 / issue #2）

> 状态：**研究 / 设计文档，不含代码改动**。本文只回答「上游能提供什么、对本项目哪个决策有用、缺口在哪、按什么顺序接」；不替用户做产品取舍，取舍项集中在 §6 与 §8.4。
>
> 本文所有「上游实测」均指 a-stock-data 作者在其 SKILL.md 中记录的实测，**不是本项目实测**；所有「本项目事实」均给 `文件:行`；没有实测数据的地方写「待测」或「未验证」，不写「应该没问题」。

## 0. 上游版本与取证（先固定，再讨论）

| 项 | 值 | 取证方式 / 备注 |
|---|---|---|
| 上游项目 | `simonlin1212/a-stock-data` | issue #1 指定 |
| 版本 | **v3.10.4**（tag） | issue #1、#2 指定；禁止浮动 `main` |
| commit | **`f21534946c42be3ad048ac938eecb1bad84cb8ab`** | GitHub API 校验：committer date `2026-11-03T23:50:57Z` |
| 抓取时间 | **2026-11-12T11:01:20Z** | `date -u` |
| SKILL.md | **7702 行 / 442,341 字节** | `wc -l -c /tmp/astock_SKILL.md` |
| SKILL.md sha256 | **`09f7e8a3f826f747d293a1e86883003dbda932cf7fdd9e61714077ba057bf0f2`** | `sha256sum` |
| LICENSE | **Apache-2.0**（190 行） | `curl .../LICENSE`；sha256 `199bab40163ad21135c2c4d5db43bffdadb14ed80762b048f04931cc48700bbf` |
| 上游自述规模 | **15 层 / 87 个能力端点（82 主 + 5 备胎）/ 34 个来源** | SKILL.md 第 14 行；本文按 § 归并，一个 § 可含多个函数 |
| 本项目基线 | **`e11d42e`**（origin/main） | `git rev-parse --short HEAD`，2026-11-12 |

**版本引用规则**：本文所有 § 编号均以 `f215349` 这一版为准。上游在 V3.10.0 明确重排过 Layer 1 编号（SKILL.md:1061-1063：旧 1.2→新 1.1、旧 1.5→新 1.2、旧 1.6→新 1.3……），因此**任何按 § 号做自动提取或文档映射的方案都必须绑定 commit**，升级时必须重新对照，不能假设 § 号稳定。

---

## 1. 结论先行（一页）

1. **本项目当前的数据层只有一个真实来源族：akshare 封装的东财 / 新浪**（`source/stock_analysis/data_loader/bars.py:203-213`）。上游最有价值的第一项不是「更多因子」，而是**不封 IP 的腾讯行情 / K 线 / 官方交易日历**，以及**可观测的逐通道降级**。
2. **决策层是纯量价**：买点条件期望 + 风控出场（`source/decision/actions.py:37-61`、`source/cli.py:60`）。估值、资金面、事件、宏观目前**没有任何输入**；regime 用的是**个股自身**收盘价对 MA20/MA60，而不是指数环境（`source/stock_analysis/advisor/analyzer.py:74-104`），且指数取数函数是空实现（`source/stock_analysis/data_loader/meta.py:429-431`）。
3. **先做「不接错」再做「接更多」**：P0 只放四件——腾讯 K 线备用通道、官方交易日历、真实交易状态（ST / 停牌 / 涨跌停）、逐通道失败可见。估值/资金面/事件一律先做**上下文**，不得直接改 `action`；是否升级为决策输入，由样本外 IC / 命中率 / 校准证据决定（现有工具 `tools/ic_report.py`、`tools/forward_log.py` 可复用）。
4. **已有反例必须尊重**：V6 复验显示 14 只票的横截面动量在 297 只沪深 300 上**未复现**（20 日 IC `-0.0239`，t=`-0.93`；`PROGRESS.md:375`、`PROGRESS.md:383-388`）。因此本文不提议任何「新因子直接进决策」的路线，只提议数据接入 + 可测假设。

---

## 2. 能力清单（87 端点按决策相关性分组）

> 口径：按**主要决策用途**归组；一个端点可服务多组时在组内注明交叉引用，不重复计数。函数名取自上游「端点路由速查」（SKILL.md:242-329）。「相关度」是本文判断，不是上游声明。

### 2.1 数据健壮性（行情 / K 线 / 日历 / 标的状态）

| 上游 § | 函数 / 能力 | 拿什么 | 相关度 | 备注（上游口径） |
|---|---|---|---|---|
| §1.1 | `tencent_quote` | 实时价 / PE / PB / 市值 / 换手 / 涨跌停 / 指数 / ETF，带 `is_stale` 僵尸报价标志 | **高** | 不封 IP；44=流通市值、45=总市值、46=PB、43=振幅（SKILL.md:1161-1194） |
| §1.2 | `tencent_kline` | 日 / 周 / 月前复权+后复权+不复权；1~60 分钟 | **高** | 三入口轮换，单入口约 600 次后返回空 JSON；**不支持北交所**；前复权是等差口径，高分红利老股可能 ≤0（§1.2） |
| §1.3 | `tdx_daily_package` | 某交易日沪深北全市场日线（含成交额） | 中 | 通达信官网盘后包；2021-01-04 已 404，未逐日验证（§1.3） |
| §1.4 | `tencent_ticks` | 最近一个交易日分笔（价/手/元/主动买卖） | 低 | 只有当日；无独立备胎（§1.4、备用源速查） |
| §1.5 | `baidu_kline_with_ma` | 日 K 带 MA5/10/20 | 低 | 与 §1.2 重叠，仅作 MA 参考 |
| §1.6 | `sina_adjust_factor` / `apply_adjust` | qfq/hfq 复权因子 + 套用到不复权 K 线 | **中高** | 基金复权修复于 V3.10.4；个股长区间建议 `adjust=''` + 本因子（§1.2、§1.6） |
| §1.7 | `tdx_client().bars/quotes/transaction` | K 线 / 五档 / 逐笔（TCP） | **低（留档）** | 2026-09 起行情命令普遍返回空（#52）；财务 / F10 正常（§1.7） |
| §6.6 | `baostock_stock_basic` | 上市日 / 退市日 / 状态 | 中 | 唯一零鉴权退市日来源；不支持北交所（§6.6） |
| §6.8 | `st_stock_list` | 沪深京 ST / *ST 当日名单 | **高** | 东财，baostock 兜底（§6.8） |
| §12.4 | `trading_calendar` | 深交所整月官方交易日历 | **高** | 不靠工作日推断；未发布月份抛错，不伪装休市（§12.4） |
| 备胎 | `bse_quote_backup` | 北交所当前快照 | 低 | 仅北交所持仓时需要 |

### 2.2 估值（价格之外的「贵不贵」）

| 上游 § | 函数 / 能力 | 拿什么 | 相关度 | 备注 |
|---|---|---|---|---|
| §1.1 | `tencent_quote`（估值字段） | 当日 PE(TTM)/PE(静)/PB/总市值/流通市值 | **高** | 与 §2.1 同一端点，交叉引用 |
| §2.1 | `eastmoney_reports` / `download_pdf` / `eastmoney_industry_reports` | 个股/行业研报、评级、三年 EPS、PDF | 中 | 东财，需限流（§2.1） |
| §2.2 | `ths_eps_forecast` | 机构一致预期 EPS | **中高** | 预测机构数 < 3 要谨慎（§2.2） |
| §2.3 | `iwencai_search` / `iwencai_query` | NL 语义搜研报 / 选股 | 低 | **唯一需要 API Key**；输出非确定性（§2.3、Prerequisites） |
| §2.4 | `sina_research_reports` | 研报列表（标题/类型/机构/研究员，无评级） | 低 | 研报第二来源；空页有速率陷阱（§2.4） |
| §6.3 | `eastmoney_stock_info` | 行业 / 股本 / 市值 / 上市日期 | 中 | 东财（§6.3） |
| §6.5 | `baostock_valuation_history` | 日频 PE/PB/PS/PCF + 换手率 + 停牌 + ST | **高** | 回溯 2016；**不支持北交所**；换手率是 §4.6 必需输入（§6.5） |
| §12.3 | `index_valuation` | 中证指数两种口径 PE + 股息率 | 中 | 不含 PB；仅中证公开文件（§12.3） |
| 本地公式 | `forward_pe` / `pe_digestion` / `calc_peg` / `full_valuation` | 前向 PE / PE 消化年数 / PEG / 单票估值全景 | 中 | 不是数据端点，是上游的计算约定（SKILL.md「估值计算公式」） |

### 2.3 资金面 / 筹码（谁在买、筹码在哪）

| 上游 § | 函数 / 能力 | 拿什么 | 相关度 | 备注 |
|---|---|---|---|---|
| §3.2 | `hsgt_realtime` | 北向分钟流向 | 中 | 上游注明 hgt 可用 / sgt 仅参考；本地自缓存历史（§3.2） |
| §3.3 | `eastmoney_concept_blocks` | 个股所属板块/概念 | 中 | 东财（§3.3） |
| §3.4 | `eastmoney_fund_flow_minute` | 个股分钟级资金流 | 中 | 东财，分钟级（§3.4） |
| §3.5 | `dragon_tiger_board` | 个股龙虎榜 + 买卖席位 TOP5 | 低 | 东财；与 20 日建议尺度不匹配（§3.5） |
| §3.6 | `lockup_expiry` | 解禁历史 + 未来 90 天待解禁 | **中高** | 供给侧事件，适合做风险提示（§3.6） |
| §3.8 | `board_fund_flow` | 板块资金流（行业/概念/地域 × 今日/5/10 日） | 中 | 东财（§3.8） |
| §3.9 | `daily_dragon_tiger` | 全市场龙虎榜 + 净买额排名 | 低 | 东财（§3.9） |
| §4.1 | `margin_trading` | 融资融券明细 | 中 | 东财；有交易所官方备胎（§4.1、备用源速查） |
| §4.2 | `block_trade` | 大宗交易 + 营业部 | 中 | 东财（§4.2） |
| §4.3 | `holder_num_change` | 股东户数变化 | 中 | 户数持续减少 = 筹码集中（§4.3） |
| §4.4 | `dividend_history` | 分红送转历史 | 中 | 与复权/估值口径相关（§4.4） |
| §4.5 | `stock_fund_flow_120d` | 个股日级资金流（120 日） | **中高** | 东财；可与价格因子做正交检验（§4.5） |
| §4.6 | `chip_distribution` | 获利比例 / 平均成本 / 90-70 成本区间 / 筹码峰 | **中高** | 本地计算；依赖 §6.5 换手率（§4.6） |
| 备胎 | `margin_trading_backup` / `fund_flow_backup` / `dragon_tiger_backup` | 两融 / 资金流 / 龙虎榜官方或新浪备胎 | 中 | 主源被封时降级（备用源速查） |

### 2.4 情绪 / 事件（为什么今天动、有没有雷）

| 上游 § | 函数 / 能力 | 拿什么 | 相关度 | 备注 |
|---|---|---|---|---|
| §3.1 | `ths_hot_reason` | 当日强势股 + 题材归因 reason tags | 低 | 同花顺独家；短周期情绪（§3.1） |
| §5.1 | `eastmoney_stock_news` | 个股新闻 | 低 | 东财；全文需 NLP |
| §5.2 | `cls_telegraph` | 财联社 7×24 电报（本地签名零 key） | 低 | 快讯（§5.2） |
| §5.3 | `eastmoney_global_news` | 全球 7×24 资讯 | 低 | 东财（§5.3） |
| §5.4 | `wallstreetcn_lives` | 华尔街见闻 7×24 快讯 | 低 | 可翻页（§5.4） |
| §5.5 | `cctv_news` | 新闻联播条目 + 文字稿 | 低 | 政策信号研究（§5.5） |
| §7.1 | `cninfo_announcements` | 公告检索 + PDF | **中** | 巨潮；事件事实源（§7.1） |
| §7.2 | `tdx F10(最新提示)` | 公告摘要 | 低 | 2026-09 起只剩这一类（§7.2） |
| §8.1 | `em_zt_pool/em_zb_pool/em_dt_pool/em_yzt_pool` | 涨停/炸板/跌停/昨涨停四池 | 低 | 东财；打板尺度（§8.1） |
| §8.2 | `ths_limit_up_pool` | 涨停原因题材 + 封板成功率 + 板型 | 低 | 同花顺（§8.2） |
| §8.3 | `limit_up_sentiment` | 炸板率 / 连板高度 / 连板梯队 | 低 | 四池组合（§8.3） |
| §8.4 | `em_stock_monitor` | 重点监控池（风险警示 + 生效时间窗） | **中高** | 风险名单，适合做硬告警（§8.4） |
| §8.5 | `em_price_anomaly` / `em_price_anomaly_count` | 日内异动明细 / 按标的聚合 | 中 | 严重异常波动（§8.5） |
| §9.1 | `sina_option_codes/tquote/greeks` | ETF 期权 T 型报价 + 希腊字母 + IV | **低（不做）** | 当前无期权持仓，见 §6 |
| §10.1 | `cninfo_irm` | 互动易问答（深市） | 低 | 文本（§10.1） |
| §10.2 | `ths_hot_list` / `em_hot_rank` / `em_hot_concept` | 热榜 / 人气榜 / 概念命中 | 低 | 噪声大（§10.2） |
| §10.3 | `sse_e_interaction` | 上证 e 互动（沪市） | 低 | 文本（§10.3） |
| §14.1 | `earnings_forecast` | 业绩预告 | **中高** | 事件黑名单候选（§14.1） |
| §14.2 | `institution_survey` | 机构调研（汇总/逐机构） | 中 | 事件（§14.2） |
| §14.3 | `holder_trades` | 股东增减持 | **中高** | 事件（§14.3） |
| §14.4 | `share_buyback` | 回购方案与进度 | 中 | 事件（§14.4） |
| §14.5 | `equity_pledge` | 股权质押比例（中国结算周度，仅沪深） | **中高** | 风险事件（§14.5） |
| §14.6 | `ipo_calendar` | 新股申购日历 | 低 | 与持仓无关（§14.6） |
| §15.1 | `convertible_bonds` | 可转债条款 + 转股价值/溢价率 | 低（不做） | 仅持仓含转债时需要（§15.1） |
| 备胎 | `announcements_backup` | 公告官方/东财备胎 | 中 | 备用源速查 |

### 2.5 市场环境（大盘 / 行业 / 宏观 / 期货）

| 上游 § | 函数 / 能力 | 拿什么 | 相关度 | 备注 |
|---|---|---|---|---|
| §1.1 | `tencent_quote`（指数） | 上证/沪深 300/创业板等指数报价 | 中 | 与 §2.1 交叉引用 |
| §3.7 | `industry_comparison` | 行业板块涨跌排名 | 中 | 东财（§3.7） |
| §6.7 | `sw_industry_history` / `sw_industry_as_of` | 申万行业**变迁史**（消除前视偏差） | 中 | 仅代码无中文名（§6.7） |
| §11.1 | `pboc_social_financing` | 社融增量（月度 12 列） | 低 | 月度，20 日尺度弱（§11.1） |
| §11.2 | `nbs_pmi` | 制造业/非制造业/综合 PMI + 大中小型 | 低 | 月度（§11.2） |
| §11.3 | `chinabond_yield_curve` | 国债 / 银行 AAA / 中短票 AAA 收益率曲线 | 低 | 利率环境（§11.3） |
| §11.4 | `repo_fixing_rates` | 回购定盘利率 FR/FDR | 低 | 资金面（§11.4） |
| §11.5 | `lpr_history` | LPR 1 年 / 5 年全历史 | 低 | 东财（§11.5） |
| §11.6 | `macro_calendar` | 全球宏观日历 | 低 | 事件日历（§11.6） |
| §12.1 | `index_constituents` | 指数成分（中证/国证） | 中 | 当前快照，不提供历史时点成分（§12.1） |
| §12.2 | `index_weights` | 指数权重 | 中 | 月末/日度快照（§12.2） |
| §12.3 | `index_valuation` | 指数 PE / 股息率 | 中 | 与 §2.2 交叉引用 |
| §13.1 | `futures_daily` | 五家期货交易所日行情 | 低 | 不持仓（§13.1） |
| §13.2 | `options_daily` | 商品/股指期权日行情 + Delta/IV | 低 | 不持仓（§13.2） |
| §13.3 | `futures_position_rank` | 会员成交/持买/持卖前 20 | 低 | 不持仓（§13.3） |
| §13.4 | `futures_realtime` | 实时期货（含大商所） | 低 | 宏观代理（§13.4） |
| §13.5 | `a50_futures` | 富时中国 A50 期指 | 低 | 隔夜情绪代理（§13.5） |
| §13.6 | `sge_spot` | 上海金交所现货日线 | 低 | 不持仓（§13.6） |
| §13.7 | `futures_kline` | 期货日 K（含大商所） | 低 | 不持仓（§13.7） |

### 2.6 基金 / ETF

| 上游 § | 函数 / 能力 | 拿什么 | 相关度 | 备注 |
|---|---|---|---|---|
| §1.1 | `tencent_quote`（ETF） | ETF 实时报价 / 市值 / 换手 | 中 | 交叉引用（§1.1） |
| §1.2 | `tencent_kline`（ETF） | ETF 日/周/月/分钟 K 线 | 中 | 交叉引用（§1.2） |
| §1.6 | `sina_adjust_factor`（基金） | 基金 qfq/hfq 因子（`s`/`u`） | **中高** | V3.10.4 修复基金复权（§1.6） |
| §4.7 | `etf_shares` | ETF 份额（上交所历史日期 / 深交所当前快照） | **中高** | 申赎/规模信号（§4.7） |
| §12.1 | `index_constituents` | 指数成分（基金基准/轮动） | 中 | 交叉引用（§12.1） |
| §12.2 | `index_weights` | 指数权重 | 中 | 交叉引用（§12.2） |
| §12.3 | `index_valuation` | 指数估值 | 中 | 交叉引用（§12.3） |

### 2.7 交易制度 / 可执行性

| 上游 § | 函数 / 能力 | 拿什么 | 相关度 | 备注 |
|---|---|---|---|---|
| §12.4 | `trading_calendar` | 官方交易日历 | **高** | 与 §2.1 交叉引用 |
| §1.1 | `tencent_quote`（涨跌停价） | 当日涨停价 / 跌停价 | **高** | `limit_up`/`limit_down` 字段（§1.1） |
| §6.8 | `st_stock_list` | ST / *ST 名单 | **高** | 与 §2.1 交叉引用 |
| §6.6 | `baostock_stock_basic` | 上市日 / 退市日 / 状态 | 中 | 与 §2.1 交叉引用 |
| §6.5 | `baostock_valuation_history`（状态列） | `tradestatus` 停牌 / `isST` 历史 | **高** | 历史回测防「停牌日成交」（§6.5） |
| §1.2 | 成交量单位规则 | 科创板是股、其余是手 | **中高** | 单位错会放大 100 倍（§1.2、V3.10.1） |
| §8.4/§8.5 | 监控池 / 异动池 | 风险警示 + 严重异常波动 | 中 | 与 §2.4 交叉引用 |

### 2.8 覆盖检查

- 上述 § 行覆盖了「端点路由速查」中的全部主端点 §（§1.1–§15.1）与 5 个备胎函数；§3.10「信号层组合用法」、§13.1–13.7 的「自包含实现」、§估值公式、§完整调研流程属于**用法/计算约定**，不是数据端点，已按用途并入对应组。
- 一个 § 含多个函数的（§2.1 两行、§8.1 四池、§8.5 两函数、§9.1 三函数、§10.2 三函数、§6.7 两函数、§13.x 多文件）在表中已用 `/` 列出，故表的行数少于上游的 87 个端点计数，这是归并口径，不是遗漏。
- 「相关度」只表达**与当前 20 日单标的决策链**的关系；不代表数据质量或长期价值。

---

## 3. 决策映射（能力 → 本项目哪一层）

### 3.1 当前决策链（真源）

```
resolve_and_fetch(code)                      source/stock_analysis/data_loader/meta.py:434
  ├─ get_stock_daily()  渠道池三通道          meta.py:153 / bars.py:203-213
  │    eastmoney → sina_stock → sina_fund     bars.py:203-213
  │    冷却 / 连续失败剔除                     bars.py:105-124
  ├─ get_fund_daily()   ETF → OTC 两通道      meta.py:242-270
  └─ 缓存新鲜度（只跳周末）                    meta.py:398-406
        ↓
instrument.classify_board(code)               source/decision/instrument.py:5-37
        ↓
market_regime(df)                            source/cli.py:166-190
  └─ analyzer.get_current_market_regime(df)   source/stock_analysis/advisor/analyzer.py:94-104
        （个股自身 close vs MA20/MA60）         analyzer.py:74-91
        ↓
run_strategy_tournament / bayes               source/cli.py:266-292
        ↓
signals / triggers / state machine            source/cli.py:355-386
  ├─ side_expectancy(horizon=20)              source/decision/expectancy.py:39-75
  ├─ trailing_expectancy_ok                   source/decision/expectancy.py:78-121
  ├─ run_policy（买点+风控出场）               source/decision/policy.py:46-179
  ├─ signals.build（仓位/状态/指标值）         source/decision/signals.py:111
  └─ evaluate_triggers                        source/decision/triggers.py:8
        ↓
scoring / consensus / confidence              source/decision/scoring.py:52-365
        ↓
actions.decide（唯一决策表）                  source/decision/actions.py:37-61
        ↓
sizing（止损/止盈/入场区/仓位/horizon）        source/decision/sizing.py:19-129
        ↓
schema 输出 / 错误路径同构                     source/decision/schema.py:9, 263-377
        ↓
CLI JSON                                      source/cli.py:492-600
```

### 3.2 逐能力映射

| 上游 § / 能力 | 本项目落点（层） | 当前现状 | 缺口（文件:行） | 可改变的决策 |
|---|---|---|---|---|
| §1.1 腾讯实时报价 | data_loader / instrument | 只取日线；无实时报价；`instrument` 只有代码前缀分类 | `meta.py` 无 quote 入口；`instrument.py:23-37` `is_st` 恒 `False`；`schema.py:67-73` 无 PE/PB/涨跌停字段 | 数据是否新鲜、是否可交易、ST 涨跌停幅度 |
| §1.2 腾讯 K 线 | data_loader（渠道池） | 三通道全是 akshare 封装（东财/新浪） | `bars.py:203-213`；东财一封 IP 只剩新浪，再失败即 `data_source_error` | `data_source_error` 率、冷启动可用性 |
| §1.3 通达信盘后包 | data_loader | 无 | 无入口；只在 `get_stock_daily` 单票取数 | 全市场横截面研究（不是盘前建议必需） |
| §1.4 腾讯逐笔 | data_loader | 无 | — | 日内执行/滑点估计（当前不消费） |
| §1.5 百度 K 线 | data_loader | 无 | — | 备胎（低） |
| §1.6 新浪复权因子 | data_loader / 回测口径 | 直接用 akshare qfq；无因子层 | `bars.py:168-200`；基金 `_fetch_from_sina_fund` 忽略 `adjust`（`bars.py:190`） | 长区间复权正确性、基金复权 |
| §1.7 mootdx | data_loader（不建议） | 无 | — | 上游自身行情命令已失效（§1.7），不作为新增依赖理由 |
| §6.6 上市/退市日 | instrument / data_loader | 无退市日；`classify_board` 只看 6 位前缀 | `instrument.py:5-37`；`meta.py` 无 basic 入口 | 回测是否把已退市标的当活票、幸存者偏差 |
| §6.8 ST 名单 | instrument / 错误路径 | `is_st` 恒 `False` | `instrument.py:36-37`；`create_error_response` 也只能拿到静态值（`schema.py:263+`） | ST 5% 涨跌停、`untradeable`、风险提示 |
| §12.4 交易日历 | data_loader（缓存新鲜度）/ 盘前调度 | 只跳周末；不含节假日；`_trading_days_between` 注释自认「下界估计」 | `meta.py:398-406`、`meta.py:90-103`；Hermes 脚本另用 akshare（`~/.hermes/scripts/trade_preopen_alert.py:39-48`） | 是否无谓触网、`lag_days` 是否准确、盘前是否该跑 |
| §1.1 / §6.5 估值 | （无估值层） | 无 PE/PB/EPS 输入；`scoring` 只吃价格与回测统计（`scoring.py:52-365`） | `README.md:315-339` 的 recommendation 字段无估值项；`schema.py:105-120` 无估值字段 | 是否买入的**上下文**（不能直接改 `action`，见 §8.2） |
| §2.2 一致预期 EPS | （无） | 无 | — | 前向估值 / PE 消化（上下文） |
| §2.1 / §2.4 研报 | （无） | 无 | — | 事件/预期修正（弱，需 NLP） |
| §12.3 指数估值 | regime / 估值 | 无 | `meta.py:429-431` 指数取数是 `return None` | 市场环境（指数贵不贵） |
| §3.x / §4.x 资金面 | （无） | 无；`triggers` 只接收策略信号（`triggers.py:8-85`），`scoring` 只吃价格序列 | `signals.py:111` 的入参只有 df/buy/sell；`sizing.py:19-29` 全是 ATR/百分比常量 | 买点确认、仓位、风控（都需先证明 IC） |
| §4.6 筹码分布 | （无） | 无换手率序列；`data_loader` 不保留 `turnover` 的日频历史（`bars.py:147-165` 仅规范化列） | 需 §6.5 换手率 | 获利盘/成本区作为止损参考（上下文） |
| §5.x / §7.x / §8.x / §10.x / §14.x 事件 | （无） | 无事件日历、无公告黑名单 | `render.py` 只生成交易文案（`render.py:51+`） | 风险提示、`untradeable`/观望理由 |
| §11.x 宏观 | regime（候选） | 无；regime 只用个股均线 | `analyzer.py:74-104` | 大盘环境（P2） |
| §12.1/12.2 指数成分/权重 | regime / 横截面研究 | 无；`get_market_index_daily` 空实现 | `meta.py:429-431` | 基准、行业中性、横截面 IC（研究） |
| §13.x 期货/商品 | （无） | 无 | — | 宏观代理（P2/不做） |
| §6.7 申万行业变迁 | （无） | 无行业字段 | `meta.py` 无行业入口 | 行业中性/前视偏差（研究） |
| §4.7 ETF 份额 | 基金 data_loader | `get_fund_daily` 只有净值/ETF 收盘，无份额 | `meta.py:242-369` | 基金申赎/规模（上下文） |

### 3.3 缺口汇总（按层）

| 层 | 现状 | 结构性缺口 | 优先级 |
|---|---|---|---|
| data_loader | 3 个股票通道 + 2 个基金通道；缓存/降级可见（G33） | **无腾讯 / 无官方日历 / 无逐通道错误明细 / 无实时报价** | **P0** |
| instrument | 代码前缀 → 板块 + 固定涨跌停 + `is_st=False` | **无真实 ST / 停牌 / 涨跌停价 / 退市状态** | **P0** |
| regime | 个股自身 MA20/MA60 + 波动 | **不是市场环境**；指数取数为空实现 | P1 |
| triggers | 只吃策略买卖序列 + 信号状态机 | 无外部确认（资金/事件/估值） | P1（先上下文） |
| scoring | 价格回测统计、bootstrap、DSR | 无正交因子；无法表达「外部证据」 | P1（先研究） |
| sizing | ATR / 固定百分比；仓位上限 30% | 无估值锚、无波动率目标 | P2 |
| schema | v1.0；错误路径同构（G35） | 新增任何字段都需 README 契约同步 + 兼容策略 | **每次改动的前置** |
| 错误路径 | `data_source_error` / `insufficient_data` / `untradeable` 可达（`test_fix_regressions.py:46-56`） | 失败原因只在 stderr + 聚合 warning；逐通道不可见 | **P0** |

---

## 4. 价值假设与验证设计

### 4.1 指标字典（只允许这七类，定义可复算）

| 指标 | 操作定义 | 现有工具 / 字段 |
|---|---|---|
| `data_source_error` 率 | 对固定票池 × 固定日期，`recommendation.status == 'data_source_error'`（退出码 30）的次数 ÷ 总请求数 | CLI 退出码；`tools/collect_evidence.py` |
| 样本外 IC | 因子值或建议分数与未来 20 日收益的秩相关；分重叠/不重叠两种口径，报 IC、t、n | `tools/ic_report.py`（`--min-bars`、`--horizons`） |
| 扣费净收益 | 按 `data.execution` 口径（`fill=next_open`、slippage 10bps、commission 3bps）回放的累计/年化收益 | `README.md:125-131`、`README.md:352-356`；`decision/policy.py:46-179` |
| 最大回撤 | 同上权益曲线的 peak-to-trough | `policy.py:156-170`、`scoring.py:122-130` |
| 触发命中率 | 买点后 20 日上涨占比；卖点误报率 | `evidence.signal_stats`（`evaluate.py:69+`；README.md:340-351） |
| 置信度校准 | 按 `recommendation.confidence` 分箱（如 5 箱），比较箱内实际 20 日胜率/均值与置信度 | 前向台账 `tools/forward_log.py`（record/evaluate） |
| 建议可执行率 | `status=ok` 且 `entry_zone/stop_loss/take_profit` 非空、一手可负担、非停牌/非一字板 的占比 | CLI 输出 + 新增交易状态字段（P0） |

> 禁止用「信号看起来更专业」「数据更多」作为价值证据；也禁止把上游作者的实测数字当作本项目的收益预期。

### 4.2 逐能力价值假设

| 能力 | 改善哪个决策 | 可测指标 | 测法（可复现） | 当前证据 |
|---|---|---|---|---|
| §1.2 腾讯 K 线备用通道 | 取数成功率、冷启动可用性 | `data_source_error` 率；`data.source` 分布 | 模拟东财+新浪失败（monkeypatch），对 10 只实仓 + 10 只对照跑 CLI；记录退出码与 `data.source` | **待测**；上游称腾讯 K 线不封 IP、单入口约 600 次后限流（§1.2） |
| §12.4 官方交易日历 | 是否无谓触网、`lag_days` 是否准确、盘前是否该跑 | `data.stale` 比例、`diagnostics.warnings` 中降级次数、盘前脚本空跑率 | 以春节/国庆/调休日期为 `as_of`，对比现实现（只跳周末）与官方日历的 `expected_date`/`lag_days` | **待测**；现实现的「只跳周末」在 `meta.py:398-406` 可复核 |
| §1.1 实时报价 + `is_stale` | 是否在停牌/废码/僵尸报价上给建议 | 建议可执行率；`untradeable` 比例；误报数 | 构造停牌、北交所老码、一字板样本；断言 `is_stale`/`untradeable`/warning | **待测**；上游记录了北交所老码僵尸报价案例（§1.1） |
| §6.8 + §6.6 + §6.5 状态列 | ST / 停牌 / 退市标的的建议正确性 | 建议可执行率；回测最大回撤（停牌日不可成交） | 用历史 ST/isST/tradestatus fixture 回放；对照现行 `is_st=False` | **待测**；现 `instrument.py:36-37` 恒 False，可复核 |
| 逐通道失败可见 | 「先报问题再报建议」能否做到 | 报错定位时间；`diagnostics.warnings` 完整率 | 三通道分别抛不同异常，检查 warnings 是否含来源+原文；Hermes 消费测试 | **待测**；现 `bars.py:139-144` 只保留最后一个 `last_err` |
| §6.5 估值历史 + §1.1 快照 + §2.2 EPS | 买入的估值上下文（不直接改 `action`） | 样本外 IC；置信度校准；触发命中率 | 以 `--as-of` 生成历史截面，计算 PE/PB 分位与未来 20 日收益的 IC；分箱校准 | **待测**；V6 已证明「14 只票的横截面动量不复现」（`PROGRESS.md:375`、`383-386`），因此估值项必须先做大池 IC |
| §3.4/§4.5 资金流 | 买点确认（过滤器或置信度） | 样本外 IC；触发命中率；扣费净收益 | 在买点集合上做「有资金流 vs 无资金流」分层，比较 20 日均值与命中率 | **待测**；无现有证据 |
| §4.6 筹码分布 | 止损/目标位（成本区参考） | 最大回撤；扣费净收益 | 用 §6.5 换手率构造历史筹码，比较 ATR 止损 vs 成本区止损 | **待测**；依赖换手率历史 |
| §3.6/§4.1/§4.3 事件与筹码 | 风险提示（解禁/两融/户数） | 建议可执行率；最大回撤；触发命中率 | 事件窗口（如解禁前 20 日）内建议分层，比较回撤/命中 | **待测** |
| §12.1/12.2/12.3 + §3.7 指数环境 | regime 是否应改为指数口径 | 置信度校准；最大回撤；`regime_win_rates` 的稳定性 | 用同一决策日、指数 as-of 成分计算 regime，与现有个股 regime 对照；比较分市况胜率 | **待测**；现 `analyzer.py:74-104` 用个股自身均线，可复核 |
| §7.1/§14.x 事件黑名单 | 是否应观望/降仓 | 建议可执行率；最大回撤 | 事件窗口内注入 warning，比较「带 warning」与「不带」的回放回撤 | **待测** |
| §4.7 ETF 份额 + §1.6 基金复权 | 场外/场内基金结论的数值正确性 | 跨源一致性；最大回撤；净值序列差异 | 与现有 `tools/check_source_parity.py` 同思路，对基金做复权/份额对照 | **待测**；V3.10.4 修复了基金复权（§1.6） |
| §11.x 宏观 / §13.x 期货 | 大盘环境（P2） | 置信度校准；最大回撤 | 月度/日度宏观因子与 regime 的领先性检验；注意 20 日尺度 | **待测**；预计弱，P2 |

### 4.3 不能声称的内容

- 不能因为上游 §1.1 有 PE/PB，就声称「本项目估值判断更准」——本项目当前没有任何估值字段，也没有 IC 证据。
- 不能把上游作者的实测收益、胜率、IC 当成本项目的结果；上游 SKILL 主要记录**数据接口实测**，不含可复现的策略收益报告。
- 不能因 V6 的 14 只票结果就推广到全市场；V6 已明确「14 只票的横截面动量是选样偏差」（`PROGRESS.md:383-388`）。
- 不能把「数据源更多」等同于「建议更可靠」；新数据只有在**改变决策或降低风险**且**指标可测**时才进入 P1+。

---

## 5. 成本与风险

### 5.1 封禁 / 限流

| 数据源 | 上游口径 | 对本项目的含义 |
|---|---|---|
| 东财 `push2`/`datacenter`/`reportapi` 等 | 每秒 >5 次 / 单 IP 并发 ≥10 / 1 分钟 ≥200 / 5 分钟 ≥300 → 封；实测 2026-06-30 有 1 小时 45000+ 请求 → **IP 级封禁 20+ 小时**（§数据源优先级 & 东财防封） | 现有 akshare 东财通道没有统一节流；批量研究/预热时最危险。新接入东财端点必须走串行 + ≥1s 间隔 + 会话复用（上游 `em_get()`） |
| 腾讯 K 线 | 单入口约 600 次后返回空 JSON；三入口轮换、失败冷却 120s（§1.2）；**连续 5000+ 次后整域返回空**（§1.2 实测案例） | 不是封 IP，但批量回补会撞限流。**issue #13 已实现**跨进程冷却 + 请求预算（见 §5.1.1） |
| 腾讯实时报价 | 不封 IP；但僵尸报价/停牌会返回 200 + 定格数据，需 `is_stale`（§1.1） | 不能把「有返回」当「数据新鲜」 |
| mootdx | TCP 7709；2026-09 起 K 线/盘口/逐笔返回空；海外 IP 通常全部超时（§1.7、FAQ） | **不要为行情引入 mootdx**；财务/F10 才可能用 |
| baostock | TCP 登录；不支持北交所（§6.5、§6.6） | 引入即增加一个 TCP 依赖与登录失败路径 |
| iwencai | 需 `IWENCAI_API_KEY`（Prerequisites） | 不进自动链路；只在人工研究时用 |
| 交易所官方 / 中债 / 货币网等 | 零鉴权、极低风险，但避免高频（备用源速查） | P1/P2 可用，但同样要缓存 |

### 5.1.1 腾讯限流：跨进程冷却与请求预算（issue #13，P0）

R5 抓 300 只日线时真实触发整域限流（`docs/P1_EVIDENCE_V2.md` §2.2/§7）：第 47 只后
三入口同时失败、全轮 22 次冷却、32 只失败、最后靠 baostock 补齐 57 只。根因是上游
`_tencent_host_down_until` 只是**进程内字典**——新进程不继承，且「每票都重试三入口」
把一次限流放大三倍。实现分三层，**均未改动上游生成物**（`_astock_generated.py`
仍是 `tools/astock_extract.py --check` 可复验的快照）：

| 层 | 位置 | 行为 |
|---|---|---|
| 状态镜像 | `data_sources/tencent_throttle.py` | 冷却状态落 `source/stock_analysis/data/state/tencent_cooldown.json`（gitignore），带过期时间；`tmp` + `os.replace` 原子写，`fcntl.flock` 串行化读-改-写（并发进程不会互相覆盖） |
| 入口闸门 | `astock.fetch_daily` / `fetch_index_daily` | 取数前 `sync_process_state()` 把磁盘冷却并入进程内字典；三入口全冷却时抛 `TencentThrottled`，**一个请求都不发**；取数后 `flush_process_state()` 立即落盘 |
| 渠道池预算 | `data_loader/bars.py::_DataSourcePool` | 单票总耗时 ≤ `DATA_SOURCE_TICKER_BUDGET_SECONDS`（默认 20s），超预算停止尝试剩余通道（attempt 带 `budget_exhausted`），调用方回退缓存并 `degraded`；主源连续 3 只失败后**本轮剩余票把备胎排到最前**，新一轮 `reset_primary_block()` 重新判定 |
| 批量预算 | `tools/context_ic.py fetch` | `--batch-size ≤ 20`、`--batch-pause ≥ 20s`（超限夹取并打印）；`--ticker-budget`（默认 20s）；`--primary-block-after`（默认 3）；`manifest.json` 记录 `price_sources` / `price_source_counts` / `price_budget` / `tencent_cooldown` |

**备胎混源约束（第 4 条）**：`price_sources` 逐票记录 `tencent_qfq` / `baostock_qfq`。
两源比例复权口径在**收益率**上等价（实测日收益最大绝对差 3.5e-6），但**价格水平不同**
（复权基准不同）——因此任何跨票的**绝对价位**比较（同价位分组、价格带统计）必须先按
来源分桶，不得混算。`stratify-buypoints --only-source/--exclude-source` 即为此提供。

运维：手动解除冷却 = 删除 `tencent_cooldown.json`（或
`tencent_throttle.clear_state()`）；`DATA_SOURCE_TICKER_BUDGET_SECONDS` 可临时放大预算。

### 5.2 延迟与盘前窗口

- 上游东财 `em_get()` 默认**串行、最小间隔 1s + 抖动**（§数据源优先级）；批量 10 只票 × 多端点会线性放大。
- 下游盘前脚本对每只票的超时是 **180s**（`~/.hermes/scripts/trade_preopen_alert.py:52-53`），10 只票**串行**执行；最坏情况远超 9:15 前的可用窗口。新增任何「每票多拉 N 个端点」的能力，必须先证明总墙钟可控，或改为**盘前只读缓存 / 收盘后预计算**。
- 建议的硬约束（待实现线确认）：新增端点只允许在**缓存刷新任务**里联网；`cli.py` 主路径在缓存新鲜时**不触网**（现有行为见 `meta.py:168-189`）。

### 5.3 依赖与 Python 3.14 兼容性

| 依赖 | 上游要求 | 本项目现状 | 风险 |
|---|---|---|---|
| `mootdx` | ≥0.10；0.11.x 有 BESTIP bug | 未依赖 | **不建议引入**；行情命令已失效（§1.7） |
| `baostock` | ≥0.8 | 未依赖 | 新 TCP 依赖；Python 3.14 是否有 wheel **未验证** |
| `stockstats` | any | 未依赖（项目用自实现指标） | 无必要 |
| `xlrd>=2.0` / `openpyxl` | 上游读取 xls/xlsx | 未依赖 | P1/P2 若接申万/中债/指数文件才需要 |
| `requests` / `pandas` / `numpy` | 上游已用 | 已有（`source/requirements.txt`） | 低 |
| Python 版本 | 上游「保留 Python 3.9」（SKILL.md:49） | 本项目支持 3.11+，3.14 已真机验证（`README.md:718-751`） | 上游代码在 3.14 上**未验证**；`mootdx`/`baostock` 的 cp314 wheel **未验证**。引入前必须在 A5500 的 3.14 环境实跑，不能只看上游声明 |

### 5.4 维护 / 版本漂移

- 上游 § 编号会变：V3.10.0 已重排 Layer 1（SKILL.md:1061-1063）。任何提取脚本/文档映射都必须**绑定 commit + 校验 sha256**。
- 上游是「单一 SKILL.md + 内嵌代码」形态，不是可安装包；函数可能在同一 § 内被改写，升级 diff 需要人工审。
- 本项目当前文档也有一处漂移：`README.md:605` 写「47 passed」，`PROGRESS.md:6` 记「67 passed」。本文不据此下结论；实现线应以**实跑输出**为准，并顺手修正文档（不属于本任务的允许改动面）。
- 门禁是 `pwsh -File tools/check_gates.ps1`（项目 `AGENTS.md` §0）；A5500 是 Linux，实现线需要确认等价执行方式。这不是本文的改动面，但会影响 P0 的「验收测试」落地。

### 5.5 许可与归属（Apache-2.0 → MIT 项目）

- 上游 LICENSE 为 **Apache-2.0**（`/tmp/astock_LICENSE`，190 行）；本项目 LICENSE 为 **MIT**（`LICENSE:1`）。
- Apache-2.0 代码可以进入 MIT 项目，但必须：保留版权与许可声明、附上 Apache-2.0 全文、标注修改过的文件。**本文不是法律意见**；实现线应在 `vendor/a-stock-data/` 放 `LICENSE` + `NOTICE`，并在移植文件的 docstring 里写 `upstream §x.y @ f215349`。
- 不得把上游代码「洗」成本项目原创；也不得在未保留归属的情况下把 SKILL.md 片段直接复制进 `source/`。

### 5.6 数据语义风险（比「能不能取到」更危险）

| 风险 | 上游依据 | 本项目后果 |
|---|---|---|
| 腾讯前复权是**等差**口径，高分红利老股早年可能 ≤0 | §1.2 | 本项目缓存是 akshare qfq；直接把腾讯 qfq 混入同一回测会改变历史收益。**必须先做重叠段 parity，再决定是否允许混源**（现有 `tools/check_source_parity.py` 是同一思路） |
| 成交量单位：科创板是股，其余是手 | §1.2、V3.10.1 | 量比/换手/筹码计算会差 100 倍；`chip_distribution` 尤其敏感 |
| 腾讯 `is_stale` 僵尸报价 | §1.1 | 北交所老码/停牌股会返回「昨天的价格」，直接当今日价会给出错误建议 |
| 北交所覆盖不一致 | §1.2（K 线不支持）、§6.5/§6.6（baostock 不支持） | 北交所标的只能走 §1.3 盘后包/腾讯实时；不能假设全链路可用 |
| 指数成分是**当前快照**，不是历史时点 | §12.1 | 用当前成分做历史 regime/横截面会有幸存者偏差（V6 已自认此局限，`PROGRESS.md:388`） |
| 停牌日/ST 历史标记 | §6.5 | 不剔除停牌日会在回测里「成交」不可成交的 bar；不还原当时 ST 会用错涨跌停 |
| 东财不同子域风控面不同 | §数据源优先级（`datacenter` 不受 `push2` 封禁牵连） | 不能因一个端点 403 就判定「东财全灭」；降级要按端点族 |
| 上游实测 ≠ 本项目实测 | 全文 | 所有收益/IC/命中率结论必须在本项目重跑 |

---

## 6. 明确不做清单（含理由与待决策项）

### 6.1 不做（本文建议，非用户决定）

| 项 | 上游 § | 理由 |
|---|---|---|
| ETF 期权 T 型报价 / 希腊字母 / IV | §9.1 | 本项目当前建议对象是个股与场外基金，**没有期权持仓、没有账户态**（`GOAL_CONTRACT.md:340-357`：不接收账户态）；希腊字母对「买不买这只股票/基金」没有直接增益 |
| 可转债条款 / 转股价值 / 溢价率 | §15.1 | 仅在持仓含可转债时才有意义；当前实仓清单无转债（`~/.hermes/scripts/trade_preopen_alert.py:29-30`） |
| 期货 / 商品 / 上金所现货 | §13.1–§13.7 | 不持仓；作为宏观代理的映射链太长，20 日尺度证据弱；P2 以后再评估 |
| 打板四池 / 连板梯队 / 炸板率 | §8.1–§8.3 | 与 20 日持有期尺度不匹配；属于短线情绪，直接进当前决策会引入噪声 |
| 热榜 / 人气榜 / 互动易 / 上证 e 互动 | §10.1–§10.3 | 文本/噪声大、无标注、需 NLP；没有可复现的 IC 证据前不接入 |
| 新闻 / 快讯 / 新闻联播全文 | §5.1–§5.5 | 同上；事件**结构化字段**（§14.x/§7.1）优先级高于全文 NLP |
| iwencai 语义搜索 | §2.3 | 需要 Key、输出非确定性、无法离线测试；只适合人工研究，不进自动链路 |
| 新股申购日历 | §14.6 | 与持仓建议无关 |
| mootdx 行情命令 | §1.7 | 上游自己标注 2026-09 起返回空；没有理由引入 |
| 北交所专用备胎 | `bse_quote_backup` | 当前实仓无北交所；等有持仓再评估 |

### 6.2 待用户决策（本文不替用户做）

1. **是否引入新依赖**：P0 不需要新依赖（腾讯/深交所都是 HTTP）；P1 的 §6.5 估值历史需要 `baostock`（TCP），§4.6 筹码需要其换手率。是否接受一个新 TCP 依赖，由用户决定。
2. **`schema_version` 策略**：EPIC 原则是「既有字段不变；新增只增不改；必要变更 bump」。P0 的 `instrument` 新字段（`is_st`/`is_suspended`/`limit_up`/`limit_down`/`quote_source`）属于「只增」，是否 bump 到 `1.1` 需用户确认。
3. **regime 口径**：是否把「个股自身 MA20/MA60」改为「市场指数 regime」。这会改变历史输出标签与 `regime_win_rates`，属于**行为变更**，不能与 P0 混在一起。
4. **盘前是否使用实时报价**：9:15 尚未开盘，实时价可能是昨收/集合竞价未成交；若要用，必须明确「盘前用昨收 + 今日涨跌停价，盘中另跑」的口径。
5. **vendor 范围**：是 vendor 完整 SKILL.md（442KB）还是只 vendor 提取后的模块？本文推荐「完整 SKILL.md 作 provenance + 只移植 P0 函数」（§7）。
6. **Hermes 侧同步责任**：`PROGRESS.md:377-380` 已记录 Hermes 仍可能按 "sell"/「卖点」关键词扫描，必须读 `recommendation.action`/`target_position`/`position_state`。谁改 Hermes 提示词与渲染，需协调者分配。
7. **文档漂移**：`README.md:605` 的 `47 passed` 与 `PROGRESS.md:6` 的 `67 passed` 不一致，由哪条线修（本任务不允许改 README）。

---

## 7. 消费架构建议（三选一 + 推荐）

### 7.1 候选方案

| 方案 | 做法 | 优点 | 缺点 |
|---|---|---|---|
| **(a) vendor 固定快照 + 提取脚本** | 把 SKILL.md 按 commit 放进 `vendor/a-stock-data/`，写脚本按标记提取代码块生成模块 | 可追溯、可重复、升级 diff 清晰 | SKILL.md 是散文+代码交错；提取规则脆弱；V3.10.0 已重排 § 号；生成代码难评审 |
| **(b) 手工移植所需函数** | 只把 P0/P1 用到的函数按本项目风格重写进 `source/stock_analysis/data_loader/astock/`，每个函数标注上游 § + commit | 代码量小、可读、可测、无死代码；与现有模块风格一致 | 与上游会漂移；升级需人工对照 |
| **(c) 运行时解析 SKILL.md** | 运行时读取 SKILL.md、提取并执行代码 | 「永远最新」 | **直接否决**：违反「import 不联网 / 可测试 / 可重复升级」；执行散文里的代码不可审计；无法离线；上游一改就变行为 |

### 7.2 推荐：**(a) 的 provenance + (b) 的实现**（混合）

```
vendor/a-stock-data/
  SKILL.md            # 固定 commit 原文（442,341 字节，sha256 09f7…）
  LICENSE             # Apache-2.0 全文
  NOTICE              # 来源、commit、抓取日期、修改说明
  VERSION             # v3.10.4 / f21534946c42be3ad048ac938eecb1bad84cb8ab
  MANIFEST.json       # §→函数→项目文件 的映射 + 上游行号 + 本项目测试
source/stock_analysis/data_loader/astock/
  __init__.py
  tencent.py          # §1.1/§1.2 手工移植（仅 P0 需要的部分）
  calendar.py         # §12.4
  quote.py            # §1.1 实时报价/涨跌停
  # 每个函数 docstring: upstream §x.y @ f215349; 本地适配说明
source/tests/fixtures/astock/
  tencent_kline_600519.json
  tencent_quote_002415.json
  szse_calendar_2026-10.json
  ...
tools/astock_manifest.py   # 离线校验 sha256 + 打印 §→函数映射（不联网、不执行上游代码）
```

**为什么满足四条硬要求**：

1. **import 不联网**：`vendor/` 只是数据文件；`source/` 是手工移植的纯函数；测试用 fixture。
2. **可测试**：每个移植函数都有 fixture 单测；联网实测只由 Hermes 在 A5500 执行。
3. **Apache-2.0 归属**：`vendor/a-stock-data/LICENSE` + `NOTICE` + 每个移植文件头注明来源/commit/修改。
4. **可重复升级**：升级 PR 必须同时改 `VERSION`、`SKILL.md`、`MANIFEST.json`、fixture；`astock_manifest.py` 校验 sha256，§ 号变化会在 diff 中显式暴露。

**不推荐纯 (a) 提取**的理由（具体证据）：SKILL.md 的代码块用 `<!-- v39-tencent-kline:start -->`、`<!-- official-data-core:start -->` 等标记分隔，但大量逻辑在散文里（字段坑、降级规则、日期边界），提取器只能拿到代码、拿不到约束；而 V3.10.0 的 § 重排（SKILL.md:1061-1063）证明「按 § 号自动定位」本身就会漂移。手工移植 + manifest 把「语义约束」写进测试，比生成代码更可靠。

**不推荐 (c)** 的理由：运行时读取 SKILL.md 会引入网络/文件依赖、执行不可审计的代码、无法锁定行为，直接违反 EPIC 的「测试不联网 / 上游固定 commit」原则。

---

## 8. 分批 backlog

> 约定：P0 = 不做就会**持续出错误建议或持续取数失败**；P1 = 能改变决策但需先有证据；P2 = 研究方向/低相关。每项都写「目标 / 改动面 / 验收测试 / 依赖 / 预计风险」。所有测试默认**离线 fixture**，联网实测由 Hermes 执行。

### 8.1 P0（先做，4 项）

#### P0-1 腾讯 K 线备用通道（§1.2）

- **目标**：在东财/新浪通道失败时，仍能取到近端日线（至少覆盖当前回测窗口的尾部）；`data.source` 如实标注 `tencent`；**不替换**现有 qfq 历史基线，先做 fallback + parity。
- **改动面**：`source/stock_analysis/data_loader/bars.py:203-213` 渠道池新增 `_fetch_from_tencent`；`meta.py:153-239` 的降级路径复用；`README.md` 数据源说明；新增 `source/tests/test_data_sources.py`。
- **验收测试**：
  1. fixture 解析：给定上游返回样本，列名/顺序/单位正确（科创板 vs 主板）。
  2. monkeypatch 让 `eastmoney`、`sina_stock`、`sina_fund` 全失败，断言 `get_stock_daily` 仍返回非空且 `last_fetch_info()['source'] == 'tencent'`。
  3. 与现有新浪 qfq 缓存重叠段做 close parity：最大绝对差 ≤ 1e-6 才允许标「一致」；不一致时测试必须失败并打印差异样本（**禁止静默混源**）。
  4. 北交所代码断言抛 `ValueError`（上游 §1.2 明确不支持），不得静默返回空。
  5. `pytest` 全绿；G33/G36 不回归。
- **依赖**：无新依赖（`requests`/`urllib` 已有）。
- **预计风险**：腾讯 qfq 是等差口径（§1.2），与 akshare qfq 可能不一致；三入口限流；北交所不支持；把腾讯 qfq 写入现有 `stock_{code}_qfq.csv` 会污染跨版本对比。**缓解**：只做 fallback + 独立缓存命名/来源标注，不覆盖既有 qfq 缓存，除非 parity 通过。

#### P0-2 官方交易日历（§12.4）

- **目标**：`_expected_latest_trade_date` 与 `_trading_days_between` 使用官方日历（含节假日/调休）；日历不可用时回退「只跳周末」并在 `data` 里标注 `calendar_source='weekday_fallback'`。
- **改动面**：`source/stock_analysis/data_loader/meta.py:398-406`、`meta.py:90-103`；新增 `source/stock_analysis/data_loader/astock/calendar.py`；`schema.py:77-92` 新增 `calendar_source`（additive）；`README.md` 契约；新增测试。
- **验收测试**：
  1. fixture：2026 年春节、国庆、调休月的官方日历；断言 `expected_date` 与手工表一致。
  2. 节假日当天（`as_of`）不应触网、不应误报 `stale`。
  3. 当月日历未发布时回退工作日近似，`calendar_source` 如实标注，且不抛异常。
  4. 现有 `test_expected_trade_date_never_weekend`（`test_fix_regressions.py:316-319`）不回归。
- **依赖**：深交所官方 API；无 key。
- **预计风险**：未发布月份；接口字段变化；跨市场休市差异；日历缓存过期。**缓解**：按月缓存 + 失败回退 + 显式来源标注。

#### P0-3 真实交易状态（§1.1 + §6.8 + §6.6 + §6.5）

- **目标**：`instrument` 输出真实 `is_st`、`price_limit_pct`（ST 5%、科创/创业 20%、北交所 30%、主板 10%）、`is_suspended`、`limit_up`/`limit_down`、`quote_stale`；停牌/一字板/ST 时给出 `untradeable` 或硬 warning，阻止「不可执行建议」。
- **改动面**：`source/decision/instrument.py:5-37`；`source/decision/schema.py:67-73`；`source/cli.py:503-509` 的 instrument 组装；`README.md` 契约；新增 fixture 测试。
- **验收测试**：
  1. ST 代码 fixture → `is_st=True`、`price_limit_pct=0.05`；非 ST → 原值不变。
  2. 停牌 fixture → `status='untradeable'` 或 `diagnostics.warnings` 含明确原因；退出码按 `schema.EXIT_CODES`（`schema.py:22-28`）。
  3. 一字板 fixture → 建议可执行率为 0，且 warning 可读。
  4. 错误路径与成功路径键同构（G35，`test_fix_regressions.py:325+`）。
  5. 无网络：所有状态来自 fixture/缓存。
- **依赖**：§1.1 实时报价、§6.8 ST 名单、§6.6 basic、§6.5 历史状态列（回测用）。
- **预计风险**：盘前 9:15 报价含义（昨收/集合竞价）；`is_stale` 与正常停牌混淆；ST 名单当日变化；新增字段触发契约同步。**缓解**：先只做「读取 + warning」，不直接改 `action`；`untradeable` 仅在明确证据（停牌/一字板/ST 规则冲突）时使用。

#### P0-4 逐通道失败可见（可观测性）

- **目标**：渠道池全失败或部分失败时，`diagnostics.warnings` 逐条记录「来源 + 错误 + 是否冷却/剔除」；Hermes 能按契约「先报问题再报建议」（`~/.hermes/skills/stock-trading-helper/SKILL.md:54-63`）。
- **改动面**：`source/stock_analysis/data_loader/bars.py:105-144`；`meta.py` 的 `_LAST_FETCH_INFO`；`schema.py:77-92`（可加 `source_attempts`，additive）；`README.md`；测试。
- **验收测试**：
  1. 三个 fake fetcher 分别抛 `429`、`timeout`、`empty`；断言 warnings 含三条来源与原始错误文本，且 `data.source` 为 `cache`/`None` 时 `stale` 语义正确。
  2. 成功路径 warnings 不新增噪声。
  3. G33（`test_fix_regressions.py:298-313`）不回归。
- **依赖**：无。
- **预计风险**：warnings 过长；错误文本可能含 URL；schema 新增字段需同步 README。**缓解**：限制每条长度、来源名白名单、只增不改。

### 8.2 P1（有证据门槛，先上下文后决策）

#### P1-1 估值上下文（§1.1 + §6.5 + §2.2 + §12.3）

- **目标**：新增 `evidence.valuation`（PE/PB/PS 分位、前向 PE、PEG）作为**上下文**，不参与 `action`；先证明它对本项目建议有增量信息。
- **改动面**：`source/decision/schema.py`（evidence 新增块）、`source/cli.py` 组装、`source/stock_analysis/data_loader/astock/valuation.py`、`README.md`、测试。
- **验收测试**：历史 as-of 分位无前视（只用 `<= as_of` 的数据）；`action` 与现状完全一致（A/B 对照）；大池 IC 报告（`tools/ic_report.py`）可复现；置信度校准分箱可复现。
- **依赖**：§6.5 需 `baostock`（新依赖，待用户批准）；§2.2 需同花顺接口。
- **预计风险**：20 日尺度估值可能无预测力；前向 EPS 有分析师偏差；北交所不支持。**门槛**：若大池 IC 不显著，保持「只展示、不决策」，不进入 P1 决策层。

#### P1-2 资金面 / 筹码上下文（§3.4 + §4.5 + §4.1 + §4.3 + §4.6）

- **目标**：新增 `evidence.flow` / `evidence.chip` 上下文；先做分层统计，证明买点命中率/回撤有改善，再讨论是否作为过滤器。
- **改动面**：data_loader 新增端点适配（东财必须走串行限流）、schema、render 文案（只描述事实）、测试。
- **验收测试**：买点分层（有/无资金流）的 20 日均值、命中率、t 值；扣费净收益/max DD 对照；fixture 离线；东财请求间隔断言（mock 计时）。
- **依赖**：§4.6 依赖 §6.5 换手率；东财风控（§5.1）。
- **预计风险**：东财封 IP；资金流字段口径变化；20 日尺度衰减。**门槛**：不显著则只保留展示。

#### P1-3 指数 regime（§12.1/12.2/12.3 + §3.7）

- **目标**：把 `market_regime` 从「个股自身 MA20/MA60」改为「市场指数 regime」；`get_market_index_daily` 从空实现变为真实数据（`meta.py:429-431`）。
- **改动面**：`meta.py:429-431`、`analyzer.py:74-123`、`cli.py:166-190`、`schema.py:96-101`、`README.md`、测试。
- **验收测试**：同一决策日、as-of 指数成分的 regime 与现有标签对照；`regime_win_rates` 可复算；无前视（不使用未来成分/未来指数值）；错误路径同构。
- **依赖**：§12.1/12.2 指数成分/权重；指数行情源（腾讯/中证）。
- **预计风险**：当前成分快照的幸存者偏差（§12.1）；指数 regime 改变历史输出，属于行为变更，需单独 PR + 用户批准。

#### P1-4 事件黑名单 / 风险提示（§7.1 + §14.1–§14.5 + §3.6 + §8.4）

- **目标**：在建议里加入「未来 N 日事件窗口」warning（业绩预告、解禁、减持、质押、回购、监控池、异动）；不直接改 `action`。
- **改动面**：data_loader 事件适配、schema `diagnostics.events`（additive）、render 文案、测试。
- **验收测试**：fixture 事件窗口 → warning 出现/不出现；事件日前后建议可执行率对照；无网络。
- **依赖**：东财多端点（限流）；巨潮公告。
- **预计风险**：事件重复/缺失；公告时间与生效时间不同；warning 疲劳。**门槛**：先统计「带事件 warning 的建议」的回撤/命中，不显著则不推。

#### P1-5 基金 / ETF 数据增强（§4.7 + §1.6 + §12.1/12.2）

- **目标**：基金复权因子、ETF 份额、基准指数权重进入基金上下文；保持场外 OTC「净值 = close」路径不变（`meta.py:300-330`）。
- **改动面**：`meta.py:242-369`、新增基金适配、schema、`tools/check_source_parity.py` 测试、README。
- **验收测试**：基金复权前后净值序列差异可解释；ETF 份额与价格/折溢价的描述统计；现有基金网格 44 组不回归；离线 fixture。
- **依赖**：§1.6 新浪基金因子、§4.7 交易所份额文件。
- **预计风险**：场外基金无 OHLCV/份额；基金代码段与股票重叠（`README.md:659-675`）；复权口径变化会改变历史净值。

#### P1-6 交易制度完整性（§6.6 + §6.5 状态列 + §12.4）

- **目标**：回测与建议排除停牌日、还原历史 ST、剔除已退市标的；避免「在不可成交的 bar 上成交」。
- **改动面**：`source/stock_analysis/backtest/`（过滤逻辑）、`data_loader`（状态列）、测试。
- **验收测试**：停牌 fixture 不产生成交；已退市 fixture 不进入建议；历史 ST 日期用对涨跌停；现有回测数字变化必须逐项解释（属于行为变更）。
- **依赖**：§6.5/§6.6（baostock）。
- **预计风险**：历史状态缺失（上游明确已退市标的除权除息三源全缺，§6.6）；改变历史回测结果，需用户批准。

### 8.3 P2（研究/低相关）

| 项 | 上游 § | 目标 | 验收 | 依赖 | 风险 |
|---|---|---|---|---|---|
| 宏观与利率 | §11.1–§11.6 | 宏观环境上下文 | 与 regime 的领先性/校准 | 官方文件 | 20 日尺度弱 |
| 期货 / 商品 | §13.1–§13.7 | 宏观代理/隔夜情绪 | A50/商品与次日收益的相关 | 五家交易所 | 映射链长 |
| 情绪 / 热榜 / 打板 | §3.1、§3.5、§3.8、§3.9、§8.1–§8.5、§10.2 | 短线确认因子 | 大池 IC/命中 | 东财/同花顺 | 噪声、尺度错配 |
| 新闻 / 研报 / 互动 | §2.1、§2.4、§5.x、§7.1、§10.1、§10.3 | 事件抽取/预期修正 | 人工标注 + 命中 | NLP | 成本高、不可离线 |
| 行业分类历史 | §6.7 | 行业中性化 | 前视偏差消除、IC 稳定性 | 申万文件 | 仅代码无中文名 |
| 北向资金 | §3.2 | 外资流向上下文 | IC/校准 | 同花顺 | 披露口径/可用性需复验 |
| 期权 / 可转债 | §9.1、§15.1 | 仅在持仓扩展后 | — | — | 当前不做（§6.1） |

### 8.4 依赖与顺序

```
P0-4（可观测性）──┬─→ P0-1（腾讯 K 线 fallback）
                  ├─→ P0-2（官方日历）
                  └─→ P0-3（真实交易状态）
                              ↓
                    P1-3（指数 regime）──→ P1-1（估值上下文）
                              ↓                    ↓
                    P1-4（事件黑名单）      P1-2（资金面/筹码）
                              ↓
                    P1-5（基金/ETF） / P1-6（制度完整性）
                              ↓
                    P2（宏观/期货/情绪/文本）
```

- P0 之间可并行，但建议先做 P0-4（可观测性），否则 P0-1/2/3 的失败无法定位。
- P1 全部要求「先上下文、后决策」；任何直接改 `action` 的提案必须单独 PR + 大池证据 + 用户批准。

---

## 9. 未验证假设与待补证据

| # | 假设 | 状态 | 补证方式 |
|---|---|---|---|
| U1 | 腾讯 K 线与现有新浪/东财 qfq 在重叠段一致 | **未验证** | 用 `tools/check_source_parity.py` 同思路做三源对照；不一致则禁止混源 |
| U2 | 官方交易日历在 A5500 可稳定访问 | **未验证** | Hermes 实机跑深交所 API；记录失败率 |
| U3 | 腾讯实时报价在 9:15 盘前的语义（昨收/集合竞价） | **未验证** | 在交易日 9:10–9:25 实测，记录 `price/last_close/is_stale` |
| U4 | ST 名单接口返回当日准确、无延迟 | **未验证** | 连续 5 个交易日记录并与交易所公告对账 |
| U5 | `baostock` 在 Python 3.14 + A5500 可安装/登录 | **未验证** | `pip install baostock` + 登录 + 取 600519 一行；记录 wheel/报错 |
| U6 | 估值/资金面因子在 20 日尺度有增量 IC | **未验证** | `tools/ic_report.py` 大池（沪深 300）分因子 × horizon；不重叠采样 |
| U7 | 指数 regime 优于个股 regime | **未验证** | 同决策日 A/B；比较校准/回撤/`regime_win_rates` |
| U8 | 事件 warning 能降低回撤 | **未验证** | 事件窗口分层回放；比较 max DD / 命中率 |
| U9 | 盘前 10 只票在新增端点后仍能在窗口内跑完 | **未验证** | 记录端到端墙钟；必要时收盘后预计算 |
| U10 | Hermes 会按 `action` 而非关键词消费 | **未验证**（`PROGRESS.md:377-380` 已记录风险） | 让 Hermes 用新 JSON 跑一轮并回帖原始输出 |
| U11 | README 的测试数与实际一致 | **未验证**（README 47 vs PROGRESS 67） | 实跑 `pytest tests/ -q` 并回写文档 |
| U12 | 上游 §1.1 字段索引在 3.10.4 未再变动 | **已核对本版**（SKILL.md:1161-1194），升级后需重核 | 升级 manifest diff |

---

## 10. 自检清单（对应 issue #2 DoD）

- [x] 上游版本已固定：v3.10.4 / `f21534946c42be3ad048ac938eecb1bad84cb8ab`；记录了行数、字节数、sha256、抓取时间。
- [x] 能力清单按 7 组归并，逐项标注上游 § 编号；一个 § 多函数的已列出函数名。
- [x] 决策映射逐项落到本项目层，并给 `文件:行`；缺口可复核。
- [x] 价值假设只用允许的七类指标；无实测的地方写「待测」；没有编造收益/IC/胜率。
- [x] 成本与风险覆盖封禁/限流、延迟、依赖、Python 3.14、维护、许可、数据语义。
- [x] 明确不做清单 + 理由；待决策项未替用户拍板。
- [x] 消费架构三选一，给出推荐与四条硬要求（离线 import / 可测试 / Apache-2.0 归属 / 可重复升级）。
- [x] P0/P1/P2 backlog 每项含目标、改动面、验收测试、依赖、预计风险。
- [x] 未验证假设单列。
- [x] 本文只新增 `docs/` 下文件，未改 `vendor/`、`source/`、`README.md`、`requirements.txt`、测试。

---

## 11. 参考索引

### 11.1 上游（固定 commit）

- SKILL.md：`https://raw.githubusercontent.com/simonlin1212/a-stock-data/f21534946c42be3ad048ac938eecb1bad84cb8ab/SKILL.md`
- LICENSE：`https://raw.githubusercontent.com/simonlin1212/a-stock-data/f21534946c42be3ad048ac938eecb1bad84cb8ab/LICENSE`
- 关键章节：端点路由速查（SKILL.md:242-329）、数据源优先级 & 东财防封（331-398）、Layer 1 行情（1059-1965）、Layer 6 基础数据（3815-4272）、Layer 12 指数与交易日历（5631-5850）、备用源速查（7504-7590）、估值公式（7052-7112）。

### 11.2 本项目

- `README.md`：对外契约；v3.0 语义（67-89）、字段（281-438）、数据源与降级（611-634）、基金与 auto（635-675）、跨通道一致性（676-707）、依赖与 Python（718-773）。
- `GOAL_CONTRACT.md`：决策表（218-244）、稳定性（245-280）、Boundaries（340-357）。
- `source/cli.py`：决策主链（60、166-190、192-235、266-292、355-447、470-600）。
- `source/decision/actions.py`：决策表（37-61）。
- `source/decision/expectancy.py`：条件期望（25-28、39-121）。
- `source/decision/policy.py`：买点+风控回放（46-179）。
- `source/decision/sizing.py`：止损/止盈/入场区/仓位（19-129）。
- `source/decision/schema.py`：schema 与错误路径（9、67-101、263-377）。
- `source/stock_analysis/data_loader/bars.py`：三通道池（203-213）、失败策略（105-144）。
- `source/stock_analysis/data_loader/meta.py`：取数/缓存/降级（153-239、242-369、398-406、429-431、434-478）。
- `source/stock_analysis/advisor/analyzer.py`：regime（74-123）。
- `source/tests/`：G33/G35 回归（test_fix_regressions.py:298-360）、决策语义（test_semantic_actions.py:37-213）。
- `tools/ic_report.py`、`tools/forward_log.py`、`tools/collect_evidence.py`、`tools/check_source_parity.py`：验证工具。
- `PROGRESS.md`：V6 IC 复验（375、383-388）、Hermes 残留风险（377-380）。

### 11.3 消费者

- `~/.hermes/skills/stock-trading-helper/SKILL.md`：操作建议模式（54-63）、自选清单（58）。
- `~/.hermes/scripts/trade_preopen_alert.py`：消费字段（63-101）、每票超时（52-53）、实仓清单（29-30）。

---

## 12. 变更记录

| 日期 | 版本 | 变更 | 作者 |
|---|---|---|---|
| 2026-11-12 | v1.0 | 初稿：能力清单 / 决策映射 / 价值假设 / 成本风险 / 不做清单 / 架构建议 / P0-P2 backlog | TF-Leo（Orca worker） |
