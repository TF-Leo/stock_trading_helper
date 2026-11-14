#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""T7（issue #1 / R11 缺口表 #7–#10）：事件风险**收盘后预计算**。

为什么必须预计算
----------------
东财 ``datacenter-web`` 与 ``push2`` 同属东财风控面（IP 级封禁史）。14 只标的
× 4 个端点 + 4 个对照探针 = 60 次请求，串行 ≥1s 即 ≥59s，**绝不可**放进
09:15 盘前窗口。本工具只在收盘后（18:30–18:50 时段）运行一次，把结果落到

    source/stock_analysis/data/state/events/events.json   （``data/`` 已 gitignore）

CLI 只读这份缓存（``stock_analysis/data_loader/events.py``），**绝不触网**。

四类端点（固定 commit ``f21534946c42be3ad048ac938eecb1bad84cb8ab``，v3.10.4）
--------------------------------------------------------------------------
=========  ======================================  ============================
子项       上游 § / reportName                       请求 / 窗口
lockup     §3.6  ``RPT_LIFT_STAGE``                 1 次；近 365 天 + 未来 90 天
holder      §14.3 ``RPT_SHARE_HOLDER_INCREASE``     1 次；近 60 天公告
forecast   §14.1 ``RPT_PUBLIC_OP_NEWPREDICT``       1 次；近 60 天公告
pledge     §14.5 ``RPT_CSDC_LIST``                  1 次；该股最近统计日
=========  ======================================  ============================

股数口径（2026-11-14 实测校正）：解禁用 ``ABLE_FREE_SHARES``（实际可流通，万股，
与 ``FREE_RATIO`` 对得上），不是 ``FREE_SHARES``（后者是更大的流通股口径）；
质押表按个股累积多年历史（实测 600000 有 633 行 / 127 页），只取按
``TRADE_DATE`` 倒序的第 1 页 1 行，**不**声称拿到全部历史。

逐票请求都带个股条件，所以 **0 行 = 确实没有**（合法的空结果）。请求失败 /
非 2xx / 非 JSON / 结构改变 / 结果超过一页 = **接口坏了**，抛异常；该子项
标 ``stale=True`` 并保留上一轮缓存，其余三个子项继续，不受影响。（对照探针
本身不带个股条件，见下。）

P2-1 行级归属校验：返回的每一行还要再核对 ``SECURITY_CODE`` 与日期窗口
（解禁 ``FREE_DATE``、增减持/预告 ``NOTICE_DATE``、质押 ``TRADE_DATE<=as_of``）；
他票行 / 窗口外行 / 日期不可解析 / 重复行**丢弃**并计入子项 ``warnings``，
不会把别的标的的事件入库。

P2-2 「无事件 vs 接口坏了」：每轮先发 4 个对照探针（每报表 1 次，与票池
大小无关）：CSDC 无条件 ``pageSize=1``；解禁 ``FREE_DATE>=as_of``；预告 /
增减持 ``NOTICE_DATE>=as_of-30d``。探针抛错或 0 行 → 本轮该子项对所有代码
标 ``available=false, reason=interface_suspected``（有旧数据则保留 + stale），
并跳过该子项的逐票请求。零成本兜底：全票池该子项皆空且上一轮有数据 →
``empty_pool_suspected`` + warning。

限流
----
所有请求一律走 ``_astock_generated.em_get()``：模块级 ≥1s 最小间隔 + 随机
抖动 + Keep-Alive 会话复用。本工具在调用侧再加一层「相邻请求（含重试）间隔
≥ ``--interval``」的节流，默认 1.0s，不得调到 1.0 以下 —— 两层时间戳都取
「上一次请求返回之后」，因此不会叠加成 2s。

用法
----
    python tools/precompute_events.py                    # 默认 14 只自选
    python tools/precompute_events.py 600000 000001      # 指定票池
    python tools/precompute_events.py --codes 600000,000001 --json
    python tools/precompute_events.py --estimate         # 只估算耗时，不触网
    python tools/precompute_events.py --probes-only      # 只跑 4 个对照探针
    python tools/precompute_events.py --out-dir /tmp/events

退出码：0 = 全部子项成功；1 = 有子项失败；2 = 参数/配置错误。
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import re
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "source")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from stock_analysis.data_loader import events as events_loader  # noqa: E402

OUT_DIR = events_loader.EVENTS_DIR
CACHE_FILENAME = events_loader.CACHE_FILENAME

#: 与 tools/precompute_flow.py 的 14 只自选保持一致（同一批标的，便于交叉核对）。
DEFAULT_CODES = [
    "000858", "002270", "002371", "002415", "002594", "300476", "300750",
    "600036", "600519", "600522", "601318", "601398", "603259", "688981",
]

DEFAULT_INTERVAL = 1.0
DEFAULT_RETRIES = 1
DEFAULT_RETRY_WAIT = 2.0

DATACENTER_URL = "https://datacenter-web.eastmoney.com/api/data/v1/get"

#: 与上游 §3.6 / §14.x 的窗口对齐。
LOCKUP_HISTORY_DAYS = 365
LOCKUP_FORWARD_DAYS = 90
EVENT_LOOKBACK_DAYS = 60
PAGE_SIZE = 500
MAX_PAGES = 1

_CODE_RE = re.compile(r"^\d{6}$")


class EventsFetchError(RuntimeError):
    """事件端点取数 / 解析失败。"""


