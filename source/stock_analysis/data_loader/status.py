# -*- coding: utf-8 -*-
"""P0-3 真实交易状态：ST / 停牌 / 涨跌停 / 退市信号（issue #6）。

数据来源（全部经 ``data_sources/astock.py`` 的固定快照，不引入新依赖）：

* **腾讯实时快照 §1.1**：当日涨停价 / 跌停价 / ``is_stale`` / 名称。
* **东财 §6.8 ST 名单**：沪深京风险警示快照，经上游 ``em_get`` 串行限流；
  本地短缓存（1 个交易日，目录 ``stock_analysis/data/status/``，已 gitignore）。
* **深交所 §12 交易日历**：判断"现在能否用报价判断停牌"；不可用时回退周末规则。

设计边界
--------
* ``is_stale`` **不等于**停牌：集合竞价前（09:30 前）腾讯同样返回
  ``price == last_close`` 且成交量为 0。只有"交易日 10:00 后 / 已收盘 /
  非交易日"的定格报价才判停牌；盘前明确返回 ``None``（无法判断）。
* 退市：本项目不引入 baostock（issue #1 决策），拿不到官方退市日。
  只在"报价不可用 + 行情已连续滞后 >= 20 个交易日"或"报价定格且名称含退市
  标记"时给出 *疑似* 退市信号，并硬提示不可交易 —— 不编造官方结论。
* 本模块只产出**状态事实**，不改 ``decision/actions.py`` 的决策表。可执行性
  拦截由 CLI 依据 ``TradingStatus.execution_block()`` 升为 ``untradeable``。
"""
from __future__ import annotations

import datetime as _dt
import json
import math
import os
import re
import sys
from dataclasses import dataclass, field
from typing import Dict, List, Optional

import pandas as pd

from stock_analysis.data_sources import astock

__all__ = [
    "OFFLINE_ENV",
    "DELIST_LAG_DAYS",
    "ST_LIST_RETRY",
    "StListEmptyError",
    "StListDuplicateError",
    "StListSnapshot",
    "TradingStatus",
    "get_st_list",
    "resolve_trading_status",
    "reset_cache",
    "cache_path",
]

OFFLINE_ENV = "ASTOCK_STATUS_OFFLINE"
# 行情连续滞后多少个交易日仍取不到报价 → 疑似退市/长期停牌（可复核阈值）。
DELIST_LAG_DAYS = int(os.getenv("ASTOCK_DELIST_LAG_DAYS", "20"))
# issue #106：空表 / 重复代码是「名单本身可疑」，允许重试一次（仍异常则回退缓存）。
ST_LIST_RETRY = 1

_GEM_BOARDS = ("sz_gem", "sh_star")
_TRADING_DAY_LOOKBACK = 30
_ST_MEMO: Dict[tuple, "StListSnapshot"] = {}


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------
def _num(value) -> Optional[float]:
    """来源数值 → 有限 float；空值 / NaN / inf / bool 一律 None。"""
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _maybe_num(value) -> Optional[float]:
    number = _num(value)
    if number is None:
        return None
    return round(number, 2)


def _offline() -> bool:
    for key in (OFFLINE_ENV, "ASTOCK_DISABLE_NETWORK"):
        if str(os.getenv(key, "")).strip().lower() in ("1", "true", "yes", "on"):
            return True
    return False


def _looks_st(name: str) -> bool:
    """名称是否带风险警示前缀：ST / *ST / S*ST / SST。"""
    return bool(re.match(r"^\s*S?\*?ST", str(name or ""), re.IGNORECASE))


def _looks_delisted(name: str) -> bool:
    text = str(name or "").strip()
    return "退市" in text or text.endswith("退") or text.startswith("退")


def _is_trading_day(day: _dt.date) -> bool:
    try:
        value = astock.is_trading_day(day)
        if value is not None:
            return bool(value)
    except Exception:
        pass
    return day.weekday() < 5


