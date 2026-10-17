# -*- coding: utf-8 -*-
"""B1/B4/B7: Scoring system - risk-adjusted scoring, Deflated Sharpe, Bootstrap CI"""
import numpy as np
from typing import Dict, List, Tuple, Optional

# Score weights (B1)
SCORE_WEIGHTS = {
    'risk_adjusted': 0.6,  # Calmar/Sharpe primary
    'win_rate': 0.2,       # Win rate secondary
    'stability': 0.2,      # Stability
}


def risk_adjusted_score(total_return: float, max_drawdown: float,
                       win_rate: float, trade_count: int) -> float:
    """B1: Risk-adjusted score with Calmar as primary term"""
    if max_drawdown == 0:
        calmar = float('inf') if total_return > 0 else 0
    else:
        annual_return = (1 + total_return) ** (252 / max(1, 252)) - 1
        calmar = annual_return / abs(max_drawdown) if max_drawdown != 0 else 0
    
    # Wilson lower bound adjustment for win rate
    adj_win_rate = wilson_lower_bound(win_rate, trade_count) if trade_count > 0 else 0
    
    score = (SCORE_WEIGHTS['risk_adjusted'] * min(calmar, 10) +  # Cap at 10 to avoid overflow
             SCORE_WEIGHTS['win_rate'] * adj_win_rate +
             SCORE_WEIGHTS['stability'] * min(trade_count / 60, 1.0))
    
    return score


def wilson_lower_bound(p: float, n: int, z: float = 1.96) -> float:
    """Wilson score interval lower bound"""
    if n == 0:
        return 0.0
    center = (p + z*z/(2*n)) / (1 + z*z/n)
    width = z * np.sqrt((p*(1-p) + z*z/(4*n)) / n) / (1 + z*z/n)
    return max(0.0, center - width)


def bootstrap_ci(values: List[float], stat_fn, n_boot: int = 2000, seed: int = 7) -> Tuple[float, float]:
    """B7: Bootstrap 95% 置信区间。

    返回 (lower, upper) 百分位区间。样本量 < 2 时退化为点估计。
    """
    if values is None or len(values) < 2:
        v = float(values[0]) if values else 0.0
        return (v, v)
    rng = np.random.RandomState(seed)
    arr = np.asarray(values, dtype=float)
    boot_stats = np.empty(n_boot, dtype=float)
    n = len(arr)
    for i in range(n_boot):
        idx = rng.randint(0, n, n)
        boot_stats[i] = stat_fn(arr[idx])
    lower = float(np.percentile(boot_stats, 2.5))
    upper = float(np.percentile(boot_stats, 97.5))
    return (lower, upper)


def point_stat(values: List[float], stat_fn) -> float:
    """统计量的点估计（与 bootstrap_ci 配对的中间值）。"""
    if not values:
        return 0.0
    return float(stat_fn(np.asarray(values, dtype=float)))


def _norm_cdf(x: float) -> float:
    """标准正态 CDF（用 erf，避免 scipy 依赖）。"""
    from math import erf, sqrt
    return 0.5 * (1.0 + erf(x / sqrt(2.0)))


def _norm_ppf(p: float) -> float:
    """标准正态分位数（Acklam 有理逼近，精度 1e-9，避免 scipy 依赖）。"""
    if p <= 0.0:
        return -8.0
    if p >= 1.0:
        return 8.0
    a = [-3.969683028665376e+01, 2.209460984245205e+02, -2.759285104469687e+02,
         1.383577518672690e+02, -3.066479806614716e+01, 2.506628277459239e+00]
    b = [-5.447609879822406e+01, 1.615858368580409e+02, -1.556989798598866e+02,
         6.680131188771972e+01, -1.328068155288572e+01]
    c = [-7.784894002430293e-03, -3.223964580411365e-01, -2.400758277161838e+00,
         -2.549732539343734e+00, 4.374664141464968e+00, 2.938163982698783e+00]
    d = [7.784695709041462e-03, 3.224671290700398e-01, 2.445134137142996e+00,
         3.754408661907416e+00]
    plow, phigh = 0.02425, 1 - 0.02425
    if p < plow:
        q = np.sqrt(-2 * np.log(p))
        return (((((c[0]*q + c[1])*q + c[2])*q + c[3])*q + c[4])*q + c[5]) / \
               ((((d[0]*q + d[1])*q + d[2])*q + d[3])*q + 1)
    if p > phigh:
        q = np.sqrt(-2 * np.log(1 - p))
        return -(((((c[0]*q + c[1])*q + c[2])*q + c[3])*q + c[4])*q + c[5]) / \
                ((((d[0]*q + d[1])*q + d[2])*q + d[3])*q + 1)
    q = p - 0.5
    r = q * q
    return (((((a[0]*r + a[1])*r + a[2])*r + a[3])*r + a[4])*r + a[5]) * q / \
           (((((b[0]*r + b[1])*r + b[2])*r + b[3])*r + b[4])*r + 1)


