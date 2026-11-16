# -*- coding: utf-8 -*-
"""交易所官方风险警示名单（ST / *ST）—— 东财不可用时的备胎。

为什么要有这个模块
------------------
2026-11-15 起东财 ``push2`` / ``push2delay`` 对本机出口 IP 触发 nginx 444，
已持续约 40 小时。ST 是硬风险，不能长期靠报价名称猜。本模块只取**交易所
官方**公开数据（零鉴权、无代理），供 ``tools/precompute_st_list.py`` 在
东财失效时兜底。

端点取证（2026-11-15，本机 curl；每所 3 次，含页面发现与 1 次数据 API）
--------------------------------------------------------------------
上交所（SSE）
    * ``GET https://www.sse.com.cn/disclosure/listedinfo/riskplate/`` →
      **HTTP 200**；页面标题「风险警示板股票列表」，数据由
      ``search_listedCompanyInfo_2021.js`` 填充（sqlId 不在 HTML 内）。
    * ``GET https://query.sse.com.cn/sseQuery/commonQuery.do``
      （``sqlId=COMMON_SSE_CP_GPJCTPZ_GPLB_GP_L``，``STOCK_TYPE=1,8``，
      ``COMPANY_STATUS=2,4,5,7,8``）→ **HTTP 200 / 2,293,163 B JSON**，
      未过滤全表 ``pageHelp.total=2319``、单页上限 **2000** 行；字段
      ``A_STOCK_CODE`` / ``SEC_NAME_CN`` / ``STATE_CODE`` / ``LIST_BOARD``。
      2026-11-16 复核（``COMPANY_STATUS=7`` 过滤后）：``pageHelp.total=80``、
      ``pageCount=1``、``pageSize=2000`` —— 单页装得下，**没有截断**；
      80 行全部 ``STATE_CODE=7``，其中名称带 ``ST``/``*ST`` 的也是 **80 行**
      （含 **12 只科创板 688**；2026-11-15 首测记的 78/10 是旧快照）。
      因此本模块用 ``COMPANY_STATUS=7`` 只取风险警示状态，并用
      ``len(result)==pageHelp.total``、``total<=pageSize``、
      ``pageCount==1`` 与「每行 STATE_CODE 必须为 7」多重校验：过滤若未
      生效（返回 2000/2319 的全表）或将来超过单页上限就显式失败，绝不当成
      完整名单。
    * 覆盖：沪市 A 股（主板 600/601/603/605 + 科创板 688），不含 B 股。

深交所（SZSE）
    * ``GET https://www.szse.cn/disclosure/listed/warn/index.html`` →
      **HTTP 200**；页面内 ``<div class="report-container"
      data-s-catalog-id="fxjsb">``。
    * ``GET https://www.szse.cn/api/report/ShowReport/data``
      （``SHOWTYPE=JSON&CATALOGID=fxjsb&TABKEY=tab1&PAGENO=1``）→
      **HTTP 200 / 3,915 B JSON**；响应是**两个数据块**：
      ``tab1`` = ``风险警示股票``、``tab2`` = ``退市整理股票``。``tab1`` 的
      ``metadata`` 给出 ``pagesize=20``、``pagecount=6``、
      ``recordcount=115``（2026-11-16 复核仍为 6 页/115 条）；行字段只有
      ``zqdm``（证券代码）与 ``gsjc``（公司简称，值里含 ``<a><u>…</u></a>``
      HTML）。``PAGESIZE`` 参数被服务端忽略（仍固定 20/页），因此本模块
      **串行翻页**并在取完后校验 ``sum(rows)==recordcount``；不完整就显式失败。
      取块时**必须**按 ``metadata.tabkey`` 命中 ``tab1``：早期实现在「顶层
      只有一个块」时直接取 ``payload[0]``，若服务端只回 ``tab2`` 就会把
      退市整理名单静默当成风险警示名单。
    * 覆盖：深市 A 股（000/001/002/003/300/301）**加 B 股**
      （``200xxx``，2026-11-16 实测 115 条中含 ``200056 *ST皇庭B``、
      ``200488 ST晨鸣B`` 两只）。B 股在深交所风险警示板内，官方口径就是
      「风险警示股票」，所以**保留**：它们被写进名单后 ``is_st`` 只会对这些
      代码生效，而本项目当前只交易 A 股，B 股代码不会进入候选池；若将来纳入
      B 股，``is_st=True`` 是正确语义（B 股同样适用风险警示）。

设计约束
--------
* 每个源**一次逻辑取数**（SSE 1 个 HTTP 请求；SZSE 按 ``pagecount`` 串行
  翻页，当前 6 个请求），**不重试**。
* 空表 / 结构变化 / 非 2xx / 非 JSON / 分页不完整一律抛
  :class:`StExchangeError`，**不返回空表**（空表不能被当成「今日无 ST」）。
* 只依赖 ``requests`` + ``pandas``（仓库既有依赖），不引入新依赖。
* 导入本模块不发起任何网络请求。
"""
from __future__ import annotations

