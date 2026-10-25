# -*- coding: utf-8 -*-
"""F24/F25：信号状态机 —— 把"事件"、"历史"与"指标值"三者分开。

背景（2026-10-25 实盘事故）
--------------------------
002415 在 2026-10-22 输出 `action=hold`，但 `action_reason` 写的是
`均线趋势(3-30-60): 卖出信号 (32.42)`：

* 32.42 是**当日收盘价**，不是任何指标读数（`triggers.current` 恒等于 `last_close`）；
* "卖出"来自 `near_sell`，其原定义是"近 10 根内出现过卖点"（10-20 的死叉），
  与"现在正在卖出"无关。于是一次死叉会在之后 10 个交易日里天天被描述为卖出，
  10-23 大涨 4.19% 当天仍在报 `卖出信号 (33.78)`。

本模块的语义（契约 v2.0 §6.1）
------------------------------
* `position_state`：策略视角的隐含仓位（`flat` / `holding`），由自身信号回放得出。
* `state`：当根事件优先；`near_*` 只在**反向仓位**下才成立。
* **硬规则**：`position_state == flat` 时，`state` 不得为 `near_sell` / `sell_triggered`。
  单这一条即可消灭本次事故——10-22 策略早已空仓，不可能报"接近卖出"。
* `current_value`：指标当前值；取不到时为 `None`（**绝不拿收盘价顶替**）。
"""
from __future__ import annotations

from typing import Any, Dict, Optional

import numpy as np
import pandas as pd

# 契约 §6.1 的五档状态（顺序即"事件优先"顺序）
STATES = ('buy_triggered', 'near_buy', 'neutral', 'near_sell', 'sell_triggered')

# "接近阈值"的容差：指标量程的 5%（契约 §6.1）
NEAR_TOL_RATIO = 0.05

# 信号块的全部键（错误路径必须同构，见 C3/G35）
SIGNAL_KEYS = (
    'strategy', 'indicator', 'position_state', 'state', 'as_of',
    'last_buy_date', 'last_buy_age', 'last_sell_date', 'last_sell_age',
    'current_value', 'last_close', 'threshold', 'conflict',
)


def null_signal() -> Dict[str, Any]:
    """错误/降级路径的信号块：键齐全、值显式为 null（G35 同构要求）。"""
    return {
        'strategy': None,
        'indicator': None,
        'position_state': None,
        'state': None,
        'as_of': None,
        'last_buy_date': None,
        'last_buy_age': None,
        'last_sell_date': None,
        'last_sell_age': None,
        'current_value': None,
        'last_close': None,
        'threshold': {'buy': None, 'sell': None},
        'conflict': None,
    }


def _align(series, index) -> np.ndarray:
    """把信号序列对齐到行情索引；缺失一律视为 False（不得抛异常）。"""
    if isinstance(series, pd.Series):
        return series.reindex(index).fillna(False).astype(bool).to_numpy()
    n = len(index)
    if series is None:
        return np.zeros(n, dtype=bool)
    arr = np.asarray(series, dtype=bool)
    if len(arr) != n:
        return np.zeros(n, dtype=bool)
    return arr


def position_states(buy: np.ndarray, sell: np.ndarray) -> np.ndarray:
    """策略隐含仓位回放：buy 置 1、sell 置 0；同根同时为真时**以 sell 优先**（风控优先）。"""
    n = len(buy)
    pos = np.zeros(n, dtype=np.int8)
    cur = 0
    for i in range(n):
        if sell[i]:
            cur = 0
        elif buy[i]:
            cur = 1
        pos[i] = cur
    return pos


def _near(value, threshold, scale) -> bool:
    """距阈值是否在 5% 量程内。任一参数不可用 → False（保守：不声称"接近"）。"""
    if value is None or threshold is None or scale is None:
        return False
    try:
        v, t, s = float(value), float(threshold), float(scale)
    except (TypeError, ValueError):
        return False
    if not (np.isfinite(v) and np.isfinite(t) and np.isfinite(s)) or s <= 0:
        return False
    return abs(v - t) <= NEAR_TOL_RATIO * s


def _safe_last(series) -> Optional[float]:
    """取序列最后一个有限值；失败返回 None。"""
    try:
        val = float(series.iloc[-1])
    except (TypeError, ValueError, IndexError, AttributeError):
        return None
    return val if np.isfinite(val) else None


