# -*- coding: utf-8 -*-
"""R11 T8：腾讯报价的量比 / 换手 / 市值（复用同一次报价，零额外请求）。

公开基准代码：``600519`` / ``600000``。基金路径用一个被 mock 成 ``fund`` 的
公开代码，不引入任何真实持仓代码。

字段取证：下标来自钉死 commit ``f215349`` 的 §1.1
（``vendor/a-stock-data/SKILL.md``「腾讯财经字段索引速查」1161-1193 行）：

========  ==========================  ====================
下标      含义                        单位
========  ==========================  ====================
38        换手率                      **百分数**（0.26 = 0.26%）
49        量比                        无量纲
44        流通市值                    **亿元**
45        总市值                      **亿元**
========  ==========================  ====================

``test_tencent_field_indices_are_bound_to_the_documented_positions`` 用四个
互不相同的哨兵值实测上游解析器，防止凭记忆写下标或把 44/45 写反。
"""
from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

SRC = Path(__file__).resolve().parents[1]

from stock_analysis import advice_render  # noqa: E402
from stock_analysis.advice_render import render_advice  # noqa: E402
from stock_analysis.data_loader import status as trading_status  # noqa: E402
from stock_analysis.data_sources import astock, _astock_generated  # noqa: E402

CODE = "600519"
FUND_CODE = "600000"


# ---------------------------------------------------------------------------
# 一组共享的腾讯报价快照（数值取自 §1.1 的实测样例）
# ---------------------------------------------------------------------------
def _quote(**overrides) -> dict:
    base = {
        "name": "贵州茅台", "price": 1410.27, "last_close": 1402.30,
        "open": 1395.13, "change_amt": 7.97, "change_pct": 0.57,
        "high": 1411.00, "low": 1388.11, "amount_wan": 453681.0,
        "turnover_pct": 0.26, "pe_ttm": 21.46, "amplitude_pct": 1.63,
        "float_mcap_yi": 17629.53, "mcap_yi": 17629.53,
        "pb": 6.50, "limit_up": 1542.53, "limit_down": 1262.07,
        "vol_ratio": 0.91, "pe_static": 20.51, "is_stale": False,
    }
    base.update(overrides)
    return base


def _unavailable_st():
    return trading_status.StListSnapshot(available=False, source="unavailable")


def _status(quote: dict, code: str = CODE) -> trading_status.TradingStatus:
    return trading_status.resolve_trading_status(
        code, "sh_main", quote=quote, st_snapshot=_unavailable_st())


# ---------------------------------------------------------------------------
# 1. 字段下标：实测上游解析器，不凭记忆
# ---------------------------------------------------------------------------
def _fake_tencent_payload(**fields) -> bytes:
    """构造 88 字段的腾讯 GBK 响应；只用哨兵值填四个目标下标。"""
    vals = [""] * 88
    vals[0] = "1"
    vals[1] = "贵州茅台"
    vals[2] = "600519"
    vals[3] = "1410.27"   # 当前价
    vals[4] = "1402.30"   # 昨收
    vals[5] = "1395.13"   # 今开
    vals[37] = "453681"   # 成交额（万）→ 非 0，is_stale=False
    vals[38] = "2.34"     # 换手率 %
    vals[44] = "111.11"   # 流通市值（亿）
    vals[45] = "222.22"   # 总市值（亿）
    vals[49] = "1.56"     # 量比
    for index, value in fields.items():
        vals[int(index)] = value
    return ('v_sh600519="' + "~".join(vals) + '";').encode("gbk")


def test_tencent_field_indices_are_bound_to_the_documented_positions(monkeypatch):
    raw = _fake_tencent_payload()

    class _Resp:
        def read(self):
            return raw

    monkeypatch.setattr(_astock_generated.urllib.request, "urlopen",
                        lambda request, timeout=None: _Resp())
    quote = astock.fetch_quote(CODE)

    # 四个哨兵互不相同：任何下标写错 / 44-45 写反都会在这里红。
    assert quote["turnover_pct"] == pytest.approx(2.34)
    assert quote["vol_ratio"] == pytest.approx(1.56)
    assert quote["float_mcap_yi"] == pytest.approx(111.11)
    assert quote["mcap_yi"] == pytest.approx(222.22)
    assert quote["is_stale"] is False


# ---------------------------------------------------------------------------
# 2. status.py：同一次报价 → TradingStatus
# ---------------------------------------------------------------------------
def test_status_exposes_turnover_volume_and_market_cap():
    out = _status(_quote())
    assert out.quote_stale is False
    assert out.turnover_rate_pct == pytest.approx(0.26)
    assert out.volume_ratio == pytest.approx(0.91)
    assert out.market_cap_yi == pytest.approx(17629.53)
    assert out.float_market_cap_yi == pytest.approx(17629.53)


