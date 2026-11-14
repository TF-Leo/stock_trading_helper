# -*- coding: utf-8 -*-
"""T6（issue #1 / R11 缺口表 #6）：全市场重点监控池 —— 离线测试。

覆盖：
* 解析：命中 / 未命中 / **北交所 ``MARKET="B"`` → ``BJ`` 而非 ``SZ``** /
  未知市场值原样带出 / 字段不合法抛错（绝不静默跳过）；
* 空结果与「接口坏了」分开：``[]`` 是**唯一合法**的空池；``null``（HTTP 200
  但 body 为空）/ 非 2xx / 非 JSON / 非列表抛 :class:`MarketWatchError`；
* 缓存：当日有效 → **请求次数 1**；第二次 0 次；接口失败回退旧缓存并
  ``stale=True``；失败且无可用缓存 → ``available=False``；**缓存损坏 ≠ 请求
  失败** —— 坏缓存被忽略并重新请求一次，warning 分开写「缓存损坏」；
* CLI：``evidence.market_watch`` additive、键同构（G35 错误路径）、
  ``diagnostics.warnings`` 在不可用/过期时给原因、A/B 不改 ``recommendation``；
* 渲染：命中行位置（闸门之后、建议行之前）、未命中/不可用整行消失、
  **``stale=True`` 且有命中时照常出行**（P2-2）、不改写 ``action`` / ``confidence``；
* 端到端：人为注入命中 → CLI JSON → 渲染输出，证明链路不是死代码。

所有网络入口在本文件里都被打桩；用例默认 ``ASTOCK_MARKET_WATCH_OFFLINE=1``
（见 conftest），需要「联网语义」的用例显式传 ``allow_network=True``。
"""
from __future__ import annotations

import copy
import json
import socket
import sys
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

SRC = Path(__file__).resolve().parents[1]
for _path in (SRC, SRC.parent / "tools"):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from stock_analysis.advice_render import (  # noqa: E402
    MARKET_WATCH_MARK,
    render_advice,
)
from stock_analysis.data_sources import market_watch as mw  # noqa: E402

NOW = datetime(2026, 11, 16, 9, 15)  # 北京时间，周一，监控窗口首日


# ---------------------------------------------------------------------------
# fixtures：上游 §8.4 真实字段名（2026-11-14 实测 19 条里的结构）
# ---------------------------------------------------------------------------
def _row(code, market="1", name="示例股份", start="2026-11-16", end="2026-11-27",
         link=""):
    return {"MARKET": market, "STKNAME": name, "STKCODE": code,
            "VALIDATESTARTDATE": start, "VALIDATEENDDATE": end,
            "LINK_URL": link}


#: 命中的是虚构代码（隐私：不出现真实持仓）。
HIT_CODE = "600000"
MISS_CODE = "600001"
BJ_CODE = "920999"


def _pool_rows():
    return [
        _row(HIT_CODE, "1", "示例股份"),
        _row(BJ_CODE, "B", "*ST示例", link="https://example.invalid/x"),
        _row("000002", "0", "示例地产"),
    ]


#: ``_Counter(payload=...)`` 的哨兵：区分「没传」与「显式传 None（body=null）」。
_UNSET = object()


class _FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


class _Counter:
    """统计 ``em_get`` 调用次数（T6 的核心断言：一次请求服务全池）。

    ``payload`` 用哨兵默认值：显式传 ``None`` 表示「接口返回 body=null」，
    不能和「没传、用默认池」混为一谈。
    """

    def __init__(self, payload=_UNSET, error=None, status_code=200):
        self.calls = []
        self.payload = _pool_rows() if payload is _UNSET else payload
        self.error = error
        self.status_code = status_code

    def __call__(self, url, headers=None, timeout=None, **kwargs):
        self.calls.append({"url": url, "headers": headers, "timeout": timeout})
        if self.error is not None:
            raise self.error
        return _FakeResponse(self.payload, self.status_code)


@pytest.fixture
def patched(monkeypatch):
    """把 ``_http_get`` 换成计数器；返回计数器供断言调用次数。"""
    counter = _Counter()
    monkeypatch.setattr(mw, "_http_get", counter)
    mw.reset_cache()
    return counter


def _state(tmp_path):
    return str(tmp_path / "state")


# ---------------------------------------------------------------------------
# 1) 解析
# ---------------------------------------------------------------------------
def test_parse_hit_and_miss():
    pool = mw.parse_pool(_pool_rows())
    assert set(pool) == {HIT_CODE, BJ_CODE, "000002"}
    entry = pool[HIT_CODE]
    assert entry["start"] == "2026-11-16"
    assert entry["end"] == "2026-11-27"
    assert entry["reason"] == "monitor"
    assert MISS_CODE not in pool


