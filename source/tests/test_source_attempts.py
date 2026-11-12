# -*- coding: utf-8 -*-
"""P0-4（issue #7）：逐通道失败可见 —— source_attempts + 可读 warnings。

离线用例，覆盖：
  * 429 / timeout / empty 三种失败各留一条 attempt（来源、错误类、截断文本、冷却）；
  * 冷却中 / 已剔除的通道在 attempts 里以 ok=None 显式留痕；
  * 全通道首试成功时 warnings 不新增噪声；
  * 降级回退缓存 / 完全失败时 data.source 与 stale 语义正确；
  * 错误文本脱敏（URL、token）与 ≤200 字截断；
  * 错误响应与成功响应 diagnostics 键同构。
"""
import datetime as dt
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd
import pytest
import requests

from stock_analysis.data_loader import bars, meta


def _toy(n=30, start="2020-01-01", base=100.0):
    index = pd.bdate_range(start, periods=n, name="date")
    close = [base + i for i in range(n)]
    return pd.DataFrame({
        "open": close, "high": [c + 1 for c in close],
        "low": [c - 1 for c in close], "close": close,
        "volume": [1000.0 + i for i in range(n)],
    }, index=index)


def _pool(sources, cooldown=120, max_consecutive=3, max_total=8):
    return bars._DataSourcePool(
        sources, cooldown_seconds=cooldown,
        max_consecutive_failures=max_consecutive,
        max_total_failures=max_total, failure_window_seconds=600)


# ---------------------------------------------------------------------------
# 渠道池：逐条尝试
# ---------------------------------------------------------------------------
def test_attempts_collect_429_timeout_empty():
    secret = "TOPSECRET123"

    def rate_limited(*a, **k):
        raise requests.HTTPError(
            "429 Client Error: Too Many Requests for url: "
            f"https://push2.eastmoney.com/api/qt/stock?token={secret}")

    def timed_out(*a, **k):
        raise requests.Timeout("read timed out")

    def empty(*a, **k):
        return pd.DataFrame()

    pool = _pool([
        ("astock_tencent", rate_limited),
        ("eastmoney", timed_out),
        ("sina_stock", empty),
    ])
    df, used = pool.fetch("600519", "20200101", "qfq")

    assert df is None and used is None
    attempts = pool.last_attempts
    assert [a["source"] for a in attempts] == [
        "astock_tencent", "eastmoney", "sina_stock"]
    assert [a["ok"] for a in attempts] == [False, False, False]

    first, second, third = attempts
    assert first["error_class"] == "HTTPError"
    assert first["cooldown"] is True
    assert first["cooldown_seconds"] == 120
    assert first["removed"] is False
    # 脱敏：不泄露 token，长 URL 被替换
    assert secret not in first["error_text"]
    assert "https://" not in first["error_text"]
    assert "<url>" in first["error_text"]

    assert second["error_class"] == "Timeout"
    assert second["cooldown"] is True
    assert "timed out" in second["error_text"].lower()

    assert third["error_class"] == "RuntimeError"
    assert third["cooldown"] is False
    assert "返回空数据" in third["error_text"]

    for item in attempts:
        assert len(item["error_text"]) <= bars.MAX_ATTEMPT_ERROR_TEXT


def test_attempt_warnings_include_source_and_error():
    attempts = [
        {"source": "astock_tencent", "ok": False, "error_class": "HTTPError",
         "error_text": "<url>", "cooldown": True, "cooldown_seconds": 120,
         "removed": False},
        {"source": "eastmoney", "ok": False, "error_class": "Timeout",
         "error_text": "read timed out", "cooldown": True,
         "cooldown_seconds": 120, "removed": False},
        {"source": "sina_stock", "ok": False, "error_class": "RuntimeError",
         "error_text": "sina_stock 返回空数据", "cooldown": False,
         "removed": False},
    ]
    lines = bars.format_attempt_warnings(attempts)
    assert any("astock_tencent 失败" in x and "HTTPError" in x and "120s" in x
               for x in lines)
    assert any("eastmoney 失败" in x and "Timeout" in x for x in lines)
    assert any("sina_stock 失败" in x and "返回空数据" in x for x in lines)


def test_cooldown_and_removed_are_visible():
    def rate_limited(*a, **k):
        raise requests.HTTPError("429 Too Many Requests")

    def alive(*a, **k):
        return _toy(5)

    # 第一轮：腾讯 429 进入冷却，东财成功
    pool = _pool([("astock_tencent", rate_limited), ("eastmoney", alive)])
    assert pool.fetch("600519", "20200101", "qfq")[1] == "eastmoney"
    assert pool.last_attempts[0]["source"] == "astock_tencent"
    assert pool.last_attempts[0]["cooldown"] is True

    # 第二轮：腾讯仍在冷却 → 以 ok=None 留痕（不是静默跳过）
    assert pool.fetch("600519", "20200101", "qfq")[1] == "eastmoney"
    skipped = [a for a in pool.last_attempts if a["source"] == "astock_tencent"]
    assert skipped and skipped[0]["ok"] is None
    assert skipped[0]["cooldown"] is True and skipped[0]["removed"] is False
    assert any("冷却中" in x for x in bars.format_attempt_warnings(pool.last_attempts))

    # 连续失败达到阈值 → 剔除；再调用时以 removed=True 留痕
    pool2 = _pool([("eastmoney", rate_limited)], max_consecutive=1)
    pool2.fetch("600519", "20200101", "qfq")
    assert pool2.last_attempts[0]["removed"] is True
    pool2.fetch("600519", "20200101", "qfq")
    assert pool2.last_attempts[0]["ok"] is None
    assert pool2.last_attempts[0]["removed"] is True
    assert any("已从渠道池剔除" in x
               for x in bars.format_attempt_warnings(pool2.last_attempts))


