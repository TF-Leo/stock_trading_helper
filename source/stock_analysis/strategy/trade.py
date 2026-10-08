import pandas as pd
import numpy as np
from stock_analysis.indicators import calc_rsi, calc_macd, calc_boll
from stock_analysis.strategy.base import BaseStrategy


class BollStrategy(BaseStrategy):
    def __init__(self, window=20, num_std=2):
        super().__init__("Boll_Band")
        self.window = window
        self.num_std = num_std

    def get_state_description(self, df):
        if len(df) < self.window: return "数据不足"
        upper, mid, lower = calc_boll(df['close'], window=self.window, num_std=self.num_std)
        curr = df.iloc[-1]
        upper = upper.iloc[-1]
        lower = lower.iloc[-1]
        mid = mid.iloc[-1]
        width = (upper - lower) / mid * 100
        state = ""
        if curr['close'] > upper:
            state += "突破上轨(超买)"
        elif curr['close'] < lower:
            state += "跌破下轨(超卖)"
        elif curr['close'] > mid:
            state += "中轨上方(偏多)"
        else:
            state += "中轨下方(偏空)"
        state += f" | 带宽:{width:.1f}%"
        return state

    def get_entry_price(self, df):
        if len(df) < self.window: return None
        _, _, lower = calc_boll(df['close'], window=self.window, num_std=self.num_std)
        return lower.iloc[-1]

    def generate_trade_signals(self, df):
        upper, mid, lower = calc_boll(df['close'], window=self.window, num_std=self.num_std)
        df = df.copy()
        df['BOLL_Upper'] = upper
        df['BOLL_Lower'] = lower
        df['BOLL_Mid'] = mid
        buy = df['close'] < df['BOLL_Lower']
        sell = df['close'] > df['BOLL_Upper']
        return pd.DataFrame({'buy': buy, 'sell': sell}, index=df.index)

    def get_signals(self, df):
        if len(df) < self.window: return {"buy":[], "sell":[], "info":[]}
        upper, mid, lower = calc_boll(df['close'], window=self.window, num_std=self.num_std)
        curr = df.iloc[-1]
        res = {"buy":[], "sell":[], "info":[]}
        if curr['close'] < lower.iloc[-1]:
            res['buy'].append("触及布林下轨(超卖反弹)")
        if curr['close'] > upper.iloc[-1]:
            res['sell'].append("触及布林上轨(超买回落)")
        width = (upper.iloc[-1] - lower.iloc[-1]) / mid.iloc[-1] * 100
        if width < 5:
            res['info'].append("布林带收窄(变盘在即)")
        return res


class MacdStrategy(BaseStrategy):
    def __init__(self, fast=12, slow=26, signal=9):
        super().__init__("MACD")
        self.fast = fast
        self.slow = slow
        self.signal = signal

    def get_state_description(self, df):
        if len(df) < self.slow + self.signal: return "数据不足"
        dif, dea, hist = calc_macd(df['close'], fast=self.fast, slow=self.slow, signal=self.signal)
        dif = dif.iloc[-1]
        dea = dea.iloc[-1]
        hist = hist.iloc[-1]
        if dif > dea and hist > 0:
            return "MACD多头(DIF>DEA, 红柱)"
        elif dif > dea and hist <= 0:
            return "MACD转多(DIF>DEA, 绿柱缩短)"
        elif dif < dea and hist < 0:
            return "MACD空头(DIF<DEA, 绿柱)"
        else:
            return "MACD转空(DIF<DEA, 红柱缩短)"

    def get_entry_price(self, df):
        return None

    def generate_trade_signals(self, df):
        dif, dea, hist = calc_macd(df['close'], fast=self.fast, slow=self.slow, signal=self.signal)
        df = df.copy()
        df['DIF'] = dif
        df['DEA'] = dea
        df['MACD_Hist'] = hist
        gold_cross = (df['DIF'] > df['DEA']) & (df['DIF'].shift(1) <= df['DEA'].shift(1))
        dead_cross = (df['DIF'] < df['DEA']) & (df['DIF'].shift(1) >= df['DEA'].shift(1))
        buy = gold_cross & (df['DIF'] < 0)
        sell = dead_cross & (df['DIF'] > 0)
        return pd.DataFrame({'buy': buy, 'sell': sell}, index=df.index)

    def get_signals(self, df):
        if len(df) < self.slow + self.signal: return {"buy":[], "sell":[], "info":[]}
        dif_s, dea_s, hist_s = calc_macd(df['close'], fast=self.fast, slow=self.slow, signal=self.signal)
        dif = dif_s.iloc[-1]
        dea = dea_s.iloc[-1]
        hist = hist_s.iloc[-1]
        prev_hist = hist_s.iloc[-2] if len(hist_s) > 1 else 0
        res = {"buy":[], "sell":[], "info":[]}
        if dif > dea and prev_hist <= 0 < hist:
            res['buy'].append("MACD金叉(买入信号)")
        if dif < 0 and dea < 0 and hist > prev_hist:
            res['buy'].append("零轴下方绿柱缩短(底部信号)")
        if dif < dea and prev_hist >= 0 > hist:
            res['sell'].append("MACD死叉(卖出信号)")
        if dif > 0 and dea > 0 and hist < prev_hist:
            res['sell'].append("零轴上方红柱缩短(顶部信号)")
        return res


def analyze_signals(df, strategies=None):
    if strategies is None:
        from stock_analysis.strategy.base import TrendStrategy, VolumeStrategy
        from stock_analysis.strategy.rules import ChipStrategy
        from stock_analysis.strategy.trade import BollStrategy, MacdStrategy
        strategies = [
            TrendStrategy(),
            VolumeStrategy(),
            ChipStrategy(),
            BollStrategy(),
            MacdStrategy(),
        ]
    all_signals = {"buy": [], "sell": [], "info": []}
    for strategy in strategies:
        signals = strategy.get_signals(df)
        for key in all_signals:
            all_signals[key].extend(signals.get(key, []))
    return all_signals
