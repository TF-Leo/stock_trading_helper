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
# F30：决策回放 —— 让回测评价线上真正执行的那条规则
# ---------------------------------------------------------------------------
REPLAY_KEYS = ('method', 'trade_count', 'annual_return', 'max_drawdown',
               'exposure', 'vs_buy_hold', 'note')


def null_replay(note=None):
    """错误/不可用路径：键齐全、值为 null（G35 同构）。"""
    return {k: None for k in REPLAY_KEYS} | {'note': note}


def decision_replay(df: pd.DataFrame, buy, sell, status: str = 'ok',
                    confidence: float = 0.0, significant: bool = False,
                    exec_price: str = 'next_open', lookback: int = 250,
                    slippage: float = 0.001) -> Dict[str, Any]:
    """按线上决策表逐日回放，统计"如果完全照做提示"的收益。

    与原始信号回测的差别（这正是 R3 要修的东西）：

        原始信号回测：买点就买、卖点就卖（`engine.py`）
        决策回放    ：还要过 position_state 状态机、证据门控、status 过滤

    近似（必须如实标注，禁止声称 walk-forward）：
      * `status` / `confidence` / `significant` 用**全样本**值，不逐日重跑 581 组网格；
      * `edge` 用 `lookback` 根滚动窗口的超额收益**点估计**，不做逐日 bootstrap。

    当 `status ∈ {no_edge, untradeable, insufficient_data}` 时，决策表本身
    不允许交易，回放必然是零交易 —— 这本身就是结论：**回测不能证明这条提示有效，
    因为决策层根本没让它出手。**
    """
    from .actions import decide
    from .signals import position_states

    out = null_replay()
    if df is None or len(df) < 60 or 'close' not in df.columns:
        out['note'] = '行情不足，无法回放'
        return out

    close = df['close'].to_numpy(dtype=float)
    opn = df['open'].to_numpy(dtype=float) if 'open' in df.columns else close
    b = pd.Series(buy, index=df.index).fillna(False).astype(bool).to_numpy()
    s = pd.Series(sell, index=df.index).fillna(False).astype(bool).to_numpy()
    pos = position_states(b, s)
    prev = np.concatenate(([0], pos[:-1]))
    both = b & s
    buy_event = b & (prev == 0) & ~both
    sell_event = s & (prev == 1)

    # 滚动超额收益点估计：策略"在仓位里"的日收益 - 买入持有日收益
    ret = np.zeros(len(close), dtype=float)
    ret[1:] = close[1:] / close[:-1] - 1.0
    held = np.concatenate(([0], pos[:-1])).astype(bool)
    diff = np.where(held, ret, 0.0) - ret
    roll = pd.Series(diff).rolling(lookback, min_periods=20).mean().to_numpy()

    blocked = status in ('no_edge', 'untradeable', 'insufficient_data')
    cash, shares = 1.0, 0.0
    equity = np.empty(len(close), dtype=float)
    trades = 0
    bars_held = 0
    for i in range(len(close)):
        verdict = 'positive_significant' if (roll[i] == roll[i] and roll[i] > 0) else 'inconclusive'
        state = ('buy_triggered' if buy_event[i] else
                 ('sell_triggered' if sell_event[i] else 'neutral'))
        action = decide(status, state, verdict, confidence, significant)
        fill = opn[i] if (exec_price == 'next_open' and opn[i] == opn[i] and opn[i] > 0) else close[i]
        if action == 'buy' and shares == 0.0 and not blocked:
            shares = cash / (fill * (1 + slippage))
            cash = 0.0
            trades += 1
        elif action == 'sell' and shares > 0.0:
            cash = shares * fill * (1 - slippage)
            shares = 0.0
            trades += 1
        if shares > 0.0:
            bars_held += 1
        equity[i] = cash + shares * close[i]

    n = len(close)
    total = float(equity[-1]) - 1.0
    annual = (equity[-1] ** (252.0 / n) - 1.0) if equity[-1] > 0 else -1.0
    peak = np.maximum.accumulate(equity)
    mdd = float(np.max(1.0 - equity / peak)) if n else 0.0
    bh_total = float(close[-1] / close[0] - 1.0)

    if blocked:
        note = 'status=%s，决策表不允许交易，回放零交易' % status
    elif trades == 0:
        note = ('决策表未放行买入（significant=False 或 edge 未达 positive_significant），'
                '回放全程空仓')
    else:
        note = None
    out.update({
        'method': f'trailing_{lookback}_point_estimate',
        'trade_count': int(trades),
        'annual_return': round(float(annual), 6),
        'max_drawdown': round(mdd, 6),
        'exposure': round(bars_held / n, 4),
        'vs_buy_hold': round(total - bh_total, 6),
        'note': note,
    })
    return out

