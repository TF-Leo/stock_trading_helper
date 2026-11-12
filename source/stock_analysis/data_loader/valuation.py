# -*- coding: utf-8 -*-
"""P1-1（issue #46）：估值上下文 —— **只读缓存，绝不触网**。

数据流
------
1. 收盘后由 ``tools/precompute_valuation.py`` 通过 baostock（研究/预计算可选
   依赖，见 ``requirements-research.txt``）拉取上游 §6.5 日频历史：
   PE(TTM) / PB(MRQ) / PS(TTM) / PCF + 换手率 + 停牌 + ST，逐票写入

       source/stock_analysis/data/valuation/<code>.json   （data/ 已 gitignore）

2. CLI 主路径只调用本模块：读缓存 → 生成摘要 → 写进 ``evidence.valuation``。
   **本模块不导入 baostock / requests，不打开 socket**；缓存缺失或过期时返回
   ``available=False`` 的同构块（``build_valuation_block``），由 CLI 决定是否
   追加 warning。9:15 盘前窗口不会因本模块多发一个请求。

摘要口径（写死在这里，供 README 与测试共用）
--------------------------------------------
* ``pe_ttm`` / ``pb`` / ``ps``：截至 ``as_of`` 的**最后一行**取值；缺失为 null。
  baostock 的 ``peTTM``/``pbMRQ``/``psTTM`` 原样保留，不做复权、不做行业中性。
* ``*_percentile_3y``：0–100 分位，定义

      100 × (窗口内严格小于当前值的样本数) / (窗口样本数 − 1)

  窗口 = 截至 ``as_of`` 的**最近 :data:`PERCENTILE_WINDOW_DAYS`（756）个交易日**；
  停牌日（``trade_status=0``）与指标非正的行**不进入样本**（亏损股的 PE 不可排序，
  与 ``docs/P1_EVIDENCE_PLAN.md`` §3.2 一致）。当前值本身缺失 / 非正、或有效样本
  少于 :data:`MIN_PERCENTILE_SAMPLES`（250）时该分位为 null，并在
  ``<metric>_percentile_reason`` 写明 ``missing`` / ``non_positive`` /
  ``insufficient_samples``；``percentile_samples`` 给出每个指标的实际样本数。
* ``turnover_latest``：最后一行换手率，**百分数**（0.31 = 0.31%），与上游一致。
* ``is_st_hist``：最后一行（``as_of`` 当日）的**历史 ST 标记**（``isST``）。
  它回答「当时是不是 ST」，与 ``instrument.is_st``（按今天的名称/状态判定）不同。
* ``as_of``：本次摘要实际用到的最后一根估值日期（不是文件 mtime，也不是运行日）。
* ``pub_lag_days=0``：估值基于当日收盘价，回放 ``--as-of d`` 时使用 ``date <= d``
  的行（P1 计划 §3.2 的 as-of 口径；不使用任何 ``date > d`` 的行）。
* ``stale``：缓存末日早于「期望最新交易日」即为过期。期望交易日按**本地磁盘
  交易日历**（深交所，只读）判定；磁盘日历缺失时退回周末推算，并在
  ``expected_source`` 标注 ``estimate``。整个判定**不触网**。

本模块只服务「展示」，不参与 ``action`` / 仓位 / 触发器计算。
"""
from __future__ import annotations

import datetime as _dt
import json
import os

# 缓存目录：source/stock_analysis/data/valuation/<code>.json（data/ 已 gitignore）
VALUATION_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "data", "valuation")

VALUATION_SOURCE = "baostock_valuation"
VALUATION_SCHEMA_VERSION = "1.0"
UPSTREAM = "a-stock-data v3.10.4 §6.5 baostock_valuation_history"

#: §3.2：过去 756 个交易日（≈3 年）分位；回放时只取 ``date <= as_of`` 的行。
PERCENTILE_WINDOW_DAYS = 756
#: 有效样本少于此数时不给分位（null + reason），避免用几十行冒充「3 年」。
MIN_PERCENTILE_SAMPLES = 250
#: 估值基于当日收盘价：与资金流不同，回放 d 日可用 d 日行（P1 计划 §3.2）。
PUB_LAG_DAYS = 0

METRIC_FIELDS = (
    ("pe", "pe_ttm"),
    ("pb", "pb"),
    ("ps", "ps"),
)

#: ``evidence.valuation`` 的键集合。成功 / 不可用 / 错误路径必须完全一致，
#: 保证 G35 的「错误路径与成功路径键同构」在新增子块上继续成立。
VALUATION_KEYS = (
    "available", "code", "source", "as_of", "fetched_at", "rows",
    "pub_lag_days", "pe_ttm", "pb", "ps",
    "pe_percentile_3y", "pb_percentile_3y", "ps_percentile_3y",
    "pe_percentile_reason", "pb_percentile_reason", "ps_percentile_reason",
    "percentile_window_days", "percentile_samples",
    "turnover_latest", "is_st_hist",
    "stale", "expected_date", "expected_source", "reason",
)

