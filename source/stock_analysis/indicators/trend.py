import pandas as pd
import numpy as np


def calc_ma(series, window):
    return series.rolling(window=window).mean()


def calc_macd(series, fast=12, slow=26, signal=9):
    exp1 = series.ewm(span=fast, adjust=False).mean()
    exp2 = series.ewm(span=slow, adjust=False).mean()
    macd = exp1 - exp2
    signal_line = macd.ewm(span=signal, adjust=False).mean()
    hist = (macd - signal_line) * 2
    return macd, signal_line, hist


def calc_kdj(high, low, close, n=9, m1=3, m2=3):
    low_n = low.rolling(n).min()
    high_n = high.rolling(n).max()
    rsv = (close - low_n) / (high_n - low_n) * 100
    k = rsv.ewm(com=m1 - 1, adjust=False).mean()
    d = k.ewm(com=m2 - 1, adjust=False).mean()
    j = 3 * k - 2 * d
    return k, d, j


def calc_boll(close, window=20, num_std=2):
    mid = close.rolling(window).mean()
    std = close.rolling(window).std()
    upper = mid + std * num_std
    lower = mid - std * num_std
    return upper, mid, lower


def add_all_indicators(df):
    df = df.copy()
    for w in [5, 10, 20, 60]:
        df[f'MA{w}'] = calc_ma(df['close'], w)
    df['DIF'], df['DEA'], df['MACD'] = calc_macd(df['close'])
    df['K'], df['D'], df['J'] = calc_kdj(df['high'], df['low'], df['close'])
    df['RSI'] = calc_rsi(df['close'])
    df['BOLL_UPPER'], df['BOLL_MID'], df['BOLL_LOWER'] = calc_boll(df['close'])
    df['OBV'] = calc_obv(df['close'], df['volume'])
    df['VR'] = calc_vr(df['close'], df['volume'])
    if 'turnover' in df.columns:
        to_rate = df['turnover'] / 100
    else:
        to_rate = pd.Series(0.01, index=df.index)
    df['CYC_Cost'] = calc_cyc_cost(df['close'], to_rate)
    df['MA_Score'] = (
        (df['MA5'] > df['MA10']).astype(int)
        + (df['MA10'] > df['MA20']).astype(int)
        + (df['MA20'] > df['MA60']).astype(int)
    )
    return df


def calc_ema(series, window):
    """指数加权移动平均。"""
    return series.ewm(span=window, adjust=False).mean()


def _wma_vec(s: pd.Series, window: int) -> pd.Series:
    """P2向量化WMA：weights=1..window，一次卷积完成，避免rolling.apply逐窗python循环。"""
    if window <= 1:
        return s.copy()
    w = np.arange(1, window + 1, dtype=float)
    denom = w.sum()
    vals = s.to_numpy(dtype=float)
    # cumsum技巧：加权滑动和
    out = np.full_like(vals, np.nan)
    cumsum = np.cumsum(np.insert(vals, 0, 0.0))
    # 加权和无法直接cumsum，需卷积；n<10000时np.convolve足够快
    wsum = np.convolve(vals, w[::-1], mode="valid")
    # valid长度 n-window+1，对齐到尾部
    out[window - 1:] = wsum / denom
    # 前window-1用实际可用长度归一化（与pandas行为近似，保持NaN语义则保留NaN）
    return pd.Series(out, index=s.index)


def calc_kama(close, period=10, fast=2, slow=30):
    """Kaufman 自适应移动平均。

    根据市场效率自适应调整平滑系数：
    - 趋势明确时接近 fast EMA（反应快）
    - 震荡时接近 slow EMA（过滤噪音）
    P2: numpy数组循环替代Series.iloc逐行（~10x提速），数学等价。
    """
    change = (close - close.shift(period)).abs()
    volatility = close.diff().abs().rolling(period).sum()
    er = change / volatility.replace(0, np.nan)  # 效率系数
    fast_sc = 2 / (fast + 1)
    slow_sc = 2 / (slow + 1)
    sc = (er * (fast_sc - slow_sc) + slow_sc) ** 2  # 平滑系数
    c = close.to_numpy(dtype=float)
    s = sc.to_numpy(dtype=float)
    n = len(c)
    out = np.empty(n, dtype=float)
    out[:period] = c[:period]
    for i in range(period, n):
        prev = out[i - 1]
        ci, si = c[i], s[i]
        if np.isnan(prev) or np.isnan(si):
            out[i] = ci
        else:
            out[i] = prev + si * (ci - prev)
    return pd.Series(out, index=close.index)


def calc_hma(close, window):
    """Hull Moving Average：减少滞后性的加权均线。P2向量化WMA实现等价加速。"""
    half = max(1, window // 2)
    wma1 = _wma_vec(close, half)
    wma2 = _wma_vec(close, window)
    diff = 2 * wma1 - wma2
    sqrt_n = max(1, int(np.sqrt(window)))
    return _wma_vec(diff, sqrt_n)


def calc_vwap(high, low, close, volume):
    """成交量加权均价（典型价格 × 成交量累计 / 成交量累计）。"""
    typical = (high + low + close) / 3
    cum_vp = (typical * volume).cumsum()
    cum_vol = volume.cumsum().replace(0, np.nan)
    return cum_vp / cum_vol


from stock_analysis.indicators.momentum import calc_rsi, calc_obv, calc_vr, detect_volume_divergence
from stock_analysis.indicators.volatility import calc_cyc_cost, calc_winner_pct, calc_atr, calc_kelly_criterion, add_advanced_indicators
