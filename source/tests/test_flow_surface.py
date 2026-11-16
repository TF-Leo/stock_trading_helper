# -*- coding: utf-8 -*-
"""issue #130：东财取数显式传 surface，域隔离在生产调用路径上真正生效。

背景
----
#128 给 ``eastmoney_guard`` 加了域级冷却，但生产调用点（``precompute_flow`` /
``precompute_st_list`` / ``market_watch``）**不传 surface**，于是 444 写的是
**顶层共享冷却块**。读取取「顶层 ∪ 本域」，所以 18:30 资金流被 444 封掉后，
18:45 的事件预计算（datacenter-web 本域无条目）被顶层块命中 → **0 请求跳过**。
域隔离只在 guard 单测里成立，生产上是失效的。

本文件用**真实生产调用路径**（不打桩 guard / 冷却 / 预算，只替换 HTTP 层）
锁住四条：

1. push2 域 444 → 只写本域，datacenter 事件任务**照常发满请求**；
2. datacenter 域 444 → 只写本域，资金流 / ST 名单照常运行；
3. 同一冷却窗口内**两个不同域**都 444 → 写顶层共享块 → 全域 0 请求；
4. 顶层共享块存在时所有域 0 请求（含监控池）。

所有网络入口都打桩（只替换 ``EM_SESSION.get`` 与 ``urllib`` 入口），
不触网、不探测东财真实端点；测试只用公开代码 600519 / 600000。
"""
from __future__ import annotations

import datetime as dt
import json
import sys
import time
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1]
TOOLS = SRC.parent / "tools"
for _path in (SRC, TOOLS):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from stock_analysis.data_loader import status as trading_status  # noqa: E402
from stock_analysis.data_sources import _astock_generated as _g  # noqa: E402
from stock_analysis.data_sources import eastmoney_guard as em  # noqa: E402
from stock_analysis.data_sources import market_watch as mw  # noqa: E402

import precompute_events as pe  # noqa: E402
import precompute_flow as flow_tool  # noqa: E402
import precompute_st_list as st_tool  # noqa: E402

NOW = dt.datetime(2026, 11, 13, 18, 35)
NOW_TS = NOW.timestamp()

DATACENTER_HOST = em.DATACENTER_SURFACE
PUSH2_HOST = em.PUSH2_SURFACE
MONITOR_HOST = mw.SURFACE

PUSH2_URL = "https://push2his.eastmoney.com/api/qt/stock/fflow/daykline/get"


def datetime_now():
    return NOW


# ---------------------------------------------------------------------------
# 桩：只替换 HTTP 层；``em_get`` / ``EmFetchGuard`` / 冷却 / 预算全走真代码
# ---------------------------------------------------------------------------
class _Response:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code
        self.content = b"{}" if status_code != 444 else b""

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


@pytest.fixture(autouse=True)
def _fast_em(monkeypatch):
    """跳过 em_get 的 1s 串行节流：测试只关心「发了几个请求 / 发到哪个域」。"""
    monkeypatch.setattr(_g, "_em_last_call", [time.time()])
    monkeypatch.setattr(_g.time, "sleep", lambda seconds: None)
    mw.reset_cache()


def _patch_session(monkeypatch, handler):
    """按 URL 分派响应，记录每个请求的 host。"""
    calls = []

    def fake_get(url, params=None, headers=None, timeout=15, **kwargs):
        calls.append(url)
        return handler(url, params)

    monkeypatch.setattr(_g.EM_SESSION, "get", fake_get)
    return calls


def _datacenter_ok(url, params=None):
    report_filter = str((params or {}).get("filter") or "")
    if "SECURITY_CODE=" in report_filter:
        return _Response({"code": 9201})
    return _Response({"code": 0, "result": {
        "pages": 5, "count": 100,
        "data": [{"SECURITY_CODE": "600519", "FREE_DATE": "2026-11-18"}]}})


def _boom(url, params=None):
    raise AssertionError(f"本轮不得发请求：{url}")


def _host(url) -> str:
    return em.surface_of(url)


def _sina_rows(n=20):
    return [{"date": f"2026-11-{day:02d}", "close": "100.0000",
             "net_amount": f"{day}.0000", "turnover": "1.0000"}
            for day in range(1, n + 1)]


