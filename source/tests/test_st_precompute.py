# -*- coding: utf-8 -*-
"""PR #107 P1：ST 名单收盘后预计算 + 盘前只读缓存（离线测试）。

背景（2026-11-15 A5500 实测）：东财 push2/push2delay 对出口 IP 触发 nginx 444，
盘前逐票拉全市场名单会反复撞封禁。本文件的断言围绕四件事：

1. 预计算只写**好名单**，坏结果（空表/重复/失败）绝不覆盖已有缓存；
2. 盘前 ``cli.py`` 主路径只读缓存，对 push2 的请求数为 0；
3. 缓存过期仍可用，但必须 ``stale=true`` + 日期 + warning；
4. 444 / 零字节 FIN 立即中止（不换域、不重试）并写冷却，冷却期内 0 请求。

所有网络入口都打桩，不触网。
"""
from __future__ import annotations

import datetime as dt
import json
import sys
from pathlib import Path

import pandas as pd
import pytest
import requests

SRC = Path(__file__).resolve().parents[1]
for _path in (SRC, SRC.parent / "tools"):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from stock_analysis.data_loader import status  # noqa: E402
from stock_analysis.data_sources import _astock_generated as _g  # noqa: E402
from stock_analysis.data_sources import st_exchange  # noqa: E402

import precompute_st_list as pre  # noqa: E402

NOW_PREOPEN = dt.datetime(2026, 11, 16, 9, 15)   # 周一盘前
NOW_CLOSE = dt.datetime(2026, 11, 13, 18, 35)    # 周五收盘后
PREV_TRADE_DAY = "2026-11-13"


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def _official_offline(monkeypatch):
    """默认不触交易所官方接口：本文件的老用例只描述东财路径。

    需要官方备胎的用例自行覆盖 ``st_exchange.fetch_official_st_lists``。
    """
    def _offline(*args, **kwargs):
        return st_exchange.OfficialListsResult(
            lists=[], requests=0,
            errors=[{"source": "sse", "error": "test offline", "requests": 0},
                    {"source": "szse", "error": "test offline", "requests": 0}])

    monkeypatch.setattr(st_exchange, "fetch_official_st_lists", _offline)


def _official_lists(sse_rows=None, szse_rows=None):
    sse = st_exchange.ExchangeStList(
        source="sse", rows=sse_rows or [
            {"code": "600001", "name": "*ST示例", "st_type": "*ST",
             "market": "sh", "source": "sse"}],
        url="https://query.sse.com.cn/", coverage=st_exchange.SSE_COVERAGE,
        requests=1, fetched_at=NOW_CLOSE.isoformat())
    szse = st_exchange.ExchangeStList(
        source="szse", rows=szse_rows or [
            {"code": "000002", "name": "ST示例二", "st_type": "ST",
             "market": "sz", "source": "szse"}],
        url="https://www.szse.cn/", coverage=st_exchange.SZSE_COVERAGE,
        requests=6, fetched_at=NOW_CLOSE.isoformat(),
        session_date=PREV_TRADE_DAY)
    return [sse, szse]


def _patch_official(monkeypatch, lists=None, errors=None):
    result = st_exchange.OfficialListsResult(
        lists=list(lists or []), errors=list(errors or []),
        requests=sum(item.requests for item in (lists or [])))
    monkeypatch.setattr(st_exchange, "fetch_official_st_lists",
                        lambda *a, **k: result)
    return result
def _st_frame(codes=("600001", "000002")):
    rows = [
        {"code": "600001", "name": "*ST示例", "st_type": "*ST", "market": "sh",
         "price": 5.0, "pct_change": 1.0},
        {"code": "000002", "name": "ST示例二", "st_type": "ST", "market": "sz",
         "price": 3.2, "pct_change": -0.3},
    ]
    frame = pd.DataFrame([row for row in rows if row["code"] in set(codes)])
    frame.attrs["coverage"] = "沪深京"
    return frame


def _snapshot(codes=("600001", "000002")):
    return status._snapshot_from_frame(_st_frame(codes))


