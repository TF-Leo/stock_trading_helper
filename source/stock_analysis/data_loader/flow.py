# -*- coding: utf-8 -*-
"""P1-2（issue #36）：日级资金面上下文 —— **只读缓存，绝不触网**。

数据流
------
1. 收盘后由 ``tools/precompute_flow.py`` 串行拉取东财 §4.5
   （``stock_fund_flow_120d``，最近 120 个交易日），逐票写入
   ``stock_analysis/data/flow/<code>.json``（``data/`` 已在 .gitignore）。
2. CLI 主路径只调用本模块：读缓存 → 生成摘要 → 写进 ``evidence.flow``。
   **本模块不导入 ``requests``，不调用 ``em_get``，也不调用会触网的
   ``astock.get_month_calendar``**；缓存缺失或过期时返回

      ``None`` + 原因（``summarize_flow``），或
      键齐全但 ``available=False`` 的块（``build_flow_block``），

   由 CLI 决定是否追加 warning。这样 9:15 盘前窗口不会因多端点请求被挤占。

摘要口径（写死在这里，供 README 与测试共用）
--------------------------------------------
缓存里必须带 ``source`` / ``caliber``，摘要按口径分支，**两种口径绝不混用**：

* ``caliber=eastmoney_main_net``（``source=eastmoney_push2his``）：
  ``main_net_5d`` / ``main_net_20d`` 是最近 5 / 20 个**交易日**的主力净流入
  之和（超大单 + 大单），单位**元**；``net_5d`` / ``net_20d`` 恒为 ``None``。
* ``caliber=sina_total_net``（``source=sina_fund_flow_backup``，东财整域不可达
  时的备胎）：新浪只有 ``netamount``（**当日总净流入**，没有主力/大单拆分），
  因此 ``net_5d`` / ``net_20d`` 是总净流入之和，``main_net_5d`` /
  ``main_net_20d`` **恒为 ``None``**，绝不把总净流入冒充成主力净流入。
* 两种口径不可直接比较（字段、窗口、含义都不同）；调用方必须先看 ``caliber``。

其余语义
--------
* 行数不足 5 / 20 时对应窗口为 ``None``（不拿更短的窗口冒充 20 日）。
* ``direction``：可用窗口的符号 —— ``inflow`` / ``outflow`` / ``flat``；
  ``direction_basis`` 如实标注用的是哪个字段与窗口
  （``main_net_5d_sign`` / ``main_net_20d_sign`` /
  ``net_5d_sign`` / ``net_20d_sign``）。
* ``as_of``：本次摘要实际用到的**最后一根资金流日期**（不是文件 mtime，
  也不是运行日期）。
* ``pub_lag_days=1``：与 ``docs/P1_EVIDENCE_PLAN.md`` §3.3 一致 —— 当日资金流
  收盘后才发布，回放 ``--as-of d`` 时只使用 ``date < d`` 的行。
* ``stale``：缓存末日早于「期望最新交易日」即为过期。期望交易日按**本地磁盘
  交易日历**（深交所，只读）判定；磁盘日历缺失时退回周末推算，并在
  ``expected_source`` 标注 ``estimate``。整个判定**不触网**。

本模块只服务「展示」，不参与 ``action`` / 仓位 / 触发器计算。
"""
from __future__ import annotations

import datetime as _dt
import json
import os

# 缓存目录：source/stock_analysis/data/flow/<code>.json（data/ 已 gitignore）
FLOW_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "data", "flow")

FLOW_SOURCE = "eastmoney_push2his"
SOURCE_SINA = "sina_fund_flow_backup"
CALIBER_EASTMONEY = "eastmoney_main_net"
CALIBER_SINA = "sina_total_net"
FLOW_SCHEMA_VERSION = "1.0"
PUB_LAG_DAYS = 1

# 与上游 §4.5 的 lmt=120 对齐：缓存最多 120 行，摘要窗口是其中最近 5/20 行。
FLOW_WINDOW_DAYS = 120
SUMMARY_WINDOWS = (5, 20)

