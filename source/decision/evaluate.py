# -*- coding: utf-8 -*-
"""F28：信号级评价 —— 每一次买卖点之后的实际走势。

为什么需要它
------------
现有 `evidence.metrics` 回答的是"这套策略整体赚不赚钱"，不是"它给出的这条
提示准不准"。二者是不同的东西：一个策略可以长期赚钱，同时每个卖点之后都反弹。

002415 的 10-20 死叉就是例子：整体年化不差，但卖点后 3 日收益 +2.93%（卖错了）。
没有这一层统计，只能靠人工看图发现，且无法回归。

输出（契约 §6.4）
-----------------
`horizons = [1, 3, 5, 10, 20]`（交易日）：
    buy.mean_fwd / buy.hit_rate    买点后收益均值 / 上涨比例
    sell.mean_fwd / sell.hit_rate  卖点后收益均值 / 下跌比例（下跌=卖对了）
    sell.missed_rally_mean_5d      卖点后 5 日为涨的样本均值（踏空成本）
    sell_false_alarm_rate_5d       卖点后 5 日为涨的比例（卖错了的比例）
"""
from __future__ import annotations

from typing import Any, Dict, Optional, Sequence

import numpy as np
import pandas as pd

DEFAULT_HORIZONS = (1, 3, 5, 10, 20)
MIN_SAMPLES = 5

STATS_KEYS = ('strategy', 'horizons', 'n_buy', 'n_sell', 'buy', 'sell',
              'sell_false_alarm_rate_5d', 'note')


def null_stats(note: Optional[str] = None) -> Dict[str, Any]:
    """错误/不可用路径：键齐全、值为 null（G35 递归比对键集合，必须展开到 horizon 子键）。"""
    h = [str(x) for x in DEFAULT_HORIZONS]
    return {
        'strategy': None,
        'horizons': list(DEFAULT_HORIZONS),
        'n_buy': None,
        'n_sell': None,
        'buy': {'mean_fwd': {k: None for k in h}, 'hit_rate': {k: None for k in h}},
        'sell': {'mean_fwd': {k: None for k in h}, 'hit_rate': {k: None for k in h},
                 'missed_rally_mean_5d': None},
        'sell_false_alarm_rate_5d': None,
        'note': note,
    }


def _side_stats(close: pd.Series, mask: np.ndarray,
                horizons: Sequence[int], want_up: bool) -> Dict[str, Any]:
    """单侧统计。`want_up=True` 用于买点（涨=对），False 用于卖点（跌=对）。"""
    mean_fwd: Dict[str, Optional[float]] = {}
    hit_rate: Dict[str, Optional[float]] = {}
    for h in horizons:
        fwd = (close.shift(-h) / close - 1.0).to_numpy(dtype=float)
        vals = fwd[mask]
        vals = vals[np.isfinite(vals)]
        if len(vals) == 0:
            mean_fwd[str(h)] = None
            hit_rate[str(h)] = None
            continue
        mean_fwd[str(h)] = round(float(np.mean(vals)), 6)
        good = vals > 0 if want_up else vals < 0
        hit_rate[str(h)] = round(float(np.mean(good)), 4)
    return {'mean_fwd': mean_fwd, 'hit_rate': hit_rate}


