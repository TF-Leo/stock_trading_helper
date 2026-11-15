# -*- coding: utf-8 -*-
"""issue #117：非交易日不得被判为停牌/不可交易。

覆盖：
* 官方交易日历判定「今天不是交易日」→ ``status=market_closed``、退出码 50；
* 休市日 ``is_suspended`` 必须保持 ``None``（定格报价是常态，不是停牌）；
* 交易日**真实停牌**行为不变（``untradeable`` / 退出码 40）；
* 交易日**盘前**（09:15/09:20 前）定格报价行为不变（不判停牌）；
* 日历不可用 → 显式「无法判定」，不得回退「工作日=交易日」；
* 渲染层出现「今日休市」类说明，且不出现停牌措辞。

测试标的只用 600519 / 600000。
"""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import numpy as np
import pandas as pd

SRC = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).parent / "fixtures" / "astock" / "status"


def _quote(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _snapshot():
    from stock_analysis.data_loader import status

    return status.StListSnapshot(available=False, source="unavailable")


def _resolve(quote, now, trading_day, code="600519", **kwargs):
    from stock_analysis.data_loader import status

    return status.resolve_trading_status(
        code, "sh_main", name="示例", now=now, st_snapshot=_snapshot(),
        quote=quote, trading_day=trading_day, **kwargs)


# 2026-10-01 是周四（法定节假日，工作日但不是交易日）。
HOLIDAY = dt.datetime(2026, 10, 1, 10, 30)
# 2026-11-12 是周四（交易日）。
TRADING = dt.datetime(2026, 11, 12, 10, 30)
PREOPEN = dt.datetime(2026, 11, 12, 9, 20)


# ---------------------------------------------------------------------------
# 1) 官方日历三态
# ---------------------------------------------------------------------------
def test_market_day_state_reads_official_calendar(monkeypatch):
    from stock_analysis.data_loader import status

    monkeypatch.setattr(status.astock, "is_trading_day", lambda day: False)
    state = status.market_day_state(dt.date(2026, 10, 1))
    assert state.closed is True
    assert state.source == status.MARKET_STATE_CALENDAR
    assert state.warning is None


def test_market_day_state_calendar_unavailable_is_unknown(monkeypatch):
    from stock_analysis.data_loader import status

    monkeypatch.setattr(status.astock, "is_trading_day", lambda day: None)
    state = status.market_day_state(dt.date(2026, 10, 1))
    assert state.closed is None, "日历不可用不得推断成交易日"
    assert state.source == status.MARKET_STATE_UNAVAILABLE
    assert "无法判定" in (state.warning or "")


# ---------------------------------------------------------------------------
# 2) 休市日：不判停牌、不产生不可交易
# ---------------------------------------------------------------------------
def test_holiday_weekday_is_market_closed_not_suspended():
    out = _resolve(_quote("quote_suspended.json"), HOLIDAY, trading_day=False)
    assert out.market_closed is True
    assert out.is_suspended is None, "休市日定格报价是常态，不得判停牌"
    assert out.delisted_suspect is False
    assert any("今日休市" in w for w in out.warnings)
    assert out.execution_block("entry", True, False) is None


def test_holiday_quote_unavailable_does_not_escalate_to_delisted():
    out = _resolve(None, HOLIDAY, trading_day=False, quote_error="offline",
                   data_lag_days=30)
    assert out.market_closed is True
    assert out.is_suspended is None
    assert out.delisted_suspect is False


def test_holiday_fresh_quote_keeps_is_suspended_null():
    """P2-1 实测回归：休市日报价**未定格**时旧实现会给 ``is_suspended=false``。

    `false` 的语义是「已确认未停牌 / 可交易」，休市日根本没有撮合，只能是
    ``null``（issue #117 要求）。这里用 is_stale=false 的报价锁定该行为。
    """
    out = _resolve(_quote("quote_normal.json"), HOLIDAY, trading_day=False)
    assert out.market_closed is True
    assert out.quote_stale is False
    assert out.is_suspended is None, "休市日即使报价未定格也必须保持 null"


def test_trading_day_fresh_quote_is_suspended_false():
    """回归：交易日正常报价仍应明确 false（不能把休市修复扩大成永久 null）。"""
    out = _resolve(_quote("quote_normal.json"), TRADING, trading_day=True)
    assert out.market_closed is False
    assert out.is_suspended is False


# ---------------------------------------------------------------------------
# 3) 回归：交易日真实停牌 / 盘前定格，两条行为都不能变
# ---------------------------------------------------------------------------
def test_trading_day_real_suspension_still_untradeable():
    out = _resolve(_quote("quote_suspended.json"), TRADING, trading_day=True)
    assert out.market_closed is False
    assert out.is_suspended is True
    assert any("判定停牌" in w for w in out.warnings)
    reason = out.execution_block("entry", True, False)
    assert reason and "停牌" in reason


def test_trading_day_preopen_stale_is_not_suspension():
    out = _resolve(_quote("quote_suspended.json"), PREOPEN, trading_day=True)
    assert out.market_closed is False
    assert out.is_suspended is None, "盘前定格是正常现象，不得判停牌"
    assert not any("判定停牌" in w for w in out.warnings)
    assert out.execution_block("entry", True, False) is None


# ---------------------------------------------------------------------------
# 4) 日历不可用：显式「无法判定」，不把工作日当交易日
# ---------------------------------------------------------------------------
def test_calendar_unavailable_never_assumes_trading_day():
    out = _resolve(_quote("quote_suspended.json"), TRADING, trading_day=None)
    assert out.market_closed is None
    assert out.is_suspended is None, "无法判定日历不得据此判停牌"
    assert any("交易日历不可用" in w for w in out.warnings)
    assert out.execution_block("entry", True, False) is None


# ---------------------------------------------------------------------------
# 5) CLI：status / action / 退出码 / --json additive
# ---------------------------------------------------------------------------
def _toy(n=300, seed=7, drift=0.001, vol=0.02, start=100.0):
    rng = np.random.default_rng(seed)
    close = start * np.cumprod(1 + rng.normal(drift, vol, n))
    idx = pd.date_range("2020-01-01", periods=n, freq="B")
    return pd.DataFrame({
        "open": close * 0.999, "high": close * (1 + vol),
        "low": close * (1 - vol), "close": close, "volume": 1e6,
    }, index=idx)


def _run_cli(monkeypatch, trading, code="600519"):
    import cli
    import stock_analysis.data_loader.meta as meta
    from stock_analysis.backtest.engine import BacktestResult

    df = _toy()
    monkeypatch.setattr(meta, "resolve_and_fetch", lambda *a, **k: (df, "stock", None))
    monkeypatch.setattr(cli, "last_fetch_info",
                        lambda: {"source": "cache", "from_cache": True,
                                 "degraded": None, "lag_days": None})
    monkeypatch.setattr(cli, "get_stock_name", lambda c: "示例")
    monkeypatch.setattr(cli, "resolve_trading_status", lambda *a, **k: trading)

    names = list(cli.build_strategy_signals().keys())[:3]
    comp = pd.DataFrame(
        [{"评分": 1.0 - i * 0.1, "评分IS": 1.0 - i * 0.1, "评分OOS": 0.9 - i * 0.1,
          "年化收益": "10.00%", "最大回撤": "5.00%", "胜率": "60.00%", "交易次数": 20}
         for i, _ in enumerate(names)],
        index=names,
    )
    res = BacktestResult(trade_count=20, win_rate=0.6, annual_return=0.1,
                         max_drawdown=0.05, unaffordable_lot_events=0)
    res.equity_curve = [100000.0] * 50
    monkeypatch.setattr(cli, "run_strategy_tournament",
                        lambda *a, **k: (names[0], res, comp))
    return cli.advise_internal(code, fast=True, no_regime=True)


def _run_cli_fund(monkeypatch, code="510300"):
    """基金路径（P2-2）：data_type=fund 时也必须过休市闸门。"""
    import cli
    import stock_analysis.data_loader.meta as meta
    from stock_analysis.backtest.engine import BacktestResult

    df = _toy()
    monkeypatch.setattr(meta, "resolve_and_fetch", lambda *a, **k: (df, "fund", None))
    monkeypatch.setattr(cli, "last_fetch_info",
                        lambda: {"source": "cache", "from_cache": True,
                                 "degraded": None, "lag_days": None})

    def _no_status(*a, **k):
        raise AssertionError("基金不查询个股交易状态（但休市判定必须走日历）")

    monkeypatch.setattr(cli, "resolve_trading_status", _no_status)

    names = list(cli.build_fund_strategy_signals().keys())[:3]
    comp = pd.DataFrame(
        [{"评分": 1.0 - i * 0.1, "评分IS": 1.0 - i * 0.1, "评分OOS": 0.9 - i * 0.1,
          "年化收益": "10.00%", "最大回撤": "5.00%", "胜率": "60.00%", "交易次数": 20}
         for i, _ in enumerate(names)],
        index=names,
    )
    res = BacktestResult(trade_count=20, win_rate=0.6, annual_return=0.1,
                         max_drawdown=0.05, unaffordable_lot_events=0)
    res.equity_curve = [100000.0] * 50
    monkeypatch.setattr(cli, "run_strategy_tournament",
                        lambda *a, **k: (names[0], res, comp))
    return cli.advise_internal(code, fast=True, no_regime=True)


def test_fund_holiday_is_market_closed_exit_50(monkeypatch):
    """P2-2 实测回归：周日/法定节假日场外基金旧实现 exit 0 / status=ok。"""
    import market_state
    from stock_analysis.advice_render import render_advice
    from stock_analysis.data_loader import status as st

    monkeypatch.setattr(st.astock, "is_trading_day", lambda day: False)
    out = _run_cli_fund(monkeypatch)

    assert out["request"]["type"] == "fund"
    inst = out["instrument"]
    assert inst["market_closed"] is True
    assert inst["market_state_source"] == "astock_calendar"
    assert inst["is_suspended"] is None
    assert out["recommendation"]["status"] == "market_closed"
    assert out["recommendation"]["action"] == "avoid"
    assert market_state.exit_code_for_status(out["recommendation"]["status"]) == 50
    text = render_advice(out)
    assert "【今日不建议执行】今日休市" in text


def test_fund_trading_day_not_market_closed(monkeypatch):
    """回归：交易日基金不得被误改成 market_closed。"""
    from stock_analysis.data_loader import status as st

    monkeypatch.setattr(st.astock, "is_trading_day", lambda day: True)
    out = _run_cli_fund(monkeypatch)
    assert out["instrument"]["market_closed"] is False
    assert out["recommendation"]["status"] != "market_closed"


def test_cli_market_closed_exit_code_50(monkeypatch):
    import market_state
    from stock_analysis.data_loader.status import TradingStatus

    trading = TradingStatus(
        is_suspended=None, quote_stale=True, market_closed=True,
        market_state_source="astock_calendar",
        status_source="tencent_quote", warnings=[])
    out = _run_cli(monkeypatch, trading)

    assert out["instrument"]["market_closed"] is True
    assert out["instrument"]["is_suspended"] is None
    assert out["instrument"]["market_state_source"] == "astock_calendar"
    assert out["recommendation"]["status"] == "market_closed"
    assert out["recommendation"]["action"] == "avoid"
    assert any("今日休市" in r for r in out["recommendation"]["action_reason"])
    assert not any("结果不可信" in r for r in out["recommendation"]["action_reason"])
    assert market_state.exit_code_for_status(out["recommendation"]["status"]) == 50
    assert any("今日休市" in w for w in out["diagnostics"]["warnings"])


def test_cli_real_suspension_exit_code_40_unchanged(monkeypatch):
    import market_state
    from stock_analysis.data_loader.status import TradingStatus

    trading = TradingStatus(
        is_suspended=True, quote_stale=True, market_closed=False,
        market_state_source="astock_calendar",
        status_source="tencent_quote", warnings=[])
    out = _run_cli(monkeypatch, trading)

    assert out["recommendation"]["status"] == "untradeable"
    assert out["recommendation"]["action"] == "avoid"
    assert market_state.exit_code_for_status(out["recommendation"]["status"]) == 40
    assert out["instrument"]["market_closed"] is False


def test_exit_code_mapping_keeps_decision_contract():
    import market_state
    from decision.schema import EXIT_CODES, exit_code_for

    for status, code in EXIT_CODES.items():
        assert market_state.exit_code_for_status(status) == code
    assert market_state.exit_code_for_status("market_closed") == 50
    assert market_state.exit_code_for_status("unknown_status") == 30
    assert 50 not in EXIT_CODES.values(), "decision/* 未被本次改动触碰"


# ---------------------------------------------------------------------------
# 5b) 闸门层与决策层分开（P2-3）
# ---------------------------------------------------------------------------
def test_gate_layer_overrides_without_touching_decision():
    """``decide()`` 不认识 market_closed；覆盖只发生在 ``apply_market_state_gate``。"""
    import market_state
    from decision.actions import decide

    assert decide("market_closed", "entry", True, False) == "buy", \
        "decision 层不得被休市改动（单一真源）"

    payload = {
        "instrument": {"market_closed": True, "market_state_source": "astock_calendar"},
        "recommendation": {"status": "ok", "action": "buy", "target_position": 1.0,
                           "position_fraction": 0.3, "no_edge": False,
                           "action_reason": ["买入"]},
        "diagnostics": {"warnings": []},
    }
    out = market_state.apply_market_state_gate(payload)
    rec = out["recommendation"]
    assert rec["status"] == "market_closed"
    assert rec["action"] == "avoid"
    assert rec["target_position"] == 0.0
    assert rec["position_fraction"] == 0.0
    assert rec["no_edge"] is False
    assert any("今日休市" in r for r in rec["action_reason"])


def test_gate_layer_keeps_data_error_priority():
    import market_state

    payload = {
        "instrument": {"market_closed": True, "market_state_source": "astock_calendar"},
        "recommendation": {"status": "data_source_error", "action": "insufficient_data"},
        "diagnostics": {"warnings": []},
    }
    out = market_state.apply_market_state_gate(payload)
    assert out["recommendation"]["status"] == "data_source_error"


def test_cli_line_budget_not_increased_by_issue_117():
    """P2-3：cli.py 行数不得高于本 PR 之前的 714 行（issue #117 只许调用闸门）。"""
    cli_path = SRC / "cli.py"
    lines = cli_path.read_text(encoding="utf-8").count("\n")
    assert lines <= 714, f"cli.py 涨到 {lines} 行（基线 714）"


# ---------------------------------------------------------------------------
# 6) 渲染层：今日休市
# ---------------------------------------------------------------------------
def test_render_market_closed_wording():
    from stock_analysis.advice_render import render_advice

    payload = {
        "instrument": {
            "code": "600519", "name": "示例", "board": "sh_main",
            "market_closed": True, "market_state_source": "astock_calendar",
            "is_suspended": None, "quote_stale": True,
            "status_source": "tencent_quote", "is_st": False,
        },
        "recommendation": {
            "status": "market_closed", "action": "avoid", "confidence": 0.5,
            "position_fraction": 0.0, "action_reason": ["今日休市"],
            "entry_zone": None, "stop_loss": None, "take_profit": None,
        },
        "data": {"last_close": 10.0, "bars": 300, "end": "2026-09-30"},
        "diagnostics": {"warnings": []},
    }
    text = render_advice(payload)
    assert "今日休市" in text
    assert "【今日不建议执行】今日休市" in text
    assert "盘前无法判断是否停牌" not in text
    assert "停牌（确认无当日成交）" not in text


def test_render_calendar_unavailable_wording():
    from stock_analysis.advice_render import render_advice

    payload = {
        "instrument": {
            "code": "600519", "name": "示例", "board": "sh_main",
            "market_closed": None, "market_state_source": "calendar_unavailable",
            "is_suspended": None, "quote_stale": True,
            "status_source": "tencent_quote", "is_st": False,
        },
        "recommendation": {
            "status": "ok", "action": "hold", "confidence": 0.5,
            "position_fraction": 0.0, "action_reason": ["观望"],
            "entry_zone": None, "stop_loss": None, "take_profit": None,
        },
        "data": {"last_close": 10.0, "bars": 300, "end": "2026-11-12"},
        "diagnostics": {"warnings": []},
    }
    text = render_advice(payload)
    assert "交易日历不可用" in text
    assert "无法判定今日是否休市" in text
    assert "停牌（确认无当日成交）" not in text


# ---------------------------------------------------------------------------
# 7) 文档：README 必须写清新退出码
# ---------------------------------------------------------------------------
def test_readme_documents_market_closed_exit_code():
    readme = (SRC.parent / "README.md").read_text(encoding="utf-8")
    assert "market_closed" in readme
    assert "| 50 | market_closed" in readme