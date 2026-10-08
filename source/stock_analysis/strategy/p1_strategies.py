"""P1 阶段策略：需新增指标函数（已实现），接入 BaseStrategy 接口。

包含：
  - EmaCrossStrategy       EMA 双均线交叉
  - KamaStrategy            KAMA 自适应均线趋势
  - HmaCrossStrategy        HMA 双均线交叉
  - VwapStrategy            VWAP 趋势跟随
  - BollSqueezeStrategy     布林带收缩后突破
  - AtrFilterStrategy       ATR 波动率过滤（高波动空仓）
  - ZscoreStrategy          Z-Score 均值回归
  - WilliamsRStrategy       Williams %R 超买超卖
  - CciStrategy             CCI 顺势指标
  - StochRsiStrategy        Stochastic RSI
  - SarStrategy             抛物线 SAR 趋势跟踪
  - DiCrossStrategy         DI+/DI- 交叉
"""
import pandas as pd
import numpy as np
from stock_analysis.indicators import (
    calc_ema, calc_kama, calc_hma, calc_vwap,
    calc_boll, calc_atr, calc_adx,
    calc_rsi, calc_williams_r, calc_cci, calc_stoch_rsi, calc_sar,
)
from stock_analysis.strategy.base import BaseStrategy


class EmaCrossStrategy(BaseStrategy):
    """EMA 双均线交叉：短期上穿长期买入，下穿卖出。"""

    def __init__(self, fast=5, slow=20):
        super().__init__("EMA交叉")
        self.fast = fast
        self.slow = slow

    def generate_trade_signals(self, df):
        df = df.copy()
        df['EMA_F'] = calc_ema(df['close'], self.fast)
        df['EMA_S'] = calc_ema(df['close'], self.slow)
        buy = (df['EMA_F'] > df['EMA_S']) & (df['EMA_F'].shift(1) <= df['EMA_S'].shift(1))
        sell = (df['EMA_F'] < df['EMA_S']) & (df['EMA_F'].shift(1) >= df['EMA_S'].shift(1))
        return pd.DataFrame({'buy': buy, 'sell': sell}, index=df.index)


class KamaStrategy(BaseStrategy):
    """KAMA 自适应均线趋势：close 上穿 KAMA 买入，下穿卖出。"""

    def __init__(self, period=10, fast=2, slow=30):
        super().__init__("KAMA")
        self.period = period
        self.fast = fast
        self.slow = slow

    def generate_trade_signals(self, df):
        df = df.copy()
        df['KAMA'] = calc_kama(df['close'], period=self.period, fast=self.fast, slow=self.slow)
        buy = (df['close'] > df['KAMA']) & (df['close'].shift(1) <= df['KAMA'].shift(1))
        sell = (df['close'] < df['KAMA']) & (df['close'].shift(1) >= df['KAMA'].shift(1))
        return pd.DataFrame({'buy': buy, 'sell': sell}, index=df.index)


class HmaCrossStrategy(BaseStrategy):
    """HMA 双均线交叉：减少滞后性的均线交叉。"""

    def __init__(self, fast=10, slow=30):
        super().__init__("HMA交叉")
        self.fast = fast
        self.slow = slow

    def generate_trade_signals(self, df):
        df = df.copy()
        df['HMA_F'] = calc_hma(df['close'], self.fast)
        df['HMA_S'] = calc_hma(df['close'], self.slow)
        buy = (df['HMA_F'] > df['HMA_S']) & (df['HMA_F'].shift(1) <= df['HMA_S'].shift(1))
        sell = (df['HMA_F'] < df['HMA_S']) & (df['HMA_F'].shift(1) >= df['HMA_S'].shift(1))
        return pd.DataFrame({'buy': buy, 'sell': sell}, index=df.index)


