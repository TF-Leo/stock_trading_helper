# -*- coding: utf-8 -*-
"""Deprecated: strategy_master.config 兼容层。

D1 已删除本模块的独立实现，但
`stock_analysis/backtest/optimizer.py` 仍从本路径导入 `get_market_regime_series`，
故保留薄委托，避免 "ModuleNotFoundError / ImportError"。

单一 regime 真源：
    stock_analysis/advisor/analyzer.py::get_market_regime_series
"""
from __future__ import annotations

import warnings


def get_market_regime_series(start_date: str = "20200101"):
    """[Deprecated] 委托到 advisor.analyzer 的统一 regime 定义。

    离线或取数失败时返回 None（调用方均按 regimes=None 处理）。
    """
    warnings.warn(
        "strategy_master.config.get_market_regime_series 已废弃，"
        "请改用 stock_analysis.advisor.analyzer.get_market_regime_series",
        DeprecationWarning,
        stacklevel=2,
    )
    try:
        from stock_analysis.advisor.analyzer import get_market_regime_series as _impl
        return _impl(start_date)
    except Exception:
        return None


def load_config(*args, **kwargs):
    """[Deprecated] 配置加载已移除（D1）。"""
    raise NotImplementedError(
        "strategy_master.config.load_config 已废弃；请使用 CLI 参数 / decision.schema"
    )


def save_config(*args, **kwargs):
    """[Deprecated] 配置保存已移除（D1）。"""
    raise NotImplementedError(
        "strategy_master.config.save_config 已废弃；请使用 CLI 参数 / decision.schema"
    )


__all__ = ["get_market_regime_series", "load_config", "save_config"]