def test_parse_bj_market_is_bj_not_sz():
    """上游三值坑：``MARKET="B"`` 是北交所，绝不能标成 SZ。"""
    pool = mw.parse_pool(_pool_rows())
    assert pool[BJ_CODE]["market"] == "BJ"
    assert pool[BJ_CODE]["market"] != "SZ"
    # 0/1 仍按深/沪
    assert pool["000002"]["market"] == "SZ"
    assert pool[HIT_CODE]["market"] == "SH"


def test_parse_unknown_market_is_kept_verbatim():
    pool = mw.parse_pool([_row("600002", market="X")])
    assert pool["600002"]["market"] == "?X"


def test_parse_st_name_is_risk_warning():
    pool = mw.parse_pool([_row(BJ_CODE, "B", "*ST示例")])
    assert pool[BJ_CODE]["reason"] == "risk_warning"


@pytest.mark.parametrize("row,needle", [
    ({"MARKET": "1", "STKNAME": "x", "STKCODE": "60000",
      "VALIDATESTARTDATE": "2026-11-16", "VALIDATEENDDATE": "2026-11-27"}, "STKCODE"),
    ({"MARKET": "1", "STKNAME": "x", "STKCODE": "600000",
      "VALIDATESTARTDATE": "2026/11/16", "VALIDATEENDDATE": "2026-11-27"}, "VALIDATESTARTDATE"),
    ({"MARKET": "1", "STKNAME": "x", "STKCODE": "600000",
      "VALIDATESTARTDATE": "2026-11-16"}, "VALIDATEENDDATE"),
    ("not-a-dict", "不是对象"),
])
def test_parse_invalid_row_raises(row, needle):
    """字段不合法一律抛错：绝不静默跳过（否则「接口改字段」会读成「无监控」）。"""
    with pytest.raises(mw.MarketWatchError) as exc:
        mw.parse_pool([row])
    assert needle in str(exc.value)


# ---------------------------------------------------------------------------
# 2) 空结果 ≠ 接口坏了
# ---------------------------------------------------------------------------
def test_empty_list_is_a_legal_empty_pool(monkeypatch, tmp_path):
    counter = _Counter(payload=[])
    monkeypatch.setattr(mw, "_http_get", counter)
    mw.reset_cache()
    snapshot = mw.load_pool(now=NOW, state_dir=_state(tmp_path), allow_network=True)
    assert snapshot.available is True
    assert snapshot.entries == {}
    assert snapshot.error is None


def test_null_body_is_not_an_empty_pool(monkeypatch, tmp_path):
    """HTTP 200 + body ``null`` 是失败，**不是**「全市场 0 条」（P2-1）。

    README 契约：「非列表一律抛错」。``[]`` 与 ``null`` 必须可区分 ——
    把 ``null`` 读成空池，正是「接口坏了」被静默当成「今天没人被监控」。
    """
    counter = _Counter(payload=None)
    monkeypatch.setattr(mw, "_http_get", counter)
    mw.reset_cache()
    with pytest.raises(mw.MarketWatchError) as exc:
        mw.fetch_pool()
    assert "null" in str(exc.value)

    # 端到端：null 不得产出 available=true 的空池
    state = _state(tmp_path)
    snapshot = mw.load_pool(now=NOW, state_dir=state, allow_network=True)
    assert snapshot.available is False
    assert snapshot.error and "null" in snapshot.error
    assert not Path(mw.cache_path(state)).exists(), "失败结果不得写缓存"


def test_empty_list_and_null_differ_end_to_end(monkeypatch, tmp_path):
    """``[]`` → available=true / 无 warning；``null`` → available=false / 有 warning。"""
    # 两半各用独立状态目录：``[]`` 那次会写当日缓存，共用目录会让第二次读缓存，
    # 测不到 ``null`` 的取数路径。
    monkeypatch.setattr(mw, "_http_get", _Counter(payload=[]))
    mw.reset_cache()
    empty_block, empty_notes = mw.market_watch_evidence(
        HIT_CODE, now=NOW, state_dir=_state(tmp_path / "empty"), allow_network=True)
    assert empty_block["available"] is True
    assert empty_block["hit"] is None
    assert empty_notes == []

    monkeypatch.setattr(mw, "_http_get", _Counter(payload=None))
    mw.reset_cache()
    null_block, null_notes = mw.market_watch_evidence(
        HIT_CODE, now=NOW, state_dir=_state(tmp_path / "null"), allow_network=True)
    assert null_block["available"] is False
    assert null_block["hit"] is None
    assert any("监控池不可用" in note for note in null_notes)


