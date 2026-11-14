# -*- coding: utf-8 -*-
"""T7（issue #1 / R11 缺口表 #7–#10）：事件风险 —— **只读缓存，绝不触网**。

数据流
------
1. 收盘后（18:30–18:50 时段）由 ``tools/precompute_events.py`` 串行拉取东财
   ``datacenter-web.eastmoney.com`` 的四类事件，逐票写入

       source/stock_analysis/data/state/events/events.json   （``data/`` 已 gitignore）

2. CLI 主路径只调用本模块：读缓存 → 组装 ``evidence.events``。
   **本模块不 import requests / em_get，不打开 socket**；缓存缺失、损坏或
   子项取数失败时返回 ``available=False`` / ``stale=True`` 的同构块，由渲染层
   明确写出「事件数据过期（{D}）」，绝不把「没取到」写成「没有事件」。

上游端点（固定 commit ``f21534946c42be3ad048ac938eecb1bad84cb8ab``，v3.10.4）
--------------------------------------------------------------------------
=========  ======================================  ==========================
子项       上游 § / reportName                       窗口 / 单位
lockup     §3.6  ``RPT_LIFT_STAGE``                 未来 90 天 + 近 365 天；
                                                        ``ABLE_FREE_SHARES``
                                                        （实际可流通）万股，
                                                        ``FREE_RATIO`` 小数
                                                       （×100 得百分比）
holder      §14.3 ``RPT_SHARE_HOLDER_INCREASE``      近 60 天公告；万股，
trades                                                 变动股数带符号
forecast   §14.1 ``RPT_PUBLIC_OP_NEWPREDICT``        近 60 天公告；金额元，
                                                       变动 %
pledge     §14.5 ``RPT_CSDC_LIST``                   最近一个统计日；中国
                                                       结算周度，只覆盖沪深
=========  ======================================  ==========================

缓存结构（``events.json``）
--------------------------
::

    {
      "<6位代码>": {
          "lockup":        {"available": bool, "stale": bool, "as_of": "YYYY-MM-DD",
                            "fetched_at": ISO, "reason": str|null, "error": str|null,
                            "warnings": [str], "items": [...]},
          "holder_trades": {...同构...},
          "forecast":      {...同构...},
          "pledge":        {...同构...},   # items 为 0 或 1 条
      },
      "schema": 1, "source": "eastmoney_datacenter",
      "upstream": "...", "fetched_at": ISO, "stale": bool
    }

* 顶层键里只有 6 位数字键是标的，其余为保留元数据键。
* 每个子项独立成功 / 失败：任一子端点失败只把**该子项**标 ``stale=True`` 并
  保留上一轮数据（``error`` 写明原因），其余子项不受影响。
* ``as_of`` 是**数据快照日**（预计算运行日；失败回退时是上一轮运行日），
  ``items[].date`` / ``notice_date`` 才是事件自身的日期。

「无事件」与「接口坏了」必须分开
--------------------------------
* 带个股条件的接口返回 0 行，且本轮**对照探针正常** = **确实没有**该事件
  （``available=True, items=[]``）。
* 请求失败 / 非 2xx / 非 JSON / 结构改变 / **对照探针失败或 0 行** = **接口坏了**
  （``available=False`` 或保留旧数据但 ``stale=True``，``reason`` 为
  ``interface_suspected``），绝不返回空表冒充「无事件」。
* 零成本兜底：某子项全票池皆空且上一轮有数据 → ``empty_pool_suspected``
  + warning，不得静默。
* 预计算侧还会丢弃不属于该标的 / 该窗口的返回行（P2-1），丢弃条数写进
  子项 ``warnings``；本模块读到后转成 CLI warnings。
* 过期判定只用官方磁盘交易日历；缓存从未生成写「未生成」而不是
  「过期（未知）」。

本模块只服务「展示 / 风险提示」，不参与 ``action`` / ``confidence`` / 仓位 /
触发器计算。
"""
from __future__ import annotations

import datetime as _dt
import json
import os
import re
from typing import Any, Dict, List, Optional, Tuple

