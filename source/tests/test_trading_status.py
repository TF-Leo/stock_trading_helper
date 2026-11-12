# -*- coding: utf-8 -*-
"""P0-3 真实交易状态离线测试（issue #6）。

所有网络入口都在测试内打桩：腾讯报价用 ``fixtures/astock/status/quote_*.json``，
东财 ST 名单用 ``st_list.json`` / 假 ``em_get``。conftest 默认
``ASTOCK_STATUS_OFFLINE=1``，并给每个用例独立的缓存路径。
"""
from __future__ import annotations

import datetime as dt
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pandas as pd
import pytest

SRC = Path(__file__).resolve().parents[1]
ROOT = SRC.parent
FIXTURES = Path(__file__).parent / "fixtures" / "astock" / "status"


def _fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _quote(name: str) -> dict:
    return _fixture(name)


def _st_frame() -> pd.DataFrame:
    payload = _fixture("st_list.json")
    rows = [{"code": code, **info} for code, info in payload["stocks"].items()]
    frame = pd.DataFrame(rows)
    frame.attrs["coverage"] = payload["coverage"]
    return frame


def _snapshot(available=True, codes=(), warning=None):
    from stock_analysis.data_loader import status

    stocks = {code: {"name": f"{code}ST", "st_type": "ST", "market": "sh"}
              for code in codes}
    return status.StListSnapshot(
        available=available, stocks=stocks, coverage="沪深京" if available else "",
        source="eastmoney" if available else "unavailable", warning=warning)


def _resolve(quote, board="sh_main", now=None, snapshot=None, **kwargs):
    from stock_analysis.data_loader import status

    return status.resolve_trading_status(
        kwargs.pop("code", "600519"), board, name=kwargs.pop("name", "示例"),
        now=now or dt.datetime(2026, 11, 12, 14, 0),
        st_snapshot=snapshot if snapshot is not None else _snapshot(),
        quote=quote, **kwargs)