import html as _html
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Iterable, List, Optional

import pandas as pd
import requests

SSE_URL = "https://query.sse.com.cn/sseQuery/commonQuery.do"
SSE_REFERER = "https://www.sse.com.cn/assortment/stock/list/share/"
SSE_SQL_ID = "COMMON_SSE_CP_GPJCTPZ_GPLB_GP_L"
# 2026-11-16 实测（COMPANY_STATUS=7）：pageHelp.total=80 / pageCount=1 /
# pageSize=2000，80 行全部 STATE_CODE=7，名称均带 ST/*ST（含 12 只 688）。
# COMMON_SSE_CP_GPJCTPZ_GPLB_GP_L 的 COMPANY_STATUS 取值范围与 akshare 一致
# （2,4,5,7,8），7 即风险警示。
SSE_RISK_STATUS = "7"
SSE_PAGE_SIZE = 2000          # 服务端硬上限：请求 10000 也只回 2000
SSE_COVERAGE = "沪A（官方，主板+科创板，不含B股）"

SZSE_URL = "https://www.szse.cn/api/report/ShowReport/data"
SZSE_REFERER = "https://www.szse.cn/disclosure/listed/warn/index.html"
SZSE_CATALOG_ID = "fxjsb"     # 风险警示板页面 data-s-catalog-id
SZSE_TABKEY = "tab1"          # tab1=风险警示股票；tab2=退市整理股票
SZSE_COVERAGE = "深A+B（官方，风险警示板含 B 股）"
SZSE_MAX_PAGES = 50           # 防御：官方若改成分页爆炸，不在探测上加码

DEFAULT_TIMEOUT = 20.0
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")

_ST_PREFIX = re.compile(r"^(?:N|S)?\*?ST", re.IGNORECASE)


class StExchangeError(RuntimeError):
    """交易所官方名单不可用（网络 / 结构 / 空表 / 分页不完整）。

    ``requests`` 记录失败前已经发出的请求数，供预计算统计（失败不重试）。
    """

    def __init__(self, message: str, requests: int = 0) -> None:
        super().__init__(message)
        self.requests = int(requests)


@dataclass
class ExchangeStList:
    """一个交易所的官方风险警示名单快照。"""

    source: str                     # "sse" / "szse"
    rows: List[dict]                # code / name / st_type / market / source
    url: str
    coverage: str
    requests: int
    fetched_at: str
    session_date: Optional[str] = None
    warnings: List[str] = field(default_factory=list)

    @property
    def codes(self) -> frozenset:
        return frozenset(row["code"] for row in self.rows)


@dataclass
class OfficialListsResult:
    """两所取数汇总（单个源失败不抛给调用方，由调用方决定是否落盘）。"""

    lists: List[ExchangeStList]
    errors: List[dict]              # [{"source": ..., "error": ...}]
    requests: int


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _timeout(value) -> float:
    return DEFAULT_TIMEOUT if value is None else float(value)


