# -*- coding: utf-8 -*-
"""issue #108：资金流预计算降级出声 / 请求预算（离线测试）。

* 东财不可达 → 新浪口径时：预计算日志、缓存 ``degraded_reason``、
  ``evidence.flow`` 与 ``diagnostics.warnings`` 三处都必须写明原因；
* 444 熔断后本轮剩余东财请求一个都不发；
* 冷却期内预计算 0 请求；
* 单域名请求预算超限 → 提前退出并告警，不落半截缓存。

所有网络入口都打桩，不触网。
"""
from __future__ import annotations

import json
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1]
TOOLS = SRC.parent / "tools"
for _path in (SRC, TOOLS):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from stock_analysis.data_loader import flow as flow_mod  # noqa: E402
from stock_analysis.data_sources import _astock_generated as _g  # noqa: E402
from stock_analysis.data_sources import eastmoney_guard as em  # noqa: E402

import precompute_flow as tool  # noqa: E402


def _bdays(end: date, n: int):
    days, cursor = [], end
    while len(days) < n:
        if cursor.weekday() < 5:
            days.append(cursor)
        cursor -= timedelta(days=1)
    return list(reversed(days))


def _full_row(day: date, value: float) -> dict:
    return {"date": day.isoformat(), "main_net": value, "small_net": 0.0,
            "mid_net": 0.0, "large_net": 0.0, "super_net": 0.0}


def _sina_row(day: date, value: float) -> dict:
    return {"date": day.isoformat(), "close": "100.0000",
            "net_amount": f"{value:.4f}", "turnover": "1.0000"}


def _sina_rows(n=20, end=date(2026, 11, 12)):
    return [_sina_row(d, float(i + 1)) for i, d in enumerate(_bdays(end, n))]


def _eastmoney_rows(n=20, end=date(2026, 11, 12)):
    return [_full_row(d, float(i + 1)) for i, d in enumerate(_bdays(end, n))]


class _BlockedResponse:
    status_code = 444
    content = b""

    def json(self):
        raise AssertionError("444 不应被解析")


@pytest.fixture(autouse=True)
def _fast_em(monkeypatch):
    monkeypatch.setattr(_g, "_em_last_call", [time.time()])
    monkeypatch.setattr(_g.time, "sleep", lambda seconds: None)


# ---------------------------------------------------------------------------
# 1) 降级出声：日志 + 缓存字段 + evidence / warnings
# ---------------------------------------------------------------------------
def test_degraded_to_sina_is_loud_in_log_cache_and_evidence(tmp_path, capsys):
    def eastmoney_down(code):
        raise ValueError("Remote end closed connection without response")

    def sina_ok(code):
        return _sina_rows()

    stats = tool.precompute(
        ["600519"], out_dir=str(tmp_path), interval=1.0, retries=0,
        fetch_fn=eastmoney_down, sina_fetch_fn=sina_ok,
        sleep_fn=lambda seconds: None, now_fn=datetime.now)

    stderr = capsys.readouterr().err
    assert "已降级到 sina_total_net" in stderr
    assert stats["degraded"] is True
    assert "东财不可达" in stats["degraded_reason"]

    payload = json.loads((tmp_path / "600519.json").read_text(encoding="utf-8"))
    assert payload["caliber"] == tool.CALIBER_SINA
    assert payload["degraded_reason"] == stats["degraded_reason"]

    summary = flow_mod.summarize_flow("600519", flow_dir=str(tmp_path))
    assert summary["caliber"] == flow_mod.CALIBER_SINA
    assert summary["stale"] in (True, False)      # 保留原字段
    assert summary["degraded_reason"] == stats["degraded_reason"]
    assert set(summary) == set(flow_mod.FLOW_KEYS)

    block, notes = flow_mod.flow_evidence("600519", "stock",
                                          flow_dir=str(tmp_path))
    assert block["degraded_reason"] == stats["degraded_reason"]
    assert any("已降级" in note and "sina_total_net" in note
               for note in notes), notes


def test_eastmoney_success_no_degraded_reason(tmp_path):
    stats = tool.precompute(
        ["600519"], out_dir=str(tmp_path), interval=1.0, retries=0,
        fetch_fn=lambda code: _eastmoney_rows(),
        sina_fetch_fn=lambda code: pytest.fail("东财成功时不得调用新浪"),
        sleep_fn=lambda seconds: None, now_fn=datetime.now)

    payload = json.loads((tmp_path / "600519.json").read_text(encoding="utf-8"))
    assert payload["caliber"] == tool.CALIBER_EASTMONEY
    assert "degraded_reason" not in payload
    assert stats["degraded"] is False and stats["degraded_reason"] is None

    block, notes = flow_mod.flow_evidence("600519", "stock",
                                          flow_dir=str(tmp_path))
    assert block["degraded_reason"] is None
    assert not any("已降级" in note for note in notes)


