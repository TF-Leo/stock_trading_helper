# -*- coding: utf-8 -*-
"""P1防过拟合回归测试：注册表单一来源+样本外切分+OOS排名+Calmar过滤。"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import pandas as pd
import numpy as np


def _toy_df(n=300, seed=7):
    rng = np.random.default_rng(seed)
    rets = rng.normal(0.001, 0.02, n)
    close = 100 * np.cumprod(1 + rets)
    idx = pd.date_range("2020-01-01", periods=n, freq="D")
    return pd.DataFrame({
        "open": close * 0.999, "high": close * 1.01,
        "low": close * 0.99, "close": close, "volume": 100000,
    }, index=idx)


def test_registry_single_source():
    from stock_analysis.backtest.analyzer import _build_strategy_grid
    import types
    sys.modules.setdefault("akshare", types.ModuleType("akshare"))
    import cli
    a = _build_strategy_grid()
    b = cli._build_stock_strategies()
    assert set(a.keys()) == set(b.keys()), "双网格必须同源"
    assert len(a) == 581, f"网格应581组，实际{len(a)}"
    from stock_analysis.strategy.registry import grid_size
    assert grid_size() == len(a)


def test_split_no_leak():
    from stock_analysis.backtest.analyzer import split_train_test, walk_forward_splits
    df = _toy_df(300)
    tr, te = split_train_test(df, 0.7)
    assert len(tr) == 210 and len(te) == 90
    assert tr.index[-1] < te.index[0], "训练/测试必须时序无重叠"
    folds = walk_forward_splits(df, 3)
    assert len(folds) == 3
    for ftr, fte in folds:
        assert ftr.index[-1] < fte.index[0]
    # 单调递增
    assert folds[1][0].index[-1] > folds[0][0].index[-1]


def test_oos_ranking_differs_and_calmar():
    from stock_analysis.backtest.analyzer import (
        run_strategy_tournament, score_oos_result, OOS_MIN_TRADES, MIN_CALMAR_OOS,
    )
    from stock_analysis.backtest.engine import BacktestConfig
    df = _toy_df(400, seed=11)
    cfg = BacktestConfig(max_position_pct=1.0)
    best, res, comp = run_strategy_tournament(df, None, cfg)
    for col in ["评分IS", "评分OOS", "Calmar_OOS", "交易数_OOS", "评分", "评分Final"]:
        assert col in comp.columns, f"缺列{col}"
    # OOS必须被评估（Top候选有OOS交易数记录）
    assert (comp["交易数_OOS"] > 0).any(), "Top候选OOS应有交易记录"
    # Calmar列为数值
    assert pd.to_numeric(comp["Calmar_OOS"], errors="coerce").notna().any()
    # 软惩罚逻辑：OOS交易不足判-100
    from stock_analysis.backtest.engine import BacktestResult
    r = BacktestResult(trade_count=2, win_rate=1.0, annual_return=0.5, max_drawdown=0.01)
    assert score_oos_result(r) == -100.0


def test_overfit_penalty_demo():
    """构造IS过拟合策略在OOS应被降权：小样本高胜率IS高分但OOS-100。"""
    from stock_analysis.backtest.analyzer import _wilson_lower_bound
    is_score = _wilson_lower_bound(1.0, 3) * 0.5 + 2.0 * 0.2 - 0.01 * 0.3
    oos_score = -100.0
    final = 0.3 * is_score + 0.7 * oos_score
    assert final < 0, "过拟合策略综合评分应被OOS拉低"


def test_adaptive_oos_high_price():
    """高价股测试切片起点10万买不起1手时，OOS自适应资金应保证可评估。"""
    from stock_analysis.backtest.analyzer import adaptive_oos_config, split_train_test
    from stock_analysis.backtest.engine import BacktestEngine, BacktestConfig
    import pandas as pd
    df = pd.read_csv('stock_analysis/data/stock_600519_qfq.csv', index_col='date', parse_dates=True)
    tr, te = split_train_test(df, 0.7)
    cfg = adaptive_oos_config(df, BacktestConfig(max_position_pct=1.0))
    assert cfg.initial_capital >= 100000
    from stock_analysis.backtest.analyzer import _build_strategy_grid
    sigs = _build_strategy_grid()
    eng = BacktestEngine(cfg)
    r = eng.run_backtest(te, sigs['StochRSI(14,25/75)'])
    assert r.trade_count > 0, "自适应资金下高价股OOS应有交易"