def _request_json(url: str, params: dict, referer: str, timeout: float,
                  source: str, requests_made: int = 1):
    """一次 GET → JSON；不重试，任何异常都归类为 StExchangeError。"""
    try:
        response = requests.get(
            url, params=params, timeout=timeout,
            headers={"User-Agent": _UA, "Referer": referer})
    except requests.RequestException as exc:
        raise StExchangeError(
            f"{source}官方接口请求失败：{type(exc).__name__}: {exc}",
            requests_made) from exc
    if response.status_code != 200:
        raise StExchangeError(
            f"{source}官方接口返回 HTTP {response.status_code}"
            f"（{response.url}）", requests_made)
    content = getattr(response, "content", b"") or b""
    if not content:
        raise StExchangeError(f"{source}官方接口返回空响应体", requests_made)
    try:
        return response.json()
    except ValueError as exc:
        text = (getattr(response, "text", "") or "")[:200]
        raise StExchangeError(
            f"{source}官方接口返回非 JSON（{text!r}）：{exc}",
            requests_made) from exc


def _has_st_prefix(name: str) -> bool:
    return bool(_ST_PREFIX.match(str(name or "").strip().upper()))


def _st_type(name: str) -> str:
    upper = str(name or "").strip().upper()
    return "*ST" if upper.startswith("*ST") or upper.startswith("S*ST") \
        or upper.startswith("N*ST") else "ST"


def _clean_name(raw) -> str:
    """去掉深交所 ``gsjc`` 里的 ``<a><u>…</u></a>`` 与 HTML 实体。"""
    text = re.sub(r"<[^>]*>", "", str(raw or ""))
    text = _html.unescape(text)
    return re.sub(r"\s+", "", text).strip()


def _session_date(raw) -> Optional[str]:
    text = str(raw or "").strip()
    match = re.search(r"(\d{4})-(\d{2})-(\d{2})", text)
    if match:
        return "-".join(match.groups())
    match = re.search(r"(\d{4})(\d{2})(\d{2})", text)
    return "-".join(match.groups()) if match else None


def _add_row(rows: List[dict], seen: dict, code: str, name: str, market: str,
             source: str, where: str) -> None:
    if not re.fullmatch(r"\d{6}", code):
        raise StExchangeError(f"{where}代码异常：{code!r}")
    if not name:
        raise StExchangeError(f"{where}{code} 缺少名称，无法复核")
    if code in seen:
        if seen[code]["name"] != name:
            raise StExchangeError(
                f"{where}代码 {code} 重复且内容冲突"
                f"（{seen[code]['name']!r} vs {name!r}）")
        return
    row = {"code": code, "name": name, "st_type": _st_type(name),
           "market": market, "source": source}
    seen[code] = row
    rows.append(row)


