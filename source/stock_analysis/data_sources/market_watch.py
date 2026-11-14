# -*- coding: utf-8 -*-
"""T6（issue #1 / R11 缺口表 #6）：交易所重点监控池 —— **一次请求服务全池**。

上游口径（固定 commit ``f21534946c42be3ad048ac938eecb1bad84cb8ab``，v3.10.4 §8.4）
------------------------------------------------------------------------------
``em_stock_monitor()`` 是东财 App「重点监控」名单：被交易所风险警示 / 重点监控
的标的及其**生效时间窗**。零鉴权静态 JSON，**全量返回、不分页**，因此「一次
请求」就能覆盖整个自选池，不需要逐票请求。

端点与字段（2026-11-14 本机实测：HTTP 200、0.20s、19 条）::

    GET https://mobappconfig.securities.eastmoney.com/emcfg/stock_monitor.json
    Referer: https://vipmoney.eastmoney.com/

    [{"MARKET":"B","STKNAME":"*ST康乐","VALIDATESTARTDATE":"2026-11-16",
      "VALIDATEENDDATE":"2026-11-20","STKCODE":"920575","LINK_URL":"..."}]

字段表（逐字来自上游，不做改名）：

===============  ====================================================
STKCODE / STKNAME  代码 / 名称
MARKET             ``"1"``=沪市，``"0"``=深市，**``"B"``=北交所**
VALIDATESTARTDATE  监控生效起始日（``YYYY-MM-DD``）
VALIDATEENDDATE    监控生效截止日（``YYYY-MM-DD``）
LINK_URL           东财 App 详情页（可为空）
===============  ====================================================

**三值市场坑**（上游明确标注，实测 2026-11-14 的 19 条里就有 3 条 ``MARKET="B"``）：
写成 ``"SH" if MARKET == "1" else "SZ"`` 会把北交所整片错标成深市。本模块按
:data:`_MARKET_MAP` 三值映射；**未知取值不猜市场**，原样带出 ``?<原值>``。

``reason`` 是**本模块派生**的字段，不是上游字段
------------------------------------------------
上游只给名单，不给「为什么在名单里」。唯一可用的信号是名称里的风险警示前缀
（``ST`` / ``*ST`` / ``S*ST``），因此 ``reason`` 取封闭两值：

* ``risk_warning``：名称带 ST 前缀 → 风险警示；
* ``monitor``：其余 → 重点监控。

两者都只影响**文案归类**，不影响「是否命中」这一事实（命中只看代码）。

缓存与降级（``source/stock_analysis/data/state/market_watch.json``，已 gitignore）
--------------------------------------------------------------------------------
* 盘前主路径最多 **1 次** 请求（``DEFAULT_TIMEOUT`` = 3s）；同一北京自然日内
  第二次及以后直接读缓存，**0 次** 请求。
* 请求/解析失败 → 回退上一份缓存并标 ``stale=True``；**没有缓存**才
  ``available=False``。
* 空结果与接口坏了必须分开：接口返回 ``[]`` 是**合法**的「全市场 0 条」；
  非 2xx / 非 JSON / 非列表 / 行字段不合法一律抛 :class:`MarketWatchError`，
  **绝不返回空 dict 冒充「无监控」**。

本模块只服务「展示 / 风险提示」，不参与 ``action`` / ``confidence`` / 仓位 /
触发器计算。CLI 侧见 :func:`market_watch_evidence`。
"""
from __future__ import annotations

import datetime as _dt
import json
import os
import re
import sys
from dataclasses import dataclass, field
from typing import Dict, List, Optional

__all__ = [
    "DEFAULT_TIMEOUT",
    "HIT_KEYS",
    "MARKET_WATCH_KEYS",
    "MONITOR_URL",
    "OFFLINE_ENV",
    "PoolSnapshot",
    "MarketWatchError",
    "cache_path",
    "cn_today",
    "fetch_pool",
    "load_pool",
    "market_watch_evidence",
    "parse_pool",
    "read_cache",
    "reset_cache",
    "with_market_watch_block",
    "write_cache",
    "empty_market_watch_block",
]

#: 上游 §8.4 的端点（固定 commit，不跟随 main）。
MONITOR_URL = "https://mobappconfig.securities.eastmoney.com/emcfg/stock_monitor.json"
MONITOR_REFERER = "https://vipmoney.eastmoney.com/"
UPSTREAM = "a-stock-data v3.10.4 §8.4 em_stock_monitor"
SOURCE = "eastmoney_stock_monitor"