def _seed_good_cache(now=NOW_CLOSE):
    return status.write_st_list_cache(_snapshot(), now=now)


def _patch_st_list(monkeypatch, frame, calls):
    def fake(**kwargs):
        calls.append(1)
        return frame

    monkeypatch.setattr(_g, "st_stock_list", fake)
    status.reset_cache()
    return fake


# ---------------------------------------------------------------------------
# 1) 预计算落盘结构 + 一次全市场取数
# ---------------------------------------------------------------------------
def test_precompute_writes_required_cache_fields(monkeypatch, tmp_path):
    calls = []
    _patch_st_list(monkeypatch, _st_frame(), calls)

    stats = pre.precompute(now=NOW_CLOSE)
    assert stats["ok"] is True
    assert stats["state"] == "ok"
    assert stats["count"] == 2
    assert stats["path"] == status.cache_path()
    assert len(calls) == 1, "成功路径只取一次全市场名单"

    payload = json.loads(Path(status.cache_path()).read_text(encoding="utf-8"))
    assert payload["schema"] == 2
    assert payload["source"] == "eastmoney"
    assert payload["fetched_at"] == NOW_CLOSE.isoformat()
    assert payload["session_date"] == PREV_TRADE_DAY
    assert payload["count"] == 2
    assert payload["codes"] == ["000002", "600001"]
    assert payload["sha256"] == status.codes_sha256(payload["codes"])
    assert set(payload["stocks"]) == {"600001", "000002"}
    assert payload["stocks"]["600001"]["name"] == "*ST示例"

    snap = status.get_st_list_cached(now=NOW_PREOPEN)
    assert snap.available is True
    assert snap.state == "ok"
    assert snap.stale is False


def test_precompute_makes_one_full_market_request_sequence(monkeypatch):
    """真实 em_get 路径：沪深 + 北交所各 1 页 = 2 次 HTTP 请求（不是逐票 N 次）。"""
    calls = {"n": 0}

    class FakeResponse:
        status_code = 200
        content = b"{}"

        def __init__(self, payload):
            self._payload = payload

        def json(self):
            return self._payload

        def raise_for_status(self):
            return None

    def fake_get(url, params=None, headers=None, timeout=15, **kwargs):
        calls["n"] += 1
        fs = (params or {}).get("fs", "")
        if fs == "m:0+t:81+s:2048":
            return FakeResponse({"rc": 0, "data": {"total": 1, "diff": [
                {"f12": "830001", "f13": 0, "f14": "ST北交示例", "f2": 2.0, "f3": 0.0},
            ]}})
        return FakeResponse({"rc": 0, "data": {"total": 2, "diff": [
            {"f12": "600001", "f13": 1, "f14": "*ST示例", "f2": 5.0, "f3": 1.0},
            {"f12": "000002", "f13": 0, "f14": "ST示例二", "f2": 3.2, "f3": -0.3},
        ]}})

    monkeypatch.setattr(_g.EM_SESSION, "get", fake_get)
    monkeypatch.setattr(_g, "_em_last_call", [__import__("time").time()])
    monkeypatch.setattr(_g.time, "sleep", lambda seconds: None)

    stats = pre.precompute(now=NOW_CLOSE)
    assert stats["ok"] is True
    assert stats["count"] == 3
    assert stats["requests"] == 2
    assert calls["n"] == 2


# ---------------------------------------------------------------------------
# 2) 坏结果不覆盖好缓存（三态）
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("bad, expected_state", [
    (pd.DataFrame(columns=["code", "name"]), "empty"),
    (pd.DataFrame([
        {"code": "600001", "name": "*ST示例"},
        {"code": "600001", "name": "ST冲突行"},
    ]), "duplicate"),
])
def test_bad_result_never_overwrites_good_cache(monkeypatch, bad,
                                                expected_state):
    _seed_good_cache()
    before = Path(status.cache_path()).read_bytes()

    calls = []
    _patch_st_list(monkeypatch, bad, calls)
    stats = pre.precompute(now=NOW_CLOSE + dt.timedelta(days=1))

    assert stats["ok"] is False
    assert stats["state"] == expected_state
    assert stats["reason"], "坏结果必须带明确 warning"
    assert len(calls) == 2, "空表/重复代码按 #106 重试一次（共 2 次）"
    assert Path(status.cache_path()).read_bytes() == before, \
        "坏结果绝不能覆盖已有好缓存"