@pytest.mark.parametrize("kwargs", [
    {"error": ConnectionError("boom")},
    {"status_code": 500},
    {"payload": ValueError("not json")},
    {"payload": {"unexpected": "object"}},
    {"payload": None},
])
def test_broken_endpoint_raises_not_empty(monkeypatch, kwargs):
    counter = _Counter(**kwargs)
    monkeypatch.setattr(mw, "_http_get", counter)
    mw.reset_cache()
    with pytest.raises(mw.MarketWatchError):
        mw.fetch_pool()


def test_load_pool_without_cache_reports_unavailable(monkeypatch, tmp_path):
    counter = _Counter(error=TimeoutError("timeout"))
    monkeypatch.setattr(mw, "_http_get", counter)
    mw.reset_cache()
    snapshot = mw.load_pool(now=NOW, state_dir=_state(tmp_path), allow_network=True)
    assert snapshot.available is False
    assert snapshot.stale is False
    assert snapshot.error and "TimeoutError" in snapshot.error


# ---------------------------------------------------------------------------
# 3) 缓存与请求次数
# ---------------------------------------------------------------------------
def test_one_request_serves_whole_pool(monkeypatch, tmp_path):
    """一次请求服务全池：10 只标的共 1 次 HTTP（不是 10 次）。"""
    counter = _Counter()
    monkeypatch.setattr(mw, "_http_get", counter)
    mw.reset_cache()

    codes = [HIT_CODE, MISS_CODE, BJ_CODE, "000002", "600003",
             "600004", "600005", "600006", "600007", "600008"]
    for code in codes:
        block, _notes = mw.market_watch_evidence(
            code, now=NOW, state_dir=_state(tmp_path), allow_network=True)
        assert set(block) == set(mw.MARKET_WATCH_KEYS)

    assert len(counter.calls) == 1, [c["url"] for c in counter.calls]
    assert counter.calls[0]["url"] == mw.MONITOR_URL
    assert counter.calls[0]["timeout"] <= mw.DEFAULT_TIMEOUT


def test_second_call_same_day_uses_cache(monkeypatch, tmp_path):
    counter = _Counter()
    monkeypatch.setattr(mw, "_http_get", counter)
    mw.reset_cache()
    state = _state(tmp_path)

    mw.load_pool(now=NOW, state_dir=state, allow_network=True)
    assert len(counter.calls) == 1

    mw.reset_cache()  # 清进程内 memo，强制走磁盘缓存
    snapshot = mw.load_pool(now=NOW, state_dir=state, allow_network=True)
    assert len(counter.calls) == 1, "当日缓存有效时不得再发请求"
    assert snapshot.stale is False
    assert snapshot.available is True


def test_cache_file_shape(monkeypatch, tmp_path):
    counter = _Counter()
    monkeypatch.setattr(mw, "_http_get", counter)
    mw.reset_cache()
    state = _state(tmp_path)
    mw.load_pool(now=NOW, state_dir=state, allow_network=True)

    payload = json.loads(Path(mw.cache_path(state)).read_text(encoding="utf-8"))
    assert payload["schema"] == mw.CACHE_SCHEMA
    assert payload["source"] == mw.SOURCE
    assert payload["fetched_at"]
    assert payload["session_date"] == "2026-11-16"
    assert payload["count"] == len(payload["entries"]) == 3
    assert {e["code"] for e in payload["entries"]} == {HIT_CODE, BJ_CODE, "000002"}


def test_fetch_failure_falls_back_to_stale_cache(monkeypatch, tmp_path):
    """接口失败但有缓存 → 回退旧缓存 + ``stale=True``（不是「无监控」）。"""
    state = _state(tmp_path)
    mw.write_cache(list(mw.parse_pool(_pool_rows()).values()),
                   "2026-11-14T18:30:00", state_dir=state,
                   session_date="2026-11-14")

    counter = _Counter(error=ConnectionError("rst"))
    monkeypatch.setattr(mw, "_http_get", counter)
    mw.reset_cache()

    snapshot = mw.load_pool(now=NOW, state_dir=state, allow_network=True)
    assert len(counter.calls) == 1
    assert snapshot.available is True
    assert snapshot.stale is True
    assert snapshot.as_of == "2026-11-14"
    assert "回退" in (snapshot.error or "")

    block, notes = mw.market_watch_evidence(HIT_CODE, now=NOW, state_dir=state,
                                            allow_network=True)
    assert block["stale"] is True
    assert block["hit"] and block["hit"]["listed"] is True
    assert any("过期" in note for note in notes)


