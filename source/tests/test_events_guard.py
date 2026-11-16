# -*- coding: utf-8 -*-
"""事件预计算接入东财统一熔断与请求预算（2026-11-16，issue #1 敞口收口）。

背景：``tools/precompute_events.py`` 此前绕过 ``em_get`` 的统一闸门 —— 即使
``datacenter-web`` 已处于 444 冷却，它仍会照常发 14×4+4 次请求继续撞墙。

本文件覆盖任务要求的四条：
1. 冷却中 → **0 请求**、退出码明确（3）、已有好缓存**不被覆盖**；
2. 请求预算超限 → 在**发请求之前**拦截（退出码 4、不写缓存）；
3. 正常路径请求数与改动前一致（14 只 × 4 + 4 探针 = 60）；
4. ``--probes-only`` 受同一闸门约束；冷却键按域隔离，一个域被封不误伤另一个域。

所有网络入口都打桩（只替换 ``EM_SESSION.get``，保留真实 ``em_get`` 与闸门），
不触网；测试只用公开代码 600519 / 600000。
"""
from __future__ import annotations

import datetime as dt
import json
import sys
import time
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1]
for _path in (SRC, SRC.parent / "tools"):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from stock_analysis.data_loader import events as ev  # noqa: E402
from stock_analysis.data_sources import _astock_generated as _g  # noqa: E402
from stock_analysis.data_sources import eastmoney_guard as em  # noqa: E402

import precompute_events as pe  # noqa: E402

REF_DAY = "2026-11-13"
NOW = dt.datetime(2026, 11, 13, 18, 35)
DATACENTER_HOST = "datacenter-web.eastmoney.com"
PUSH2_URL = "https://push2his.eastmoney.com/api/qt/stock/fflow/daykline/get"


# ---------------------------------------------------------------------------
# 桩：只替换 HTTP 层，``em_get`` / ``EmFetchGuard`` / 预算 / 冷却全部走真代码
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


@pytest.fixture(autouse=True)
def _fast_em(monkeypatch):
    """跳过 em_get 的 1s 串行节流：测试只关心请求次数。"""
    monkeypatch.setattr(_g, "_em_last_call", [time.time()])
    monkeypatch.setattr(_g.time, "sleep", lambda seconds: None)


def _datacenter_ok(url, params=None, **kwargs):
    """正常路径：探针返回 1 行；带个股条件的请求返回 9201（合法的空结果）。"""
    report_filter = str((params or {}).get("filter") or "")
    if "SECURITY_CODE=" in report_filter:
        return _Response({"code": 9201})
    return _Response({"code": 0, "result": {
        "pages": 5, "count": 100,
        "data": [{"SECURITY_CODE": "600519", "FREE_DATE": "2026-11-18"}]}})


def _patch_session(monkeypatch, handler):
    calls = []

    def fake_get(url, params=None, headers=None, timeout=15, **kwargs):
        calls.append(url)
        return handler(url, params)

    monkeypatch.setattr(_g.EM_SESSION, "get", fake_get)
    return calls


def _boom(url, params=None):
    raise AssertionError(f"本轮不得发请求：{url}")


def _seed_cache(out_dir: Path, payload=None):
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "events.json"
    payload = payload or {
        "schema": ev.EVENTS_SCHEMA,
        "source": ev.SOURCE,
        "upstream": ev.UPSTREAM,
        "fetched_at": REF_DAY + "T18:35:00",
        "stale": False,
        "600519": {
            key: {"available": True, "stale": False, "as_of": REF_DAY,
                  "fetched_at": REF_DAY + "T18:35:00", "reason": None,
                  "error": None, "items": [{"date": "2026-11-18"}],
                  "warnings": []}
            for key in ev.SECTION_KEYS
        },
    }
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# 1) 冷却中：0 请求、退出码 3、缓存不被覆盖
# ---------------------------------------------------------------------------
def test_cooling_makes_zero_requests_and_keeps_cache(tmp_path, monkeypatch,
                                                     capsys):
    calls = _patch_session(monkeypatch, _boom)
    out_dir = tmp_path / "events"
    cache = _seed_cache(out_dir)
    before = cache.read_bytes()

    em.mark_cooldown("http_444", "封禁窗口", seconds=600, now=NOW,
                     surface=pe.SURFACE)
    stats = pe.precompute(["600519"], out_dir=str(out_dir), retries=0,
                          sleep_fn=lambda _s: None, now_fn=lambda: NOW)

    assert calls == [], "冷却期内一个请求都不发"
    assert stats["skipped_cooldown"] is True
    assert stats["requests"] == 0 and stats["requests_sent"] == 0
    assert stats["path"] is None, "跳过时不得声称写了缓存"
    assert cache.read_bytes() == before, "已有好缓存必须原样保留"
    assert "SKIP" in capsys.readouterr().err