def _previous_trading_day(day: _dt.date) -> _dt.date:
    for _ in range(_TRADING_DAY_LOOKBACK):
        day = day - _dt.timedelta(days=1)
        if _is_trading_day(day):
            return day
    return day


def _snapshot_session_date(now: _dt.datetime) -> _dt.date:
    """快照所描述的交易日：盘中（>=10:00）用当日，盘前/非交易日用上一交易日。"""
    today = now.date()
    if _is_trading_day(today) and (now.hour, now.minute) >= (10, 0):
        return today
    return _previous_trading_day(today)


def _assessment_phase(now: _dt.datetime) -> str:
    """报价定格能否用于停牌判定：盘前 ``preopen``，其余 ``session``。"""
    if _is_trading_day(now.date()) and (now.hour, now.minute) < (10, 0):
        return "preopen"
    return "session"


# ---------------------------------------------------------------------------
# ST 名单短缓存
# ---------------------------------------------------------------------------
def cache_path() -> str:
    # <repo>/source/stock_analysis/data/status/st_list.json —— data/ 已 gitignore
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(root, "data", "status", "st_list.json")


@dataclass
class StListSnapshot:
    available: bool
    stocks: Dict[str, dict] = field(default_factory=dict)
    coverage: str = ""
    source: str = ""
    fetched_at: Optional[str] = None
    session_date: Optional[str] = None
    degraded: bool = False
    warning: Optional[str] = None
    # issue #106：三态区分 —— ``ok`` / ``empty``（可疑空表）/ ``duplicate``
    # （可疑重复代码）/ ``failed``（请求失败）/ ``offline``（显式离线未请求）。
    # ``available=False`` 时 state 必须明确说明「为什么不可用」，调用方不得
    # 把空表当成「今天没有 ST」。
    state: str = "ok"
    # 回退本地缓存时为 True；同时保留缓存日期（``fetched_at`` / ``session_date``）。
    stale: bool = False
    retried: bool = False
    cleaned_duplicates: int = 0

    @property
    def codes(self):
        return frozenset(self.stocks)


class StListEmptyError(RuntimeError):
    """ST 名单返回空表：可疑状态，不能等同于「今日无 ST」。"""


class StListDuplicateError(RuntimeError):
    """ST 名单出现无法清洗的重复代码（同一代码内容冲突）。"""


def _st_failure_kind(exc: BaseException) -> str:
    """把取数异常归类为 ``empty`` / ``duplicate`` / ``failed``。

    上游 ``st_stock_list()`` 没有专用异常类型，只能按错误文案归类；归类只影响
    warning 措辞与「是否重试一次」，不会把失败静默成空名单。
    """
    if isinstance(exc, StListEmptyError):
        return "empty"
    if isinstance(exc, StListDuplicateError):
        return "duplicate"
    text = str(exc or "")
    if "代码重复" in text or "重复" in text:
        return "duplicate"
    if "为空" in text or "0 条" in text or "empty" in text.lower():
        return "empty"
    return "failed"


def _snapshot_from_payload(payload, degraded=False, warning=None,
                           state="ok", stale=False) -> StListSnapshot:
    stocks = {}
    raw = payload.get("stocks")
    if isinstance(raw, dict):
        for code, info in raw.items():
            code = str(code).zfill(6)
            if not re.fullmatch(r"\d{6}", code):
                continue
            stocks[code] = dict(info or {})
    return StListSnapshot(
        available=bool(stocks),
        stocks=stocks,
        coverage=str(payload.get("coverage") or ""),
        source=str(payload.get("source") or "eastmoney"),
        fetched_at=payload.get("fetched_at"),
        session_date=payload.get("session_date"),
        degraded=degraded,
        warning=warning,
        state=state,
        stale=stale,
    )