def fetch_sse_st_list(timeout=None) -> ExchangeStList:
    """上交所风险警示名单（1 个 HTTP 请求）。

    先用 ``COMPANY_STATUS=7`` 只取风险警示状态；再校验
    ``len(result)==pageHelp.total``、``total<=pageSize`` 与 ``pageCount==1``
    且每行 ``STATE_CODE=='7'``。过滤若未生效（单页上限 2000、全表 2319）或
    风险警示数将来超过单页上限，都会显式失败，不会把截断名单当成完整名单。
    """
    params = {
        "STOCK_TYPE": "1,8",             # 主板 A 股 + 科创板
        "REG_PROVINCE": "",
        "CSRC_CODE": "",
        "STOCK_CODE": "",
        "sqlId": SSE_SQL_ID,
        "COMPANY_STATUS": SSE_RISK_STATUS,
        "type": "inParams",
        "isPagination": "true",
        "pageHelp.cacheSize": "1",
        "pageHelp.beginPage": "1",
        "pageHelp.pageSize": str(SSE_PAGE_SIZE),
        "pageHelp.pageNo": "1",
        "pageHelp.endPage": "1",
    }
    payload = _request_json(SSE_URL, params, SSE_REFERER, _timeout(timeout),
                            "上交所", requests_made=1)
    if not isinstance(payload, dict):
        raise StExchangeError("上交所名单返回结构异常：顶层不是 JSON 对象", 1)
    result = payload.get("result")
    page_help = payload.get("pageHelp")
    if not isinstance(result, list) or not isinstance(page_help, dict):
        raise StExchangeError(
            "上交所名单返回结构异常：缺少 result[] 或 pageHelp", 1)
    try:
        total = int(page_help.get("total"))
    except (TypeError, ValueError) as exc:
        raise StExchangeError(
            f"上交所名单缺少 pageHelp.total（{page_help.get('total')!r}）",
            1) from exc
    if total <= 0:
        raise StExchangeError(
            "上交所风险警示名单为空（total=0）：不能当成「今日没有 ST」", 1)
    # 单页上限（2026-11-16 实测 pageHelp.pageSize=2000、pageCount=1）。本模块
    # 只取第 1 页，若风险警示数超过单页上限，返回的必然是截断名单：显式失败，
    # 不靠「len==total」兜底（服务端可能把 total 也改成当页行数）。
    page_size = page_help.get("pageSize")
    page_count = page_help.get("pageCount")
    try:
        page_size = int(page_size)
        page_count = int(page_count)
    except (TypeError, ValueError):
        page_size = page_count = None
    if page_size is not None and total > page_size:
        raise StExchangeError(
            f"上交所名单 total={total} 超过单页上限 {page_size}："
            "需要分页才能取全，拒绝把第 1 页当成完整名单", 1)
    if page_count is not None and page_count > 1:
        raise StExchangeError(
            f"上交所名单 pageCount={page_count}>1：需要分页才能取全，"
            "拒绝把第 1 页当成完整名单", 1)
    if len(result) != total:
        raise StExchangeError(
            f"上交所名单不完整：返回 {len(result)} 行 / 自报 total={total}"
            f"（单页上限 {SSE_PAGE_SIZE}；COMPANY_STATUS={SSE_RISK_STATUS} "
            "过滤可能未生效）", 1)

    rows: List[dict] = []
    seen: dict = {}
    for index, rec in enumerate(result):
        if not isinstance(rec, dict):
            raise StExchangeError(f"上交所名单第 {index} 行不是对象", 1)
        code = str(rec.get("A_STOCK_CODE") or "").strip()
        name = str(rec.get("SEC_NAME_CN") or "").strip()
        state = str(rec.get("STATE_CODE") or "").strip()
        if state != SSE_RISK_STATUS:
            raise StExchangeError(
                f"上交所 COMPANY_STATUS={SSE_RISK_STATUS} 过滤未生效："
                f"{code or '?'} 的 STATE_CODE={state or '?'}（应为 "
                f"{SSE_RISK_STATUS}），拒绝把混合名单当成风险警示名单", 1)
        _add_row(rows, seen, code, name, "sh", "sse", "上交所名单")

    warnings = []
    no_prefix = [row["code"] for row in rows if not _has_st_prefix(row["name"])]
    if no_prefix:
        warnings.append(
            f"上交所风险警示名单有 {len(no_prefix)} 行名称不含 ST 前缀"
            f"（样例 {no_prefix[:5]}）；按官方风险警示状态仍计为 ST")
    return ExchangeStList(
        source="sse", rows=rows, url=SSE_URL, coverage=SSE_COVERAGE,
        requests=1, fetched_at=_now_iso(), warnings=warnings)