def build(df: pd.DataFrame, buy, sell, strategy: Optional[str] = None,
          indicator: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """产出契约 §6.1 的 `recommendation.signal` 块。

    Args:
        df: 行情（索引为交易日，需含 close）
        buy/sell: 策略原始信号（Series 或等长布尔序列）
        strategy: 策略名
        indicator: `describe_indicator()` 的返回值（可含 current_value /
                   buy_threshold / sell_threshold / scale）；None 时全部为 null。
    """
    block = null_signal()
    block['strategy'] = strategy
    n = len(df)
    if n == 0:
        return block

    index = df.index
    ind = indicator or {}
    current_value = ind.get('current_value')
    buy_th = ind.get('buy_threshold')
    sell_th = ind.get('sell_threshold')
    scale = ind.get('scale')

    block['indicator'] = ind.get('indicator')
    block['current_value'] = None if current_value is None else float(current_value)
    block['threshold'] = {'buy': buy_th, 'sell': sell_th}
    block['last_close'] = _safe_last(df['close']) if 'close' in df.columns else None
    block['as_of'] = str(index[-1])[:10]

    b = _align(buy, index)
    s = _align(sell, index)
    pos = position_states(b, s)

    # 事件：只有"仓位真的发生变化"才算事件；同根同时为真时以 sell 优先
    both = b & s
    prev = np.concatenate(([0], pos[:-1]))
    buy_event = b & (prev == 0) & ~both
    sell_event = s & (prev == 1)

    last = n - 1
    holding = bool(pos[last] == 1)

    if buy_event[last]:
        state = 'buy_triggered'
    elif sell_event[last]:
        state = 'sell_triggered'
    elif (not holding) and _near(current_value, buy_th, scale):
        # 空仓时才谈"接近买入"；同理，持仓时才谈"接近卖出"
        state = 'near_buy'
    elif holding and _near(current_value, sell_th, scale):
        state = 'near_sell'
    else:
        state = 'neutral'

    bidx = np.flatnonzero(b)
    sidx = np.flatnonzero(s)
    if len(bidx):
        block['last_buy_date'] = str(index[bidx[-1]])[:10]
        block['last_buy_age'] = int(last - bidx[-1])
    if len(sidx):
        block['last_sell_date'] = str(index[sidx[-1]])[:10]
        block['last_sell_age'] = int(last - sidx[-1])

    block['position_state'] = 'holding' if holding else 'flat'
    block['state'] = state
    block['conflict'] = bool(both[last])
    return block


# ---------------------------------------------------------------------------
# F25：指标当前值（按策略族分派）
# ---------------------------------------------------------------------------
# 只覆盖能**从同一份指标定义**复算出数值的族；其余返回 None，文案不给数字。
# 禁止拿收盘价、成交量或"最近信号价"顶替指标值 —— 那正是本次事故的成因。
_FAMILY_LABELS = {
    '均线趋势': 'MA{}/MA{}',
    'MACD': 'MACD 柱(DIF-DEA)',
    'KDJ': 'K 值',
    '唐奇安': '通道位置',
    'StochRSI': 'StochRSI',
    'CCI': 'CCI',
    'WilliamsR': 'Williams %R',
    'KAMA': 'close/KAMA',
    '布林': '%B',
}


def describe_indicator(inst, family: Optional[str], df: pd.DataFrame) -> Dict[str, Any]:
    """按策略族给出 `{indicator, current_value, buy_threshold, sell_threshold, scale}`。

    任何异常都退化为全 None —— 显示"指标值不可得"远好于显示一个错数字。
    """
    empty = {'indicator': _FAMILY_LABELS.get(family or '', None),
             'current_value': None, 'buy_threshold': None,
             'sell_threshold': None, 'scale': None}
    if inst is None or family is None or len(df) == 0 or 'close' not in df.columns:
        return empty
    try:
        from stock_analysis.indicators import (
            calc_boll, calc_cci, calc_kama, calc_kdj, calc_macd,
            calc_stoch_rsi, calc_williams_r,
        )
        close = df['close']
        high = df['high'] if 'high' in df.columns else close
        low = df['low'] if 'low' in df.columns else close

        if family == '均线趋势':
            # 该族的买卖事件由 MA_short 与 MA_mid 的交叉产生（见 TrendStrategy.generate_trade_signals），
            # 因此指标值必须取 MA_short/MA_mid —— 取 MA_long 会算出与信号无关的偏离度。
            s, m = int(getattr(inst, 's')), int(getattr(inst, 'm'))
            ma_s = _safe_last(close.rolling(s).mean())
            ma_m = _safe_last(close.rolling(m).mean())
            if ma_s is None or ma_m in (None, 0):
                return empty
            return {'indicator': _FAMILY_LABELS[family].format(s, m),
                    'current_value': ma_s / ma_m,
                    'buy_threshold': 1.0, 'sell_threshold': 1.0, 'scale': 1.0}

        if family == 'MACD':
            dif, dea, hist = calc_macd(close, fast=int(getattr(inst, 'fast')),
                                       slow=int(getattr(inst, 'slow')),
                                       signal=int(getattr(inst, 'signal')))
            cur = _safe_last(hist)
            if cur is None:
                return empty
            # 零轴穿越型指标：用柱体自身的滚动标准差当量程
            scale = _safe_last(hist.rolling(60).std())
            return {'indicator': _FAMILY_LABELS[family], 'current_value': cur,
                    'buy_threshold': 0.0, 'sell_threshold': 0.0,
                    'scale': abs(scale) if scale else None}

        if family == 'KDJ':
            k, _d, _j = calc_kdj(high, low, close, n=int(getattr(inst, 'n')),
                                 m1=int(getattr(inst, 'm1')), m2=int(getattr(inst, 'm2')))
            return {'indicator': _FAMILY_LABELS[family], 'current_value': _safe_last(k),
                    'buy_threshold': float(getattr(inst, 'buy_k')),
                    'sell_threshold': float(getattr(inst, 'sell_k')), 'scale': 100.0}

        if family == '唐奇安':
            upper = high.rolling(int(getattr(inst, 'entry_n'))).max().shift(1)
            lower = low.rolling(int(getattr(inst, 'exit_n'))).min().shift(1)
            u, lo, c = _safe_last(upper), _safe_last(lower), _safe_last(close)
            if None in (u, lo, c) or u <= lo:
                return empty
            return {'indicator': _FAMILY_LABELS[family], 'current_value': (c - lo) / (u - lo),
                    'buy_threshold': 1.0, 'sell_threshold': 0.0, 'scale': 1.0}

        if family == 'StochRSI':
            stoch = calc_stoch_rsi(close, rsi_period=int(getattr(inst, 'rsi_period')),
                                   stoch_period=int(getattr(inst, 'stoch_period')))
            return {'indicator': _FAMILY_LABELS[family], 'current_value': _safe_last(stoch),
                    'buy_threshold': float(getattr(inst, 'lower')),
                    'sell_threshold': float(getattr(inst, 'upper')), 'scale': 100.0}

        if family == 'CCI':
            cci = calc_cci(high, low, close, period=int(getattr(inst, 'period')))
            return {'indicator': _FAMILY_LABELS[family], 'current_value': _safe_last(cci),
                    'buy_threshold': float(getattr(inst, 'lower')),
                    'sell_threshold': float(getattr(inst, 'upper')), 'scale': 100.0}

        if family == 'WilliamsR':
            wr = calc_williams_r(high, low, close, period=int(getattr(inst, 'period')))
            return {'indicator': _FAMILY_LABELS[family], 'current_value': _safe_last(wr),
                    'buy_threshold': float(getattr(inst, 'buy_wr')),
                    'sell_threshold': float(getattr(inst, 'sell_wr')), 'scale': 100.0}

        if family == 'KAMA':
            kama = calc_kama(close, period=int(getattr(inst, 'period')),
                             fast=int(getattr(inst, 'fast')), slow=int(getattr(inst, 'slow')))
            k, c = _safe_last(kama), _safe_last(close)
            if k in (None, 0) or c is None:
                return empty
            return {'indicator': _FAMILY_LABELS[family], 'current_value': c / k,
                    'buy_threshold': 1.0, 'sell_threshold': 1.0, 'scale': 1.0}

        if family == '布林':
            upper, _mid, lower = calc_boll(close, window=int(getattr(inst, 'window')),
                                           num_std=float(getattr(inst, 'num_std')))
            u, lo, c = _safe_last(upper), _safe_last(lower), _safe_last(close)
            if None in (u, lo, c) or u <= lo:
                return empty
            return {'indicator': _FAMILY_LABELS[family], 'current_value': (c - lo) / (u - lo),
                    'buy_threshold': 0.0, 'sell_threshold': 1.0, 'scale': 1.0}

    except Exception:
        return empty
    return empty
