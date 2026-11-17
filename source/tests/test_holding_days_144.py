# -*- coding: utf-8 -*-
"""issue #144：持有天数改用结构化字段 ``recommendation.holding_days``。

背景：渲染层此前从 ``action_reason`` 文本里正则解析「第 N/M 个交易日」，
文案一改就静默失配；``signal.last_buy_age`` 语义不同（距最近买点信号），
持仓期间出现新买点会被重置（实测 600519：2 vs 实际 21）。

本文件锁住三件事：

1. **字段来源**：CLI payload 的 ``recommendation.holding_days`` 与决策层
   ``decision.policy.run_policy`` 的 ``bars_held`` 逐值一致（多样本）；
2. **不回退文本**：把 ``build_reasons`` 的文案整体改掉（不再包含任何
   「第 N/M 个交易日」）后，渲染出的持有天数不变；
3. **缺失即省略**：字段为 ``null`` 时主行只写「继续持有」，既不解析
   ``action_reason``，也不退回 ``signal.last_buy_age``。

测试标的固定为 600519 / 600000，不使用任何真实持仓代码。
"""
from __future__ import annotations

import pytest

import cli
from stock_analysis.advice_render import render_advice

_MAIN_ROW_PREFIXES = ("可买", "继续持有", "暂不买", "卖出", "不参与")


def _toy_df(n=150, seed=7, drift=0.001, vol=0.02, start=100.0):
    import numpy as np
    import pandas as pd

    rng = np.random.default_rng(seed)
    close = start * np.cumprod(1 + rng.normal(drift, vol, n))
    idx = pd.date_range("2020-01-01", periods=n, freq="B")
    return pd.DataFrame({
        "open": close * 0.999, "high": close * (1 + vol),
        "low": close * (1 - vol), "close": close, "volume": 1e6,
    }, index=idx)


def _advise(monkeypatch, *, code="600519", holding=True, bars_held=8,
            wording=None):
    """离线驱动 ``cli.advise_internal``；``wording`` 可整体替换理由文案。

    复用 ``test_horizon_wording_139._advise_offline`` 的离线骨架：行情、
    报价、证据块全部打桩，``run_policy`` 先跑真实现再覆写仓位事实，保证
    ``holding_days`` 的来源仍是决策层的 ``bars_held`` 而不是测试自造值。
    """
    import pandas as pd

    import stock_analysis.data_loader.meta as meta
    from stock_analysis.backtest.engine import BacktestResult
    from stock_analysis.data_loader.flow import empty_flow_block
    from stock_analysis.data_loader.status import TradingStatus
    from stock_analysis.data_loader.valuation import empty_valuation_block

    df = _toy_df()
    monkeypatch.setattr(meta, "resolve_and_fetch", lambda *a, **k: (df, "stock", None))
    monkeypatch.setattr(cli, "last_fetch_info", lambda: {
        "source": "cache", "from_cache": True, "degraded": False,
        "cache_last_date": "2026-11-13", "lag_days": 0,
        "expected_date": "2026-11-13", "requested_source": "cache",
        "source_attempts": [],
    })
    monkeypatch.setattr(cli, "get_stock_name", lambda c: "示例银行")
    monkeypatch.setattr(cli, "resolve_trading_status", lambda *a, **k: TradingStatus(
        is_st=False, is_suspended=False, limit_up=13.2, limit_down=10.8,
        quote_stale=False, status_source="eastmoney_st_list+tencent_quote"))
    monkeypatch.setattr(cli, "evidence_context", lambda *a, **k: (
        empty_flow_block(code=code),
        empty_valuation_block(code=code, reason="cache_missing"),
        [],
    ))

    names = list(cli.build_strategy_signals().keys())[:3]
    comparison = pd.DataFrame(
        [{"评分": 1.0 - i * 0.1, "评分IS": 1.0 - i * 0.1,
          "评分OOS": 0.9 - i * 0.1, "年化收益": "10.00%",
          "最大回撤": "5.00%", "胜率": "60.00%", "交易次数": 20}
         for i, _ in enumerate(names)],
        index=names,
    )
    result = BacktestResult(trade_count=20, win_rate=0.6, annual_return=0.1,
                            max_drawdown=0.05, unaffordable_lot_events=0)
    result.equity_curve = [100000.0] * 50
    result.avg_holding_days = 20.0
    monkeypatch.setattr(cli, "run_strategy_tournament",
                        lambda *a, **k: (names[0], result, comparison))

    real_run_policy = cli.pol_mod.run_policy

    def _spy(df_, buy, exp_ok, horizon=20, **kwargs):
        out = real_run_policy(df_, buy, exp_ok, horizon=horizon, **kwargs)
        out["holding"] = bool(holding)
        out["pending"] = None if holding else out.get("pending")
        if holding:
            out.update(bars_held=bars_held, entry_price=1450.0,
                       entry_date="2026-11-02", stop_price=1400.0,
                       target_price=1600.0)
        else:
            out.update(bars_held=0, entry_price=None, entry_date=None,
                       stop_price=None, target_price=None)
        return out

    monkeypatch.setattr(cli.pol_mod, "run_policy", _spy)
    if wording is not None:
        monkeypatch.setattr(cli.rnd_mod, "build_reasons", wording)
    return cli.advise_internal(code, fast=True, no_regime=True)


