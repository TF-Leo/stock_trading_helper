# -*- coding: utf-8 -*-
"""C7: 基金参数网格单一真源。

历史缺陷（F15）：
  * `grid_size()` 是硬编码算式 `4*4+3*3+2*2*2+3*2+2*2 = 43`，注释自己写着
    `# = 43 (≈44 as per contract)`；实际 `build_fund_strategy_signals()` 只生成 40 个；
    契约 C7 要求 44 组 / 6 族 —— 三个数字互相矛盾。
  * 没有 `_grid_defs()`，无法与股票侧同构，B5 的族去相关无法复用。
  * 信号生成器直接读 `df['RSI']` / `df['MA20']` 等列，而基金净值数据只有 close，
    导致所有信号恒 False（修好 F01 后基金会永远 hold）。

现状（本文件）：
  * `_grid_defs()` 返回与股票侧同构的 `(prefix, factory, [kwargs...], namer)`；
  * `grid_size()` 由 `_grid_defs()` 派生，不再硬编码；
  * 族数 = 6，组合数 = 44：
        RSI 4×4=16 | 双均线 3×3=9 | MACD 2×2×2=8
        波动率目标 3×2=6 | 指数轮动 2×2=4 | 风险平价 1×1=1   → 44
  * 所有信号生成器只依赖 close，内部自算指标（close-only 数据可用）。
"""
from __future__ import annotations

import functools
from typing import Callable, Dict, List, Tuple

import numpy as np
import pandas as pd

FUND_GRID_SIZE = 44
FUND_FAMILY_COUNT = 6


# ---------------------------------------------------------------------------
# 指标与信号（close-only）
# ---------------------------------------------------------------------------
def _rsi(close: pd.Series, period: int = 14) -> pd.Series:
    """Wilder RSI（只用历史数据）。"""
    delta = close.diff()
    up = delta.clip(lower=0).ewm(alpha=1.0 / period, adjust=False).mean()
    down = (-delta.clip(upper=0)).ewm(alpha=1.0 / period, adjust=False).mean()
    rs = up / down.replace(0, np.nan)
    return 100.0 - 100.0 / (1.0 + rs)


def _empty_like(df: pd.DataFrame) -> Dict[str, pd.Series]:
    return {'buy': pd.Series(False, index=df.index),
            'sell': pd.Series(False, index=df.index)}


def make_rsi_signal(rsi_low: float, rsi_high: float) -> Callable:
    """RSI 超卖回升买入 / 超买回落卖出（状态式，非交叉式）。"""
    def sig_gen(df):
        if 'close' not in df.columns:
            return _empty_like(df)
        rsi = _rsi(df['close'], 14)
        buy = (rsi > rsi_low) & (rsi <= rsi_low + 15)
        sell = (rsi < rsi_high) & (rsi >= rsi_high - 15)
        return {'buy': buy.fillna(False).astype(bool), 'sell': sell.fillna(False).astype(bool)}
    return sig_gen


def make_risk_parity_signal(vol_window: int = 20) -> Callable:
    """风险平价（单标的）：波动率分位低（风险便宜）+ 价在均线上方时持有。

    语义：按"单位风险的价格"调节暴露 —— 波动率处于历史低分位时加仓，高分位时空仓。
    """
    def sig_gen(df):
        if 'close' not in df.columns:
            return _empty_like(df)
        close = df['close'].astype(float)
        vol = close.pct_change().rolling(vol_window).std() * np.sqrt(252)
        vol_rank = vol.rolling(120, min_periods=max(20, vol_window)).rank(pct=True)
        ma = close.rolling(vol_window).mean()
        buy = (vol_rank < 0.5) & (close > ma)
        sell = (vol_rank > 0.7) | (close < ma)
        return {'buy': buy.fillna(False).astype(bool), 'sell': sell.fillna(False).astype(bool)}
    return sig_gen


def _adapt(strategy_cls, **kwargs) -> Callable:
    """把 p2_strategies 的 `generate_signals`（返回 DataFrame）适配为 dict 形式。"""
    inst = strategy_cls(**kwargs)

    def sig_gen(df):
        out = inst.generate_signals(df)
        return {'buy': out['buy'].astype(bool), 'sell': out['sell'].astype(bool)}
    return sig_gen