def test_corrupt_cache_is_treated_as_missing(monkeypatch, tmp_path):
    state = _state(tmp_path)
    path = Path(mw.cache_path(state))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json", encoding="utf-8")

    counter = _Counter(error=ConnectionError("rst"))
    monkeypatch.setattr(mw, "_http_get", counter)
    mw.reset_cache()

    snapshot = mw.load_pool(now=NOW, state_dir=state, allow_network=True)
    assert snapshot.available is False


@pytest.mark.parametrize("payload", [
    "{not json",                                              # JSON 坏
    "[]",                                                     # 顶层不是对象
    json.dumps({"schema": 99, "entries": []}),                # schema 不符
    json.dumps({"schema": 1, "entries": "nope"}),             # entries 非列表
    json.dumps({"schema": 1, "count": 1,
                "entries": [{"code": "600000"}]}),            # 条目缺日期
    json.dumps({"schema": 1, "count": 5, "entries": []}),     # count 不一致
    json.dumps({"schema": 1, "count": ["x"], "entries": []}),  # count 是 list
    json.dumps({"schema": 1, "count": "abc", "entries": []}),  # count 非数字字符串
    json.dumps({"schema": 1, "count": None, "entries": []}),   # count 是 null
])
def test_corrupt_cache_is_ignored_and_refetched(monkeypatch, tmp_path, payload):
    """坏缓存**不等于**请求失败：忽略坏缓存并重新请求一次（P3-1）。"""
    state = _state(tmp_path)
    path = Path(mw.cache_path(state))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(payload, encoding="utf-8")

    counter = _Counter()  # 接口本身正常
    monkeypatch.setattr(mw, "_http_get", counter)
    mw.reset_cache()

    snapshot = mw.load_pool(now=NOW, state_dir=state, allow_network=True)
    assert len(counter.calls) == 1, "坏缓存必须触发一次重新请求"
    assert snapshot.available is True
    assert snapshot.stale is False, "坏缓存不得被当作旧缓存回退"
    assert snapshot.cache_problem and snapshot.cache_problem.startswith("corrupt:")
    # 新数据已经覆盖坏缓存
    assert json.loads(path.read_text(encoding="utf-8"))["count"] == 3


def test_corrupt_cache_warning_says_cache_not_fetch_failure(monkeypatch, tmp_path):
    """缓存坏了 + 接口也失败：两条原因必须分开写，不能只报「请求失败」。"""
    state = _state(tmp_path)
    path = Path(mw.cache_path(state))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json", encoding="utf-8")

    counter = _Counter(error=ConnectionError("rst"))
    monkeypatch.setattr(mw, "_http_get", counter)
    mw.reset_cache()

    snapshot = mw.load_pool(now=NOW, state_dir=state, allow_network=True)
    assert snapshot.available is False
    assert len(counter.calls) == 1, "坏缓存必须触发一次重新请求（不是直接放弃）"

    block, notes = mw.market_watch_evidence(HIT_CODE, now=NOW, state_dir=state,
                                            allow_network=True)
    assert block["available"] is False
    corrupt_notes = [n for n in notes if "缓存损坏" in n]
    assert corrupt_notes, notes
    assert "已忽略坏缓存并重新请求" in corrupt_notes[0]
    # 不可用 warning 里同时保留真正的请求失败原因
    unavailable = [n for n in notes if "监控池不可用" in n]
    assert unavailable and "ConnectionError" in unavailable[0]


def test_missing_cache_has_no_cache_problem(monkeypatch, tmp_path):
    """冷启动（无缓存文件）不是「缓存损坏」，不得报缓存告警。"""
    counter = _Counter()
    monkeypatch.setattr(mw, "_http_get", counter)
    mw.reset_cache()

    snapshot = mw.load_pool(now=NOW, state_dir=_state(tmp_path), allow_network=True)
    assert snapshot.cache_problem is None
    block, notes = mw.market_watch_evidence(HIT_CODE, now=NOW,
                                            state_dir=_state(tmp_path),
                                            allow_network=True)
    assert not any("缓存损坏" in n for n in notes)


