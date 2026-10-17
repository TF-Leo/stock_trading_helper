# -*- coding: utf-8 -*-
"""Deprecated strategy_master 包（向后兼容层，非真源）。

真源迁移去向：
  - 配置/契约      -> decision/schema.py
  - 单一 regime 定义 -> stock_analysis/advisor/analyzer.py
  - 寻优           -> stock_analysis/backtest/optimizer.py
保留本包仅因 tests/test_p2_engineering.py 依赖其导入路径。
"""
from stock_analysis.strategy_master.config import (
    get_market_regime_series,
    load_config,
    save_config,
)

__all__ = ["load_config", "save_config", "get_market_regime_series"]