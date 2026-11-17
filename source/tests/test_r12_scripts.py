# -*- coding: utf-8 -*-
"""R12（issue #146）脚本的离线单测。

只测纯函数：不联网、不读 ``~/data/r12``、不碰任何产品决策逻辑。
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import numpy as np
import pandas as pd

SRC = Path(__file__).resolve().parents[1]
ROOT = SRC.parent
TOOLS = ROOT / "tools"
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, TOOLS / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


lib = _load("r12_lib")
analysis = _load("r12_analysis")
h3 = _load("r12_h3")
fetch = _load("r12_fetch")


def test_period_end_uses_last_trading_day_no_lookahead():
    idx = pd.bdate_range("2024-01-01", "2024-03-29")
    s = pd.Series(np.arange(len(idx), dtype=float), index=idx)
    w = lib.period_end(s, "W")
    # 每个周频点必须是本周一~周五内的最后一个交易日，且值等于该日原值
    for d, v in w.items():
        assert d.weekday() == 4 or (idx[idx <= d][-1] == d)
        assert float(s.loc[d]) == float(v)
    m = lib.period_end(s, "M")
    assert list(m.index.strftime("%Y-%m")) == ["2024-01", "2024-02", "2024-03"]
    assert m.iloc[0] == s.loc[s.index[s.index.month == 1][-1]]


def test_oos_r2_extremes():
    y = pd.Series([0.01, -0.02, 0.03, 0.005])
    bench = pd.Series(0.0, index=y.index)
    assert lib.oos_r2(y, y.copy(), bench) == 1.0
    assert abs(lib.oos_r2(y, bench.copy(), bench)) < 1e-12
    # 比基准更差 → 负 R²
    assert lib.oos_r2(y, -y, bench) < 0


def test_direction_hit_and_dm():
    y = pd.Series([1.0, -1.0, 2.0, -2.0])
    assert lib.direction_hit(y, y.copy()) == 1.0
    assert lib.direction_hit(y, -y) == 0.0
    e1 = pd.Series(np.zeros(50))
    e2 = pd.Series(np.linspace(0.01, 1.0, 50))
    stat, p = lib.dm_test(e1, e2)
    assert stat < 0 and p < 0.05          # 模型1误差更小 → 负统计量、显著
    assert np.isnan(lib.dm_test(e1, e1)[0])   # 损失差恒 0，统计量无定义


def test_forward_drawdown_definition():
    # 0→10→5→20：第 0 点未来 2 日内最大回撤 = min(0,10,5)/0 - 1（此处基准 100）
    close = pd.Series([100.0, 90.0, 95.0, 80.0, 120.0],
                      index=pd.bdate_range("2024-01-01", periods=5))
    dd = lib.forward_drawdown(close, horizon=2)
    assert abs(dd.iloc[0] - (-0.10)) < 1e-12   # 未来两日最低 90
    assert abs(dd.iloc[2] - (80.0 / 95.0 - 1.0)) < 1e-12   # 未来两日最低 80
    assert np.isnan(dd.iloc[-1])          # 最后一根没有未来


def test_adf_flags_white_noise_stationary():
    rng = np.random.default_rng(0)
    s = pd.Series(rng.normal(size=300))
    res = lib.adf_tstat(s)
    assert res["t"] < -2.0 and res["stationary_5pct"] is True


def test_ols_recovers_exact_linear_relation():
    rng = np.random.default_rng(1)
    x = rng.normal(size=200)
    y = 0.5 + 2.0 * x
    res = analysis._ols(y, x.reshape(-1, 1))
    assert res["r2"] == 1.0
    assert abs(res["coef"][1] - 2.0) < 1e-9


def test_expanding_forecast_never_uses_future_data():
    idx = pd.bdate_range("2020-01-01", periods=40)
    y = pd.Series(np.zeros(len(idx)), index=idx)
    y.iloc[20:] = 1.0
    X = pd.DataFrame({"x": np.arange(len(idx), dtype=float)}, index=idx)
    pred = lib.expanding_forecast(y, X, "2020-01-20")
    # 在 2020-01-20 之前训练集 y 全为 0，预测必须恰为 0
    assert abs(float(pred.iloc[20])) < 1e-12


def test_perm_pvalue_detects_monotone_relation():
    x = np.arange(20, dtype=float)
    assert h3._perm_pvalue(x, x, n=500) < 0.01
    rng = np.random.default_rng(3)
    assert h3._perm_pvalue(x, rng.permutation(x), n=500) > 0.01


def test_month_ends_are_calendar_month_ends():
    days = fetch.month_ends("2024-01", "2024-03")
    assert days == ["2024-01-31", "2024-02-29", "2024-03-31"]