def test_cache_with_zero_count_is_still_available(monkeypatch, tmp_path):
    """合法空池（当日 ``[]``）写入的缓存 ``count=0``：回读必须仍是可用空池。

    ``_snapshot_from_cache`` 现在不再用 ``count == 0`` 判断可用性，这里锁住
    「合法空缓存不被误判为不可用」这一侧（P3-1 的改动没有反向破坏）。
    """
    state = _state(tmp_path)
    mw.write_cache([], "2026-11-16T09:00:00", state_dir=state,
                   session_date="2026-11-16")
    counter = _Counter()
    monkeypatch.setattr(mw, "_http_get", counter)
    mw.reset_cache()

    snapshot = mw.load_pool(now=NOW, state_dir=state, allow_network=True)
    assert counter.calls == [], "当日空池缓存有效时不得再发请求"
    assert snapshot.available is True
    assert snapshot.entries == {}
    assert snapshot.stale is False
    assert snapshot.cache_problem is None


def test_read_cache_checked_distinguishes_states(tmp_path):
    """``read_cache_checked`` 三态：可用 / 缺失 / 损坏。"""
    state = _state(tmp_path)
    payload, problem = mw.read_cache_checked(state)
    assert payload is None and problem == mw.CACHE_MISSING

    mw.write_cache(list(mw.parse_pool(_pool_rows()).values()),
                   "2026-11-16T09:00:00", state_dir=state, session_date="2026-11-16")
    payload, problem = mw.read_cache_checked(state)
    assert problem is None and payload["session_date"] == "2026-11-16"

    Path(mw.cache_path(state)).write_text("{not json", encoding="utf-8")
    payload, problem = mw.read_cache_checked(state)
    assert payload is None and problem.startswith("corrupt:")


# ---------------------------------------------------------------------------
# 4) 命中语义
# ---------------------------------------------------------------------------
def test_hit_block_only_exposes_contract_keys(monkeypatch, tmp_path):
    """命中对象只有 ``listed/start/end/reason``：名称/链接不进 CLI JSON。"""
    counter = _Counter()
    monkeypatch.setattr(mw, "_http_get", counter)
    mw.reset_cache()

    block, _ = mw.market_watch_evidence(HIT_CODE, now=NOW, state_dir=_state(tmp_path),
                                        allow_network=True)
    assert set(block) == set(mw.MARKET_WATCH_KEYS)
    assert set(block["hit"]) == set(mw.HIT_KEYS)
    assert block["hit"] == {"listed": True, "start": "2026-11-16",
                            "end": "2026-11-27", "reason": "monitor"}


def test_miss_returns_null_hit(monkeypatch, tmp_path):
    counter = _Counter()
    monkeypatch.setattr(mw, "_http_get", counter)
    mw.reset_cache()
    block, notes = mw.market_watch_evidence(MISS_CODE, now=NOW,
                                            state_dir=_state(tmp_path),
                                            allow_network=True)
    assert block["available"] is True
    assert block["hit"] is None
    assert notes == []


def test_window_filter_excludes_expired_entry(monkeypatch, tmp_path):
    """生效窗口外不报命中（``start <= today <= end``）。"""
    rows = [_row(HIT_CODE, start="2026-11-01", end="2026-11-10")]
    counter = _Counter(payload=rows)
    monkeypatch.setattr(mw, "_http_get", counter)
    mw.reset_cache()
    block, _ = mw.market_watch_evidence(HIT_CODE, now=NOW, state_dir=_state(tmp_path),
                                        allow_network=True)
    assert block["hit"] is None


def test_as_of_replay_does_not_apply_current_snapshot(monkeypatch, tmp_path):
    """``--as-of`` 回放不得把今天的名单套到过去（前视）。"""
    counter = _Counter()
    monkeypatch.setattr(mw, "_http_get", counter)
    mw.reset_cache()
    block, notes = mw.market_watch_evidence(HIT_CODE, now=NOW,
                                            state_dir=_state(tmp_path),
                                            allow_network=True,
                                            as_of="2026-06-01")
    assert block["hit"] is None
    assert any("回放" in note for note in notes)


def test_offline_without_cache_is_unavailable(monkeypatch, tmp_path):
    counter = _Counter()
    monkeypatch.setattr(mw, "_http_get", counter)
    mw.reset_cache()
    block, notes = mw.market_watch_evidence(HIT_CODE, now=NOW,
                                            state_dir=_state(tmp_path),
                                            allow_network=False)
    assert counter.calls == []
    assert block["available"] is False
    assert block["hit"] is None
    assert any("离线" in note for note in notes)


