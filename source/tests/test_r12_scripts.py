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
h2 = _load("r12_h2")
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


def test_last_close_before_information_available():
    """strict=True 必须排除期间末当天的外盘收盘（A 股 15:00 尚未发生）。"""
    idx = pd.bdate_range("2024-01-01", "2024-01-31")
    s = pd.Series(np.arange(len(idx), dtype=float) + 100.0, index=idx)
    end = pd.Series([pd.Timestamp("2024-01-12"), pd.Timestamp("2024-01-19")],
                    index=pd.PeriodIndex(["2024-01-12", "2024-01-19"], freq="W-FRI"))
    strict = lib.last_close_before(s, end, strict=True)
    loose = lib.last_close_before(s, end, strict=False)
    assert strict.iloc[0] == s.loc["2024-01-11"]      # 周四收盘
    assert loose.iloc[0] == s.loc["2024-01-12"]       # 含周五收盘（收盘后重叠）
    assert strict.iloc[1] == s.loc["2024-01-18"]
    assert loose.iloc[1] == s.loc["2024-01-19"]


def test_alpha_diagnostics_noise_vs_dispersion():
    # 纯噪声：alpha 的横截面离散小于估计噪声 → λ<1、τ=0
    rng = np.random.default_rng(0)
    se = np.full(200, 0.01)
    a_noise = rng.normal(0, 0.005, size=200)
    d0 = lib.alpha_diagnostics(a_noise, se, a_noise / se)
    assert d0["alpha_lambda"] < 1.0
    assert d0["alpha_tau_ann"] == 0.0
    # 有真实横截面离散：λ>1、τ>0
    a_sig = rng.normal(0, 0.03, size=200)
    d1 = lib.alpha_diagnostics(a_sig, se, a_sig / se)
    assert d1["alpha_lambda"] > 1.0
    assert d1["alpha_tau_ann"] > 0.0


def test_ols_hac_reduces_to_ols_without_lags():
    rng = np.random.default_rng(1)
    x = rng.normal(size=300)
    y = 0.2 + 1.5 * x + rng.normal(size=300)
    a = lib.ols_hac(y, x, lags=0)
    A = np.column_stack([np.ones(len(x)), x])
    b, *_ = np.linalg.lstsq(A, y, rcond=None)
    e = y - A @ b
    se = np.sqrt((float(e @ e) / (len(y) - 2)) * np.linalg.pinv(A.T @ A)[0, 0])
    assert abs(a["alpha"] - b[0]) < 1e-12
    assert abs(a["se_alpha"] - se) < 1e-12
    assert abs(a["beta"] - b[1]) < 1e-12


def test_gaussian_calibration_near_nominal_five_percent():
    # 旧判据 |t|>2 在高斯零假设下应接近 5%（合成噪声的理论值）
    res = h2.run_gaussian_calibration(n=200, reps=2000, seed=3)
    assert 0.02 < res["share_abs_t_gt_2"] < 0.08


def test_block_bootstrap_indices_are_bounded_and_full_length():
    rng = np.random.default_rng(2)
    idx = h2._block_idx(503, 20, rng)
    assert len(idx) == 503
    assert idx.min() >= 0 and idx.max() < 503


def test_cal_window_smoke_returns_null_and_power():
    rng = np.random.default_rng(4)
    T, N = 520, 30
    x = rng.normal(0, 0.012, T)
    Y = np.array([0.8 * x + rng.normal(0, 0.012, T) for _ in range(N)]).T
    ew = rng.normal(0, 0.01, T)
    res = h2._cal_window(Y, x, ew, rng, B=5)
    for key in ("lambda_obs", "p_lambda", "portfolio_alpha_csi",
                "portfolio_alpha_ew", "power_lambda"):
        assert key in res
    assert 0.0 <= res["p_lambda"] <= 1.0
    assert res["n_stocks"] == N