# ---------------------------------------------------------------------------
# 2) 444 熔断：本轮剩余东财请求一个都不发，降级仍出声
# ---------------------------------------------------------------------------
def test_444_block_stops_eastmoney_requests_and_records_reason(tmp_path, monkeypatch, capsys):
    em_calls = []

    def fake_get(url, params=None, headers=None, timeout=15, **kwargs):
        em_calls.append(url)
        return _BlockedResponse()

    monkeypatch.setattr(_g.EM_SESSION, "get", fake_get)

    def eastmoney_fetch(code):
        # 模拟生成物 stock_fund_flow_120d：吞掉异常并返回 []。
        try:
            _g.em_get("https://push2his.eastmoney.com/api/qt/stock/fflow/daykline/get")
        except Exception:
            pass
        return []

    stats = tool.precompute(
        ["600519", "600000"], out_dir=str(tmp_path), interval=1.0, retries=0,
        fetch_fn=eastmoney_fetch,
        sina_fetch_fn=lambda code: _sina_rows(),
        sleep_fn=lambda seconds: None, now_fn=datetime.now)

    assert len(em_calls) == 1, "命中 444 后不得再发东财请求"
    assert stats["blocked"] is True
    assert stats["block_reason"] == "http_444"
    assert "反爬阻断" in stats["degraded_reason"]
    assert "已降级到 sina_total_net" in capsys.readouterr().err
    assert em.cooldown_state()["reason"] == "http_444"

    for code in ("600519", "600000"):
        payload = json.loads((tmp_path / f"{code}.json").read_text(encoding="utf-8"))
        assert payload["caliber"] == tool.CALIBER_SINA
        assert "反爬阻断" in payload["degraded_reason"]


# ---------------------------------------------------------------------------
# 3) 冷却期内 0 请求
# ---------------------------------------------------------------------------
def test_precompute_skips_entirely_while_cooling(tmp_path, monkeypatch):
    em.mark_cooldown("http_444", "封禁窗口", seconds=60)
    calls = []

    def never(code):
        calls.append(code)
        raise AssertionError("冷却期内不得发请求")

    stats = tool.precompute(
        ["600519"], out_dir=str(tmp_path), interval=1.0, retries=0,
        fetch_fn=never, sina_fetch_fn=never,
        sleep_fn=lambda seconds: None, now_fn=datetime.now)

    assert stats["skipped_cooldown"] is True
    assert stats["requests"] == 0
    assert calls == []
    assert not (tmp_path / "600519.json").exists()


def test_main_exit_3_when_cooling(tmp_path, capsys):
    em.mark_cooldown("http_444", "封禁窗口", seconds=60)
    out = tmp_path / "flow_out"
    assert tool.main(["--out-dir", str(out)]) == 3
    captured = capsys.readouterr()
    assert "requests=0" in captured.out
    assert "SKIP" in captured.err
    assert not out.exists(), "冷却期内不应创建缓存目录 / 写半截缓存"


# ---------------------------------------------------------------------------
# 4) 请求预算超限：提前退出 + 告警，不落半截缓存
# ---------------------------------------------------------------------------
def test_budget_exceeded_stops_run_before_extra_requests(tmp_path, capsys):
    calls = []

    def ok(code):
        calls.append(code)
        return _eastmoney_rows()

    stats = tool.precompute(
        ["600519", "600000", "300750"], out_dir=str(tmp_path),
        interval=1.0, retries=0, budget=1,
        fetch_fn=ok, sina_fetch_fn=lambda code: pytest.fail("不应降级"),
        sleep_fn=lambda seconds: None, now_fn=datetime.now)

    assert len(calls) == 1, "预算 1 时第二次请求必须在发送前被拦下"
    assert stats["budget_exceeded"] is True
    assert stats["requests"] == 1
    assert "预算超限" in stats["reason"]
    assert "BUDGET" in capsys.readouterr().err
    assert stats["request_limits"]["counts"] == {"push2his.eastmoney.com": 1}
    assert stats["request_limits"]["limit_per_domain"] == 1
    assert stats["failed"][0]["code"] == "600000"
    assert stats["skipped"] == ["300750"]
    assert (tmp_path / "600519.json").exists()
    assert not (tmp_path / "600000.json").exists()
    assert not (tmp_path / "300750.json").exists()