#: 盘前主路径的超时上限（秒）。上游示例用 20s；这里按 T6 契约压到 3s。
DEFAULT_TIMEOUT = 3.0

#: 缓存 schema 版本（文件内 ``schema`` 字段）。
CACHE_SCHEMA = 1

#: A 股的「今天」按北京时间算：本机在别的时区时 ``date.today()`` 会错开一天，
#: 监控窗口首日/末日会因此提前纳入或提前剔除（上游 §8.4 明确标注）。
CN_TZ = _dt.timezone(_dt.timedelta(hours=8))

#: 显式离线开关（测试与断网环境用）；与交易状态层同一约定。
OFFLINE_ENV = "ASTOCK_MARKET_WATCH_OFFLINE"

#: 状态目录覆盖（测试用；不设时落到 ``source/stock_analysis/data/state/``）。
STATE_DIR_ENV = "MARKET_WATCH_STATE_DIR"

#: 上游三值市场映射：``"B"`` 是北交所，**不是**深市。
_MARKET_MAP = {"1": "SH", "0": "SZ", "B": "BJ"}

#: ``reason`` 的封闭取值（本模块派生，见模块 docstring）。
REASON_RISK_WARNING = "risk_warning"
REASON_MONITOR = "monitor"

#: ``evidence.market_watch`` 的键集合。成功 / 不可用 / 错误路径必须完全一致，
#: 保证 G35 的「错误路径与成功路径键同构」在新增子块上继续成立。
MARKET_WATCH_KEYS = ("available", "stale", "as_of", "hit")
#: 命中对象的键集合（``hit`` 为 null 时不出现）。
HIT_KEYS = ("listed", "start", "end", "reason")

#: 与 ``data_loader/status.py::_looks_st`` 同一口径（ST / *ST / S*ST / SST）。
#: 这里写正则而不 import status，是为了让本模块不依赖交易状态层。
_ST_NAME_RE = re.compile(r"^\s*S?\*?ST", re.IGNORECASE)
_CODE_RE = re.compile(r"^\d{6}$")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

REASON_TEXT = {
    "offline": "离线模式（ASTOCK_MARKET_WATCH_OFFLINE），未请求接口",
    "fetch_failed": "监控池接口请求失败",
    "parse_failed": "监控池接口返回格式不可解析",
    "cache_invalid": "本地监控池缓存格式不可用",
}

#: 进程内 memo：同一北京自然日 + 同一状态目录只取一次（多票共用）。
_POOL_MEMO: Dict[tuple, "PoolSnapshot"] = {}


class MarketWatchError(RuntimeError):
    """监控池取数 / 解析失败。

    取数失败**必须**抛异常而不是返回空 dict —— 否则调用方会把「接口坏了」
    读成「今天没有标的被监控」，这正是 T6 要避免的静默失败。
    """


# ---------------------------------------------------------------------------
# 时间与路径
# ---------------------------------------------------------------------------
def cn_today(now: Optional[_dt.datetime] = None) -> str:
    """北京时间的今天（``YYYY-MM-DD``）。

    ``now`` 为 ``None`` 时取当前时刻；带时区的时间会先换算到东八区；
    不带时区的时间视为已经是北京时间（测试用）。
    """
    moment = now or _dt.datetime.now(CN_TZ)
    if moment.tzinfo is not None:
        moment = moment.astimezone(CN_TZ)
    return moment.date().isoformat()


def _state_dir(state_dir: Optional[str] = None) -> str:
    if state_dir:
        return state_dir
    override = os.getenv(STATE_DIR_ENV)
    if override:
        return override
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(root, "data", "state")


def cache_path(state_dir: Optional[str] = None) -> str:
    """缓存文件路径：``<state_dir>/market_watch.json``（``data/`` 已 gitignore）。"""
    return os.path.join(_state_dir(state_dir), "market_watch.json")


# ---------------------------------------------------------------------------
# 取数（唯一会触网的函数）
# ---------------------------------------------------------------------------
def _http_get(url: str, headers: dict, timeout: float):
    """实际 HTTP 入口。单独一层是为了让测试只 mock 这一处。"""
    from stock_analysis.data_sources._astock_generated import em_get

    return em_get(url, headers=headers, timeout=timeout)


