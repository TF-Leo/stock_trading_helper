# -*- coding: utf-8 -*-
"""PR #131 P2：全域封锁升级规则的三条收紧（离线测试）。

背景（复验发现的两个生产风险）
------------------------------
1. ``zero_byte_fin``（ConnectionReset / SSLEOF / 空 body 这类**瞬时断连**，线上
   冷却文件当天就有实例）与 ``http_444`` **同权投票**：两个域各抖一次就写
   30–60 分钟全域封锁，把本来可用的第三、第四个域一起关掉；
2. 升级判定复用 30–60 分钟的**冷却窗口**，半小时前的一次失败与刚才的一次失败
   被算成同一轮；
3. 升级后写的是顶层块，而 ``--clear-cooldown`` 只删本域、``precompute_flow``
   根本没有该开关 —— 一旦误升级只能手删文件。

本文件锁住：

* 两个域各一次 ``zero_byte_fin`` → **不**升级；
* 两个域各一次 ``http_444``（投票窗口内）→ 升级；
* 一次 ``http_444`` 在窗口外 + 一次在窗口内 → 不升级；
* ``--clear-cooldown --all`` 清除顶层块，之后各域恢复请求（三个预计算脚本
  都支持）；
* 升级留痕：``surfaces_blocked`` 带原因与时间戳，升级日志同时写 stderr 与
  日志文件。

所有 HTTP 入口都打桩，不触网；标的只用 600519 / 600000。
"""
from __future__ import annotations

import datetime as dt
import json
import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1]
TOOLS = SRC.parent / "tools"
for _path in (SRC, TOOLS):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from stock_analysis.data_sources import _astock_generated as _g  # noqa: E402
from stock_analysis.data_sources import eastmoney_guard as em  # noqa: E402

import precompute_events as pe  # noqa: E402
import precompute_flow as flow_tool  # noqa: E402
import precompute_st_list as st_tool  # noqa: E402

NOW = dt.datetime(2026, 11, 16, 18, 35)
NOW_TS = NOW.timestamp()
PUSH2 = em.PUSH2_SURFACE
DATACENTER = em.DATACENTER_SURFACE
MONITOR = em.MONITOR_SURFACE


class _Response:
    def __init__(self, payload=None, status_code=200):
        self._payload = payload if payload is not None else {}
        self.status_code = status_code
        self.content = b"" if status_code == 444 else b"{}"

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


@pytest.fixture(autouse=True)
def _fast_em(monkeypatch):
    monkeypatch.setattr(_g, "_em_last_call", [0.0])
    monkeypatch.setattr(_g.time, "sleep", lambda seconds: None)


def _raw() -> dict:
    return json.loads(Path(em.state_path()).read_text(encoding="utf-8"))


def _patch_session(monkeypatch, handler):
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


# ---------------------------------------------------------------------------
# 1) zero_byte_fin 不投票：两个域各抖一次不得升级
# ---------------------------------------------------------------------------
def test_two_domains_zero_byte_fin_do_not_escalate():
    """瞬时断连（ConnectionReset / SSLEOF）在两个域各出现一次 → 不写顶层块。"""
    em.mark_cooldown("zero_byte_fin", "push2his 连接被重置", seconds=1800,
                     now=NOW, surface="push2his.eastmoney.com")
    written = em.mark_cooldown("zero_byte_fin", "datacenter SSLEOF",
                               seconds=1800, now=NOW, surface=DATACENTER)

    assert written["escalated_shared"] is False
    raw = _raw()
    assert set(raw[em.SURFACES_KEY]) == {PUSH2, DATACENTER}
    assert "blocked_until" not in raw, "zero_byte_fin 不得升级为全域封锁"
    assert em.cooldown_state(now=NOW) is None, "顶层共享封锁必须保持干净"
    # 本域冷却仍然生效（瞬时断连也要停本域，避免继续撞墙）。
    assert em.scoped_cooldown_state(
        now=NOW, surface=DATACENTER)["reason"] == "zero_byte_fin"


def test_http_444_plus_zero_byte_fin_do_not_escalate():
    """一域 444 + 另一域瞬时断连：只有一张 444 票 → 不升级。"""
    em.mark_cooldown("zero_byte_fin", "push2his 连接被重置", seconds=1800,
                     now=NOW, surface=PUSH2)
    written = em.mark_cooldown("http_444", "datacenter 444", seconds=1800,
                               now=NOW, surface=DATACENTER)

    assert written["escalated_shared"] is False
    assert "blocked_until" not in _raw()