def fetch_szse_st_list(timeout=None) -> ExchangeStList:
    """深交所风险警示名单（按 ``metadata.pagecount`` 串行翻页，不重试）。

    ``PAGESIZE`` 实测被忽略（固定 20/页）。响应顶层通常同时含 ``tab1``
    （风险警示股票）与 ``tab2``（退市整理股票），**每一页**都必须按
    ``metadata.tabkey=='tab1'`` 命中，否则显式失败（issue #123）。取完必须
    满足 ``sum(rows)==metadata.recordcount``，否则显式失败。
    """
    timeout = _timeout(timeout)
    rows: List[dict] = []
    seen: dict = {}
    warnings: List[str] = []
    page_no = 1
    pagecount = recordcount = None
    session_date = None

    while True:
        params = {
            "SHOWTYPE": "JSON",
            "CATALOGID": SZSE_CATALOG_ID,
            "TABKEY": SZSE_TABKEY,
            "PAGENO": str(page_no),
            # 实测被忽略；保留以表达「一次取全」的意图，服务端若支持会提速。
            "PAGESIZE": "1000",
            "random": "0.4242424242",
        }
        payload = _request_json(SZSE_URL, params, SZSE_REFERER, timeout,
                                "深交所", requests_made=page_no)
        if not isinstance(payload, list) or not payload:
            raise StExchangeError(
                f"深交所名单返回结构异常（第 {page_no} 页）：顶层不是非空数组",
                page_no)
        item = None
        for candidate in payload:
            metadata = candidate.get("metadata") if isinstance(candidate, dict) \
                else None
            if isinstance(metadata, dict) and str(
                    metadata.get("tabkey") or "").lower() == SZSE_TABKEY:
                item = candidate
                break
        # 不匹配时的**单块回退已删除**（issue #123）：响应顶层通常同时含
        # tab1（风险警示股票）与 tab2（退市整理股票）；早期实现只要顶层恰好
        # 一个块就直接吃 payload[0]，服务端若只回 tab2 就会静默把「退市整理」
        # 当成「风险警示」。现在 tabkey 不符即显式失败。
        if item is None:
            tabkeys = [
                str((candidate.get("metadata") or {}).get("tabkey") or "?")
                if isinstance(candidate, dict) else "?"
                for candidate in payload]
            raise StExchangeError(
                f"深交所名单找不到 TABKEY={SZSE_TABKEY} 的数据块"
                f"（第 {page_no} 页；响应含 tabkey={tabkeys}）："
                "拒绝把另一张表当成风险警示名单", page_no)
        metadata = item.get("metadata")
        data = item.get("data")
        if not isinstance(metadata, dict) or not isinstance(data, list):
            raise StExchangeError(
                f"深交所名单返回结构异常（第 {page_no} 页）："
                "缺少 metadata 或 data[]", page_no)
        try:
            current_pages = int(metadata["pagecount"])
            current_records = int(metadata["recordcount"])
        except (KeyError, TypeError, ValueError) as exc:
            raise StExchangeError(
                f"深交所名单缺少 pagecount/recordcount（第 {page_no} 页）",
                page_no) from exc

        if page_no == 1:
            pagecount, recordcount = current_pages, current_records
            session_date = _session_date(metadata.get("subname"))
            if recordcount <= 0:
                raise StExchangeError(
                    "深交所风险警示名单为空（recordcount=0）："
                    "不能当成「今日没有 ST」", 1)
            if pagecount <= 0:
                raise StExchangeError(
                    f"深交所名单 pagecount={pagecount} 非法", 1)
            if pagecount > SZSE_MAX_PAGES:
                raise StExchangeError(
                    f"深交所名单 pagecount={pagecount} 超过安全上限 "
                    f"{SZSE_MAX_PAGES}，拒绝继续翻页", 1)
        elif (current_pages, current_records) != (pagecount, recordcount):
            raise StExchangeError(
                f"深交所名单翻页期间发生变化：第 1 页 "
                f"({pagecount}/{recordcount})，第 {page_no} 页 "
                f"({current_pages}/{current_records})", page_no)

        before = len(rows)
        for index, rec in enumerate(data):
            if not isinstance(rec, dict):
                raise StExchangeError(
                    f"深交所名单第 {page_no} 页第 {index} 行不是对象", page_no)
            code = str(rec.get("zqdm") or rec.get("agdm") or "").strip()
            name = _clean_name(rec.get("gsjc") or rec.get("zqjc"))
            if not code:
                raise StExchangeError(
                    f"深交所名单第 {page_no} 页第 {index} 行缺少 zqdm", page_no)
            _add_row(rows, seen, code.zfill(6), name, "sz", "szse",
                     "深交所名单")
        if len(rows) == before:
            raise StExchangeError(
                f"深交所名单第 {page_no}/{pagecount} 页为空", page_no)
        if page_no >= pagecount:
            break
        page_no += 1

    if len(rows) != recordcount:
        raise StExchangeError(
            f"深交所名单不完整：共取 {len(rows)} 行 / 自报 "
            f"recordcount={recordcount}", page_no)
    no_prefix = [row["code"] for row in rows if not _has_st_prefix(row["name"])]
    if no_prefix:
        warnings.append(
            f"深交所风险警示名单有 {len(no_prefix)} 行名称不含 ST 前缀"
            f"（样例 {no_prefix[:5]}）；按官方风险警示状态仍计为 ST")
    return ExchangeStList(
        source="szse", rows=rows, url=SZSE_URL, coverage=SZSE_COVERAGE,
        requests=page_no, fetched_at=_now_iso(), session_date=session_date,
        warnings=warnings)