class FetchResult(list):
    """单个子项的取数结果。

    继承 ``list``：既有的调用方 / 测试仍可把它当 items 列表用（``==``、
    迭代、``len``）。额外携带：

    * ``warnings`` —— P2-1 丢弃的行、P3-4 未知类型等，随子项一起落盘；
    * ``reason`` —— 例如北交所质押的 ``not_supported``（P3-1）。
    """

    def __init__(self, items=(), warnings=None, reason=None):
        super().__init__(items)
        self.warnings = [str(item) for item in (warnings or [])]
        self.reason = reason


_DROP_LABELS = {
    "other_code": "他票行",
    "out_of_window": "窗口外行",
    "invalid_date": "日期不可解析",
    "duplicate": "重复行",
}


class _RowCheck:
    """P2-1：逐行归属校验的统计器（丢弃原因 → 条数 → 一条 warning）。"""

    def __init__(self):
        self.counts = {}
        self.nulls = {}
        self.seen = set()

    def reject(self, reason: str) -> None:
        self.counts[reason] = self.counts.get(reason, 0) + 1

    def null(self, field: str) -> None:
        """可选字段缺失/不可解析：该行**保留**，只把字段置 null 并计数。"""
        self.nulls[field] = self.nulls.get(field, 0) + 1

    def duplicate(self, key: str) -> bool:
        if key in self.seen:
            self.reject("duplicate")
            return True
        self.seen.add(key)
        return False

    def warnings(self):
        warnings = []
        if self.counts:
            total = sum(self.counts.values())
            detail = "、".join(f"{_DROP_LABELS[key]} {value}"
                              for key, value in self.counts.items() if value)
            warnings.append(f"丢弃 {total} 行（{detail}）")
        if self.nulls:
            total = sum(self.nulls.values())
            detail = "、".join(f"{field} {value}"
                              for field, value in self.nulls.items() if value)
            warnings.append(f"可选日期缺失 {total} 处（{detail}），"
                            "已保留该行并置 null")
        return warnings


def _row_key(item: dict) -> str:
    return json.dumps(item, sort_keys=True, ensure_ascii=False, default=str)


def _row_attribution(row: dict, code: str, date_field: str,
                     start: str = None, end: str = None, check: _RowCheck = None):
    """校验一行确实属于请求的标的与日期窗口；失败返回 ``None`` 并记账。

    上游 ``_em_event_rows()`` 对这一步逐行核对（``equal={"SECURITY_CODE": …}``
    + 日期窗口）；本实现此前只信请求参数，一旦东财忽略筛选就会把别的标的
    的行当成本标的的事件入库（张冠李戴）。这里按任务要求**丢弃**并计入
    warning（不是整批抛错，避免一行坏数据丢掉整页好数据）。
    """
    row_code = str(row.get("SECURITY_CODE") or "").strip()
    if row_code != code:
        check.reject("other_code")
        return None
    try:
        day = _day(row.get(date_field), date_field)
    except EventsFetchError:
        check.reject("invalid_date")
        return None
    if start is not None and day < start:
        check.reject("out_of_window")
        return None
    if end is not None and day > end:
        check.reject("out_of_window")
        return None
    return day


def _coerce_result(result):
    """fetcher 返回值 → ``(items, warnings, reason)``；兼容旧的 list 返回值。"""
    if isinstance(result, FetchResult):
        return list(result), result.warnings, result.reason
    if isinstance(result, list):
        return result, [], None
    raise EventsFetchError(f"fetcher 返回值不是列表：{type(result).__name__}")


# ---------------------------------------------------------------------------
# 数值 / 日期
# ---------------------------------------------------------------------------
def _num(value, field: str):
    """东财数值字段 → float / None；无法识别抛错（不把格式变化写成 0）。"""
    if value is None:
        return None
    if isinstance(value, bool):
        raise EventsFetchError(f"{field} 是布尔值 {value!r}")
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).replace(",", "").strip()
    if text in ("", "-", "--", "None", "null"):
        return None
    try:
        return float(text)
    except ValueError as exc:
        raise EventsFetchError(f"{field} 不是数值：{value!r}") from exc


def _day(value, field: str) -> str:
    """东财日期字段 → ``YYYY-MM-DD``；认不出抛错。"""
    text = str(value or "").strip()
    if "T" in text:
        text = text.split("T", 1)[0]
    if " " in text:
        text = text.split(" ", 1)[0]
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        raise EventsFetchError(f"{field} 不是 YYYY-MM-DD：{value!r}")
    return text


def _optional_day(value, field: str, check: _RowCheck) -> Optional[str]:
    """可选日期字段 → ``YYYY-MM-DD`` / None（缺失或不可解析都不丢行）。

    实测东财增减持报表存在 ``START_DATE=None`` 的合法行（如员工持股计划
    尚未开始），严格 ``_day()`` 会让整只票的「股东增减持」子项失败。可选
    日期缺失时保留该行、字段置 null，并在子项 warnings 里计数。
    """
    try:
        return _day(value, field)
    except EventsFetchError:
        check.null(field)
        return None


def _shift(day: str, days: int) -> str:
    base = _dt.date.fromisoformat(day)
    return (base + _dt.timedelta(days=days)).isoformat()


def _get_prefix(code: str) -> str:
    try:
        from stock_analysis.data_sources._astock_generated import get_prefix
        return get_prefix(code)
    except Exception:
        if code.startswith(("4", "8", "92")):
            return "bj"
        if code.startswith(("5", "6", "9")):
            return "sh"
        return "sz"