def test_escalation_requires_current_hit_to_be_http_444():
    """已有一张 444 票，但本次命中是瞬时断连 → 本次不升级（瞬时断连不投票）。"""
    em.mark_cooldown("http_444", "push2his 444", seconds=1800, now=NOW,
                     surface=PUSH2)
    written = em.mark_cooldown("zero_byte_fin", "datacenter SSLEOF",
                               seconds=1800, now=NOW, surface=DATACENTER)

    assert written["escalated_shared"] is False
    assert "blocked_until" not in _raw()


# ---------------------------------------------------------------------------
# 2) 两个域各一次 http_444（投票窗口内）→ 升级
# ---------------------------------------------------------------------------
def test_two_domains_http_444_within_window_escalate(capsys):
    em.mark_cooldown("http_444", "push2his 444", seconds=1800, now=NOW,
                     surface="push2his.eastmoney.com")
    written = em.mark_cooldown("http_444", "datacenter 444", seconds=1800,
                               now=NOW + dt.timedelta(seconds=120),
                               surface=DATACENTER)

    assert written["escalated_shared"] is True
    raw = _raw()
    assert "blocked_until" in raw, "两个域 444 → 写顶层全域封锁"
    assert set(raw["surfaces_blocked_names"]) == {PUSH2, DATACENTER}

    # P3-1：``surfaces_blocked`` 每条带触发原因 + 时间戳。
    assert raw["escalation_reason"] == "http_444"
    assert raw["escalation_window_seconds"] == int(em.escalation_window())
    assert "escalated_at" in raw
    evidence = {item["surface"]: item for item in raw["surfaces_blocked"]}
    assert set(evidence) == {PUSH2, DATACENTER}
    assert all(item["reason"] == "http_444" for item in evidence.values())
    assert all(item["updated_at"] and item["blocked_at"]
               for item in evidence.values())

    # P3-2：stderr 醒目提示 + 日志文件。
    err = capsys.readouterr().err
    assert "[ESCALATED]" in err and "--clear-cooldown --all" in err
    log_path = Path(em.state_path()).parent / "eastmoney_escalation.log"
    assert log_path.exists()
    assert "--clear-cooldown --all" in log_path.read_text(encoding="utf-8")

    # 第三个域（监控池）读到顶层块 → 0 请求。
    guard = em.EmFetchGuard(surface=MONITOR, now_fn=lambda: NOW_TS + 180)
    assert guard.cooling()["scope"] == "shared"


def test_transient_reset_does_not_erase_an_earlier_444_vote():
    """同域先 444、后瞬时断连：后者的 ``reason`` 覆盖前者，但 444 票仍在。"""
    em.mark_cooldown("http_444", "push2his 444", seconds=1800, now=NOW,
                     surface=PUSH2)
    em.mark_cooldown("zero_byte_fin", "push2his 连接被重置", seconds=1800,
                     now=NOW + dt.timedelta(seconds=30), surface=PUSH2)
    raw = _raw()
    assert raw[em.SURFACES_KEY][PUSH2]["reason"] == "zero_byte_fin"
    assert raw[em.SURFACES_KEY][PUSH2]["last_http_444_at"], "444 票必须留痕"

    written = em.mark_cooldown("http_444", "datacenter 444", seconds=1800,
                               now=NOW + dt.timedelta(seconds=60),
                               surface=DATACENTER)
    assert written["escalated_shared"] is True, "窗口内的 444 票仍然有效"


def test_escalation_window_is_configurable(monkeypatch):
    monkeypatch.setenv(em.ESCALATION_WINDOW_ENV, "120")
    assert em.escalation_window() == 120.0
    em.mark_cooldown("http_444", "push2his 444", seconds=1800, now=NOW,
                     surface=PUSH2)
    written = em.mark_cooldown("http_444", "datacenter 444", seconds=1800,
                               now=NOW + dt.timedelta(seconds=130),
                               surface=DATACENTER)
    assert written["escalated_shared"] is False, "超出 120s 窗口 → 不算同一轮"


# ---------------------------------------------------------------------------
# 3) 窗口外的一次 444 + 窗口内的一次 → 不升级
# ---------------------------------------------------------------------------
def test_444_outside_window_plus_inside_does_not_escalate():
    window = em.escalation_window()
    em.mark_cooldown("http_444", "半小时前 push2his 444", seconds=1800,
                     now=NOW, surface=PUSH2)
    # 仍在冷却期内（1800s），但命中时间已早于投票窗口（600s）。
    later = NOW + dt.timedelta(seconds=window + 60)
    written = em.mark_cooldown("http_444", "刚才 datacenter 444", seconds=1800,
                               now=later, surface=DATACENTER)

    assert written["escalated_shared"] is False
    raw = _raw()
    assert "blocked_until" not in raw, "跨轮次的孤立 444 不得合并成全域封锁"
    # 两个本域条目都还在冷却期内 —— 只是不投票。
    assert em.scoped_cooldown_state(
        now=later, surface=PUSH2)["remaining_seconds"] > 0
    assert em.scoped_cooldown_state(
        now=later, surface=DATACENTER)["remaining_seconds"] > 0


