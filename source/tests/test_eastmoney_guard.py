# -*- coding: utf-8 -*-
"""issue #108：东财 push2 家族 444 反爬的熔断与请求预算（离线测试）。

覆盖：
* HTTP 444 / 零字节 FIN / 正常响应三态识别；
* 命中即写**持久冷却**（含到期时间与原因），冷却期内 0 请求；
* 冷却到期自动放行一次探测；
* 单域名单轮请求预算超限在发请求之前抛出；
* 资金流与 ST 名单共用同一套冷却文件（不再各自维护一份）。

所有 HTTP 入口都打桩，不触网。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest
import requests

SRC = Path(__file__).resolve().parents[1]
for _path in (SRC, SRC.parent / "tools"):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from stock_analysis.data_loader import status  # noqa: E402
from stock_analysis.data_sources import _astock_generated as _g  # noqa: E402
from stock_analysis.data_sources import eastmoney_guard as em  # noqa: E402

URL = "https://push2.eastmoney.com/api/qt/clist/get"


@pytest.fixture(autouse=True)
def _fast_em(monkeypatch):
    """跳过 em_get 的 1s 串行节流，测试只关心请求次数。"""
    monkeypatch.setattr(_g, "_em_last_call", [time.time()])
    monkeypatch.setattr(_g.time, "sleep", lambda seconds: None)


class _Response:
    def __init__(self, status_code=200, content=b"{}"):
        self.status_code = status_code
        self.content = content


def _patch_get(monkeypatch, handler):
    calls = []

    def fake_get(url, params=None, headers=None, timeout=15, **kwargs):
        calls.append(url)
        return handler(url)

    monkeypatch.setattr(_g.EM_SESSION, "get", fake_get)
    return calls


# ---------------------------------------------------------------------------
# 1) 三态识别
# ---------------------------------------------------------------------------
def test_http_444_blocks_after_one_request_and_writes_cooldown(monkeypatch):
    calls = _patch_get(monkeypatch, lambda url: _Response(444, b""))

    guard = em.EmFetchGuard()
    with pytest.raises(em.AntiScrapeBlocked) as excinfo:
        with guard:
            _g.em_get(URL)
    assert "http_444" in str(excinfo.value)
    assert guard.requests == 1
    assert guard.block_reason == "http_444"
    assert calls == [URL], "444 后不得换域/重试"

    state = em.cooldown_state()
    assert state is not None
    assert state["reason"] == "http_444"
    assert 0 < state["remaining_seconds"] <= em.cooldown_seconds()
    payload = json.loads(Path(em.state_path()).read_text(encoding="utf-8"))
    assert payload["schema"] == 1
    assert payload["blocked_until"] > payload["blocked_at"]


def test_zero_byte_fin_response_is_treated_as_block(monkeypatch):
    calls = _patch_get(monkeypatch, lambda url: _Response(200, b""))

    guard = em.EmFetchGuard()
    with pytest.raises(em.AntiScrapeBlocked):
        with guard:
            _g.em_get(URL)
    assert guard.requests == 1
    assert guard.block_reason == "zero_byte_fin"
    assert em.cooldown_state()["reason"] == "zero_byte_fin"


def test_connection_closed_before_response_is_fin(monkeypatch):
    def boom(url):
        raise requests.exceptions.ConnectionError(
            "('Connection aborted.', RemoteDisconnected("
            "'Remote end closed connection without response'))")

    calls = _patch_get(monkeypatch, boom)
    guard = em.EmFetchGuard()
    with pytest.raises(em.AntiScrapeBlocked):
        with guard:
            _g.em_get(URL)
    assert guard.requests == 1 and calls == [URL]
    assert guard.block_reason == "zero_byte_fin"


def test_normal_response_passes_and_writes_no_cooldown(monkeypatch):
    calls = _patch_get(monkeypatch, lambda url: _Response(200, b'{"rc":0}'))

    guard = em.EmFetchGuard()
    with guard:
        response = _g.em_get(URL)
    assert response.status_code == 200
    assert guard.requests == 1 and calls == [URL]
    assert guard.block_reason is None
    assert em.cooldown_state() is None


# ---------------------------------------------------------------------------
# 2) 冷却：期内 0 请求；到期自动放行一次
# ---------------------------------------------------------------------------
def test_cooling_period_sends_zero_requests(monkeypatch):
    calls = _patch_get(monkeypatch, lambda url: _Response())
    em.mark_cooldown("http_444", "上一轮封禁", seconds=60, now=1000.0)

    guard = em.EmFetchGuard(now_fn=lambda: 1000.0)
    with pytest.raises(em.EastmoneyCooling):
        with guard:
            _g.em_get(URL)
    assert calls == [], "冷却期内一个请求都不发"
    assert guard.requests == 0


def test_cooling_expiry_allows_exactly_one_probe(monkeypatch):
    calls = _patch_get(monkeypatch, lambda url: _Response(200, b'{"rc":0}'))
    em.mark_cooldown("http_444", "旧封禁", seconds=60, now=1000.0)

    guard = em.EmFetchGuard(now_fn=lambda: 1061.0)
    with guard:
        _g.em_get(URL)
    assert calls == [URL]
    assert guard.requests == 1


def test_mark_cooldown_never_shortens_a_longer_block(monkeypatch, tmp_path):
    path = str(tmp_path / "cool.json")
    em.mark_cooldown("http_444", "长冷却", seconds=60, now=100.0, path=path)
    # 100s 时写下的长冷却到 160s；110s 时的短冷却只能到 120s —— 不得缩短。
    second = em.mark_cooldown("zero_byte_fin", "短冷却", seconds=10,
                              now=110.0, path=path)
    assert second["blocked_until"] == em._iso(160.0)
    assert second["reason"] == "zero_byte_fin"


# ---------------------------------------------------------------------------
# 3) 预算：超限在发请求之前抛出
# ---------------------------------------------------------------------------
def test_request_budget_blocks_third_request_before_sending(monkeypatch):
    calls = _patch_get(monkeypatch, lambda url: _Response(200, b'{"rc":0}'))

    guard = em.EmFetchGuard(budget=2)
    with guard:
        _g.em_get(URL)
        _g.em_get(URL)
        with pytest.raises(em.EastmoneyBudgetExceeded) as excinfo:
            _g.em_get(URL)
    assert "预算超限" in str(excinfo.value)
    assert calls == [URL, URL]
    assert guard.requests == 2
    assert guard.budget.exceeded is True
    assert guard.budget.counts["push2.eastmoney.com"] == 2


def test_budget_is_per_domain(monkeypatch):
    calls = _patch_get(monkeypatch, lambda url: _Response(200, b'{"rc":0}'))
    guard = em.EmFetchGuard(budget=1)
    with guard:
        _g.em_get("https://push2.eastmoney.com/a")
        _g.em_get("https://push2his.eastmoney.com/b")
        with pytest.raises(em.EastmoneyBudgetExceeded):
            _g.em_get("https://push2.eastmoney.com/c")
    assert len(calls) == 2
    assert guard.budget.counts == {
        "push2.eastmoney.com": 1,
        "push2his.eastmoney.com": 1,
    }


# ---------------------------------------------------------------------------
# 4) 与 ST 名单共用同一份冷却文件
# ---------------------------------------------------------------------------
def test_st_and_flow_share_one_cooldown_file(monkeypatch):
    em.mark_cooldown("http_444", "资金流命中", seconds=60, now=1000.0)

    assert Path(status.cooldown_path()) == Path(em.state_path())
    seen = status.cooldown_state(now=__import__("datetime").datetime.fromtimestamp(1000.0))
    assert seen["reason"] == "http_444"
    assert "remaining_seconds" in seen


# ---------------------------------------------------------------------------
# 5) 跨进程：进程 A 熔断写盘，进程 B 冷启动后 0 请求
# ---------------------------------------------------------------------------
_CHILD_SCRIPT = r"""
import json
import time
from stock_analysis.data_sources import _astock_generated as _g
from stock_analysis.data_sources import eastmoney_guard as em