# ---------------------------------------------------------------------------
# 5) 渲染层
# ---------------------------------------------------------------------------
def _payload(hit=True, available=True, stale=False):
    return {
        "schema_version": "1.0",
        "request": {"code": HIT_CODE, "type": "stock"},
        "instrument": {"code": HIT_CODE, "name": "示例股份", "board": "sh_main",
                       "is_st": False, "is_suspended": False, "quote_stale": False,
                       "status_source": "offline"},
        "data": {"last_close": 10.0, "end": "2026-11-13", "start": "2020-01-01",
                 "expected_date": "2026-11-13", "lag_days": 0, "stale": False},
        "market_regime": {"label": "Bull", "confidence": 0.7},
        "recommendation": {
            "status": "ok", "action": "buy", "confidence": 0.72,
            "action_reason": ["触发买点"], "position_fraction": 0.15,
            "horizon_days": [20, 20], "review_date": "2026-12-11",
            "entry_zone": {"lower": 9.8, "upper": 10.2},
            "stop_loss": 9.0, "take_profit": 12.0,
        },
        "evidence": {
            "triggers": [],
            "flow": {"available": False},
            "valuation": {"available": False},
            "market_watch": {
                "available": available,
                "stale": stale,
                "as_of": "2026-11-16",
                "hit": ({"listed": True, "start": "2026-11-16",
                         "end": "2026-11-27", "reason": "monitor"}
                        if hit else None),
            },
        },
        "diagnostics": {"warnings": []},
        "disclaimer": "本结果不构成投资建议，市场有风险，投资需谨慎。",
    }


def test_render_hit_line_present():
    output = render_advice(_payload())
    assert f"{MARKET_WATCH_MARK}（2026-11-16~2026-11-27）" in output


def test_render_hit_line_position():
    """命中行在可执行性闸门之后、价格/建议行之前。"""
    payload = _payload()
    payload["instrument"]["is_suspended"] = True
    output = render_advice(payload)
    lines = output.splitlines()
    gate = next(i for i, line in enumerate(lines)
                if line.startswith("【今日不建议执行】"))
    hit = next(i for i, line in enumerate(lines) if MARKET_WATCH_MARK in line)
    price = next(i for i, line in enumerate(lines) if HIT_CODE in line)
    assert gate < hit < price


def test_render_hit_line_disappears_when_missed():
    assert MARKET_WATCH_MARK not in render_advice(_payload(hit=False))


def test_render_hit_line_disappears_when_unavailable():
    """不可用时不产生风险结论（由 warnings 说明），整行消失。"""
    assert MARKET_WATCH_MARK not in render_advice(_payload(hit=False, available=False))


def test_render_hit_line_disappears_when_stale_without_hit():
    assert MARKET_WATCH_MARK not in render_advice(
        _payload(hit=False, available=True, stale=True))


def test_render_hit_line_present_when_stale_with_hit():
    """``stale=true`` 但有命中：照常出行，日期取该条目的生效窗口（P2-2）。

    实现口径（docstring 已按实现改正）：``stale`` 只说明名单来自旧缓存，
    不改变「是否命中」这一事实；命中是风险提示，宁可多报。
    """
    output = render_advice(_payload(hit=True, available=True, stale=True))
    assert f"{MARKET_WATCH_MARK}（2026-11-16~2026-11-27）" in output


def test_render_stale_hit_line_equals_fresh_hit_line():
    """stale 与否只影响 JSON 的 ``stale`` 键，渲染行逐字节一致。"""
    fresh = render_advice(_payload(hit=True, stale=False))
    stale = render_advice(_payload(hit=True, stale=True))
    assert fresh == stale


def test_render_does_not_rewrite_action_or_confidence():
    """T6 只加一行事实，绝不改写 action / confidence。"""
    with_hit = render_advice(_payload())
    without = render_advice(_payload(hit=False))
    assert "动作 buy｜置信 72%" in with_hit
    assert "动作 buy｜置信 72%" in without
    stripped = [line for line in with_hit.splitlines()
                if MARKET_WATCH_MARK not in line]
    assert stripped == without.splitlines()


def test_render_missing_block_is_noop():
    payload = _payload()
    payload["evidence"].pop("market_watch")
    assert MARKET_WATCH_MARK not in render_advice(payload)


def test_render_does_not_touch_network(monkeypatch):
    def _boom(*args, **kwargs):
        raise AssertionError("渲染阶段不得发起任何网络请求")

    monkeypatch.setattr(socket, "socket", _boom)
    monkeypatch.setattr(socket, "create_connection", _boom)
    assert MARKET_WATCH_MARK in render_advice(_payload())