# ---------------------------------------------------------------------------
# 生成物 / import
# ---------------------------------------------------------------------------
def test_status_module_import_does_not_touch_network_or_deps():
    script = (
        "import socket\n"
        "class _Blocker:\n"
        "    def __init__(self, *a, **k):\n"
        "        raise RuntimeError('network access during import')\n"
        "socket.socket = _Blocker\n"
        "socket.create_connection = lambda *a, **k: (_ for _ in ()).throw(RuntimeError('net'))\n"
        "import stock_analysis.data_loader.status\n"
        "import sys\n"
        "assert 'mootdx' not in sys.modules\n"
        "assert 'baostock' not in sys.modules\n"
        "print('ok')\n"
    )
    env = dict(os.environ, PYTHONPATH=str(SRC))
    result = subprocess.run([sys.executable, "-c", script], cwd=str(SRC), env=env,
                            capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    assert "ok" in result.stdout


def test_extracted_artifact_check_passes():
    result = subprocess.run([sys.executable, "tools/astock_extract.py", "--check"],
                            cwd=str(ROOT), capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stderr
    assert "OK source/stock_analysis/data_sources/_astock_generated.py" in result.stdout


def test_generated_st_list_has_no_baostock_fallback():
    from stock_analysis.data_sources import _astock_generated as _g

    assert hasattr(_g, "st_stock_list")
    assert hasattr(_g, "em_get")
    assert _g.EM_MIN_INTERVAL >= 1.0, "东财必须保持串行限流"
    source = Path(_g.__file__).read_text(encoding="utf-8")
    assert "_st_list_baostock" not in source
    assert "baostock 兜底已禁用" in source
    manifest = json.loads((SRC / "stock_analysis" / "data_sources"
                           / "_astock_manifest.json").read_text(encoding="utf-8"))
    kinds = {item["kind"] for item in manifest["modifications"]}
    assert "baostock_fallback_disabled" in kinds
    assert manifest["upstream"]["commit"] == "f21534946c42be3ad048ac938eecb1bad84cb8ab"


# ---------------------------------------------------------------------------
# ST 名单缓存 / 限流
# ---------------------------------------------------------------------------
def _enable_network(monkeypatch):
    monkeypatch.setenv("ASTOCK_STATUS_OFFLINE", "0")


def test_st_list_cache_hit_avoids_refetch(monkeypatch):
    from stock_analysis.data_loader import status

    _enable_network(monkeypatch)
    calls = {"n": 0}

    def fake_list():
        calls["n"] += 1
        return _st_frame()

    monkeypatch.setattr("stock_analysis.data_sources._astock_generated.st_stock_list",
                        fake_list)
    now = dt.datetime(2026, 11, 12, 14, 0)

    first = status.get_st_list(now=now)
    assert first.available and not first.degraded
    assert set(first.codes) == {"600001", "000002"}
    assert calls["n"] == 1
    assert Path(status.cache_path()).exists()

    status.reset_cache()          # 强制走磁盘缓存
    second = status.get_st_list(now=now)
    assert second.available and calls["n"] == 1, "同一交易日应命中磁盘缓存"
    assert second.stocks["600001"]["name"] == "*ST示例"


def test_st_list_cache_expires_next_trading_day(monkeypatch):
    from stock_analysis.data_loader import status

    _enable_network(monkeypatch)
    calls = {"n": 0}

    def fake_list():
        calls["n"] += 1
        return _st_frame()

    monkeypatch.setattr("stock_analysis.data_sources._astock_generated.st_stock_list",
                        fake_list)
    status.get_st_list(now=dt.datetime(2026, 11, 12, 14, 0))
    status.reset_cache()
    status.get_st_list(now=dt.datetime(2026, 11, 13, 14, 0))
    assert calls["n"] == 2, "下一个交易日应重新拉取"


def test_st_list_failure_falls_back_to_expired_cache(monkeypatch):
    from stock_analysis.data_loader import status

    _enable_network(monkeypatch)
    monkeypatch.setattr("stock_analysis.data_sources._astock_generated.st_stock_list",
                        lambda: _st_frame())
    status.get_st_list(now=dt.datetime(2026, 11, 12, 14, 0))
    status.reset_cache()

    def boom():
        raise RuntimeError("429 Too Many Requests")

    monkeypatch.setattr("stock_analysis.data_sources._astock_generated.st_stock_list",
                        boom)
    snap = status.get_st_list(now=dt.datetime(2026, 11, 13, 14, 0))
    assert snap.available and snap.degraded
    assert snap.warning and "429" in snap.warning and "缓存" in snap.warning
    assert "600001" in snap.codes


def test_st_list_failure_without_cache_degrades(monkeypatch):
    from stock_analysis.data_loader import status

    _enable_network(monkeypatch)
    monkeypatch.setattr("stock_analysis.data_sources._astock_generated.st_stock_list",
                        lambda: (_ for _ in ()).throw(RuntimeError("429 rate limited")))
    snap = status.get_st_list(now=dt.datetime(2026, 11, 12, 14, 0))
    assert not snap.available and not snap.degraded
    assert snap.warning and "ST 名单接口失败" in snap.warning and "429" in snap.warning


def test_st_list_offline_uses_cache_without_network(monkeypatch):
    from stock_analysis.data_loader import status

    _enable_network(monkeypatch)
    monkeypatch.setattr("stock_analysis.data_sources._astock_generated.st_stock_list",
                        lambda: _st_frame())
    status.get_st_list(now=dt.datetime(2026, 11, 12, 14, 0))
    status.reset_cache()

    monkeypatch.setenv("ASTOCK_STATUS_OFFLINE", "1")
    monkeypatch.setattr(
        "stock_analysis.data_sources._astock_generated.st_stock_list",
        lambda: (_ for _ in ()).throw(AssertionError("offline 不得触网")))
    snap = status.get_st_list(now=dt.datetime(2026, 11, 12, 15, 30), force=True)
    assert snap.available and snap.degraded
    assert "600001" in snap.codes


def test_em_get_throttle_and_pagination(monkeypatch):
    """§6.8 走 em_get：两次 clist 调用（沪深/北交所），且串行限流生效。"""
    from stock_analysis.data_sources import _astock_generated as _g

    class FakeResponse:
        url = "https://push2.eastmoney.com/api/qt/clist/get"

        def __init__(self, payload):
            self._payload = payload

        def json(self):
            return self._payload

        def raise_for_status(self):
            return None

    def fake_get(url, params=None, headers=None, timeout=15, **kwargs):
        fs = (params or {}).get("fs", "")
        if fs == "m:0+t:81+s:2048":
            return FakeResponse({"rc": 0, "data": {"total": 1, "diff": [
                {"f12": "830001", "f13": 0, "f14": "ST北交示例", "f2": 2.0, "f3": 0.0},
            ]}})
        return FakeResponse({"rc": 0, "data": {"total": 2, "diff": [
            {"f12": "600001", "f13": 1, "f14": "*ST示例", "f2": 5.0, "f3": 1.0},
            {"f12": "000002", "f13": 0, "f14": "ST示例二", "f2": 3.2, "f3": -0.3},
        ]}})

    sleeps = []
    # 保留真实 em_get（它才是限流入口），只替换底层 HTTP 与 sleep。
    monkeypatch.setattr(_g.EM_SESSION, "get", fake_get)
    monkeypatch.setattr(_g, "EM_MIN_INTERVAL", 1.0)
    monkeypatch.setattr(_g, "_em_last_call", [time.time()])
    monkeypatch.setattr(_g.time, "sleep", lambda seconds: sleeps.append(seconds))

    frame = _g.st_stock_list()
    assert len(frame) == 3
    assert set(frame["code"]) == {"600001", "000002", "830001"}
    assert sleeps and all(seconds > 0 for seconds in sleeps), "em_get 必须串行限流"
    assert frame.attrs["coverage"] == "沪深京"


# ---------------------------------------------------------------------------
# 单票状态
# ---------------------------------------------------------------------------
def test_normal_stock_status():
    out = _resolve(_quote("quote_normal.json"))
    assert out.is_st is False
    assert out.is_suspended is False
    assert out.quote_stale is False
    assert out.limit_up == pytest.approx(1542.53)
    assert out.limit_down == pytest.approx(1262.07)
    assert out.price_limit_pct == pytest.approx(0.10)
    assert out.one_word_limit is None
    assert "tencent_quote" in out.status_source
    assert "eastmoney_st_list" in out.status_source


def test_st_main_board_limit_matches_2026_rule():
    """2026-07-06 起主板 ST 与普通主板同为 10%（旧 5% 规则已废止）。"""
    out = _resolve(_quote("quote_st_main.json"), code="600001",
                   snapshot=_snapshot(codes={"600001"}))
    assert out.is_st is True
    assert out.price_limit_pct == pytest.approx(0.10)
    assert not any("涨跌停幅度与静态规则不一致" in w for w in out.warnings)
    assert any("ST 标的" in w for w in out.warnings)


def test_st_main_board_static_limit_without_quote():
    """报价不可用时也不能回落到已废止的 5%。"""
    out = _resolve(None, quote_error="AstockError: no quote", code="600001",
                   snapshot=_snapshot(codes={"600001"}))
    assert out.is_st is True
    assert out.price_limit_pct == pytest.approx(0.10)


def test_st_from_quote_name_when_list_unavailable():
    snapshot = _snapshot(available=False, warning="ST 名单接口失败（429），且无本地缓存")
    out = _resolve(_quote("quote_st_main.json"), code="600001", snapshot=snapshot)
    assert out.is_st is True
    assert out.status_source.startswith("tencent_quote")
    assert any("ST 名单接口失败" in w for w in out.warnings)
    assert any("ST 标的" in w for w in out.warnings)


# --- issue #17：ST 解析四条分支 -------------------------------------------
def test_st_list_is_authoritative():
    """① 名单可用：以名单为准，True/False 都是确定值。"""
    snapshot = _snapshot(codes={"600001"})
    assert _resolve(_quote("quote_st_main.json"), code="600001",
                    snapshot=snapshot).is_st is True
    assert _resolve(_quote("quote_normal.json"), code="600519",
                    snapshot=snapshot).is_st is False
    assert _resolve(None, quote_error="AstockError: no quote", code="600519",
                    snapshot=snapshot).is_st is False


def test_st_list_available_but_name_conflict_still_true():
    """① 名单未含、名称带 ST → True + 冲突 warning（保守处理）。"""
    out = _resolve(None, quote_error="AstockError: no quote", code="600568",
                   snapshot=_snapshot(), name="ST中珠")
    assert out.is_st is True
    assert any("不一致" in w for w in out.warnings)


def test_quote_name_decides_when_list_unavailable():
    """② 报价可用：名称带 ST → True，否则 False（即使名单不可用）。"""
    snapshot = _snapshot(available=False)
    assert _resolve(_quote("quote_st_main.json"), code="600001",
                    snapshot=snapshot).is_st is True
    assert _resolve(_quote("quote_normal.json"), code="600519",
                    snapshot=snapshot).is_st is False


def test_name_only_st_marks_true_with_warning():
    """③ 报价不可用、名称含 ST → True +「仅依据名称」warning。"""
    out = _resolve(None, quote_error="AstockError: no quote", code="600568",
                   snapshot=_snapshot(available=False), name="ST中珠")
    assert out.is_st is True
    assert any("仅依据名称" in w for w in out.warnings)
    assert any("ST 标的" in w for w in out.warnings)


def test_name_only_non_st_is_unknown_not_false():
    """③ 报价不可用、名称不含 ST → None（不能确认非 ST）+「is_st 未知」warning。"""
    out = _resolve(None, quote_error="AstockError: no quote", code="600519",
                   snapshot=_snapshot(available=False), name="贵州茅台")
    assert out.is_st is None
    assert any("is_st 未知" in w for w in out.warnings)


def test_all_sources_unavailable_is_st_none():
    """④ 名单/报价/名称全不可用（名称退化成代码）→ None + warning。"""
    out = _resolve(None, quote_error="AstockError: no quote", code="999999",
                   snapshot=_snapshot(available=False), name="999999")
    assert out.is_st is None
    assert any("is_st 未知" in w for w in out.warnings)


def test_gem_st_keeps_20pct_limit():
    quote = _quote("quote_st_main.json")
    quote["limit_up"] = 5.94
    quote["limit_down"] = 3.96
    out = _resolve(quote, board="sz_gem", code="300001",
                   snapshot=_snapshot(codes={"300001"}))
    assert out.is_st is True
    assert out.price_limit_pct == pytest.approx(0.20), "创业板 ST 仍为 20%"


def test_limit_conflict_warns_and_uses_quote():
    out = _resolve(_quote("quote_normal.json"), board="bj")
    assert out.price_limit_pct == pytest.approx(0.10)
    assert any("涨跌停幅度与静态规则不一致" in w for w in out.warnings)


def test_suspended_after_open():
    out = _resolve(_quote("quote_suspended.json"),
                   now=dt.datetime(2026, 11, 12, 10, 30))
    assert out.is_suspended is True
    assert out.quote_stale is True
    assert any("判定停牌" in w for w in out.warnings)
    reason = out.execution_block("entry", True, False)
    assert reason and "停牌" in reason


def test_stale_before_open_is_not_suspension():
    out = _resolve(_quote("quote_suspended.json"),
                   now=dt.datetime(2026, 11, 12, 9, 20))
    assert out.is_suspended is None, "盘前定格是正常现象，不得判停牌"
    assert not any("判定停牌" in w for w in out.warnings)


def test_one_word_limit_up_blocks_buy_only():
    out = _resolve(_quote("quote_one_word_up.json"))
    assert out.one_word_limit == "up"
    reason = out.execution_block("entry", True, False)
    assert reason and "一字涨停" in reason
    assert out.execution_block("entry", False, False) is None
    assert out.execution_block("stop", True, True) is None


def test_one_word_limit_down_blocks_sell_only():
    out = _resolve(_quote("quote_one_word_down.json"))
    assert out.one_word_limit == "down"
    reason = out.execution_block("stop", True, True)
    assert reason and "一字跌停" in reason
    assert out.execution_block("entry", True, False) is None


def test_delisted_suspect_from_stale_data_and_missing_quote():
    out = _resolve(None, quote_error="AstockError: no quote", data_lag_days=30,
                   snapshot=_snapshot())
    assert out.is_suspended is True
    assert out.delisted_suspect is True
    assert any("疑似退市/长期停牌" in w for w in out.warnings)
    reason = out.execution_block("hold", False, False)
    assert reason and "疑似退市" in reason


def test_delisted_name_marker():
    out = _resolve(_quote("quote_delisted_name.json"),
                   now=dt.datetime(2026, 11, 12, 10, 30))
    assert out.is_suspended is True
    assert out.delisted_suspect is True
    assert any("退市" in w for w in out.warnings)


def test_nan_quote_degrades_to_nulls():
    out = _resolve(_quote("quote_nan.json"))
    assert out.quote_stale is None
    assert out.is_suspended is None
    assert out.limit_up is None and out.limit_down is None
    assert out.price_limit_pct == pytest.approx(0.10)
    assert any("报价字段异常" in w for w in out.warnings)


# ---------------------------------------------------------------------------
# CLI 接线（instrument 字段 + 可执行性拦截 + 错误路径同构）
# ---------------------------------------------------------------------------
def _toy(n=150, seed=7, drift=0.001, vol=0.02, start=100.0):
    import numpy as np
    rng = np.random.default_rng(seed)
    close = start * np.cumprod(1 + rng.normal(drift, vol, n))
    idx = pd.date_range("2020-01-01", periods=n, freq="B")
    return pd.DataFrame({
        "open": close * 0.999, "high": close * (1 + vol),
        "low": close * (1 - vol), "close": close, "volume": 1e6,
    }, index=idx)


def _run_cli(monkeypatch, trading, stock_name="示例", code="600519"):
    import cli
    import stock_analysis.data_loader.meta as meta
    from stock_analysis.backtest.engine import BacktestResult

    df = _toy()
    monkeypatch.setattr(meta, "resolve_and_fetch", lambda *a, **k: (df, "stock", None))
    monkeypatch.setattr(cli, "last_fetch_info",
                        lambda: {"source": "cache", "from_cache": True,
                                 "degraded": None, "lag_days": None})
    monkeypatch.setattr(cli, "get_stock_name", lambda c: stock_name)
    if trading is not None:
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


def test_cli_instrument_additive_fields(monkeypatch):
    from stock_analysis.data_loader.status import TradingStatus

    trading = TradingStatus(is_st=True, is_suspended=False, limit_up=5.2,
                            limit_down=4.7, quote_stale=False,
                            status_source="eastmoney_st_list+tencent_quote",
                            price_limit_pct=0.05, warnings=[])
    out = _run_cli(monkeypatch, trading)
    inst = out["instrument"]
    assert inst["is_st"] is True
    assert inst["is_suspended"] is False
    assert inst["limit_up"] == pytest.approx(5.2)
    assert inst["limit_down"] == pytest.approx(4.7)
    assert inst["quote_stale"] is False
    assert inst["status_source"] == "eastmoney_st_list+tencent_quote"
    assert inst["price_limit_pct"] == pytest.approx(0.05)


def test_cli_suspended_becomes_untradeable(monkeypatch):
    from stock_analysis.data_loader.status import TradingStatus
    from decision.schema import exit_code_for

    trading = TradingStatus(is_suspended=True, quote_stale=True,
                            status_source="tencent_quote", warnings=[])
    out = _run_cli(monkeypatch, trading)
    assert out["instrument"]["is_suspended"] is True
    assert out["recommendation"]["status"] == "untradeable"
    assert out["recommendation"]["action"] == "avoid"
    assert exit_code_for(out["recommendation"]["status"]) == 40
    assert any("停牌" in w for w in out["diagnostics"]["warnings"])


def test_cli_one_word_limit_up_blocks_buy(monkeypatch):
    """一字涨停 + 策略待入场 → untradeable（退出码 40），不改 actions 决策表。"""
    import cli
    from decision.policy import null_policy
    from stock_analysis.data_loader.status import TradingStatus

    trading = TradingStatus(is_suspended=False, one_word_limit="up",
                            status_source="tencent_quote", warnings=[])
    monkeypatch.setattr(cli.exp_mod, "side_expectancy", lambda *a, **k: {
        "side": "buy", "horizon": 20, "mean": 0.02, "hit_rate": 0.6,
        "n": 100, "t_stat": 2.5, "ok": True})
    monkeypatch.setattr(cli.exp_mod, "trailing_expectancy_ok",
                        lambda close, *a, **k: pd.Series(True, index=close.index))

    def fake_policy(*a, **k):
        policy = null_policy()
        policy.update({"pending": "entry", "holding": False, "position_series": None})
        return policy

    monkeypatch.setattr(cli.pol_mod, "run_policy", fake_policy)
    out = _run_cli(monkeypatch, trading)
    assert out["recommendation"]["status"] == "untradeable"
    assert out["recommendation"]["action"] == "avoid"
    assert any("一字涨停" in w for w in out["diagnostics"]["warnings"])


def test_error_response_instrument_keys_isomorphic():
    import cli

    doc = cli.create_error_response('insufficient_data', '数据不足', '600519', 'stock',
                                    bars=23, start='2026-09-08', end='2026-10-16',
                                    last_close=1329.0, elapsed=0.01)
    assert set(doc["instrument"]) == {
        "code", "name", "board", "price_limit_pct", "is_st",
        "is_suspended", "limit_up", "limit_down", "quote_stale", "status_source",
    }
    assert doc["instrument"]["is_suspended"] is None
    assert doc["instrument"]["status_source"] == "unavailable"


def test_schema_instrument_declares_additive_fields():
    from decision.schema import Instrument

    fields = Instrument.__dataclass_fields__
    for name in ("is_suspended", "limit_up", "limit_down", "quote_stale",
                 "status_source"):
        assert name in fields


# --- issue #17：CLI 边界不再把未知 is_st 伪装成 false ----------------------
def test_cli_offline_st_name_passes_through_true(monkeypatch):
    """端到端：ASTOCK_STATUS_OFFLINE=1 + 600568（本地名称 ST中珠）→ 不为 false。"""
    out = _run_cli(monkeypatch, trading=None, stock_name="ST中珠", code="600568")
    inst = out["instrument"]
    assert inst["is_st"] is True, "离线时名称带 ST 必须判 true，不能伪装成 false"
    assert inst["is_st"] is not False
    assert inst["status_source"] == "offline"
    assert any("仅依据名称" in w for w in out["diagnostics"]["warnings"])


def test_cli_offline_without_name_is_st_null(monkeypatch):
    """端到端：离线且名称退化成代码 → is_st=null + warning（不默认 false）。"""
    out = _run_cli(monkeypatch, trading=None, stock_name="600568", code="600568")
    assert out["instrument"]["is_st"] is None
    assert out["diagnostics"]["warnings"], "状态不可得时必须有 warning"


def _run_cli_error(monkeypatch, code="999999", df=None, start=None):
    import cli
    import stock_analysis.data_loader.meta as meta

    type_err = "行情拉取失败（渠道池全失败且无可用缓存）" if df is None else None
    monkeypatch.setattr(meta, "resolve_and_fetch",
                        lambda *a, **k: (df, "stock", type_err))
    monkeypatch.setattr(cli, "last_fetch_info",
                        lambda: {"source": None, "from_cache": None,
                                 "degraded": None, "lag_days": None})
    return cli.advise_internal(code, start=start, fast=True, no_regime=True)


def test_cli_data_source_error_is_st_null(monkeypatch):
    out = _run_cli_error(monkeypatch, code="999999")
    assert out["recommendation"]["status"] == "data_source_error"
    assert out["instrument"]["is_st"] is None
    assert out["diagnostics"]["warnings"]


def test_cli_insufficient_data_is_st_null(monkeypatch):
    """--start 近 10 天 → insufficient_data，is_st 显式 null。"""
    out = _run_cli_error(monkeypatch, code="600519", df=_toy(n=10), start="20261103")
    assert out["recommendation"]["status"] == "insufficient_data"
    assert out["instrument"]["is_st"] is None


def test_error_response_is_st_null_for_failure_statuses():
    import cli

    for status in ("data_source_error", "insufficient_data"):
        doc = cli.create_error_response(status, "失败", "600519", "stock")
        assert doc["instrument"]["is_st"] is None, status
        assert doc["instrument"]["status_source"] == "unavailable"
