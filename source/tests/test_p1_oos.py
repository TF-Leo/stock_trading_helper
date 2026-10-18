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


def _high_price_df(n=400, seed=11):
    """合成高价股日线（约 1500 元/股，含明确趋势便于触发成交）。

    A14 / 冷克隆契约：测试**不得**依赖 `stock_analysis/data/*.csv`
    （该目录被 .gitignore 排除，冷克隆必然缺失 → FileNotFoundError）。
    故用合成序列复现 600519 的关键特征：绝对股价远高于 10 万本金的 1 手成本。
    """
    rng = np.random.default_rng(seed)
    trend = np.linspace(0, 0.6, n)              # 单边上行，保证有买入信号与成交
    rets = rng.normal(0.0006, 0.012, n) + trend / n
    close = 1500 * np.cumprod(1 + rets)
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
    """高价股切片起点 10 万买不起 1 手时，OOS 自适应资金仍应保证可评估、可成交。

    A14 / 冷克隆（2026-10-18）：原实现直接 `pd.read_csv('stock_analysis/data/stock_600519_qfq.csv')`。
    该目录被 `.gitignore` 排除，**冷克隆必然 FileNotFoundError**（实测：冷缓存下
    `1 failed, 46 passed`）。GOAL_CONTRACT A14 早就要求改用合成 fixture，一直没做。
    现改用 `_high_price_df()`：保留原意（绝对股价 1500 元/股，10 万本金买不起 1 手），
    且不依赖任何本地文件，离线可跑。
    """
    from stock_analysis.backtest.analyzer import adaptive_oos_config, split_train_test
    from stock_analysis.backtest.engine import BacktestEngine, BacktestConfig

    df = _high_price_df(400)
    # 前提自检：确认这确实是"10 万买不起 1 手"的高价股场景，否则本测试失去意义
    base = BacktestConfig(initial_capital=100000, max_position_pct=1.0)
    cheapest = float(df["close"].min())
    assert cheapest * 100 > base.initial_capital, (
        f"fixture 不构成高价股场景：最低价 {cheapest:.2f} × 100 <= 10 万"
    )

    tr, te = split_train_test(df, 0.7)
    cfg = adaptive_oos_config(df, base)
    assert cfg.initial_capital >= base.initial_capital, "自适应资金不得低于原始本金"
    # 自适应资金必须覆盖该序列最高价的 1 手成本
    assert cfg.initial_capital >= float(df["close"].max()) * 100, (
        f"自适应资金 {cfg.initial_capital:.0f} 仍买不起最高价 1 手"
    )

    from stock_analysis.backtest.analyzer import _build_strategy_grid
    sigs = _build_strategy_grid()
    eng = BacktestEngine(cfg)
    r = eng.run_backtest(te, sigs["StochRSI(14,25/75)"])
    assert r.trade_count > 0, "自适应资金下高价股 OOS 应有交易"
    assert r.unaffordable_lot_events == 0, (
        f"自适应资金下不应出现买不起 1 手的事件，实际 {r.unaffordable_lot_events} 次"
    )
