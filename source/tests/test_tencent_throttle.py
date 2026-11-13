# -*- coding: utf-8 -*-
"""issue #13（P0）：腾讯限流 —— 跨进程冷却 / 请求预算 / 快速切备胎 / 耗时上限。

事故背景（``docs/P1_EVIDENCE_V2.md`` §2.2 / §7）：R5 抓 300 只日线时真实触发
腾讯整域限流 —— 第 47 只后三入口同时失败、全轮 22 次冷却、32 只失败，最后靠
baostock 补齐 57 只。根因是进程内冷却字典不跨进程，且「每票都重试三入口」
把一次限流放大三倍。

本文件全部离线（打桩 HTTP，不触网），覆盖：
  1. 冷却状态文件：原子写、过期剔除、损坏容错、并发合并；
  2. **跨进程**：进程 A 触发冷却 → 进程 B 启动后不再请求已冷却入口；
  3. 退避次数符合预算（整域冷却时一个请求都不发）；
  4. 连续失败后本轮切备胎；冷却过期后恢复请求；
  5. 单票耗时上限触发 degraded（渠道池层 + 批量抓取层）。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pandas as pd
import pytest

SRC = Path(__file__).resolve().parents[1]
ROOT = SRC.parent
TOOLS = ROOT / "tools"
for path in (SRC, TOOLS):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from stock_analysis.data_sources import astock, tencent_throttle as throttle  # noqa: E402
from stock_analysis.data_sources import _astock_generated as generated  # noqa: E402
from stock_analysis.data_loader import bars  # noqa: E402

HOSTS = list(generated.TENCENT_KLINE_HOSTS)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
class FakeResponse:
    def __init__(self, text="", status=200, url="https://example.invalid/x"):
        self.text = text
        self.status_code = status
        self.url = url

    def json(self):
        return json.loads(self.text)

    def raise_for_status(self):
        if self.status_code >= 400:
            import requests
            raise requests.HTTPError(f"{self.status_code} error", response=self)


def _toy(n=30, start="2020-01-01", base=100.0):
    index = pd.bdate_range(start, periods=n, name="date")
    close = [base + i for i in range(n)]
    return pd.DataFrame({
        "open": close, "high": [c + 1 for c in close],
        "low": [c - 1 for c in close], "close": close,
        "volume": [1000.0 + i for i in range(n)],
    }, index=index)


def _kline_payload(symbol="sh600519", days=3, start="2020-01-02"):
    rows = []
    cursor = pd.Timestamp(start)
    for i in range(days):
        day = (cursor + pd.Timedelta(days=i)).strftime("%Y-%m-%d")
        rows.append([day, "100", "101", "102", "99", "1000"])
    return {"code": 0, "msg": "", "data": {symbol: {"day": rows}}}


def _fake_kline_http(calls, payload=None, status=200):
    """按请求窗口过滤假响应（``tencent_kline`` 会分页并校验返回日期在窗口内）。"""
    base = payload or _kline_payload()

    def fake_http(url, params=None, data=None, headers=None, method="GET",
                  timeout=None, **kwargs):
        calls.append(url)
        body = json.loads(json.dumps(base))
        parts = str((params or {}).get("param", "")).split(",")
        if len(parts) >= 4:
            symbol, start, end = parts[0], parts[2], parts[3]
            node = (body.get("data") or {}).get(symbol)
            if isinstance(node, dict):
                for key, rows in list(node.items()):
                    if isinstance(rows, list) and rows and isinstance(rows[0], list):
                        node[key] = [row for row in rows
                                     if (not start or row[0] >= start)
                                     and (not end or row[0] <= end)]
        return FakeResponse(json.dumps(body), status=status)

    return fake_http


@pytest.fixture
def clean_throttle(monkeypatch, tmp_path):
    """每个用例独立冷却状态文件 + 清空进程内字典。"""
    monkeypatch.setenv(throttle.STATE_PATH_ENV,
                       str(tmp_path / "state" / "tencent_cooldown.json"))
    generated._tencent_host_down_until.clear()
    yield tmp_path / "state" / "tencent_cooldown.json"
    generated._tencent_host_down_until.clear()


# ---------------------------------------------------------------------------
# 1) 状态文件本身
# ---------------------------------------------------------------------------
def test_state_file_written_atomically_and_readable(clean_throttle):
    path = clean_throttle
    throttle.mark_hosts([HOSTS[0]], seconds=60, reason="test")
    assert path.exists()
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["schema_version"] == throttle.SCHEMA_VERSION
    assert HOSTS[0] in payload["hosts"]
    assert payload["last_reason"] == "test"
    assert payload["pid"] == os.getpid()
    # 不留 tmp 残片（原子替换的前提）
    assert not [p for p in path.parent.iterdir() if p.name.endswith(".tmp")]
    assert throttle.active_hosts()[HOSTS[0]] > time.time()


def test_expired_entries_are_dropped(clean_throttle):
    path = clean_throttle
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "schema_version": 1,
        "hosts": {HOSTS[0]: time.time() - 1, HOSTS[1]: time.time() + 600},
    }), encoding="utf-8")
    active = throttle.load_state()
    assert list(active["hosts"]) == [HOSTS[1]]
    assert throttle.active_hosts() == {HOSTS[1]: pytest.approx(
        json.loads(path.read_text())["hosts"][HOSTS[1]])}


def test_corrupt_state_is_tolerated(clean_throttle):
    path = clean_throttle
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json", encoding="utf-8")
    assert throttle.load_state()["hosts"] == {}
    assert throttle.active_hosts() == {}
    # 损坏文件不能让取数崩：mark 仍能写回合法状态
    throttle.mark_hosts([HOSTS[0]], seconds=30)
    assert HOSTS[0] in throttle.active_hosts()


def test_concurrent_mark_merges_instead_of_overwriting(clean_throttle):
    """两个「进程」各自冷却不同入口：后写的不得覆盖先写的。"""
    throttle.mark_hosts([HOSTS[0]], seconds=60)
    throttle.mark_hosts([HOSTS[1]], seconds=60)
    active = throttle.active_hosts()
    assert HOSTS[0] in active and HOSTS[1] in active


# ---------------------------------------------------------------------------
# 2) 跨进程：进程 A 触发冷却 → 进程 B 不请求已冷却入口
# ---------------------------------------------------------------------------
_CHILD_SCRIPT = r"""
import json, sys
sys.path.insert(0, {src!r})
from stock_analysis.data_sources import astock, _astock_generated as g