class VwapStrategy(BaseStrategy):
    """VWAP 趋势跟随：close 上穿 VWAP 买入，下穿卖出。"""

    def __init__(self, rolling_window=None):
        super().__init__("VWAP")
        self.rolling_window = rolling_window

    def generate_trade_signals(self, df):
        df = df.copy()
        vwap = calc_vwap(df['high'], df['low'], df['close'], df['volume'])
        if self.rolling_window:
            vwap = vwap.rolling(self.rolling_window).mean()
        df['VWAP'] = vwap
        buy = (df['close'] > df['VWAP']) & (df['close'].shift(1) <= df['VWAP'].shift(1))
        sell = (df['close'] < df['VWAP']) & (df['close'].shift(1) >= df['VWAP'].shift(1))
        return pd.DataFrame({'buy': buy, 'sell': sell}, index=df.index)


class BollSqueezeStrategy(BaseStrategy):
    """布林带收缩后突破：带宽低位时，突破上轨买入，跌破下轨卖出。"""

    def __init__(self, window=20, num_std=2.0, squeeze_pct=0.1):
        super().__init__("布林Squeeze")
        self.window = window
        self.num_std = num_std
        self.squeeze_pct = squeeze_pct

    def generate_trade_signals(self, df):
        upper, mid, lower = calc_boll(df['close'], window=self.window, num_std=self.num_std)
        df = df.copy()
        df['UPPER'] = upper
        df['MID'] = mid
        df['LOWER'] = lower
        df['WIDTH'] = (upper - lower) / mid.replace(0, np.nan)
        # P0: 消除前视 — 原rank(pct=True)用全样本未来数据，改为252日滚动分位（仅用过去）
        width_pct = (
            df['WIDTH']
            .rolling(252, min_periods=20)
            .apply(lambda x: pd.Series(x).rank(pct=True).iloc[-1], raw=False)
        )
        squeezed = width_pct < self.squeeze_pct
        buy = squeezed & (df['close'] > df['UPPER'])
        sell = squeezed & (df['close'] < df['LOWER'])
        return pd.DataFrame({'buy': buy.fillna(False), 'sell': sell.fillna(False)}, index=df.index)


class AtrFilterStrategy(BaseStrategy):
    """ATR 波动率过滤：高波动空仓。close 上穿 MA 且波动率适中时买入。"""

    def __init__(self, atr_period=14, ma_period=20, vol_low_pct=0.01, vol_high_pct=0.05):
        super().__init__("ATR过滤")
        self.atr_period = atr_period
        self.ma_period = ma_period
        self.vol_low_pct = vol_low_pct
        self.vol_high_pct = vol_high_pct

    def generate_trade_signals(self, df):
        atr = calc_atr(df['high'], df['low'], df['close'], period=self.atr_period)
        df = df.copy()
        df['ATR'] = atr
        df['ATR_PCT'] = atr / df['close']
        df['MA'] = df['close'].rolling(self.ma_period).mean()
        vol_ok = (df['ATR_PCT'] > self.vol_low_pct) & (df['ATR_PCT'] < self.vol_high_pct)
        buy = vol_ok & (df['close'] > df['MA']) & (df['close'].shift(1) <= df['MA'].shift(1))
        sell = (~vol_ok | (df['close'] < df['MA'])) & (df['close'].shift(1) >= df['MA'].shift(1))
        return pd.DataFrame({'buy': buy, 'sell': sell}, index=df.index)


class ZscoreStrategy(BaseStrategy):
    """Z-Score 均值回归：close 偏离 MA 超过 N 个标准差时反转。"""

    def __init__(self, window=20, entry_z=-2.0, exit_z=1.0):
        super().__init__("Z-Score")
        self.window = window
        self.entry_z = entry_z
        self.exit_z = exit_z

    def generate_trade_signals(self, df):
        df = df.copy()
        df['MA'] = df['close'].rolling(self.window).mean()
        df['STD'] = df['close'].rolling(self.window).std()
        df['Z'] = (df['close'] - df['MA']) / df['STD'].replace(0, np.nan)
        buy = df['Z'] < self.entry_z
        sell = df['Z'] > self.exit_z
        return pd.DataFrame({'buy': buy, 'sell': sell}, index=df.index)


