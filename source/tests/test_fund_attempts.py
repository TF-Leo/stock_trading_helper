# -*- coding: utf-8 -*-
"""P0（issue #24）：基金 ETF / OTC 通道失败逐通道可见。

离线用例，覆盖：
  * ETF 调用失败（异常）→ OTC 成功：attempts 留 ETF 失败 + OTC 成功，warnings 可读；
  * 场外基金常态（ETF 0 行）→ OTC 成功：attempts 留痕但 warnings **不新增噪声**；
  * ETF + OTC 全失败、有旧缓存 → degraded + 完整 attempts；
  * 全失败且无缓存 → 返回 None，错误响应 exit 30 语义 / attempts 同构；
  * 基金错误文本复用 bars 的脱敏/截断（URL、token 不泄露）；
  * resolve_and_fetch auto 双向探测时，基金 attempts 与股票 attempts 合并不丢。
"""
import datetime as dt
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd
import pytest
import requests

from stock_analysis.data_loader import bars, meta


def _otc(n=30, start="2020-01-01"):
    """东财 OTC 单位净值走势的原始列（净值日期 / 单位净值）。"""
    dates = pd.bdate_range(start, periods=n)
    return pd.DataFrame({
        "净值日期": dates,
        "单位净值": [1.0 + i * 0.001 for i in range(n)],
    })


def _etf(n=30, start="2020-01-01"):
    """新浪场内 ETF 的原始列（日期 + OHLCV）。"""
    dates = pd.bdate_range(start, periods=n)
    close = [1.0 + i * 0.002 for i in range(n)]
    return pd.DataFrame({
        "日期": dates, "开盘": close, "最高": [c + 0.01 for c in close],
        "最低": [c - 0.01 for c in close], "收盘": close,
        "成交量": [10000 + i for i in range(n)],
    })


def _cache(n=30, start="2020-01-01"):
    """已落盘的基金缓存（OHLCV + date 索引）。"""
    frame = _etf(n, start)
    frame = frame.rename(columns={
        "日期": "date", "开盘": "open", "最高": "high",
        "最低": "low", "收盘": "close", "成交量": "volume",
    }).set_index("date")
    return frame