#: ``evidence.flow`` 的键集合。成功 / 不可用 / 错误路径必须完全一致，
#: 保证 G35 的「错误路径与成功路径键同构」在新增子块上继续成立。
FLOW_KEYS = (
    "available", "code", "source", "caliber", "as_of", "fetched_at", "rows",
    "pub_lag_days", "main_net_5d", "main_net_20d", "net_5d", "net_20d",
    "direction", "direction_basis", "stale", "expected_date",
    "expected_source", "reason",
)

REASON_TEXT = {
    "cache_missing": "无本地资金流缓存（需先运行 tools/precompute_flow.py）",
    "cache_invalid": "本地资金流缓存格式不可用",
    "no_rows_as_of": "缓存中没有早于 as-of 的资金流行",
    "fund_not_supported": "基金无个股资金流（上游 §4.5 不覆盖 ETF/场外）",
    "data_source_error": "取数阶段失败，资金流上下文不可得",
}


def _normalize_code(code) -> str:
    """任意受支持写法 → 6 位纯数字。失败时原样返回（缓存查找自然 miss）。"""
    try:
        from stock_analysis.data_sources._astock_generated import norm_ticker
        return norm_ticker(str(code))
    except Exception:
        return str(code).strip().lower()


def _cache_path(code, flow_dir=None) -> str:
    safe = str(code).strip().lower().replace("/", "_").replace("\\", "_")
    return os.path.join(flow_dir or FLOW_DIR, f"{safe}.json")


def _read_disk_calendar(year: int, month: int):
    """只读交易日历（不触网）；没有磁盘缓存时返回 ``None``。"""
    try:
        from stock_analysis.data_sources import astock
        return astock.calendar_days_from_disk(year, month)
    except Exception:
        return None


def _expected_trade_date(now=None, as_of=None):
    """计算期望最新交易日（**纯本地**）。

    * ``now`` 为 ``datetime`` 时沿用既有 15:00 收盘规则：15:00 前视当日未收盘，
      从昨天开始找；15:00 后从今天开始找。
    * ``as_of`` 为日期字符串 / date / datetime 时，表示「回放到该日收盘」，
      直接以该日为起点（不再做 15:00 判断）。
    * 优先用磁盘交易日历（深交所官方整月文件）；文件缺失的月份退回周末推算。
      返回 ``(date, source)``，``source`` ∈ ``calendar_cache`` / ``estimate``。
    """
    if as_of is not None:
        if isinstance(as_of, _dt.datetime):
            candidate = as_of.date()
        elif isinstance(as_of, _dt.date):
            candidate = as_of
        else:
            candidate = _dt.date.fromisoformat(str(as_of)[:10])
        now = None
    else:
        now = now or _dt.datetime.now()
        candidate = now.date()
        if now.hour < 15:
            candidate = candidate - _dt.timedelta(days=1)

    source = "calendar_cache"
    for _ in range(40):
        days = _read_disk_calendar(candidate.year, candidate.month)
        is_open = None if days is None else days.get(candidate.isoformat())
        if is_open is True:
            return candidate, source
        if is_open is None:
            source = "estimate"
            if candidate.weekday() < 5:
                return candidate, source
        candidate = candidate - _dt.timedelta(days=1)
    return candidate, "estimate"


def _resolve_caliber(payload):
    """返回 ``(source, caliber)``；口径未知或自相矛盾时返回 ``(None, None)``。

    P1-2 旧缓存没有 ``caliber`` 字段：``source`` 为新浪备胎则按新浪口径，
    其余按东财口径（旧版只会写东财缓存）。显式口径优先；**绝不**根据
    「行里恰好有哪个字段」去猜口径。
    """
    source = str(payload.get("source") or FLOW_SOURCE)
    caliber = payload.get("caliber")
    if caliber in (None, ""):
        return source, (CALIBER_SINA if source == SOURCE_SINA
                        else CALIBER_EASTMONEY)
    caliber = str(caliber)
    if caliber == CALIBER_SINA and source == SOURCE_SINA:
        return source, CALIBER_SINA
    if caliber == CALIBER_EASTMONEY and source != SOURCE_SINA:
        return source, CALIBER_EASTMONEY
    return None, None