def test_success_attempt_adds_no_warning_noise():
    pool = _pool([("astock_tencent", lambda *a, **k: _toy(5))])
    df, used = pool.fetch("600519", "20200101", "qfq")
    assert used == "astock_tencent" and len(df) == 5
    assert pool.last_attempts == [{
        "source": "astock_tencent", "ok": True, "error_class": None,
        "error_text": None, "cooldown": False, "removed": False,
    }]
    assert bars.format_attempt_warnings(pool.last_attempts) == []


def test_error_text_truncated_and_source_whitelisted():
    long_text = ("api_key=topsecret https://a.example/path?q=1"
                 " with url: /api/qt/stock?fields1=f1%2Cf2%2Cf3 " + "x" * 500)
    text = bars._sanitize_error_text(RuntimeError(long_text))
    assert len(text) <= bars.MAX_ATTEMPT_ERROR_TEXT
    assert "topsecret" not in text
    assert "https://" not in text
    # 相对路径 URL（requests 的 `with url: /api/...?fields1=...`）也要脱敏
    assert "/api/qt/stock" not in text and "fields1=" not in text
    assert "url: <url>" in text
    assert bars._safe_source_name("../../etc/passwd") == "unknown"
    assert bars._safe_source_name("astock_tencent") == "astock_tencent"


# ---------------------------------------------------------------------------
# meta 集成：_LAST_FETCH_INFO.source_attempts + data.source / stale 语义
# ---------------------------------------------------------------------------
class _AttemptPool:
    """最小 fake 池：返回固定 payload，并带 last_attempts（与真实池契约一致）。"""

    def __init__(self, payload, attempts):
        self.payload = payload
        self.last_attempts = attempts

    def fetch(self, symbol, start_date, adjust, preferred=None):
        source = None
        if self.payload is not None:
            source = self.last_attempts[-1]["source"] if self.last_attempts else None
        return self.payload, source


_FAILED_ATTEMPTS = [
    {"source": "astock_tencent", "ok": False, "error_class": "AstockError",
     "error_text": "ASTOCK_FORCE_FAIL=1：腾讯源被强制失败（降级演练）",
     "cooldown": False, "removed": False},
    {"source": "eastmoney", "ok": False, "error_class": "ConnectionError",
     "error_text": "connection refused", "cooldown": False, "removed": False},
    {"source": "sina_stock", "ok": False, "error_class": "Timeout",
     "error_text": "read timed out", "cooldown": True,
     "cooldown_seconds": 120, "removed": False},
]