REASON_TEXT = {
    "cache_missing": "无本地估值缓存（需先运行 tools/precompute_valuation.py）",
    "cache_invalid": "本地估值缓存格式不可用",
    "no_rows_as_of": "缓存中没有不晚于 as-of 的估值行",
    "fund_not_supported": "基金无 baostock 个股估值历史（上游 §6.5 仅覆盖沪深个股）",
    "data_source_error": "取数阶段失败，估值上下文不可得",
}

_CACHE_ROW_FIELDS = ("date", "close", "pe_ttm", "pb", "ps", "pcf",
                     "turnover", "trade_status", "is_st")


def _normalize_code(code) -> str:
    """任意受支持写法 → 6 位纯数字。失败时原样返回（缓存查找自然 miss）。"""
    try:
        from stock_analysis.data_sources._astock_generated import norm_ticker
        return norm_ticker(str(code))
    except Exception:
        return str(code).strip().lower()


def _cache_path(code, valuation_dir=None) -> str:
    safe = str(code).strip().lower().replace("/", "_").replace("\\", "_")
    return os.path.join(valuation_dir or VALUATION_DIR, f"{safe}.json")


def _expected_trade_date(now=None, as_of=None):
    """期望最新交易日（**纯本地**）；委托给 P1-2 的同一份判定，避免第二套日历。

    ``pub_lag_days=0`` 由调用方体现（直接传 as_of，不减 lag）。
    """
    from stock_analysis.data_loader.flow import _expected_trade_date as _base
    return _base(now=now, as_of=as_of)


def _as_float(value):
    if value is None or value == "":
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    if out != out:  # NaN
        return None
    return out


def _as_flag(value):
    """``1``/``0`` → bool；空值或未知 → None（不把未知伪装成 False）。"""
    if value is None or value == "":
        return None
    try:
        return bool(int(float(value)))
    except (TypeError, ValueError):
        return None


