# -*- coding: utf-8 -*-
"""条件期望：判断"这一侧的信号在这个标的上到底有没有边际"。

为什么替换掉全样本 alpha 检验
------------------------------
v2.0 用"策略超额收益 vs 买入持有的 95% CI 下界 > 0"作为买入门槛。那是
**策略是否值得信任**的检验，不是**这笔交易能不能做**的判断。它几乎不可能通过，
于是 `action=buy` 实际不可达，系统变成哑巴（2026-10-25 用户指出）。

数据（14 只标的，2018 起）显示：
  * 1–5 日尺度所有因子 IC 不显著（|t|<2）→ 该尺度本就不该给方向；
  * 20 日尺度买点条件期望 +1.72%，13/14 只为正 → 买入信号有真实边际；
  * 20 日尺度卖点条件期望 +1.04%，仅 5/14 为负 → 趋势反转卖出没有边际。

因此本模块只做一件事：给定信号序列与持有期 H，算出该侧的历史条件期望，
并给出一个**可复算的可用性判定**（样本量、均值、t 值）。它不关心全样本 alpha。
"""
from __future__ import annotations

from typing import Any, Dict, Optional

import numpy as np
import pandas as pd

DEFAULT_HORIZON = 20      # 契约 v3.0：默认有效期 20 个交易日（IC t=4.4 峰值）
MIN_SAMPLES = 30          # 少于 30 个样本不给方向
MIN_T = 1.5               # 均值显著性下限（单侧，弱于 1.96，允许弱信号但要求方向一致）
WARMUP_BARS = 250         # 回放时的预热期

EXPECTANCY_KEYS = ('side', 'horizon', 'mean', 'hit_rate', 'n', 't_stat', 'ok')


def null_expectancy(side: str = 'buy', horizon: int = DEFAULT_HORIZON) -> Dict[str, Any]:
    """错误/不可用路径：键齐全、值为 null（G35 同构）。"""
    return {'side': side, 'horizon': horizon, 'mean': None, 'hit_rate': None,
            'n': 0, 't_stat': None, 'ok': False}


def side_expectancy(close: pd.Series, signal, horizon: int = DEFAULT_HORIZON,
                    side: str = 'buy') -> Dict[str, Any]:
    """该侧信号在 `horizon` 日后的条件期望。

    Args:
        close: 收盘价序列
        signal: 布尔序列（买点或卖点）
        horizon: 持有期（交易日）
        side: 'buy'（涨为对）或 'sell'（跌为对）

    Returns:
        {side, horizon, mean, hit_rate, n, t_stat, ok}
        `ok` 的判据：n >= 30 且方向正确（buy: mean>0；sell: mean<0）且 |t| >= 1.5
    """
    out = null_expectancy(side, horizon)
    if close is None or len(close) <= horizon + 5:
        return out
    c = pd.Series(np.asarray(close, dtype=float))
    sig = pd.Series(np.asarray(signal, dtype=bool)).reindex(c.index).fillna(False).to_numpy()
    fwd = (c.shift(-horizon) / c - 1.0).to_numpy(dtype=float)
    vals = fwd[sig & np.isfinite(fwd)]
    n = int(len(vals))
    out['n'] = n
    if n == 0:
        return out
    mean = float(np.mean(vals))
    sd = float(np.std(vals, ddof=1)) if n > 1 else 0.0
    t_stat = (mean / (sd / np.sqrt(n))) if sd > 0 else 0.0
    hit = float(np.mean(vals < 0)) if side == 'sell' else float(np.mean(vals > 0))
    direction_ok = (mean < 0) if side == 'sell' else (mean > 0)
    out.update({
        'mean': round(mean, 6),
        'hit_rate': round(hit, 4),
        't_stat': round(float(t_stat), 2),
        'ok': bool(n >= MIN_SAMPLES and direction_ok and abs(t_stat) >= MIN_T),
    })
    return out


def trailing_expectancy_ok(close: pd.Series, signal, horizon: int = DEFAULT_HORIZON,
                           min_samples: int = MIN_SAMPLES, min_t: float = MIN_T,
                           warmup: int = WARMUP_BARS) -> np.ndarray:
    """逐日给出"截至该日、用**已实现**结果算出的买点期望是否为正"。

    回放必须用它而不是全样本期望，否则就是前视偏差：
    在第 t 根 K 线上，只能使用 `j + horizon <= t` 的那些买点的已实现收益。

    Returns:
        布尔数组，长度同 close；预热期内一律 False。
    """
    c = np.asarray(close, dtype=float)
    n = len(c)
    ok = np.zeros(n, dtype=bool)
    if n <= horizon + 10:
        return ok
    sig = np.asarray(signal, dtype=bool)[:n]
    fwd = np.full(n, np.nan)
    fwd[:-horizon] = c[horizon:] / c[:-horizon] - 1.0
    usable = sig & np.isfinite(fwd)
    y = np.where(usable, fwd, 0.0)
    y2 = np.where(usable, fwd * fwd, 0.0)
    cnt = usable.astype(np.float64)

    cs = np.cumsum(cnt)
    cs1 = np.cumsum(y)
    cs2 = np.cumsum(y2)

    for t in range(n):
        end = t - horizon          # 只用到 j <= t-horizon 的已实现收益
        if end < warmup:
            continue
        k = cs[end]
        if k < min_samples:
            continue
        s1 = cs1[end]
        s2 = cs2[end]
        mean = s1 / k
        var = max(s2 / k - mean * mean, 0.0) * k / max(k - 1, 1)
        if var <= 0:
            continue
        t_stat = mean / np.sqrt(var / k)
        ok[t] = bool(mean > 0 and t_stat >= min_t)
    return ok