def fetch_pool(timeout: float = DEFAULT_TIMEOUT) -> List[dict]:
    """拉取全市场监控池的**原始条目**（一次请求，不分页）。

    * 非 2xx → :class:`MarketWatchError`
    * 响应不是 JSON / 不是列表 → :class:`MarketWatchError`
    * 返回 ``[]`` 是**合法**结果（全市场 0 条），由调用方与「接口坏了」区分。
    """
    try:
        response = _http_get(MONITOR_URL,
                             {"Referer": MONITOR_REFERER}, timeout)
    except Exception as exc:  # 网络层异常统一收敛成明确异常
        raise MarketWatchError(
            f"监控池请求失败（{type(exc).__name__}: {exc}）") from exc

    status = getattr(response, "status_code", None)
    if status is not None and int(status) != 200:
        raise MarketWatchError(f"监控池接口 HTTP {status}")

    try:
        payload = response.json()
    except Exception as exc:
        raise MarketWatchError(
            f"监控池接口返回非 JSON（{type(exc).__name__}: {exc}）") from exc

    if payload is None:
        # 上游写法 `r.json() or []`；显式 null 视为空列表（合法空结果）。
        return []
    if not isinstance(payload, list):
        raise MarketWatchError(
            f"监控池接口返回的不是列表（{type(payload).__name__}）")
    return payload


# ---------------------------------------------------------------------------
# 解析
# ---------------------------------------------------------------------------
def _entry(row, index: int) -> tuple:
    """单行 → ``(code, entry)``；任何字段不合法都抛错（不静默跳过）。"""
    if not isinstance(row, dict):
        raise MarketWatchError(f"监控池第 {index} 行不是对象：{type(row).__name__}")

    code = str(row.get("STKCODE") or "").strip()
    if not _CODE_RE.match(code):
        raise MarketWatchError(f"监控池第 {index} 行 STKCODE 不是 6 位数字：{code!r}")

    start = str(row.get("VALIDATESTARTDATE") or "").strip()
    end = str(row.get("VALIDATEENDDATE") or "").strip()
    for label, value in (("VALIDATESTARTDATE", start), ("VALIDATEENDDATE", end)):
        if not _DATE_RE.match(value):
            raise MarketWatchError(
                f"监控池 {code} 的 {label} 不是 YYYY-MM-DD：{value!r}")

    raw_market = str(row.get("MARKET", "")).strip().upper()
    # 未知取值不猜市场，原样带出（上游同口径），避免静默标错。
    market = _MARKET_MAP.get(raw_market, f"?{raw_market}")

    name = str(row.get("STKNAME") or "").strip()
    reason = REASON_RISK_WARNING if _ST_NAME_RE.match(name) else REASON_MONITOR

    return code, {
        "code": code,
        "name": name,
        "market": market,
        "start": start,
        "end": end,
        "reason": reason,
        "link": str(row.get("LINK_URL") or "").strip(),
    }


def parse_pool(rows) -> Dict[str, dict]:
    """原始条目 → ``{code: {code, name, market, start, end, reason, link}}``。

    同码重复时**后者覆盖**（上游不做去重；这里保持确定性即可）。
    """
    if rows is None:
        return {}
    if not isinstance(rows, list):
        raise MarketWatchError(
            f"监控池数据不是列表（{type(rows).__name__}），无法解析")

    pool: Dict[str, dict] = {}
    for index, row in enumerate(rows):
        code, entry = _entry(row, index)
        pool[code] = entry
    return pool


# ---------------------------------------------------------------------------
# 缓存
# ---------------------------------------------------------------------------
def read_cache(state_dir: Optional[str] = None) -> Optional[dict]:
    """读缓存；不存在 / 不可解析 / schema 不符一律 ``None``（由调用方降级）。"""
    path = cache_path(state_dir)
    try:
        with open(path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except Exception:
        return None
    if not isinstance(payload, dict):
        return None
    if int(payload.get("schema") or 0) != CACHE_SCHEMA:
        return None
    if not isinstance(payload.get("entries"), list):
        return None
    return payload


def write_cache(entries: List[dict], fetched_at: str,
                state_dir: Optional[str] = None,
                session_date: Optional[str] = None) -> Optional[str]:
    """原子写缓存（tmp + replace）。写失败只告警，不影响主流程。"""
    path = cache_path(state_dir)
    payload = {
        "schema": CACHE_SCHEMA,
        "source": SOURCE,
        "upstream": UPSTREAM,
        "url": MONITOR_URL,
        "fetched_at": fetched_at,
        "session_date": session_date or cn_today(_parse_iso(fetched_at)),
        "count": len(entries),
        "entries": list(entries),
    }
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=1)
        os.replace(tmp, path)
        return path
    except Exception as exc:
        print(f"[WARN] 写监控池缓存失败：{exc}", file=sys.stderr)
        return None


