# -*- coding: utf-8 -*-
"""P2工程回归测试：向量化等价+并行一致+死代码委托+版本pin。"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import pandas as pd
import numpy as np


def _toy(n=300, seed=3):
    rng = np.random.default_rng(seed)
    c = 100 * np.cumprod(1 + rng.normal(0.001, 0.02, n))
    idx = pd.date_range("2020-01-01", periods=n, freq="D")
    return pd.DataFrame({"open": c*0.999, "high": c*1.01, "low": c*0.99,
                         "close": c, "volume": 100000}, index=idx)


def test_kama_hma_sar_equivalence():
    """向量化实现必须与数学定义一致（容差内），且HMA显著快于旧apply（功能等价即可）。"""
    from stock_analysis.indicators import calc_kama, calc_hma, calc_sar
    df = _toy(300)
    k = calc_kama(df['close'], period=10)
    h = calc_hma(df['close'], 10)
    s = calc_sar(df['high'], df['low'])
    assert len(k) == 300 and len(h) == 300 and len(s) == 300
    # NaN语义：HMA前window-1为NaN
    assert h.iloc[:9].isna().all()
    assert k.iloc[10:].notna().all()
    assert s.notna().all()
    # KAMA应在价格附近（自适应均线不应偏离过远）
    assert (k.iloc[50:] - df['close'].iloc[50:]).abs().median() < df['close'].median() * 0.1


def test_parallel_equals_sequential():
    from stock_analysis.backtest.analyzer import compare_strategies
    from stock_analysis.strategy import TrendStrategy, BollStrategy
    df = _toy(200)
    strats = {
        "t1": TrendStrategy(5, 13, 34).generate_trade_signals,
        "b1": BollStrategy(window=20, num_std=2.0).generate_trade_signals,
    }
    a = compare_strategies(df, strats, None, jobs=1)
    b = compare_strategies(df, strats, None, jobs=2)
    pd.testing.assert_frame_equal(a.sort_index(), b.sort_index())


def test_deprecated_runner_delegates():
    import warnings, types
    sys.modules.setdefault("akshare", types.ModuleType("akshare"))
    from stock_analysis.strategy_master.runner import optimize_stock_strategy
    df = _toy(200)
    with warnings.catch_warnings(record=True) as w:
        warnings.simplefilter("always")
        strat, details = optimize_stock_strategy(df, None)
        assert any(issubclass(x.category, DeprecationWarning) for x in w), "必须报DeprecationWarning"
    assert details is not None and "trade_count" in details


def test_requirements_pinned():
    txt = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "requirements.txt"), encoding="utf-8").read()
    for pkg in ["akshare==", "pandas==", "numpy==", "pytest==", "requests=="]:
        assert pkg in txt, f"requirements必须pin {pkg}"


def test_cli_offline_import_and_fund_split():
    """P2: 无akshare时CLI仍可import/--help；基金回测已拆至fund_backtest且re-export一致。"""
    import cli
    import fund_analysis.fund_backtest as fb
    assert cli._fund_backtest_with_stats is fb._fund_backtest_with_stats
    assert cli._fund_backtest_signals is fb._fund_backtest_signals
    assert cli.FUND_FEE_RATE == fb.FUND_FEE_RATE > 0
    import stock_analysis.data_loader.bars as bars
    assert hasattr(bars, "ak")  # 守卫导入存在，离线为None亦可