# ---------------------------------------------------------------------------
# 东财 datacenter（唯一触网入口）
# ---------------------------------------------------------------------------
def _http_get(url: str, params=None, headers=None, timeout=15):
    """实际 HTTP 入口。单独一层是为了让测试只 mock 这一处。"""
    from stock_analysis.data_sources._astock_generated import em_get
    return em_get(url, params=params, headers=headers, timeout=timeout)


def _datacenter(report: str, filter_str: str = "", sort_columns: str = "ALL",
                sort_types: str = "-1", page_size: int = PAGE_SIZE,
                allow_partial_page: bool = False):
    """东财数据中心单页查询。

    * ``code == 0`` 且 ``result.data`` 为列表 → 返回该列表；
    * ``code == 9201``（返回数据为空）→ ``[]``（带个股条件时是「确实没有」）；
    * 其余错误码 / 非 2xx / 非 JSON / 结构改变 → :class:`EventsFetchError`；
    * ``pages > 1`` → 默认抛错（不把截断结果当完整结果）。质押只取**最新一行**，
      传 ``allow_partial_page=True`` + ``page_size=1``；此时返回第 1 页的唯一一行，
      不声称拿到了全部历史。
    """
    params = {
        "reportName": report,
        "columns": "ALL",
        "filter": filter_str,
        "pageNumber": "1",
        "pageSize": str(page_size),
        "sortColumns": sort_columns,
        "sortTypes": sort_types,
        "source": "WEB",
        "client": "WEB",
    }
    try:
        response = _http_get(DATACENTER_URL, params=params, timeout=15)
    except Exception as exc:
        raise EventsFetchError(
            f"东财 {report} 请求失败：{type(exc).__name__}: {exc}") from exc

    status = getattr(response, "status_code", None)
    if status is not None and int(status) != 200:
        raise EventsFetchError(f"东财 {report} HTTP {status}")
    try:
        payload = response.json()
    except Exception as exc:
        raise EventsFetchError(
            f"东财 {report} 返回非 JSON（{type(exc).__name__}: {exc}）") from exc
    if not isinstance(payload, dict):
        raise EventsFetchError(
            f"东财 {report} 返回的不是对象（{type(payload).__name__}）")

    code = payload.get("code")
    try:
        code_int = int(code)
    except (TypeError, ValueError):
        code_int = None
    if code_int == 9201:
        return []
    if code_int != 0:
        raise EventsFetchError(
            f"东财 {report} 返回错误：{code} {payload.get('message')}")

    result = payload.get("result")
    if not isinstance(result, dict):
        raise EventsFetchError(
            f"东财 {report} 的 result 不是对象：{str(result)[:120]}")
    try:
        pages = int(result.get("pages") or 1)
    except (TypeError, ValueError):
        raise EventsFetchError(f"东财 {report} 的 pages 不是整数：{result.get('pages')!r}")
    if pages > MAX_PAGES and not allow_partial_page:
        raise EventsFetchError(
            f"东财 {report} 结果 {pages} 页 > {MAX_PAGES}；本工具只取单页，"
            "请缩小窗口或显式分页后再接入")

    data = result.get("data")
    if data is None:
        count = result.get("count")
        if count in (0, "0", None):
            return []
        raise EventsFetchError(f"东财 {report} 的 data 为空但 count={count!r}")
    if not isinstance(data, list) or not all(isinstance(row, dict) for row in data):
        raise EventsFetchError(
            f"东财 {report} 的 data 不是对象列表（{type(data).__name__}）")
    if not data and pages > 1:
        raise EventsFetchError(
            f"东财 {report} 第 1/{pages} 页为空，结果不完整")
    return data


# ---------------------------------------------------------------------------
# 四类端点
# ---------------------------------------------------------------------------
def fetch_lockup(code: str, as_of: str):
    """§3.6 解禁日历：一次请求同时取近 365 天历史与未来 90 天待解禁。

    股数口径（2026-11-14 实测校正）：东财 ``RPT_LIFT_STAGE`` 的
    ``ABLE_FREE_SHARES`` 才是「本次实际可流通」的万股数，且与 ``FREE_RATIO``
    对得上（例：600050 本次 25150.2 万股 ≈ 占总股本 0.81%）；``FREE_SHARES``
    是另一个更大口径（本次解禁前后的流通股总量），**不能**用来判断抛压。
    因此 ``shares_10k`` 取 ``ABLE_FREE_SHARES``；``FREE_SHARES`` 仅作为
    ``free_shares_10k`` 留档，不参与阈值。
    """
    start, end = _shift(as_of, -LOCKUP_HISTORY_DAYS), _shift(as_of, LOCKUP_FORWARD_DAYS)
    rows = _datacenter(
        "RPT_LIFT_STAGE",
        filter_str=(f'(SECURITY_CODE="{code}")(FREE_DATE>=\'{start}\')'
                    f"(FREE_DATE<='{end}')"),
        sort_columns="FREE_DATE", sort_types="1",
    )
    check = _RowCheck()
    items = []
    for row in rows:
        day = _row_attribution(row, code, "FREE_DATE", start, end, check)
        if day is None:
            continue
        ratio = _num(row.get("FREE_RATIO"), "FREE_RATIO")
        ratio_pct = None if ratio is None else round(ratio * 100.0, 4)
        if ratio_pct is not None and not 0.0 <= ratio_pct <= 100.0:
            raise EventsFetchError(f"FREE_RATIO 越界：{row.get('FREE_RATIO')!r}")
        item = {
            "date": day,
            "type": str(row.get("FREE_SHARES_TYPE") or "").strip(),
            "shares_10k": _num(row.get("ABLE_FREE_SHARES"), "ABLE_FREE_SHARES"),
            "free_shares_10k": _num(row.get("FREE_SHARES"), "FREE_SHARES"),
            "ratio_pct": ratio_pct,
        }
        if check.duplicate(_row_key(item)):
            continue
        items.append(item)
    items.sort(key=lambda item: item["date"])
    return FetchResult(items, warnings=check.warnings())