def test_status_keeps_float_and_total_market_cap_distinct():
    """44=流通、45=总：两者不同时不得互换（中船特气式票）。"""
    out = _status(_quote(float_mcap_yi=356.15, mcap_yi=1300.61))
    assert out.float_market_cap_yi == pytest.approx(356.15)
    assert out.market_cap_yi == pytest.approx(1300.61)


def test_status_nulls_quote_fields_when_stale():
    """报价定格 = 上一交易日冻结快照；量比/换手不得当成今日。"""
    out = _status(_quote(amount_wan=0.0, price=1402.30, is_stale=True,
                         turnover_pct=0.0, vol_ratio=0.0))
    assert out.quote_stale is True
    assert out.turnover_rate_pct is None
    assert out.volume_ratio is None
    assert out.market_cap_yi is None
    assert out.float_market_cap_yi is None


def test_status_nulls_zero_and_missing_fields():
    out = _status(_quote(turnover_pct=0.0, vol_ratio=None, mcap_yi=0))
    assert out.turnover_rate_pct is None
    assert out.volume_ratio is None
    assert out.market_cap_yi is None
    # 缺失不是 0：显式 None 才能让渲染层整句消失。
    assert out.float_market_cap_yi == pytest.approx(17629.53)


def test_status_without_quote_has_no_quote_fields():
    out = trading_status.resolve_trading_status(
        CODE, "sh_main", quote_error="AstockError: offline",
        st_snapshot=_unavailable_st())
    assert out.turnover_rate_pct is None
    assert out.volume_ratio is None
    assert out.market_cap_yi is None
    assert out.float_market_cap_yi is None


# ---------------------------------------------------------------------------
# 3. 渲染层：一句话，上一交易日口径；不满足条件整句消失
# ---------------------------------------------------------------------------
def _payload(**instrument) -> dict:
    inst = {
        "code": CODE, "name": "贵州茅台", "board": "sh_main",
        "is_st": False, "is_st_source": "exchange_list",
        "is_suspended": False, "quote_stale": False, "limit_up": 1542.53,
        "limit_down": 1262.07,
        "status_source": "eastmoney_st_list+tencent_quote",
        "turnover_rate_pct": 0.26, "volume_ratio": 0.91,
        "market_cap_yi": 17629.53, "float_market_cap_yi": 17629.53,
    }
    inst.update(instrument)
    return {
        "schema_version": "1.0",
        "request": {"code": CODE, "type": "stock"},
        "instrument": inst,
        "data": {"bars": 150, "end": "2026-11-13",
                 "expected_date": "2026-11-13", "last_close": 1410.27,
                 "stale": False, "lag_days": 0},
        "recommendation": {"status": "no_edge", "action": "watch"},
    }


def _advice_lines(payload) -> list[str]:
    return [line for line in render_advice(payload).splitlines()
            if line and not line.startswith("⚠️ 数据:")]


def test_render_turnover_line_marks_previous_session():
    lines = _advice_lines(_payload())
    line = next(line for line in lines if line.startswith("换手 "))
    assert line == "换手 0.26%｜量比 0.91（上一交易日口径）"
    # 百分号口径不得被二次放大（0.26 不是 26%）。
    assert "换手 26%" not in line
    # 量比是无量纲倍数，不得写成百分比。
    assert "量比 91%" not in line


@pytest.mark.parametrize("missing", ["turnover_rate_pct", "volume_ratio"])
def test_render_turnover_line_disappears_when_a_field_is_missing(missing):
    payload = _payload()
    payload["instrument"][missing] = None
    output = render_advice(payload)
    assert "换手 " not in output
    assert "量比 " not in output
    assert "None" not in output


@pytest.mark.parametrize("zeroed", [
    {"turnover_rate_pct": 0.0},
    {"volume_ratio": 0.0},
    {"turnover_rate_pct": 0.0, "volume_ratio": 0.0},
])
def test_render_turnover_line_never_prints_zero(zeroed):
    output = render_advice(_payload(**zeroed))
    assert "换手 " not in output
    assert "量比 " not in output
    assert "0%" not in output


def test_render_turnover_line_disappears_when_quote_is_stale():
    output = render_advice(_payload(quote_stale=True))
    assert "换手 " not in output
    assert "量比 " not in output
    assert "上一交易日口径" not in output


def test_render_turnover_line_disappears_for_fund():
    payload = _payload(board="fund", status_source="fund_static",
                       turnover_rate_pct=None, volume_ratio=None)
    payload["request"]["type"] = "fund"
    output = render_advice(payload)
    assert "换手 " not in output
    assert "量比 " not in output
    assert "上一交易日口径" not in output