def _parse_iso(text: Optional[str]) -> Optional[_dt.datetime]:
    if not text:
        return None
    try:
        return _dt.datetime.fromisoformat(str(text))
    except Exception:
        return None


def _snapshot_from_cache(cached: dict, stale: bool, error: Optional[str]) -> "PoolSnapshot":
    entries = {}
    for item in cached.get("entries") or []:
        if isinstance(item, dict) and _CODE_RE.match(str(item.get("code") or "")):
            entries[str(item["code"])] = dict(item)
    return PoolSnapshot(
        available=bool(entries) or cached.get("count") == 0,
        stale=stale,
        as_of=cached.get("session_date"),
        entries=entries,
        fetched_at=cached.get("fetched_at"),
        source=str(cached.get("source") or SOURCE),
        error=error,
    )


# ---------------------------------------------------------------------------
# 快照 / 装载
# ---------------------------------------------------------------------------
@dataclass
class PoolSnapshot:
    """一次装载的结果（可用 / 过期 / 条目）。"""

    available: bool
    stale: bool = False
    as_of: Optional[str] = None
    entries: Dict[str, dict] = field(default_factory=dict)
    fetched_at: Optional[str] = None
    source: str = SOURCE
    error: Optional[str] = None

    def hit(self, code: Optional[str], on_date: Optional[str] = None) -> Optional[dict]:
        """``code`` 是否在监控池内；给了 ``on_date`` 还要求落在生效窗口内。

        窗口判定用 ``start <= on_date <= end``（与上游 ``only_active`` 同一口径）。
        ``on_date`` 为 ``None`` 时不做窗口过滤 —— 接口本身已只返回「当前生效」
        的名单，再过滤一次会让窗口首尾日的边界行为不可预测。
        """
        if not self.available or not code:
            return None
        entry = self.entries.get(str(code))
        if entry is None or on_date is None:
            return entry
        start, end = entry.get("start"), entry.get("end")
        if start and on_date < str(start):
            return None
        if end and on_date > str(end):
            return None
        return entry


def _offline(allow_network: Optional[bool] = None) -> bool:
    if allow_network is not None:
        return not allow_network
    return str(os.getenv(OFFLINE_ENV, "")).strip().lower() in ("1", "true", "yes", "on")


def load_pool(now: Optional[_dt.datetime] = None,
              state_dir: Optional[str] = None,
              timeout: float = DEFAULT_TIMEOUT,
              allow_network: Optional[bool] = None,
              force: bool = False) -> PoolSnapshot:
    """装载监控池：当日缓存优先 → 否则一次请求 → 失败回退旧缓存。

    请求次数上界：同一北京自然日 + 同一状态目录，**最多 1 次**。
    """
    today = cn_today(now)
    offline = _offline(allow_network)
    key = (today, cache_path(state_dir), offline)
    if not force and key in _POOL_MEMO:
        return _POOL_MEMO[key]

    cached = read_cache(state_dir)
    fresh = bool(cached and cached.get("session_date") == today)

    if fresh and not force:
        snapshot = _snapshot_from_cache(cached, stale=False, error=None)
    elif offline:
        # 显式离线：能用旧缓存就用（标 stale），没有则明确不可用。
        if cached:
            snapshot = _snapshot_from_cache(
                cached, stale=True,
                error=f"{REASON_TEXT['offline']}；回退 {cached.get('session_date')} 的缓存")
        else:
            snapshot = PoolSnapshot(available=False, stale=False, as_of=None,
                                    error=REASON_TEXT["offline"])
    else:
        try:
            rows = fetch_pool(timeout=timeout)
            entries = parse_pool(rows)
            fetched_at = (now or _dt.datetime.now(CN_TZ)).isoformat()
            write_cache(list(entries.values()), fetched_at, state_dir=state_dir)
            snapshot = PoolSnapshot(
                available=True, stale=False, as_of=today,
                entries=entries, fetched_at=fetched_at, source=SOURCE)
        except MarketWatchError as exc:
            if cached:
                snapshot = _snapshot_from_cache(
                    cached, stale=True,
                    error=f"{exc}；回退 {cached.get('session_date')} 的缓存（可能过时）")
            else:
                snapshot = PoolSnapshot(available=False, stale=False, as_of=None,
                                        error=str(exc))
        except Exception as exc:  # 兜底：绝不把异常读成「无监控」
            reason = f"{type(exc).__name__}: {exc}"
            if cached:
                snapshot = _snapshot_from_cache(
                    cached, stale=True,
                    error=f"{reason}；回退 {cached.get('session_date')} 的缓存（可能过时）")
            else:
                snapshot = PoolSnapshot(available=False, stale=False, as_of=None,
                                        error=reason)

    _POOL_MEMO[key] = snapshot
    return snapshot