def _read_disk_cache() -> Optional[dict]:
    path = cache_path()
    try:
        with open(path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
        if not isinstance(payload, dict):
            return None
        if int(payload.get("schema", 0)) != 1:
            return None
        if not isinstance(payload.get("stocks"), dict):
            return None
        return payload
    except Exception:
        return None


def _write_disk_cache(snapshot: StListSnapshot, now: _dt.datetime) -> None:
    payload = {
        "schema": 1,
        "source": snapshot.source,
        "coverage": snapshot.coverage,
        "fetched_at": now.isoformat(),
        "session_date": _snapshot_session_date(now).isoformat(),
        "count": len(snapshot.stocks),
        "stocks": snapshot.stocks,
    }
    path = cache_path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=1, sort_keys=True)
        os.replace(tmp, path)
    except Exception as exc:  # 缓存写失败不影响主流程
        print(f"[WARN] 写 ST 名单缓存失败：{exc}", file=sys.stderr)


def _snapshot_from_frame(frame, allow_clean: bool = False) -> StListSnapshot:
    """把上游 DataFrame 变成快照；空表 / 重复代码必须显式报错。

    ``allow_clean=True`` 用于「已重试一次」的第二次结果：内容**完全一致**的
    重复代码可以清洗掉（计数并告警）；内容冲突的重复代码仍然报错，因为无法
    判断哪一行才算权威名单。
    """
    if not isinstance(frame, pd.DataFrame) or frame.empty:
        raise StListEmptyError("ST 名单为空，不能当成完整快照")
    stocks = {}
    cleaned = 0
    for row in frame.to_dict("records"):
        raw_code = row.get("code")
        code = str(raw_code or "").zfill(6)
        if not re.fullmatch(r"\d{6}", code):
            raise RuntimeError(f"ST 名单代码异常: {raw_code!r}")
        name = str(row.get("name") or "").strip()
        if not name:
            raise RuntimeError(f"ST 名单 {code} 缺少名称，无法复核")
        info = {
            "name": name,
            "st_type": str(row.get("st_type") or ("*ST" if name.startswith("*") else "ST")),
            "market": str(row.get("market") or ""),
            "price": _num(row.get("price")),
            "pct_change": _num(row.get("pct_change")),
        }
        if code in stocks:
            if allow_clean and stocks[code] == info:
                cleaned += 1
                continue
            raise StListDuplicateError(
                f"ST 名单代码 {raw_code!r} 重复（{stocks[code]!r} vs {info!r}），"
                "无法判断哪一行才是权威名单")
        stocks[code] = info
    coverage = str(getattr(frame, "attrs", {}).get("coverage") or "")
    source = ""
    if "source" in frame.columns:
        source = str(frame["source"].iloc[0] or "")
    warning = (f"ST 名单含 {cleaned} 条内容一致的重复代码，已清洗"
               if cleaned else None)
    return StListSnapshot(available=True, stocks=stocks, coverage=coverage,
                          source=source or "eastmoney", warning=warning,
                          state="ok", cleaned_duplicates=cleaned)


def _fetch_st_list(allow_clean: bool = False) -> StListSnapshot:
    from stock_analysis.data_sources import _astock_generated as _g

    frame = _g.st_stock_list()
    return _snapshot_from_frame(frame, allow_clean=allow_clean)