def test_cooling_skip_does_not_create_cache_dir(tmp_path, monkeypatch):
    """无旧缓存时，冷却跳过也不得创建目录 / 写半截缓存。"""
    calls = _patch_session(monkeypatch, _boom)
    out_dir = tmp_path / "fresh"
    em.mark_cooldown("http_444", "封禁窗口", seconds=600, now=NOW,
                     surface=pe.SURFACE)

    stats = pe.precompute(["600519"], out_dir=str(out_dir), retries=0,
                          sleep_fn=lambda _s: None, now_fn=lambda: NOW)

    assert stats["skipped_cooldown"] is True
    assert calls == []
    assert not out_dir.exists(), "跳过时不得创建缓存目录"


def test_main_exit_code_3_while_cooling(tmp_path, monkeypatch, capsys):
    """``main`` 走真实时钟：冷却按**当前**时间写，避免受固定 NOW 影响。"""
    calls = _patch_session(monkeypatch, _boom)
    out_dir = tmp_path / "events"
    cache = _seed_cache(out_dir)
    before = cache.read_bytes()
    em.mark_cooldown("http_444", "封禁窗口", seconds=600, surface=pe.SURFACE)

    code = pe.main(["--codes", "600519", "--out-dir", str(out_dir),
                    "--retries", "0"])

    assert code == pe.EXIT_COOLDOWN == 3
    assert calls == []
    assert cache.read_bytes() == before
    captured = capsys.readouterr()
    assert "SKIP" in captured.err
    assert "未写缓存" in captured.err
    assert captured.err.count("\n") == 1, "冷却跳过只写一行日志"


def test_shared_cooldown_also_blocks_events(tmp_path, monkeypatch):
    """顶层共享封锁（资金流 / ST 名单写入）同样让事件预计算 0 请求。"""
    calls = _patch_session(monkeypatch, _boom)
    em.mark_cooldown("http_444", "整域封锁", seconds=600, now=NOW)

    stats = pe.precompute(["600519"], out_dir=str(tmp_path / "events"),
                          retries=0, sleep_fn=lambda _s: None,
                          now_fn=lambda: NOW)
    assert stats["skipped_cooldown"] is True
    assert calls == []


# ---------------------------------------------------------------------------
# 2) 444 熔断：命中即中止、写**域级**冷却、不写缓存
# ---------------------------------------------------------------------------
def test_444_aborts_after_first_request_and_writes_scoped_cooldown(
        tmp_path, monkeypatch, capsys):
    calls = _patch_session(monkeypatch, lambda url, params: _Response({}, 444))
    out_dir = tmp_path / "events"
    cache = _seed_cache(out_dir)
    before = cache.read_bytes()

    stats = pe.precompute(["600519", "600000"], out_dir=str(out_dir),
                          retries=1, sleep_fn=lambda _s: None,
                          now_fn=lambda: NOW)

    assert len(calls) == 1, "命中 444 后不得再发任何请求（含重试 / 其余票）"
    assert stats["blocked"] is True
    assert stats["block_reason"] == "http_444"
    assert stats["path"] is None
    assert cache.read_bytes() == before, "熔断轮不得覆盖好缓存"
    assert "BLOCKED" in capsys.readouterr().err

    raw = json.loads(Path(em.state_path()).read_text(encoding="utf-8"))
    assert em.SURFACES_KEY in raw
    assert DATACENTER_HOST in raw[em.SURFACES_KEY]
    assert "blocked_until" not in raw, "域级封锁不得升级成整域共享封锁"
    # 另一个风控面（push2）不受影响。
    assert em.scoped_cooldown_state(
        now=NOW, surface="push2his.eastmoney.com") is None


