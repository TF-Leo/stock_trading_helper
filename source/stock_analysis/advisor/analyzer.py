import pandas as pd
import numpy as np
from stock_analysis.indicators import calc_rsi, calc_cyc_cost, calc_winner_pct
from stock_analysis.data_loader import get_market_index_daily


def analyze_trend_strength(df):
    if len(df) < 60:
        return 0, "数据不足"
    close = df['close']
    ma5 = close.rolling(5).mean().iloc[-1]
    ma20 = close.rolling(20).mean().iloc[-1]
    ma60 = close.rolling(60).mean().iloc[-1]
    curr = close.iloc[-1]
    score = 0
    if curr > ma5:
        score += 1
    if curr > ma20:
        score += 2
    if curr > ma60:
        score += 2
    if ma5 > ma20 > ma60:
        score += 3
    ret_5d = (curr / close.iloc[-5] - 1) * 100 if len(close) >= 5 else 0
    ret_20d = (curr / close.iloc[-20] - 1) * 100 if len(close) >= 20 else 0
    if ret_5d > 2:
        score += 1
    elif ret_5d < -2:
        score -= 1
    if ret_20d > 5:
        score += 1
    elif ret_20d < -5:
        score -= 1
    if score >= 7:
        desc = "强势上涨"
    elif score >= 4:
        desc = "偏多震荡"
    elif score >= 1:
        desc = "弱势震荡"
    else:
        desc = "下跌趋势"
    return score, desc


def analyze_market_level(df):
    if len(df) < 250:
        return 50, "数据不足"
    close = df['close']
    curr = close.iloc[-1]
    high_250 = close.rolling(250).max().iloc[-1]
    low_250 = close.rolling(250).min().iloc[-1]
    if high_250 == low_250:
        level = 50
    else:
        level = (curr - low_250) / (high_250 - low_250) * 100
    if level < 20:
        desc = "低位(安全区)"
    elif level < 40:
        desc = "中低位(可配区)"
    elif level < 60:
        desc = "中位(持有区)"
    elif level < 80:
        desc = "中高位(谨慎区)"
    else:
        desc = "高位(风险区)"
    return level, desc


# A8: 全项目唯一的 regime 标签集合。历史序列与"当前判定"必须共用这一套，
# 否则 engine 的 Bull/Bear/Shock 分市况统计会把 StrongBull/Crash 全部落进 Shock。
REGIME_LABELS = ("Bull", "Bear", "Shock")


def _regime_from_row(close, ma20, ma60, ret_20d, ret_60d, vol) -> tuple:
    """单行 regime 判定（当前判定与历史序列共用的唯一规则）。"""
    if close > ma20 and ret_20d > 0:
        regime, confidence = "Bull", 0.7
        if close > ma20 > ma60 and ret_20d > 5 and ret_60d > 10:
            confidence = 0.9          # 强趋势：仍是 Bull，只是置信度更高
    elif close < ma20 and close < ma60 and ret_20d < -5:
        regime, confidence = "Bear", 0.7
        if close < ma60 and ret_60d < -15:
            confidence = 0.9          # 急跌：仍是 Bear
    else:
        regime, confidence = "Shock", 0.5
    try:
        if vol is not None and vol > 0.35 and regime == "Bull":
            regime, confidence = "Shock", confidence * 0.7
    except Exception:
        pass
    return regime, confidence


def get_current_market_regime(df):
    if len(df) < 60:
        return "Unknown", 0.5
    close = df['close']
    ma20 = close.rolling(20).mean().iloc[-1]
    ma60 = close.rolling(60).mean().iloc[-1]
    curr = close.iloc[-1]
    ret_20d = (curr / close.iloc[-20] - 1) * 100 if len(close) >= 20 else 0
    ret_60d = (curr / close.iloc[-60] - 1) * 100 if len(close) >= 60 else 0
    vol = close.pct_change().rolling(20).std().iloc[-1] * np.sqrt(252)
    return _regime_from_row(curr, ma20, ma60, ret_20d, ret_60d, vol)


