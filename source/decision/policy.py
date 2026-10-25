# -*- coding: utf-8 -*-
"""策略引擎：买入看条件期望，卖出只做风控。

为什么重写
----------
v2.0 的策略是"买点买、卖点卖"的对称结构。实测（14 只，2018 起）：

    买点后 20 日：+1.72%，13/14 只为正
    卖点后 20 日：+1.04%，仅 5/14 只为负   ← 9 只在"卖出"之后继续上涨

即"跌破均线就清仓"在本池是**负期望**，用户看到的"卖出提示后大涨"不是误报，
是机制缺陷。因此本引擎：

  * 入场：买点 + 该侧 20 日条件期望为正（`expectancy.trailing_expectancy_ok`）
  * 出场：**只由风控触发** —— 止损 / 止盈 / 时间止损（满 H 日仍浮亏）
  * 趋势卖出信号**不参与**清仓决策（仅作为 `signal.state` 的描述信息）

时点约定：所有判断在 T 日收盘做出，**T+1 日开盘成交**（与 `engine.exec_price=next_open` 一致）。
止损/止盈用当日 high/low 判断是否触发，但成交仍在次日开盘——这样"提示"与
"人能做到的动作"是同一件事，不会出现"盘中已成交、收盘才告诉你"的错位。
"""
from __future__ import annotations

from typing import Any, Dict, Optional

import numpy as np
import pandas as pd

POLICY_KEYS = ('method', 'trade_count', 'annual_return', 'max_drawdown', 'exposure',
               'vs_buy_hold', 'exit_reasons', 'note')


def null_policy(note: Optional[str] = None) -> Dict[str, Any]:
    """错误路径：键齐全、值为 null；exit_reasons 固定三键（G35 递归比对）。"""
    out = {k: None for k in POLICY_KEYS}
    out['exit_reasons'] = {'stop': 0, 'target': 0, 'time': 0}
    out['note'] = note
    return out


def public_view(policy: Dict[str, Any]) -> Dict[str, Any]:
    """只保留可进 JSON 的字段（`position_series` 等内部数组不对外）。"""
    return {k: (policy or {}).get(k) for k in POLICY_KEYS}