__all__ = [
    "CACHE_MISSING",
    "EVENTS_KEYS",
    "EVENTS_SCHEMA",
    "REASON_TEXT",
    "SECTION_BLOCK_KEYS",
    "SECTION_KEYS",
    "SECTION_LABELS",
    "UPSTREAM",
    "build_events_block",
    "cache_path",
    "empty_events_block",
    "empty_section",
    "events_evidence",
    "read_cache_checked",
    "read_payload",
    "with_events_block",
]

#: 缓存目录：``source/stock_analysis/data/state/events``（``data/`` 已 gitignore）。
EVENTS_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "data", "state", "events")

CACHE_FILENAME = "events.json"
EVENTS_SCHEMA = 1
SOURCE = "eastmoney_datacenter"
UPSTREAM = ("a-stock-data v3.10.4 §3.6 lockup_expiry / §14.1 earnings_forecast "
            "/ §14.3 holder_trades / §14.5 equity_pledge")

SECTION_KEYS = ("lockup", "holder_trades", "forecast", "pledge")
SECTION_LABELS = {
    "lockup": "解禁",
    "holder_trades": "股东增减持",
    "forecast": "业绩预告",
    "pledge": "股权质押",
}

#: ``evidence.events`` 的子项键集合。成功 / 不可用 / 错误路径必须完全一致，
#: 保证 G35 的「错误路径与成功路径键同构」在新增子块上继续成立。
SECTION_BLOCK_KEYS = (
    "available", "stale", "as_of", "fetched_at", "reason", "error", "items",
    # T7 P2-1/P3-4 additive：预计算阶段丢弃的行 / 未知字段类型在这里落盘，
    # 由 CLI 转成 diagnostics.warnings；空子项也带它（键同构）。
    "warnings",
)
EVENTS_KEYS = ("available", "stale", "as_of", "fetched_at", "reason") + SECTION_KEYS

REASON_TEXT = {
    "cache_missing": "无本地事件缓存（需先运行 tools/precompute_events.py）",
    "code_missing": "本地事件缓存中没有该标的（需先运行 tools/precompute_events.py）",
    "cache_invalid": "本地事件缓存格式不可用",
    "fund_not_supported": "场外基金无个股事件数据（上游 §3.6/§14 只覆盖个股）",
    "replay_not_supported": "事件数据是收盘后快照，--as-of 回放不提供历史状态",
    "fetch_failed": "取数阶段失败，事件数据不可得",
    "interface_suspected": "接口疑似异常（对照探针报错或返回 0 行）",
    "empty_pool_suspected": "全票池该子项为空且上一轮有数据，接口疑似异常",
    "not_supported": "该市场无此事件统计口径",
    "section_missing": "缓存中缺少该子项",
    "section_invalid": "缓存子项格式不可用",
}

#: ``read_cache_checked`` 的第二返回值：文件根本不存在（不是损坏）。
CACHE_MISSING = "missing"
_CODE_MISSING = "code_missing"

_CODE_RE = re.compile(r"^\d{6}$")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


# ---------------------------------------------------------------------------
# 路径 / 时间
# ---------------------------------------------------------------------------
def _events_dir(events_dir: Optional[str] = None) -> str:
    if events_dir:
        return events_dir
    override = os.getenv("EVENTS_STATE_DIR")
    if override:
        return override
    return EVENTS_DIR


def cache_path(events_dir: Optional[str] = None) -> str:
    """缓存文件路径：``<events_dir>/events.json``。"""
    return os.path.join(_events_dir(events_dir), CACHE_FILENAME)


def _normalize_code(code) -> str:
    """任意受支持写法 → 6 位纯数字。失败时原样返回（缓存查找自然 miss）。"""
    try:
        from stock_analysis.data_sources._astock_generated import norm_ticker
        return norm_ticker(str(code))
    except Exception:
        return str(code).strip().lower()


