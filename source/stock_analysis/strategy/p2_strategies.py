"""P2 阶段策略：量价资金流、形态识别、网格交易、波动率目标、配对/价差、策略组合。

包含：
  - MfiStrategy            MFI 资金流量指数
  - CmfStrategy            CMF 蔡金资金流
  - GapStrategy            缺口跳空策略
  - EngulfingStrategy       吞没形态
  - HammerStrategy          锤子线/上吊线
  - DojiStrategy            十字星反转
  - GridArithmeticStrategy  等差网格交易
  - GridGeometricStrategy   等比网格交易
  - GridAtrStrategy         ATR 自适应网格
  - VolTargetStrategy       波动率目标仓位过滤
  - PairsStrategy           配对交易（vs 指数基准）
  - SpreadZscoreStrategy    价差 Z-Score 回归（vs 指数基准）
  - StrategyVoting          多策略投票组合
  - EtfRotationStrategy     指数轮动门控（单标的版：基准动量+自身趋势）
  - RiskParityStrategy      风险平价组合（单标的版：逆波动加权）

注：多标的同时持有轮仓需要多资产组合架构（超出单标的锦标赛范围）；
以下两策略是单标的兼容实现——用基准动量做门控/用逆波动做加权，不做跨标的调仓。
"""
import pandas as pd
import numpy as np
from stock_analysis.indicators import (
    calc_mfi, calc_cmf, calc_atr, calc_rsi, calc_ma,
    detect_gap, detect_engulfing, detect_hammer, detect_doji,
)
from stock_analysis.strategy.base import BaseStrategy


class MfiStrategy(BaseStrategy):
    """MFI 资金流量指数：超卖买入，超买卖出。"""

    def __init__(self, period=14, buy_mfi=20, sell_mfi=80):
        super().__init__("MFI")
        self.period = period
        self.buy_mfi = buy_mfi
        self.sell_mfi = sell_mfi

    def generate_trade_signals(self, df):
        mfi = calc_mfi(df['high'], df['low'], df['close'], df['volume'], period=self.period)
        df = df.copy()
        df['MFI'] = mfi
        buy = (df['MFI'] < self.buy_mfi) & (df['MFI'].shift(1) >= self.buy_mfi)
        sell = (df['MFI'] > self.sell_mfi) & (df['MFI'].shift(1) <= self.sell_mfi)
        return pd.DataFrame({'buy': buy, 'sell': sell}, index=df.index)


class CmfStrategy(BaseStrategy):
    """CMF 蔡金资金流：资金流入为正买入，流出为负卖出。"""

    def __init__(self, period=20, buy_cmf=0.1, sell_cmf=-0.1):
        super().__init__("CMF")
        self.period = period
        self.buy_cmf = buy_cmf
        self.sell_cmf = sell_cmf

    def generate_trade_signals(self, df):
        cmf = calc_cmf(df['high'], df['low'], df['close'], df['volume'], period=self.period)
        df = df.copy()
        df['CMF'] = cmf
        buy = (df['CMF'] > self.buy_cmf) & (df['CMF'].shift(1) <= self.buy_cmf)
        sell = (df['CMF'] < self.sell_cmf) & (df['CMF'].shift(1) >= self.sell_cmf)
        return pd.DataFrame({'buy': buy, 'sell': sell}, index=df.index)


class GapStrategy(BaseStrategy):
    """缺口跳空：跳空高开买入，跳空低开卖出。"""

    def __init__(self, gap_threshold=0.01):
        super().__init__("缺口跳空")
        self.gap_threshold = gap_threshold

    def generate_trade_signals(self, df):
        gap_up, gap_down = detect_gap(df, gap_threshold=self.gap_threshold)
        return pd.DataFrame({'buy': gap_up, 'sell': gap_down}, index=df.index)


class EngulfingStrategy(BaseStrategy):
    """吞没形态：看涨吞没买入，看跌吞没卖出。"""

    def __init__(self):
        super().__init__("吞没形态")

    def generate_trade_signals(self, df):
        bull, bear = detect_engulfing(df)
        return pd.DataFrame({'buy': bull, 'sell': bear}, index=df.index)


class HammerStrategy(BaseStrategy):
    """锤子线/上吊线：锤子线买入，上吊线卖出。"""

    def __init__(self, body_ratio=0.3, shadow_ratio=2.0):
        super().__init__("锤子线")
        self.body_ratio = body_ratio
        self.shadow_ratio = shadow_ratio

    def generate_trade_signals(self, df):
        bull, bear = detect_hammer(df, body_ratio=self.body_ratio, shadow_ratio=self.shadow_ratio)
        return pd.DataFrame({'buy': bull, 'sell': bear}, index=df.index)