def test_network_failure_does_not_overwrite_cache(monkeypatch):
    _seed_good_cache()
    before = Path(status.cache_path()).read_bytes()

    def boom():
        raise RuntimeError("东财 ST 名单不可达（push2 / push2delay 均不可达）")

    monkeypatch.setattr(_g, "st_stock_list", boom)
    status.reset_cache()
    stats = pre.precompute(now=NOW_CLOSE + dt.timedelta(days=1))

    assert stats["ok"] is False
    assert stats["state"] == "failed"
    assert "不可达" in stats["reason"]
    assert Path(status.cache_path()).read_bytes() == before


# ---------------------------------------------------------------------------
# 3) 444 / 零字节 FIN：一次即熔断 + 冷却期内 0 请求
# ---------------------------------------------------------------------------
class _BlockedResponse:
    status_code = 444
    content = b""

    def json(self):
        raise AssertionError("444 响应不应被解析")

    def raise_for_status(self):
        raise AssertionError("444 应由 StListFetchGuard 提前拦截")


def test_http_444_aborts_after_one_request_and_cools(monkeypatch):
    calls = {"n": 0}

    def fake_get(url, params=None, headers=None, timeout=15, **kwargs):
        calls["n"] += 1
        return _BlockedResponse()

    monkeypatch.setattr(_g.EM_SESSION, "get", fake_get)
    monkeypatch.setattr(_g, "_em_last_call", [__import__("time").time()])
    monkeypatch.setattr(_g.time, "sleep", lambda seconds: None)

    stats = pre.precompute(now=NOW_CLOSE)
    assert stats["ok"] is False
    assert stats["blocked"] is True
    assert stats["block_reason"] == "http_444"
    assert stats["requests"] == 1, "444 后不得换域/重试（请求数必须为 1）"
    assert calls["n"] == 1
    assert Path(status.cooldown_path()).exists()
    assert not Path(status.cache_path()).exists(), "阻断窗口内不落半截名单"

    cooled = pre.precompute(now=NOW_CLOSE + dt.timedelta(seconds=1),
                            official=False)
    assert cooled["skipped_cooldown"] is True
    assert cooled["requests"] == 0
    assert calls["n"] == 1, "冷却期内不得再发请求"


def test_zero_byte_fin_aborts_after_one_request_and_cools(monkeypatch):
    calls = {"n": 0}

    def fake_get(url, params=None, headers=None, timeout=15, **kwargs):
        calls["n"] += 1
        raise requests.exceptions.ConnectionError(
            "('Connection aborted.', RemoteDisconnected("
            "'Remote end closed connection without response'))")

    monkeypatch.setattr(_g.EM_SESSION, "get", fake_get)
    monkeypatch.setattr(_g, "_em_last_call", [__import__("time").time()])
    monkeypatch.setattr(_g.time, "sleep", lambda seconds: None)

    stats = pre.precompute(now=NOW_CLOSE)
    assert stats["block_reason"] == "zero_byte_fin"
    assert stats["requests"] == 1
    assert calls["n"] == 1
    cooldown = json.loads(Path(status.cooldown_path()).read_text(encoding="utf-8"))
    assert cooldown["reason"] == "zero_byte_fin"
    assert cooldown["seconds"] == status.ST_LIST_COOLDOWN_SECONDS