def read_cache(code, valuation_dir=None):
    """读取并校验 ``valuation/<code>.json``；不可用时返回 ``None``（不抛异常）。"""
    path = _cache_path(_normalize_code(code), valuation_dir)
    try:
        with open(path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except Exception:
        return None
    if not isinstance(payload, dict):
        return None
    if payload.get("source") not in (None, "", VALUATION_SOURCE):
        return None
    rows = payload.get("rows")
    if not isinstance(rows, list) or not rows:
        return None
    clean = []
    for row in rows:
        if not isinstance(row, dict) or any(k not in row for k in _CACHE_ROW_FIELDS):
            return None
        try:
            day = _dt.date.fromisoformat(str(row["date"])[:10]).isoformat()
        except Exception:
            return None
        trade_status = _as_flag(row["trade_status"])
        is_st = _as_flag(row["is_st"])
        if trade_status is None:
            return None
        clean.append({
            "date": day,
            "close": _as_float(row["close"]),
            "pe_ttm": _as_float(row["pe_ttm"]),
            "pb": _as_float(row["pb"]),
            "ps": _as_float(row["ps"]),
            "pcf": _as_float(row["pcf"]),
            "turnover": _as_float(row["turnover"]),
            "trade_status": trade_status,
            "is_st": is_st,
        })
    clean.sort(key=lambda r: r["date"])
    payload["rows"] = clean
    return payload


def empty_valuation_block(code=None, reason="cache_missing", expected_date=None,
                          expected_source=None):
    """不可用时的 ``evidence.valuation`` 块：键与成功路径完全一致，值显式 null。

    P2（issue #40）：``code`` 与 ``evidence.flow`` 同理走 ``_normalize_code``，
    保证错误路径与成功路径对同一标的给出同一种编码（6 位纯码）。
    """
    return {
        "available": False,
        "code": None if code is None else _normalize_code(code),
        "source": None,
        "as_of": None,
        "fetched_at": None,
        "rows": 0,
        "pub_lag_days": PUB_LAG_DAYS,
        "pe_ttm": None,
        "pb": None,
        "ps": None,
        "pe_percentile_3y": None,
        "pb_percentile_3y": None,
        "ps_percentile_3y": None,
        "pe_percentile_reason": None,
        "pb_percentile_reason": None,
        "ps_percentile_reason": None,
        "percentile_window_days": None,
        "percentile_samples": {"pe": None, "pb": None, "ps": None},
        "turnover_latest": None,
        "is_st_hist": None,
        "stale": None,
        "expected_date": None if expected_date is None else str(expected_date),
        "expected_source": expected_source,
        "reason": reason,
    }


def percentile_rank(current, values):
    """分位（0–100）：``100 × count(v < current) / (n − 1)``；样本 <2 时 None。

    当前值包含在 ``values`` 里。并列值不计入「严格小于」，因此最大值恒为 100。
    这是本模块唯一的百分比定义，README / 测试 / 预计算共用。
    """
    if current is None or len(values) < 2:
        return None
    less = sum(1 for value in values if value < current)
    return round(100.0 * less / (len(values) - 1), 2)


def _metric_percentile(field, latest_value, window):
    """返回 ``(percentile, reason, sample_count)``；不在窗口内造假。"""
    if latest_value is None:
        return None, "missing", 0
    if latest_value <= 0:
        return None, "non_positive", 0
    sample = [row[field] for row in window
              if row["trade_status"] and row[field] is not None and row[field] > 0]
    if len(sample) < MIN_PERCENTILE_SAMPLES:
        return None, "insufficient_samples", len(sample)
    return percentile_rank(latest_value, sample), None, len(sample)


def summarize_valuation(code, as_of=None, valuation_dir=None, now=None):
    """生成估值摘要；缓存缺失 / 无效 / 无 as-of 之前的行时返回 ``None``。

    返回字典包含 :data:`VALUATION_KEYS` 的全部键（``available=True``）。
    """
    payload = read_cache(code, valuation_dir=valuation_dir)
    if payload is None:
        return None

    rows = payload["rows"]
    expected, expected_source = _expected_trade_date(now=now, as_of=as_of)
    if as_of is not None:
        try:
            limit = _dt.date.fromisoformat(str(as_of)[:10]).isoformat()
        except Exception:
            return None
        rows = [row for row in rows if row["date"] <= limit]
        if not rows:
            return None

    last = rows[-1]
    window = rows[-PERCENTILE_WINDOW_DAYS:]
    percentiles, reasons, samples = {}, {}, {}
    for metric, field in METRIC_FIELDS:
        value = last[field]
        percentile, reason, count = _metric_percentile(field, value, window)
        percentiles[metric] = percentile
        reasons[metric] = reason
        samples[metric] = count

    try:
        stale = _dt.date.fromisoformat(last["date"]) < expected
    except Exception:
        stale = None

    return {
        "available": True,
        "code": _normalize_code(code),
        "source": VALUATION_SOURCE,
        "as_of": last["date"],
        "fetched_at": payload.get("fetched_at"),
        "rows": len(rows),
        "pub_lag_days": PUB_LAG_DAYS,
        "pe_ttm": None if last["pe_ttm"] is None else round(last["pe_ttm"], 4),
        "pb": None if last["pb"] is None else round(last["pb"], 4),
        "ps": None if last["ps"] is None else round(last["ps"], 4),
        "pe_percentile_3y": percentiles["pe"],
        "pb_percentile_3y": percentiles["pb"],
        "ps_percentile_3y": percentiles["ps"],
        "pe_percentile_reason": reasons["pe"],
        "pb_percentile_reason": reasons["pb"],
        "ps_percentile_reason": reasons["ps"],
        "percentile_window_days": PERCENTILE_WINDOW_DAYS,
        "percentile_samples": samples,
        "turnover_latest": (None if last["turnover"] is None
                            else round(last["turnover"], 4)),
        "is_st_hist": last["is_st"],
        "stale": stale,
        "expected_date": expected.isoformat(),
        "expected_source": expected_source,
        "reason": None,
    }


def build_valuation_block(code, as_of=None, valuation_dir=None, now=None):
    """``evidence.valuation`` 块（始终返回；不可用时 ``available=False`` + ``reason``）。"""
    summary = summarize_valuation(code, as_of=as_of,
                                  valuation_dir=valuation_dir, now=now)
    if summary is not None:
        return summary
    payload = read_cache(code, valuation_dir=valuation_dir)
    if payload is None:
        path = _cache_path(_normalize_code(code), valuation_dir)
        reason = "cache_invalid" if os.path.exists(path) else "cache_missing"
    else:
        reason = "no_rows_as_of"
    expected, expected_source = _expected_trade_date(now=now, as_of=as_of)
    return empty_valuation_block(code=_normalize_code(code), reason=reason,
                                 expected_date=expected,
                                 expected_source=expected_source)


def valuation_evidence(code, data_type="stock", as_of=None, valuation_dir=None,
                       now=None):
    """CLI 接线：返回 ``(block, warnings)``。

    * 基金：返回 ``available=False`` + ``fund_not_supported``，**不追加 warning**
      （上游 §6.5 不覆盖基金，每次基金运行都告警只会变成噪声）。
    * 股票：缓存缺失 / 过期会追加一条可操作的 warning；该 warning 只描述
      上下文不可用，不影响 ``action`` / ``status`` / 仓位。
    """
    if str(data_type) == "fund":
        return empty_valuation_block(code=_normalize_code(code),
                                     reason="fund_not_supported"), []

    block = build_valuation_block(code, as_of=as_of,
                                  valuation_dir=valuation_dir, now=now)
    notes = []
    shown = block["code"] or code
    if not block["available"]:
        reason = REASON_TEXT.get(block["reason"], block["reason"])
        notes.append(f"估值上下文不可用（{shown}）：{reason}；只展示，不影响 action")
    elif block.get("stale"):
        notes.append(
            f"估值缓存过期（{shown}）：as_of={block['as_of']} < 期望 "
            f"{block['expected_date']}；请重跑 tools/precompute_valuation.py"
            f"（只展示，不影响 action）")
    return block, notes