def fetch_holder_trades(code: str, as_of: str):
    """§14.3 重要股东增减持：近 60 天公告，股数带符号（减持为负）。"""
    start = _shift(as_of, -EVENT_LOOKBACK_DAYS)
    rows = _datacenter(
        "RPT_SHARE_HOLDER_INCREASE",
        filter_str=(f'(SECURITY_CODE="{code}")(NOTICE_DATE>=\'{start}\')'
                    f"(NOTICE_DATE<='{as_of}')"),
        sort_columns="NOTICE_DATE,SECURITY_CODE,HOLDER_NAME,START_DATE,END_DATE",
        sort_types="-1,1,1,1,1",
    )
    check = _RowCheck()
    items = []
    for row in rows:
        notice = _row_attribution(row, code, "NOTICE_DATE", start, as_of, check)
        if notice is None:
            continue
        direction = str(row.get("DIRECTION") or "").strip()
        if direction not in ("增持", "减持"):
            raise EventsFetchError(
                f"增减持方向不是增持/减持：{direction!r}（{row.get('SECURITY_CODE')}）")
        shares = _num(row.get("CHANGE_NUM_SYMBOL"), "CHANGE_NUM_SYMBOL")
        if shares is not None and (shares < 0) != (direction == "减持"):
            raise EventsFetchError(
                f"增减持方向与变动股数符号不一致：{row.get('SECURITY_CODE')} "
                f"{row.get('HOLDER_NAME')}")
        item = {
            "notice_date": notice,
            "holder": str(row.get("HOLDER_NAME") or "").strip(),
            "direction": direction,
            "change_shares_10k": shares,
            # 东财 AFTER_CHANGE_RATE 实为「本次变动占总股本比例」。
            "change_pct_total": _num(row.get("AFTER_CHANGE_RATE"), "AFTER_CHANGE_RATE"),
            "change_pct_float": _num(row.get("CHANGE_FREE_RATIO"), "CHANGE_FREE_RATIO"),
            "avg_price": _num(row.get("TRADE_AVERAGE_PRICE"), "TRADE_AVERAGE_PRICE"),
            "channel": str(row.get("MARKET") or "").strip(),
            "start_date": _optional_day(row.get("START_DATE"), "START_DATE", check),
            "end_date": _optional_day(row.get("END_DATE"), "END_DATE", check),
        }
        if check.duplicate(_row_key(item)):
            continue
        items.append(item)
    items.sort(key=lambda item: item["notice_date"], reverse=True)
    return FetchResult(items, warnings=check.warnings())


#: 业绩预告里属于「风险提示（黑名单）」的类型（封闭集合）。
FORECAST_NEGATIVE_TYPES = frozenset({"首亏", "续亏", "预减", "增亏", "略减"})

#: P3-4：已知 PREDICT_TYPE 封闭词表。不在表内的类型**不静默映射**，只告警；
#: 新增类型（如「预亏」）不会被当成非负面而悄悄漏报。
FORECAST_KNOWN_TYPES = frozenset(
    FORECAST_NEGATIVE_TYPES
    | {"预增", "略增", "扭亏", "续盈", "减亏", "预盈", "不确定"})


def fetch_forecast(code: str, as_of: str):
    """§14.1 业绩预告：近 60 天公告；一次预告按指标拆多行。"""
    start = _shift(as_of, -EVENT_LOOKBACK_DAYS)
    rows = _datacenter(
        "RPT_PUBLIC_OP_NEWPREDICT",
        filter_str=(f'(SECURITY_CODE="{code}")(NOTICE_DATE>=\'{start}\')'
                    f"(NOTICE_DATE<='{as_of}')"),
        sort_columns="NOTICE_DATE,SECURITY_CODE,REPORT_DATE,PREDICT_FINANCE_CODE",
        sort_types="-1,1,-1,1",
    )
    check = _RowCheck()
    items = []
    unknown_types = set()
    for row in rows:
        notice = _row_attribution(row, code, "NOTICE_DATE", start, as_of, check)
        if notice is None:
            continue
        forecast_type = str(row.get("PREDICT_TYPE") or "").strip() or None
        if forecast_type is not None and forecast_type not in FORECAST_KNOWN_TYPES:
            unknown_types.add(forecast_type)
        item = {
            "notice_date": notice,
            "report_date": _optional_day(
                row.get("REPORT_DATE"), "REPORT_DATE", check),
            "indicator": str(row.get("PREDICT_FINANCE") or "").strip(),
            "forecast_type": forecast_type,
            "amount_lower": _num(row.get("PREDICT_AMT_LOWER"), "PREDICT_AMT_LOWER"),
            "amount_upper": _num(row.get("PREDICT_AMT_UPPER"), "PREDICT_AMT_UPPER"),
            "change_pct_lower": _num(row.get("ADD_AMP_LOWER"), "ADD_AMP_LOWER"),
            "change_pct_upper": _num(row.get("ADD_AMP_UPPER"), "ADD_AMP_UPPER"),
        }
        if check.duplicate(_row_key(item)):
            continue
        items.append(item)
    items.sort(key=lambda item: (item["notice_date"], item["indicator"]), reverse=True)
    warnings = check.warnings()
    if unknown_types:
        warnings.append(
            "未知 PREDICT_TYPE：" + "、".join(sorted(unknown_types))
            + "（未按负面类型处理，请核对上游词表）")
    return FetchResult(items, warnings=warnings)