def test_main_exit_codes_cooldown_and_failure(monkeypatch, capsys):
    monkeypatch.setattr(_g.EM_SESSION, "get",
                        lambda *a, **k: _BlockedResponse())
    monkeypatch.setattr(_g, "_em_last_call", [__import__("time").time()])
    monkeypatch.setattr(_g.time, "sleep", lambda seconds: None)

    assert pre.main(["--now", NOW_CLOSE.isoformat(),
                     "--no-official"]) == pre.EXIT_FAILED
    assert pre.main(["--now", (NOW_CLOSE + dt.timedelta(seconds=1)).isoformat(),
                     "--no-official"]) == pre.EXIT_COOLDOWN
    assert pre.main(["--clear-cooldown"]) == pre.EXIT_OK
    assert not Path(status.cooldown_path()).exists()


# ---------------------------------------------------------------------------
# 4) 盘前只读：当日/过期/无缓存三种结果
# ---------------------------------------------------------------------------
def test_preopen_reads_fresh_cache_without_network(monkeypatch):
    _seed_good_cache(now=NOW_CLOSE)

    def _boom(*args, **kwargs):
        raise AssertionError("盘前只读路径不得触网")

    monkeypatch.setattr(_g.EM_SESSION, "get", _boom)
    monkeypatch.setattr(status, "fetch_st_list", _boom)
    monkeypatch.setattr(st_exchange, "fetch_official_st_lists", _boom)

    snap = status.get_st_list_cached(now=NOW_PREOPEN)
    assert snap.available is True
    assert snap.state == "ok"
    assert snap.stale is False
    assert "600001" in snap.codes


def test_expired_cache_is_used_with_stale_date_and_warning():
    status.write_st_list_cache(_snapshot(), now=dt.datetime(2026, 11, 12, 18, 35))
    snap = status.get_st_list_cached(now=NOW_PREOPEN)

    assert snap.available is True
    assert snap.stale is True and snap.degraded is True
    assert snap.state == "stale"
    assert "2026-11-12" in snap.warning
    assert PREV_TRADE_DAY in snap.warning
    assert "stale=true" in snap.warning

    trading = status.resolve_trading_status(
        "600001", "sh_main", name="示例", now=NOW_PREOPEN,
        st_snapshot=snap, quote_error="offline")
    assert trading.is_st is True
    assert trading.is_st_source == "exchange_list", "过期缓存仍是名单来源"


def test_missing_cache_is_unavailable_with_clear_warning():
    snap = status.get_st_list_cached(now=NOW_PREOPEN)
    assert snap.available is False
    assert snap.state == "unavailable"
    assert "盘前不请求" in snap.warning
    assert "18:30" in snap.warning

    quote = {"name": "*ST示例", "price": 5.0, "last_close": 5.1,
             "high": 5.2, "low": 4.9, "limit_up": 5.5, "limit_down": 4.6}
    trading = status.resolve_trading_status(
        "600001", "sh_main", name="示例", now=NOW_PREOPEN,
        st_snapshot=snap, quote=quote)
    assert trading.is_st is True
    assert trading.is_st_source == "quote_name"


def test_corrupt_cache_is_rejected_not_trusted(tmp_path):
    path = Path(status.cache_path())
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "schema": 2,
        "source": "eastmoney",
        "fetched_at": NOW_CLOSE.isoformat(),
        "session_date": PREV_TRADE_DAY,
        "count": 1,
        "sha256": "deadbeef",
        "codes": ["600001"],
        "stocks": {"600001": {"name": "*ST示例"}},
    }), encoding="utf-8")

    snap = status.get_st_list_cached(now=NOW_PREOPEN)
    assert snap.available is False
    assert snap.state == "corrupt"
    assert "校验失败" in snap.warning


def test_success_clears_stale_cooldown(monkeypatch):
    status.mark_cooldown("http_444", "旧封锁", seconds=60, now=NOW_CLOSE)
    assert Path(status.cooldown_path()).exists()

    calls = []
    _patch_st_list(monkeypatch, _st_frame(), calls)
    stats = pre.precompute(now=NOW_CLOSE + dt.timedelta(seconds=61), force=True)

    assert stats["ok"] is True
    assert not Path(status.cooldown_path()).exists(), \
        "成功取数说明封锁已解除，应清掉冷却文件"