def _main_row(text: str) -> str:
    return next(line for line in text.splitlines()
                if line.startswith(_MAIN_ROW_PREFIXES))


# ---------------------------------------------------------------------------
# 1) 字段来源：holding_days == 决策层 bars_held（多样本、两个标的）
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("code", ["600519", "600000"])
@pytest.mark.parametrize("bars_held", [1, 2, 7, 20, 21, 46])
def test_holding_days_matches_policy_bars_held(monkeypatch, code, bars_held):
    doc = _advise(monkeypatch, code=code, holding=True, bars_held=bars_held)
    rec = doc["recommendation"]
    assert rec["holding_days"] == bars_held
    # 渲染出的主行直接使用该值，不再经过文本。
    assert f"第 {bars_held} 个交易日" in _main_row(render_advice(doc))


def test_flat_position_reports_zero_holding_days(monkeypatch):
    """空仓时决策层 bars_held=0，字段如实透传；主行不出现持有天数。"""
    doc = _advise(monkeypatch, holding=False, bars_held=0)
    assert doc["recommendation"]["holding_days"] == 0
    row = _main_row(render_advice(doc))
    assert row.startswith("暂不买")
    assert "个交易日" not in row


# ---------------------------------------------------------------------------
# 2) 跨模块：build_reasons 文案整体改写，持有天数不变
# ---------------------------------------------------------------------------
def test_wording_change_does_not_change_holding_days(monkeypatch):
    """文案改成完全不含「第 N/M 个交易日」后，渲染结果与改前逐字一致。"""
    baseline_doc = _advise(monkeypatch, holding=True, bars_held=21)
    baseline = _main_row(render_advice(baseline_doc))
    assert "第 21 个交易日" in baseline

    def _rewritten(*args, **kwargs):
        return ["持仓说明已改版：本行不再包含任何交易日计数",
                "止损 1400.0、止盈 1600.0"]

    changed_doc = _advise(monkeypatch, holding=True, bars_held=21,
                          wording=_rewritten)
    assert changed_doc["recommendation"]["action_reason"] == [
        "持仓说明已改版：本行不再包含任何交易日计数",
        "止损 1400.0、止盈 1600.0",
    ]
    assert changed_doc["recommendation"]["holding_days"] == 21
    assert _main_row(render_advice(changed_doc)) == baseline


def test_renderer_ignores_conflicting_text_and_signal_age():
    """即使文本与 last_buy_age 写了别的数字，渲染也只认 holding_days。"""
    payload = {
        "instrument": {"code": "600519", "name": "示例酒业"},
        "recommendation": {
            "action": "hold",
            "confidence": 0.6,
            "action_reason": ["持有中（第 99/20 个交易日，入场 2026-01-01 @ 1450）"],
            "signal": {"position_state": "holding", "last_buy_age": 99},
            "horizon_days": [20, 20],
            "holding_days": 7,
            "stop_loss": 1430.0,
            "take_profit": 1600.0,
        },
    }
    row = _main_row(render_advice(payload))
    assert "第 7 个交易日" in row
    assert "99" not in row


# ---------------------------------------------------------------------------
# 3) 缺失即省略：null 时不解析文本、不退回 last_buy_age
# ---------------------------------------------------------------------------
def test_null_holding_days_omits_segment_without_fallback(monkeypatch):
    doc = _advise(monkeypatch, holding=True, bars_held=7)
    rec = doc["recommendation"]
    rec["holding_days"] = None
    # 文本与 last_buy_age 都还写着 7：渲染层仍不得读取它们。
    rec["action_reason"] = ["持有中（第 7/20 个交易日，入场 2026-11-02 @ 1450）"]
    rec["signal"]["last_buy_age"] = 7

    row = _main_row(render_advice(doc))
    assert row.startswith("继续持有")
    assert "个交易日" not in row
    assert "第 7" not in row


def test_holding_days_key_is_additive_in_renderer_contract():
    """字段缺失（旧 payload）时与 null 等价：省略该段，不抛异常。"""
    payload = {
        "instrument": {"code": "600000", "name": "示例银行"},
        "recommendation": {
            "action": "hold",
            "confidence": 0.6,
            "action_reason": ["持有中（第 7/20 个交易日，入场 2026-11-02 @ 1450）"],
            "signal": {"position_state": "holding", "last_buy_age": 7},
            "horizon_days": [20, 20],
        },
    }
    row = _main_row(render_advice(payload))
    assert row.startswith("继续持有")
    assert "个交易日" not in row