def fetch_pledge(code: str, as_of: str):
    """§14.5 股权质押：中国结算周度统计；只覆盖沪深，北交所显式 ``not_supported``。"""
    if _get_prefix(code) == "bj":
        # P3-1：不是「没有质押事件」，而是「该市场无此统计口径」。写入
        # reason=not_supported，渲染层显示「未覆盖」，不得退化成「无事件」。
        return FetchResult([], reason="not_supported")
    rows = _datacenter(
        "RPT_CSDC_LIST",
        filter_str=f'(SECURITY_CODE="{code}")',
        # 中国结算质押表按个股累积多年历史（实测 600000 有 633 行 / 127 页），
        # 但我们只要**最新一个统计日**：按 TRADE_DATE 倒序取第 1 页的 1 行，
        # 不声称拿到了全部历史，因此允许 pages>1。
        sort_columns="TRADE_DATE", sort_types="-1",
        page_size=1, allow_partial_page=True,
    )
    check = _RowCheck()
    picked = None
    for row in rows:
        day = _row_attribution(row, code, "TRADE_DATE", None, as_of, check)
        if day is not None:
            picked = (row, day)
            break
    if picked is None:
        return FetchResult([], warnings=check.warnings())
    row, day = picked
    total = _num(row.get("REPURCHASE_BALANCE"), "REPURCHASE_BALANCE")
    free = _num(row.get("REPURCHASE_UNLIMITED_BALANCE"),
                "REPURCHASE_UNLIMITED_BALANCE")
    locked = _num(row.get("REPURCHASE_LIMITED_BALANCE"),
                  "REPURCHASE_LIMITED_BALANCE")
    if None not in (total, free, locked) and abs(free + locked - total) > max(1.0, total * 0.001):
        raise EventsFetchError(f"质押数据 无限售 + 限售 ≠ 合计：{code} "
                               f"{row.get('TRADE_DATE')}")
    ratio = _num(row.get("PLEDGE_RATIO"), "PLEDGE_RATIO")
    if ratio is not None and not 0.0 <= ratio <= 100.0:
        raise EventsFetchError(f"PLEDGE_RATIO 越界：{row.get('PLEDGE_RATIO')!r}")
    item = {
        "date": day,
        "ratio_pct": ratio,
        "pledged_shares_10k": total,
        "pledged_mktcap_10k": _num(row.get("PLEDGE_MARKET_CAP"), "PLEDGE_MARKET_CAP"),
        "pledge_count": _num(row.get("PLEDGE_DEAL_NUM"), "PLEDGE_DEAL_NUM"),
    }
    return FetchResult([item], warnings=check.warnings())


#: 子项顺序固定：估算、串行与测试都按这个顺序。
FETCHERS = (
    ("lockup", fetch_lockup),
    ("holder_trades", fetch_holder_trades),
    ("forecast", fetch_forecast),
    ("pledge", fetch_pledge),
)
REQUESTS_PER_CODE = len(FETCHERS)

# ---------------------------------------------------------------------------
# P2-2：每轮每报表 1 次「已知应该非空」的对照探针（共 4 请求）
# ---------------------------------------------------------------------------
#: 探针的最近公告窗口（天）。全市场 30 天窗口恒有大量公告。
PROBE_LOOKBACK_DAYS = 30


def probe_lockup(as_of: str):
    """全市场未来解禁恒有排期 → 0 行即接口疑似异常。"""
    return _datacenter(
        "RPT_LIFT_STAGE",
        filter_str=f"(FREE_DATE>='{as_of}')",
        sort_columns="FREE_DATE", sort_types="1",
        page_size=1, allow_partial_page=True,
    )


def probe_holder_trades(as_of: str):
    """全市场近 30 天增减持公告恒有数据。"""
    start = _shift(as_of, -PROBE_LOOKBACK_DAYS)
    return _datacenter(
        "RPT_SHARE_HOLDER_INCREASE",
        filter_str=f"(NOTICE_DATE>='{start}')",
        sort_columns="NOTICE_DATE", sort_types="-1",
        page_size=1, allow_partial_page=True,
    )


def probe_forecast(as_of: str):
    """全市场近 30 天业绩预告恒有数据。"""
    start = _shift(as_of, -PROBE_LOOKBACK_DAYS)
    return _datacenter(
        "RPT_PUBLIC_OP_NEWPREDICT",
        filter_str=f"(NOTICE_DATE>='{start}')",
        sort_columns="NOTICE_DATE", sort_types="-1",
        page_size=1, allow_partial_page=True,
    )


def probe_pledge(as_of: str):
    """中国结算周度全市场统计恒有最新一行（无条件过滤）。"""
    return _datacenter(
        "RPT_CSDC_LIST",
        filter_str="",
        sort_columns="TRADE_DATE", sort_types="-1",
        page_size=1, allow_partial_page=True,
    )


PROBES = (
    ("lockup", probe_lockup),
    ("holder_trades", probe_holder_trades),
    ("forecast", probe_forecast),
    ("pledge", probe_pledge),
)
PROBE_REQUESTS = len(PROBES)