# ---------------------------------------------------------------------------
# 网格定义（单一真源）
# ---------------------------------------------------------------------------
def _grid_defs() -> List[Tuple[str, Callable, List[dict], Callable]]:
    """[(prefix, factory, [kwargs...], namer)]，组合数必须 == FUND_GRID_SIZE。"""
    from fund_analysis.fund_strategy.p2_strategies import (
        FundMaStrategy, FundMacdStrategy, FundVolTargetStrategy, FundIndexRotationStrategy,
    )

    defs: List[Tuple[str, Callable, List[dict], Callable]] = []

    # 1. RSI 4×4 = 16
    defs.append((
        "RSI", make_rsi_signal,
        [{"rsi_low": lo, "rsi_high": hi} for lo in [20, 25, 30, 35] for hi in [65, 70, 75, 80]],
        lambda k: f"RSI({k['rsi_low']}-{k['rsi_high']})",
    ))
    # 2. 双均线 3×3 = 9
    defs.append((
        "双均线", lambda **k: _adapt(FundMaStrategy, **k),
        [{"short": s, "long": l} for s in [10, 20, 30] for l in [50, 60, 90]],
        lambda k: f"双均线({k['short']}-{k['long']})",
    ))
    # 3. MACD 2×2×2 = 8
    defs.append((
        "MACD", lambda **k: _adapt(FundMacdStrategy, **k),
        [{"fast": f, "slow": s, "signal": g} for f in [12, 26] for s in [26, 52] for g in [9, 18]],
        lambda k: f"MACD({k['fast']}-{k['slow']}-{k['signal']})",
    ))
    # 4. 波动率目标 3×2 = 6
    defs.append((
        "波动率目标", lambda **k: _adapt(FundVolTargetStrategy, **k),
        [{"target_vol": tv, "vol_window": vw} for tv in [0.10, 0.15, 0.20] for vw in [20, 60]],
        lambda k: f"波动率目标({k['vol_window']},{k['target_vol']})",
    ))
    # 5. 指数轮动 2×2 = 4
    defs.append((
        "指数轮动", lambda **k: _adapt(FundIndexRotationStrategy, **k),
        [{"short": s, "long": l} for s in [10, 20] for l in [60, 90]],
        lambda k: f"指数轮动({k['short']}-{k['long']})",
    ))
    # 6. 风险平价 1×1 = 1
    defs.append((
        "风险平价", make_risk_parity_signal,
        [{"vol_window": w} for w in [20]],
        lambda k: f"风险平价({k['vol_window']})",
    ))
    return defs


@functools.lru_cache(maxsize=1)
def build_fund_strategy_signals() -> Dict[str, Callable]:
    """{策略名: 信号生成器}；键数 == grid_size() == 44。"""
    signals: Dict[str, Callable] = {}
    for _prefix, factory, params_list, namer in _grid_defs():
        for kw in params_list:
            signals[namer(kw)] = factory(**kw)
    return signals


@functools.lru_cache(maxsize=1)
def family_map() -> Dict[str, str]:
    """{策略名: 族前缀}（F14 基金侧复用）。"""
    mapping: Dict[str, str] = {}
    for prefix, _factory, params_list, namer in _grid_defs():
        for kw in params_list:
            mapping[namer(kw)] = prefix
    return mapping


def family_of(name: str) -> str:
    return family_map().get(name, "unknown")


def family_names() -> List[str]:
    seen: List[str] = []
    for prefix, _f, _p, _n in _grid_defs():
        if prefix not in seen:
            seen.append(prefix)
    return seen


def grid_size() -> int:
    """由 `_grid_defs()` 派生（不再是硬编码算式）。"""
    return sum(len(params) for _p, _f, params, _n in _grid_defs())


# 向后兼容别名（旧调用方可能用 generate_* 形式）
generate_rsi_signal = make_rsi_signal
generate_risk_parity_signal = make_risk_parity_signal