def deflated_sharpe(sharpe: float, n_trials: int, n_obs: int = 252,
                    skew: float = 0.0, kurtosis: float = 3.0) -> float:
    """B4: Deflated Sharpe Ratio（Bailey & López de Prado 2014）。

    给定观察到的 Sharpe 与试验次数 n_trials，扣掉"多重检验带来的最大期望 Sharpe"后，
    返回**校正后 Sharpe 的单侧概率**（0..1）。实现要点：

        SR0 = sqrt(Var(SR_trials)) * [(1-γ)Φ^-1(1-1/N) + γΦ^-1(1-1/(N·e))]
        Z   = (SR - SR0) * sqrt(n_obs-1) /
              sqrt(1 - skew·SR + (kurtosis-1)/4 · SR^2)
        DSR = Φ(Z)

    其中 γ 为 Euler–Mascheroni 常数；Var(SR_trials) 用 1/(n_obs-1) 近似
    （在只有单条策略序列时这是标准近似，见原论文 §3）。
    """
    n_trials = max(int(n_trials), 1)
    n_obs = max(int(n_obs), 2)
    sharpe = float(sharpe)
    if n_trials == 1:
        sr0 = 0.0
    else:
        gamma = 0.5772156649015329
        var_sr = 1.0 / (n_obs - 1)
        sd = np.sqrt(var_sr)
        sr0 = sd * ((1 - gamma) * _norm_ppf(1 - 1.0 / n_trials)
                    + gamma * _norm_ppf(1 - 1.0 / (n_trials * np.e)))
    denom = 1.0 - skew * sharpe + (kurtosis - 1.0) / 4.0 * sharpe ** 2
    if denom <= 0:
        denom = 1e-9
    z = (sharpe - sr0) * np.sqrt(n_obs - 1) / np.sqrt(denom)
    return float(_norm_cdf(z))


def deflated_sharpe_from_returns(returns: List[float], n_trials: int) -> float:
    """从收益序列直接算 DSR（自动取偏度/峰度与观察数）。

    注意口径：这里全程使用**同频（逐期）Sharpe**，不做年化。
    DSR 的 SR0 与样本量 n_obs 是同一频度下的量，混入年化 Sharpe 会让结果饱和到 0/1。
    """
    if returns is None or len(returns) < 3:
        return 0.0
    r = np.asarray(returns, dtype=float)
    sd = float(np.std(r, ddof=1))
    if sd <= 0:
        return 0.0
    mu = float(np.mean(r))
    sharpe = mu / sd  # 同频 Sharpe（不年化）
    skew = float(((r - mu) ** 3).mean() / (sd ** 3)) if sd > 0 else 0.0
    kurt = float(((r - mu) ** 4).mean() / (sd ** 4)) if sd > 0 else 3.0
    return deflated_sharpe(sharpe, n_trials, n_obs=len(r), skew=skew, kurtosis=kurt)


def deflated_sharpe_from_annual(annual_sharpe: float, n_trials: int,
                                n_obs: int, periods_per_year: int = 252) -> float:
    """年化 Sharpe → DSR。内部先换成同频 Sharpe，避免量纲错配导致饱和。"""
    per = max(int(periods_per_year), 1)
    same_freq = float(annual_sharpe) / np.sqrt(per)
    return deflated_sharpe(same_freq, n_trials, n_obs=n_obs)


def equity_to_returns(equity: List[float]) -> List[float]:
    """权益曲线 → 逐期简单收益率序列（用于 bootstrap/DSR）。"""
    if not equity or len(equity) < 2:
        return []
    eq = np.asarray(equity, dtype=float)
    prev = eq[:-1]
    mask = prev != 0
    if not mask.any():
        return []
    return list((eq[1:][mask] - prev[mask]) / prev[mask])


def excess_return_ci(strategy_equity: List[float], benchmark_equity: List[float],
                     n_boot: int = 2000, seed: int = 7) -> Dict:
    """B8: 相对基准超额收益的 bootstrap 95% CI（配对抽样）。

    返回 {'point', 'ci_95', 'p_positive'}；p_positive = 超额收益 > 0 的 bootstrap 占比，
    用于 no_edge 判定（而不是只看年化点值）。
    """
    sr = equity_to_returns(strategy_equity)
    br = equity_to_returns(benchmark_equity)
    n = min(len(sr), len(br))
    if n < 3:
        return {'point': 0.0, 'ci_95': [0.0, 0.0], 'p_positive': 0.0, 'n_obs': n}
    s = np.asarray(sr[:n], dtype=float)
    b = np.asarray(br[:n], dtype=float)
    diff = s - b
    point = float(np.mean(diff))
    rng = np.random.RandomState(seed)
    boots = np.empty(n_boot, dtype=float)
    for i in range(n_boot):
        idx = rng.randint(0, n, n)
        boots[i] = diff[idx].mean()
    lo = float(np.percentile(boots, 2.5))
    hi = float(np.percentile(boots, 97.5))
    p_pos = float((boots > 0).mean())
    return {'point': point, 'ci_95': [lo, hi], 'p_positive': p_pos, 'n_obs': n}


def calculate_dispersion(scores: List[float]) -> float:
    """Calculate score standard deviation (for consensus dispersion)"""
    if len(scores) < 2:
        return 0.0
    return float(np.std(scores))
