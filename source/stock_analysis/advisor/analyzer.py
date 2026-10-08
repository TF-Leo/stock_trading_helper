import pandas as pd
import numpy as np
from stock_analysis.indicators import calc_rsi, calc_cyc_cost, calc_winner_pct


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
    if curr > ma20 > ma60 and ret_20d > 5 and ret_60d > 10:
        regime = "StrongBull"
        confidence = 0.9
    elif curr > ma20 and ret_20d > 0:
        regime = "Bull"
        confidence = 0.7
    elif curr < ma20 and curr < ma60 and ret_20d < -5:
        regime = "Bear"
        confidence = 0.7
    elif curr < ma60 and ret_60d < -15:
        regime = "Crash"
        confidence = 0.9
    else:
        regime = "Shock"
        confidence = 0.5
    if vol > 0.35:
        if regime in ("Bull", "StrongBull"):
            regime = "Shock"
            confidence *= 0.7
    return regime, confidence


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