def _st_list_warning(kind: str, reason: str, cached: Optional[dict],
                     retried: bool) -> str:
    """空表 / 重复 / 失败的统一告警文案。

    空表与失败**绝不**写成「今日无 ST」；回退缓存必须写明缓存日期与
    ``stale=true``。
    """
    label = {
        "empty": "ST 名单为空（可疑）",
        "duplicate": "ST 名单代码重复（可疑）",
        "failed": "ST 名单接口失败",
    }.get(kind, "ST 名单接口异常")
    retry_note = "，已重试 1 次仍异常" if retried else ""
    if cached:
        date = (str(cached.get("fetched_at") or "")
                or str(cached.get("session_date") or "") or "未知日期")
        coverage = str(cached.get("coverage") or "") or "未知"
        return (f"{label}{retry_note}（{reason}），回退到 {date} 的本地缓存"
                f"（覆盖 {coverage}，stale=true，可能过时；不能据此确认名单最新）")
    if kind == "empty":
        return (f"{label}{retry_note}（{reason}）：接口返回 0 条，不能当成"
                "「今日没有 ST」，is_st 只能依据实时报价/本地名称判断")
    if kind == "duplicate":
        return (f"{label}{retry_note}（{reason}）：名单不能当成完整快照，"
                "is_st 只能依据实时报价/本地名称判断")
    return (f"{label}（{reason}）{retry_note}，且无本地缓存："
            "is_st 只能依据实时报价名称判断")


def get_st_list(now: Optional[_dt.datetime] = None,
                force: bool = False) -> StListSnapshot:
    """东财 ST 名单（1 个交易日短缓存；失败回退旧缓存，绝不中断主流程）。

    issue #106 三态：``state='ok'``（名单正常）/ ``'empty'``（空表，可疑）/
    ``'failed'``（请求失败，含 ``'duplicate'`` 重复代码异常）。空表与失败都会
    写明确 warning，且**不会**被当成「今日无 ST」。
    """
    now = now or _dt.datetime.now()
    key = (_snapshot_session_date(now).isoformat(), _offline())
    if not force and key in _ST_MEMO:
        return _ST_MEMO[key]

    cached = _read_disk_cache()
    cache_fresh = bool(
        cached and cached.get("session_date") == key[0]
        and isinstance(cached.get("stocks"), dict) and cached["stocks"])

    if cache_fresh and not force:
        snapshot = _snapshot_from_payload(cached)
    elif _offline():
        # 显式离线：能用旧缓存就用，不触网；没有则返回不可用（由调用方降级）。
        snapshot = (_snapshot_from_payload(
                        cached, degraded=True, state="offline", stale=True,
                        warning=(f"显式离线（{OFFLINE_ENV}=1），使用 "
                                 f"{cached.get('fetched_at') or '未知日期'} 的"
                                 "本地 ST 名单缓存（stale=true，可能过时）"))
                    if cached
                    else StListSnapshot(available=False, source="offline",
                                        state="offline",
                                        warning="显式离线：未请求 ST 名单"))
    else:
        snapshot = None
        last_exc: Optional[BaseException] = None
        kind = "failed"
        retried = False
        for attempt in range(1 + ST_LIST_RETRY):
            try:
                snapshot = _fetch_st_list(allow_clean=(attempt > 0))
                if attempt > 0:
                    retried = True
                    snapshot.retried = True
                break
            except Exception as exc:  # 网络 / 空表 / 重复代码
                last_exc = exc
                kind = _st_failure_kind(exc)
                # 只有「名单本身可疑」（空表 / 重复代码）才重试一次；
                # 网络失败不额外重试，避免放大限流压力。
                if attempt < ST_LIST_RETRY and kind in ("empty", "duplicate"):
                    retried = True
                    continue
                break
        if snapshot is None:
            reason = f"{type(last_exc).__name__}: {last_exc}"
            warning = _st_list_warning(kind, reason, cached, retried)
            if cached:
                snapshot = _snapshot_from_payload(
                    cached, degraded=True, warning=warning, state=kind,
                    stale=True)
                snapshot.retried = retried
            else:
                snapshot = StListSnapshot(
                    available=False, source="unavailable", state=kind,
                    warning=warning, retried=retried)
        else:
            _write_disk_cache(snapshot, now)

    _ST_MEMO[key] = snapshot
    return snapshot


def reset_cache() -> None:
    """清进程内 memo（测试与长驻进程显式刷新用）。"""
    _ST_MEMO.clear()


