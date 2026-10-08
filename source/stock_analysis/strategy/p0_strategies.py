"""P0 阶段策略：指标已存在或计算简单，直接接入 BaseStrategy 接口。

包含：
  - KdjStrategy        KDJ 金叉死叉
  - ObvStrategy        OBV 趋势跟随
  - VrStrategy         VR 容量比率
  - KellyStrategy      Kelly 公式仓位过滤
  - DonchianStrategy   唐奇安通道海龟交易法
  - AdxStrategy         ADX 趋势强度过滤
  - AtrChannelStrategy ATR 通道突破
"""
import pandas as pd
import numpy as np
from stock_analysis.indicators import (
    calc_kdj, calc_obv, calc_vr, calc_atr, calc_adx, calc_kelly_criterion,
)
from stock_analysis.strategy.base import BaseStrategy


class KdjStrategy(BaseStrategy):
    """KDJ 金叉死叉：超卖区金叉买入，超买区死叉卖出。"""

    def __init__(self, n=9, m1=3, m2=3, buy_k=20, sell_k=80):
        super().__init__("KDJ")
        self.n = n
        self.m1 = m1
        self.m2 = m2
        self.buy_k = buy_k
        self.sell_k = sell_k

    def generate_trade_signals(self, df):
        k, d, j = calc_kdj(
            df['high'], df['low'], df['close'], n=self.n, m1=self.m1, m2=self.m2
        )
        df = df.copy()
        df['K'] = k
        df['D'] = d
        df['J'] = j
        gold = (df['K'] > df['D']) & (df['K'].shift(1) <= df['D'].shift(1))
        dead = (df['K'] < df['D']) & (df['K'].shift(1) >= df['D'].shift(1))
        buy = gold & (df['K'] < self.buy_k)
        sell = dead & (df['K'] > self.sell_k)
        return pd.DataFrame({'buy': buy, 'sell': sell}, index=df.index)


class ObvStrategy(BaseStrategy):
    """OBV 趋势跟随：OBV 上穿均线买入，下穿卖出。"""

    def __init__(self, obv_ma=20):
        super().__init__("OBV")
        self.obv_ma = obv_ma

    def generate_trade_signals(self, df):
        obv = calc_obv(df['close'], df['volume'])
        df = df.copy()
        df['OBV'] = obv
        df['OBV_MA'] = obv.rolling(self.obv_ma).mean()
        buy = (df['OBV'] > df['OBV_MA']) & (df['OBV'].shift(1) <= df['OBV_MA'].shift(1))
        sell = (df['OBV'] < df['OBV_MA']) & (df['OBV'].shift(1) >= df['OBV_MA'].shift(1))
        return pd.DataFrame({'buy': buy, 'sell': sell}, index=df.index)


class VrStrategy(BaseStrategy):
    """VR 容量比率：VR 低位超卖买入，高位超买卖出。"""

    def __init__(self, window=26, buy_vr=70, sell_vr=150):
        super().__init__("VR")
        self.window = window
        self.buy_vr = buy_vr
        self.sell_vr = sell_vr

    def generate_trade_signals(self, df):
        vr = calc_vr(df['close'], df['volume'], window=self.window)
        df = df.copy()
        df['VR'] = vr
        buy = (df['VR'] < self.buy_vr) & (df['VR'].shift(1) >= self.buy_vr)
        sell = (df['VR'] > self.sell_vr) & (df['VR'].shift(1) <= self.sell_vr)
        return pd.DataFrame({'buy': buy, 'sell': sell}, index=df.index)


