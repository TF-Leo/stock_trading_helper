import pandas as pd
import numpy as np
from typing import Dict, List, Tuple, Callable

from stock_analysis.backtest.engine import (
    BacktestConfig,
    BacktestResult,
    BacktestEngine,
    Trade,
)


def _calculate_metrics(
    result: BacktestResult,
    equity_curve: List[float],
    market_regimes: pd.Series,
    df: pd.DataFrame,
):
    if not result.trades:
        return

    trades = result.trades
    result.trade_count = len(trades)

    wins = [t for t in trades if t.pnl > 0]
    losses = [t for t in trades if t.pnl <= 0]

    result.win_rate = len(wins) / len(trades) if trades else 0
    result.avg_win = np.mean([t.pnl_pct for t in wins]) if wins else 0
    result.avg_loss = np.mean([t.pnl_pct for t in losses]) if losses else 0
    result.avg_holding_days = np.mean([t.holding_days for t in trades])

    total_win = sum(t.pnl for t in wins)
    total_loss = abs(sum(t.pnl for t in losses))
    result.profit_factor = total_win / total_loss if total_loss > 0 else float('inf')

    result.max_consecutive_wins = _calc_max_streak(trades, win=True)
    result.max_consecutive_losses = _calc_max_streak(trades, win=False)

    if equity_curve:
        result.total_return = (equity_curve[-1] - equity_curve[0]) / equity_curve[0]
        days = len(equity_curve)
        result.annual_return = (1 + result.total_return) ** (252 / days) - 1
        result.max_drawdown = _calc_max_drawdown(equity_curve)
        returns = pd.Series(equity_curve).pct_change().dropna()
        if returns.std() > 0:
            result.sharpe_ratio = returns.mean() / returns.std() * np.sqrt(252)
        downside_returns = returns[returns < 0]
        if len(downside_returns) > 0 and downside_returns.std() > 0:
            result.sortino_ratio = returns.mean() / downside_returns.std() * np.sqrt(252)
        result.var_95 = np.percentile(returns, 5)

    if result.max_drawdown > 0:
        result.calmar_ratio = result.annual_return / result.max_drawdown

    if market_regimes is not None:
        bull_trades = []
        bear_trades = []
        shock_trades = []
        # P0: 停牌/日期不对齐时原精确匹配会掉到Shock，改用asof向前对齐
        try:
            mr = market_regimes.sort_index()
        except Exception:
            mr = market_regimes
        for t in trades:
            try:
                if t.entry_date in mr.index:
                    regime = mr.loc[t.entry_date]
                else:
                    # 取entry_date之前最近一个regime
                    prev = mr.loc[:t.entry_date]
                    regime = prev.iloc[-1] if len(prev) else 'Shock'
                if isinstance(regime, pd.Series):
                    regime = regime.iloc[-1]
            except Exception:
                regime = 'Shock'
            if regime == 'Bull':
                bull_trades.append(t)
            elif regime == 'Bear':
                bear_trades.append(t)
            else:
                shock_trades.append(t)
        result.bull_win_rate = len([t for t in bull_trades if t.pnl > 0]) / len(bull_trades) if bull_trades else 0
        result.bear_win_rate = len([t for t in bear_trades if t.pnl > 0]) / len(bear_trades) if bear_trades else 0
        result.shock_win_rate = len([t for t in shock_trades if t.pnl > 0]) / len(shock_trades) if shock_trades else 0


def _calc_max_drawdown(equity_curve: List[float]) -> float:
    peak = equity_curve[0]
    max_dd = 0
    for val in equity_curve:
        if val > peak:
            peak = val
        dd = (peak - val) / peak
        if dd > max_dd:
            max_dd = dd
    return max_dd


def _calc_max_streak(trades: List[Trade], win: bool) -> int:
    max_streak = 0
    current_streak = 0
    for t in trades:
        if (win and t.pnl > 0) or (not win and t.pnl <= 0):
            current_streak += 1
            max_streak = max(max_streak, current_streak)
        else:
            current_streak = 0
    return max_streak