# ---------------------------------------------------------------------------
# 4) --clear-cooldown --all 清除顶层块，之后各域恢复请求
# ---------------------------------------------------------------------------
def _escalate_now(tmp_path, monkeypatch):
    """造一个「刚被误升级」的冷却文件（顶层块 + 两个域条目）。"""
    monkeypatch.setenv(em.STATE_PATH_ENV, str(tmp_path / "cool.json"))
    em.mark_cooldown("http_444", "push2his 444", seconds=1800,
                     now=dt.datetime.now(), surface=PUSH2)
    em.mark_cooldown("http_444", "datacenter 444", seconds=1800,
                     now=dt.datetime.now(), surface=DATACENTER)
    assert "blocked_until" in _raw()


def test_clear_all_clears_shared_block_and_surfaces_resume(tmp_path,
                                                           monkeypatch):
    _escalate_now(tmp_path, monkeypatch)

    # 升级期间：事件任务 0 请求。
    blocked_calls = _patch_session(
        monkeypatch, lambda url, params: pytest.fail(f"冷却中不得请求：{url}"))
    stats = pe.precompute(["600519"], out_dir=str(tmp_path / "events"),
                          retries=0, sleep_fn=lambda _s: None)
    assert stats["skipped_cooldown"] is True and blocked_calls == []

    assert pe.main(["--clear-cooldown", "--all"]) == pe.EXIT_OK
    raw = _raw()
    assert "blocked_until" not in raw, "顶层全域封锁必须被清除"
    assert DATACENTER not in raw.get(em.SURFACES_KEY, {}), "本域条目一并清除"
    assert em.cooldown_state() is None

    # 之后各域恢复请求：datacenter 事件任务照常发满 4 探针 + 4 端点。
    calls = _patch_session(monkeypatch, _datacenter_ok)
    stats = pe.precompute(["600519"], out_dir=str(tmp_path / "events"),
                          retries=0, sleep_fn=lambda _s: None)
    assert stats["skipped_cooldown"] is False
    assert len(calls) == 4 + 4


def test_clear_all_keeps_other_surface_entries(tmp_path, monkeypatch):
    """``--all`` 只清顶层块 + 本域条目，其它域的**独立**封锁不被误清。"""
    _escalate_now(tmp_path, monkeypatch)
    # 模拟一次新写下的、与本次误升级无关的第三域封锁（重写顶层块前先清掉）。
    em.clear_cooldown(include_shared=True)
    em.mark_cooldown("http_444", "监控池 444", seconds=1800,
                     now=dt.datetime.now(), surface=MONITOR)

    assert pe.main(["--clear-cooldown", "--all"]) == pe.EXIT_OK
    raw = _raw()
    assert MONITOR in raw[em.SURFACES_KEY], "其它域的封锁不得被误清"
    assert "blocked_until" not in raw


def test_flow_and_st_clear_all_remove_shared_block(tmp_path, monkeypatch):
    """各预计算脚本统一支持 ``--clear-cooldown --all``（P2-3 的可操作性）。"""
    _escalate_now(tmp_path, monkeypatch)

    assert flow_tool.main(["--clear-cooldown", "--all"]) == 0
    assert "blocked_until" not in _raw()

    # 再造一次，验证 ST 名单脚本。
    em.mark_cooldown("http_444", "push2 444", seconds=1800,
                     now=dt.datetime.now(), surface=PUSH2)
    em.mark_cooldown("http_444", "datacenter 444", seconds=1800,
                     now=dt.datetime.now(), surface=DATACENTER)
    assert "blocked_until" in _raw()
    assert st_tool.main(["--clear-cooldown", "--all"]) == st_tool.EXIT_OK
    assert "blocked_until" not in _raw()


def test_flow_without_clear_flag_still_cools(tmp_path, monkeypatch):
    """``--clear-cooldown`` 不带 ``--all`` 时只清本域（旧行为不回退）。"""
    _escalate_now(tmp_path, monkeypatch)
    assert flow_tool.main(["--clear-cooldown"]) == 0
    raw = _raw()
    assert "blocked_until" in raw, "不带 --all 不得清除顶层全域封锁"
    assert PUSH2 not in raw[em.SURFACES_KEY]
    assert DATACENTER in raw[em.SURFACES_KEY]