def _raw_state() -> dict:
    return json.loads(Path(em.state_path()).read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# 1) push2 域 444 → 只封本域，datacenter 事件任务照常运行
# ---------------------------------------------------------------------------
def test_push2_444_does_not_block_datacenter_events(tmp_path, monkeypatch,
                                                   capsys):
    """生产路径：18:30 资金流 444 → 18:45 事件任务仍发满 8 个请求。

    这是 issue #130 的核心回归：修复前资金流不传 surface，444 写顶层共享块，
    事件预计算读到顶层封锁后 0 请求跳过。
    """
    flow_calls, events_calls = [], []

    def fake_get(url, params=None, headers=None, timeout=15, **kwargs):
        host = _host(url)
        if host == PUSH2_HOST:
            flow_calls.append(url)
            return _Response(None, 444)
        events_calls.append(url)
        return _datacenter_ok(url, params)

    monkeypatch.setattr(_g.EM_SESSION, "get", fake_get)

    def eastmoney_fetch(code):
        # 模拟生成物 stock_fund_flow_120d：吞掉异常并返回 []。
        try:
            _g.em_get(PUSH2_URL)
        except Exception:
            pass
        return []

    flow_stats = flow_tool.precompute(
        ["600519"], out_dir=str(tmp_path / "flow"), interval=1.0, retries=0,
        fetch_fn=eastmoney_fetch,
        sina_fetch_fn=lambda code: _sina_rows(),
        sleep_fn=lambda seconds: None, now_fn=datetime_now)

    assert flow_stats["blocked"] is True
    assert flow_stats["block_reason"] == "http_444"
    assert len(flow_calls) == 1
    raw = _raw_state()
    assert PUSH2_HOST in raw[em.SURFACES_KEY], "资金流必须写本域条目"
    assert "blocked_until" not in raw, "单个域 444 不得写顶层共享块"
    assert em.cooldown_state() is None

    stats = pe.precompute(["600519"], out_dir=str(tmp_path / "events"),
                          retries=0, sleep_fn=lambda _s: None,
                          now_fn=lambda: NOW)

    assert stats["skipped_cooldown"] is False, "datacenter 不得被 push2 连坐"
    assert stats["blocked"] is False
    assert len(events_calls) == 4 + 4, "4 个探针 + 4 个端点都必须照常发出"
    assert all(_host(url) == DATACENTER_HOST for url in events_calls)
    assert capsys.readouterr().err.count("SKIP") == 0


# ---------------------------------------------------------------------------
# 2) datacenter 域 444 → 只封本域，资金流照常运行
# ---------------------------------------------------------------------------
def test_datacenter_444_does_not_block_push2_flow(tmp_path, monkeypatch):
    calls = _patch_session(monkeypatch, lambda url, params: _Response(None, 444))

    stats = pe.precompute(["600519"], out_dir=str(tmp_path / "events"),
                          retries=0, sleep_fn=lambda _s: None,
                          now_fn=lambda: NOW)
    assert stats["blocked"] is True
    assert len(calls) == 1, "命中 444 后不得再发请求"
    raw = _raw_state()
    assert DATACENTER_HOST in raw[em.SURFACES_KEY]
    assert "blocked_until" not in raw
    assert em.cooldown_state() is None

    # 资金流本域无条目、顶层无封锁 → 正常发请求。
    flow_calls = []

    def eastmoney_fetch(code):
        flow_calls.append(code)
        return [{"date": f"2026-11-{day:02d}", "main_net": 1.0,
                 "small_net": 0.0, "mid_net": 0.0, "large_net": 0.0,
                 "super_net": 0.0} for day in range(1, 21)]

    flow_stats = flow_tool.precompute(
        ["600519"], out_dir=str(tmp_path / "flow"), interval=1.0, retries=0,
        fetch_fn=eastmoney_fetch,
        sina_fetch_fn=lambda code: pytest.fail("东财成功时不得调用新浪"),
        sleep_fn=lambda seconds: None, now_fn=datetime_now)

    assert flow_stats["skipped_cooldown"] is False
    assert flow_stats["blocked"] is False
    assert flow_calls == ["600519"]


# ---------------------------------------------------------------------------
# 3) 同一冷却窗口内两个不同域都 444 → 写顶层块 → 全域封锁
# ---------------------------------------------------------------------------
def test_two_domains_444_escalate_to_shared_block(tmp_path, monkeypatch):
    """资金流（push2）先 444，随后事件（datacenter）也 444 → 顶层块被写下。"""
    # 18:30 资金流：push2 444。
    _patch_session(monkeypatch, lambda url, params: _Response(None, 444))

    def eastmoney_fetch(code):
        try:
            _g.em_get(PUSH2_URL)
        except Exception:
            pass
        return []

    flow_tool.precompute(
        ["600519"], out_dir=str(tmp_path / "flow"), interval=1.0, retries=0,
        fetch_fn=eastmoney_fetch,
        sina_fetch_fn=lambda code: _sina_rows(),
        sleep_fn=lambda seconds: None, now_fn=datetime_now)

    raw = _raw_state()
    assert PUSH2_HOST in raw[em.SURFACES_KEY]
    assert "blocked_until" not in raw, "第一个域单独命中时不得写顶层块"

    # 18:40 事件：datacenter 也 444（仍在升级投票窗口 600s 内）。
    stats = pe.precompute(["600519"], out_dir=str(tmp_path / "events"),
                          retries=0, sleep_fn=lambda _s: None,
                          now_fn=lambda: NOW + dt.timedelta(minutes=5))

    assert stats["blocked"] is True
    raw = _raw_state()
    assert DATACENTER_HOST in raw[em.SURFACES_KEY]
    assert "blocked_until" in raw, "两个域都 444 → 判定 IP 级封锁，写顶层块"
    # P3-1（PR #131）：``surfaces_blocked`` 每条带触发原因与时间戳；
    # 域名清单在 ``surfaces_blocked_names``。
    assert set(raw.get("surfaces_blocked_names") or []) == {PUSH2_HOST,
                                                            DATACENTER_HOST}
    assert all(item["reason"] == "http_444"
               for item in raw["surfaces_blocked"])
    # 冷却按注入的 ``now_fn`` 写（NOW / NOW+5min），因此按同一时钟读。
    assert em.cooldown_state(now=NOW + dt.timedelta(minutes=5)) is not None

    # 第三个域（监控池）读「顶层 ∪ 本域」→ 也被封锁，0 请求。
    guard = em.EmFetchGuard(surface=MONITOR_HOST,
                            now_fn=lambda: NOW_TS + 300)
    cooling = guard.cooling()
    assert cooling is not None and cooling["scope"] == "shared"


def test_same_surface_twice_does_not_escalate(tmp_path, monkeypatch):
    """push2 / push2delay / push2his 是同一风控面：两次命中不得升级成全域封锁。"""
    em.mark_cooldown("http_444", "push2his 被封", seconds=600, now=NOW,
                     surface="push2his.eastmoney.com")
    em.mark_cooldown("http_444", "push2 也被封", seconds=600, now=NOW,
                     surface=em.PUSH2_SURFACE)

    raw = _raw_state()
    assert set(raw[em.SURFACES_KEY]) == {PUSH2_HOST}
    assert "blocked_until" not in raw, "同一风控面不得升级为 IP 级封锁"
    assert em.cooldown_state(now=NOW) is None


# ---------------------------------------------------------------------------
# 4) 顶层块存在时所有域 0 请求（资金流 / ST 名单 / 事件 / 监控池）
# ---------------------------------------------------------------------------
def test_shared_block_makes_every_surface_send_zero_requests(tmp_path,
                                                             monkeypatch):
    calls = _patch_session(monkeypatch, _boom)
    # 冷却按**当前**时间写：``fetch_pool`` / ``main`` 走真实时钟。
    em.mark_cooldown("http_444", "整条出口被封", seconds=1800)

    # 资金流：冷却 → 0 请求、退出码语义（skipped_cooldown）。
    flow_stats = flow_tool.precompute(
        ["600519"], out_dir=str(tmp_path / "flow"), interval=1.0, retries=0,
        fetch_fn=lambda code: pytest.fail("冷却期内不得发请求"),
        sina_fetch_fn=lambda code: pytest.fail("冷却期内不得发请求"),
        sleep_fn=lambda seconds: None, now_fn=datetime_now)
    assert flow_stats["skipped_cooldown"] is True
    assert flow_stats["requests"] == 0

    # 事件：整轮 0 请求。
    event_stats = pe.precompute(["600519"], out_dir=str(tmp_path / "events"),
                                retries=0, sleep_fn=lambda _s: None)
    assert event_stats["skipped_cooldown"] is True
    assert event_stats["requests_sent"] == 0

    # ST 名单：东财 0 请求（官方备胎是不同主机，本用例只验东财）。
    monkeypatch.setattr(st_tool.st_exchange, "fetch_official_st_lists",
                        lambda *a, **k: pytest.fail("--no-official 才跳过；"
                                                    "本用例只要东财 0 请求"))
    st_stats = st_tool.precompute(official=False)
    assert st_stats["eastmoney_skipped_cooldown"] is True
    assert st_stats["requests"] == 0

    # 监控池：冷却 → MarketWatchError（不静默成「今天没有监控」）。
    with pytest.raises(mw.MarketWatchError):
        mw.fetch_pool()

    assert calls == [], "顶层共享封锁下所有域都必须 0 请求"


# ---------------------------------------------------------------------------
# 5) 每个生产调用点都真的带上了自己的 surface
# ---------------------------------------------------------------------------
def test_every_production_guard_passes_its_own_surface(monkeypatch):
    """直接检查构造出来的闸门 surface —— 防止将来又漏传退回顶层封锁。"""
    seen = []

    class _Spy(em.EmFetchGuard):
        def __init__(self, *args, **kwargs):
            seen.append(kwargs.get("surface"))
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(flow_tool.em_guard, "EmFetchGuard", _Spy)
    flow_tool.precompute(
        [], out_dir="/tmp/unused", fetch_fn=lambda code: [],
        sina_fetch_fn=lambda code: [], sleep_fn=lambda seconds: None,
        now_fn=datetime_now)
    assert seen == [PUSH2_HOST], "资金流闸门必须带 push2 家族 surface"

    # ST 名单闸门默认 surface。
    guard = trading_status.StListFetchGuard(now_fn=lambda: NOW_TS)
    assert guard.surface == trading_status.ST_LIST_SURFACE == PUSH2_HOST

    # 事件闸门。
    event_guard, _budget = pe._make_guard()
    assert event_guard.surface == DATACENTER_HOST

    # 监控池：走真 fetch_pool，只换 HTTP 层。
    monkeypatch.setattr(mw._em_guard, "EmFetchGuard", _Spy)
    _patch_session(monkeypatch, lambda url, params: _Response([]))
    mw.fetch_pool()
    assert seen[-1] == MONITOR_HOST, "监控池闸门必须带自己的 surface"
