# -*- coding: utf-8 -*-
"""a-stock-data 适配层离线测试（issue #3）。

所有网络入口都在测试内打桩；fixture 为 2026-11-12 从真实端点录制的响应，
放在 ``tests/fixtures/astock/``。``conftest.py`` 强制日历离线、清理进程缓存。
"""
from __future__ import annotations

import datetime as dt
import json
import os
import subprocess
import sys
from pathlib import Path

import pandas as pd
import pytest
import requests

FIXTURES = Path(__file__).parent / "fixtures" / "astock"
SRC = Path(__file__).resolve().parents[1]
ROOT = SRC.parent


class FakeResponse:
    """requests.Response 的最小替身：文本 + json()。"""

    def __init__(self, text: str, url: str = "https://example.invalid/x", status: int = 200):
        self.text = text
        self.url = url
        self.status_code = status

    def json(self):
        return json.loads(self.text)

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code} error", response=self)


def _fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def _patch_factors(monkeypatch, fixture: str):
    """Stub Sina's qfq/hfq factor endpoint."""
    text = _fixture(fixture)
    calls = []

    def fake_get(url, **kwargs):
        calls.append(url)
        return FakeResponse(text, url=url)

    from stock_analysis.data_sources import _astock_generated
    monkeypatch.setattr(_astock_generated.requests, "get", fake_get)
    return calls


def _patch_kline(monkeypatch, fixture: str):
    """Serve a recorded response, honouring the requested date window.

    ``tencent_kline(start=...)`` paginates from start to today in <640-bar
    chunks, so a single narrow recording must be filtered per request exactly
    like the real backend does; otherwise the out-of-window guard (correctly)
    rejects it.
    """
    text = _fixture(fixture)
    calls = []

    def fake_http(url, params=None, data=None, headers=None, method="GET",
                  timeout=None, **kwargs):
        calls.append({"url": url, "params": params, "timeout": timeout})
        payload = json.loads(text)
        param = (params or {}).get("param", "")
        parts = param.split(",")
        if len(parts) >= 4:
            symbol, start, end = parts[0], parts[2], parts[3]
            node = (payload.get("data") or {}).get(symbol)
            if isinstance(node, dict):
                for key, rows in list(node.items()):
                    if (isinstance(rows, list) and rows
                            and isinstance(rows[0], list)):
                        node[key] = [
                            row for row in rows
                            if (not start or row[0] >= start)
                            and (not end or row[0] <= end)
                        ]
        return FakeResponse(json.dumps(payload))

    from stock_analysis.data_sources import _astock_generated
    monkeypatch.setattr(_astock_generated, "_v39_http", fake_http)
    return calls