def test_write_rejects_bad_snapshot():
    with pytest.raises(ValueError):
        status.write_st_list_cache(
            status.StListSnapshot(available=False, state="empty"), now=NOW_CLOSE)
    with pytest.raises(ValueError):
        status.write_st_list_cache(
            status.StListSnapshot(available=True, state="duplicate",
                                  stocks={"600001": {}}), now=NOW_CLOSE)


# ---------------------------------------------------------------------------
# 5) CLI 主路径零请求（真实 advise_internal，打桩行情/回测）
# ---------------------------------------------------------------------------
def _toy(n=150, seed=7):
    import numpy as np

    rng = np.random.default_rng(seed)
    close = 100.0 * np.cumprod(1 + rng.normal(0.001, 0.02, n))
    idx = pd.date_range("2024-01-01", periods=n, freq="B")
    return pd.DataFrame({
        "open": close * 0.999, "high": close * 1.02,
        "low": close * 0.98, "close": close, "volume": 1e6,
    }, index=idx)


def test_cli_main_path_makes_zero_st_list_requests(monkeypatch, capsys):
    import cli
    from stock_analysis.backtest.engine import BacktestResult

    seed_now = dt.datetime.now()
    _seed_good_cache(now=seed_now)

    calls = {"n": 0}

    def _boom(*args, **kwargs):
        calls["n"] += 1
        raise AssertionError("盘前 cli 主路径不得请求 ST 名单")

    monkeypatch.setattr(_g.EM_SESSION, "get", _boom)
    monkeypatch.setattr(_g, "st_stock_list", _boom)
    monkeypatch.setattr(status, "fetch_st_list", _boom)
    monkeypatch.setattr(status, "get_st_list", _boom)
    monkeypatch.setattr(st_exchange, "fetch_official_st_lists", _boom)
    monkeypatch.setattr(st_exchange.requests, "get", _boom)

    df = _toy()
    monkeypatch.setattr(cli, "get_stock_daily", lambda *a, **k: df)
    # issue #124：``advise_internal`` 实际走 ``meta.resolve_and_fetch``（函数内
    # import），旧的 ``cli.get_stock_daily`` 打桩不是调用点。此前这条用例能过，
    # 是因为同一会话里别的用例把缓存写进了真实 ``data/``；隔离 state 后必须
    # 显式注入行情，否则会落到渠道池失败分支。
    import stock_analysis.data_loader.meta as meta_mod
    monkeypatch.setattr(meta_mod, "resolve_and_fetch",
                        lambda *a, **k: (df, "stock", None))
    monkeypatch.setattr(cli, "get_stock_name", lambda code: code)

    names = list(cli.build_strategy_signals().keys())[:3]
    comp = pd.DataFrame(
        [{"评分": 1.0 - i * 0.1, "评分IS": 1.0 - i * 0.1,
          "评分OOS": 0.9 - i * 0.1, "年化收益": "10.00%",
          "最大回撤": "5.00%", "胜率": "60.00%", "交易次数": 20}
         for i, _ in enumerate(names)],
        index=names,
    )
    result = BacktestResult(trade_count=20, win_rate=0.6, annual_return=0.1,
                            max_drawdown=0.05)
    result.equity_curve = [100000.0] * 50
    monkeypatch.setattr(cli, "run_strategy_tournament",
                        lambda *a, **k: (names[0], result, comp))

    out = cli.advise_internal("600519", fast=True, no_regime=True)

    assert out["instrument"]["is_st_source"] == "exchange_list"
    assert out["instrument"]["is_st"] is False
    assert calls["n"] == 0, "盘前路径对 push2 / 旧取数入口的请求数必须为 0"