def compare_strategies(
    df: pd.DataFrame,
    strategies: Dict[str, Callable],
    market_regimes: pd.Series = None,
    config: BacktestConfig = None,
    jobs: int = 1,
) -> pd.DataFrame:
    """P2: jobs>1时用线程池并行跑锦标赛（IO/计算混合，pandas释放GIL部分加速）。
    默认jobs=1保证测试确定性；CLI可传jobs=4/8。"""
    engine = BacktestEngine(config)
    items = list(strategies.items())
    if jobs is None or jobs <= 1:
        results = []
        for name, signal_gen in items:
            result = engine.run_backtest(df, signal_gen, market_regimes)
            result_dict = result.to_dict()
            result_dict['策略'] = name
            results.append(result_dict)
    else:
        import concurrent.futures

        def _run(pair):
            name, signal_gen = pair
            eng = BacktestEngine(config)
            r = eng.run_backtest(df, signal_gen, market_regimes)
            d = r.to_dict()
            d['策略'] = name
            return d

        with concurrent.futures.ThreadPoolExecutor(max_workers=jobs) as ex:
            results = list(ex.map(_run, items))
    df_results = pd.DataFrame(results)
    df_results = df_results.set_index('策略')
    return df_results


def _build_strategy_grid() -> dict:
    """系统化参数网格：P1统一注册表委托，单一事实来源见 strategy/registry.py。"""
    from stock_analysis.strategy.registry import build_strategy_signals
    return build_strategy_signals()


def _wilson_lower_bound(win_rate: float, n: int, z: float = 2.576) -> float:
    """Wilson score 下界：在样本量 n 下对胜率的保守下界估计。

    样本越少，下界越低于观测胜率（惩罚不确定性）；
    样本越多，下界越接近观测胜率。z=2.576 对应 99% 置信度（更严苛的小样本惩罚）。

    例: 胜率 100% / 3 次  → 下界 ~29.5%（极保守，避免小样本过拟合）
        胜率 71.4% / 7 次  → 下界 ~25.9%
        胜率 66.7% / 24 次 → 下界 ~47.3%
        胜率 60.0% / 25 次 → 下界 ~40.7%（接近观测值）
    """
    if n == 0:
        return 0.0
    p = win_rate
    denom = 1 + z * z / n
    center = (p + z * z / (2 * n)) / denom
    spread = z * ((p * (1 - p) + z * z / (4 * n)) / n) ** 0.5 / denom
    return max(0.0, center - spread)


# 最小交易次数门槛：低于此值的策略直接判 -100，避免小样本过拟合
MIN_TRADE_COUNT = 10
# P1: 样本外最小交易数与Calmar阈值
OOS_MIN_TRADES = 5
MIN_CALMAR_OOS = 0.5
TOP_N_OOS = 50


def split_train_test(df: pd.DataFrame, train_ratio: float = 0.7):
    """P1: 时序切分，前train_ratio为训练，后为测试（无未来泄漏）。"""
    n = len(df)
    cut = int(n * train_ratio)
    cut = max(30, min(n - 30, cut)) if n > 60 else n
    return df.iloc[:cut].copy(), df.iloc[cut:].copy()


def split_regimes(market_regimes: pd.Series, train_df: pd.DataFrame, test_df: pd.DataFrame):
    if market_regimes is None:
        return None, None
    try:
        tr = market_regimes.loc[train_df.index[0]:train_df.index[-1]]
        te = market_regimes.loc[test_df.index[0]:test_df.index[-1]]
        return tr, te
    except Exception:
        return market_regimes, market_regimes


def walk_forward_splits(df: pd.DataFrame, n_splits: int = 3):
    """P1: TimeSeriesSplit式walk-forward，训练集递增，测试集滚动。返回[(train,test),...]。"""
    n = len(df)
    if n < 100 or n_splits < 1:
        return []
    # 分成 n_splits+1 等份
    chunk = n // (n_splits + 1)
    folds = []
    for i in range(n_splits):
        train_end = chunk * (i + 1)
        test_end = chunk * (i + 2) if i + 2 <= n_splits else n
        if train_end < 60 or test_end - train_end < 30:
            continue
        folds.append((df.iloc[:train_end].copy(), df.iloc[train_end:test_end].copy()))
    return folds


def score_backtest_result(r: "BacktestResult") -> float:
    """P1: 统一评分 Wilson*0.5+年化*0.2-回撤*0.3，低于MIN_TRADE_COUNT判-100。"""
    if r.trade_count < MIN_TRADE_COUNT:
        return -100.0
    adj = _wilson_lower_bound(r.win_rate, r.trade_count)
    return adj * 0.5 + r.annual_return * 0.2 - r.max_drawdown * 0.3