# ---------------------------------------------------------------------------
# 3) 请求预算：发请求之前拦截，不写缓存
# ---------------------------------------------------------------------------
def test_budget_blocks_before_sending(tmp_path, monkeypatch, capsys):
    calls = _patch_session(monkeypatch, _datacenter_ok)
    out_dir = tmp_path / "events"
    cache = _seed_cache(out_dir)
    before = cache.read_bytes()

    stats = pe.precompute(["600519", "600000"], out_dir=str(out_dir),
                          retries=0, sleep_fn=lambda _s: None,
                          now_fn=lambda: NOW, budget=3)

    assert len(calls) == 3, "超限请求必须在发出之前被拦截"
    assert stats["budget_exceeded"] is True
    assert stats["requests_sent"] == 3
    assert stats["path"] is None
    assert cache.read_bytes() == before
    limits = stats["request_limits"]
    assert limits["counts"][DATACENTER_HOST] == 3
    assert limits["limit_per_domain"] == 3
    assert "BUDGET" in capsys.readouterr().err


def test_main_exit_code_4_on_budget(tmp_path, monkeypatch):
    calls = _patch_session(monkeypatch, _datacenter_ok)
    code = pe.main(["--codes", "600519,600000", "--out-dir",
                    str(tmp_path / "events"), "--retries", "0",
                    "--budget", "2"])
    assert code == pe.EXIT_BUDGET == 4
    assert len(calls) == 2
    assert not (tmp_path / "events" / "events.json").exists()


def test_default_budget_is_80():
    assert pe.DEFAULT_BUDGET == 80


def test_budget_env_override_and_cli_precedence(monkeypatch):
    monkeypatch.delenv(em.REQUEST_BUDGET_ENV, raising=False)
    assert pe._budget_limit(None) == 80
    monkeypatch.setenv(em.REQUEST_BUDGET_ENV, "123")
    assert pe._budget_limit(None) == 123
    assert pe._budget_limit(7) == 7, "--budget 优先于环境变量"
    monkeypatch.setenv(em.REQUEST_BUDGET_ENV, "not-a-number")
    assert pe._budget_limit(None) == 80


# ---------------------------------------------------------------------------
# 4) 正常路径：请求数与改动前一致（14 × 4 + 4 探针 = 60）
# ---------------------------------------------------------------------------
def test_normal_path_request_count_unchanged(tmp_path, monkeypatch):
    calls = _patch_session(monkeypatch, _datacenter_ok)
    codes = list(pe.DEFAULT_CODES)
    assert len(set(codes)) == 14

    stats = pe.precompute(codes, out_dir=str(tmp_path / "events"),
                          retries=0, sleep_fn=lambda _s: None,
                          now_fn=lambda: NOW)

    assert len(calls) == 14 * 4 + 4 == 60
    assert stats["requests_sent"] == 60
    assert stats["requests"] == 60
    assert stats["ok_sections"] == 56
    assert stats["failed_sections"] == 0
    assert stats["budget_exceeded"] is False
    assert stats["skipped_cooldown"] is False
    assert Path(stats["path"]).exists()


# ---------------------------------------------------------------------------
# 5) --probes-only 受同一闸门约束
# ---------------------------------------------------------------------------
def test_probes_only_respects_cooldown(monkeypatch, capsys):
    calls = _patch_session(monkeypatch, _boom)
    em.mark_cooldown("zero_byte_fin", "封禁窗口", seconds=600, now=NOW,
                     surface=pe.SURFACE)

    stats = pe.run_probes(as_of=REF_DAY, now_fn=lambda: NOW,
                          sleep_fn=lambda _s: None)
    assert stats["skipped_cooldown"] is True
    assert stats["requests_sent"] == 0
    assert calls == []