def test_meta_records_attempts_and_stale_semantics(tmp_path, monkeypatch):
    monkeypatch.setattr(meta, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(meta, "_expected_latest_trade_date",
                        lambda now=None: dt.date(2030, 1, 1))
    cached = _toy(30)
    cached.to_csv(tmp_path / "stock_600519_qfq.csv")

    monkeypatch.setattr(meta, "_STOCK_DATA_SOURCE_POOL",
                        _AttemptPool(None, _FAILED_ATTEMPTS + [
                            {"source": "sina_fund", "ok": False,
                             "error_class": "RuntimeError",
                             "error_text": "sina_fund 返回空数据",
                             "cooldown": False, "removed": False}]))
    out = meta.get_stock_daily("600519", start_date="20200101", source="akshare")
    info = meta.last_fetch_info()

    assert len(out) == 30                          # 回退旧缓存
    assert info["source"] == "cache"
    assert info["from_cache"] is True
    assert info["degraded"] is True                # → data.stale
    assert info["cache_last_date"] == "2020-02-11"
    assert [a["source"] for a in info["source_attempts"]] == [
        "astock_tencent", "eastmoney", "sina_stock", "sina_fund"]
    assert all(a["ok"] is False for a in info["source_attempts"])

    # 错误响应同构：data.source / stale / diagnostics.source_attempts / warnings
    from decision.schema import create_error_response
    doc = create_error_response(
        "data_source_error", "行情拉取失败（渠道池全失败且无可用缓存）",
        "600519", "stock", fetch_info=info)
    assert doc["data"]["source"] == "cache"
    assert doc["data"]["stale"] is True
    assert len(doc["diagnostics"]["source_attempts"]) == 4
    joined = "\n".join(doc["diagnostics"]["warnings"])
    for token in ("astock_tencent 失败", "eastmoney 失败", "sina_stock 失败",
                  "sina_fund 失败"):
        assert token in joined


def test_meta_total_failure_source_none_stale_false(tmp_path, monkeypatch):
    monkeypatch.setattr(meta, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(meta, "_expected_latest_trade_date",
                        lambda now=None: dt.date(2030, 1, 1))
    monkeypatch.setattr(meta, "_STOCK_DATA_SOURCE_POOL",
                        _AttemptPool(None, list(_FAILED_ATTEMPTS)))

    assert meta.get_stock_daily("600519", source="akshare") is None
    info = meta.last_fetch_info()
    assert info["source"] is None
    assert info["from_cache"] is False   # 既有语义：_record_fetch(None, None) → bool(None)
    assert info["degraded"] is False
    assert len(info["source_attempts"]) == 3

    from decision.schema import create_error_response
    doc = create_error_response(
        "data_source_error", "行情拉取失败（渠道池全失败且无可用缓存）",
        "600519", "stock", fetch_info=info)
    assert doc["data"]["source"] is None
    assert doc["data"]["stale"] is False
    assert len(doc["diagnostics"]["source_attempts"]) == 3


def test_meta_success_attempts_no_noise(tmp_path, monkeypatch):
    monkeypatch.setattr(meta, "DATA_DIR", str(tmp_path))
    frame = _toy(30)
    monkeypatch.setattr(meta, "_expected_latest_trade_date",
                        lambda now=None: frame.index[-1].date())
    ok_attempts = [{"source": "astock_tencent", "ok": True,
                    "error_class": None, "error_text": None,
                    "cooldown": False, "removed": False}]
    monkeypatch.setattr(meta, "_STOCK_DATA_SOURCE_POOL",
                        _AttemptPool(frame, ok_attempts))

    out = meta.get_stock_daily("600519", start_date="20200101", source="akshare")
    assert len(out) == 30
    info = meta.last_fetch_info()
    assert info["source"] == "astock_tencent"
    assert info["degraded"] is False
    assert info["source_attempts"] == ok_attempts
    assert bars.format_attempt_warnings(info["source_attempts"]) == []


def test_resolve_and_fetch_merges_attempts_across_probes(tmp_path, monkeypatch):
    """auto 探测「先股票后基金」时，基金分支的 reset 不得冲掉股票渠道明细。"""
    monkeypatch.setattr(meta, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(meta, "_expected_latest_trade_date",
                        lambda now=None: dt.date(2030, 1, 1))
    monkeypatch.setattr(meta, "_STOCK_DATA_SOURCE_POOL",
                        _AttemptPool(None, list(_FAILED_ATTEMPTS)))

    def fake_fund(symbol, start_date=None, adjust='', source='akshare'):
        meta._reset_fetch_info(source)   # 模拟真实 get_fund_daily 的清账行为
        meta._record_fetch(None, None)
        return None

    monkeypatch.setattr(meta, "get_fund_daily", fake_fund)
    df, dtype, err = meta.resolve_and_fetch("601398", type_hint="auto")
    assert df is None and dtype == "stock" and err
    info = meta.last_fetch_info()
    assert [a["source"] for a in info["source_attempts"]] == [
        "astock_tencent", "eastmoney", "sina_stock"]


def test_last_fetch_info_returns_copy_of_attempts(monkeypatch):
    monkeypatch.setattr(meta, "_STOCK_DATA_SOURCE_POOL",
                        _AttemptPool(None, list(_FAILED_ATTEMPTS)))
    meta._reset_fetch_info("akshare")
    meta._capture_source_attempts()
    info = meta.last_fetch_info()
    info["source_attempts"][0]["source"] = "tampered"
    assert meta.last_fetch_info()["source_attempts"][0]["source"] == "astock_tencent"


# ---------------------------------------------------------------------------
# CLI：成功路径不因 attempts 新增噪声
# ---------------------------------------------------------------------------
def test_cli_success_path_has_no_attempt_noise(monkeypatch):
    import cli

    frame = _toy(300)
    info = {
        "source": "astock_tencent", "from_cache": False, "requested_source": "akshare",
        "degraded": False, "degraded_reason": None, "cache_last_date": "2021-02-19",
        "lag_days": 0, "expected_date": "2021-02-19", "expected_source": "estimate",
        "merged_from_cache": False, "cache_rows_kept": 0,
        "source_attempts": [{"source": "astock_tencent", "ok": True,
                             "error_class": None, "error_text": None,
                             "cooldown": False, "removed": False}],
    }
    monkeypatch.setattr(meta, "resolve_and_fetch",
                        lambda *a, **k: (frame, "stock", None))
    monkeypatch.setattr(cli, "last_fetch_info", lambda: dict(info))
    monkeypatch.setattr(cli, "get_stock_name", lambda c: c)

    out = cli.advise_internal("600519", fast=True)
    warns = out["diagnostics"]["warnings"]
    assert not any("失败：" in w or "冷却中" in w or "已从渠道池剔除" in w
                   for w in warns), warns
    assert out["diagnostics"]["source_attempts"][0]["ok"] is True
    assert out["schema_version"] == "1.0"
