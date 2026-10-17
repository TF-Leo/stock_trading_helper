# -*- coding: utf-8 -*-
"""B1/B4/B7: Scoring system - risk-adjusted scoring, Deflated Sharpe, Bootstrap CI"""
import numpy as np
from typing import Dict, List, Tuple, Optional

# Score weights (B1) —— 权重只允许在此处定义，analyzer 不得另写一套
SCORE_WEIGHTS = {
    'risk_adjusted': 0.6,  # Calmar/Sharpe primary
    'win_rate': 0.2,       # Win rate secondary
    'stability': 0.2,      # Stability
}
# Calmar 上限，避免极端小回撤导致评分发散
CALMAR_CAP = 10.0
# 交易数达到该值即认为"稳定性"满格（B1 的 stability 项）
STABILITY_TRADE_FULL = 60.0


def _annualize(total_return: float, n_bars: int, periods_per_year: int = 252) -> float:
    """把区间总收益年化。n_bars<=0 时视为已年化，原样返回。

    历史缺陷：旧实现写成 `(1+total_return)**(252/max(1,252))-1`，指数恒为 1，
    等于没有年化（B1 明确点名的 bug）。
    """
    if not n_bars or int(n_bars) <= 0:
        return float(total_return)
    if total_return <= -1.0:
        return -1.0
    return float((1.0 + total_return) ** (periods_per_year / max(int(n_bars), 1)) - 1.0)


def score_components(total_return: float, max_drawdown: float,
                     win_rate: float, trade_count: int,
                     n_bars: int = None, periods_per_year: int = 252) -> Dict[str, float]:
    """B1: 拆出三项分量，供"胜率项贡献占比 ≤ 40%"的验证与门禁使用。"""
    mdd = abs(float(max_drawdown))
    if mdd == 0:
        calmar = CALMAR_CAP if total_return > 0 else 0.0
    else:
        calmar = _annualize(total_return, n_bars, periods_per_year) / mdd
    adj_win_rate = wilson_lower_bound(win_rate, trade_count) if trade_count > 0 else 0.0
    stability = min(float(trade_count) / STABILITY_TRADE_FULL, 1.0)
    return {
        'calmar': float(calmar),
        'calmar_term': SCORE_WEIGHTS['risk_adjusted'] * min(calmar, CALMAR_CAP),
        'win_rate_term': SCORE_WEIGHTS['win_rate'] * adj_win_rate,
        'stability_term': SCORE_WEIGHTS['stability'] * stability,
        'adj_win_rate': float(adj_win_rate),
        'annual_return': _annualize(total_return, n_bars, periods_per_year),
    }


def risk_adjusted_score(total_return: float, max_drawdown: float,
                        win_rate: float, trade_count: int,
                        n_bars: int = None, periods_per_year: int = 252) -> float:
    """B1: 以风险调整收益（Calmar）为主项的评分。

    total_return 为**区间总收益**；传 n_bars 时按该 bar 数年化后再算 Calmar，
    不传则视为调用方已给出年化收益（保持对旧调用点/测试的兼容）。
    """
    c = score_components(total_return, max_drawdown, win_rate, trade_count,
                         n_bars=n_bars, periods_per_year=periods_per_year)
    return c['calmar_term'] + c['win_rate_term'] + c['stability_term']


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


# ---------------------------------------------------------------------------
# B7: 逐期收益 / 逐笔盈亏序列上的常用统计量（供 metrics.ci_95 使用）
# 全部用 numpy 自实现，不引入 scipy。
# ---------------------------------------------------------------------------
def stat_total_return(vals) -> float:
    arr = np.asarray(vals, dtype=float)
    if arr.size == 0:
        return 0.0
    return float(np.prod(1.0 + arr) - 1.0)


def stat_annual_return(vals, periods_per_year: int = 252) -> float:
    arr = np.asarray(vals, dtype=float)
    n = int(arr.size)
    if n == 0:
        return 0.0
    tr = float(np.prod(1.0 + arr) - 1.0)
    if tr <= -1.0:
        return -1.0
    return float((1.0 + tr) ** (periods_per_year / n) - 1.0)


def stat_sharpe(vals, periods_per_year: int = 252) -> float:
    arr = np.asarray(vals, dtype=float)
    if arr.size < 2:
        return 0.0
    sd = float(np.std(arr, ddof=1))
    if sd <= 0:
        return 0.0
    return float(np.mean(arr) / sd * np.sqrt(periods_per_year))


def stat_max_drawdown(vals) -> float:
    """由逐期收益序列重建权益曲线，返回最大回撤（正数，与 BacktestResult 口径一致）。"""
    arr = np.asarray(vals, dtype=float)
    if arr.size == 0:
        return 0.0
    eq = np.cumprod(1.0 + arr)
    peak = np.maximum.accumulate(eq)
    with np.errstate(divide='ignore', invalid='ignore'):
        dd = np.where(peak > 0, 1.0 - eq / peak, 0.0)
    return float(np.max(dd)) if dd.size else 0.0


def stat_calmar(vals, periods_per_year: int = 252) -> float:
    mdd = stat_max_drawdown(vals)
    if mdd <= 0:
        return 0.0
    return float(stat_annual_return(vals, periods_per_year) / mdd)


def stat_win_rate(vals) -> float:
    arr = np.asarray(vals, dtype=float)
    if arr.size == 0:
        return 0.0
    return float((arr > 0).mean())


def stat_profit_factor(vals) -> float:
    arr = np.asarray(vals, dtype=float)
    if arr.size == 0:
        return 0.0
    win = float(arr[arr > 0].sum())
    loss = float(-arr[arr < 0].sum())
    if loss <= 0:
        return float(win) if win > 0 else 0.0
    return float(win / loss)


def metric_with_ci(values, stat_fn, n_boot: int = 2000, seed: int = 7) -> Dict:
    """B7: 返回 {'point', 'ci_95'}；样本不足 2 时 ci_95 为 None（不硬造区间）。

    ci_95 会外扩到包含点估计 —— 百分位法在偏态分布下可能不含点估计，
    而对外报"点估计不在自己的 95% 区间内"属于自相矛盾（C4）。
    """
    vals = [float(v) for v in (values or [])]
    if len(vals) < 2:
        return {'point': float(vals[0]) if vals else 0.0, 'ci_95': None}
    point = float(stat_fn(np.asarray(vals, dtype=float)))
    lo, hi = bootstrap_ci(vals, stat_fn, n_boot=n_boot, seed=seed)
    lo = float(min(lo, point))
    hi = float(max(hi, point))
    return {'point': round(point, 6), 'ci_95': [round(lo, 6), round(hi, 6)]}


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
