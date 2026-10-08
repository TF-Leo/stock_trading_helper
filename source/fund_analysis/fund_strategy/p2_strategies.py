"""P2 基金侧策略：close-only，适配基金净值数据（无 OHLCV）。

包含：
  - FundMaStrategy      双均线择时
  - FundMacdStrategy    MACD 择时
  - FundVolTargetStrategy 波动率目标仓位
  - FundIndexRotationStrategy 指数增强轮动（基金相对基准动量门控）
"""
import pandas as pd
import numpy as np


class FundMaStrategy:
    """基金双均线择时：短期上穿长期买入，下穿卖出。"""

    def __init__(self, short=20, long=60):
        self.short = short
        self.long = long

    def generate_signals(self, df):
        df = df.copy()
        df['MA_S'] = df['close'].rolling(self.short).mean()
        df['MA_L'] = df['close'].rolling(self.long).mean()
        buy = (df['MA_S'] > df['MA_L']) & (df['MA_S'].shift(1) <= df['MA_L'].shift(1))
        sell = (df['MA_S'] < df['MA_L']) & (df['MA_S'].shift(1) >= df['MA_L'].shift(1))
        return pd.DataFrame({'buy': buy, 'sell': sell}, index=df.index)

    @property
    def name(self):
        return f"双均线({self.short}-{self.long})"


class FundMacdStrategy:
    """基金 MACD 择时：金叉买入，死叉卖出。"""

    def __init__(self, fast=12, slow=26, signal=9):
        self.fast = fast
        self.slow = slow
        self.signal = signal

    def generate_signals(self, df):
        close = df['close']
        exp1 = close.ewm(span=self.fast, adjust=False).mean()
        exp2 = close.ewm(span=self.slow, adjust=False).mean()
        macd = exp1 - exp2
        signal_line = macd.ewm(span=self.signal, adjust=False).mean()
        buy = (macd > signal_line) & (macd.shift(1) <= signal_line.shift(1))
        sell = (macd < signal_line) & (macd.shift(1) >= signal_line.shift(1))
        return pd.DataFrame({'buy': buy, 'sell': sell}, index=df.index)

    @property
    def name(self):
        return f"MACD({self.fast}-{self.slow}-{self.signal})"


class FundVolTargetStrategy:
    """基金波动率目标仓位：低波动时持有，高波动时空仓。"""

    def __init__(self, vol_window=20, target_vol=0.15, ma_period=20):
        self.vol_window = vol_window
        self.target_vol = target_vol
        self.ma_period = ma_period

    def generate_signals(self, df):
        df = df.copy()
        df['VOL'] = df['close'].pct_change().rolling(self.vol_window).std() * np.sqrt(252)
        df['MA'] = df['close'].rolling(self.ma_period).mean()
        vol_ok = df['VOL'] < self.target_vol
        buy = vol_ok & (df['close'] > df['MA']) & (df['close'].shift(1) <= df['MA'].shift(1))
        sell = (~vol_ok | (df['close'] < df['MA'])) & (df['close'].shift(1) >= df['MA'].shift(1))
        return pd.DataFrame({'buy': buy, 'sell': sell}, index=df.index)

    @property
    def name(self):
        return f"波动率目标({self.vol_window},{self.target_vol})"


class FundIndexRotationStrategy:
    """基金指数增强轮动（close-only）：基金双均线金叉 + 基准指数动量为正才持有。

    benchmark可传指数close序列（DataFrame/Series）；未提供时退化为纯双均线择时。
    仅用过去数据，无前视。
    """

    def __init__(self, benchmark=None, short=10, long=60, mom_window=20):
        self.benchmark = benchmark
        self.short = short
        self.long = long
        self.mom_window = mom_window

    def generate_signals(self, df):
        c = df.copy()
        c['MA_S'] = c['close'].rolling(self.short).mean()
        c['MA_L'] = c['close'].rolling(self.long).mean()
        try:
            if self.benchmark is not None and len(self.benchmark):
                b = self.benchmark['close'] if isinstance(self.benchmark, pd.DataFrame) else self.benchmark
                b = pd.Series(b).reindex(c.index).ffill()
                mom = b / b.shift(self.mom_window) - 1
            else:
                mom = c['close'] / c['close'].shift(self.mom_window) - 1
        except Exception:
            mom = c['close'] / c['close'].shift(self.mom_window) - 1
        gate = (mom > 0).fillna(False)
        buy = (c['MA_S'] > c['MA_L']) & (c['MA_S'].shift(1) <= c['MA_L'].shift(1)) & gate
        sell = ((c['MA_S'] < c['MA_L']) & (c['MA_S'].shift(1) >= c['MA_L'].shift(1))) | (~gate & (c['MA_S'] < c['MA_L']))
        return pd.DataFrame({'buy': buy.fillna(False), 'sell': sell.fillna(False)}, index=c.index)

    @property
    def name(self):
        return f"指数轮动({self.short}-{self.long})"
