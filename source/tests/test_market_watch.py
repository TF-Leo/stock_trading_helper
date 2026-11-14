# -*- coding: utf-8 -*-
"""T6（issue #1 / R11 缺口表 #6）：全市场重点监控池 —— 离线测试。

覆盖：
* 解析：命中 / 未命中 / **北交所 ``MARKET="B"`` → ``BJ`` 而非 ``SZ``** /
  未知市场值原样带出 / 字段不合法抛错（绝不静默跳过）；
* 空结果与「接口坏了」分开：``[]`` 合法，非 2xx / 非 JSON / 非列表抛
  :class:`MarketWatchError`；
* 缓存：当日有效 → **请求次数 1**；第二次 0 次；接口失败回退旧缓存并
  ``stale=True``；失败且无缓存 → ``available=False``；
* CLI：``evidence.market_watch`` additive、键同构（G35 错误路径）、
  ``diagnostics.warnings`` 在不可用/过期时给原因、A/B 不改 ``recommendation``；
* 渲染：命中行位置（闸门之后、建议行之前）、未命中/不可用/过期整行消失、
  不改写 ``action`` / ``confidence``；
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


class _FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


class _Counter:
    """统计 ``em_get`` 调用次数（T6 的核心断言：一次请求服务全池）。"""

    def __init__(self, payload=None, error=None, status_code=200):
        self.calls = []
        self.payload = payload if payload is not None else _pool_rows()
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
def test_empty_list_is_a_legal_empty_pool(monkeypatch):
    counter = _Counter(payload=[])
    monkeypatch.setattr(mw, "_http_get", counter)
    mw.reset_cache()
    snapshot = mw.load_pool(now=NOW, state_dir=None, allow_network=True)
    assert snapshot.available is True
    assert snapshot.entries == {}
    assert snapshot.error is None


@pytest.mark.parametrize("kwargs", [
    {"error": ConnectionError("boom")},
    {"status_code": 500},
    {"payload": ValueError("not json")},
    {"payload": {"unexpected": "object"}},
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
def _run_cli(monkeypatch, state_dir, code=HIT_CODE):
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
    return cli.advise_internal(code, fast=True, no_regime=True)


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