def read_cache(code, flow_dir=None):
    """读取并校验 ``flow/<code>.json``；不可用时返回 ``None``（不抛异常）。"""
    path = _cache_path(_normalize_code(code), flow_dir)
    try:
        with open(path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except Exception:
        return None
    if not isinstance(payload, dict):
        return None
    source, caliber = _resolve_caliber(payload)
    if source is None:
        return None
    rows = payload.get("rows")
    if not isinstance(rows, list) or not rows:
        return None
    field = "net_amount" if caliber == CALIBER_SINA else "main_net"
    clean = []
    for row in rows:
        if not isinstance(row, dict):
            return None
        day = row.get("date")
        try:
            day = _dt.date.fromisoformat(str(day)[:10]).isoformat()
            value = float(row.get(field))
        except Exception:
            return None
        clean.append({"date": day, field: value})
    clean.sort(key=lambda r: r["date"])
    payload["rows"] = clean
    payload["source"] = source
    payload["caliber"] = caliber
    return payload


def empty_flow_block(code=None, reason="cache_missing", expected_date=None,
                     expected_source=None):
    """不可用时的 ``evidence.flow`` 块：键与成功路径完全一致，值显式 null。

    P2（issue #40）：``code`` 必须走 ``_normalize_code``，与成功路径的
    ``summarize_flow`` 一致 —— 否则 ``sh600519`` / ``600519.SH`` 这类写法
    在错误路径会原样进 JSON，同一个字段在两条路径上给出两种编码。
    """
    return {
        "available": False,
        "code": None if code is None else _normalize_code(code),
        "source": None,
        "caliber": None,
        "as_of": None,
        "fetched_at": None,
        "rows": 0,
        "pub_lag_days": PUB_LAG_DAYS,
        "main_net_5d": None,
        "main_net_20d": None,
        "net_5d": None,
        "net_20d": None,
        "direction": None,
        "direction_basis": None,
        "stale": None,
        "expected_date": None if expected_date is None else str(expected_date),
        "expected_source": expected_source,
        "reason": reason,
    }


def _sum(rows, window, field="main_net"):
    """窗口内净流入之和；行数不足时返回 ``None``（不缩短窗口冒充）。"""
    if len(rows) < window:
        return None
    return round(sum(r[field] for r in rows[-window:]), 2)


def _direction(value, basis):
    if value is None:
        return None, None
    return ("flat" if value == 0 else ("inflow" if value > 0 else "outflow")), basis


def summarize_flow(code, as_of=None, flow_dir=None, now=None):
    """生成资金流摘要；缓存缺失 / 无效 / 无 as-of 之前的行时返回 ``None``。

    返回字典包含 :data:`FLOW_KEYS` 的全部键（``available=True``），
    调用方可直接用 ``build_flow_block`` 取得不可用时的同构块。
    """
    payload = read_cache(code, flow_dir=flow_dir)
    if payload is None:
        return None

    rows = payload["rows"]
    if as_of is not None:
        # §3.3：资金流 pub_lag_days=1，回放 d 日时不得使用 d 日收盘后发布的行。
        try:
            limit = _dt.date.fromisoformat(str(as_of)[:10]).isoformat()
        except Exception:
            return None
        rows = [r for r in rows if r["date"] < limit]
        if not rows:
            return None

    last_date = rows[-1]["date"]
    caliber = payload["caliber"]
    if caliber == CALIBER_SINA:
        # 新浪只有总净流入：只写 net_*，main_net_* 显式 null（绝不冒充）。
        sums = {w: _sum(rows, w, "net_amount") for w in SUMMARY_WINDOWS}
        main_5d = main_20d = None
        net_5d, net_20d = sums[5], sums[20]
        if net_5d is not None:
            direction, direction_basis = _direction(net_5d, "net_5d_sign")
        else:
            direction, direction_basis = _direction(net_20d, "net_20d_sign")
    else:
        # 东财主力口径：仍只写 main_net_*，net_* 显式 null。
        sums = {w: _sum(rows, w, "main_net") for w in SUMMARY_WINDOWS}
        net_5d = net_20d = None
        main_5d, main_20d = sums[5], sums[20]
        if main_5d is not None:
            direction, direction_basis = _direction(main_5d, "main_net_5d_sign")
        else:
            direction, direction_basis = _direction(main_20d, "main_net_20d_sign")

    if as_of is not None:
        try:
            base = _dt.date.fromisoformat(str(as_of)[:10]) - _dt.timedelta(days=PUB_LAG_DAYS)
        except Exception:
            base = None
        expected, expected_source = _expected_trade_date(as_of=base or as_of)
    else:
        expected, expected_source = _expected_trade_date(now=now)

    try:
        stale = _dt.date.fromisoformat(last_date) < expected
    except Exception:
        stale = None

    return {
        "available": True,
        "code": _normalize_code(code),
        "source": str(payload.get("source") or FLOW_SOURCE),
        "caliber": caliber,
        "as_of": last_date,
        "fetched_at": payload.get("fetched_at"),
        "rows": len(rows),
        "pub_lag_days": PUB_LAG_DAYS,
        "main_net_5d": main_5d,
        "main_net_20d": main_20d,
        "net_5d": net_5d,
        "net_20d": net_20d,
        "direction": direction,
        "direction_basis": direction_basis,
        "stale": stale,
        "expected_date": expected.isoformat(),
        "expected_source": expected_source,
        "reason": None,
    }


def build_flow_block(code, as_of=None, flow_dir=None, now=None):
    """``evidence.flow`` 块（始终返回；不可用时 ``available=False`` + ``reason``）。"""
    summary = summarize_flow(code, as_of=as_of, flow_dir=flow_dir, now=now)
    if summary is not None:
        return summary
    payload = read_cache(code, flow_dir=flow_dir)
    if payload is None:
        path = _cache_path(_normalize_code(code), flow_dir)
        reason = "cache_invalid" if os.path.exists(path) else "cache_missing"
    else:
        reason = "no_rows_as_of"
    expected, expected_source = _expected_trade_date(now=now, as_of=as_of)
    return empty_flow_block(code=_normalize_code(code), reason=reason,
                            expected_date=expected, expected_source=expected_source)


def flow_evidence(code, data_type="stock", as_of=None, flow_dir=None, now=None):
    """CLI 接线：返回 ``(block, warnings)``。

    * 基金：返回 ``available=False`` + ``fund_not_supported``，**不追加 warning**
      （上游 §4.5 本就不覆盖 ETF，每次基金运行都告警只会变成噪声）。
    * 股票：缓存缺失 / 过期会追加一条可操作的 warning；该 warning 只描述
      上下文不可用，不影响 ``action`` / ``status`` / 仓位。
    """
    if str(data_type) == "fund":
        return empty_flow_block(code=_normalize_code(code),
                                reason="fund_not_supported"), []

    block = build_flow_block(code, as_of=as_of, flow_dir=flow_dir, now=now)
    notes = []
    shown = block["code"] or code
    if not block["available"]:
        reason = REASON_TEXT.get(block["reason"], block["reason"])
        notes.append(f"资金面上下文不可用（{shown}）：{reason}；只展示，不影响 action")
    elif block.get("stale"):
        notes.append(
            f"资金面缓存过期（{shown}）：as_of={block['as_of']} < 期望 "
            f"{block['expected_date']}；请重跑 tools/precompute_flow.py（只展示，不影响 action）")
    return block, notes