# ---------------------------------------------------------------------------
# 6) 多源（2026-11-15）：官方备胎 / 合并 / 冲突 / 不覆盖
# ---------------------------------------------------------------------------
def test_official_fallback_writes_cache_when_eastmoney_blocked(monkeypatch):
    monkeypatch.setattr(_g.EM_SESSION, "get",
                        lambda *a, **k: _BlockedResponse())
    monkeypatch.setattr(_g, "_em_last_call", [__import__("time").time()])
    monkeypatch.setattr(_g.time, "sleep", lambda seconds: None)
    _patch_official(monkeypatch, _official_lists())

    stats = pre.precompute(now=NOW_CLOSE, force=True)

    assert stats["ok"] is True
    assert stats["source"] == "exchange"
    assert stats["merged_from"] == ["sse", "szse"]
    assert stats["count"] == 2
    assert stats["requests"] == 8, "东财 1（444）+ SSE 1 + SZSE 6"
    assert stats["blocked"] is True and stats["cooldown"]

    payload = json.loads(Path(status.cache_path()).read_text(encoding="utf-8"))
    assert payload["source"] == "exchange"
    assert payload["merged_from"] == ["sse", "szse"]
    assert payload["codes"] == ["000002", "600001"]
    assert payload["coverage"].startswith("沪深A+B（官方"), \
        "issue #123：深市 fxjsb 实含 B 股，覆盖文案必须写出来"


def test_incomplete_official_never_overwrites_good_cache(monkeypatch):
    _seed_good_cache()
    before = Path(status.cache_path()).read_bytes()

    monkeypatch.setattr(_g, "st_stock_list", lambda: (_ for _ in ()).throw(
        RuntimeError("东财 ST 名单不可达（push2 / push2delay 均不可达）")))
    status.reset_cache()
    _patch_official(monkeypatch, lists=[_official_lists()[0]],
                    errors=[{"source": "szse", "error": "深交所挂了",
                             "requests": 6}])

    stats = pre.precompute(now=NOW_CLOSE + dt.timedelta(days=1))

    assert stats["ok"] is False
    assert "szse 官方源失败" in stats["reason"]
    assert Path(status.cache_path()).read_bytes() == before, \
        "官方只有一个所成功（沪深不完整）时不得覆盖好缓存"


def test_official_empty_never_overwrites_good_cache(monkeypatch):
    _seed_good_cache()
    before = Path(status.cache_path()).read_bytes()

    monkeypatch.setattr(_g, "st_stock_list", lambda: (_ for _ in ()).throw(
        RuntimeError("东财 ST 名单不可达")))
    status.reset_cache()
    _patch_official(monkeypatch, lists=[], errors=[
        {"source": "sse", "error": "上交所风险警示名单为空", "requests": 1},
        {"source": "szse", "error": "深交所风险警示名单为空", "requests": 6},
    ])

    stats = pre.precompute(now=NOW_CLOSE + dt.timedelta(days=1))
    assert stats["ok"] is False
    assert Path(status.cache_path()).read_bytes() == before


def test_eastmoney_and_official_merge_conflict_takes_union(monkeypatch):
    frame = pd.DataFrame([
        {"code": "600001", "name": "*ST示例", "st_type": "*ST", "market": "sh"},
        {"code": "600003", "name": "ST东财独有", "st_type": "ST", "market": "sh"},
        {"code": "830001", "name": "ST北交示例", "st_type": "ST", "market": "bj"},
    ])
    frame.attrs["coverage"] = "沪深京"
    calls = []
    _patch_st_list(monkeypatch, frame, calls)
    _patch_official(monkeypatch, _official_lists(
        sse_rows=[{"code": "600001", "name": "ST示例改名", "st_type": "ST",
                   "market": "sh", "source": "sse"}],
        szse_rows=[{"code": "000002", "name": "ST示例二", "st_type": "ST",
                    "market": "sz", "source": "szse"}]))

    stats = pre.precompute(now=NOW_CLOSE)

    assert stats["ok"] is True
    assert stats["source"] == "eastmoney"
    assert stats["merged_from"] == ["eastmoney", "sse", "szse"]
    assert stats["count"] == 4, "并集：600001/600003/830001 + 000002"
    conflicts = stats["conflicts"]
    assert isinstance(conflicts, list), "issue #123：conflicts 是条目列表"
    kinds = {item["kind"] for item in conflicts}
    assert kinds == {"name_mismatch", "east_only", "official_only"}, kinds
    for item in conflicts:
        assert set(item) == {"kind", "count", "codes", "message"}
        assert item["count"] == len(item["codes"])
    conflict = stats["conflict_summary"] or ""
    for needle in ("两源冲突", "名称不一致", "东财独有", "官方独有", "是 ST"):
        assert needle in conflict, (needle, conflict)
    assert "830001" not in conflict, "北交所不在官方覆盖内，不算冲突"
    assert all("830001" not in item["codes"] for item in conflicts)
    assert stats["reason"] == conflict

    payload = json.loads(Path(status.cache_path()).read_text(encoding="utf-8"))
    assert payload["merged_from"] == ["eastmoney", "sse", "szse"]
    assert set(payload["codes"]) == {"000002", "600001", "600003", "830001"}
    assert payload["stocks"]["600001"]["st_type"] == "*ST", "*ST 优先"


