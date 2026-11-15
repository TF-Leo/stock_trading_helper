# -*- coding: utf-8 -*-
"""交易所官方 ST 名单备胎（离线测试）。

对应 ``source/stock_analysis/data_sources/st_exchange.py``。所有网络入口
（``st_exchange.requests.get``）都打桩，不触网。测试代码只使用公开基准/虚构
代码（600519/600000/600001/000002/688001），不出现任何真实持仓代码。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1]
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from stock_analysis.data_sources import st_exchange  # noqa: E402


class FakeResponse:
    def __init__(self, payload=None, status=200, content=b"{}", text=None):
        self._payload = payload
        self.status_code = status
        self.url = "https://example.invalid/"
        self.content = content
        self.text = text if text is not None else (
            "" if isinstance(payload, Exception) else
            json.dumps(payload, ensure_ascii=False) if payload is not None else "")

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


def _sse_payload(rows=None, total=None):
    rows = rows if rows is not None else [
        {"A_STOCK_CODE": "600001", "SEC_NAME_CN": "*ST示例", "STATE_CODE": "7"},
        {"A_STOCK_CODE": "600002", "SEC_NAME_CN": "ST示例二", "STATE_CODE": "7"},
        {"A_STOCK_CODE": "688001", "SEC_NAME_CN": "ST科创示例", "STATE_CODE": "7"},
    ]
    return {"result": rows,
            "pageHelp": {"total": len(rows) if total is None else total}}


def _szse_item(data, page_no=1, pagecount=2, recordcount=3,
               tabkey="tab1"):
    return {
        "metadata": {"catalogid": "fxjsb", "name": "风险警示股票",
                     "tabkey": tabkey, "pagesize": 20, "pageno": page_no,
                     "pagecount": pagecount, "recordcount": recordcount,
                     "subname": "2026-11-13 "},
        "data": data,
        "error": None,
    }


def _szse_pages():
    return {
        1: [_szse_item([
            {"zqdm": "000002", "gsjc": "<a href='#'><u>ST示例二</u></a>"},
            {"zqdm": "000003", "gsjc": "*ST示例三"},
        ], page_no=1)],
        2: [_szse_item([
            {"zqdm": "300001", "gsjc": "ST创业示例"},
        ], page_no=2)],
    }


def _fake_get(monkeypatch, calls, sse=None, szse=None):
    def fake(url, params=None, headers=None, timeout=None):
        calls.append((url, dict(params or {})))
        if "query.sse.com.cn" in url:
            value = sse
        elif "szse.cn" in url:
            page = int((params or {}).get("PAGENO", 1))
            value = (szse or {}).get(page)
        else:  # pragma: no cover
            raise AssertionError(f"未预期的 URL：{url}")
        if isinstance(value, Exception):
            raise value
        if isinstance(value, FakeResponse):
            return value
        return FakeResponse(value)

    monkeypatch.setattr(st_exchange.requests, "get", fake)


# ---------------------------------------------------------------------------
# SSE
# ---------------------------------------------------------------------------
def test_fetch_sse_normal_one_request(monkeypatch):
    calls = []
    _fake_get(monkeypatch, calls, sse=_sse_payload())
    snapshot = st_exchange.fetch_sse_st_list(timeout=5)

    assert len(calls) == 1
    assert snapshot.source == "sse"
    assert snapshot.requests == 1
    assert snapshot.codes == {"600001", "600002", "688001"}
    assert snapshot.rows[0]["st_type"] == "*ST"
    assert snapshot.rows[2]["market"] == "sh"
    assert snapshot.warnings == []
    assert calls[0][1]["COMPANY_STATUS"] == "7"
    assert calls[0][1]["pageHelp.pageSize"] == "2000"


def test_fetch_sse_rejects_truncated_page(monkeypatch):
    """单页上限 2000 / 全表 2319：过滤未生效时必须显式失败。"""
    calls = []
    rows = [{"A_STOCK_CODE": f"60000{i}", "SEC_NAME_CN": "ST示例",
             "STATE_CODE": "7"} for i in range(3)]
    _fake_get(monkeypatch, calls, sse=_sse_payload(rows=rows, total=2319))
    with pytest.raises(st_exchange.StExchangeError, match="不完整"):
        st_exchange.fetch_sse_st_list()


def test_fetch_sse_rejects_filter_not_applied(monkeypatch):
    calls = []
    rows = [
        {"A_STOCK_CODE": "600001", "SEC_NAME_CN": "ST示例", "STATE_CODE": "7"},
        {"A_STOCK_CODE": "600000", "SEC_NAME_CN": "浦发银行", "STATE_CODE": "2"},
    ]
    _fake_get(monkeypatch, calls, sse=_sse_payload(rows=rows))
    with pytest.raises(st_exchange.StExchangeError, match="过滤未生效"):
        st_exchange.fetch_sse_st_list()


def test_fetch_sse_empty_is_explicit_failure(monkeypatch):
    calls = []
    _fake_get(monkeypatch, calls, sse=_sse_payload(rows=[], total=0))
    with pytest.raises(st_exchange.StExchangeError, match="为空"):
        st_exchange.fetch_sse_st_list()


def test_fetch_sse_http_and_json_errors(monkeypatch):
    calls = []
    _fake_get(monkeypatch, calls, sse=FakeResponse(None, status=503,
                                                   content=b"no"))
    with pytest.raises(st_exchange.StExchangeError, match="HTTP 503"):
        st_exchange.fetch_sse_st_list()

    calls = []
    _fake_get(monkeypatch, calls,
              sse=FakeResponse(ValueError("bad json"), content=b"{not-json"))
    with pytest.raises(st_exchange.StExchangeError, match="非 JSON"):
        st_exchange.fetch_sse_st_list()


def test_fetch_sse_warns_on_name_without_st_prefix(monkeypatch):
    calls = []
    _fake_get(monkeypatch, calls, sse=_sse_payload(rows=[
        {"A_STOCK_CODE": "600001", "SEC_NAME_CN": "示例无前缀",
         "STATE_CODE": "7"}]))
    snapshot = st_exchange.fetch_sse_st_list()
    assert snapshot.codes == {"600001"}
    assert snapshot.warnings and "不含 ST 前缀" in snapshot.warnings[0]


# ---------------------------------------------------------------------------
# SZSE
# ---------------------------------------------------------------------------
def test_fetch_szse_paginates_serially_and_strips_html(monkeypatch):
    calls = []
    _fake_get(monkeypatch, calls, szse=_szse_pages())
    snapshot = st_exchange.fetch_szse_st_list(timeout=5)

    assert len(calls) == 2, "pagecount=2 → 2 次串行请求"
    assert [call[1]["PAGENO"] for call in calls] == ["1", "2"]
    assert snapshot.source == "szse"
    assert snapshot.requests == 2
    assert snapshot.session_date == "2026-11-13"
    assert snapshot.codes == {"000002", "000003", "300001"}
    names = {row["code"]: row["name"] for row in snapshot.rows}
    assert names["000002"] == "ST示例二", "HTML 标签必须去掉"
    assert names["000003"] == "*ST示例三"
    assert snapshot.warnings == []


def test_fetch_szse_incomplete_raises(monkeypatch):
    calls = []
    pages = _szse_pages()
    pages[1][0]["metadata"]["recordcount"] = 99   # 第 2 页仍报 99
    pages[2][0]["metadata"]["recordcount"] = 2    # 但只回 2 行
    _fake_get(monkeypatch, calls, szse=pages)
    with pytest.raises(st_exchange.StExchangeError, match="翻页期间发生变化"):
        st_exchange.fetch_szse_st_list()


def test_fetch_szse_sum_mismatch_raises(monkeypatch):
    calls = []
    pages = {
        1: [_szse_item([{"zqdm": "000002", "gsjc": "ST示例二"}],
                       page_no=1, pagecount=1, recordcount=3)],
    }
    _fake_get(monkeypatch, calls, szse=pages)
    with pytest.raises(st_exchange.StExchangeError, match="不完整"):
        st_exchange.fetch_szse_st_list()


def test_fetch_szse_empty_is_explicit_failure(monkeypatch):
    calls = []
    pages = {1: [_szse_item([], page_no=1, pagecount=1, recordcount=0)]}
    _fake_get(monkeypatch, calls, szse=pages)
    with pytest.raises(st_exchange.StExchangeError, match="为空"):
        st_exchange.fetch_szse_st_list()
    assert len(calls) == 1


def test_fetch_szse_missing_metadata_raises(monkeypatch):
    calls = []
    pages = {1: [{"metadata": {"tabkey": "tab1"}, "data": []}]}
    _fake_get(monkeypatch, calls, szse=pages)
    with pytest.raises(st_exchange.StExchangeError, match="pagecount"):
        st_exchange.fetch_szse_st_list()


# ---------------------------------------------------------------------------
# 两所汇总 / 合并
# ---------------------------------------------------------------------------
def _snapshot(source, rows, requests=1, coverage="x"):
    return st_exchange.ExchangeStList(
        source=source, rows=rows, url="u", coverage=coverage,
        requests=requests, fetched_at="2026-11-15T18:35:00")


def test_fetch_official_is_serial_and_records_one_failure(monkeypatch):
    order = []

    def fake_sse(timeout=None):
        order.append("sse")
        return _snapshot("sse", [{"code": "600001", "name": "*ST示例",
                                  "st_type": "*ST", "market": "sh",
                                  "source": "sse"}])

    def fake_szse(timeout=None):
        order.append("szse")
        raise st_exchange.StExchangeError("深交所挂了", requests=3)

    monkeypatch.setattr(st_exchange, "fetch_sse_st_list", fake_sse)
    monkeypatch.setattr(st_exchange, "fetch_szse_st_list", fake_szse)
    result = st_exchange.fetch_official_st_lists()

    assert order == ["sse", "szse"], "必须串行"
    assert [item.source for item in result.lists] == ["sse"]
    assert result.errors == [{"source": "szse", "error": "深交所挂了",
                              "requests": 3}]
    assert result.requests == 4


def test_fetch_official_catches_unexpected_exception(monkeypatch):
    monkeypatch.setattr(st_exchange, "fetch_sse_st_list",
                        lambda timeout=None: (_ for _ in ()).throw(
                            RuntimeError("boom")))
    monkeypatch.setattr(st_exchange, "fetch_szse_st_list",
                        lambda timeout=None: _snapshot("szse", [
                            {"code": "000002", "name": "ST示例二",
                             "st_type": "ST", "market": "sz",
                             "source": "szse"}], requests=6))
    result = st_exchange.fetch_official_st_lists()
    assert result.errors[0]["source"] == "sse"
    assert "boom" in result.errors[0]["error"]
    assert result.requests == 6


def test_merge_lists_union_and_name_conflict():
    sse = _snapshot("sse", [
        {"code": "600001", "name": "*ST示例", "st_type": "*ST",
         "market": "sh", "source": "sse"},
        {"code": "600002", "name": "ST示例二", "st_type": "ST",
         "market": "sh", "source": "sse"},
    ], coverage=st_exchange.SSE_COVERAGE)
    szse = _snapshot("szse", [
        {"code": "600001", "name": "ST示例改名", "st_type": "ST",
         "market": "sh", "source": "szse"},
        {"code": "000002", "name": "ST深市示例", "st_type": "ST",
         "market": "sz", "source": "szse"},
    ], requests=6, coverage=st_exchange.SZSE_COVERAGE)

    rows, coverage, warning = st_exchange.merge_lists([sse, szse])
    assert [row["code"] for row in rows] == ["000002", "600001", "600002"]
    assert coverage == "沪深A（官方，不含北交所/B股）"
    assert warning and "600001" in warning and "*ST 优先" in warning
    kept = {row["code"]: row for row in rows}["600001"]
    assert kept["name"] == "*ST示例" and kept["st_type"] == "*ST"


def test_merge_to_frame_attrs_and_market_of():
    frame = st_exchange.merge_to_frame([_snapshot("sse", [
        {"code": "600001", "name": "*ST示例", "st_type": "*ST",
         "market": "sh", "source": "sse"}], coverage="沪A")])
    assert list(frame.columns) == ["code", "name", "st_type", "market", "source"]
    assert frame.attrs["coverage"] == "沪A（官方，不含B股）"
    assert st_exchange.market_of("830001") == "bj"
    assert st_exchange.market_of("600519") == "sh"
    assert st_exchange.market_of("000002") == "sz"
    assert st_exchange.market_of("920001") == "bj"