class KellyStrategy(BaseStrategy):
    """Kelly 公式仓位过滤：基于滚动窗口的胜率/盈亏比计算 Kelly 分数，
    仅当 Kelly 分数为正时跟随均线趋势信号。"""

    def __init__(self, ma_short=5, ma_long=20, lookback=60, kelly_threshold=0.0):
        super().__init__("Kelly")
        self.ma_short = ma_short
        self.ma_long = ma_long
        self.lookback = lookback
        self.kelly_threshold = kelly_threshold

    def _rolling_kelly(self, close: pd.Series) -> pd.Series:
        """根据滚动窗口内的日收益率估算 Kelly 分数。"""
        rets = close.pct_change()
        wins = rets.clip(lower=0)
        losses = (-rets).clip(lower=0)
        win_rate = (rets > 0).rolling(self.lookback).mean()
        avg_win = wins.rolling(self.lookback).mean()
        avg_loss = losses.rolling(self.lookback).mean()
        b = avg_win / avg_loss.replace(0, np.nan)
        kelly = (win_rate * b - (1 - win_rate)) / b.replace(0, np.nan)
        return kelly.fillna(0)

    def generate_trade_signals(self, df):
        df = df.copy()
        df['MA_S'] = df['close'].rolling(self.ma_short).mean()
        df['MA_L'] = df['close'].rolling(self.ma_long).mean()
        df['Kelly'] = self._rolling_kelly(df['close'])
        gold = (df['MA_S'] > df['MA_L']) & (df['MA_S'].shift(1) <= df['MA_L'].shift(1))
        dead = (df['MA_S'] < df['MA_L']) & (df['MA_S'].shift(1) >= df['MA_L'].shift(1))
        kelly_ok = df['Kelly'] > self.kelly_threshold
        buy = gold & kelly_ok
        sell = dead | (~kelly_ok & (df['MA_S'] < df['MA_L']))
        return pd.DataFrame({'buy': buy, 'sell': sell}, index=df.index)


class DonchianStrategy(BaseStrategy):
    """唐奇安通道海龟交易法：突破 N 日高点买入，跌破 M 日低点卖出。"""

    def __init__(self, entry_n=20, exit_n=10):
        super().__init__("Donchian")
        self.entry_n = entry_n
        self.exit_n = exit_n

    def generate_trade_signals(self, df):
        df = df.copy()
        upper = df['high'].rolling(self.entry_n).max().shift(1)
        lower = df['low'].rolling(self.exit_n).min().shift(1)
        buy = df['close'] > upper
        sell = df['close'] < lower
        return pd.DataFrame({'buy': buy, 'sell': sell}, index=df.index)


class AdxStrategy(BaseStrategy):
    """ADX 趋势强度过滤：ADX 高于阈值且 DI+ > DI- 时跟随多头，反之空头。"""

    def __init__(self, adx_period=14, adx_threshold=25):
        super().__init__("ADX")
        self.adx_period = adx_period
        self.adx_threshold = adx_threshold

    def generate_trade_signals(self, df):
        adx, plus_di, minus_di = calc_adx(
            df['high'], df['low'], df['close'], period=self.adx_period
        )
        df = df.copy()
        df['ADX'] = adx
        df['DI_PLUS'] = plus_di
        df['DI_MINUS'] = minus_di
        strong_trend = df['ADX'] > self.adx_threshold
        bull = df['DI_PLUS'] > df['DI_MINUS']
        bear = df['DI_MINUS'] > df['DI_PLUS']
        buy = strong_trend & bull & (df['DI_PLUS'].shift(1) <= df['DI_MINUS'].shift(1))
        sell = strong_trend & bear & (df['DI_MINUS'].shift(1) <= df['DI_PLUS'].shift(1))
        return pd.DataFrame({'buy': buy, 'sell': sell}, index=df.index)


class AtrChannelStrategy(BaseStrategy):
    """ATR 通道突破：MA ± multiplier × ATR，突破上轨买入，跌破下轨卖出。"""

    def __init__(self, atr_period=14, ma_period=20, multiplier=3.0):
        super().__init__("ATR_Channel")
        self.atr_period = atr_period
        self.ma_period = ma_period
        self.multiplier = multiplier

    def generate_trade_signals(self, df):
        atr = calc_atr(df['high'], df['low'], df['close'], period=self.atr_period)
        df = df.copy()
        df['ATR'] = atr
        df['MA'] = df['close'].rolling(self.ma_period).mean()
        df['UPPER'] = df['MA'] + self.multiplier * df['ATR']
        df['LOWER'] = df['MA'] - self.multiplier * df['ATR']
        buy = df['close'] > df['UPPER']
        sell = df['close'] < df['LOWER']
        return pd.DataFrame({'buy': buy, 'sell': sell}, index=df.index)