def _date_only(value) -> Optional[str]:
    """ISO 时间戳 / 日期 → ``YYYY-MM-DD``；认不出返回 ``None``。"""
    if value is None:
        return None
    text = str(value).strip()
    if "T" in text:
        text = text.split("T", 1)[0]
    return text if _DATE_RE.match(text) else None


def _expected_trade_date(now=None):
    """期望最新交易日 —— **只用深交所官方磁盘日历**（纯本地，不触网）。

    P3-3：旧实现复用 ``flow._expected_trade_date``，日历文件缺月时会退回
    「按周末推算」并**立刻返回**，长假后首日（例如 2026-10-08）就会把
    9-30 的快照误判成过期。这里改为：

    * 只认官方日历（``astock.calendar_days_from_disk``）；
    * 当前月日历缺失时**继续向前翻月**，直到找到有官方日历的月份，取其中
      最近一个交易日；
    * 连一份官方日历都没有 → 返回 ``(None, "calendar_unavailable")``，
      调用方**不做**日期过期判定（宁可晚报，不误报）。

    返回 ``(date | None, source)``，``source`` ∈ ``calendar_cache`` /
    ``calendar_unavailable``。
    """
    moment = now or _dt.datetime.now()
    candidate = moment.date()
    if getattr(moment, "hour", 12) < 15:
        candidate = candidate - _dt.timedelta(days=1)
    try:
        from stock_analysis.data_sources import astock
        read_calendar = astock.calendar_days_from_disk
    except Exception:
        read_calendar = None
    for _ in range(62):
        days = None
        if read_calendar is not None:
            try:
                days = read_calendar(candidate.year, candidate.month)
            except Exception:
                days = None
        if isinstance(days, dict) and days:
            if days.get(candidate.isoformat()) is True:
                return candidate, "calendar_cache"
        candidate = candidate - _dt.timedelta(days=1)
    return None, "calendar_unavailable"


# ---------------------------------------------------------------------------
# 块构造
# ---------------------------------------------------------------------------
def empty_section(reason: Optional[str] = "fetch_failed",
                  available: bool = False,
                  stale: bool = False,
                  as_of: Optional[str] = None,
                  fetched_at: Optional[str] = None,
                  error: Optional[str] = None,
                  warnings: Optional[List[str]] = None) -> dict:
    """键齐全的空子项（成功 / 不可用 / 错误路径同构）。"""
    return {
        "available": bool(available),
        "stale": bool(stale),
        "as_of": as_of,
        "fetched_at": fetched_at,
        "reason": reason,
        "error": error,
        "items": [],
        "warnings": [str(item) for item in (warnings or [])],
    }


def _normalize_section(section) -> dict:
    """缓存中的子项 → 键齐全的块；结构不合法时按该子项不可用处理。

    这里不走「整体缓存损坏」：一个子项格式坏掉不应该抹掉另外三个已经
    成功取到的子项（失败隔离）。
    """
    if not isinstance(section, dict):
        return empty_section(reason="section_missing", available=False, stale=True)
    items = section.get("items")
    if not isinstance(items, list) or not all(isinstance(item, dict) for item in items):
        return empty_section(reason="section_invalid", available=False, stale=True,
                             as_of=_date_only(section.get("as_of")),
                             fetched_at=section.get("fetched_at"),
                             error="缓存子项 items 不是对象列表")
    block = {
        "available": section.get("available") is not False,
        "stale": section.get("stale") is True,
        "as_of": _date_only(section.get("as_of")),
        "fetched_at": section.get("fetched_at"),
        "reason": section.get("reason"),
        "error": section.get("error"),
        "items": [dict(item) for item in items],
        "warnings": [str(item) for item in (section.get("warnings") or [])
                     if isinstance(section.get("warnings"), list)],
    }
    return block