def test_eastmoney_cooldown_still_uses_official_backup(monkeypatch):
    status.mark_cooldown("http_444", "旧封锁", seconds=1800, now=NOW_CLOSE)
    calls = {"official": 0}

    def fake_official(*args, **kwargs):
        calls["official"] += 1
        return st_exchange.OfficialListsResult(
            lists=_official_lists(), errors=[], requests=7)

    monkeypatch.setattr(st_exchange, "fetch_official_st_lists", fake_official)

    def _east_boom(*args, **kwargs):
        raise AssertionError("东财冷却期内不得请求东财")

    monkeypatch.setattr(_g.EM_SESSION, "get", _east_boom)

    stats = pre.precompute(now=NOW_CLOSE + dt.timedelta(seconds=1))

    assert stats["eastmoney_skipped_cooldown"] is True
    assert stats["skipped_cooldown"] is False, \
        "官方备胎被尝试过，本轮不是「0 请求跳过」"
    assert calls["official"] == 1
    assert stats["ok"] is True and stats["source"] == "exchange"
    assert stats["requests"] == 7, "冷却期内东财 0 请求，官方 7 次"


def test_no_official_keeps_cooldown_exit_contract(monkeypatch):
    status.mark_cooldown("http_444", "旧封锁", seconds=1800, now=NOW_CLOSE)
    monkeypatch.setattr(st_exchange, "fetch_official_st_lists",
                        lambda *a, **k: (_ for _ in ()).throw(
                            AssertionError("--no-official 不得请求官方源")))

    stats = pre.precompute(now=NOW_CLOSE + dt.timedelta(seconds=1),
                           official=False)

    assert stats["skipped_cooldown"] is True
    assert stats["requests"] == 0
    assert stats["sources"]["sse"]["state"] == "disabled"
    assert stats["sources"]["szse"]["state"] == "disabled"


def test_partial_official_does_not_flag_uncovered_market(monkeypatch):
    """只有上交所成功时，不能把全部深市东财代码误报成「东财独有」。"""
    frame = pd.DataFrame([
        {"code": "600001", "name": "*ST示例", "st_type": "*ST", "market": "sh"},
        {"code": "000002", "name": "ST示例二", "st_type": "ST", "market": "sz"},
    ])
    frame.attrs["coverage"] = "沪深京"
    _patch_st_list(monkeypatch, frame, [])
    _patch_official(monkeypatch, lists=[_official_lists()[0]])  # 仅 SSE

    stats = pre.precompute(now=NOW_CLOSE)

    assert stats["ok"] is True
    assert stats["merged_from"] == ["eastmoney", "sse"]
    assert stats["count"] == 2
    assert stats["conflicts"] == [], stats["conflicts"]
    conflict = stats["conflict_summary"] or ""
    assert "东财独有" not in conflict, conflict
    assert "两源冲突" not in conflict, conflict