def reset_cache() -> None:
    """清进程内 memo（测试与长驻进程显式刷新用）。"""
    _POOL_MEMO.clear()


# ---------------------------------------------------------------------------
# CLI 接线
# ---------------------------------------------------------------------------
def _normalize_code(code) -> str:
    try:
        from stock_analysis.data_sources._astock_generated import norm_ticker
        return norm_ticker(str(code))
    except Exception:
        return str(code).strip()


def _hit_block(entry: Optional[dict]) -> Optional[dict]:
    """命中对象：只暴露契约里的四个键，不把名称/链接带进 CLI JSON。"""
    if not entry:
        return None
    return {
        "listed": True,
        "start": entry.get("start"),
        "end": entry.get("end"),
        "reason": entry.get("reason"),
    }


def empty_market_watch_block(available: bool = False, stale: bool = False,
                             as_of: Optional[str] = None) -> dict:
    """键齐全的不可用块（错误路径与成功路径同构）。"""
    return {"available": bool(available), "stale": bool(stale),
            "as_of": as_of, "hit": None}


def with_market_watch_block(builder):
    """给错误响应构造器套一层：补上键同构的 ``evidence.market_watch``（G35）。

    取数阶段失败时监控池**不读缓存、不触网**，显式 ``available=False``。
    用装饰器而不是改 ``decision/schema.py``，是为了让新增子块与既有契约解耦。
    """
    def wrapped(*args, **kwargs):
        document = builder(*args, **kwargs)
        evidence = document.get("evidence")
        if isinstance(evidence, dict):
            evidence["market_watch"] = empty_market_watch_block()
        return document

    wrapped.__name__ = getattr(builder, "__name__", "wrapped")
    wrapped.__doc__ = getattr(builder, "__doc__", None)
    return wrapped


def market_watch_evidence(code, data_type: Optional[str] = None,
                          now: Optional[_dt.datetime] = None,
                          state_dir: Optional[str] = None,
                          timeout: float = DEFAULT_TIMEOUT,
                          allow_network: Optional[bool] = None,
                          pool: Optional[PoolSnapshot] = None,
                          as_of: Optional[str] = None):
    """CLI 接线：返回 ``(evidence.market_watch, warnings)``。

    * 命中 → ``hit={listed,start,end,reason}``；未命中 / 不可用 → ``hit=null``。
    * ``available=False``（无缓存且取数失败）或 ``stale=True``（回退旧缓存）
      各给一条可操作 warning，说明原因；两者都**不改**任何决策字段。
    * ``as_of``（``--as-of`` 历史回放）时**不给命中**：监控池只有当前快照，
      没有历史版本，把今天的名单套到过去某天等于引入前视。此时只报
      ``available/stale/as_of``，并说明「回放不提供历史监控状态」。
    """
    shown = _normalize_code(code)
    snapshot = pool if pool is not None else load_pool(
        now=now, state_dir=state_dir, timeout=timeout, allow_network=allow_network)

    block = empty_market_watch_block(available=snapshot.available,
                                     stale=snapshot.stale,
                                     as_of=snapshot.as_of)

    notes: List[str] = []
    replay = bool(str(as_of or "").strip())
    entry = snapshot.hit(shown, on_date=cn_today(now))
    if not replay:
        block["hit"] = _hit_block(entry)
    elif entry is not None:
        # 监控池只有当前快照，没有历史版本：把今天的名单套到过去某天等于
        # 引入前视。回放时**不报命中**，但要显式说明「确实命中了、只是被抑制」，
        # 不能让调用方误以为「那天没被监控」。
        notes.append(
            f"监控池命中（{shown}，{entry.get('start')}~{entry.get('end')}）"
            f"但为当前快照，--as-of {as_of} 回放不提供历史监控状态；"
            "只展示，不影响 action")

    if not snapshot.available:
        reason = snapshot.error or REASON_TEXT["fetch_failed"]
        notes.append(f"监控池不可用（{shown}）：{reason}；只展示，不影响 action")
    elif snapshot.stale:
        reason = snapshot.error or "接口未返回当日数据"
        notes.append(
            f"监控池缓存过期（{shown}）：as_of={snapshot.as_of or '未知'}，"
            f"{reason}；只展示，不影响 action")
    return block, notes