# ---------------------------------------------------------------------------
# 节流 / 预计算
# ---------------------------------------------------------------------------
class _Throttle:
    """相邻请求（含重试）至少间隔 ``interval`` 秒。

    时间戳取「上一次请求**返回**之后」，与 ``em_get`` 的节流时间戳同口径，
    因此两层限流不会叠加成 2×interval。
    """

    def __init__(self, interval: float, now_fn, sleep_fn):
        self.interval = float(interval)
        self.now_fn = now_fn
        self.sleep_fn = sleep_fn
        self.last = None
        self.requests = 0

    def call(self, fn, *args):
        if self.last is not None:
            wait = self.interval - (self.now_fn().timestamp() - self.last)
            if wait > 0:
                self.sleep_fn(wait)
        self.requests += 1
        try:
            return fn(*args)
        finally:
            self.last = self.now_fn().timestamp()


def _section_ok(items, warnings=None) -> dict:
    return {
        "available": True,
        "stale": False,
        "as_of": None,       # 调用处填运行日
        "fetched_at": None,  # 调用处填运行时刻
        "reason": None,
        "error": None,
        "items": list(items),
        "warnings": [str(item) for item in (warnings or [])],
    }


def _failed_section(previous, error: str) -> dict:
    """失败子项：有旧数据就保留并标 stale；没有就显式不可用。"""
    if isinstance(previous, dict) and isinstance(previous.get("items"), list):
        carried = dict(previous)
        carried["stale"] = True
        carried["error"] = error
        carried["reason"] = "fetch_failed"
        carried["items"] = list(previous.get("items") or [])
        carried["warnings"] = [str(item)
                                for item in (previous.get("warnings") or [])]
        return carried
    section = events_loader.empty_section(reason="fetch_failed", available=False,
                                          stale=True, error=error)
    return section


def _suspected_section(previous, error: str) -> dict:
    """P2-2：对照探针失败 → 本轮该子项对**所有代码** ``available=false``。

    有旧数据时保留 ``items`` 仅供审计 / 复查，但 ``available=false``（本轮
    不采信任何结果），``stale=true`` + ``reason=interface_suspected``。
    """
    note = "本轮对照探针失败/0 行，接口疑似异常"
    if isinstance(previous, dict) and isinstance(previous.get("items"), list):
        carried = dict(previous)
        carried["available"] = False
        carried["stale"] = True
        carried["reason"] = "interface_suspected"
        carried["error"] = error
        carried["items"] = list(previous.get("items") or [])
        carried["warnings"] = [str(item)
                               for item in (previous.get("warnings") or [])] + [note]
        return carried
    return events_loader.empty_section(
        reason="interface_suspected", available=False, stale=True, error=error,
        warnings=[note])


def _write_payload(payload: dict, out_dir=None) -> str:
    path = os.path.join(out_dir or OUT_DIR, CACHE_FILENAME)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=1, sort_keys=True)
        handle.write("\n")
    os.replace(tmp, path)
    return path


def _run_probes(probe_specs, run_day, throttle, stats) -> set:
    """跑对照探针并把结果写进 ``stats``；返回疑似异常的子项键集合。"""
    suspected = set()
    for key, probe in probe_specs:
        error = None
        rows = []
        try:
            rows = throttle.call(probe, run_day)
            if not rows:
                error = "对照探针返回 0 行"
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
        stats["probes"][key] = {"ok": error is None, "rows": len(rows),
                                "error": error}
        if error is not None:
            suspected.add(key)
            stats["interface_suspected"].append(key)
            stats["failed_sections"] += 1
            stats["failed"].append({"code": None, "section": key,
                                    "reason": f"interface_suspected: {error}"})
            message = (f"WARN 对照探针 {key} 失败：{error}；"
                       "本轮该子项全部标 interface_suspected")
            stats["warnings"].append(message)
            print(message, file=sys.stderr)
        else:
            print(f"PROBE {key}: {len(rows)} 行（正常）", file=sys.stderr)
    stats["probe_requests"] = len(probe_specs)
    return suspected


def run_probes(interval=DEFAULT_INTERVAL, probes=None, sleep_fn=time.sleep,
               now_fn=_dt.datetime.now, as_of=None):
    """只跑 4 个对照探针（不写缓存）；用于实测探针耗时 / 体检接口。"""
    if interval < 1.0:
        raise ValueError("interval 不得小于 1.0 秒（东财防封铁律）")
    if probes is None:
        probe_specs = list(PROBES)
    elif isinstance(probes, dict):
        probe_specs = list(probes.items())
    else:
        probe_specs = list(probes)
    started = now_fn()
    run_day = (_dt.date.fromisoformat(as_of).isoformat()
               if as_of else started.date().isoformat())
    throttle = _Throttle(interval, now_fn, sleep_fn)
    stats = {"as_of": run_day, "requests": 0, "probe_requests": 0,
             "probes": {}, "interface_suspected": [], "failed_sections": 0,
             "failed": [], "warnings": []}
    _run_probes(probe_specs, run_day, throttle, stats)
    stats["requests"] = throttle.requests
    stats["wall_seconds"] = round(now_fn().timestamp() - started.timestamp(), 2)
    return stats


