import pandas as pd
import numpy as np
from stock_analysis.indicators import calc_rsi, calc_cyc_cost, calc_winner_pct


class BaseStrategy:
    def __init__(self, name):
        self.name = name

    def get_state_description(self, df):
        if len(df) < 1: return "数据不足"
        curr = df.iloc[-1]
        ma_s = curr.get(f'MA{self.s}', 0)
        ma_m = curr.get(f'MA{self.m}', 0)
        ma_l = curr.get(f'MA{self.l}', 0)
        if ma_s > ma_m > ma_l:
            return f"多头排列({self.s}>{self.m}>{self.l})"
        elif ma_s < ma_m < ma_l:
            return f"空头排列({self.s}<{self.m}<{self.l})"
        elif curr['close'] > ma_l:
            return f"震荡偏多(>{self.l})"
        else:
            return f"震荡偏空(<{self.l})"

    def get_entry_price(self, df):
        return None

    def get_signals(self, df):
        raise NotImplementedError

    def backtest(self, df):
        df = df.copy()
        signals = self.generate_trade_signals(df)
        df['buy'] = signals['buy']
        df['sell'] = signals['sell']
        trades = []
        holding = False
        buy_price = 0.0
        for i in range(len(df)):
            if df['buy'].iloc[i] and not holding:
                buy_price = df['close'].iloc[i]
                holding = True
            elif df['sell'].iloc[i] and holding:
                sell_price = df['close'].iloc[i]
                pct = (sell_price - buy_price) / buy_price
                trades.append(pct)
                holding = False
        if not trades:
            return 0, 0, 0.0
        wins = sum(1 for t in trades if t > 0)
        win_rate = wins / len(trades)
        avg_return = np.mean(trades)
        score = win_rate * 100 + avg_return * 100
        return score, len(trades), win_rate

    def generate_trade_signals(self, df):
        raise NotImplementedError


class TrendStrategy(BaseStrategy):
    def __init__(self, short_w=5, mid_w=13, long_w=34):
        super().__init__("Trend_MA")
        self.s = short_w
        self.m = mid_w
        self.l = long_w

    def get_entry_price(self, df):
        if len(df) < 1: return None
        curr = df.iloc[-1]
        ma_mid = curr.get(f'MA{self.m}', None)
        if ma_mid is None:
             ma_mid = df['close'].rolling(self.m).mean().iloc[-1]
        return ma_mid

    def generate_trade_signals(self, df):
        df['MA_S'] = df['close'].rolling(self.s).mean()
        df['MA_M'] = df['close'].rolling(self.m).mean()
        df['MA_L'] = df['close'].rolling(self.l).mean()
        df['Vol_MA5'] = df['volume'].rolling(5).mean()
        vol_active = df['volume'] > df['Vol_MA5']
        cross_gold = (df['MA_S'] > df['MA_M']) & (df['MA_S'].shift(1) <= df['MA_M'].shift(1))
        cross_dead = (df['MA_S'] < df['MA_M']) & (df['MA_S'].shift(1) >= df['MA_M'].shift(1))
        bull_trend = df['close'] > df['MA_L']
        perfect_align = (df['MA_S'] > df['MA_M']) & (df['MA_M'] > df['MA_L'])
        dip_buy = perfect_align & (df['close'] < df['MA_M'] * 1.01) & (df['close'] > df['MA_M'] * 0.95)
        buy_signal = (cross_gold & bull_trend & vol_active) | dip_buy
        return pd.DataFrame({'buy': buy_signal, 'sell': cross_dead}, index=df.index)

    def get_signals(self, df):
        if len(df) < 60: return {"buy":[], "sell":[], "info":[]}
        for w in [self.s, self.m, self.l]:
            col = f'MA{w}'
            if col not in df.columns:
                df[col] = df['close'].rolling(w).mean()
        curr = df.iloc[-1]
        prev = df.iloc[-2]
        ma_s = curr[f'MA{self.s}']
        ma_m = curr[f'MA{self.m}']
        ma_l = curr[f'MA{self.l}']
        res = {"buy":[], "sell":[], "info":[]}
        if ma_s > ma_m > ma_l:
            res['info'].append(f"均线多头排列({self.s}/{self.m}/{self.l})")
            if curr['close'] < ma_m * 1.01 and curr['close'] > ma_l:
                res['buy'].append("多头排列回调(逢低吸纳)")
        cross_gold = (curr[f'MA{self.s}'] > curr[f'MA{self.m}']) and (prev[f'MA{self.s}'] <= prev[f'MA{self.m}'])
        vol_ma5 = df['volume'].rolling(5).mean().iloc[-1]
        is_active = curr['volume'] > vol_ma5
        if cross_gold:
            if is_active:
                res['buy'].append("均线放量金叉(量价配合)")
            else:
                res['info'].append("均线金叉但缩量(需补量)")
        return res


class VolumeStrategy(BaseStrategy):
    def __init__(self):
        super().__init__("Volume_Breakout")

    def get_state_description(self, df):
        if len(df) < 20: return "数据不足"
        curr = df.iloc[-1]
        if 'OBV' not in df.columns:
            change = np.sign(df['close'].diff()).fillna(0)
            df['OBV'] = (change * df['volume']).cumsum()
        obv_ma = df['OBV'].rolling(20).mean().iloc[-1]
        curr_obv = df['OBV'].iloc[-1]
        state = ""
        if curr_obv > obv_ma:
            state += "OBV趋势向上"
        else:
            state += "OBV趋势向下"
        ma20 = df['close'].rolling(20).mean().iloc[-1]
        if curr['close'] > ma20:
            state += " | 价格强势"
        else:
            state += " | 价格弱势"
        return state

    def get_entry_price(self, df):
        if len(df) < 1: return None
        return df['close'].rolling(20).mean().iloc[-1]

    def generate_trade_signals(self, df):
        df['MA20'] = df['close'].rolling(20).mean()
        df['Vol_MA20'] = df['volume'].rolling(20).mean()
        change = np.sign(df['close'].diff()).fillna(0)
        df['OBV'] = (change * df['volume']).cumsum()
        df['OBV_MA20'] = df['OBV'].rolling(20).mean()
        if 'RSI' not in df.columns:
            df['RSI'] = calc_rsi(df['close'])
        price_cond = df['close'] > df['MA20']
        vol_cond = df['volume'] > 1.5 * df['Vol_MA20']
        obv_cond = df['OBV'] > df['OBV_MA20']
        rsi_cond = (df['RSI'] > 50) & (df['RSI'] < 80)
        buy = price_cond & vol_cond & obv_cond & rsi_cond
        sell = df['close'] < df['MA20']
        return pd.DataFrame({'buy': buy, 'sell': sell}, index=df.index)

    def get_signals(self, df):
        curr = df.iloc[-1]
        res = {"buy":[], "sell":[], "info":[]}
        vol_ma = df['volume'].rolling(20).mean().iloc[-1]
        if curr['volume'] > 1.5 * vol_ma:
            change = np.sign(df['close'].diff()).fillna(0)
            obv = (change * df['volume']).cumsum()
            obv_ma = obv.rolling(20).mean()
            if obv.iloc[-1] > obv_ma.iloc[-1]:
                res['buy'].append("放量突破且OBV向上(真突破)")
            else:
                res['info'].append("放量但OBV背离(可能诱多)")
        if curr.get('RSI', 50) > 50:
            res['info'].append("RSI处于强势区间")
        return res
