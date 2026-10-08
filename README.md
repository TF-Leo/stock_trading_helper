# Stock Trading Helper

A 股 / 基金多策略回测工具：在终端输入标的代码，自动拉取历史数据、跑多策略锦标赛（股票 40 类 / 581 组参数），输出胜率最高的操作策略与分市场（牛/熊/震荡）表现。

> 本项目是回测研究工具，输出结果基于历史数据，**不构成投资建议**。

## 特性

- **一键回测**：自动识别股票/基金类型，输出胜率、盈亏比、年化收益、最大回撤、夏普、Calmar、连赢连亏
- **多策略锦标赛**：股票 581 组参数 + 基金 44 组参数同台竞技，按 Wilson 置信下界评分，抑制小样本高胜率的过拟合
- **防过拟合**：70/30 样本内/外切分 + 3 折 walk-forward，按样本外表现排名；跑不赢买入持有/空仓基线时提示"无优势，建议观望"
- **贴近实盘的交易成本建模**：佣金、印花税、滑点、止损止盈、100 股整手、涨跌停限制、信号 T+1 执行
- **贝叶斯参数寻优**：`--opt bayes` 30 次评估替代全网格（约 1s vs 约 45s），快速验证
- **无需任何 API key**：行情均来自公开接口（东方财富/新浪/akshare），本地 CSV 缓存

## 安装

需要 Python 3.11+。

```bash
cd source
python3 -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

依赖为 `akshare` / `requests` / `numpy` / `pandas`（另有 `pytest` 用于测试），版本已在 `requirements.txt` 中固定。

## 用法

```bash
cd source

# 自动识别类型（股票/基金），跑多策略锦标赛（默认全网格 + 样本外排名）
python cli.py 600519      # 股票：贵州茅台
python cli.py 510300      # 基金：沪深300ETF

# 强制指定类型
python cli.py 600519 --type stock
python cli.py 510300 --type fund

# 显示全策略对比表（默认按样本外 Final 排序，Top30）
python cli.py 600519 --detail

# 跳过市场环境加载（更快，但不输出牛/熊/震荡分市场胜率）
python cli.py 600519 --no-regime

# 指定数据起始日期（默认 20200101）
python cli.py 600519 --start 20210101

# 锦标赛并行（默认 1，多核机器推荐 4/8）
python cli.py 600519 --jobs 4

# 贝叶斯优化：30 次评估替代全网格（~1s vs ~45s，快速验证用）
python cli.py 600519 --opt bayes --opt-iters 30
```

| 参数 | 说明 |
| --- | --- |
| `code` | 6 位股票/基金代码 |
| `-t, --type` | `auto`（默认）/ `stock` / `fund` |
| `--detail` | 显示全策略对比表 |
| `--no-regime` | 跳过市场环境加载 |
| `--start` | 数据起始日期，默认 `20200101` |
| `--jobs` | 锦标赛并行数，默认 1 |
| `--opt` | `grid`（默认，全网格）/ `bayes`（贝叶斯寻优） |
| `--opt-iters` | 贝叶斯寻优迭代次数，默认 30 |

输出示例：

```
=== 股票: 贵州茅台 (600519) ===
数据区间: 2020-01-02 ~ 2026-08-04（1578 条）
加载市场环境...
市场环境加载完成
运行多策略锦标赛...

>>> 最优策略: 均线趋势(5-13-34)
    胜率      : 58.33%
    盈亏比    : 1.87
    年化收益  : 12.45%
    最大回撤  : 18.20%
    夏普比率  : 0.92
    交易次数  : 24
    平均持仓  : 23.5 天
    分市场胜率: 牛72% / 熊30% / 震荡55%
```

## 工作原理

1. **数据拉取**：股票日线走多渠道池（东方财富/新浪）+ 本地 CSV 缓存（前复权，拉取失败自动回退缓存）；基金走 akshare 净值。
2. **多策略锦标赛**：全部参数组在统一回测引擎上运行，评分：

   `得分 = Wilson下界 × 0.5 + 年化收益 × 0.2 − 最大回撤 × 0.3`（交易数 < 10 直接判 -100）

   两阶段防过拟合：70/30 样本内/外切分 + 3 折 walk-forward，`final = 0.3 × 样本内 + 0.7 × 样本外`（样本外交易 < 5 判 -100，Calmar < 0.5 软惩罚）。若所有策略均跑不赢买入持有/空仓基线，会提示"无优势，建议观望"。
3. **回测引擎**：模拟真实交易——佣金万 3（最低 5 元）、印花税千 1、滑点千 1、止损 8% / 止盈 15%、100 股整手、涨停买不进 / 跌停卖不出、信号 T+1 执行。输出胜率、盈亏比、夏普、Calmar、最大回撤、连赢连亏、牛熊震荡胜率（asof 对齐，停牌不丢数据）。

## 内置策略

### 股票（40 类 / 581 组，参数网格单一来源：`strategy/registry.py`）

| 类别 | 策略 |
| --- | --- |
| 趋势 | 均线趋势、唐奇安通道、EMA 交叉、KAMA、HMA 交叉、VWAP、SAR、DI 交叉 |
| 动量 | MACD、KDJ、CCI、WilliamsR、StochRSI、ADX |
| 均值回归 | 布林、布林 Squeeze、Z-Score、ATR 过滤 |
| 量价 | 量价突破、OBV、VR、MFI、CMF、缺口跳空、吞没、锤子线、十字星 |
| 筹码/共振 | 筹码分布、三维共振（量价筹）、Kelly |
| 网格 | 等差网格、等比网格、ATR 网格 |
| 波动率 | 波动率目标、ATR 通道 |
| 配对/价差 | 配对交易、价差 Z-Score |
| 组合 | 多策略投票 |
| 配置 | 指数轮动（基准动量门控）、风险平价 |

### 基金（6 类 / 44 组）

RSI 波段（16 组）+ MA20 持有 + 双均线（9 组）/ MACD（8 组）/ 波动率目标（6 组）/ 指数增强轮动（4 组，基金双均线金叉 + 基准指数动量门控），均含双边申赎费（0.15%），评分与股票侧一致（Wilson 下界）。

## 扩展新策略

继承 `BaseStrategy` 实现 `generate_trade_signals(df)`（返回含 `buy` / `sell` 两列布尔序列的 DataFrame），再到 `strategy/registry.py` 的 `_grid_defs()` 加一行参数网格定义，即自动进入锦标赛：

```python
from stock_analysis.strategy.base import BaseStrategy
import pandas as pd

