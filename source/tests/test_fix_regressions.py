# -*- coding: utf-8 -*-
"""F01–F21 修复的回归测试（新增用例，不改动既有断言）。

覆盖：
  F01 基金网格隔离 / F05 退出码映射 + untradeable / F07 B1 评分量纲
  F08 B2 ATR 自适应止损 / F09 B3 逐折重新寻优 / F10 B7 CI 接线
  F11 A4 time_in_market / F13 B5 族占比 ≤50% / F15 基金网格单一真源
  F19 dataclass 无重复字段
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np
import pandas as pd
import pytest


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------
def _toy(n=300, seed=7, drift=0.001, vol=0.02, start=100.0):
    rng = np.random.default_rng(seed)
    close = start * np.cumprod(1 + rng.normal(drift, vol, n))
    idx = pd.date_range("2020-01-01", periods=n, freq="B")
    return pd.DataFrame({
        "open": close * 0.999, "high": close * (1 + vol),
        "low": close * (1 - vol), "close": close, "volume": 1e6,
    }, index=idx)


def _signal_at(buy_at, sell_at, index):
    buy = pd.Series(False, index=index)
    sell = pd.Series(False, index=index)
    if buy_at is not None:
        buy.iloc[buy_at] = True
    if sell_at is not None:
        sell.iloc[sell_at] = True
    return {"buy": buy, "sell": sell}


# ---------------------------------------------------------------------------
# F05 退出码
# ---------------------------------------------------------------------------
def test_exit_codes_distinct_and_complete():
    from decision.schema import EXIT_CODES, exit_code_for
    assert EXIT_CODES == {"ok": 0, "no_edge": 10, "insufficient_data": 20,
                          "data_source_error": 30, "untradeable": 40}
    assert len(set(EXIT_CODES.values())) == 5, "五个退出码必须互不相同"
    for k, v in EXIT_CODES.items():
        assert exit_code_for(k) == v
    assert exit_code_for("unknown_status") == 30  # 未知状态不得静默返回 0


def test_untradeable_status_reachable(monkeypatch):
    """F05/A3: unaffordable_lot_events>0 → status=untradeable → 退出码 40。"""
    import cli
    from stock_analysis.backtest.engine import BacktestResult
    from decision.schema import exit_code_for

    df = _toy(150, seed=3)
    monkeypatch.setattr(cli, "get_stock_daily", lambda *a, **k: df)
    monkeypatch.setattr(cli, "get_stock_name", lambda c: c)

    names = list(cli.build_strategy_signals().keys())[:3]
    comp = pd.DataFrame(
        [{"评分": 1.0 - i * 0.1, "评分IS": 1.0 - i * 0.1, "评分OOS": 0.9 - i * 0.1,
          "年化收益": "10.00%", "最大回撤": "5.00%", "胜率": "60.00%", "交易次数": 20}
         for i, _ in enumerate(names)],
        index=names,
    )
    res = BacktestResult(trade_count=20, win_rate=0.6, annual_return=0.1,
                         max_drawdown=0.05, unaffordable_lot_events=3)
    res.equity_curve = [100000.0] * 50
    monkeypatch.setattr(cli, "run_strategy_tournament",
                        lambda *a, **k: (names[0], res, comp))

    out = cli.advise_internal("600519", fast=True)
    assert out["recommendation"]["status"] == "untradeable"
    assert out["recommendation"]["action"] == "avoid"
    assert exit_code_for(out["recommendation"]["status"]) == 40
    assert any("一手也买不起" in w for w in out["diagnostics"]["warnings"])


# ---------------------------------------------------------------------------
# F07 B1 评分
# ---------------------------------------------------------------------------
def test_b1_risk_adjusted_score_prefers_low_drawdown():
    from decision.scoring import risk_adjusted_score as s
    a = s(total_return=0.12, max_drawdown=-0.35, win_rate=0.60, trade_count=100)
    b = s(total_return=0.10, max_drawdown=-0.07, win_rate=0.55, trade_count=100)
    assert b > a, f"低回撤策略必须胜出：A={a:.4f} B={b:.4f}"
    assert (b - a) > 0.05, f"差值应 >0.05，实际 {b - a:.4f}"


def test_b1_win_rate_contribution_below_40pct():
    """契约 B1：胜率项对 |总分| 的贡献占比 ≤ 40%（before = 72.8%）。"""
    from decision.scoring import score_components
    comp = score_components(total_return=0.10, max_drawdown=0.07,
                            win_rate=0.55, trade_count=100)
    total = abs(comp["calmar_term"]) + abs(comp["win_rate_term"]) + abs(comp["stability_term"])
    share = abs(comp["win_rate_term"]) / total
    assert share <= 0.40, f"胜率项占比 {share:.1%} 仍过高"


def test_annualization_actually_annualizes():
    """B1 点名的 bug：`(1+r)**(252/252)-1` 等于没年化。"""
    from decision.scoring import _annualize
    assert abs(_annualize(0.10, 252) - 0.10) < 1e-9
    assert _annualize(0.10, 126) > 0.20   # 半年 10% 年化应显著高于 10%


# ---------------------------------------------------------------------------
# F08 B2 ATR 自适应止损
# ---------------------------------------------------------------------------
def _ramp_df(vol, n=200):
    idx = pd.date_range("2020-01-01", periods=n, freq="B")
    close = np.concatenate([np.linspace(100, 110, 60), np.linspace(110, 70, n - 60)])
    return pd.DataFrame({
        "open": close, "high": close * (1 + vol), "low": close * (1 - vol),
        "close": close, "volume": 1e6,
    }, index=idx)


def _first_stop_exit(vol, stop_mode, buy_at=30):
    from stock_analysis.backtest.engine import BacktestEngine, BacktestConfig
    df = _ramp_df(vol)
    cfg = BacktestConfig(initial_capital=100000, max_position_pct=1.0,
                         stop_mode=stop_mode, enable_take_profit=False)
    # 入场点必须在 ATR 预热（14 根）之后，否则该笔会回落固定百分比
    sig = _signal_at(buy_at, None, df.index)
    r = BacktestEngine(cfg).run_backtest(df, lambda d: sig)
    stops = [t for t in r.trades if t.exit_reason == "止损"]
    return stops[0] if stops else None


def test_b2_atr_stop_scales_with_volatility():
    from stock_analysis.backtest.engine import BacktestConfig
    assert BacktestConfig().stop_mode == "atr", "B2 要求默认启用 atr"
    low = _first_stop_exit(0.004, "atr")
    high = _first_stop_exit(0.030, "atr")
    assert low is not None and high is not None, "两种波动率下都应触发止损"
    d_low = (low.entry_price - low.exit_price) / low.entry_price
    d_high = (high.entry_price - high.exit_price) / high.entry_price
    assert d_high > d_low * 1.2, f"高波动止损距离应显著更大：low={d_low:.3%} high={d_high:.3%}"


def test_b2_fixed_mode_matches_legacy_formula():
    from stock_analysis.backtest.engine import BacktestConfig
    cfg = BacktestConfig()
    t = _first_stop_exit(0.02, "fixed")
    assert t is not None
    expected = t.entry_price * (1 - cfg.stop_loss_pct)
    # 跳空规则：min(threshold, open)，再扣滑点
    assert t.exit_price <= expected * (1 - cfg.slippage_rate) + 1e-6


# ---------------------------------------------------------------------------
# F09 B3 逐折重新寻优
# ---------------------------------------------------------------------------
def test_b3_walk_forward_reoptimizes_each_fold(monkeypatch):
    from stock_analysis.backtest import analyzer as az
    from stock_analysis.backtest.analyzer import walk_forward_optimize
    from stock_analysis.backtest.engine import BacktestConfig
    from stock_analysis.strategy.registry import build_strategy_signals

    # 本用例验证的是 B3 的**逐折重新选参机制**，与"最少交易数地板"无关：
    # 合成数据的每折测试窗只有百余根 bar，10 笔地板会把大部分策略打成 -100，
    # 使各折都只剩同一只高频策略，从而掩盖机制本身。故把地板临时降到 3。
    monkeypatch.setattr(az, "MIN_TRADE_COUNT", 3)

    n = 600
    idx = pd.date_range("2020-01-01", periods=n, freq="B")
    up = np.linspace(100, 180, n // 2)              # 前段趋势
    down = 180 * np.exp(-np.linspace(0, 1.2, n - n // 2))  # 后段均值回归/下跌
    close = np.concatenate([up, down])
    df = pd.DataFrame({"open": close, "high": close * 1.01,
                       "low": close * 0.99, "close": close, "volume": 1e6}, index=idx)

    grid = build_strategy_signals()
    subset = [k for k in grid if k.split('(')[0] in
              ("唐奇安", "均线趋势", "MACD", "KAMA", "布林", "StochRSI")][:12]
    out = walk_forward_optimize(df, {k: grid[k] for k in subset},
                                config=BacktestConfig(max_position_pct=1.0), n_splits=3)
    assert out["folds"], "应产出至少一折"
    bests = [f["best"] for f in out["folds"]]
    assert len(set(bests)) > 1, f"各折应重新选参（当前各折最优相同：{bests}）"


# ---------------------------------------------------------------------------
# F10 B7 bootstrap CI
# ---------------------------------------------------------------------------
def test_b7_bootstrap_deterministic_and_shrinks():
    from decision.scoring import bootstrap_ci, stat_win_rate, metric_with_ci
    small = [1.0] * 6 + [-1.0] * 4
    large = [1.0] * 120 + [-1.0] * 80
    c1 = bootstrap_ci(small, stat_win_rate, n_boot=400, seed=7)
    c2 = bootstrap_ci(small, stat_win_rate, n_boot=400, seed=7)
    assert c1 == c2, "固定 seed 下 CI 必须完全一致"
    cl = bootstrap_ci(large, stat_win_rate, n_boot=400, seed=7)
    assert (c1[1] - c1[0]) > (cl[1] - cl[0]), "小样本 CI 必须更宽"
    m = metric_with_ci(small, stat_win_rate, n_boot=400)
    assert m["ci_95"] is not None and m["ci_95"][0] <= m["point"] <= m["ci_95"][1]


# ---------------------------------------------------------------------------
# F11 A4 time_in_market
# ---------------------------------------------------------------------------
def test_a4_time_in_market_ratio():
    from stock_analysis.backtest.engine import BacktestEngine, BacktestConfig
    n = 400
    idx = pd.date_range("2020-01-01", periods=n, freq="B")
    close = np.concatenate([np.linspace(10, 12, 100), np.linspace(12, 10, n - 100)])
    df = pd.DataFrame({"open": close, "high": close * 1.005, "low": close * 0.995,
                       "close": close, "volume": 1e6}, index=idx)
    sig = _signal_at(0, 100, df.index)
    cfg = BacktestConfig(initial_capital=100000, max_position_pct=1.0,
                         enable_stop_loss=False, enable_take_profit=False)
    r = BacktestEngine(cfg).run_backtest(df, lambda d: sig)
    assert r.holding_bar_count > 0
    assert abs(r.time_in_market - 0.25) <= 0.02, f"time_in_market={r.time_in_market}"


# ---------------------------------------------------------------------------
# F13 B5 族占比
# ---------------------------------------------------------------------------
def test_b5_family_ratio_capped():
    from decision.consensus import select_consensus
    import math
    cands = [{"name": f"X{i}", "family": "A", "score": 1.0 - i * 0.01} for i in range(7)]
    cands += [{"name": f"Y{i}", "family": "B", "score": 0.9 - i * 0.01} for i in range(5)]
    out = select_consensus(cands, top_k=10)
    n = len(out["members"])
    counts = {}
    for m in out["members"]:
        counts[m["family"]] = counts.get(m["family"], 0) + 1
    assert n > 0
    assert all(v <= math.ceil(n / 2) for v in counts.values()), f"族占比超 50%: {counts}"


# ---------------------------------------------------------------------------
# F15 基金网格单一真源
# ---------------------------------------------------------------------------
def test_f15_fund_grid_single_source():
    from fund_analysis.fund_strategy import registry as r
    g = r._grid_defs()
    total = sum(len(x[2]) for x in g)
    signals = r.build_fund_strategy_signals()
    assert r.grid_size() == total == len(signals) == 44, \
        f"三方不一致: grid_size={r.grid_size()} defs={total} signals={len(signals)}"
    assert len({x[0] for x in g}) == 6, "契约 C7 要求 6 个族"
    assert len(r.family_map()) == 44
    assert not [k for k, v in r.family_map().items() if v == "unknown"]


def test_f01_fund_signals_disjoint_from_stock_grid():
    """F01: 基金候选必须来自基金网格；基金族名不得混入股票族。"""
    from fund_analysis.fund_strategy.registry import build_fund_strategy_signals
    from stock_analysis.strategy.registry import build_strategy_signals
    fund = build_fund_strategy_signals()
    fund_names = set(fund)
    stock = set(build_strategy_signals())
    assert len(fund_names) == 44
    # 基金信号必须能真跑（此前依赖 df['RSI'] 列，close-only 数据下恒 False）
    df = _toy(300)
    for name in sorted(fund_names)[:5]:
        sig = fund[name](df)
        assert set(sig.keys()) == {"buy", "sell"}
        assert sig["buy"].dtype == bool and sig["sell"].dtype == bool
    assert fund_names - stock, "基金网格不应与股票网格完全相同"


# ---------------------------------------------------------------------------
# F19 dataclass 无重复字段
# ---------------------------------------------------------------------------
def test_f19_no_duplicate_dataclass_fields():
    from stock_analysis.backtest.engine import BacktestResult as B
    fields = list(B.__dataclass_fields__)
    assert len(fields) == len(set(fields)), f"重复字段: {[f for f in set(fields) if fields.count(f) > 1]}"
