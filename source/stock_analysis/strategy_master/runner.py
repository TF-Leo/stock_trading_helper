# -*- coding: utf-8 -*-
"""Deprecated: strategy_master.runner 兼容层。

契约来源：`source/tests/test_p2_engineering.py::test_deprecated_runner_delegates`
——该测试要求本模块存在、发出 DeprecationWarning、且返回 details 含 "trade_count"。
（契约同时规定"禁止删除 tests 中现有断言"，故 D1 的删除动作不能删掉这个委托入口。）

寻优真源：
    stock_analysis/backtest/optimizer.py::bayes_search
"""
from __future__ import annotations

import warnings


def optimize_stock_strategy(df, config=None, *, iters: int = 20, seed: int = 7):
    """[Deprecated] 委托到 `backtest.optimizer.bayes_search`。

    返回 ``(best_name, details)``；``details`` 至少含 ``trade_count``，并附带对比表。
    """
    warnings.warn(
        "strategy_master.runner.optimize_stock_strategy 已废弃，"
        "请改用 stock_analysis.backtest.optimizer.bayes_search",
        DeprecationWarning,
        stacklevel=2,
    )
    from stock_analysis.backtest.engine import BacktestConfig
    from stock_analysis.backtest.optimizer import bayes_search

    cfg = config if config is not None else BacktestConfig(max_position_pct=1.0)
    best_name, best_result, comparison = bayes_search(df, None, cfg, iters=iters, seed=seed)
    details = {
        "best_name": best_name,
        "trade_count": int(getattr(best_result, "trade_count", 0)),
        "annual_return": float(getattr(best_result, "annual_return", 0.0)),
        "max_drawdown": float(getattr(best_result, "max_drawdown", 0.0)),
        "win_rate": float(getattr(best_result, "win_rate", 0.0)),
        "comparison": comparison,
    }
    return best_name, details


__all__ = ["optimize_stock_strategy"]