def signal_stats(df: pd.DataFrame, buy, sell,
                 horizons: Sequence[int] = DEFAULT_HORIZONS,
                 strategy: Optional[str] = None) -> Dict[str, Any]:
    """统计每个买点/卖点之后 h 个交易日的收益（向量化，不遍历全历史）。"""
    out = null_stats()
    out['strategy'] = strategy
    out['horizons'] = list(horizons)
    if df is None or len(df) < 2 or 'close' not in df.columns:
        out['note'] = '行情不足，无法计算信号级统计'
        return out

    close = pd.Series(df['close'].to_numpy(dtype=float), index=df.index)
    b = pd.Series(buy, index=df.index).fillna(False).astype(bool).to_numpy()
    s = pd.Series(sell, index=df.index).fillna(False).astype(bool).to_numpy()

    out['n_buy'] = int(b.sum())
    out['n_sell'] = int(s.sum())

    if out['n_buy'] < MIN_SAMPLES and out['n_sell'] < MIN_SAMPLES:
        out['note'] = f"买点 {out['n_buy']} 个、卖点 {out['n_sell']} 个，样本不足 {MIN_SAMPLES}"
        return out

    out['buy'] = _side_stats(close, b, horizons, want_up=True)
    sell_stats = _side_stats(close, s, horizons, want_up=False)
    out['sell'] = sell_stats

    # 踏空成本与误报率（固定 5 日口径）
    fwd5 = (close.shift(-5) / close - 1.0).to_numpy(dtype=float)
    sv = fwd5[s]
    sv = sv[np.isfinite(sv)]
    if len(sv):
        up = sv[sv > 0]
        out['sell']['missed_rally_mean_5d'] = round(float(np.mean(up)), 6) if len(up) else 0.0
        out['sell_false_alarm_rate_5d'] = round(float(np.mean(sv > 0)), 4)
    else:
        out['sell']['missed_rally_mean_5d'] = None
        out['sell_false_alarm_rate_5d'] = None
    return out



# ---------------------------------------------------------------------------
# 指标组装（原 cli._ci_block / cli._build_metrics，迁入决策层以保持 cli 精简）
# ---------------------------------------------------------------------------
def _ci_block(values, stat_fn, point, n_boot):
    """F10: 组装 {'point', 'ci_95'}；CI 覆盖点估计，样本不足时 ci_95 为 None。"""
    from .scoring import metric_with_ci
    try:
        point = float(point)
    except (TypeError, ValueError):
        point = 0.0
    if not np.isfinite(point):
        return {'point': None, 'ci_95': None}
    out = metric_with_ci(values, stat_fn, n_boot=n_boot)
    ci = out.get('ci_95')
    if ci:
        ci = [round(min(ci[0], point), 6), round(max(ci[1], point), 6)]
    return {'point': round(point, 6), 'ci_95': ci}


def build_metrics(result, fast: bool = False) -> Dict[str, Any]:
    """F10/B7: evidence.metrics 逐项填 point + bootstrap 95% CI。"""
    from .schema import METRIC_KEYS
    from .scoring import (equity_to_returns, stat_annual_return, stat_calmar,
                          stat_max_drawdown, stat_profit_factor, stat_sharpe, stat_win_rate)

    rets = equity_to_returns(list(getattr(result, 'equity_curve', None) or []))
    trades = list(getattr(result, 'trades', None) or [])
    pnl = [float(t.pnl_pct) for t in trades]
    hold = [float(t.holding_days) for t in trades]
    nb = 400 if fast else 2000
    block = {
        'annual_return': _ci_block(rets, stat_annual_return, getattr(result, 'annual_return', 0), nb),
        'max_drawdown': _ci_block(rets, stat_max_drawdown, getattr(result, 'max_drawdown', 0), nb),
        'win_rate': _ci_block(pnl, stat_win_rate, getattr(result, 'win_rate', 0), nb),
        'sharpe': _ci_block(rets, stat_sharpe, getattr(result, 'sharpe_ratio', 0), nb),
        'calmar': _ci_block(rets, stat_calmar, getattr(result, 'calmar_ratio', 0), nb),
        'profit_factor': _ci_block(pnl, stat_profit_factor, getattr(result, 'profit_factor', 0), nb),
        'trade_count': {'point': int(getattr(result, 'trade_count', 0) or 0), 'ci_95': None},
        'avg_holding_days': _ci_block(hold, lambda v: float(np.mean(v)),
                                      getattr(result, 'avg_holding_days', 0), nb),
        'time_in_market': {'point': round(float(getattr(result, 'time_in_market', 0.0) or 0.0), 6),
                           'ci_95': None},
    }
    if set(block) != set(METRIC_KEYS):
        raise RuntimeError(
            'evidence.metrics 键集合漂移（C3 同构被破坏）：'
            f'仅在一侧存在 {sorted(set(block) ^ set(METRIC_KEYS))}'
        )
    return block