def test_probes_only_respects_budget(monkeypatch):
    calls = _patch_session(monkeypatch, _datacenter_ok)
    stats = pe.run_probes(as_of=REF_DAY, now_fn=lambda: NOW,
                          sleep_fn=lambda _s: None, budget=2)
    assert len(calls) == 2
    assert stats["budget_exceeded"] is True


def test_probes_only_exit_code_on_budget(monkeypatch):
    _patch_session(monkeypatch, _datacenter_ok)
    assert pe.main(["--probes-only", "--budget", "1"]) == pe.EXIT_BUDGET


# ---------------------------------------------------------------------------
# 6) 冷却键设计：按域隔离，互不误伤；--clear-cooldown 只清本域
# ---------------------------------------------------------------------------
def test_push2_surface_cooldown_does_not_block_events(tmp_path, monkeypatch):
    calls = _patch_session(monkeypatch, _datacenter_ok)
    em.mark_cooldown("http_444", "push2 被封", seconds=600, now=NOW,
                     surface="push2his.eastmoney.com")

    stats = pe.precompute(["600519"], out_dir=str(tmp_path / "events"),
                          retries=0, sleep_fn=lambda _s: None,
                          now_fn=lambda: NOW)
    assert stats["skipped_cooldown"] is False
    assert len(calls) == 4 + 4
    # 反过来：push2 自己的闸门仍然认为在冷却中。
    guard = em.EmFetchGuard(surface="push2his.eastmoney.com",
                            now_fn=lambda: NOW.timestamp())
    assert guard.cooling()["scope"] == "surface"


def test_events_cooldown_does_not_block_push2(tmp_path, monkeypatch):
    em.mark_cooldown("http_444", "datacenter 被封", seconds=600, now=NOW,
                     surface=pe.SURFACE)
    guard = em.EmFetchGuard(surface="push2his.eastmoney.com",
                            now_fn=lambda: NOW.timestamp())
    assert guard.cooling() is None
    assert em.cooldown_state(now=NOW) is None, "顶层共享封锁未被污染"


def test_clear_cooldown_only_clears_own_surface(tmp_path, monkeypatch):
    monkeypatch.setenv(em.STATE_PATH_ENV, str(tmp_path / "cool.json"))
    em.mark_cooldown("http_444", "整域", seconds=600, now=NOW)
    em.mark_cooldown("http_444", "datacenter", seconds=600, now=NOW,
                     surface=pe.SURFACE)

    assert pe.main(["--clear-cooldown"]) == pe.EXIT_OK

    assert em.cooldown_state(now=NOW)["reason"] == "http_444"
    assert em.scoped_cooldown_state(now=NOW, surface=pe.SURFACE)["scope"] == "shared"


def test_clear_cooldown_surface_keeps_other_surfaces(tmp_path, monkeypatch):
    monkeypatch.setenv(em.STATE_PATH_ENV, str(tmp_path / "cool.json"))
    em.mark_cooldown("http_444", "datacenter", seconds=600, now=NOW,
                     surface=pe.SURFACE)
    em.mark_cooldown("http_444", "push2", seconds=600, now=NOW,
                     surface="push2his.eastmoney.com")

    pe.main(["--clear-cooldown"])

    raw = json.loads((tmp_path / "cool.json").read_text(encoding="utf-8"))
    assert DATACENTER_HOST not in raw[em.SURFACES_KEY]
    assert "push2his.eastmoney.com" in raw[em.SURFACES_KEY]


# ---------------------------------------------------------------------------
# 7) --estimate 仍然不触网
# ---------------------------------------------------------------------------
def test_estimate_makes_no_requests(monkeypatch, capsys):
    calls = _patch_session(monkeypatch, _boom)
    assert pe.main(["--estimate"]) == pe.EXIT_OK
    assert calls == []
