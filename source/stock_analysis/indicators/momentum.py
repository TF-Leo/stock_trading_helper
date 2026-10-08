import pandas as pd
import numpy as np


def calc_rsi(close, period=14):
    delta = close.diff()
    up = delta.clip(lower=0)
    down = -1 * delta.clip(upper=0)
    ema_up = up.ewm(com=period - 1, adjust=False).mean()
    ema_down = down.ewm(com=period - 1, adjust=False).mean()
    rs = ema_up / ema_down
    rsi = 100 - (100 / (1 + rs))
    return rsi


def calc_obv(close, volume):
    change = np.sign(close.diff()).fillna(0)
    obv = (change * volume).cumsum()
    return obv


def calc_vr(close, volume, window=26):
    price_diff = close.diff()
    vol_up = volume.where(price_diff > 0, 0)
    vol_down = volume.where(price_diff < 0, 0)
    vol_flat = volume.where(price_diff == 0, 0)
    sum_up = vol_up.rolling(window).sum()
    sum_down = vol_down.rolling(window).sum()
    sum_flat = vol_flat.rolling(window).sum()
    vr = (sum_up + 0.5 * sum_flat) / (sum_down + 0.5 * sum_flat) * 100
    return vr


def detect_volume_divergence(df, lookback=20, price_threshold=0.03, vol_threshold=0.15):
    if len(df) < lookback:
        return {
            'top_divergence': False, 'bottom_divergence': False,
            'fake_breakout': False, 'accumulation': False,
            'strength': 0, 'description': '数据不足',
        }

    result = {
        'top_divergence': False, 'bottom_divergence': False,
        'fake_breakout': False, 'accumulation': False,
        'strength': 0, 'description': '',
    }

    close = df['close']
    price_change = (close.iloc[-1] - close.iloc[-lookback]) / close.iloc[-lookback]
    volume = df['volume']
    vol_ma = volume.rolling(lookback).mean()

    vol_recent = volume.iloc[-lookback:].values
    vol_slope = np.polyfit(np.arange(lookback), vol_recent, 1)[0]
    vol_slope_pct = vol_slope / vol_ma.iloc[-1] if vol_ma.iloc[-1] > 0 else 0

    obv = calc_obv(close, volume)
    obv_slope = (obv.iloc[-1] - obv.iloc[-lookback]) / abs(obv.iloc[-lookback]) if obv.iloc[-lookback] != 0 else 0

    price_recent = close.iloc[-lookback:].values
    price_slope = np.polyfit(np.arange(lookback), price_recent, 1)[0]
    price_slope_pct = price_slope / close.iloc[-1] if close.iloc[-1] > 0 else 0

    vol_recent5 = volume.iloc[-5:].mean()
    vol_prev15 = volume.iloc[-20:-5].mean()
    vol_change = (vol_recent5 - vol_prev15) / vol_prev15 if vol_prev15 > 0 else 0

    price_recent5_high = close.iloc[-5:].max()
    price_prev15_high = close.iloc[-20:-5].max()

    if price_slope_pct > price_threshold and vol_slope_pct < -vol_threshold:
        result['top_divergence'] = True
        result['strength'] = min(abs(vol_slope_pct) / vol_threshold, 1.0)
        result['description'] = f'顶背离: 价格上涨{price_slope_pct * 100:.1f}%但量能萎缩{abs(vol_slope_pct) * 100:.1f}%，上涨动能衰竭'

    elif price_recent5_high > price_prev15_high * 1.02 and vol_change < -0.2:
        result['fake_breakout'] = True
        result['strength'] = min(abs(vol_change), 1.0)
        result['description'] = f'假突破: 创新高但量能萎缩{abs(vol_change) * 100:.1f}%，突破有效性存疑'

    elif price_slope_pct < -price_threshold:
        if obv_slope > 0.1:
            result['bottom_divergence'] = True
            result['strength'] = min(obv_slope, 1.0)
            result['description'] = f'底背离: 价格下跌{abs(price_slope_pct) * 100:.1f}%但OBV上升，主力吸筹信号'
        elif vol_recent5 > vol_prev15 * 1.3:
            result['bottom_divergence'] = True
            result['strength'] = 0.7
            result['description'] = f'底部放量: 价格下跌但量能放大，可能见底'

    if abs(price_slope_pct) < 0.02 and vol_slope_pct < -vol_threshold:
        result['accumulation'] = True
        if not result['description']:
            result['strength'] = min(abs(vol_slope_pct) / vol_threshold, 0.8)
            result['description'] = f'缩量横盘: 价格稳定但量能萎缩{abs(vol_slope_pct) * 100:.1f}%，主力锁筹'

    return result


def calc_williams_r(high, low, close, period=14):
    """Williams %R：超买超卖震荡指标，范围 [-100, 0]。"""
    hh = high.rolling(period).max()
    ll = low.rolling(period).min()
    wr = -100 * (hh - close) / (hh - ll).replace(0, np.nan)
    return wr.fillna(-50)


def calc_cci(high, low, close, period=20, constant=0.015):
    """CCI 顺势指标：价格与均值的偏离程度。"""
    typical = (high + low + close) / 3
    ma = typical.rolling(period).mean()
    mean_dev = typical.rolling(period).apply(lambda x: np.abs(x - x.mean()).mean(), raw=True)
    cci = (typical - ma) / (constant * mean_dev.replace(0, np.nan))
    return cci.fillna(0)