calls = []
def fake_http(url, params=None, data=None, headers=None, method='GET',
              timeout=None, **kwargs):
    calls.append(url)
    raise RuntimeError('boom: 429 Too Many Requests')

g._v39_http = fake_http
mode = sys.argv[1]
try:
    astock.fetch_daily('600519', start_date='20200101', adjust='qfq')
    outcome = 'ok'
except Exception as exc:
    outcome = type(exc).__name__
print(json.dumps({{'mode': mode, 'calls': calls, 'outcome': outcome,
                   'down': sorted(g._tencent_host_down_until)}}))
"""


def _run_child(mode, state_path, timeout=90):
    script = _CHILD_SCRIPT.format(src=str(SRC))
    env = dict(os.environ, PYTHONPATH=str(SRC),
               TENCENT_COOLDOWN_PATH=str(state_path),
               ASTOCK_CALENDAR_OFFLINE="1")
    env.pop("ASTOCK_FORCE_FAIL", None)
    result = subprocess.run([sys.executable, "-c", script, mode],
                            cwd=str(SRC), env=env, capture_output=True,
                            text=True, timeout=timeout)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout.strip().splitlines()[-1])


def test_cross_process_cooldown_blocks_second_process(clean_throttle, tmp_path):
    """验收 3：进程 A 触发冷却后，进程 B 启动时**一个请求都不发**。"""
    state = tmp_path / "cross" / "tencent_cooldown.json"
    first = _run_child("A", state)
    # 进程 A：三个入口各被调用一次（都 429），并把冷却写进磁盘
    assert len(first["calls"]) == len(HOSTS), first
    assert first["outcome"] == "AstockError"
    payload = json.loads(state.read_text(encoding="utf-8"))
    assert set(payload["hosts"]) == set(HOSTS)
    assert all(until > time.time() for until in payload["hosts"].values())

    # 进程 B：全新解释器，进程内字典为空，只能靠磁盘状态
    second = _run_child("B", state)
    assert second["calls"] == [], second
    assert second["outcome"] == "TencentThrottled"


def test_cross_process_cooldown_expiry_restores_requests(clean_throttle, tmp_path):
    """冷却过期后恢复请求（磁盘里的过期条目不得永久封锁入口）。"""
    state = tmp_path / "cross" / "tencent_cooldown.json"
    state.parent.mkdir(parents=True, exist_ok=True)
    state.write_text(json.dumps({
        "schema_version": 1,
        "hosts": {host: time.time() - 5 for host in HOSTS},
        "last_reason": "expired",
    }), encoding="utf-8")
    result = _run_child("B", state)
    assert len(result["calls"]) == len(HOSTS), result


# ---------------------------------------------------------------------------
# 3) 退避次数符合预算
# ---------------------------------------------------------------------------
def test_all_hosts_cooling_short_circuits_without_requests(clean_throttle, monkeypatch):
    calls = []

    def fake_http(url, **kwargs):
        calls.append(url)
        return FakeResponse(json.dumps(_kline_payload()))

    monkeypatch.setattr(generated, "_v39_http", fake_http)
    throttle.mark_hosts(HOSTS, seconds=300)
    with pytest.raises(astock.TencentThrottled) as excinfo:
        astock.fetch_daily("600519", start_date="20200101", adjust="qfq")
    assert calls == []
    assert "冷却" in str(excinfo.value)
    # 同一次运行里第二次调用也不发请求（进程内字典已同步）
    with pytest.raises(astock.TencentThrottled):
        astock.fetch_daily("600519", start_date="20200101", adjust="qfq")
    assert calls == []


def test_partial_cooldown_only_skips_cooling_host(clean_throttle, monkeypatch):
    """只有一个入口冷却时，其余入口照常请求（不能因为一个入口挂掉就整域停摆）。"""
    calls = []
    monkeypatch.setattr(generated, "_v39_http", _fake_kline_http(calls))
    throttle.mark_hosts([HOSTS[0]], seconds=300)
    astock.fetch_daily("600519", start_date="20200101", adjust="")
    assert calls and all(HOSTS[0] not in url for url in calls), calls
    assert calls[0].startswith(HOSTS[1])


def test_success_does_not_write_cooldown(clean_throttle, monkeypatch):
    calls = []
    monkeypatch.setattr(generated, "_v39_http", _fake_kline_http(calls))
    astock.fetch_daily("600519", start_date="20200101", adjust="")
    assert calls
    assert throttle.active_hosts() == {}
    assert throttle.load_state()["hosts"] == {}


# ---------------------------------------------------------------------------
# 4) 渠道池：连续失败 → 本轮切备胎；耗时上限 → degraded
# ---------------------------------------------------------------------------
def _pool(sources, cooldown=120, max_consecutive=99, max_total=99, budget=20.0):
    """测试用池：把「通道自身冷却/剔除」阈值调高，单独观察主源停用逻辑。

    真实默认值是 3/8；这里不设高的话，主源连续失败 3 次会先被
    ``_mark_failure`` 剔除，测到的就不是 issue #13 的「本轮优先备胎」了。
    """
    pool = bars._DataSourcePool(
        sources, cooldown_seconds=cooldown,
        max_consecutive_failures=max_consecutive,
        max_total_failures=max_total, failure_window_seconds=600)
    pool.ticker_budget_seconds = budget
    return pool


def test_primary_blocked_after_consecutive_failures_then_fallback_first():
    """连续 3 只主源失败后，第 4 只先试备胎（不是每票都先撞主源）。"""
    primary_calls = []

    def primary(*a, **k):
        primary_calls.append(1)
        # 非瞬态错误：不触发通道级冷却，保证主源一直「可用但失败」
        raise RuntimeError("empty payload from tencent")

    pool = _pool([("astock_tencent", primary), ("sina_stock", lambda *a, **k: _toy(5))])
    pool.primary_source_name = "astock_tencent"
    preferred = ["astock_tencent", "sina_stock"]
    # 前 3 只：主源先试（失败）→ 备胎成功
    for _ in range(3):
        df, used = pool.fetch("600519", "20200101", "qfq", preferred=preferred)
        assert used == "sina_stock" and len(df) == 5
        assert pool.last_attempts[0]["source"] == "astock_tencent"
    assert pool.primary_blocked_reason is not None
    assert len(primary_calls) == 3

    # 第 4 只：主源一次都不再被调用（即使 preferred 仍把它排在第一位）
    df, used = pool.fetch("600519", "20200101", "qfq", preferred=preferred)
    assert used == "sina_stock"
    assert len(primary_calls) == 3, "主源本轮应已停用"
    assert pool.last_attempts[0]["source"] == "sina_stock"

    # 新一轮（reset）后重新判定：主源又被排到前面
    pool.reset_primary_block()
    pool.fetch("600519", "20200101", "qfq", preferred=preferred)
    assert len(primary_calls) == 4


def test_primary_success_resets_consecutive_counter():
    outcomes = ["fail", "ok", "fail"]

    def flaky(*a, **k):
        if outcomes.pop(0) == "fail":
            raise RuntimeError("empty payload from tencent")
        return _toy(5)

    pool = _pool([("astock_tencent", flaky), ("sina_stock", lambda *a, **k: _toy(5))])
    pool.primary_source_name = "astock_tencent"
    # 固定优先顺序：否则渠道池的轮转游标会让第 2 次先试备胎，主源根本不参与
    preferred = ["astock_tencent", "sina_stock"]
    pool.fetch("600519", "20200101", "qfq", preferred=preferred)
    assert pool.primary_consecutive_failures == 1
    pool.fetch("600519", "20200101", "qfq", preferred=preferred)   # 主源这次成功
    assert pool.primary_consecutive_failures == 0
    pool.fetch("600519", "20200101", "qfq", preferred=preferred)
    assert pool.primary_consecutive_failures == 1
    assert pool.primary_blocked_reason is None


def test_ticker_budget_stops_remaining_channels(monkeypatch):
    """单票总耗时超预算：不再调用剩余通道，attempts 显式留 budget_exhausted。"""
    calls = []

    def slow(*a, **k):
        calls.append("slow")
        time.sleep(0.3)
        raise RuntimeError("429")

    def never(*a, **k):
        calls.append("never")
        return _toy(5)

    pool = _pool([("astock_tencent", slow), ("eastmoney", slow),
                  ("sina_stock", never)], budget=0.4)
    df, used = pool.fetch("600519", "20200101", "qfq")
    assert df is None and used is None
    assert calls == ["slow", "slow"], calls
    exhausted = [a for a in pool.last_attempts if a.get("budget_exhausted")]
    assert exhausted and exhausted[0]["source"] == "sina_stock"
    assert pool.budget_timeouts == 1
    assert any("预算" in line for line in bars.format_attempt_warnings(pool.last_attempts))


def test_ticker_budget_not_triggered_when_fast():
    pool = _pool([("astock_tencent", lambda *a, **k: _toy(5))], budget=20.0)
    df, used = pool.fetch("600519", "20200101", "qfq")
    assert used == "astock_tencent" and pool.budget_timeouts == 0
    assert pool.last_attempts == [{
        "source": "astock_tencent", "ok": True, "error_class": None,
        "error_text": None, "cooldown": False, "removed": False,
    }]


def test_budget_failure_falls_back_to_cache_and_marks_degraded(tmp_path, monkeypatch):
    """耗时上限触发后，meta 回退旧缓存并标 degraded（验收第 5 条）。"""
    from stock_analysis.data_loader import meta

    cache = tmp_path / "stock_600519_qfq.csv"
    stale = _toy(10, start="2020-01-01")
    stale.to_csv(cache, index_label="date")
    monkeypatch.setattr(meta, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(meta, "_expected_latest_trade_date",
                        lambda: pd.Timestamp("2026-11-13").date())

    def slow_fail(*a, **k):
        time.sleep(0.25)
        # 非瞬态错误：不触发通道冷却，确保走到预算判定而不是被冷却短路
        raise RuntimeError("empty payload from tencent")

    pool = _pool([("astock_tencent", slow_fail), ("eastmoney", slow_fail)], budget=0.3)
    monkeypatch.setattr(meta, "_STOCK_DATA_SOURCE_POOL", pool)

    df = meta.get_stock_daily("600519", start_date="20200101")
    info = meta.last_fetch_info()
    assert df is not None and len(df) > 0
    assert info["degraded"] is True
    assert info["source"] == "cache"
    assert info["degraded_reason"] and "回退" in info["degraded_reason"]
    assert pool.budget_timeouts >= 1


# ---------------------------------------------------------------------------
# 4b) issue #67 D2：单通道阻塞也受预算约束（墙钟 ≤ 预算 + 小容差）
# ---------------------------------------------------------------------------
def test_single_channel_hang_is_capped_by_budget():
    """预算 3s + 单通道阻塞 12s → 墙钟 ≤ 3s（修复前实测 12.01s，4 倍预算）。"""
    release = threading.Event()

    def hang(*a, **k):
        release.wait(12)
        return _toy(5)

    pool = _pool([("astock_tencent", hang)], budget=3.0)
    started = time.monotonic()
    try:
        df, used = pool.fetch("600519", "20200101", "qfq")
    finally:
        release.set()
    wall = time.monotonic() - started

    assert df is None and used is None
    assert wall <= 3.0 + 0.5, f"单通道阻塞吃满预算：墙钟 {wall:.2f}s > 3s"
    assert pool.budget_timeouts == 1
    timed_out = [a for a in pool.last_attempts
                 if a.get("error_class") == "TencentBudgetExceeded"]
    assert timed_out and timed_out[0]["source"] == "astock_tencent"
    # 超预算不判通道故障（可能是被前面通道「饿死」），所以不冷却、不剔除。
    assert timed_out[0]["cooldown"] is False and timed_out[0]["removed"] is False


def test_hung_channel_cannot_block_remaining_channels():
    """单通道挂死时，剩余通道的预算判定照旧：一个都不许再调用。"""
    release = threading.Event()
    calls = []

    def hang(*a, **k):
        calls.append("hang")
        release.wait(12)
        return _toy(5)

    def backup(*a, **k):
        calls.append("backup")
        return _toy(5)

    pool = _pool([("astock_tencent", hang), ("eastmoney", backup)], budget=3.0)
    started = time.monotonic()
    try:
        df, used = pool.fetch("600519", "20200101", "qfq")
    finally:
        release.set()
    wall = time.monotonic() - started

    assert df is None and used is None
    assert calls == ["hang"], calls
    assert wall <= 3.0 + 0.5
    assert pool.budget_timeouts == 1


def test_hung_channel_falls_back_to_cache_within_budget(tmp_path, monkeypatch):
    """生产路径（meta）：单通道挂死 + 预算 3s → 墙钟受限、回退缓存、degraded。"""
    from stock_analysis.data_loader import meta

    cache = tmp_path / "stock_600519_qfq.csv"
    stale = _toy(10, start="2020-01-01")
    stale.to_csv(cache, index_label="date")
    monkeypatch.setattr(meta, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(meta, "_expected_latest_trade_date",
                        lambda: pd.Timestamp("2026-11-13").date())

    release = threading.Event()

    def hang(*a, **k):
        release.wait(12)
        raise RuntimeError("挂死通道不应走到这里")

    pool = _pool([("astock_tencent", hang)], budget=3.0)
    monkeypatch.setattr(meta, "_STOCK_DATA_SOURCE_POOL", pool)

    started = time.monotonic()
    try:
        df = meta.get_stock_daily("600519", start_date="20200101")
    finally:
        release.set()
    wall = time.monotonic() - started
    info = meta.last_fetch_info()

    assert df is not None and len(df) > 0
    assert wall <= 3.0 + 0.5, f"生产路径墙钟 {wall:.2f}s > 3s 预算"
    assert info["degraded"] is True
    assert info["source"] == "cache"
    assert info["degraded_reason"] and "回退" in info["degraded_reason"]
    assert pool.budget_timeouts == 1


def test_http_timeout_for_budget_keeps_default_when_roomy():
    """预算充足时不动默认 (5, 15)：不削弱正常取数的容错。"""
    assert throttle.http_timeout_for_budget(20.0) == (5.0, 15.0)
    assert throttle.http_timeout_for_budget(600.0) == (5.0, 15.0)


def test_http_timeout_for_budget_fits_inside_budget():
    """预算收紧时 connect + read ≤ 剩余预算（避免「预算 3s、单请求 20s」错配）。"""
    for remaining in (3.0, 1.0, 0.3, 0.05):
        connect, read = throttle.http_timeout_for_budget(remaining)
        assert connect + read <= remaining + 1e-9, (remaining, connect, read)
        assert connect > 0 and read > 0
    assert throttle.http_timeout_for_budget(3.0) == (1.0, 2.0)


def test_fetch_daily_clamps_http_timeout_to_budget(monkeypatch, clean_throttle):
    """``fetch_daily(budget=…)`` 在取数窗口内收紧 HTTP 超时，退出后还原默认值。"""
    seen = {}
    default = generated._ASTOCK_HTTP_TIMEOUT

    def fake_tencent_kline(*args, **kwargs):
        seen["timeout"] = generated._ASTOCK_HTTP_TIMEOUT
        days = pd.bdate_range("2020-01-01", periods=3)
        return pd.DataFrame({
            "date": days.strftime("%Y-%m-%d"),
            "open": [1.0, 1.0, 1.0], "high": [2.0, 2.0, 2.0],
            "low": [0.5, 0.5, 0.5], "close": [1.5, 1.5, 1.5],
            "volume": [100.0, 100.0, 100.0],
        })

    monkeypatch.setattr(generated, "tencent_kline", fake_tencent_kline)
    frame = astock.fetch_daily("600519", start_date="20200101", adjust="",
                               budget=3.0)

    assert not frame.empty
    assert seen["timeout"] == (1.0, 2.0)
    assert generated._ASTOCK_HTTP_TIMEOUT == default, "取数结束后必须还原默认超时"


def test_fetch_daily_without_budget_keeps_default_http_timeout(monkeypatch,
                                                              clean_throttle):
    seen = {}

    def fake_tencent_kline(*args, **kwargs):
        seen["timeout"] = generated._ASTOCK_HTTP_TIMEOUT
        days = pd.bdate_range("2020-01-01", periods=3)
        return pd.DataFrame({
            "date": days.strftime("%Y-%m-%d"),
            "open": [1.0, 1.0, 1.0], "high": [2.0, 2.0, 2.0],
            "low": [0.5, 0.5, 0.5], "close": [1.5, 1.5, 1.5],
            "volume": [100.0, 100.0, 100.0],
        })

    monkeypatch.setattr(generated, "tencent_kline", fake_tencent_kline)
    astock.fetch_daily("600519", start_date="20200101", adjust="")

    assert seen["timeout"] == generated._ASTOCK_HTTP_TIMEOUT


# ---------------------------------------------------------------------------
# 5) 批量参数预算 + 失败预算对象
# ---------------------------------------------------------------------------
def test_normalize_batch_args_clamps_and_reports():
    size, pause, notes = throttle.normalize_batch_args(300, 1.0)
    assert (size, pause) == (throttle.MAX_BATCH_SIZE, throttle.MIN_BATCH_PAUSE_SECONDS)
    assert any("batch-size" in n for n in notes)
    assert any("batch-pause" in n for n in notes)
    # 合规参数不被改动、也不产生噪声
    assert throttle.normalize_batch_args(20, 20.0) == (20, 20.0, [])
    assert throttle.normalize_batch_args(0, 30.0)[0] == 1


def test_failure_budget_blocks_after_threshold():
    budget = throttle.FailureBudget(3)
    assert budget.record_failure("e1") is False
    assert budget.record_failure("e2") is False
    assert budget.record_failure("e3") is True
    assert budget.primary_blocked is True
    assert "连续 3 只失败" in budget.blocked_reason
    budget.record_success()
    assert budget.primary_blocked is True  # 成功不清除本轮封锁
    snap = budget.snapshot()
    assert snap["total_failures"] == 3 and snap["total_successes"] == 1


def test_run_with_deadline_returns_and_times_out():
    assert throttle.run_with_deadline(lambda: 42, 5.0) == 42

    def slow():
        time.sleep(0.5)
        return "late"

    with pytest.raises(throttle.TencentBudgetExceeded):
        throttle.run_with_deadline(slow, 0.1)
    # 原异常原样回传（不是被包成超时）
    def boom():
        raise ValueError("bad")

    with pytest.raises(ValueError):
        throttle.run_with_deadline(boom, 5.0)


def test_count_requests_wraps_requests_request(monkeypatch):
    calls = []

    def fake_request(*args, **kwargs):
        calls.append(args)
        return FakeResponse("{}")

    monkeypatch.setattr(generated.requests, "request", fake_request)
    wrapped = generated.requests.request
    with throttle.count_requests() as stats:
        generated.requests.request("GET", "https://example.invalid/x")
        generated.requests.request("GET", "https://example.invalid/y")
    assert stats["count"] == 2
    assert generated.requests.request is wrapped
    assert len(calls) == 2