class GoldenCrossStrategy(BaseStrategy):
    def __init__(self):
        super().__init__("Golden_Cross")

    def generate_trade_signals(self, df):
        df = df.copy()
        df["MA5"] = df["close"].rolling(5).mean()
        df["MA20"] = df["close"].rolling(20).mean()
        buy = (df["MA5"] > df["MA20"]) & (df["MA5"].shift(1) <= df["MA20"].shift(1))
        sell = (df["MA5"] < df["MA20"]) & (df["MA5"].shift(1) >= df["MA20"].shift(1))
        return pd.DataFrame({"buy": buy, "sell": sell}, index=df.index)
```

## 自选 / 参数配置（可选）

主 CLI 对单个代码跑全参数网格，不依赖任何配置。若需要固定的自选股列表或锁定每只标的的参数，可启用配置文件（示例模板已随仓库提供，真实配置文件在 `.gitignore` 中，不会被提交）：

```bash
# 股票：自选列表 + 每只标的的策略/参数
cp source/stock_analysis/stock_config.example.json source/stock_analysis/stock_config.json
# 基金：每只基金的策略/参数
cp source/fund_analysis/fund_config.example.json source/fund_analysis/fund_config.json
```

配置会被以下辅助入口使用（均以 `python -m` 方式运行，需在 `source/` 目录下）：

| 入口 | 用途 |
| --- | --- |
| `python -m stock_analysis.strategy_master.runner` | 批量优化：遍历自选股，为每只找到最优策略并回写配置 |
| `python -m stock_analysis.advisor.recommender` | 股票顾问：结合市场环境与自选股输出操作建议 |
| `python -m fund_analysis.fund_strategy.runner` | 基金批量回测/参数优化 |
| `python -m fund_analysis.fund_advisor.recommender` | 基金顾问：输出申购/持有建议 |

## 测试

```bash
cd source
python -m pytest tests/ -q     # 25 项
```

## 目录结构

```
.
├── LICENSE
├── README.md
└── source/
    ├── cli.py                          # 终端入口（--detail/--jobs/--opt bayes 等）
    ├── requirements.txt
    ├── pytest.ini
    ├── stock_analysis/
    │   ├── data_loader/                # 多渠道行情池 + CSV 缓存
    │   │   ├── bars.py                 # 日线抓取（eastmoney/sina 渠道池）
    │   │   └── meta.py                 # 名称/指数/市场宽度等元数据
    │   ├── indicators/                 # 技术指标（KAMA/HMA/SAR 已向量化）
    │   │   ├── trend.py
    │   │   ├── momentum.py
    │   │   └── volatility.py
    │   ├── strategy/                   # 策略库（40 类 / 581 组）
    │   │   ├── registry.py             # 参数网格单一来源
    │   │   ├── base.py                 # BaseStrategy 统一接口
    │   │   ├── trade.py / rules.py     # P0 基础策略
    │   │   ├── p0_strategies.py        # P0 扩展策略
    │   │   ├── p1_strategies.py        # P1 策略
    │   │   └── p2_strategies.py        # P2 策略
    │   ├── backtest/
    │   │   ├── engine.py               # 回测引擎（费用/整手/涨跌停/T+1）
    │   │   ├── analyzer.py             # IS/OOS 两阶段锦标赛
    │   │   └── optimizer.py            # 贝叶斯参数寻优
    │   ├── advisor/
    │   │   ├── analyzer.py             # 趋势/水位/量价筹码分析
    │   │   └── recommender.py          # 股票操作建议
    │   └── strategy_master/
    │       ├── config.py                # 每标的参数配置 + 市场 regime 序列
    │       └── runner.py                # 批量策略优化
    └── fund_analysis/
        ├── fund_backtest.py            # 基金信号回测执行层（含申赎费）
        ├── utils.py
        ├── fund_advisor/
        │   ├── analyzer.py
        │   └── recommender.py
        └── fund_strategy/
            ├── strategy.py             # 基金数据/指标/基准
            ├── runner.py               # 基金回测/参数优化
            └── p2_strategies.py        # 双均线/MACD/波动率目标/指数轮动
```

## 数据源

- 股票日线：东方财富 / 新浪公开接口，渠道池容错；缓存于 `source/stock_analysis/data/`（gitignore）
- 指数/市场宽度：新浪公开接口
- 基金净值：akshare；缓存于 `source/fund_analysis/data/`（gitignore）
- 全部为公开接口，无需 token

## 免责声明

本项目仅用于量化回测研究。回测结果基于历史数据，不代表未来表现；文中任何策略与指标均不构成投资建议，据此操作风险自负。

## License

[MIT](LICENSE)