# ---------------------------------------------------------------------------
# 6) CLI 接线
# ---------------------------------------------------------------------------
def _patch_cli_deps(monkeypatch, state_dir):
    """把 CLI 的重活（行情 / 回测 / 状态 / 名称）全部打桩，只留真实接线。

    ``monkeypatch.setattr(cli, "last_fetch_info", ...)`` 等必须打在 ``cli`` 的
    **同名绑定**上（``cli.py`` 是 ``from ... import last_fetch_info``），否则
    真实函数仍会触网。
    """
    import cli
    import stock_analysis.data_loader.meta as meta
    from stock_analysis.backtest.engine import BacktestResult
    from stock_analysis.data_loader.status import TradingStatus

    df = pd.DataFrame(
        {"open": [1.0] * 300, "high": [1.0] * 300, "low": [1.0] * 300,
         "close": [10.0 + i * 0.01 for i in range(300)], "volume": [1e6] * 300},
        index=pd.bdate_range("2020-01-01", periods=300),
    )
    monkeypatch.setattr(meta, "resolve_and_fetch", lambda *a, **k: (df, "stock", None))
    monkeypatch.setattr(cli, "last_fetch_info", lambda: {
        "source": "cache", "from_cache": True, "degraded": False,
        "cache_last_date": "2026-11-13", "lag_days": 0,
        "expected_date": "2026-11-13", "requested_source": "cache",
        "source_attempts": [],
    })
    monkeypatch.setattr(cli, "get_stock_name", lambda c: "示例股份")
    monkeypatch.setattr(cli, "resolve_trading_status", lambda *a, **k: TradingStatus(
        is_st=False, is_suspended=False, status_source="offline", warnings=[]))

    names = list(cli.build_strategy_signals().keys())[:3]
    comparison = pd.DataFrame(
        [{"评分": 1.0 - i * 0.1, "评分IS": 1.0 - i * 0.1, "评分OOS": 0.9 - i * 0.1,
          "年化收益": "10.00%", "最大回撤": "5.00%", "胜率": "60.00%", "交易次数": 20}
         for i, _ in enumerate(names)],
        index=names,
    )
    result = BacktestResult(trade_count=20, win_rate=0.6, annual_return=0.1,
                            max_drawdown=0.05, unaffordable_lot_events=0)
    result.equity_curve = [100000.0] * 50
    monkeypatch.setattr(cli, "run_strategy_tournament",
                        lambda *a, **k: (names[0], result, comparison))
    # CLI 内部不带 now：把「今天」钉到 NOW（北京 2026-11-16），并指向临时状态目录。
    monkeypatch.setattr(mw, "cn_today", lambda now=None: NOW.date().isoformat())
    monkeypatch.setenv("ASTOCK_MARKET_WATCH_OFFLINE", "0")
    monkeypatch.setenv(mw.STATE_DIR_ENV, state_dir)
    mw.reset_cache()
    return cli


def _run_cli(monkeypatch, state_dir, code=HIT_CODE):
    cli = _patch_cli_deps(monkeypatch, state_dir)
    return cli.advise_internal(code, fast=True, no_regime=True)


def _run_main(monkeypatch, capsys, state_dir, code=HIT_CODE):
    """真跑 ``cli.main()``：覆盖 main 的 try/except 与 ``--json`` 输出/退出码。

    直接调用 ``advise_internal`` 测不到顶层异常处理 —— 而 P3 缺陷正是
    ``int(count)`` 的 TypeError/ValueError 漏到 main，变成 exit 30 + 空 stdout。
    """
    cli = _patch_cli_deps(monkeypatch, state_dir)
    monkeypatch.setattr(sys, "argv",
                        ["cli.py", code, "--json", "--fast", "--no-regime"])
    with pytest.raises(SystemExit) as exc:
        cli.main()
    captured = capsys.readouterr()
    return exc.value.code, captured


def test_cli_adds_market_watch_block(monkeypatch, tmp_path):
    counter = _Counter()
    monkeypatch.setattr(mw, "_http_get", counter)
    mw.reset_cache()
    out = _run_cli(monkeypatch, _state(tmp_path))

    block = out["evidence"]["market_watch"]
    assert set(block) == set(mw.MARKET_WATCH_KEYS)
    assert block["available"] is True
    assert block["hit"] == {"listed": True, "start": "2026-11-16",
                            "end": "2026-11-27", "reason": "monitor"}
    # 一次请求服务全池
    assert len(counter.calls) == 1


def test_cli_warns_when_unavailable(monkeypatch, tmp_path):
    counter = _Counter(error=ConnectionError("rst"))
    monkeypatch.setattr(mw, "_http_get", counter)
    mw.reset_cache()
    out = _run_cli(monkeypatch, _state(tmp_path))
    block = out["evidence"]["market_watch"]
    assert block["available"] is False
    assert block["hit"] is None
    assert any("监控池不可用" in w for w in out["diagnostics"]["warnings"])