def calc_stoch_rsi(close, rsi_period=14, stoch_period=14):
    """Stochastic RSI：RSI 的 RSI，对超买超卖更敏感。"""
    rsi = calc_rsi(close, period=rsi_period)
    rsi_min = rsi.rolling(stoch_period).min()
    rsi_max = rsi.rolling(stoch_period).max()
    stoch_rsi = (rsi - rsi_min) / (rsi_max - rsi_min).replace(0, np.nan)
    return stoch_rsi.fillna(0.5) * 100


def calc_sar(high, low, af_start=0.02, af_max=0.2):
    """抛物线 SAR（Stop and Reverse）：跟踪止损指标。

    返回 SAR 序列，价格在 SAR 上方为多头，下方为空头。
    """
    n = len(high)
    if n == 0:
        return pd.Series(dtype=float)
    # P2: numpy数组循环替代Series.iloc（SAR本征串行，逐bar依赖无法向量化，但去iloc开销~5x提速）
    h = high.to_numpy(dtype=float)
    l = low.to_numpy(dtype=float)
    out = np.empty(n, dtype=float)
    af = af_start
    bull = True
    out[0] = l[0]
    ep = h[0]
    for i in range(1, n):
        prev_sar = out[i - 1]
        new_sar = prev_sar + af * (ep - prev_sar)
        if bull:
            if l[i] < new_sar:
                bull = False
                new_sar = ep
                af = af_start
                ep = l[i]
            else:
                if h[i] > ep:
                    ep = h[i]
                    af = min(af + af_start, af_max)
        else:
            if h[i] > new_sar:
                bull = True
                new_sar = ep
                af = af_start
                ep = h[i]
            else:
                if l[i] < ep:
                    ep = l[i]
                    af = min(af + af_start, af_max)
        out[i] = new_sar
    return pd.Series(out, index=high.index)


def calc_mfi(high, low, close, volume, period=14):
    """Money Flow Index：带量的 RSI，范围 [0, 100]。

    将典型价格 × 成交量作为资金流，上涨日累加正流量，下跌日累加负流量。
    """
    typical = (high + low + close) / 3
    money_flow = typical * volume
    pos_flow = money_flow.where(typical > typical.shift(1), 0.0)
    neg_flow = money_flow.where(typical < typical.shift(1), 0.0)
    pos_sum = pos_flow.rolling(period).sum()
    neg_sum = neg_flow.rolling(period).sum()
    mfr = pos_sum / neg_sum.replace(0, np.nan)
    mfi = 100 - 100 / (1 + mfr)
    return mfi.fillna(50)


def calc_cmf(high, low, close, volume, period=20):
    """Chaikin Money Flow：蔡金资金流指标，范围约 [-1, 1]。

    正值代表资金流入，负值代表流出。
    """
    clv = ((close - low) - (high - close)) / (high - low).replace(0, np.nan)
    clv = clv.fillna(0)
    money_flow_volume = clv * volume
    cmf = money_flow_volume.rolling(period).sum() / volume.rolling(period).sum().replace(0, np.nan)
    return cmf.fillna(0)


def detect_gap(df, gap_threshold=0.01):
    """缺口跳空：今日开盘价相对昨日高低点跳空。

    返回 (gap_up_series, gap_down_series)。
    """
    prev_high = df['high'].shift(1)
    prev_low = df['low'].shift(1)
    gap_up = df['open'] > prev_high * (1 + gap_threshold)
    gap_down = df['open'] < prev_low * (1 - gap_threshold)
    return gap_up, gap_down


def detect_engulfing(df):
    """吞没形态：今日实体完全包住昨日实体。

    返回 (bullish_engulfing, bearish_engulfing)。
    """
    prev_open = df['open'].shift(1)
    prev_close = df['close'].shift(1)
    prev_body = (prev_close - prev_open).abs()
    curr_body = (df['close'] - df['open']).abs()
    body_bigger = curr_body > prev_body
    bullish = body_bigger & (prev_close < prev_open) & (df['close'] > df['open']) & \
              (df['close'] >= prev_open) & (df['open'] <= prev_close)
    bearish = body_bigger & (prev_close > prev_open) & (df['close'] < df['open']) & \
              (df['close'] <= prev_open) & (df['open'] >= prev_close)
    return bullish, bearish


def detect_hammer(df, body_ratio=0.3, shadow_ratio=2.0):
    """锤子线/上吊线：小实体 + 长下影线。

    返回 (hammer_bullish, hanging_bearish)。
    """
    body = (df['close'] - df['open']).abs()
    range_hl = df['high'] - df['low']
    lower_shadow = df[['open', 'close']].min(axis=1) - df['low']
    upper_shadow = df['high'] - df[['open', 'close']].max(axis=1)
    small_body = body < range_hl * body_ratio
    long_lower = lower_shadow > body * shadow_ratio
    short_upper = upper_shadow < body
    hammer = small_body & long_lower & short_upper
    # 在下跌趋势中为锤子线（看涨），上涨趋势中为上吊线（看跌）
    uptrend = df['close'] > df['close'].shift(5)
    hammer_bullish = hammer & (~uptrend)
    hanging_bearish = hammer & uptrend
    return hammer_bullish, hanging_bearish


def detect_doji(df, body_ratio=0.1):
    """十字星：开盘价≈收盘价，实体极小。

    返回 doji_series（反转信号，配合趋势判断）。
    """
    body = (df['close'] - df['open']).abs()
    range_hl = df['high'] - df['low']
    doji = body < range_hl * body_ratio
    return doji