def test_render_turnover_line_unit_boundaries():
    """换手是百分数、量比是无量纲；极小值保留有效位，大值不截断成整数。"""
    lines = _advice_lines(_payload(turnover_rate_pct=0.005, volume_ratio=12.5))
    line = next(line for line in lines if line.startswith("换手 "))
    assert line == "换手 0.005%｜量比 12.5（上一交易日口径）"

    lines = _advice_lines(_payload(turnover_rate_pct=100.0, volume_ratio=1000.5))
    line = next(line for line in lines if line.startswith("换手 "))
    assert line == "换手 100%｜量比 1000.5（上一交易日口径）"


# ---------------------------------------------------------------------------
# 4. CLI 接线：additive 字段 + 基金 null + 请求次数不增加
# ---------------------------------------------------------------------------
def _run_cli(monkeypatch, *, trading, code=CODE, data_type="stock"):
    """复用 test_trading_status 的离线打法；``trading`` 为 None 时走真实
    ``resolve_trading_status``（用于断言请求次数）。"""
    import cli
    import stock_analysis.data_loader.meta as meta
    from stock_analysis.backtest.engine import BacktestResult

    close = pd.Series([100.0 + i * 0.1 for i in range(150)],
                      index=pd.date_range("2020-01-01", periods=150, freq="B"))
    df = pd.DataFrame({
        "open": close * 0.999, "high": close * 1.02,
        "low": close * 0.98, "close": close, "volume": 1e6,
    })
    monkeypatch.setattr(meta, "resolve_and_fetch",
                        lambda *a, **k: (df, data_type, None))
    monkeypatch.setattr(cli, "last_fetch_info",
                        lambda: {"source": "cache", "from_cache": True,
                                 "degraded": None, "lag_days": None})
    monkeypatch.setattr(cli, "get_stock_name", lambda c: "示例")
    if trading is not None:
        monkeypatch.setattr(cli, "resolve_trading_status", lambda *a, **k: trading)
    if data_type != "stock":
        monkeypatch.setattr(cli, "get_st_list_cached", _unavailable_st)

    names = list(cli.build_strategy_signals().keys())[:3]
    comp = pd.DataFrame(
        [{"评分": 1.0 - i * 0.1, "评分IS": 1.0 - i * 0.1,
          "评分OOS": 0.9 - i * 0.1, "年化收益": "10.00%",
          "最大回撤": "5.00%", "胜率": "60.00%", "交易次数": 20}
         for i, _ in enumerate(names)],
        index=names,
    )
    res = BacktestResult(trade_count=20, win_rate=0.6, annual_return=0.1,
                         max_drawdown=0.05, unaffordable_lot_events=0)
    res.equity_curve = [100000.0] * 50
    monkeypatch.setattr(cli, "run_strategy_tournament",
                        lambda *a, **k: (names[0], res, comp))
    return cli.advise_internal(code, fast=True, no_regime=True)


def test_cli_instrument_additive_quote_fields(monkeypatch):
    trading = trading_status.TradingStatus(
        is_st=False, is_suspended=False, quote_stale=False,
        status_source="tencent_quote",
        turnover_rate_pct=0.26, volume_ratio=0.91,
        market_cap_yi=17629.53, float_market_cap_yi=17629.53)
    out = _run_cli(monkeypatch, trading=trading)
    inst = out["instrument"]
    assert inst["turnover_rate_pct"] == pytest.approx(0.26)
    assert inst["volume_ratio"] == pytest.approx(0.91)
    assert inst["market_cap_yi"] == pytest.approx(17629.53)
    assert inst["float_market_cap_yi"] == pytest.approx(17629.53)


def test_cli_fund_quote_fields_are_explicit_null(monkeypatch):
    out = _run_cli(monkeypatch, trading=None, code=FUND_CODE, data_type="fund")
    inst = out["instrument"]
    assert out["request"]["type"] == "fund"
    assert inst["turnover_rate_pct"] is None
    assert inst["volume_ratio"] is None
    assert inst["market_cap_yi"] is None
    assert inst["float_market_cap_yi"] is None
    assert "换手 " not in render_advice(out)


def test_cli_quote_request_count_does_not_increase(monkeypatch):
    """T8 只消费 P0-3 已有的 ``fetch_quote``：整轮个股建议恰好 1 次。"""
    monkeypatch.delenv("ASTOCK_STATUS_OFFLINE", raising=False)
    monkeypatch.delenv("ASTOCK_DISABLE_NETWORK", raising=False)
    calls = []

    def fake_fetch_quote(symbol):
        calls.append(symbol)
        return _quote()

    monkeypatch.setattr(astock, "fetch_quote", fake_fetch_quote)
    out = _run_cli(monkeypatch, trading=None)

    assert calls == [CODE], f"腾讯报价应只请求 1 次，实际 {calls}"
    inst = out["instrument"]
    assert inst["turnover_rate_pct"] == pytest.approx(0.26)
    assert inst["volume_ratio"] == pytest.approx(0.91)
    assert inst["market_cap_yi"] == pytest.approx(17629.53)
    assert "换手 0.26%｜量比 0.91（上一交易日口径）" in render_advice(out)