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
    """B7: Bootstrap 95% confidence interval"""
    rng = np.random.RandomState(seed)
    boot_stats = []
    
    for _ in range(n_boot):
        sample = rng.choice(values, size=len(values), replace=True)
        boot_stats.append(stat_fn(sample))
    
    lower = np.percentile(boot_stats, 2.5)
    upper = np.percentile(boot_stats, 97.5)
    
    return (lower, upper)


def deflated_sharpe(sharpe: float, n_trials: int, skew: float = 0, kurtosis: float = 3) -> float:
    """B4: Deflated Sharpe Ratio - multiple testing correction"""
    # Simplified DSR calculation
    adjustment = np.sqrt(2 * np.log(max(n_trials, 1))) / 10  # Assume ~100 samples
    dsr = sharpe - adjustment
    
    return dsr


def calculate_dispersion(scores: List[float]) -> float:
    """Calculate score standard deviation (for consensus dispersion)"""
    if len(scores) < 2:
        return 0.0
    return float(np.std(scores))