def market_regime_series(df):
    """A8: 逐 bar regime 序列（Bull/Bear/Shock），供分市况胜率统计使用。

    与 get_current_market_regime 共用 _regime_from_row，标签集合完全一致。
    """
    if df is None or len(df) < 60 or 'close' not in df.columns:
        return None
    close = df['close'].astype(float)
    ma20 = close.rolling(20).mean()
    ma60 = close.rolling(60).mean()
    ret_20d = (close / close.shift(20) - 1) * 100
    ret_60d = (close / close.shift(60) - 1) * 100
    vol = close.pct_change().rolling(20).std() * np.sqrt(252)
    labels = []
    for c, m20, m60, r20, r60, v in zip(close, ma20, ma60, ret_20d, ret_60d, vol):
        if any(x != x for x in (c, m20, m60)):   # NaN 检查
            labels.append('Shock')
            continue
        labels.append(_regime_from_row(c, m20, m60, 0 if r20 != r20 else r20,
                                       0 if r60 != r60 else r60,
                                       None if v != v else v)[0])
    return pd.Series(labels, index=df.index)


def analyze_volume_chip_structure(df):
    if len(df) < 90:
        return {"score": 50, "desc": "数据不足", "details": {}}
    close = df['close']
    volume = df['volume']
    curr_price = close.iloc[-1]
    # P0: calc_cyc_cost(close, turnover_rate)，原calc_cyc_cost(df)必崩
    if 'turnover' in df.columns:
        _to = df['turnover'] / 100
    else:
        _to = pd.Series(0.01, index=df.index)
    cyc_cost_s = calc_cyc_cost(close, _to)
    cyc_cost = float(cyc_cost_s.iloc[-1])
    winner_pct = float(calc_winner_pct(close, cyc_cost_s).iloc[-1])
    vol_ma20 = volume.rolling(20).mean().iloc[-1]
    curr_vol = volume.iloc[-1]
    vol_ratio = curr_vol / vol_ma20 if vol_ma20 > 0 else 1
    score = 50
    details = {}
    if curr_price < cyc_cost:
        score += 15
        details["cost_support"] = "价格低于筹码成本(支撑强)"
    else:
        score -= 5
        details["cost_support"] = "价格高于筹码成本"
    if winner_pct < 30:
        score += 20
        details["winner"] = f"获利盘仅{winner_pct:.0f}%(抛压小)"
    elif winner_pct > 80:
        score -= 20
        details["winner"] = f"获利盘{winner_pct:.0f}%(抛压大)"
    else:
        details["winner"] = f"获利盘{winner_pct:.0f}%"
    if vol_ratio > 1.5:
        score += 10
        details["volume"] = "放量(资金活跃)"
    elif vol_ratio < 0.7:
        score -= 10
        details["volume"] = "缩量(资金观望)"
    else:
        details["volume"] = "量能正常"
    score = max(0, min(100, score))
    if score >= 70:
        desc = "量价筹码共振(强势)"
    elif score >= 50:
        desc = "量价筹码中性"
    else:
        desc = "量价筹码偏弱"
    return {"score": score, "desc": desc, "details": details}


# A8: 统一的 regime 定义（合并 strategy_master/config.py）
def get_market_regime_series(start_date='20200101'):
    df = get_market_index_daily("000985", start_date=start_date, source='sina')
    if df is None or df.empty:
        df = get_market_index_daily("sh000001", start_date=start_date, source='sina')
    if df is None or df.empty:
        return None
    df['MA60'] = df['close'].rolling(60).mean()
    df['MA120'] = df['close'].rolling(120).mean()

    def label_regime(row):
        if row['close'] > row['MA60'] and row['MA60'] > row['MA120']:
            return 'Bull'
        elif row['close'] < row['MA60'] and row['MA60'] < row['MA120']:
            return 'Bear'
        else:
            return 'Shock'

    return df.apply(label_regime, axis=1)