class DojiStrategy(BaseStrategy):
    """十字星反转：高位十字星卖出，低位十字星买入。"""

    def __init__(self, body_ratio=0.1, trend_window=5):
        super().__init__("十字星")
        self.body_ratio = body_ratio
        self.trend_window = trend_window

    def generate_trade_signals(self, df):
        doji = detect_doji(df, body_ratio=self.body_ratio)
        uptrend = df['close'] > df['close'].shift(self.trend_window)
        downtrend = df['close'] < df['close'].shift(self.trend_window)
        buy = doji & downtrend
        sell = doji & uptrend
        return pd.DataFrame({'buy': buy, 'sell': sell}, index=df.index)


class GridArithmeticStrategy(BaseStrategy):
    """等差网格交易：以 N 日均价为中心，按固定价差分网格，跌破下格买入，涨破上格卖出。"""

    def __init__(self, ma_period=20, grid_steps=5, grid_pct=0.03):
        super().__init__("等差网格")
        self.ma_period = ma_period
        self.grid_steps = grid_steps
        self.grid_pct = grid_pct

    def generate_trade_signals(self, df):
        ma = df['close'].rolling(self.ma_period).mean()
        grid = self.grid_pct
        df = df.copy()
        df['MA'] = ma
        df['GRID_LOWER'] = ma - self.grid_steps * grid * ma
        df['GRID_UPPER'] = ma + self.grid_steps * grid * ma
        # 跌破下网格买入，涨破上网格卖出
        buy = (df['close'] < df['GRID_LOWER']) & (df['close'].shift(1) >= df['GRID_LOWER'].shift(1))
        sell = (df['close'] > df['GRID_UPPER']) & (df['close'].shift(1) <= df['GRID_UPPER'].shift(1))
        return pd.DataFrame({'buy': buy, 'sell': sell}, index=df.index)


class GridGeometricStrategy(BaseStrategy):
    """等比网格交易：按固定百分比分网格。"""

    def __init__(self, ma_period=20, grid_steps=5, grid_pct=0.03):
        super().__init__("等比网格")
        self.ma_period = ma_period
        self.grid_steps = grid_steps
        self.grid_pct = grid_pct

    def generate_trade_signals(self, df):
        ma = df['close'].rolling(self.ma_period).mean()
        df = df.copy()
        df['GRID_LOWER'] = ma * ((1 - self.grid_pct) ** self.grid_steps)
        df['GRID_UPPER'] = ma * ((1 + self.grid_pct) ** self.grid_steps)
        buy = (df['close'] < df['GRID_LOWER']) & (df['close'].shift(1) >= df['GRID_LOWER'].shift(1))
        sell = (df['close'] > df['GRID_UPPER']) & (df['close'].shift(1) <= df['GRID_UPPER'].shift(1))
        return pd.DataFrame({'buy': buy, 'sell': sell}, index=df.index)


class GridAtrStrategy(BaseStrategy):
    """ATR 自适应网格：网格间距按 ATR 调整。"""

    def __init__(self, atr_period=14, ma_period=20, grid_mult=2.0):
        super().__init__("ATR网格")
        self.atr_period = atr_period
        self.ma_period = ma_period
        self.grid_mult = grid_mult

    def generate_trade_signals(self, df):
        atr = calc_atr(df['high'], df['low'], df['close'], period=self.atr_period)
        ma = df['close'].rolling(self.ma_period).mean()
        df = df.copy()
        df['ATR'] = atr
        df['GRID_LOWER'] = ma - self.grid_mult * atr
        df['GRID_UPPER'] = ma + self.grid_mult * atr
        buy = (df['close'] < df['GRID_LOWER']) & (df['close'].shift(1) >= df['GRID_LOWER'].shift(1))
        sell = (df['close'] > df['GRID_UPPER']) & (df['close'].shift(1) <= df['GRID_UPPER'].shift(1))
        return pd.DataFrame({'buy': buy, 'sell': sell}, index=df.index)