@pytest.fixture
def fund_env(tmp_path, monkeypatch):
    """隔离缓存目录 + 让「期望交易日」在未来（确保走触网分支）。"""
    monkeypatch.setattr(meta, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(meta, "_expected_latest_trade_date",
                        lambda now=None: dt.date(2030, 1, 1))
    import akshare as ak
    yield ak, tmp_path, monkeypatch


def test_etf_failure_then_otc_success(fund_env):
    ak, _, monkeypatch = fund_env
    secret = "TOPSECRET123"

    def etf_fail(**kwargs):
        raise requests.HTTPError(
            "429 Client Error: Too Many Requests for url: "
            f"https://push2.example.com/api?token={secret}")

    monkeypatch.setattr(ak, "fund_etf_hist_sina", etf_fail)
    monkeypatch.setattr(ak, "fund_open_fund_info_em", lambda **k: _otc())

    df = meta.get_fund_daily("023299")
    assert df is not None and len(df) >= 20

    attempts = meta.last_fetch_info()["source_attempts"]
    assert [a["source"] for a in attempts] == ["fund_etf_sina", "eastmoney_otc"]
    assert attempts[0]["ok"] is False
    assert attempts[0]["error_class"] == "HTTPError"
    # 脱敏：token 与绝对 URL 都不进对外 JSON
    assert secret not in attempts[0]["error_text"]
    assert "https://" not in attempts[0]["error_text"]
    assert "<url>" in attempts[0]["error_text"]
    assert attempts[1]["ok"] is True

    lines = bars.format_attempt_warnings(attempts)
    assert any("fund_etf_sina 失败" in x for x in lines), lines
    assert not any("eastmoney_otc" in x for x in lines), lines


def test_otc_fund_etf_empty_is_not_warning_noise(fund_env):
    """023299/001917 这类场外基金：ETF 通道常态 0 行，正常路径 warning 不增噪。"""
    ak, _, monkeypatch = fund_env
    monkeypatch.setattr(ak, "fund_etf_hist_sina", lambda **k: pd.DataFrame())
    monkeypatch.setattr(ak, "fund_open_fund_info_em", lambda **k: _otc())

    df = meta.get_fund_daily("023299")
    assert df is not None and len(df) >= 20

    attempts = meta.last_fetch_info()["source_attempts"]
    # 逐次留痕：ETF 空数据（EmptyDataError，不是故障）+ OTC 成功
    assert [a["source"] for a in attempts] == ["fund_etf_sina", "eastmoney_otc"]
    assert attempts[0]["ok"] is False
    assert attempts[0]["error_class"] == "EmptyDataError"
    assert attempts[1]["ok"] is True
    # 同轮已有通道成功 → 空数据不当作噪声输出
    assert bars.format_attempt_warnings(attempts) == []


def test_etf_success_skips_otc(fund_env):
    ak, _, monkeypatch = fund_env

    def otc_must_not_run(**kwargs):  # ETF 已成功，OTC 不应被调用
        raise AssertionError("OTC 通道不应在 ETF 成功后被调用")

    monkeypatch.setattr(ak, "fund_etf_hist_sina", lambda **k: _etf())
    monkeypatch.setattr(ak, "fund_open_fund_info_em", otc_must_not_run)

    df = meta.get_fund_daily("510300")
    assert df is not None and len(df) >= 20
    attempts = meta.last_fetch_info()["source_attempts"]
    assert attempts == [{
        "source": "fund_etf_sina", "ok": True, "error_class": None,
        "error_text": None, "cooldown": False, "removed": False,
    }]
    assert bars.format_attempt_warnings(attempts) == []


def test_both_fail_with_cache_degrades_with_full_attempts(fund_env):
    ak, tmp_path, monkeypatch = fund_env
    _cache().to_csv(tmp_path / "fund_023299.csv")

    def etf_fail(**kwargs):
        raise requests.Timeout("read timed out")

    def otc_fail(**kwargs):
        raise ConnectionError("connection refused")

    monkeypatch.setattr(ak, "fund_etf_hist_sina", etf_fail)
    monkeypatch.setattr(ak, "fund_open_fund_info_em", otc_fail)

    out = meta.get_fund_daily("023299")
    assert out is not None and len(out) == 30          # 回退旧缓存

    info = meta.last_fetch_info()
    assert info["source"] == "cache"
    assert info["from_cache"] is True
    assert info["degraded"] is True                    # → data.stale
    assert [a["source"] for a in info["source_attempts"]] == [
        "fund_etf_sina", "eastmoney_otc"]
    assert all(a["ok"] is False for a in info["source_attempts"])

    lines = bars.format_attempt_warnings(info["source_attempts"])
    assert any("fund_etf_sina 失败" in x and "Timeout" in x for x in lines)
    assert any("eastmoney_otc 失败" in x and "ConnectionError" in x for x in lines)


def test_both_fail_no_cache_returns_none_and_error_response(fund_env):
    ak, _, monkeypatch = fund_env
    monkeypatch.setattr(ak, "fund_etf_hist_sina", lambda **k: pd.DataFrame())
    monkeypatch.setattr(ak, "fund_open_fund_info_em", lambda **k: pd.DataFrame())

    assert meta.get_fund_daily("023299") is None

    info = meta.last_fetch_info()
    assert info["source"] is None
    assert info["from_cache"] is False
    assert info["degraded"] is False
    assert len(info["source_attempts"]) == 2

    from decision.schema import create_error_response
    doc = create_error_response(
        "data_source_error", "基金行情拉取失败（ETF/OTC 均无数据且无可用缓存）",
        "023299", "fund", type_hint="fund", fetch_info=info)
    assert doc["schema_version"] == "1.0"
    assert doc["data"]["source"] is None
    assert doc["data"]["stale"] is False
    assert len(doc["diagnostics"]["source_attempts"]) == 2
    joined = "\n".join(doc["diagnostics"]["warnings"])
    assert "fund_etf_sina 失败" in joined
    assert "eastmoney_otc 失败" in joined


def test_fund_error_text_truncated(fund_env):
    ak, _, monkeypatch = fund_env
    long_text = "api_key=topsecret https://a.example/p?q=1 " + "x" * 500

    def etf_fail(**kwargs):
        raise RuntimeError(long_text)

    monkeypatch.setattr(ak, "fund_etf_hist_sina", etf_fail)
    monkeypatch.setattr(ak, "fund_open_fund_info_em", lambda **k: _otc())

    meta.get_fund_daily("023299")
    attempt = meta.last_fetch_info()["source_attempts"][0]
    assert len(attempt["error_text"]) <= bars.MAX_ATTEMPT_ERROR_TEXT
    assert "topsecret" not in attempt["error_text"]
    assert "https://" not in attempt["error_text"]


# ---------------------------------------------------------------------------
# resolve_and_fetch auto：股票探测失败 + 基金探测失败 → 两侧 attempts 合并
# ---------------------------------------------------------------------------
class _AttemptPool:
    def __init__(self, payload, attempts):
        self.payload = payload
        self.last_attempts = attempts

    def fetch(self, symbol, start_date, adjust, preferred=None):
        return self.payload, None


_STOCK_FAILED_ATTEMPTS = [
    {"source": "astock_tencent", "ok": False, "error_class": "AstockError",
     "error_text": "forced failure", "cooldown": False, "removed": False},
    {"source": "eastmoney", "ok": False, "error_class": "ConnectionError",
     "error_text": "connection refused", "cooldown": False, "removed": False},
    {"source": "sina_stock", "ok": False, "error_class": "Timeout",
     "error_text": "read timed out", "cooldown": False, "removed": False},
]


def test_resolve_and_fetch_merges_fund_attempts(tmp_path, monkeypatch):
    monkeypatch.setattr(meta, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(meta, "_expected_latest_trade_date",
                        lambda now=None: dt.date(2030, 1, 1))
    monkeypatch.setattr(meta, "_STOCK_DATA_SOURCE_POOL",
                        _AttemptPool(None, list(_STOCK_FAILED_ATTEMPTS)))

    import akshare as ak
    monkeypatch.setattr(ak, "fund_etf_hist_sina", lambda **k: pd.DataFrame())
    monkeypatch.setattr(ak, "fund_open_fund_info_em", lambda **k: pd.DataFrame())

    df, dtype, err = meta.resolve_and_fetch("023299", type_hint="auto")
    assert df is None and dtype == "stock" and err
    sources = [a["source"] for a in meta.last_fetch_info()["source_attempts"]]
    assert sources == ["astock_tencent", "eastmoney", "sina_stock",
                       "fund_etf_sina", "eastmoney_otc"]
