# -*- coding: utf-8 -*-
"""剩余roadmap回归测试：指数轮动+风险平价（股票）+指数增强轮动（基金）。"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import pandas as pd
import numpy as np


def _toy(n=300, seed=21):
    rng = np.random.default_rng(seed)
    c = 100 * np.cumprod(1 + rng.normal(0.001, 0.02, n))
    idx = pd.date_range("2020-01-01", periods=n, freq="D")
    return pd.DataFrame({"open": c*0.999, "high": c*1.01, "low": c*0.99,
                         "close": c, "volume": 100000}, index=idx)


def test_registry_has_rotation_and_risk_parity():
    from stock_analysis.strategy.registry import build_strategy_signals, grid_size
    g = build_strategy_signals()
    assert grid_size() == len(g) == 581
    assert sum(1 for k in g if k.startswith("指数轮动")) == 4
    assert sum(1 for k in g if k.startswith("风险平价")) == 2
    assert "策略组合(2票)" in g  # 投票组合仍在


def test_rotation_riskparity_signals_valid_and_no_lookahead():
    from stock_analysis.strategy import EtfRotationStrategy, RiskParityStrategy
    df = _toy(300)
    for strat in [EtfRotationStrategy(), EtfRotationStrategy(ma_short=10, ma_long=30),
                  RiskParityStrategy(), RiskParityStrategy(vol_window=60)]:
        sig = strat.generate_trade_signals(df)
        assert set(sig.columns) == {"buy", "sell"}
        assert sig['buy'].dtype == bool and sig['sell'].dtype == bool
        assert not (sig['buy'] & sig['sell']).any(), "同bar不应同时买卖"
        # 截断不变性：前150条信号不应随未来数据改变（无前视）
        sig_cut = strat.generate_trade_signals(df.iloc[:150])
        a = sig['buy'].iloc[30:140].values
        b = sig_cut['buy'].iloc[30:140].values
        assert (a == b).all(), f"{strat.name}前视"


def test_rotation_with_benchmark_gate():
    from stock_analysis.strategy import EtfRotationStrategy
    df = _toy(300)
    bench_up = pd.Series(np.linspace(100, 200, 300), index=df.index)
    bench_down = pd.Series(np.linspace(200, 100, 300), index=df.index)
    s_up = EtfRotationStrategy(benchmark=bench_up).generate_trade_signals(df)['buy'].sum()
    s_dn = EtfRotationStrategy(benchmark=bench_down).generate_trade_signals(df)['buy'].sum()
    assert s_up >= s_dn, "基准下跌时门控应抑制买入"


def test_fund_index_rotation():
    from fund_analysis.fund_strategy.p2_strategies import FundIndexRotationStrategy
    import cli
    idx = pd.date_range("2023-01-01", periods=800, freq="D")
    close = np.linspace(1.0, 2.0, 800)
    df = pd.DataFrame({"close": close}, index=idx)
    strat = FundIndexRotationStrategy(short=10, long=60)
    assert strat.name == "指数轮动(10-60)"
    sig = strat.generate_signals(df)
    assert set(sig.columns) == {"buy", "sell"}
    stats = cli._fund_backtest_signals(df, sig)
    assert stats is not None and stats["trade_count"] >= 0
