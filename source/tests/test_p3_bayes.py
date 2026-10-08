# -*- coding: utf-8 -*-
"""P3贝叶斯优化回归测试：快速+确定性+有建议输出。"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import pandas as pd
import numpy as np


def _toy(n=300, seed=9):
    rng = np.random.default_rng(seed)
    c = 100 * np.cumprod(1 + rng.normal(0.001, 0.02, n))
    idx = pd.date_range("2020-01-01", periods=n, freq="D")
    return pd.DataFrame({"open": c*0.999, "high": c*1.01, "low": c*0.99,
                         "close": c, "volume": 100000}, index=idx)


def test_bayes_fast_and_deterministic():
    import time
    from stock_analysis.backtest.optimizer import bayes_search
    from stock_analysis.backtest.engine import BacktestConfig
    df = _toy(300)
    t0 = time.time()
    b1, r1, c1 = bayes_search(df, None, BacktestConfig(), iters=20, seed=7)
    b2, r2, c2 = bayes_search(df, None, BacktestConfig(), iters=20, seed=7)
    dt = time.time() - t0
    assert b1 == b2, "同seed必须确定性"
    assert len(c1) <= 20 and len(c1) >= 10
    assert r1 is not None and r1.trade_count >= 0
    assert dt < 60, f"20次评估应远快于全网格581组，实际{dt:.1f}s"


def test_bayes_cli_entry_exists():
    import types
    sys.modules.setdefault("akshare", types.ModuleType("akshare"))
    import cli
    import inspect
    src = inspect.getsource(cli.main)
    assert "--opt" in src and "bayes" in src
    from stock_analysis.backtest.optimizer import run_bayes_tournament
    assert callable(run_bayes_tournament)