class VolTargetStrategy(BaseStrategy):
    """波动率目标仓位：低波动时跟随均线买入，高波动时减仓/空仓。"""

    def __init__(self, vol_window=20, target_vol=0.15, ma_short=5, ma_long=20):
        super().__init__("波动率目标")
        self.vol_window = vol_window
        self.target_vol = target_vol
        self.ma_short = ma_short
        self.ma_long = ma_long

    def generate_trade_signals(self, df):
        df = df.copy()
        df['MA_S'] = df['close'].rolling(self.ma_short).mean()
        df['MA_L'] = df['close'].rolling(self.ma_long).mean()
        df['VOL'] = df['close'].pct_change().rolling(self.vol_window).std() * np.sqrt(252)
        vol_ok = df['VOL'] < self.target_vol
        gold = (df['MA_S'] > df['MA_L']) & (df['MA_S'].shift(1) <= df['MA_L'].shift(1))
        dead = (df['MA_S'] < df['MA_L']) & (df['MA_S'].shift(1) >= df['MA_L'].shift(1))
        buy = gold & vol_ok
        sell = dead | (~vol_ok & (df['MA_S'] < df['MA_L']))
        return pd.DataFrame({'buy': buy, 'sell': sell}, index=df.index)


class PairsStrategy(BaseStrategy):
    """配对交易（vs 指数基准）：股票相对指数的价差超卖买入，超买卖出。

    需要 benchmark_df 作为外部数据。若未提供，退化为 RSI 均值回归。
    """

    def __init__(self, benchmark_df=None, window=20, entry_z=-1.5, exit_z=0.5):
        super().__init__("配对交易")
        self.benchmark_df = benchmark_df
        self.window = window
        self.entry_z = entry_z
        self.exit_z = exit_z

    def generate_trade_signals(self, df):
        df = df.copy()
        if self.benchmark_df is not None and not self.benchmark_df.empty:
            bench = self.benchmark_df['close'].reindex(df.index).fillna(method='ffill')
            spread = df['close'] / bench - 1
        else:
            # 退化：用 close/MA 偏离作为价差
            spread = df['close'] / df['close'].rolling(self.window).mean() - 1
        spread_ma = spread.rolling(self.window).mean()
        spread_std = spread.rolling(self.window).std()
        z = (spread - spread_ma) / spread_std.replace(0, np.nan)
        buy = z < self.entry_z
        sell = z > self.exit_z
        return pd.DataFrame({'buy': buy, 'sell': sell}, index=df.index)


class SpreadZscoreStrategy(BaseStrategy):
    """价差 Z-Score 回归（vs 指数基准）：价差 Z-Score 超卖买入，回归均值卖出。"""

    def __init__(self, benchmark_df=None, window=20, entry_z=-2.0, exit_z=0.0):
        super().__init__("价差ZScore")
        self.benchmark_df = benchmark_df
        self.window = window
        self.entry_z = entry_z
        self.exit_z = exit_z

    def generate_trade_signals(self, df):
        df = df.copy()
        if self.benchmark_df is not None and not self.benchmark_df.empty:
            bench = self.benchmark_df['close'].reindex(df.index).fillna(method='ffill')
            spread = (df['close'] / bench).fillna(method='ffill')
        else:
            spread = df['close'] / df['close'].rolling(self.window).mean()
        spread_ma = spread.rolling(self.window).mean()
        spread_std = spread.rolling(self.window).std()
        z = (spread - spread_ma) / spread_std.replace(0, np.nan)
        buy = z < self.entry_z
        sell = z > self.exit_z
        return pd.DataFrame({'buy': buy, 'sell': sell}, index=df.index)


class StrategyVoting(BaseStrategy):
    """多策略投票组合：RSI + MACD + 布林 三策略投票，≥2 票买入则买入。"""

    def __init__(self, vote_threshold=2):
        super().__init__("策略组合")
        self.vote_threshold = vote_threshold
        from stock_analysis.indicators import calc_rsi, calc_macd, calc_boll
        self._calc_rsi = calc_rsi
        self._calc_macd = calc_macd
        self._calc_boll = calc_boll

    def generate_trade_signals(self, df):
        rsi = self._calc_rsi(df['close'])
        macd, signal, hist = self._calc_macd(df['close'])
        upper, mid, lower = self._calc_boll(df['close'])
        votes_buy = (
            (rsi < 30).astype(int)
            + ((macd > signal) & (macd.shift(1) <= signal.shift(1))).astype(int)
            + (df['close'] < lower).astype(int)
        )
        votes_sell = (
            (rsi > 70).astype(int)
            + ((macd < signal) & (macd.shift(1) >= signal.shift(1))).astype(int)
            + (df['close'] > upper).astype(int)
        )
        buy = votes_buy >= self.vote_threshold
        sell = votes_sell >= self.vote_threshold
        return pd.DataFrame({'buy': buy, 'sell': sell}, index=df.index)


