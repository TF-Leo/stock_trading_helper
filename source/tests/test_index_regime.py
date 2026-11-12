# -*- coding: utf-8 -*-
"""P1-3（issue #32）：指数 regime —— 只展示、不改 action。

覆盖：
  * 腾讯指数日线取数（离线 fixture / 空数据 / HTTP 失败 / 分页）；
  * 缓存命中与过期增量（不触网）；
  * ``get_index_regime`` 与个股 regime **共用同一判定规则**（不复制第二份）；
  * CLI：原有 ``market_regime`` 四字段逐字不变，新增 ``index`` 为 additive；
  * 指数源失败时 ``index=null`` + warning，``action`` / ``status`` 不受影响。

所有网络入口在用例内打桩；fixture 为 2026-11-12 从真实端点录制。
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pandas as pd
import pytest

SRC = Path(__file__).resolve().parents[1]
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

FIXTURES = Path(__file__).parent / "fixtures" / "astock"


class FakeResponse:
    def __init__(self, text: str, status: int = 200):
        self.text = text
        self.status_code = status
        self.url = "https://example.invalid/index"

    def json(self):
        return json.loads(self.text)

    def raise_for_status(self):
        if self.status_code >= 400:
            import requests
            raise requests.HTTPError(f"{self.status_code}", response=self)


def _index_payload() -> dict:
    return json.loads((FIXTURES / "index_kline_sh000300.json").read_text(encoding="utf-8"))


def _patch_index_http(monkeypatch, payload=None):
    """把 ``_v39_http`` 换成按 ``end`` 参数过滤的离线回放，并记录每次调用。"""
    data = payload if payload is not None else _index_payload()
    calls = []

    def fake_http(url, params=None, data_=None, headers=None, method="GET",
                  timeout=None, **kwargs):
        calls.append({"url": url, "param": (params or {}).get("param", "")})
        body = json.loads(json.dumps(data))
        parts = (params or {}).get("param", "").split(",")
        if len(parts) >= 4 and parts[3]:
            end = parts[3]
            node = (body.get("data") or {}).get(parts[0]) or {}
            if isinstance(node.get("day"), list):
                node["day"] = [r for r in node["day"] if r[0] <= end]
        return FakeResponse(json.dumps(body))

    from stock_analysis.data_sources import _astock_generated
    monkeypatch.setattr(_astock_generated, "_v39_http", fake_http)
    return calls


# ---------------------------------------------------------------------------
# 取数
# ---------------------------------------------------------------------------
def test_fetch_index_daily_offline_fixture(monkeypatch):
    from stock_analysis.data_sources import astock

    calls = _patch_index_http(monkeypatch)
    df = astock.fetch_index_daily("sh000300", start_date="20260301")

    assert list(df.columns) == ["open", "high", "low", "close", "volume"]
    assert isinstance(df.index, pd.DatetimeIndex)
    assert df.index.name == "date"
    assert df.index.is_monotonic_increasing
    assert not df.index.duplicated().any()
    assert len(df) == 173, f"2026-03-02..2026-11-12 应有 173 根，实际 {len(df)}"
    # 与 docs/P1_EVIDENCE_PLAN.md §2.1 C3 实测一致
    assert df.index[0].date().isoformat() == "2026-03-02"
    assert df.index[-1].date().isoformat() == "2026-11-12"
    assert df.loc["2026-11-12", "close"] == pytest.approx(4043.93)
    # 指数优先走 newfqkline（fqkline 在本机被 WAF 501）
    assert calls and "/newfqkline/get" in calls[0]["url"]


def test_fetch_index_daily_prefers_newfqkline(monkeypatch):
    """首个入口必须是 newfqkline；fqkline 只作兜底。"""
    from stock_analysis.data_sources import astock

    calls = _patch_index_http(monkeypatch)
    astock.fetch_index_daily("sh000300", start_date="20260301")
    assert astock._INDEX_KLINE_PATHS[0] == "/appstock/app/newfqkline/get"
    assert "/newfqkline/get" in calls[0]["url"]


def test_fetch_index_daily_empty_data_raises(monkeypatch):
    from stock_analysis.data_sources import astock

    empty = {"code": 0, "msg": "", "data": {"sh000300": {"day": [], "qt": {}}}}
    _patch_index_http(monkeypatch, empty)
    with pytest.raises(astock.AstockError, match="0 根"):
        astock.fetch_index_daily("sh000300", start_date="20260301")


def test_fetch_index_daily_http_failure_raises(monkeypatch):
    from stock_analysis.data_sources import astock, _astock_generated

    def boom(*a, **k):
        raise RuntimeError("请求失败: HTTPError: 501 WAF")

    monkeypatch.setattr(_astock_generated, "_v39_http", boom)
    with pytest.raises(astock.AstockError, match="入口均不可用"):
        astock.fetch_index_daily("sh000300", start_date="20260301")


def test_fetch_index_daily_offline_switch(monkeypatch):
    from stock_analysis.data_sources import astock

    monkeypatch.setenv(astock.INDEX_OFFLINE_ENV, "1")
    with pytest.raises(astock.AstockError, match="已禁用"):
        astock.fetch_index_daily("sh000300", start_date="20260301")


@pytest.mark.parametrize("given,want", [
    ("sh000300", "sh000300"),
    ("000300", "sh000300"),      # 裸码：沪市指数号段
    ("000985", "sh000985"),      # 不在上游 SH_INDEX 名单里，也不能误判成 sz
    ("000001.SH", "sh000001"),
    ("399006", "sz399006"),      # 深市指数号段
    ("399006.SZ", "sz399006"),
])
def test_index_symbol_disambiguation(given, want):
    """裸 6 位指数码必须按指数号段消歧，不能落到个股的默认 sz。"""
    from stock_analysis.data_sources import astock

    assert astock._index_symbol(given) == want


def test_fetch_index_daily_pages_backwards(monkeypatch):
    """首屏满 640 根且未覆盖 start → 以「最早日 - 1 天」为 end 继续向前翻页。"""
    from stock_analysis.data_sources import astock

    seen = []
    # 首屏：640 根，最早 2025-01-01（晚于 start 2018-01-01）→ 必须翻页
    page1 = [[d.strftime("%Y-%m-%d"), "1", "1", "1", "1", "1"]
             for d in pd.bdate_range(end="2026-11-12", periods=640)]
    # 第二屏：640 根，最早 2016-01-01（早于 start）→ 覆盖后停止
    page2 = [[d.strftime("%Y-%m-%d"), "1", "1", "1", "1", "1"]
             for d in pd.bdate_range(start="2016-01-01", periods=640)]

    def fake_call(param):
        seen.append(param)
        parts = param.split(",")
        end = parts[3]
        rows = page1 if not end else page2
        return {"sh000300": {"day": rows}}, "https://example.invalid"

    monkeypatch.setattr(astock, "_index_kline_call", fake_call)
    df = astock.fetch_index_daily("sh000300", start_date="20180101")

    assert len(seen) == 2, f"应翻页一次，实际 {seen}"
    assert seen[0].split(",")[3] == "", "首屏应不带 end（从最新往前）"
    # 第二屏 end = 首屏最早日 - 1 天
    expected_end = (pd.Timestamp(page1[0][0]) - pd.Timedelta(days=1)).date().isoformat()
    assert seen[1].split(",")[3] == expected_end
    assert df.index[0].date().isoformat() == "2018-01-01", "start 之后的根才保留"
    assert df.index[-1].date().isoformat() == "2026-11-12"


def test_fetch_index_daily_rejects_out_of_window_rows(monkeypatch):
    """源返回了 end 之后的日期 → 结果不可信，必须报错而不是静默接受。"""
    from stock_analysis.data_sources import astock

    calls = []

    def fake_call(param):
        calls.append(param)
        if len(calls) == 1:
            page = [[d.strftime("%Y-%m-%d"), "1", "1", "1", "1", "1"]
                    for d in pd.bdate_range("2025-01-01", periods=640)]
            return {"sh000300": {"day": page}}, "h"
        # 第二屏：无视 end 限制，返回了 end 之后的日期
        return {"sh000300": {"day": [["2026-11-12", "1", "1", "1", "1", "1"]]}}, "h"

    monkeypatch.setattr(astock, "_index_kline_call", fake_call)
    with pytest.raises(astock.AstockError, match="结果不可信"):
        astock.fetch_index_daily("sh000300", start_date="20180101")


def _seed_index_cache(meta, closes, start="2026-05-01"):
    idx = pd.bdate_range(start, periods=len(closes))
    df = pd.DataFrame(
        {"open": closes, "high": closes, "low": closes,
         "close": closes, "volume": [1e6] * len(closes)},
        index=idx,
    )
    df.to_csv(meta._index_cache_path("sh000300"))
    return df


def _freeze_expected(monkeypatch, meta, day):
    """冻结「期望最新交易日」，让缓存新鲜度可离线判定。"""
    monkeypatch.setattr(meta, "_expected_latest_trade_date",
                        lambda now=None: pd.Timestamp(day).date())


# ---------------------------------------------------------------------------
# 缓存
# ---------------------------------------------------------------------------
def test_index_cache_hit_does_not_touch_network(monkeypatch, tmp_path):
    import stock_analysis.data_loader.meta as meta
    from stock_analysis.data_sources import astock

    monkeypatch.setattr(meta, "DATA_DIR", str(tmp_path))
    closes = [4000.0 + i for i in range(80)]
    seeded = _seed_index_cache(meta, closes)
    _freeze_expected(monkeypatch, meta, seeded.index[-1])

    def boom(*a, **k):
        raise AssertionError("缓存新鲜时不得触网")

    monkeypatch.setattr(astock, "fetch_index_daily", boom)

    df = meta.get_market_index_daily("sh000300", start_date="20260101")
    assert df is not None and len(df) == 80
    assert df.index[-1] == seeded.index[-1]


def test_index_cache_expired_refetches_and_merges(monkeypatch, tmp_path):
    import stock_analysis.data_loader.meta as meta
    from stock_analysis.data_sources import astock

    monkeypatch.setattr(meta, "DATA_DIR", str(tmp_path))
    old = _seed_index_cache(meta, [4000.0] * 70, start="2026-06-01")
    _freeze_expected(monkeypatch, meta, "2026-11-12")

    fresh = pd.DataFrame(
        {"open": [1.0] * 5, "high": [1.0] * 5, "low": [1.0] * 5,
         "close": [4100.0] * 5, "volume": [1e6] * 5},
        index=pd.bdate_range("2026-11-06", periods=5),
    )
    asked = {}

    def fake_fetch(symbol, start_date=None):
        asked["start"] = start_date
        return fresh

    monkeypatch.setattr(astock, "fetch_index_daily", fake_fetch)
    df = meta.get_market_index_daily("sh000300", start_date="20260101")

    assert df is not None
    assert len(df) == 75, "旧缓存 + 近端新窗口应取并集"
    assert df.index[-1].date().isoformat() == "2026-11-12"
    # 过期缓存只拉近端窗口，不是全量
    assert asked["start"] is not None and asked["start"] > "20260101"


def test_index_fetch_failure_falls_back_to_cache(monkeypatch, tmp_path):
    import stock_analysis.data_loader.meta as meta
    from stock_analysis.data_sources import astock

    monkeypatch.setattr(meta, "DATA_DIR", str(tmp_path))
    _seed_index_cache(meta, [4000.0] * 70, start="2026-06-01")
    _freeze_expected(monkeypatch, meta, "2026-11-12")

    def boom(symbol, start_date=None):
        raise astock.AstockError("模拟断网")

    monkeypatch.setattr(astock, "fetch_index_daily", boom)
    df = meta.get_market_index_daily("sh000300", start_date="20260101")
    assert df is not None and len(df) == 70, "取数失败必须回退旧缓存，不返回空"


def test_index_fetch_failure_without_cache_returns_none(monkeypatch, tmp_path):
    import stock_analysis.data_loader.meta as meta
    from stock_analysis.data_sources import astock

    monkeypatch.setattr(meta, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(astock, "fetch_index_daily",
                        lambda *a, **k: (_ for _ in ()).throw(astock.AstockError("断网")))
    assert meta.get_market_index_daily("sh000300", start_date="20260101") is None


# ---------------------------------------------------------------------------
# regime 判定：复用，不复制
# ---------------------------------------------------------------------------
def test_index_regime_reuses_stock_rule(monkeypatch, tmp_path):
    """指数 regime 必须与个股 regime 用**同一个** _regime_from_row。"""
    import stock_analysis.data_loader.meta as meta
    from stock_analysis.advisor import analyzer

    monkeypatch.setattr(meta, "DATA_DIR", str(tmp_path))
    closes = [3000.0 + i * 5 for i in range(120)]
    seeded = _seed_index_cache(meta, closes)
    _freeze_expected(monkeypatch, meta, seeded.index[-1])

    block = meta.get_index_regime("sh000300")
    expected_label, expected_conf = analyzer.get_current_market_regime(seeded)
    assert block is not None
    assert block["label"] == expected_label
    assert block["confidence"] == pytest.approx(round(expected_conf, 3))
    assert block["definition"] == "ma20_ma60_return_volatility"
    assert block["source"] == "tencent_index:sh000300"
    assert block["bars"] == 120


def test_index_regime_rule_is_not_duplicated():
    """源码层面确认没有第二份 regime 判定：只允许调用 analyzer._regime_from_row。"""
    import inspect
    import stock_analysis.data_loader.meta as meta

    src = inspect.getsource(meta.get_index_regime)
    assert "get_current_market_regime" in src
    # 不得自行重算 MA20/MA60/ret20 等阈值（那是复制第二份判定逻辑）
    for forbidden in ("rolling(", "pct_change", "0.35", "ret_20d"):
        assert forbidden not in src, f"get_index_regime 不得复制判定逻辑：{forbidden}"


def test_index_regime_none_when_short(monkeypatch, tmp_path):
    import stock_analysis.data_loader.meta as meta
    from stock_analysis.data_sources import astock

    monkeypatch.setattr(meta, "DATA_DIR", str(tmp_path))
    short = pd.DataFrame(
        {"open": [1.0] * 30, "high": [1.0] * 30, "low": [1.0] * 30,
         "close": [3000.0] * 30, "volume": [1e6] * 30},
        index=pd.bdate_range("2026-10-01", periods=30),
    )
    monkeypatch.setattr(astock, "fetch_index_daily", lambda *a, **k: short)
    assert meta.get_index_regime("sh000300") is None, "不足 60 根必须返回 None，不得编造标签"


# ---------------------------------------------------------------------------
# CLI 接线：additive，且不改 action
# ---------------------------------------------------------------------------
def _run_cli(monkeypatch, index_block, no_regime=False):
    import cli
    import stock_analysis.data_loader as loader
    import stock_analysis.data_loader.meta as meta
    from stock_analysis.backtest.engine import BacktestResult

    df = pd.DataFrame(
        {"open": [1.0] * 300, "high": [1.0] * 300, "low": [1.0] * 300,
         "close": [100.0 + i * 0.1 for i in range(300)], "volume": [1e6] * 300},
        index=pd.bdate_range("2020-01-01", periods=300),
    )
    monkeypatch.setattr(meta, "resolve_and_fetch", lambda *a, **k: (df, "stock", None))
    monkeypatch.setattr(cli, "last_fetch_info",
                        lambda: {"source": "cache", "from_cache": True,
                                 "degraded": None, "lag_days": None})
    monkeypatch.setattr(cli, "get_stock_name", lambda c: c)
    from stock_analysis.data_loader.status import TradingStatus
    monkeypatch.setattr(cli, "resolve_trading_status",
                        lambda *a, **k: TradingStatus(is_st=False, is_suspended=False,
                                                      status_source="offline",
                                                      warnings=[]))
    monkeypatch.setattr(loader, "get_index_regime", lambda *a, **k: index_block)

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
    return cli.advise_internal("600519", fast=True, no_regime=no_regime)


def test_cli_index_regime_is_additive(monkeypatch):
    block = {"label": "Shock", "confidence": 0.5, "as_of": "2026-11-12 00:00:00",
             "definition": "ma20_ma60_return_volatility",
             "source": "tencent_index:sh000300", "bars": 1662}
    out = _run_cli(monkeypatch, block)

    regime = out["market_regime"]
    # 原有四字段逐字不变
    assert set(regime) == {"label", "confidence", "as_of", "definition", "index"}
    assert regime["definition"] == "ma20_ma60_return_volatility"
    assert regime["label"] in ("Bull", "Bear", "Shock")
    # 新增 index 子块
    assert regime["index"] == block
    assert out["schema_version"] == "1.0"


def test_cli_index_regime_failure_is_null_with_warning(monkeypatch):
    out = _run_cli(monkeypatch, None)
    index = out["market_regime"]["index"]
    assert index == {"label": None, "confidence": None, "as_of": None,
                     "definition": None, "source": None, "bars": None}
    assert any("指数 regime 不可用" in w for w in out["diagnostics"]["warnings"])
    # 原 regime 与 action 不受影响
    assert out["market_regime"]["label"] in ("Bull", "Bear", "Shock")
    assert out["recommendation"]["action"] in ("buy", "hold", "sell", "avoid",
                                               "insufficient_data")


def test_cli_no_regime_disables_index_too(monkeypatch):
    block = {"label": "Shock", "confidence": 0.5, "as_of": "x",
             "definition": "ma20_ma60_return_volatility", "source": "s", "bars": 100}
    out = _run_cli(monkeypatch, block, no_regime=True)
    regime = out["market_regime"]
    assert regime["label"] == "Unknown"
    assert regime["definition"] == "disabled"
    assert regime["index"]["definition"] == "disabled"
    assert regime["index"]["label"] is None


def test_error_response_index_block_isomorphic():
    """G35：错误路径必须与成功路径键同构（index 子块不得整块缺失）。"""
    import cli

    doc = cli.create_error_response("data_source_error", "失败", "600519", "stock")
    index = doc["market_regime"]["index"]
    assert set(index) == {"label", "confidence", "as_of", "definition",
                          "source", "bars"}
    assert all(v is None for v in index.values()), "不适用处必须显式 null"