calls = []

class Blocked:
    status_code = 444
    content = b""

def fake_get(url, params=None, headers=None, timeout=15, **kwargs):
    calls.append(url)
    return Blocked()

_g.EM_SESSION.get = fake_get
_g._em_last_call = [time.time()]
_g.time.sleep = lambda seconds: None
outcome = None
try:
    with em.EmFetchGuard():
        _g.em_get("https://push2.eastmoney.com/api/qt/clist/get")
except Exception as exc:
    outcome = type(exc).__name__
print(json.dumps({"calls": len(calls), "outcome": outcome}))
"""


def _run_child(path: str) -> dict:
    env = dict(os.environ, PYTHONPATH=str(SRC),
               EASTMONEY_COOLDOWN_PATH=path,
               ASTOCK_CALENDAR_OFFLINE="1",
               ASTOCK_STATUS_OFFLINE="1",
               ASTOCK_MARKET_WATCH_OFFLINE="1")
    result = subprocess.run([sys.executable, "-c", _CHILD_SCRIPT],
                            cwd=str(SRC), env=env, capture_output=True,
                            text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout.strip().splitlines()[-1])


def test_cross_process_cooldown_blocks_second_process(tmp_path):
    path = tmp_path / "cross" / "eastmoney_cooldown.json"
    first = _run_child(str(path))
    assert first == {"calls": 1, "outcome": "AntiScrapeBlocked"}, first
    assert path.exists(), "冷却必须落盘，进程退出后仍然有效"

    second = _run_child(str(path))
    assert second == {"calls": 0, "outcome": "EastmoneyCooling"}, second