def test_cli_warns_when_stale(monkeypatch, tmp_path):
    state = _state(tmp_path)
    mw.write_cache(list(mw.parse_pool(_pool_rows()).values()),
                   "2026-11-14T18:30:00", state_dir=state, session_date="2026-11-14")
    counter = _Counter(error=ConnectionError("rst"))
    monkeypatch.setattr(mw, "_http_get", counter)
    mw.reset_cache()
    out = _run_cli(monkeypatch, _state(tmp_path))
    block = out["evidence"]["market_watch"]
    assert block["stale"] is True
    assert block["available"] is True
    assert any("监控池缓存过期" in w for w in out["diagnostics"]["warnings"])


def test_cli_does_not_change_recommendation(monkeypatch, tmp_path):
    """A/B：命中 vs 未命中，``recommendation`` 逐键一致。"""
    counter = _Counter()
    monkeypatch.setattr(mw, "_http_get", counter)
    mw.reset_cache()
    out_hit = _run_cli(monkeypatch, _state(tmp_path / "a"), code=HIT_CODE)
    out_miss = _run_cli(monkeypatch, _state(tmp_path / "b"), code=MISS_CODE)

    assert out_hit["recommendation"] == out_miss["recommendation"]
    assert out_hit["evidence"]["market_watch"]["hit"] is not None
    assert out_miss["evidence"]["market_watch"]["hit"] is None


def test_error_response_market_watch_isomorphic():
    """G35：错误路径的 ``evidence.market_watch`` 键与成功路径一致。"""
    import cli

    doc = cli.create_error_response("data_source_error", "失败", HIT_CODE, "stock")
    block = doc["evidence"]["market_watch"]
    assert set(block) == set(mw.MARKET_WATCH_KEYS)
    assert block == {"available": False, "stale": False, "as_of": None, "hit": None}


@pytest.mark.parametrize("bad_count", [["x"], "abc", None])
def test_cli_json_survives_non_integer_cache_count(monkeypatch, capsys, tmp_path,
                                                   bad_count):
    """``count`` 是 list / 字符串 / null 时 CLI 必须正常出 JSON（P3）。

    修复前 ``read_cache_checked`` 直接 ``int(count)``：list 抛 TypeError、
    非数字字符串抛 ValueError，一路漏到 ``cli.main`` 的兜底 -> exit 30，
    且 ``--json`` 的 stdout **完全为空**。现在坏缓存按「缓存损坏」忽略并
    重取一次，CLI 正常返回完整文档。
    """
    state = _state(tmp_path)
    path = Path(mw.cache_path(state))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"schema": 1, "count": bad_count, "entries": []}),
                    encoding="utf-8")

    counter = _Counter()  # 接口本身正常
    monkeypatch.setattr(mw, "_http_get", counter)
    mw.reset_cache()

    code, captured = _run_main(monkeypatch, capsys, state)

    assert code != 30, captured.err
    assert code in (0, 10, 20, 40), (code, captured.err)
    assert captured.err == "", "CLI 不得把未捕获异常写成「错误：…」"
    document = json.loads(captured.out)  # stdout 必须是完整 JSON 文档
    assert set(document) >= {
        "schema_version", "request", "instrument", "recommendation",
        "evidence", "diagnostics",
    }
    assert set(document["evidence"]["market_watch"]) == set(mw.MARKET_WATCH_KEYS)
    assert len(counter.calls) == 1, "坏 count 必须被忽略并重取一次"
    warnings = document["diagnostics"]["warnings"]
    corrupt = [w for w in warnings if "缓存损坏" in w]
    assert corrupt, warnings
    assert "count" in corrupt[0], "损坏原因必须带字段名 count"


# ---------------------------------------------------------------------------
# 7) 端到端：人为注入命中 → 渲染输出（证明链路不是死代码）
# ---------------------------------------------------------------------------
def test_end_to_end_injected_hit_reaches_rendered_text(monkeypatch, tmp_path):
    counter = _Counter()
    monkeypatch.setattr(mw, "_http_get", counter)
    mw.reset_cache()

    payload = _run_cli(monkeypatch, _state(tmp_path))
    text = render_advice(copy.deepcopy(payload))

    assert MARKET_WATCH_MARK in text
    assert "2026-11-16~2026-11-27" in text
    assert "None" not in text
    assert "免责声明：" in text