def score_oos_result(r: "BacktestResult") -> float:
    if r.trade_count < OOS_MIN_TRADES:
        return -100.0
    adj = _wilson_lower_bound(r.win_rate, r.trade_count)
    s = adj * 0.5 + r.annual_return * 0.2 - r.max_drawdown * 0.3
    # P1: Calmar过滤软惩罚
    try:
        calmar = r.calmar_ratio if r.calmar_ratio else 0.0
    except Exception:
        calmar = 0.0
    if calmar < MIN_CALMAR_OOS:
        s -= 0.1
    return s


def adaptive_oos_config(df: pd.DataFrame, base: BacktestConfig = None) -> BacktestConfig:
    """P1修复：高价股(600519)在测试切片起点用10万买不起1手导致OOS全0交易。
    按全样本最高价自适应放大OOS初始资金，保证至少可买1手；收益率类评分对资金规模近似不变。"""
    import copy
    base = base or BacktestConfig()
    try:
        px = float(df['close'].max())
    except Exception:
        return base
    need = px * 100 / max(getattr(base, 'max_position_pct', 1.0), 0.1) * 1.2
    if need > base.initial_capital:
        cfg = copy.copy(base)
        cfg.initial_capital = float(need)
        return cfg
    return base


def run_strategy_tournament(
    df: pd.DataFrame,
    market_regimes: pd.Series = None,
    config: BacktestConfig = None,
    train_ratio: float = 0.7,
    n_splits: int = 3,
    oos_weight: float = 0.7,
    jobs: int = 1,
) -> Tuple[str, BacktestResult, pd.DataFrame]:
    """P1: 样本内选优→样本外验证两阶段锦标赛，按样本外排名。

    - 全量IS评分取Top50，再在test slice + walk-forward folds上算OOS；
    - final = (1-oos_weight)*IS + oos_weight*OOS均值，OOS交易数<5判-100，Calmar<0.5软惩罚-0.1；
    - 保持返回 (best_name, best_result全量回测, comparison含IS/OOS列) 向后兼容。
    """
    strategies = _build_strategy_grid()
    engine = BacktestEngine(config)

    comparison = compare_strategies(df, strategies, market_regimes, config, jobs=jobs)

    def parse_pct(s):
        return float(s.replace('%', '')) / 100

    # 评分IS：Wilson下界*0.5+年化*0.2-回撤*0.3，<MIN_TRADE_COUNT判-100
    scores = {}
    adj_win_rates = {}
    for name in comparison.index:
        win_rate = parse_pct(comparison.loc[name, '胜率'])
        annual_ret = parse_pct(comparison.loc[name, '年化收益'])
        max_dd = parse_pct(comparison.loc[name, '最大回撤'])
        trade_count = int(comparison.loc[name, '交易次数'])
        if trade_count < MIN_TRADE_COUNT:
            scores[name] = -100
            adj_win_rates[name] = 0.0
        else:
            adj_wr = _wilson_lower_bound(win_rate, trade_count)
            adj_win_rates[name] = adj_wr
            scores[name] = adj_wr * 0.5 + annual_ret * 0.2 - max_dd * 0.3
    comparison['评分IS'] = [scores.get(n, -100) for n in comparison.index]
    comparison['评分'] = comparison['评分IS']  # 向后兼容旧列名
    comparison['样本调整胜率'] = [
        f'{adj_win_rates.get(n, 0.0) * 100:.2f}%' for n in comparison.index
    ]

    # P1两阶段：IS TopN再算OOS（test slice + walk-forward均值），按OOS排名
    oos_scores: Dict[str, float] = {n: -100.0 for n in comparison.index}
    oos_calmars: Dict[str, float] = {}
    oos_trades: Dict[str, int] = {}
    try:
        ranked_is = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
        candidates = [n for n, s in ranked_is if s > -100][:TOP_N_OOS]
        if not candidates:
            candidates = [n for n, _s in ranked_is[:TOP_N_OOS]]
        train_df, test_df = split_train_test(df, train_ratio)
        train_reg, test_reg = split_regimes(market_regimes, train_df, test_df)
        wf_folds = walk_forward_splits(df, n_splits)
        oos_cfg = adaptive_oos_config(df, config)
        engine_oos = BacktestEngine(oos_cfg)
        # 切分regime到fold
        for name in candidates:
            sig = strategies[name]
            # test slice OOS（自适应资金保证高价股可买1手）
            r_test = engine_oos.run_backtest(test_df, sig, test_reg)
            s_test = score_oos_result(r_test)
            oos_calmars[name] = float(r_test.calmar_ratio or 0.0)
            oos_trades[name] = int(r_test.trade_count)
            # walk-forward均值
            wf_scores = []
            for ftr, fte in wf_folds:
                try:
                    ftr_reg, fte_reg = split_regimes(market_regimes, ftr, fte)
                    r_fte = engine_oos.run_backtest(fte, sig, fte_reg)
                    wf_scores.append(score_oos_result(r_fte))
                except Exception:
                    continue
            s_wf = float(sum(wf_scores) / len(wf_scores)) if wf_scores else s_test
            # 综合OOS：test与WF平均
            s_oos = (s_test + s_wf) / 2.0
            oos_scores[name] = s_oos
    except Exception:
        pass
    comparison['评分OOS'] = [oos_scores.get(n, -100.0) for n in comparison.index]
    comparison['Calmar_OOS'] = [round(float(oos_calmars.get(n, 0.0)), 3) if n in oos_calmars else 0.0 for n in comparison.index]
    comparison['交易数_OOS'] = [int(oos_trades.get(n, 0)) for n in comparison.index]
    # final = (1-w)*IS + w*OOS；OOS未评估(-100初值但IS高)的非候选保持IS排名 fallback
    final_scores: Dict[str, float] = {}
    for n in comparison.index:
        s_is = float(scores.get(n, -100))
        s_oos = float(oos_scores.get(n, -100.0))
        if n not in oos_calmars:
            # 非Top候选：不参与OOS竞争，保持IS（避免全-100淹没）
            final_scores[n] = s_is - 50.0 if s_is > -100 else -100.0
        else:
            if s_is <= -100 and s_oos <= -100:
                final_scores[n] = -100.0
            elif s_oos <= -100:
                final_scores[n] = (1 - oos_weight) * s_is + oos_weight * (-100.0)
            else:
                final_scores[n] = (1 - oos_weight) * s_is + oos_weight * s_oos
    comparison['评分'] = [final_scores.get(n, -100) for n in comparison.index]
    comparison['评分Final'] = comparison['评分']

    best_name = max(final_scores, key=final_scores.get)
    best_result = engine.run_backtest(df, strategies[best_name], market_regimes)

    # P0: 评分地板 — 与买入持有/空仓基线对比，低于基线则提示无优势
    # 买入持有：首收盘买入、尾收盘卖出（含一次双边费用近似）
    try:
        first_c = float(df['close'].iloc[0])
        last_c = float(df['close'].iloc[-1])
        bh_gross = (last_c - first_c) / first_c if first_c > 0 else 0.0
        _cfg = config or BacktestConfig()
        _fee = max(first_c * 100 * _cfg.commission_rate, _cfg.min_commission) / 100000.0
        bh_total = bh_gross - _fee * 2 - _cfg.slippage_rate * 2 - 0.001
        days = max(len(df), 1)
        bh_annual = (1 + bh_total) ** (252 / days) - 1 if bh_total > -1 else -1.0
    except Exception:
        bh_total, bh_annual = 0.0, 0.0
    best_score = scores.get(best_name, -100)
    # 无优势判定：全员-100 / 最优年化同时跑输空仓(0)与买持 / 最优总收益为负且跑输买持
    no_edge = False
    if best_score <= -100:
        no_edge = True
    elif best_result.annual_return <= 0 and best_result.annual_return <= bh_annual:
        no_edge = True
    elif best_result.total_return < bh_total and best_result.total_return <= 0:
        no_edge = True
    if no_edge:
        best_name = best_name + "（无优势，建议观望）"
    # 把基线写入对比表尾部便于核查（不参与评分排序）
    try:
        comparison.loc["__买入持有基线__", "年化收益"] = f"{bh_annual:.2%}"
        comparison.loc["__买入持有基线__", "总收益率"] = f"{bh_total:.2%}"
    except Exception:
        pass

    return best_name, best_result, comparison


def calc_buy_hold_baseline(df: pd.DataFrame, config: BacktestConfig = None) -> dict:
    """供CLI/测试复用的买入持有基线。"""
    cfg = config or BacktestConfig()
    try:
        first_c = float(df['close'].iloc[0])
        last_c = float(df['close'].iloc[-1])
        gross = (last_c - first_c) / first_c if first_c > 0 else 0.0
        fee = max(first_c * 100 * cfg.commission_rate, cfg.min_commission) / 100000.0
        total = gross - fee * 2 - cfg.slippage_rate * 2 - 0.001
        days = max(len(df), 1)
        annual = (1 + total) ** (252 / days) - 1 if total > -1 else -1.0
    except Exception:
        total, annual = 0.0, 0.0
    return {"total_return": total, "annual_return": annual}