def run_policy(df: pd.DataFrame, buy, exp_ok, horizon: int = 20,
               stop_mult: float = 2.5, profit_mult: float = 4.0,
               atr_period: int = 14, exec_price: str = 'next_open',
               slippage: float = 0.001) -> Dict[str, Any]:
    """按"买点+期望 → 风控出场"的规则回放，并给出当前应做的动作。

    Args:
        df: 行情（需 close；有 high/low 时用 ATR 定止损，否则回落固定 8%/15%）
        buy: 原始买点序列（未 shift）
        exp_ok: 逐日布尔数组，表示"截至该日买点期望是否可用"（见 expectancy）
        horizon: 有效期（交易日），同时用于时间止损

    Returns:
        {method, trade_count, annual_return, max_drawdown, exposure, vs_buy_hold,
         exit_reasons, note,
         holding, bars_held, entry_price, entry_date, stop_price, target_price,
         pending, position_series}
    """
    out = null_policy()
    out.update({'holding': False, 'bars_held': 0, 'entry_price': None,
                'entry_date': None, 'stop_price': None, 'target_price': None,
                'pending': None, 'position_series': None})
    n = len(df)
    if n < 60 or 'close' not in df.columns:
        out['note'] = '行情不足，无法回放'
        return out

    close = df['close'].to_numpy(dtype=float)
    opn = df['open'].to_numpy(dtype=float) if 'open' in df.columns else close
    high = df['high'].to_numpy(dtype=float) if 'high' in df.columns else close
    low = df['low'].to_numpy(dtype=float) if 'low' in df.columns else close
    sig = pd.Series(buy, index=df.index).fillna(False).astype(bool).to_numpy()
    ok = np.asarray(exp_ok, dtype=bool)
    if len(ok) != n:
        ok = np.zeros(n, dtype=bool)

    atr = None
    try:
        from stock_analysis.indicators import calc_atr
        if {'high', 'low'}.issubset(df.columns):
            atr = calc_atr(df['high'], df['low'], df['close'], period=atr_period).to_numpy(dtype=float)
    except Exception:
        atr = None

    use_open = (exec_price == 'next_open')

    def _fill(i, side):
        px = opn[i] if (use_open and np.isfinite(opn[i]) and opn[i] > 0) else close[i]
        return px * (1 + slippage) if side == 'buy' else px * (1 - slippage)

    cash, shares = 1.0, 0.0
    equity = np.empty(n, dtype=float)
    pos_series = np.zeros(n, dtype=np.int8)
    trades = 0
    bars_held = 0
    entry_price = 0.0
    entry_date = None
    stop_px = target_px = None
    pending = None          # None | 'entry' | 'stop' | 'target' | 'time'
    exit_reasons: Dict[str, int] = {'stop': 0, 'target': 0, 'time': 0}
    holding = False
    hold_bars_total = 0

    for i in range(n):
        # 1) 执行上一根收盘定下的动作（T+1 开盘）
        if holding and pending in ('stop', 'target', 'time'):
            px = _fill(i, 'sell')
            cash = shares * px
            shares = 0.0
            holding = False
            trades += 1
            exit_reasons[pending] = exit_reasons.get(pending, 0) + 1
            pending = None
            stop_px = target_px = None
            bars_held = 0
        elif (not holding) and pending == 'entry':
            px = _fill(i, 'buy')
            if px > 0:
                shares = cash / px
                cash = 0.0
                holding = True
                trades += 1
                entry_price = px
                entry_date = df.index[i]
                dist = None
                if atr is not None and np.isfinite(atr[i]) and atr[i] > 0:
                    dist = float(atr[i])
                stop_dist = min(max(stop_mult * dist / px, 0.03), 0.20) if dist else 0.08
                prof_dist = min(max(profit_mult * dist / px, 0.05), 0.60) if dist else 0.15
                stop_px = px * (1 - stop_dist)
                target_px = px * (1 + prof_dist)
                bars_held = 0
            pending = None

        # 2) 收盘评估
        if holding:
            bars_held += 1
            hold_bars_total += 1
            if stop_px is not None and low[i] <= stop_px:
                pending = 'stop'
            elif target_px is not None and high[i] >= target_px:
                pending = 'target'
            elif bars_held >= horizon and close[i] <= entry_price:
                pending = 'time'
        elif pending is None and sig[i] and ok[i]:
            pending = 'entry'

        pos_series[i] = 1 if holding else 0
        equity[i] = cash + shares * close[i]

    total = float(equity[-1]) - 1.0
    annual = (equity[-1] ** (252.0 / n) - 1.0) if equity[-1] > 0 else -1.0
    peak = np.maximum.accumulate(equity)
    mdd = float(np.max(1.0 - equity / peak))
    bh = float(close[-1] / close[0] - 1.0)

    out.update({
        'method': f'policy_h{horizon}_risk_exit',
        'trade_count': int(trades),
        'annual_return': round(float(annual), 6),
        'max_drawdown': round(mdd, 6),
        'exposure': round(hold_bars_total / n, 4),
        'vs_buy_hold': round(total - bh, 6),
        'exit_reasons': exit_reasons,
        'note': None,
        'holding': bool(holding),
        'bars_held': int(bars_held),
        'entry_price': (round(float(entry_price), 4) if holding else None),
        'entry_date': (str(entry_date)[:10] if holding and entry_date is not None else None),
        'stop_price': (round(float(stop_px), 4) if holding and stop_px is not None else None),
        'target_price': (round(float(target_px), 4) if holding and target_px is not None else None),
        'pending': pending,
        'position_series': pos_series,
    })
    return out