# ---------------------------------------------------------------------------
# import / 生成物
# ---------------------------------------------------------------------------
def test_import_does_not_touch_network_or_mootdx():
    """导入数据源包不得触网，也不得依赖 mootdx/baostock。"""
    script = (
        "import socket\n"
        "class _Blocker:\n"
        "    def __init__(self, *a, **k):\n"
        "        raise RuntimeError('network access during import')\n"
        "socket.socket = _Blocker\n"
        "socket.create_connection = lambda *a, **k: (_ for _ in ()).throw(RuntimeError('net'))\n"
        "import stock_analysis\n"
        "import stock_analysis.data_sources\n"
        "import stock_analysis.data_sources.astock\n"
        "assert 'mootdx' not in __import__('sys').modules\n"
        "assert 'baostock' not in __import__('sys').modules\n"
        "print('ok')\n"
    )
    env = dict(os.environ, PYTHONPATH=str(SRC))
    result = subprocess.run([sys.executable, "-c", script], cwd=str(SRC), env=env,
                            capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    assert "ok" in result.stdout


def test_generated_artifact_matches_snapshot():
    """入库的生成物必须能由固定快照重复生成（hash/函数清单校验）。"""
    result = subprocess.run(
        [sys.executable, str(ROOT / "tools" / "astock_extract.py"), "--check"],
        cwd=str(ROOT), capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    manifest = json.loads((SRC / "stock_analysis" / "data_sources" /
                           "_astock_manifest.json").read_text(encoding="utf-8"))
    assert manifest["upstream"]["commit"] == "f21534946c42be3ad048ac938eecb1bad84cb8ab"
    assert manifest["upstream"]["version"] == "3.10.4"
    names = {item["symbol"] for item in manifest["selections"]}
    assert {"tencent_kline", "tencent_quote", "trading_calendar",
            "sina_adjust_factor", "apply_adjust", "norm_ticker"} <= names


# ---------------------------------------------------------------------------
# 日线契约 / 单位
# ---------------------------------------------------------------------------
def test_daily_contract_and_ratio_qfq(monkeypatch):
    from stock_analysis.data_sources import astock

    kline_calls = _patch_kline(monkeypatch, "kline_600519_raw.json")
    factor_calls = _patch_factors(monkeypatch, "sina_qfq_600519.js")
    df = astock.fetch_daily("600519", start_date="20200101", adjust="qfq")

    assert list(df.columns) == ["open", "high", "low", "close", "volume"]
    assert isinstance(df.index, pd.DatetimeIndex)
    assert df.index.name == "date"
    assert df.index.is_monotonic_increasing
    assert not df.index.duplicated().any()
    assert len(df) == 7
    # 比例前复权：raw 1130.000 / factor 1.1815046729 = 956.4076
    # （与既有新浪缓存 2020-01-02 close=956.41 同口径；不是腾讯等差 qfq 870.741）
    assert df.loc["2020-01-02", "close"] == pytest.approx(956.4076, abs=1e-4)
    assert df.loc["2020-01-03", "close"] == pytest.approx(912.8699, abs=1e-4)
    assert abs(df.loc["2020-01-02", "close"] - 870.741) > 1.0
    # 非科创板：腾讯「手」→ 统一为「股」
    assert df.loc["2020-01-02", "volume"] == pytest.approx(148099 * 100)
    # 腾讯取的是不复权（param 最后一段为空），因子来自 §1.6
    assert kline_calls and kline_calls[0]["timeout"] == (5, 15)
    assert kline_calls[0]["params"]["param"].rstrip().endswith(",")
    assert factor_calls and "600519" in factor_calls[0] and "qfq.js" in factor_calls[0]


def test_star_market_volume_stays_shares(monkeypatch):
    """科创板（688/689）腾讯本就返回「股」，不得再 ×100。"""
    from stock_analysis.data_sources import astock

    _patch_kline(monkeypatch, "kline_688981_qfq.json")
    df = astock.fetch_daily("688981", start_date="20261103", adjust="")
    assert df.loc["2026-11-03", "volume"] == pytest.approx(51926450)
    assert df.loc["2026-11-03", "close"] == pytest.approx(102.110)


def test_factor_failure_is_not_silently_unadjusted(monkeypatch):
    """复权因子不可用时必须失败，绝不能把原始价当复权价返回。"""
    from stock_analysis.data_sources import astock, _astock_generated

    _patch_kline(monkeypatch, "kline_600519_raw.json")

    def boom(url, **kwargs):
        raise requests.ConnectionError("sina down")

    monkeypatch.setattr(_astock_generated.requests, "get", boom)
    with pytest.raises(astock.AstockError) as excinfo:
        astock.fetch_daily("600519", start_date="20200101", adjust="qfq")
    assert "复权因子" in str(excinfo.value)


# ---------------------------------------------------------------------------
# 失败路径：空 / 限流 / HTTP 错误 / 超时 / 格式变化
# ---------------------------------------------------------------------------
def test_empty_payload_raises(monkeypatch):
    from stock_analysis.data_sources import astock

    _patch_kline(monkeypatch, "kline_empty.json")
    with pytest.raises(astock.AstockError):
        astock.fetch_daily("600519", start_date="20200101")


def test_rate_limited_all_hosts_raise(monkeypatch):
    from stock_analysis.data_sources import astock

    _patch_kline(monkeypatch, "kline_rate_limited.json")
    with pytest.raises(astock.AstockError) as excinfo:
        astock.fetch_daily("600519", start_date="20200101")
    assert "限流" in str(excinfo.value)


def test_param_error_is_not_swallowed(monkeypatch):
    from stock_analysis.data_sources import astock

    _patch_kline(monkeypatch, "kline_param_error.json")
    with pytest.raises(astock.AstockError) as excinfo:
        astock.fetch_daily("600519", start_date="20200101")
    assert "参数错误" in str(excinfo.value)


def test_bad_shape_raises(monkeypatch):
    from stock_analysis.data_sources import astock

    _patch_kline(monkeypatch, "kline_bad_shape.json")
    with pytest.raises(astock.AstockError):
        astock.fetch_daily("600519", start_date="20200101")


def test_http_error_raises(monkeypatch):
    from stock_analysis.data_sources import astock, _astock_generated

    response = FakeResponse("upstream exploded", status=500)

    def fake_request(*args, **kwargs):
        return response

    monkeypatch.setattr(_astock_generated.requests, "request", fake_request)
    with pytest.raises(astock.AstockError) as excinfo:
        astock.fetch_daily("600519", start_date="20200101")
    assert "500" in str(excinfo.value) or "HTTPError" in str(excinfo.value)


def test_force_fail_env_raises(monkeypatch):
    """ASTOCK_FORCE_FAIL=1 必须让腾讯通道失败（渠道池降级演练入口）。"""
    from stock_analysis.data_sources import astock

    monkeypatch.setenv("ASTOCK_FORCE_FAIL", "1")
    with pytest.raises(astock.AstockError) as excinfo:
        astock.fetch_daily("600519", start_date="20200101")
    assert "强制失败" in str(excinfo.value)


def test_timeout_raises(monkeypatch):
    from stock_analysis.data_sources import astock, _astock_generated

    def fake_request(*args, **kwargs):
        raise requests.Timeout("read timed out")

    monkeypatch.setattr(_astock_generated.requests, "request", fake_request)
    with pytest.raises(astock.AstockError) as excinfo:
        astock.fetch_daily("600519", start_date="20200101")
    assert "timeout" in str(excinfo.value).lower()


# ---------------------------------------------------------------------------
# 实时快照（可选辅助）
# ---------------------------------------------------------------------------
def test_quote_fixture(monkeypatch):
    from stock_analysis.data_sources import astock, _astock_generated

    raw = (FIXTURES / "quote_600519.gbk.txt").read_bytes()

    class _FakeUrlOpen:
        def read(self):
            return raw

    monkeypatch.setattr(_astock_generated.urllib.request, "urlopen",
                        lambda request, timeout=None: _FakeUrlOpen())
    quote = astock.fetch_quote("600519")
    assert quote["name"] == "贵州茅台"
    assert quote["price"] == pytest.approx(1410.27)
    assert quote["pe_ttm"] == pytest.approx(15.0, abs=1e-6) or quote["pe_ttm"] > 0
    assert quote["pb"] > 0
    assert quote["limit_up"] > quote["price"] > quote["limit_down"]


# ---------------------------------------------------------------------------
# 官方交易日历 / 回退
# ---------------------------------------------------------------------------
def _enable_calendar(monkeypatch, fixture="calendar_szse_2026-11.json"):
    from stock_analysis.data_sources import _astock_generated
    monkeypatch.delenv("ASTOCK_CALENDAR_OFFLINE", raising=False)
    text = _fixture(fixture)
    monkeypatch.setattr(_astock_generated, "_official_get",
                        lambda url, params=None, referer=None: FakeResponse(text))


def test_official_calendar_and_latest_day(monkeypatch):
    from stock_analysis.data_sources import astock

    _enable_calendar(monkeypatch)
    days = astock.get_month_calendar(2026, 11)
    assert days["2026-11-02"] is True
    assert days["2026-11-01"] is False
    assert astock.is_trading_day("2026-11-02") is True
    assert astock.is_trading_day("2026-11-01") is False

    # 收盘后：当天就是最近交易日
    assert astock.latest_trading_day_at(dt.datetime(2026, 11, 4, 16, 0)) == dt.date(2026, 11, 4)
    # 未收盘：算到昨天
    assert astock.latest_trading_day_at(dt.datetime(2026, 11, 4, 10, 0)) == dt.date(2026, 11, 3)
    # 周日：回退到周五
    assert astock.latest_trading_day_at(dt.datetime(2026, 11, 8, 10, 0)) == dt.date(2026, 11, 6)
    assert astock.calendar_source() == "astock_calendar"


def test_calendar_failure_returns_none(monkeypatch):
    from stock_analysis.data_sources import astock, _astock_generated

    monkeypatch.delenv("ASTOCK_CALENDAR_OFFLINE", raising=False)

    def boom(*args, **kwargs):
        raise requests.ConnectionError("offline")

    monkeypatch.setattr(_astock_generated, "_official_get", boom)
    assert astock.get_month_calendar(2026, 11) is None
    assert astock.latest_trading_day_at(dt.datetime(2026, 11, 4, 16, 0)) is None
    assert astock.calendar_last_error()


def test_expected_trade_date_official_and_fallback(monkeypatch):
    from stock_analysis.data_loader import meta
    from stock_analysis.data_sources import astock

    monkeypatch.setattr(astock, "latest_trading_day_at",
                        lambda now=None: dt.date(2026, 11, 4))
    assert meta._expected_latest_trade_date(dt.datetime(2026, 11, 4, 16, 0)) == dt.date(2026, 11, 4)
    assert meta._EXPECTED_SOURCE["value"] == "astock_calendar"

    monkeypatch.setattr(astock, "latest_trading_day_at", lambda now=None: None)
    # 2026-11-08 是周日，回退推算应得到 2026-11-06
    assert meta._expected_latest_trade_date(dt.datetime(2026, 11, 8, 10, 0)) == dt.date(2026, 11, 6)
    assert meta._EXPECTED_SOURCE["value"] == "estimate"

    meta._reset_fetch_info("akshare")
    meta._record_fetch("cache", True)
    assert meta.last_fetch_info()["expected_source"] == "estimate"


def test_expected_trade_date_never_weekend_offline():
    from stock_analysis.data_loader import meta
    assert meta._expected_latest_trade_date().weekday() < 5


# ---------------------------------------------------------------------------
# 渠道池：优先级 / 降级 / 缓存往返
# ---------------------------------------------------------------------------
def test_pool_priority_and_failover():
    from stock_analysis.data_loader import bars, meta

    names = [state.name for state in meta._STOCK_DATA_SOURCE_POOL._states]
    assert names[:4] == ["astock_tencent", "eastmoney", "sina_stock", "sina_fund"]
    assert meta._SOURCE_PREFERENCE["akshare"][0] == "astock_tencent"

    toy = pd.DataFrame({
        "date": pd.date_range("2020-01-01", periods=5, freq="B"),
        "open": 1.0, "high": 2.0, "low": 1.0, "close": 1.5, "volume": 100.0,
    })

    def dead(*args, **kwargs):
        raise RuntimeError("astock down")

    def alive(*args, **kwargs):
        return toy.copy()

    pool = bars._DataSourcePool(
        [("astock_tencent", dead), ("eastmoney", alive)],
        cooldown_seconds=0, max_consecutive_failures=3,
        max_total_failures=8, failure_window_seconds=600)
    df, used = pool.fetch("600519", "20200101", "qfq")
    assert used == "eastmoney"
    assert df is not None and len(df) == 5


def test_cache_roundtrip_and_degraded(tmp_path, monkeypatch):
    from stock_analysis.data_loader import meta

    monkeypatch.setattr(meta, "DATA_DIR", str(tmp_path))
    index = pd.date_range("2020-01-01", periods=30, freq="B", name="date")
    frame = pd.DataFrame({
        "open": range(30), "high": range(30), "low": range(30),
        "close": [100.0 + i for i in range(30)], "volume": [1000.0] * 30,
    }, index=index)

    class _Pool:
        def __init__(self, payload):
            self.payload = payload

        def fetch(self, symbol, start_date, adjust, preferred=None):
            return self.payload, ("astock_tencent" if self.payload is not None else None)

    # 30 个工作日从 2020-01-01 起，最后一根是 2020-02-11
    monkeypatch.setattr(meta, "_expected_latest_trade_date",
                        lambda now=None: dt.date(2020, 2, 11))
    monkeypatch.setattr(meta, "_STOCK_DATA_SOURCE_POOL", _Pool(frame))
    df = meta.get_stock_daily("600519", start_date="20200101", source="akshare")
    info = meta.last_fetch_info()
    assert info["source"] == "astock_tencent" and info["from_cache"] is False
    assert len(df) == 30

    cache_file = tmp_path / "stock_600519_qfq.csv"
    assert cache_file.exists()
    cached = pd.read_csv(cache_file, index_col="date", parse_dates=True)
    pd.testing.assert_frame_equal(
        df.sort_index()[["open", "high", "low", "close", "volume"]],
        cached[["open", "high", "low", "close", "volume"]].sort_index(),
        check_freq=False)

    # 缓存新鲜 → 直接命中，不再触网
    monkeypatch.setattr(meta, "_STOCK_DATA_SOURCE_POOL", _Pool(None))
    df2 = meta.get_stock_daily("600519", start_date="20200101", source="akshare")
    assert len(df2) == 30
    assert meta.last_fetch_info()["source"] == "cache"
    assert meta.last_fetch_info()["degraded"] is False

    # 缓存过期 + 渠道全灭 → 回退旧缓存并标记 degraded
    monkeypatch.setattr(meta, "_expected_latest_trade_date",
                        lambda now=None: dt.date(2030, 1, 1))
    df3 = meta.get_stock_daily("600519", start_date="20200101", source="akshare")
    info3 = meta.last_fetch_info()
    assert len(df3) == 30
    assert info3["source"] == "cache" and info3["degraded"] is True
    assert info3["cache_last_date"] == "2020-02-11"
    assert info3["lag_days"] and info3["lag_days"] > 0


# ---------------------------------------------------------------------------
# PR #9 阻断回归：None=全量 / 旧缓存不被缩短 / 增量合并
# ---------------------------------------------------------------------------
def _ohlcv_frame(dates, base=100.0, step=1.0):
    index = pd.DatetimeIndex(dates, name="date")
    close = [base + step * i for i in range(len(index))]
    return pd.DataFrame({
        "open": close, "high": [c + 1.0 for c in close],
        "low": [c - 1.0 for c in close], "close": close,
        "volume": [1000.0 + i for i in range(len(index))],
    }, index=index)


def _write_cache(tmp_path, dates, base=100.0):
    frame = _ohlcv_frame(dates, base=base)
    frame.to_csv(tmp_path / "stock_600519_qfq.csv")
    return frame


class _RecordingPool:
    """记录渠道池收到的 start_date；responder(start) → (df, source_name)。"""

    def __init__(self, responder):
        self.calls = []
        self._responder = responder

    def fetch(self, symbol, start_date, adjust, preferred=None):
        self.calls.append(start_date)
        return self._responder(start_date)


def test_none_start_requests_full_history(monkeypatch):
    """start_date=None/空 → 传给 tencent_kline 的下限必须是 1990 年，不是 2020。"""
    from stock_analysis.data_sources import astock, _astock_generated

    starts = []

    def fake_kline(code, period="day", adjust=None, start=None, end=None, count=320):
        starts.append(start)
        return pd.DataFrame({
            "date": ["2026-11-11", "2026-11-12"],
            "open": [1.0, 1.0], "high": [2.0, 2.0], "low": [0.5, 0.5],
            "close": [1.5, 1.5], "volume": [10.0, 10.0],
        })

    monkeypatch.setattr(_astock_generated, "tencent_kline", fake_kline)
    for value in (None, "", "   "):
        df = astock.fetch_daily("600519", start_date=value, adjust="")
        assert len(df) == 2
    astock.fetch_daily("600519", adjust="")  # 默认参数同样视为全量
    assert len(starts) == 4
    assert all(s is not None and s <= "1991-01-01" for s in starts)


def test_cold_start_fetches_full_history(tmp_path, monkeypatch):
    """无缓存时渠道池收到的下限是 1990；--start 只截断返回值，不截断缓存。"""
    from stock_analysis.data_loader import meta

    monkeypatch.setattr(meta, "DATA_DIR", str(tmp_path))
    dates = pd.bdate_range("2019-01-01", periods=500)  # 覆盖 2019–2020
    pool = _RecordingPool(lambda start: (_ohlcv_frame(dates), "astock_tencent"))
    monkeypatch.setattr(meta, "_STOCK_DATA_SOURCE_POOL", pool)
    monkeypatch.setattr(meta, "_expected_latest_trade_date",
                        lambda now=None: dates[-1].date())

    df = meta.get_stock_daily("600519", start_date="20200101", source="akshare")
    assert pool.calls == [meta.FULL_HISTORY_START]
    assert meta.FULL_HISTORY_START <= "19910101"
    assert 0 < len(df) < len(dates)  # 返回值按 --start 截断
    cached = pd.read_csv(tmp_path / "stock_600519_qfq.csv",
                         index_col="date", parse_dates=True)
    assert len(cached) == len(dates)  # 缓存仍是全量


def test_stale_cache_merged_not_shortened(tmp_path, monkeypatch):
    """6000 行旧缓存 + 近端 20 行（重叠一致）→ 合并后历史一根不丢。"""
    from stock_analysis.data_loader import meta

    monkeypatch.setattr(meta, "DATA_DIR", str(tmp_path))
    dates = pd.bdate_range("2000-01-03", periods=6000)
    cached = _write_cache(tmp_path, dates)
    last = dates[-1]

    extra_idx = pd.bdate_range(last + pd.Timedelta(days=1), periods=2)
    extra = _ohlcv_frame(extra_idx, base=float(cached["close"].iloc[-1]) + 10.0)
    fresh = pd.concat([cached.iloc[-20:].copy(), extra])  # 重叠段数值完全一致

    monkeypatch.setattr(meta, "_expected_latest_trade_date",
                        lambda now=None: extra_idx[-1].date())
    pool = _RecordingPool(lambda start: (fresh.copy(), "astock_tencent"))
    monkeypatch.setattr(meta, "_STOCK_DATA_SOURCE_POOL", pool)

    out = meta.get_stock_daily("600519", start_date=None, source="akshare")
    info = meta.last_fetch_info()
    near = (last - pd.Timedelta(days=meta.CACHE_OVERLAP_DAYS)).strftime("%Y%m%d")
    assert pool.calls == [near]  # 只拉近端窗口，不是全量
    assert len(out) == len(dates) + 2
    assert out.index[0] == dates[0]  # 2000 年的历史仍在
    written = pd.read_csv(tmp_path / "stock_600519_qfq.csv",
                          index_col="date", parse_dates=True)
    assert len(written) == len(dates) + 2
    assert info["source"] == "astock_tencent" and info["from_cache"] is False
    assert info["degraded"] is False


def test_overlap_mismatch_triggers_full_refetch(tmp_path, monkeypatch):
    """重叠段不一致（除权/复权基准变化）→ 从缓存首日全量重取并整体替换。"""
    from stock_analysis.data_loader import meta

    monkeypatch.setattr(meta, "DATA_DIR", str(tmp_path))
    dates = pd.bdate_range("2000-01-03", periods=6000)
    cached = _write_cache(tmp_path, dates)
    last = dates[-1]
    near_start = (last - pd.Timedelta(days=meta.CACHE_OVERLAP_DAYS)).strftime("%Y%m%d")

    near = cached.iloc[-20:].copy()
    near[["open", "high", "low", "close"]] += 5.0  # 复权基准已变
    full_dates = dates.append(pd.bdate_range(last + pd.Timedelta(days=1), periods=2))
    replacement = _ohlcv_frame(full_dates, base=200.0)  # 从缓存首日起的全量替换

    def responder(start):
        if start == near_start:
            return near, "astock_tencent"
        return replacement, "astock_tencent"

    monkeypatch.setattr(meta, "_expected_latest_trade_date",
                        lambda now=None: full_dates[-1].date())
    pool = _RecordingPool(responder)
    monkeypatch.setattr(meta, "_STOCK_DATA_SOURCE_POOL", pool)

    out = meta.get_stock_daily("600519", start_date=None, source="akshare")
    assert pool.calls == [near_start, dates[0].strftime("%Y%m%d")]
    assert pool.calls[1] != meta.FULL_HISTORY_START  # 从缓存首日，不是 1990
    assert len(out) == len(full_dates)
    assert out["close"].iloc[0] == pytest.approx(200.0)  # 整体替换，不是拼接


def test_shorter_fetch_does_not_overwrite_cache(tmp_path, monkeypatch):
    """重取结果比旧缓存短 → 保留原缓存、报 degraded，绝不覆盖。"""
    from stock_analysis.data_loader import meta

    monkeypatch.setattr(meta, "DATA_DIR", str(tmp_path))
    dates = pd.bdate_range("2000-01-03", periods=6000)
    cached = _write_cache(tmp_path, dates)
    cache_file = tmp_path / "stock_600519_qfq.csv"
    original = cache_file.read_bytes()
    last = dates[-1]
    near_start = (last - pd.Timedelta(days=meta.CACHE_OVERLAP_DAYS)).strftime("%Y%m%d")

    near = cached.iloc[-20:].copy()
    near[["close"]] += 5.0  # 触发重叠不一致
    short = cached.iloc[-100:].copy()  # 全量重取却只有 100 行

    def responder(start):
        return (near, "astock_tencent") if start == near_start else (short, "astock_tencent")

    monkeypatch.setattr(meta, "_expected_latest_trade_date",
                        lambda now=None: (last + pd.offsets.BDay(1)).date())
    monkeypatch.setattr(meta, "_STOCK_DATA_SOURCE_POOL", _RecordingPool(responder))

    out = meta.get_stock_daily("600519", start_date=None, source="akshare")
    info = meta.last_fetch_info()
    assert cache_file.read_bytes() == original  # 一字节未改
    assert len(out) == len(dates)
    assert info["source"] == "cache" and info["from_cache"] is True
    assert info["degraded"] is True
    assert "比本地缓存短" in (info["degraded_reason"] or "")


# ---------------------------------------------------------------------------
# 覆盖范围不变量（2026-11-12）：行数不是判据
#
# 事故：缓存过期 >40 天 → 近端窗口与旧缓存无重叠/不一致 → 从缓存首日全量重取；
# 腾讯返回 6040 根（缺 2007-09-04），旧缓存 6041 根 → 旧规则「比缓存短就拒绝」
# 导致缓存末日永远不前进，每次运行都重复全量重取并 degraded。
# 正确判据是「是否覆盖缓存起始日」。
# ---------------------------------------------------------------------------
def test_no_overlap_full_refetch_merges_union_and_advances(tmp_path, monkeypatch, capsys):
    """全量重取覆盖起始日且重叠一致 → 取并集：源端缺根不丢、缓存末日前进。"""
    from stock_analysis.data_loader import meta

    monkeypatch.setattr(meta, "DATA_DIR", str(tmp_path))
    cache_dates = pd.bdate_range("2000-01-03", periods=6041)
    cached = _write_cache(tmp_path, cache_dates)
    cache_file = tmp_path / "stock_600519_qfq.csv"

    # 全量重取：起点与缓存相同，缺中间 2 根，另含 1 个新交易日 → 6040 根
    # （比 6041 根的旧缓存短，正是旧规则会误判为「截断」的形状）。
    missing = pd.DatetimeIndex([cache_dates[2500], cache_dates[4000]])
    new_day = pd.bdate_range(cache_dates[-1] + pd.Timedelta(days=1), periods=1)[0]
    full_dates = cache_dates.difference(missing).append(pd.DatetimeIndex([new_day]))
    assert len(full_dates) == len(cache_dates) - 1
    full_frame = cached.loc[cache_dates.intersection(full_dates)].copy()
    full_frame = pd.concat([full_frame, _ohlcv_frame([new_day], base=999.0)])

    # 近端窗口只返回「缓存末日之后」的一根 → 与旧缓存无重叠（None 分支）
    near = _ohlcv_frame([new_day])

    cache_first = cache_dates[0].strftime("%Y%m%d")
    near_start = (cache_dates[-1] - pd.Timedelta(days=meta.CACHE_OVERLAP_DAYS)).strftime("%Y%m%d")

    def responder(start):
        if start == cache_first:
            return full_frame, "astock_tencent"
        return near, "astock_tencent"

    monkeypatch.setattr(meta, "_expected_latest_trade_date",
                        lambda now=None: new_day.date())
    pool = _RecordingPool(responder)
    monkeypatch.setattr(meta, "_STOCK_DATA_SOURCE_POOL", pool)

    out = meta.get_stock_daily("600519", start_date=None, source="akshare")
    info = meta.last_fetch_info()

    assert pool.calls == [near_start, cache_first]  # 恰好 1 次增量 + 1 次全量重取
    assert out.index[0] == cache_dates[0]           # 起始日不变
    assert out.index[-1] == new_day                 # 缓存末日前进
    assert len(out) == len(cache_dates) + 1         # 并集 = 旧缓存 + 新交易日
    for day in missing:                             # 源端缺的两根由旧缓存保留
        assert day in out.index
    assert info["degraded"] is False
    assert info["merged_from_cache"] is True
    assert info["cache_rows_kept"] == 2
    assert info["cache_last_date"] == new_day.date().isoformat()

    written = pd.read_csv(cache_file, index_col="date", parse_dates=True)
    assert len(written) == len(out)
    assert written.index[-1] == new_day
    assert "增量合并：保留旧缓存 2 行" in capsys.readouterr().err

    # 再跑一次：缓存已新鲜 → 命中缓存，0 次请求（不再重复全量重取）
    def boom(start):
        raise AssertionError("缓存已新鲜，不应触网")

    pool2 = _RecordingPool(boom)
    monkeypatch.setattr(meta, "_STOCK_DATA_SOURCE_POOL", pool2)
    out2 = meta.get_stock_daily("600519", start_date=None, source="akshare")
    assert pool2.calls == []
    assert meta.last_fetch_info()["from_cache"] is True
    assert len(out2) == len(out)


def test_full_refetch_starting_later_keeps_cache_degraded(tmp_path, monkeypatch):
    """全量重取起始日晚于缓存首日（真正的历史截断）→ 保留原缓存 + degraded。"""
    from stock_analysis.data_loader import meta

    monkeypatch.setattr(meta, "DATA_DIR", str(tmp_path))
    cache_dates = pd.bdate_range("2000-01-03", periods=6000)
    cached = _write_cache(tmp_path, cache_dates)
    cache_file = tmp_path / "stock_600519_qfq.csv"
    original = cache_file.read_bytes()
    new_day = pd.bdate_range(cache_dates[-1] + pd.Timedelta(days=1), periods=1)[0]

    cache_first = cache_dates[0].strftime("%Y%m%d")
    near_start = (cache_dates[-1] - pd.Timedelta(days=meta.CACHE_OVERLAP_DAYS)).strftime("%Y%m%d")
    near = _ohlcv_frame([new_day])                       # 与旧缓存无重叠
    truncated = cached.iloc[-100:].copy()                # 只覆盖缓存最后 100 行

    def responder(start):
        return (truncated, "astock_tencent") if start == cache_first else (near, "astock_tencent")

    monkeypatch.setattr(meta, "_expected_latest_trade_date",
                        lambda now=None: new_day.date())
    monkeypatch.setattr(meta, "_STOCK_DATA_SOURCE_POOL", _RecordingPool(responder))

    out = meta.get_stock_daily("600519", start_date=None, source="akshare")
    info = meta.last_fetch_info()

    assert cache_file.read_bytes() == original           # 一字节未改
    assert len(out) == len(cache_dates)                  # 返回原缓存
    assert info["source"] == "cache" and info["from_cache"] is True
    assert info["degraded"] is True
    assert "晚于缓存首日" in (info["degraded_reason"] or "")
    assert info["merged_from_cache"] is False
    assert info["cache_rows_kept"] == 0


def test_basis_change_adopts_new_full_and_warns(tmp_path, monkeypatch, capsys):
    """重叠价格差 >0.01（除权/复权基准变化）→ 采用新 full，不保留旧基准，并 warning。"""
    from stock_analysis.data_loader import meta

    monkeypatch.setattr(meta, "DATA_DIR", str(tmp_path))
    cache_dates = pd.bdate_range("2000-01-03", periods=6000)
    cached = _write_cache(tmp_path, cache_dates)
    cache_file = tmp_path / "stock_600519_qfq.csv"
    last = cache_dates[-1]
    cache_first = cache_dates[0].strftime("%Y%m%d")
    near_start = (last - pd.Timedelta(days=meta.CACHE_OVERLAP_DAYS)).strftime("%Y%m%d")

    near = cached.iloc[-20:].copy()
    near[["open", "high", "low", "close"]] += 5.0       # 近端重叠段已不一致

    full_dates = cache_dates.append(
        pd.bdate_range(last + pd.Timedelta(days=1), periods=2))
    replacement = _ohlcv_frame(full_dates, base=200.0)  # 新复权基准，覆盖缓存首日

    def responder(start):
        return (near, "astock_tencent") if start == near_start else (replacement, "astock_tencent")

    monkeypatch.setattr(meta, "_expected_latest_trade_date",
                        lambda now=None: full_dates[-1].date())
    monkeypatch.setattr(meta, "_STOCK_DATA_SOURCE_POOL", _RecordingPool(responder))

    out = meta.get_stock_daily("600519", start_date=None, source="akshare")
    info = meta.last_fetch_info()

    assert len(out) == len(replacement)
    assert out["close"].iloc[0] == pytest.approx(200.0)  # 整体采用新基准
    assert info["degraded"] is False
    assert info["merged_from_cache"] is False            # 不与旧基准并集
    assert info["cache_rows_kept"] == 0
    assert "复权基准已变" in capsys.readouterr().err
    written = pd.read_csv(cache_file, index_col="date", parse_dates=True)
    assert written["close"].iloc[0] == pytest.approx(200.0)