def empty_events_block(reason: str = "cache_missing",
                       stale: bool = False,
                       code: Optional[str] = None) -> dict:
    """所有子项都不可用的 ``evidence.events`` 块（键与成功路径同构）。

    ``stale=True`` 用于「本该有数据但没取到」的路径（缓存缺失 / 损坏 /
    取数失败）；渲染层据此写出「事件数据过期（{D}）」，而不是当作无事件。
    """
    block = {
        "available": False,
        "stale": bool(stale),
        "as_of": None,
        "fetched_at": None,
        "reason": reason,
        "code": _normalize_code(code) if code is not None else None,
    }
    for key in SECTION_KEYS:
        block[key] = empty_section(reason=reason, available=False, stale=stale)
    return block


# ---------------------------------------------------------------------------
# 缓存读取
# ---------------------------------------------------------------------------
def read_payload(events_dir: Optional[str] = None):
    """读整个缓存文件；不存在 / 不可解析 / schema 不符返回 ``(None, 原因)``。"""
    path = cache_path(events_dir)
    if not os.path.exists(path):
        return None, CACHE_MISSING
    try:
        with open(path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except Exception as exc:
        return None, f"corrupt: JSON 不可解析（{type(exc).__name__}: {exc}）"
    if not isinstance(payload, dict):
        return None, f"corrupt: 顶层不是对象（{type(payload).__name__}）"
    try:
        schema = int(payload.get("schema") or 0)
    except Exception:
        return None, "corrupt: schema 不是整数"
    if schema != EVENTS_SCHEMA:
        return None, f"corrupt: schema={schema}，期望 {EVENTS_SCHEMA}"
    return payload, None


def read_cache_checked(code, events_dir: Optional[str] = None):
    """读取指定标的的子项集合，返回 ``(sections, problem)``。

    ``problem`` 四态：

    * ``None`` —— 缓存可用（``sections`` 为 dict）；
    * :data:`CACHE_MISSING` —— 缓存文件不存在；
    * :data:`_CODE_MISSING` —— 文件存在但没有该 6 位代码；
    * ``"corrupt: <原因>"`` —— 文件存在但不可用。
    """
    payload, problem = read_payload(events_dir)
    if payload is None:
        return None, problem
    normalized = _normalize_code(code)
    sections = payload.get(normalized)
    if not isinstance(sections, dict):
        if any(_CODE_RE.match(str(key)) for key in payload):
            return None, _CODE_MISSING
        return None, f"corrupt: 没有合法的 6 位代码键（{normalized}）"
    return sections, None


# ---------------------------------------------------------------------------
# CLI 接线
# ---------------------------------------------------------------------------
def _overall(sorted_values):
    values = [value for value in sorted_values if value]
    return max(values) if values else None


def build_events_block(code, data_type="stock",
                       events_dir: Optional[str] = None, now=None):
    """``evidence.events`` 块（始终返回，键齐全）。"""
    normalized = _normalize_code(code)
    if str(data_type) == "fund":
        return empty_events_block(reason="fund_not_supported", stale=False,
                                  code=normalized)

    sections, problem = read_cache_checked(normalized, events_dir)
    if sections is None:
        reason = ("cache_invalid" if str(problem or "").startswith("corrupt")
                  else ("code_missing" if problem == _CODE_MISSING
                        else "cache_missing"))
        return empty_events_block(reason=reason, stale=True, code=normalized)

    expected, _source = _expected_trade_date(now=now)
    expected_iso = expected.isoformat() if expected is not None else None
    blocks = {}
    stale_keys = []
    for key in SECTION_KEYS:
        section = _normalize_section(sections.get(key))
        fetched_day = _date_only(section.get("fetched_at")) or section.get("as_of")
        # P3-3：只有官方日历可用时才做日期过期判定；日历不可用时宁可
        # 不报过期，也不要在长假首日误报。显式 error / stale 仍照常生效。
        if section.get("error") or (expected_iso and fetched_day
                                    and fetched_day < expected_iso):
            section["stale"] = True
        if section["stale"] or not section["available"]:
            stale_keys.append(key)
        blocks[key] = section

    available = any(blocks[key]["available"] for key in SECTION_KEYS)
    stale = any(blocks[key]["stale"] for key in SECTION_KEYS)
    reason = None
    for key in SECTION_KEYS:
        if blocks[key].get("error"):
            reason = f"{key}: {blocks[key]['error']}"
            break
    if reason is None:
        for key in SECTION_KEYS:
            if blocks[key].get("reason") not in (None, ""):
                reason = blocks[key]["reason"]
                break

    block = {
        "available": available,
        "stale": stale,
        "as_of": _overall(blocks[key]["as_of"] for key in SECTION_KEYS),
        "fetched_at": _overall(blocks[key]["fetched_at"] for key in SECTION_KEYS),
        "reason": reason,
        "code": normalized,
    }
    block.update(blocks)
    return block


def events_evidence(code, data_type="stock", as_of=None,
                    events_dir: Optional[str] = None, now=None):
    """CLI 接线：返回 ``(evidence.events, warnings)``。

    * 基金：``available=False`` + ``fund_not_supported``，**不追加 warning**
      （上游事件端点只覆盖个股；每只基金都告警只会变成噪声）。
    * ``--as-of`` 回放：事件缓存只有当前快照，套到过去等于前视；显式
      ``replay_not_supported``，不读缓存、不报命中，只给一条说明。
    * 缓存缺失 / 损坏 / 过期 / 子项取数失败各给一条可操作 warning；都
      **不改**任何决策字段。
    """
    normalized = _normalize_code(code)
    if str(data_type) == "fund":
        return empty_events_block(reason="fund_not_supported", stale=False,
                                  code=normalized), []

    if str(as_of or "").strip():
        notes = [f"事件风险为收盘后快照（{normalized}）：--as-of {as_of} "
                 "回放不提供历史状态；只展示，不影响 action"]
        return empty_events_block(reason="replay_not_supported", stale=False,
                                  code=normalized), notes

    block = build_events_block(normalized, "stock",
                               events_dir=events_dir, now=now)
    notes: List[str] = []
    if not block["available"] and block.get("reason") in (
            "cache_missing", "code_missing", "cache_invalid"):
        reason = REASON_TEXT.get(block["reason"], block["reason"])
        notes.append(f"事件风险上下文不可用（{normalized}）：{reason}；"
                     "只展示，不影响 action")
        return block, notes

    for key in SECTION_KEYS:
        section = block[key]
        label = SECTION_LABELS[key]
        for warning in section.get("warnings") or []:
            notes.append(
                f"事件风险行校验（{normalized}，{label}）：{warning}；"
                "只展示，不影响 action")
        if section.get("error"):
            notes.append(
                f"事件风险取数失败（{normalized}，{label}）：{section['error']}；"
                f"已回退 {section.get('as_of') or '无可用'} 缓存（只展示，不影响 action）")
        elif section.get("reason") == "interface_suspected":
            notes.append(
                f"事件风险接口疑似异常（{normalized}，{label}）："
                "本轮对照探针报错或返回 0 行；该子项标为不可用（只展示，不影响 action）")
        elif section.get("reason") == "empty_pool_suspected":
            notes.append(
                f"事件风险接口疑似异常（{normalized}，{label}）："
                "全票池该子项为空且上一轮有数据；请核对接口（只展示，不影响 action）")
        elif section.get("stale"):
            notes.append(
                f"事件风险缓存过期（{normalized}，{label}）："
                f"as_of={section.get('as_of') or '未知'}；请重跑 "
                "tools/precompute_events.py（只展示，不影响 action）")
    return block, notes


def with_events_block(builder):
    """给错误响应构造器补上键同构的 ``evidence.events``（G35）。

    取数阶段失败时不读缓存、不触网，显式 ``available=False``；用装饰器而不是
    改 ``decision/schema.py``，让新增子块与既有契约解耦。
    """
    def wrapped(*args, **kwargs):
        document = builder(*args, **kwargs)
        evidence = document.get("evidence")
        if isinstance(evidence, dict):
            evidence["events"] = empty_events_block(reason="data_source_error",
                                                    stale=True)
        return document

    wrapped.__name__ = getattr(builder, "__name__", "wrapped")
    wrapped.__doc__ = getattr(builder, "__doc__", None)
    return wrapped