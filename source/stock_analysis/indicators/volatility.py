import pandas as pd
import numpy as np


def calc_cyc_cost(close, turnover_rate):
    turnover = turnover_rate.clip(0, 1)
    price_values = close.values
    turnover_values = turnover.values
    cyc_values = np.zeros_like(price_values)
    cyc_values[0] = price_values[0]
    for i in range(1, len(price_values)):
        t = turnover_values[i]
        p = price_values[i]
        c_prev = cyc_values[i - 1]
        cyc_values[i] = c_prev * (1 - t) + p * t
    return pd.Series(cyc_values, index=close.index)


def calc_winner_pct(close, cyc_cost):
    bias = (close - cyc_cost) / cyc_cost
    winner_pct = 1 / (1 + np.exp(-10 * bias)) * 100
    return winner_pct


def calc_atr(high, low, close, period=14):
    tr1 = high - low
    tr2 = abs(high - close.shift(1))
    tr3 = abs(low - close.shift(1))
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    atr = tr.rolling(period).mean()
    return atr


def calc_adx(high, low, close, period=14):
    """计算 ADX / DI+ / DI-。

    Returns:
        (adx, plus_di, minus_di) 三个 pd.Series
    """
    up_move = high.diff()
    down_move = -low.diff()
    plus_dm = up_move.where((up_move > down_move) & (up_move > 0), 0.0)
    minus_dm = down_move.where((down_move > up_move) & (down_move > 0), 0.0)
    tr1 = high - low
    tr2 = abs(high - close.shift(1))
    tr3 = abs(low - close.shift(1))
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    atr = tr.ewm(alpha=1 / period, adjust=False).mean()
    plus_di = 100 * plus_dm.ewm(alpha=1 / period, adjust=False).mean() / atr.replace(0, np.nan)
    minus_di = 100 * minus_dm.ewm(alpha=1 / period, adjust=False).mean() / atr.replace(0, np.nan)
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    adx = dx.ewm(alpha=1 / period, adjust=False).mean()
    return adx, plus_di.fillna(0), minus_di.fillna(0)


def calc_kelly_criterion(win_rate, avg_win, avg_loss):
    if avg_loss == 0:
        return 0
    b = avg_win / avg_loss
    p = win_rate
    q = 1 - p
    kelly = (p * b - q) / b
    half_kelly = max(0, kelly * 0.5)
    return min(half_kelly, 1.0)


def add_advanced_indicators(df):
    df = df.copy()
    df['ATR'] = calc_atr(df['high'], df['low'], df['close'])
    df['ATR_Pct'] = df['ATR'] / df['close']
    vol_ma = df['volume'].rolling(20).mean()
    vol_std = df['volume'].rolling(20).std()
    df['Vol_ZScore'] = (df['volume'] - vol_ma) / vol_std
    df['Momentum'] = df['close'] / df['close'].shift(10) - 1
    df['Volatility'] = df['close'].pct_change().rolling(20).std() * np.sqrt(252)
    return df