def fetch_official_st_lists(timeout=None) -> OfficialListsResult:
    """串行取两所名单；单个源失败只记录，不抛给调用方。

    两所各调用一次（每次内部最多 1 次逻辑取数，SZSE 按官方分页串行翻页）。
    """
    lists: List[ExchangeStList] = []
    errors: List[dict] = []
    requests_made = 0
    for source, fetcher in (("sse", fetch_sse_st_list),
                            ("szse", fetch_szse_st_list)):
        try:
            snapshot = fetcher(timeout=timeout)
            lists.append(snapshot)
            requests_made += snapshot.requests
        except StExchangeError as exc:
            requests_made += exc.requests
            errors.append({"source": source, "error": str(exc),
                           "requests": exc.requests})
        except Exception as exc:  # 防御：不让意外异常吞掉另一所的结果
            errors.append({"source": source,
                           "error": f"{type(exc).__name__}: {exc}",
                           "requests": 0})
    return OfficialListsResult(lists=lists, errors=errors,
                              requests=requests_made)


def merge_lists_detailed(lists: Iterable[ExchangeStList]):
    """合并两所名单，并返回**结构化**冲突条目。

    返回 ``(rows, coverage, warning, conflicts)``。``conflicts`` 是条目列表
    （issue #123）：``{"kind": "name_mismatch", "count": n, "codes": [...],
    "message": "..."}``；没有任何冲突时为空列表。``warning`` 是给人看的
    汇总文案（保持原措辞）。
    """
    lists = list(lists)
    if not lists:
        raise ValueError("merge_lists 需要至少一个官方名单")
    merged: dict = {}
    name_conflicts: List[str] = []
    for snapshot in lists:
        for row in snapshot.rows:
            code = row["code"]
            current = merged.get(code)
            if current is None:
                merged[code] = dict(row)
                continue
            if current["name"] != row["name"]:
                name_conflicts.append(code)
                if row["st_type"] == "*ST" and current["st_type"] != "*ST":
                    merged[code] = dict(row)
    rows = [merged[code] for code in sorted(merged)]
    sources = {snapshot.source for snapshot in lists}
    coverage = "沪深A+B（官方，不含北交所）" if {"sse", "szse"} <= sources \
        else "沪A（官方，不含B股）" if "sse" in sources \
        else "深A+B（官方，风险警示板含 B 股）"
    conflicts: List[dict] = []
    warning = None
    if name_conflicts:
        codes = sorted(set(name_conflicts))
        message = (f"官方两源有 {len(codes)} 个代码名称不一致"
                   f"（样例 {codes[:8]}）；按「是 ST」取并集，*ST 优先")
        conflicts.append({"kind": "name_mismatch", "count": len(codes),
                          "codes": codes, "message": message})
        warning = message
    return rows, coverage, warning, conflicts


def merge_lists(lists: Iterable[ExchangeStList]):
    """合并两所名单：按代码取并集（任一源说是 ST 就是 ST）。

    返回 ``(rows, coverage, warning)``。同代码名称不一致时保留 ``*ST`` 优先，
    并给出告警；只在一所出现的代码本来就不重叠（沪/深），不算冲突。
    需要结构化冲突条目时用 :func:`merge_lists_detailed`。
    """
    rows, coverage, warning, _conflicts = merge_lists_detailed(lists)
    return rows, coverage, warning


def merge_to_frame(lists: Iterable[ExchangeStList]) -> pd.DataFrame:
    """合并结果 → DataFrame（列与东财 ST 表同构，供 status 侧复用）。"""
    rows, coverage, warning, conflicts = merge_lists_detailed(lists)
    if not rows:
        raise StExchangeError("官方名单合并后为空：不能当成完整快照")
    frame = pd.DataFrame(rows, columns=["code", "name", "st_type", "market",
                                        "source"])
    frame.attrs["coverage"] = coverage
    if warning:
        frame.attrs["warning"] = warning
    if conflicts:
        frame.attrs["conflicts"] = conflicts
    return frame


def market_of(code: str) -> str:
    """6 位代码 → 交易所（bj/sh/sz）；用于合并时判断覆盖重叠。"""
    code = str(code or "")
    if code.startswith(("92", "4", "8")):
        return "bj"
    if code.startswith(("6", "9")):
        return "sh"
    return "sz"