# ---------------------------------------------------------------------------
# 单票状态
# ---------------------------------------------------------------------------
@dataclass
class TradingStatus:
    is_st: Optional[bool] = None
    # issue #106 additive：ST 判定来源（exchange_list / quote_name /
    # local_name / unavailable）。渲染层据此措辞，不得把 unavailable 写成「非 ST」。
    is_st_source: str = "unavailable"
    is_suspended: Optional[bool] = None
    limit_up: Optional[float] = None
    limit_down: Optional[float] = None
    quote_stale: Optional[bool] = None
    status_source: str = "unavailable"
    price_limit_pct: Optional[float] = None
    one_word_limit: Optional[str] = None  # 'up' / 'down' / None
    delisted_suspect: bool = False
    warnings: List[str] = field(default_factory=list)

    def execution_block(self, pending, expectancy_ok: bool, holding: bool) -> Optional[str]:
        """P0-3 可执行性硬约束；返回 None 表示不拦截。

        只回答"这笔动作今天能不能成交"，不改变 ``decision/actions.py`` 的
        买卖判定本身：
        * 停牌 / 疑似退市 → 任何动作都不可执行；
        * 一字涨停 → 买不进（pending=entry 且买侧期望达标、当前空仓）；
        * 一字跌停 → 卖不出（pending 为风控离场且当前持仓）。
        """
        if self.is_suspended:
            if self.delisted_suspect:
                return "行情与实时报价同时停滞，疑似退市/长期停牌，判定不可交易（P0-3）"
            return "腾讯报价定格且无当日成交，判定停牌，今日不可交易（P0-3）"
        if self.one_word_limit == "up" and pending == "entry" and expectancy_ok and not holding:
            return "一字涨停（最高=最低=涨停价），买单无法成交，判定不可交易（P0-3）"
        if self.one_word_limit == "down" and pending in ("stop", "target", "time") and holding:
            return "一字跌停（最高=最低=跌停价），卖单无法成交，判定不可交易（P0-3）"
        return None


def _static_price_limit_pct(board: str, is_st: Optional[bool]) -> float:
    """板块静态涨跌停幅度。

    **2026-07-06 起沪深主板 ST/*ST 已由 5% 并轨为 10%**（上交所/深交所
    《交易规则（2026 年修订）》）；创业板/科创板 ST 维持 20%，北交所 30%。
    因此这里不再对主板 ST 做 5% 修正 —— issue #6 任务书写的「ST 按 5%」
    是旧规则，按现行交易所规则与腾讯实时涨跌停价为准（见 README）。
    真实报价可用时最终以 ``limit_up/last_close`` 推算为准。
    """
    if board in _GEM_BOARDS:
        return 0.20
    if board == "bj":
        return 0.30
    if board == "fund":
        return 0.10
    return 0.10