class EtfRotationStrategy(BaseStrategy):
    """指数轮动门控（单标的兼容版）。

    完整行业ETF轮动需多标的同时持仓，而单标的锦标赛只输出一只标的的买卖信号；
    此处实现轮动思想的门控侧：仅当基准指数动量为正（牛市/震荡偏强）才允许跟随
    自身均线趋势，否则空仓。用 `benchmark` 传入指数close序列（对齐reindex+ffill）；
    未提供时退化为纯双均线趋势（门控恒为真），保证可回测。
    仅用过去数据（rolling/shift），无前视。
    """

    def __init__(self, benchmark=None, ma_short=5, ma_long=20, mom_window=20):
        super().__init__("指数轮动")
        self.benchmark = benchmark
        self.ma_short = ma_short
        self.ma_long = ma_long
        self.mom_window = mom_window

    def _bench_momentum(self, df: pd.DataFrame) -> pd.Series:
        if self.benchmark is not None and len(self.benchmark):
            try:
                b = self.benchmark['close'] if isinstance(self.benchmark, pd.DataFrame) else self.benchmark
                b = pd.Series(b).reindex(df.index).ffill()
                return b / b.shift(self.mom_window) - 1
            except Exception:
                pass
        # 退化：用自身动量代替基准动量
        return df['close'] / df['close'].shift(self.mom_window) - 1

    def generate_trade_signals(self, df):
        df = df.copy()
        df['MA_S'] = df['close'].rolling(self.ma_short).mean()
        df['MA_L'] = df['close'].rolling(self.ma_long).mean()
        mom = self._bench_momentum(df).fillna(0)
        gate = mom > 0
        gold = (df['MA_S'] > df['MA_L']) & (df['MA_S'].shift(1) <= df['MA_L'].shift(1))
        dead = (df['MA_S'] < df['MA_L']) & (df['MA_S'].shift(1) >= df['MA_L'].shift(1))
        buy = gold & gate
        sell = dead | (~gate & (df['MA_S'] < df['MA_L']))
        return pd.DataFrame({'buy': buy.fillna(False), 'sell': sell.fillna(False)}, index=df.index)


class RiskParityStrategy(BaseStrategy):
    """风险平价组合（单标的兼容版）。

    完整风险平价需多资产协方差矩阵；单标的下实现其等风险贡献思想：
    RSI / MACD / 布林三子信号各算一份信号收益序列，按近期逆波动率加权综合得分，
    得分上穿阈值买入、下穿卖出。波动越大权重越低，三个子策略贡献的风险近似均衡。
    仅用过去vol_window数据，无前视。
    """

    def __init__(self, vol_window=20, buy_score=0.5, sell_score=-0.5):
        super().__init__("风险平价")
        self.vol_window = vol_window
        self.buy_score = buy_score
        self.sell_score = sell_score
        from stock_analysis.indicators import calc_rsi, calc_macd, calc_boll
        self._calc_rsi = calc_rsi
        self._calc_macd = calc_macd
        self._calc_boll = calc_boll

    def generate_trade_signals(self, df):
        rsi = self._calc_rsi(df['close'])
        macd, signal, _hist = self._calc_macd(df['close'])
        upper, _mid, lower = self._calc_boll(df['close'])
        s1 = ((rsi < 30).astype(float) - (rsi > 70).astype(float))
        s2 = (((macd > signal) & (macd.shift(1) <= signal.shift(1))).astype(float)
              - ((macd < signal) & (macd.shift(1) >= signal.shift(1))).astype(float))
        s3 = ((df['close'] < lower).astype(float) - (df['close'] > upper).astype(float))
        rets = df['close'].pct_change()
        # 子信号近期实现波动（带下限防除零），权重=逆波动归一化
        v1 = (s1 * rets).rolling(self.vol_window).std().fillna(0.02) + 1e-6
        v2 = (s2 * rets).rolling(self.vol_window).std().fillna(0.02) + 1e-6
        v3 = (s3 * rets).rolling(self.vol_window).std().fillna(0.02) + 1e-6
        w = pd.concat([1 / v1, 1 / v2, 1 / v3], axis=1)
        w.columns = ["w1", "w2", "w3"]
        w = w.div(w.sum(axis=1).replace(0, np.nan), axis=0).fillna(1 / 3)
        score = s1 * w.iloc[:, 0] + s2 * w.iloc[:, 1] + s3 * w.iloc[:, 2]
        buy = (score > self.buy_score) & (score.shift(1) <= self.buy_score)
        sell = (score < self.sell_score) & (score.shift(1) >= self.sell_score)
        return pd.DataFrame({'buy': buy.fillna(False), 'sell': sell.fillna(False)}, index=df.index)
