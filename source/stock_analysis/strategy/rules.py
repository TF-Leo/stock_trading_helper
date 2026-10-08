import pandas as pd
import numpy as np
from stock_analysis.indicators import calc_rsi, calc_cyc_cost, calc_winner_pct
from stock_analysis.strategy.base import BaseStrategy


class ChipStrategy(BaseStrategy):
    def __init__(self, winner_lower=30, winner_upper=85):
        super().__init__("Chip_Analysis")
        self.winner_lower = winner_lower
        self.winner_upper = winner_upper

    def _calc_chip(self, df):
        """返回 (cyc_cost_series, winner_pct_series)，与 add_all_indicators 一致。"""
        if 'turnover' in df.columns:
            to_rate = df['turnover'] / 100
        else:
            to_rate = pd.Series(0.01, index=df.index)
        cyc_cost = calc_cyc_cost(df['close'], to_rate)
        winner = calc_winner_pct(df['close'], cyc_cost)
        return cyc_cost, winner

    def get_state_description(self, df):
        if len(df) < 90: return "数据不足"
        cyc_cost, winner = self._calc_chip(df)
        curr = df.iloc[-1]
        c, w = cyc_cost.iloc[-1], winner.iloc[-1]
        state = ""
        if curr['close'] < c * 0.95:
            state += "深度套牢区(筹码密集下方)"
        elif curr['close'] < c:
            state += "浅套区(接近筹码密集区)"
        elif curr['close'] < c * 1.05:
            state += "筹码密集区(多空分歧大)"
        else:
            state += "获利区(筹码密集上方)"
        state += f" | 获利盘:{w:.1f}%"
        return state

    def get_entry_price(self, df):
        if len(df) < 90: return None
        cyc_cost, _ = self._calc_chip(df)
        return cyc_cost.iloc[-1]

    def generate_trade_signals(self, df):
        cyc_cost, winner = self._calc_chip(df)
        df = df.copy()
        df['Cyc_Cost'] = cyc_cost
        df['Winner_Pct'] = winner
        buy = (df['close'] < df['Cyc_Cost'] * 1.02) & (df['Winner_Pct'] < self.winner_lower)
        sell = df['Winner_Pct'] > self.winner_upper
        return pd.DataFrame({'buy': buy, 'sell': sell}, index=df.index)

    def get_signals(self, df):
        if len(df) < 90: return {"buy":[], "sell":[], "info":[]}
        cyc_cost, winner = self._calc_chip(df)
        curr = df.iloc[-1]
        c, w = cyc_cost.iloc[-1], winner.iloc[-1]
        res = {"buy":[], "sell":[], "info":[]}
        if curr['close'] < c and w < self.winner_lower:
            res['buy'].append(f"筹码密集区下方(获利盘仅{w:.0f}%)")
        elif curr['close'] < c * 1.02 and w < self.winner_lower + 10:
            res['buy'].append("接近筹码密集区支撑")
        if w > self.winner_upper:
            res['sell'].append(f"获利盘过高({w:.0f}%)，注意风险")
        res['info'].append(f"筹码成本:{c:.2f} 获利盘:{w:.1f}%")
        return res


class ComprehensiveStrategy(BaseStrategy):
    def __init__(self):
        super().__init__("Comprehensive")
        self.trend = None
        self.volume = None
        self.chip = None

    def _init_sub(self):
        from stock_analysis.strategy.base import TrendStrategy, VolumeStrategy
        from stock_analysis.strategy.rules import ChipStrategy
        if self.trend is None:
            self.trend = TrendStrategy()
            self.volume = VolumeStrategy()
            self.chip = ChipStrategy()

    def generate_trade_signals(self, df):
        self._init_sub()
        t_sig = self.trend.generate_trade_signals(df)
        v_sig = self.volume.generate_trade_signals(df)
        c_sig = self.chip.generate_trade_signals(df)
        buy = t_sig['buy'] & v_sig['buy']
        sell = t_sig['sell'] | c_sig['sell']
        return pd.DataFrame({'buy': buy, 'sell': sell}, index=df.index)

    def get_signals(self, df):
        self._init_sub()
        res = {"buy":[], "sell":[], "info":[]}
        t = self.trend.get_signals(df)
        v = self.volume.get_signals(df)
        c = self.chip.get_signals(df)
        res['buy'].extend(t.get('buy', []))
        res['buy'].extend(v.get('buy', []))
        res['buy'].extend(c.get('buy', []))
        res['sell'].extend(t.get('sell', []))
        res['sell'].extend(c.get('sell', []))
        res['info'].extend(t.get('info', []))
        res['info'].extend(v.get('info', []))
        res['info'].extend(c.get('info', []))
        return res
