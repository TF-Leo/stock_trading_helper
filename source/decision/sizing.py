# -*- coding: utf-8 -*-
"""F12 / 契约「新增模块布局」：仓位、止损、止盈、入场区、horizon 推导的**唯一**实现。

历史缺陷：这些数字曾散落在 cli.py 里以字面量硬编码
（`last_close * 0.95` / `last_close * 1.10` / `stop_distance = 0.05`），
与回测实际使用的 `BacktestConfig.stop_loss_pct=0.08 / take_profit_pct=0.15` 互相矛盾，
导致 Hermes 拿到的止损价与本项目回测结论不是同一个东西。

本模块只做纯计算，不读文件、不触网、不依赖 pandas。
"""
from __future__ import annotations

import math
from typing import Dict, List, Optional, Tuple

# ---------------------------------------------------------------------------
# 具名常量（契约「派生规则」；禁止在调用方另写字面量）
# ---------------------------------------------------------------------------
RISK_PER_TRADE = 0.01          # 单笔风险占权益比例
MAX_POSITION_FRACTION = 0.30   # 仓位上限（契约硬约束）
ATR_STOP_MULT = 2.5            # 止损 = entry - k*ATR
ATR_PROFIT_MULT = 4.0          # 止盈 = entry + m*ATR
ENTRY_ZONE_ATR_MULT = 0.5      # 入场区 = [last-0.5ATR, last+0.5ATR]
MIN_STOP_DISTANCE = 0.03       # 止损距离下限（契约 clamp）
MAX_STOP_DISTANCE = 0.20       # 止损距离上限（契约 clamp）
DEFAULT_STOP_LOSS_PCT = 0.08   # 与 BacktestConfig.stop_loss_pct 默认值一致
DEFAULT_TAKE_PROFIT_PCT = 0.15  # 与 BacktestConfig.take_profit_pct 默认值一致
HORIZON_MIN_FLOOR = 2
HORIZON_MAX_CAP = 60


def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def derive_stops(last_close: float, atr: Optional[float] = None,
                 config=None) -> Dict[str, object]:
    """推导止损/止盈。

    atr 可用时（`method='atr'`）：stop = entry - 2.5*ATR、profit = entry + 4.0*ATR，
    再把 stop_distance clamp 到 [0.03, 0.20]；
    atr 不可用或 <=0 时回落固定百分比（`method='fixed'`），百分比取
    `config.stop_loss_pct / take_profit_pct`（默认 0.08 / 0.15），**不是**历史上的 0.05。
    """
    entry = float(last_close)
    sl_pct = float(getattr(config, 'stop_loss_pct', DEFAULT_STOP_LOSS_PCT) or DEFAULT_STOP_LOSS_PCT)
    tp_pct = float(getattr(config, 'take_profit_pct', DEFAULT_TAKE_PROFIT_PCT) or DEFAULT_TAKE_PROFIT_PCT)

    if atr is not None and float(atr) > 0:
        method = 'atr'
        stop_distance = _clamp(ATR_STOP_MULT * float(atr) / entry, MIN_STOP_DISTANCE, MAX_STOP_DISTANCE)
        profit_distance = ATR_PROFIT_MULT * float(atr) / entry
        # 止盈至少给到止损距离的 1.2 倍，避免低波动下止盈空间小于止损
        profit_distance = max(profit_distance, stop_distance * 1.2)
    else:
        method = 'fixed'
        stop_distance = _clamp(sl_pct, MIN_STOP_DISTANCE, MAX_STOP_DISTANCE)
        profit_distance = tp_pct

    return {
        'stop_loss': round(entry * (1.0 - stop_distance), 2),
        'take_profit': round(entry * (1.0 + profit_distance), 2),
        'stop_distance': round(stop_distance, 4),
        'take_profit_distance': round(profit_distance, 4),
        'method': method,
        'atr': None if atr is None else round(float(atr), 4),
    }


def derive_entry_zone(last_close: float, atr: Optional[float] = None,
                      buy_price: Optional[float] = None) -> Optional[List[float]]:
    """入场区 = [max(buy_price, last_close-0.5ATR), last_close+0.5ATR]。

    无 ATR 时退化为 ±1%（但下限仍取 buy_price，保证不高于触发价太多）。
    """
    entry = float(last_close)
    half = (ENTRY_ZONE_ATR_MULT * float(atr)) if (atr is not None and float(atr) > 0) else entry * 0.01
    lower = entry - half
    if buy_price is not None:
        try:
            lower = max(lower, float(buy_price))
        except (TypeError, ValueError):
            pass
    return [round(lower, 2), round(entry + half, 2)]


def derive_horizon(avg_holding_days: Optional[float]) -> List[int]:
    """horizon_days = [max(2, floor(0.5*h)), min(60, ceil(1.5*h))]。"""
    try:
        h = float(avg_holding_days or 0)
    except (TypeError, ValueError):
        h = 0.0
    if h <= 0:
        return [5, 20]
    lo = max(HORIZON_MIN_FLOOR, int(math.floor(0.5 * h)))
    hi = min(HORIZON_MAX_CAP, int(math.ceil(1.5 * h)))
    if hi < lo:
        hi = lo
    return [lo, hi]


def derive_position_fraction(confidence: float, stop_distance: float,
                             cap: float = MAX_POSITION_FRACTION) -> float:
    """position_fraction = clamp(risk_per_trade/stop_distance * confidence, 0, cap)。"""
    sd = float(stop_distance or 0)
    if sd <= 0:
        return 0.0
    conf = _clamp(float(confidence or 0), 0.0, 1.0)
    return round(_clamp(RISK_PER_TRADE / sd * conf, 0.0, float(cap)), 3)


def distance_to_trigger(last_close: float,
                        buy_price: Optional[float],
                        sell_price: Optional[float]) -> Dict[str, Optional[float]]:
    """契约：buy_pct = (buy_price-last)/last*100；价格为 null 时该值为 null。"""
    last = float(last_close)

    def _pct(px):
        if px is None:
            return None
        try:
            return round((float(px) - last) / last * 100.0, 2)
        except (TypeError, ValueError, ZeroDivisionError):
            return None

    return {'buy_pct': _pct(buy_price), 'sell_pct': _pct(sell_price)}


def build_invalidation(stop_loss: Optional[float], family: Optional[str],
                       horizon_hi: int) -> str:
    """B9 模板：'收盘跌破 {stop:.2f}，或 {family} 族共识转为看空，或 {n} 个交易日内未触发入场'。"""
    stop_txt = f"{float(stop_loss):.2f}" if stop_loss is not None else "止损价"
    fam = family or "主导"
    return f"收盘跌破 {stop_txt}，或 {fam} 族共识转为看空，或 {int(horizon_hi)} 个交易日内未触发入场"