def precompute(codes, out_dir=None, interval=DEFAULT_INTERVAL,
               retries=DEFAULT_RETRIES, retry_wait=DEFAULT_RETRY_WAIT,
               fetchers=None, probes=None, sleep_fn=time.sleep,
               now_fn=_dt.datetime.now, as_of=None):
    """逐票、逐子项串行预计算；返回统计字典（不抛异常，失败逐子项记录）。

    ``fetchers`` / ``probes`` 可注入（测试用）；默认 :data:`FETCHERS` /
    :data:`PROBES`。任意子项失败只影响该子项，其余继续；失败时保留上一轮的
    同名子项并标 ``stale=True``。

    P2-2：先跑 4 个对照探针（每报表 1 次）。探针抛错或 0 行 → 本轮该子项对
    **所有代码**标 ``available=false, reason=interface_suspected``，且跳过该子项
    的逐票请求（反正接口已疑似异常，省请求）。
    """
    if interval < 1.0:
        raise ValueError("interval 不得小于 1.0 秒（东财防封铁律）")
    if retries < 0:
        raise ValueError("retries 不得为负")
    fetchers = tuple(fetchers or FETCHERS)
    if probes is None:
        probe_specs = list(PROBES)
    elif isinstance(probes, dict):
        probe_specs = list(probes.items())
    else:
        probe_specs = list(probes)
    started = now_fn()
    run_day = _dt.date.fromisoformat(as_of).isoformat() if as_of else started.date().isoformat()
    run_stamp = started.isoformat()

    existing, problem = events_loader.read_payload(out_dir)
    if existing is None and problem != events_loader.CACHE_MISSING:
        print(f"[WARN] 旧缓存不可用（{problem}），本轮不继承旧子项", file=sys.stderr)
        existing = {}
    existing = existing or {}

    throttle = _Throttle(interval, now_fn, sleep_fn)
    stats = {
        "out_dir": out_dir or OUT_DIR,
        "as_of": run_day,
        "interval_seconds": float(interval),
        "retries": int(retries),
        "requests": 0,
        "probe_requests": 0,
        "probes": {},
        "interface_suspected": [],
        "not_supported_sections": 0,
        "ok_sections": 0,
        "failed_sections": 0,
        "ok": [],
        "failed": [],
        "warnings": [],
    }

    seen, ordered = set(), []
    for raw in codes:
        code = events_loader._normalize_code(raw)
        if not _CODE_RE.match(code):
            stats["failed"].append({"code": str(raw), "section": None,
                                    "reason": f"代码无法解析：{raw!r}"})
            stats["failed_sections"] += 1
            continue
        if code not in seen:
            seen.add(code)
            ordered.append(code)

    # --- P2-2 第一层：对照探针（每报表 1 次，与票池大小无关）-------------
    suspected = _run_probes(probe_specs, run_day, throttle, stats)

    # --- 逐票取数 --------------------------------------------------------
    payload = dict(existing)
    for code in ordered:
        previous = existing.get(code) if isinstance(existing.get(code), dict) else {}
        sections = {}
        for key, fetch in fetchers:
            if key in suspected:
                error = stats["probes"].get(key, {}).get("error") or "探针失败"
                sections[key] = _suspected_section(previous.get(key), error)
                continue
            last_error = None
            result = None
            for attempt in range(int(retries) + 1):
                try:
                    result = throttle.call(fetch, code, run_day)
                    last_error = None
                    break
                except Exception as exc:
                    last_error = f"{type(exc).__name__}: {exc}"
                    if attempt < int(retries):
                        sleep_fn(retry_wait)
            if result is None:
                stats["failed_sections"] += 1
                stats["failed"].append({"code": code, "section": key,
                                        "reason": last_error})
                sections[key] = _failed_section(previous.get(key), last_error)
                print(f"FAIL {code} {key}: {last_error}", file=sys.stderr)
                continue
            try:
                items, warnings, reason = _coerce_result(result)
            except EventsFetchError as exc:
                stats["failed_sections"] += 1
                stats["failed"].append({"code": code, "section": key,
                                        "reason": str(exc)})
                sections[key] = _failed_section(previous.get(key), str(exc))
                print(f"FAIL {code} {key}: {exc}", file=sys.stderr)
                continue
            if reason == "not_supported":
                # P3-1：口径不适用 ≠ 取数失败 ≠ 无事件。保留 reason 供渲染。
                section = events_loader.empty_section(
                    reason="not_supported", available=False, stale=False,
                    as_of=run_day, fetched_at=run_stamp, warnings=warnings)
                sections[key] = section
                stats["not_supported_sections"] += 1
                print(f"N/A  {code} {key}: 该市场无此统计口径", file=sys.stderr)
                continue
            section = _section_ok(items, warnings)
            section["as_of"] = run_day
            section["fetched_at"] = run_stamp
            sections[key] = section
            stats["ok_sections"] += 1
            stats["ok"].append({"code": code, "section": key,
                                "items": len(items), "as_of": run_day,
                                "warnings": list(warnings)})
            for warning in warnings:
                stats["warnings"].append(f"{code} {key}: {warning}")
            print(f"OK   {code} {key}: {len(items)} 条", file=sys.stderr)
        payload[code] = sections

    # --- P2-2 第二层（零成本兜底）：全票池皆空且上一轮有数据 -----------
    for key, _fetch in fetchers:
        if key in suspected:
            continue
        current = [(code, payload[code][key]) for code in ordered
                   if isinstance(payload.get(code), dict)
                   and isinstance(payload[code].get(key), dict)]
        if not current or any(sec.get("items") for _code, sec in current):
            continue
        if any(bool(sec.get("error")) or sec.get("reason") == "not_supported"
               for _code, sec in current):
            continue
        prior_has_data = any(
            isinstance(existing.get(code), dict)
            and isinstance(existing[code].get(key), dict)
            and existing[code][key].get("items")
            for code in ordered)
        if not prior_has_data:
            continue
        message = (f"WARN 全票池 {key} 为空且上一轮有数据；"
                   "已标 empty_pool_suspected（不得静默当作无事件）")
        stats["warnings"].append(message)
        print(message, file=sys.stderr)
        for _code, section in current:
            section["stale"] = True
            section["reason"] = "empty_pool_suspected"
            section["warnings"] = list(section.get("warnings") or []) + [
                "全票池该子项为空且上一轮有数据，接口疑似异常"]

    stats["requests"] = throttle.requests
    stats["wall_seconds"] = round(now_fn().timestamp() - started.timestamp(), 2)
    payload.update({
        "schema": events_loader.EVENTS_SCHEMA,
        "source": events_loader.SOURCE,
        "upstream": events_loader.UPSTREAM,
        "fetched_at": run_stamp,
        "stale": any(
            isinstance(sec, dict) and sec.get("stale")
            for code in ordered for sec in (payload.get(code) or {}).values()
            if isinstance(sec, dict)
        ),
    })
    # 只保留 6 位代码键 + 保留元数据键：坏键（例如旧版本残留）不进入新缓存。
    cleaned = {key: value for key, value in payload.items()
               if key in ("schema", "source", "upstream", "fetched_at", "stale")
               or _CODE_RE.match(str(key))}
    stats["path"] = _write_payload(cleaned, out_dir=out_dir)
    return stats