def resolve_trading_status(code: str, board: str, name: Optional[str] = None,
                           data_lag_days: Optional[int] = None,
                           now: Optional[_dt.datetime] = None,
                           st_snapshot: Optional[StListSnapshot] = None,
                           quote: Optional[dict] = None,
                           quote_error: Optional[str] = None) -> TradingStatus:
    """汇总单票交易状态。**永不抛异常**：任何来源失败都降级并写 warning。

    ``quote`` / ``quote_error`` 仅供测试注入；生产路径由本函数自行调用
    ``astock.fetch_quote``。
    """
    now = now or _dt.datetime.now()
    warnings: List[str] = []
    sources: List[str] = []

    snapshot = st_snapshot if st_snapshot is not None else get_st_list(now=now)
    if snapshot.warning:
        warnings.append(snapshot.warning)

    if quote is None and quote_error is None and not _offline():
        try:
            quote = astock.fetch_quote(code)
        except Exception as exc:
            quote_error = f"{type(exc).__name__}: {exc}"
    elif quote is None and quote_error is None and _offline():
        sources.append("offline")

    quote_ok = False
    price = last_close = high = low = None
    if isinstance(quote, dict):
        price = _num(quote.get("price"))
        last_close = _num(quote.get("last_close"))
        high = _num(quote.get("high"))
        low = _num(quote.get("low"))
        quote_ok = bool(price and price > 0 and last_close and last_close > 0)
        if not quote_ok:
            warnings.append("腾讯报价字段异常（价格/昨收缺失或非有限值），交易状态降级")

    status_source_parts = []
    if snapshot.available:
        status_source_parts.append("st_cache" if snapshot.degraded else "eastmoney_st_list")
    if quote_ok:
        status_source_parts.append("tencent_quote")
    elif quote_error or quote is not None:
        status_source_parts.append("quote_unavailable")
    status_source_parts.extend(sources)

    # ---- ST ---------------------------------------------------------------
    # 解析优先级（issue #17 验收退回；issue #106 增加来源标注与冲突保守）：
    #   ① ST 名单可用 → 权威 True/False；名称带 ST 而名单未含 → True + 冲突 warning
    #   ② 腾讯报价可用 → 按报价名称前缀判断（ST → True，否则 False）
    #   ③ 报价不可用、传入 name 可用 → 名称含 ST → True + 「仅依据名称」warning；
    #      否则 → None + 「is_st 未知」warning（绝不把未知伪装成 False）
    #   ④ 全部不可用 → None + warning
    is_st: Optional[bool] = None
    is_st_source = "unavailable"
    quote_name = str(quote.get("name") or "").strip() if quote_ok else ""
    provided_name = str(name or "").strip()
    # get_stock_name 取数失败时会把代码本身当名称返回；代码不是可用名称。
    name_usable = bool(provided_name) and provided_name != str(code).strip()
    echo_name = quote_name or provided_name

    quote_says_st = bool(quote_name) and _looks_st(quote_name)
    local_says_st = name_usable and _looks_st(provided_name)
    name_st_sources = []
    if quote_says_st:
        name_st_sources.append("quote_name")
    if local_says_st:
        name_st_sources.append("local_name")

    if snapshot.available:
        is_st = code in snapshot.codes
        is_st_source = "exchange_list"
        if not is_st and name_st_sources:
            # 名单说「不是 ST」、名称说「是 ST」→ 取保守的一方并告警。
            is_st = True
            is_st_source = name_st_sources[0]
            which = "实时报价" if is_st_source == "quote_name" else "本地"
            warnings.append(
                f"ST 名单与名称不一致（名单未含该代码，{which}名称带 ST 前缀）："
                "按 ST 保守处理")
        elif is_st and quote_name and not quote_says_st:
            warnings.append(
                "ST 名单与实时报价名称不一致（名单含该代码，报价名称未带 ST "
                "前缀）：以名单为准，仍按 ST 处理")
    elif quote_ok and quote_name:
        is_st = quote_says_st
        is_st_source = "quote_name"
        if not is_st and local_says_st:
            # 报价名称说「不是 ST」、本地名称说「是 ST」→ 同样取保守的一方。
            is_st = True
            is_st_source = "local_name"
            warnings.append(
                "实时报价名称与本地名称不一致（报价名称不含 ST，本地名称带 ST "
                "前缀）：按 ST 保守处理")
    elif quote_ok and name_usable:
        # 报价可用但名称为空：不能用空名称冒充「非 ST」，退回本地名称。
        is_st_source = "local_name"
        if local_says_st:
            is_st = True
            warnings.append(
                f"仅依据名称判断 ST（ST 名单与实时报价名称均不可用）："
                f"{provided_name}，无法交叉复核")
        else:
            warnings.append(
                f"is_st 未知（名称 {provided_name} 不含 ST 标记，但 ST 名单与"
                "实时报价名称均不可用，不能据此确认非 ST）")
    elif quote_ok:
        warnings.append(
            "is_st 未知：ST 名单、实时报价名称与本地名称均不可用，无法判断是否 ST")
    elif name_usable:
        is_st_source = "local_name"
        if local_says_st:
            is_st = True
            warnings.append(
                f"仅依据名称判断 ST（ST 名单与实时报价均不可用）：{provided_name}，"
                "无法交叉复核")
        else:
            warnings.append(
                f"is_st 未知（名称 {provided_name} 不含 ST 标记，但 ST 名单与"
                "实时报价均不可用，不能据此确认非 ST）")
    else:
        warnings.append(
            "is_st 未知：ST 名单、实时报价与本地名称均不可用，无法判断是否 ST")
    if is_st:
        warnings.append(
            f"ST 标的（{echo_name or code}）：风险警示股，退市与流动性风险显著高于普通股")

    # ---- 涨跌停价与幅度 ---------------------------------------------------
    limit_up = limit_down = None
    if quote_ok:
        limit_up = _maybe_num(quote.get("limit_up"))
        limit_down = _maybe_num(quote.get("limit_down"))
        if limit_up is not None and limit_up <= 0:
            limit_up = None
        if limit_down is not None and limit_down <= 0:
            limit_down = None

    static_pct = _static_price_limit_pct(board, is_st)
    price_limit_pct = static_pct
    if limit_up and limit_down and last_close:
        implied = round(((limit_up / last_close - 1) + (1 - limit_down / last_close)) / 2, 2)
        if 0.04 <= implied <= 0.31:
            if abs(implied - static_pct) > 0.005:
                warnings.append(
                    f"涨跌停幅度与静态规则不一致：报价推算 {implied:.0%}，"
                    f"静态规则 {static_pct:.0%}（以报价为准）")
            price_limit_pct = implied

    # ---- 停牌 / 退市 ------------------------------------------------------
    quote_stale = bool(quote.get("is_stale")) if quote_ok else None
    is_suspended: Optional[bool] = None
    delisted_suspect = False
    if quote_ok:
        if quote_stale:
            if _assessment_phase(now) == "preopen":
                is_suspended = None  # 集合竞价前定格是正常现象，不判停牌
            else:
                is_suspended = True
                reason = quote.get("stale_reason")
                detail = f"（{reason}）" if reason else ""
                if _looks_delisted(quote_name):
                    delisted_suspect = True
                    warnings.append(
                        f"报价定格且名称含退市标记（{quote_name}），疑似退市/退市整理{detail}")
                warnings.append(f"腾讯报价定格且无当日成交，判定停牌/无成交{detail}")
        else:
            is_suspended = False

    if quote_error and not quote_ok:
        warnings.append(
            f"无法获取实时交易状态（腾讯报价不可用：{quote_error}）："
            "停牌/退市/代码无效无法区分")
    if (not quote_ok and data_lag_days is not None
            and int(data_lag_days) >= DELIST_LAG_DAYS):
        delisted_suspect = True
        is_suspended = True
        warnings.append(
            f"实时报价不可用且行情已滞后 {int(data_lag_days)} 个交易日"
            f"（阈值 {DELIST_LAG_DAYS}），疑似退市/长期停牌，判定不可交易")

    # ---- 一字板 -----------------------------------------------------------
    one_word = None
    if quote_ok and not is_suspended and limit_up and limit_down:
        if (high is not None and low is not None and high > 0 and low > 0
                and abs(high - low) < 0.005):
            if price is not None and abs(price - limit_up) < 0.005:
                one_word = "up"
            elif price is not None and abs(price - limit_down) < 0.005:
                one_word = "down"

    status_source = "+".join(dict.fromkeys(status_source_parts)) or "unavailable"
    return TradingStatus(
        is_st=is_st,
        is_st_source=is_st_source,
        is_suspended=is_suspended,
        limit_up=limit_up,
        limit_down=limit_down,
        quote_stale=quote_stale,
        status_source=status_source,
        price_limit_pct=price_limit_pct,
        one_word_limit=one_word,
        delisted_suspect=delisted_suspect,
        warnings=warnings,
    )