class WilliamsRStrategy(BaseStrategy):
    """Williams %R 超买超卖：超卖买入，超买卖出。"""

    def __init__(self, period=14, buy_wr=-80, sell_wr=-20):
        super().__init__("WilliamsR")
        self.period = period
        self.buy_wr = buy_wr
        self.sell_wr = sell_wr

    def generate_trade_signals(self, df):
        wr = calc_williams_r(df['high'], df['low'], df['close'], period=self.period)
        df = df.copy()
        df['WR'] = wr
        buy = (df['WR'] < self.buy_wr) & (df['WR'].shift(1) >= self.buy_wr)
        sell = (df['WR'] > self.sell_wr) & (df['WR'].shift(1) <= self.sell_wr)
        return pd.DataFrame({'buy': buy, 'sell': sell}, index=df.index)


class CciStrategy(BaseStrategy):
    """CCI 顺势指标：超卖上穿 -100 买入，超买下穿 +100 卖出。"""

    def __init__(self, period=20, lower=-100, upper=100):
        super().__init__("CCI")
        self.period = period
        self.lower = lower
        self.upper = upper

    def generate_trade_signals(self, df):
        cci = calc_cci(df['high'], df['low'], df['close'], period=self.period)
        df = df.copy()
        df['CCI'] = cci
        buy = (df['CCI'] > self.lower) & (df['CCI'].shift(1) <= self.lower)
        sell = (df['CCI'] < self.upper) & (df['CCI'].shift(1) >= self.upper)
        return pd.DataFrame({'buy': buy, 'sell': sell}, index=df.index)


class StochRsiStrategy(BaseStrategy):
    """Stochastic RSI：超卖上穿 20 买入，超买下穿 80 卖出。"""

    def __init__(self, rsi_period=14, stoch_period=14, lower=20, upper=80):
        super().__init__("StochRSI")
        self.rsi_period = rsi_period
        self.stoch_period = stoch_period
        self.lower = lower
        self.upper = upper

    def generate_trade_signals(self, df):
        stoch = calc_stoch_rsi(df['close'], rsi_period=self.rsi_period, stoch_period=self.stoch_period)
        df = df.copy()
        df['STOCH_RSI'] = stoch
        buy = (df['STOCH_RSI'] > self.lower) & (df['STOCH_RSI'].shift(1) <= self.lower)
        sell = (df['STOCH_RSI'] < self.upper) & (df['STOCH_RSI'].shift(1) >= self.upper)
        return pd.DataFrame({'buy': buy, 'sell': sell}, index=df.index)


class SarStrategy(BaseStrategy):
    """抛物线 SAR 趋势跟踪：close 上穿 SAR 买入，下穿卖出。"""

    def __init__(self, af_start=0.02, af_max=0.2):
        super().__init__("SAR")
        self.af_start = af_start
        self.af_max = af_max

    def generate_trade_signals(self, df):
        sar = calc_sar(df['high'], df['low'], af_start=self.af_start, af_max=self.af_max)
        df = df.copy()
        df['SAR'] = sar
        buy = (df['close'] > df['SAR']) & (df['close'].shift(1) <= df['SAR'].shift(1))
        sell = (df['close'] < df['SAR']) & (df['close'].shift(1) >= df['SAR'].shift(1))
        return pd.DataFrame({'buy': buy, 'sell': sell}, index=df.index)


class DiCrossStrategy(BaseStrategy):
    """DI+/DI- 交叉：DI+ 上穿 DI- 买入，下穿卖出（需 ADX 强趋势确认）。"""

    def __init__(self, adx_period=14, adx_threshold=20):
        super().__init__("DI交叉")
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
        strong = df['ADX'] > self.adx_threshold
        buy = strong & (df['DI_PLUS'] > df['DI_MINUS']) & (df['DI_PLUS'].shift(1) <= df['DI_MINUS'].shift(1))
        sell = (df['DI_PLUS'] < df['DI_MINUS']) & (df['DI_PLUS'].shift(1) >= df['DI_MINUS'].shift(1))
        return pd.DataFrame({'buy': buy, 'sell': sell}, index=df.index)