def estimate(n_codes: int, interval: float = DEFAULT_INTERVAL) -> dict:
    """串行耗时估算：``n × 4 + 4 探针`` 次请求，首次无等待，之后各等 ``interval``。"""
    if interval < 1.0:
        raise ValueError("interval 不得小于 1.0 秒（东财防封铁律）")
    requests = int(n_codes) * REQUESTS_PER_CODE + PROBE_REQUESTS
    return {
        "codes": int(n_codes),
        "requests": requests,
        "requests_per_code": REQUESTS_PER_CODE,
        "probe_requests": PROBE_REQUESTS,
        "min_interval_seconds": float(interval),
        "min_serial_seconds": round(max(0, requests - 1) * float(interval), 2),
        "estimated_seconds": round(requests * float(interval), 2),
        "note": ("估算不含网络时延；每轮先跑 4 个对照探针（解禁/增减持/预告/质押），"
                 "串行下限是 (请求数-1)×间隔，含时延后接近 请求数×间隔"),
    }


def build_parser():
    parser = argparse.ArgumentParser(
        description="事件风险预计算（解禁 / 增减持 / 业绩预告 / 质押；写 "
                    "source/stock_analysis/data/state/events/）")
    parser.add_argument("codes", nargs="*", help="股票代码（默认 14 只自选）")
    parser.add_argument("--codes", dest="codes_csv", default=None,
                        help="逗号分隔的票池（与位置参数合并去重）")
    parser.add_argument("--out-dir", default=OUT_DIR, help=f"缓存目录（默认 {OUT_DIR}）")
    parser.add_argument("--interval", type=float, default=DEFAULT_INTERVAL,
                        help="相邻请求最小间隔秒数，不得 <1.0（默认 1.0）")
    parser.add_argument("--retries", type=int, default=DEFAULT_RETRIES,
                        help="单子项失败重试次数（默认 1）")
    parser.add_argument("--retry-wait", type=float, default=DEFAULT_RETRY_WAIT,
                        help="重试前的额外等待秒数（默认 2.0）")
    parser.add_argument("--as-of", default=None,
                        help="预计算基准日（YYYY-MM-DD；默认今天，仅测试/补跑用）")
    parser.add_argument("--estimate", action="store_true",
                        help="只输出串行耗时估算，不触网")
    parser.add_argument("--probes-only", action="store_true",
                        help="只跑 4 个对照探针（体检接口，不写缓存）")
    parser.add_argument("--json", action="store_true", help="输出机读统计 JSON")
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    codes = list(args.codes)
    if args.codes_csv:
        codes += [c.strip() for c in args.codes_csv.split(",") if c.strip()]
    if not codes:
        codes = list(DEFAULT_CODES)
    try:
        if args.estimate:
            stats = estimate(len(set(codes)), interval=args.interval)
            print(json.dumps(stats, ensure_ascii=False, indent=2))
            return 0
        if args.probes_only:
            stats = run_probes(interval=args.interval, as_of=args.as_of)
            print(f"probe_requests={stats['requests']} "
                  f"suspected={','.join(stats['interface_suspected']) or '-'} "
                  f"wall={stats['wall_seconds']}s")
            if args.json:
                print(json.dumps(stats, ensure_ascii=False, indent=2))
            return 0 if not stats["interface_suspected"] else 1
        stats = precompute(codes, out_dir=args.out_dir, interval=args.interval,
                           retries=args.retries, retry_wait=args.retry_wait,
                           as_of=args.as_of)
    except ValueError as exc:
        print(f"ERROR {exc}", file=sys.stderr)
        return 2

    print(f"requests={stats['requests']} "
          f"(probe={stats['probe_requests']}) "
          f"ok_sections={stats['ok_sections']} "
          f"failed_sections={stats['failed_sections']} "
          f"not_supported={stats['not_supported_sections']} "
          f"suspected={','.join(stats['interface_suspected']) or '-'} "
          f"wall={stats['wall_seconds']}s interval={stats['interval_seconds']}s "
          f"-> {stats['path']}")
    for item in stats["failed"]:
        print(f"  - {item['code']} {item.get('section') or '-'}: {item['reason']}",
              file=sys.stderr)
    if args.json:
        print(json.dumps(stats, ensure_ascii=False, indent=2))
    return 0 if not stats["failed_sections"] else 1


if __name__ == "__main__":
    raise SystemExit(main())