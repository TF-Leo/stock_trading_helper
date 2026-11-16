# -*- coding: utf-8 -*-
"""东财 push2 家族反爬：识别、跨进程熔断与请求预算（issue #108）。

背景（2026-11-15 A5500 只读排查）
---------------------------------
东财 ``push2`` / ``push2delay`` / ``push2his`` 对 ``/api/qt/*`` 返回**零字节
FIN**（nginx 444 类 L7 反爬），同期 ``datacenter-web`` 正常 200，证书 /
TLS / DNS / 路由均正常 → 这是**对本机出口 IP 的反爬封禁**，不是网络故障。
触发阈值很低：3 分钟内约 10 次请求即封；封禁窗口内重试只会继续撞墙。

因此东财取数必须满足三条：

1. **识别**：HTTP 444 或「连接被对端关闭 + 读到零字节」归类为
   :class:`AntiScrapeBlocked`。识别放在 ``em_get`` 这一层（本模块的
   :class:`EmFetchGuard` 包装 ``_astock_generated.em_get``），资金流与
   ST 名单共用同一套判定，不再各写一份。
2. **熔断**：命中即写**持久冷却文件**（默认
   ``source/stock_analysis/data/state/eastmoney_cooldown.json``，跨进程
   ``fcntl.flock`` 保护读-改-写），记录到期时间与原因。冷却期内**一个请求
   都不发**，直接抛 :class:`EastmoneyCooling`；到期后自动放行一次探测。
3. **预算**：同一次预计算 run 内，同一域名的请求数有硬上限（默认 60），
   超限抛 :class:`EastmoneyBudgetExceeded`，调用方必须提前退出并告警，
   不得静默继续。

域级冷却（2026-11-16，issue #1 事件预计算接入）
----------------------------------------------
东财内部并非一个风控面：``push2`` / ``push2his`` 与 ``datacenter-web`` 是两套
网关，历史实测 ``push2`` 被 444 时 ``datacenter-web`` 仍正常 200。若共用同一个
"整域封锁"键，事件预计算（datacenter）一次 444 就会让资金流 / ST 名单
（push2）整轮停摆 —— 那是**误伤**。

因此冷却文件支持**域级条目**（additive，schema 仍为 1）::

    {
      "schema": 1, "schema_version": 1,
      "surfaces": {
        "datacenter-web.eastmoney.com": {
          "schema": 1, "reason": "http_444", "blocked_until": "...",
          "seconds": 1800.0, ...
        }
      }
    }

* **写**：带 ``surface`` 的闸门只写 ``surfaces[<域名>]``，顶层共享字段原样保留
  —— 一个域被封不会让另一个域的任务整轮停摆（唯一例外是下面说的
  「≥2 个不同域都命中 444」的显式全域封锁）；
* **读**：``scoped_cooldown_state()`` 先看顶层共享封锁（任何域被封都视为
  "此刻别碰东财"，保守），再看本域条目；顶层封锁只会让本轮 **0 请求**并保留
  旧缓存，不会延长或升级封锁；
* 不带 ``surface`` 的既有调用仍只读写顶层字段（兼容旧调用 / 测试）。

顶层共享封锁 = **显式全域封锁**（issue #130）
---------------------------------------------
顶层块不再是「某个域 444」的副作用，而是「疑似 IP 级封禁」的判定结果：
``mark_cooldown(surface=…)`` 默认**自动升级** —— 若冷却文件里已有**另一个仍在
生效的域条目**（同一冷却窗口内 ≥2 个不同域命中 444），说明不是单个网关的
策略，而是整条出口 IP 被封，此时同时写顶层共享块（记录
``surfaces_blocked``）。只有单个域命中时**只写本域**，其它域的任务照常运行。

升级判定可用参数覆盖：``escalate=True`` 强制写顶层（人工判定 / 测试），
``escalate=False`` 永不写顶层。因此：

* ``push2his`` 在 18:30 被 444 → 只写 ``surfaces["push2.eastmoney.com"]``
  （push2 家族共用一个键），18:45 的 datacenter 任务读取并集时本域无条目、
  顶层无封锁 → **照常运行**；
* 同一窗口内 push2his 与 datacenter 都被 444 → 第二个域写本域条目时自动写
  顶层共享块 → 之后所有域 0 请求（这才是「整条出口别碰」）。

与 ``tencent_throttle`` 的关系
------------------------------
腾讯的跨进程冷却是**另一条上游**（429/空响应限流 + 501 WAF），状态文件与
语义不同，本模块不合并它。issue #107 为 ST 名单写的冷却已经并入本模块：
``status.StListFetchGuard`` / ``status.cooldown_path`` 等公开 API 保留为薄
委托，文件格式统一为下面这一份。

冷却文件格式（schema 1，与 #107 的 ST 冷却兼容）::

    {
      "schema": 1,
      "schema_version": 1,
      "reason": "http_444" | "zero_byte_fin",
      "detail": "...",
      "blocked_at": "2026-11-15T18:35:00",
      "blocked_until": "2026-11-15T19:05:00",
      "seconds": 1800.0,
      "updated_at": "2026-11-15T18:35:00",
      "pid": 12345
    }

环境变量
--------
* ``EASTMONEY_COOLDOWN_PATH``：冷却文件路径（测试 / 运维覆盖）；
* ``EASTMONEY_COOLDOWN_SECONDS``：冷却秒数（默认 1800，上限 86400）；
* ``EASTMONEY_REQUEST_BUDGET``：单域名单轮请求硬上限（默认 60）。

人工解除封锁后重跑预计算：``python tools/precompute_st_list.py
--clear-cooldown``，或删除冷却文件即可。
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
from datetime import datetime
from urllib.parse import urlsplit

__all__ = [
    "STATE_PATH_ENV",
    "COOLDOWN_SECONDS_ENV",
    "REQUEST_BUDGET_ENV",
    "DEFAULT_COOLDOWN_SECONDS",
    "DEFAULT_REQUEST_BUDGET",
    "BLOCKED_STATUS",
    "SCHEMA_VERSION",
    "SURFACES_KEY",
    "PUSH2_SURFACE",
    "DATACENTER_SURFACE",
    "MONITOR_SURFACE",
    "surface_of",
    "AntiScrapeBlocked",
    "EastmoneyCooling",
    "EastmoneyBudgetExceeded",
    "cooldown_seconds",
    "request_budget",
    "state_path",
    "cooldown_state",
    "scoped_cooldown_state",
    "mark_cooldown",
    "clear_cooldown",
    "classify_response",
    "classify_exception",
    "RequestBudget",
    "EmFetchGuard",
]

#: 测试 / 运维用：覆盖冷却状态文件路径。
STATE_PATH_ENV = "EASTMONEY_COOLDOWN_PATH"
#: 冷却秒数覆盖（默认 1800s）。
COOLDOWN_SECONDS_ENV = "EASTMONEY_COOLDOWN_SECONDS"
#: 单域名单轮请求硬上限（默认 60）。
REQUEST_BUDGET_ENV = "EASTMONEY_REQUEST_BUDGET"

DEFAULT_COOLDOWN_SECONDS = 1800.0
DEFAULT_REQUEST_BUDGET = 60
#: nginx 反爬状态码；零字节 FIN 在 requests 里通常表现为连接被对端关闭。
BLOCKED_STATUS = 444
SCHEMA_VERSION = 1
#: 域级冷却条目的键（``surfaces[<hostname>]``）；顶层字段是共享封锁。
SURFACES_KEY = "surfaces"

#: 域级冷却的**规范键**（issue #130）。``push2`` / ``push2delay`` / ``push2his``
#: 是同一套 L7 反爬规则（同一个风控面，只是主机名不同），因此三者共用一个
#: surface 键；否则「资金流命中 push2his + ST 名单命中 push2」会被误判成
#: 「两个不同域都 444」而错误升级成全域封锁。
PUSH2_SURFACE = "push2.eastmoney.com"
DATACENTER_SURFACE = "datacenter-web.eastmoney.com"
MONITOR_SURFACE = "mobappconfig.securities.eastmoney.com"

#: 零字节 FIN / 连接被反爬层直接切断的常见文案（requests / ssl / 原生 socket）。
_BLOCK_MARKERS = (
    "remote end closed",
    "remotedisconnected",
    "connection aborted",
    "connection reset",
    "reset by peer",
    "empty reply",
    "incompleteread",
    "ssleoferror",
    "unexpected eof",
    "eof occurred",
)

_BLOCK_EXC_TYPES = (
    "ConnectionError",
    "ConnectionResetError",
    "ChunkedEncodingError",
    "SSLEOFError",
    "RemoteDisconnected",
)


class AntiScrapeBlocked(Exception):
    """东财反爬命中（HTTP 444 / 零字节 FIN）：本轮**不换域重试**，已写冷却。

    刻意**不继承** ``RuntimeError`` / ``requests.RequestException``：
    生成物的 ``_em_clist_all`` 用 ``except requests.RequestException`` 换域重试，
    ``stock_fund_flow_120d`` 用 ``except Exception`` 吞掉失败；一次封禁必须
    成为调用方可见的阻断信号，而不是被重试放大成更多请求。
    """


class EastmoneyCooling(AntiScrapeBlocked):
    """冷却期内**一个请求都没发**（跨进程冷却文件仍未到期）。

    与 :class:`AntiScrapeBlocked` 的区别对调用方很重要：这不是「这次请求
    被拦了」，而是「现在整个东财都别碰」。预计算据此直接跳过，退出码 3。
    """


class EastmoneyBudgetExceeded(RuntimeError):
    """单域名单轮请求数超过硬上限：本轮必须提前退出并告警，不得静默继续。"""


# ---------------------------------------------------------------------------
# 时间 / 路径 / 参数
# ---------------------------------------------------------------------------
def _now_ts(now=None) -> float:
    """``now`` 可为 ``datetime`` 或 epoch 秒；缺省取当前时间。"""
    if now is None:
        return time.time()
    if isinstance(now, datetime):
        return now.timestamp()
    return float(now)


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(float(ts)).isoformat(timespec="seconds")


def cooldown_seconds() -> float:
    """冷却秒数：``EASTMONEY_COOLDOWN_SECONDS`` 优先，非法值回默认。

    下限 1s（0 等于没有熔断），上限 86400s（一天）防止误配成不受控封锁。
    """
    raw = str(os.getenv(COOLDOWN_SECONDS_ENV, "") or "").strip()
    try:
        value = float(raw) if raw else DEFAULT_COOLDOWN_SECONDS
    except ValueError:
        value = DEFAULT_COOLDOWN_SECONDS
    return min(max(value, 1.0), 86400.0)


def request_budget(default=None) -> int:
    """单域名单轮请求硬上限：``EASTMONEY_REQUEST_BUDGET`` 优先。

    上限 10000 只是防止误配成天文数字；正常预计算远低于它。
    """
    if default is not None:
        return max(1, min(int(default), 10000))
    raw = str(os.getenv(REQUEST_BUDGET_ENV, "") or "").strip()
    try:
        value = int(raw) if raw else DEFAULT_REQUEST_BUDGET
    except ValueError:
        value = DEFAULT_REQUEST_BUDGET
    return max(1, min(value, 10000))


def state_path(state_dir=None) -> str:
    """冷却状态文件路径。

    ``EASTMONEY_COOLDOWN_PATH`` 优先；否则 ``<state_dir>/eastmoney_cooldown.json``；
    ``state_dir`` 缺省为 ``source/stock_analysis/data/state``。ST 名单把
    ``status.cache_path()`` 所在目录传进来，于是两边指向**同一个文件**。
    """
    override = str(os.getenv(STATE_PATH_ENV, "") or "").strip()
    if override:
        return override
    if state_dir:
        return os.path.join(str(state_dir), "eastmoney_cooldown.json")
    base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, "data", "state", "eastmoney_cooldown.json")


def _lock_path(path: str) -> str:
    return path + ".lock"


class _FileLock:
    """跨进程互斥。拿不到 ``fcntl`` 时退化为无锁（仍是原子替换写入）。"""

    def __init__(self, path: str):
        self._path = path
        self._handle = None

    def __enter__(self):
        try:
            import fcntl
        except Exception:  # pragma: no cover - 非 POSIX 平台
            return self
        try:
            os.makedirs(os.path.dirname(self._path) or ".", exist_ok=True)
            self._handle = open(self._path, "a+")
            fcntl.flock(self._handle.fileno(), fcntl.LOCK_EX)
        except Exception:  # pragma: no cover - 锁文件不可写时不阻断取数
            self._handle = None
        return self

    def __exit__(self, *exc_info):
        if self._handle is not None:
            try:
                import fcntl
                fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
            except Exception:
                pass
            try:
                self._handle.close()
            except Exception:
                pass
            self._handle = None
        return False


# ---------------------------------------------------------------------------
# 冷却状态
# ---------------------------------------------------------------------------
def _read_payload(path=None):
    """读冷却文件原文；文件不存在 / 损坏 / schema 不符 → ``None``。

    冷却文件是**可选加速器**：坏了不能把取数路径卡死，因此所有解析失败都
    退化成"没有冷却"。
    """
    try:
        with open(path or state_path(), "r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    try:
        schema = int(payload.get("schema", payload.get("schema_version", 0)) or 0)
    except (TypeError, ValueError):
        return None
    if schema != SCHEMA_VERSION:
        return None
    return payload


def _active(entry, now_ts: float):
    """单条冷却记录 → 仍在生效则补 ``remaining_seconds`` 返回，否则 None。"""
    if not isinstance(entry, dict):
        return None
    try:
        until_ts = datetime.fromisoformat(str(entry.get("blocked_until"))).timestamp()
    except (TypeError, ValueError):
        return None
    remaining = until_ts - now_ts
    if remaining <= 0:
        return None
    result = dict(entry)
    result["remaining_seconds"] = remaining
    return result


def _surface_entry(payload, surface):
    surfaces = payload.get(SURFACES_KEY)
    if not isinstance(surfaces, dict):
        return None
    return surfaces.get(str(surface))


def cooldown_state(now=None, path=None) -> dict | None:
    """读取**共享**冷却状态；仍在生效才返回（附带 ``remaining_seconds``），否则 None。

    文件不存在 / 损坏 / 已过期一律返回 ``None`` —— 冷却文件是**可选加速器**，
    不能因为它坏了把整条取数路径卡死。
    """
    payload = _read_payload(path)
    if payload is None:
        return None
    return _active(payload, _now_ts(now))


def scoped_cooldown_state(now=None, path=None, surface=None) -> dict | None:
    """读取**本域**冷却状态：顶层共享封锁 ∪ ``surfaces[surface]``。

    为什么取并集而不是只看本域
    --------------------------
    顶层共享封锁（既有资金流 / ST 名单写入，不带域）代表"此刻整条东财出口
    都不该再碰"。域级条目只证明"这个域被封"。两者任一生效都必须 **0 请求**，
    否则 444 冷却期间仍会继续撞墙。

    返回的记录带 ``scope``：``"shared"``（顶层）或 ``"surface"``（本域），
    以及 ``surface``（本域条目时为域名）。两者同时生效取到期更晚的那条 ——
    它才决定本轮要等多久。
    """
    payload = _read_payload(path)
    if payload is None:
        return None
    now_ts = _now_ts(now)
    surface = surface_of(surface) if surface else None
    candidates = []
    shared = _active(payload, now_ts)
    if shared is not None:
        shared = dict(shared, scope="shared", surface=None)
        candidates.append(shared)
    if surface:
        scoped = _active(_surface_entry(payload, surface), now_ts)
        if scoped is not None:
            scoped = dict(scoped, scope="surface", surface=str(surface))
            candidates.append(scoped)
    if not candidates:
        return None
    return max(candidates, key=lambda item: item["remaining_seconds"])


def mark_cooldown(reason: str, detail=None, seconds=None, now=None,
                  path=None, surface=None, escalate=None) -> dict:
    """写入跨进程冷却（原子替换），返回本次写下的那条冷却记录。

    「读-改-写」在文件锁内完成：并发进程各自只追加自己知道的封锁，不会用
    旧状态覆盖对方刚写下的更晚到期时间（取 ``max``）。

    ``surface`` 给定时写 ``surfaces[<域名>]``（域级封锁），顶层共享字段与
    其它域条目原样保留 —— 一个域被封不会误伤另一个域。``surface`` 为空时
    写顶层共享封锁（显式全域封锁），同样保留已有域级条目。

    顶层共享封锁的升级（issue #130）
    --------------------------------
    ``escalate=None``（默认）时**自动判定**：若冷却文件里已有**另一个仍在生效
    的域条目**（同一冷却窗口内 ≥2 个不同域都命中 444），则不是单个网关的策略
    而是整条出口 IP 被封 —— 本次除写本域条目外**同时写顶层共享块**
    （``surfaces_blocked`` 列出当时生效的域名）。只有单个域命中时只写本域。
    ``escalate=True`` 强制写顶层，``escalate=False`` 永不写顶层（测试 /
    人工判定用）。
    """
    now_ts = _now_ts(now)
    wait = cooldown_seconds() if seconds is None else max(0.0, float(seconds))
    until_ts = now_ts + wait
    target = path or state_path()
    surface = surface_of(surface) if surface else None
    with _FileLock(_lock_path(target)):
        raw = _read_payload(target) or {}
        surfaces = raw.get(SURFACES_KEY)
        surfaces = dict(surfaces) if isinstance(surfaces, dict) else {}
        existing = (_active(_surface_entry(raw, surface), now_ts) if surface
                    else _active(raw, now_ts))
        if existing:
            try:
                existing_until = datetime.fromisoformat(
                    str(existing.get("blocked_until"))).timestamp()
            except (TypeError, ValueError):
                existing_until = 0.0
            until_ts = max(until_ts, existing_until)
        entry = {
            "schema": SCHEMA_VERSION,
            "schema_version": SCHEMA_VERSION,
            "reason": str(reason or "unknown"),
            "detail": str(detail or ""),
            "blocked_at": _iso(now_ts),
            "blocked_until": _iso(until_ts),
            "seconds": round(until_ts - now_ts, 3),
            "updated_at": _iso(now_ts),
            "pid": os.getpid(),
        }
        if surface:
            entry["surface"] = surface
            surfaces[surface] = entry
            others = sorted(name for name, value in surfaces.items()
                            if name != surface and _active(value, now_ts))
            if escalate is None:
                escalate_global = bool(others)
            else:
                escalate_global = bool(escalate)
            payload = {key: value for key, value in raw.items()
                       if key != SURFACES_KEY}
            payload["schema"] = SCHEMA_VERSION
            payload["schema_version"] = SCHEMA_VERSION
            payload[SURFACES_KEY] = surfaces
            if escalate_global:
                # 已有更晚到期的顶层封锁时不得缩短（与域条目同一规则）。
                shared_until = until_ts
                current_shared = _active(raw, now_ts)
                if current_shared:
                    try:
                        shared_until = max(shared_until, datetime.fromisoformat(
                            str(current_shared.get("blocked_until"))
                        ).timestamp())
                    except (TypeError, ValueError):
                        pass
                shared = dict(entry)
                shared.pop("surface", None)
                shared["blocked_until"] = _iso(shared_until)
                shared["seconds"] = round(shared_until - now_ts, 3)
                shared["detail"] = (
                    f"{entry['detail']}；同一冷却窗口内多个域命中 444"
                    f"（{', '.join([*others, surface])}），判定为出口 IP 级封锁"
                ).strip("；")
                shared["scope"] = "shared"
                shared["surfaces_blocked"] = [*others, surface]
                payload.update(shared)
        else:
            payload = dict(entry)
            if surfaces:
                payload[SURFACES_KEY] = surfaces
        os.makedirs(os.path.dirname(target) or ".", exist_ok=True)
        tmp = f"{target}.{os.getpid()}.tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=1,
                      sort_keys=True)
            handle.write("\n")
        os.replace(tmp, target)
    result = dict(entry)
    if surface:
        result["escalated_shared"] = bool(escalate_global)
    return result


def clear_cooldown(path=None, surface=None) -> None:
    """删除冷却状态（人工确认封锁解除后复跑预计算时用）。

    ``surface`` 给定时只删该域的条目（其它域 / 顶层共享封锁保留）；条目删空后
    不留下空的 ``surfaces`` 键。不给 ``surface`` 时删除整个文件（既有语义）。

    若删完后**没有任何封锁记录**（无顶层块、无其它域条目），整个文件一并
    删除 —— 否则残留一个空壳文件，运维容易误判成「还在冷却」。只删已过期的
    条目时文件保留（是否生效由 ``cooldown_state()`` 按时间判定）。
    """
    target = path or state_path()
    if not surface:
        try:
            os.unlink(target)
        except FileNotFoundError:
            pass
        return
    surface = surface_of(surface)
    with _FileLock(_lock_path(target)):
        raw = _read_payload(target)
        if not raw:
            return
        surfaces = raw.get(SURFACES_KEY)
        if not isinstance(surfaces, dict) or str(surface) not in surfaces:
            return
        surfaces = dict(surfaces)
        surfaces.pop(str(surface), None)
        payload = {key: value for key, value in raw.items()
                   if key != SURFACES_KEY}
        if surfaces:
            payload[SURFACES_KEY] = surfaces
        if not surfaces and not payload.get("blocked_until"):
            try:
                os.unlink(target)
            except FileNotFoundError:
                pass
            return
        tmp = f"{target}.{os.getpid()}.tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=1,
                      sort_keys=True)
            handle.write("\n")
        os.replace(tmp, target)


# ---------------------------------------------------------------------------
# 反爬判定
# ---------------------------------------------------------------------------
def _status_of(response):
    try:
        status = getattr(response, "status_code", None)
        return None if status is None else int(status)
    except (TypeError, ValueError):
        return None


def classify_response(response) -> str | None:
    """响应 → ``"http_444"`` / ``"zero_byte_fin"`` / ``None``。

    * HTTP 444：nginx 反爬的标准状态码；
    * 零字节 body：反爬层直接切断连接时 ``requests`` 也可能拿到 200 + 空内容，
      因此内容长度为 0 同样判定为阻断（东财正常接口不会返回空 body）。
    """
    if _status_of(response) == BLOCKED_STATUS:
        return "http_444"
    try:
        content = getattr(response, "content", None)
    except Exception:
        content = None
    if content is not None:
        try:
            size = len(content)
        except Exception:
            size = None
        if size == 0:
            return "zero_byte_fin"
    return None


def classify_exception(exc: BaseException) -> str | None:
    """异常 → ``"http_444"`` / ``"zero_byte_fin"`` / ``None``。"""
    response = getattr(exc, "response", None)
    if _status_of(response) == BLOCKED_STATUS:
        return "http_444"
    text = f"{type(exc).__name__}: {exc}"
    if re.search(r"\b444\b", text):
        return "http_444"
    lowered = text.lower()
    type_name = type(exc).__name__
    if type_name in _BLOCK_EXC_TYPES or any(
            marker in lowered for marker in _BLOCK_MARKERS):
        return "zero_byte_fin"
    return None


def _domain(url) -> str:
    """URL → 域名；已是裸域名时原样返回（预算按域名分桶）。"""
    text = str(url or "")
    parsed = urlsplit(text)
    return (parsed.hostname or text).lower()


#: push2 家族主机 → 同一个风控面（issue #130）。
_PUSH2_HOSTS = {
    "push2.eastmoney.com",
    "push2delay.eastmoney.com",
    "push2his.eastmoney.com",
}


def surface_of(host_or_url) -> str:
    """主机名 / URL → 冷却用的规范 surface 键。

    ``push2`` / ``push2delay`` / ``push2his`` 是同一套 L7 反爬规则，归并为
    :data:`PUSH2_SURFACE`；``datacenter-web`` 与监控池（``mobappconfig``）
    各自独立。未知主机原样返回（小写域名），不猜归属。
    """
    host = _domain(host_or_url)
    if host in _PUSH2_HOSTS:
        return PUSH2_SURFACE
    return host


# ---------------------------------------------------------------------------
# 请求预算
# ---------------------------------------------------------------------------
class RequestBudget:
    """单域名单轮请求硬上限（预计算 run 级别的计数器）。

    ``reserve()`` 在**发请求之前**扣减：超限时抛异常，调用方不得再发请求。
    计数按域名分桶（``push2his.eastmoney.com`` 与新浪备胎互不挤占）。
    """

    def __init__(self, limit=None):
        self.limit = request_budget(limit)
        self.counts: dict[str, int] = {}
        self.exceeded = False
        self.last_error = None

    def reserve(self, domain_or_url) -> str:
        domain = _domain(domain_or_url)
        current = int(self.counts.get(domain, 0))
        if current >= self.limit:
            self.exceeded = True
            self.last_error = (
                f"东财请求预算超限：{domain} 已发 {current} 次 ≥ 上限 "
                f"{self.limit}（本轮提前退出，未继续请求）")
            raise EastmoneyBudgetExceeded(self.last_error)
        self.counts[domain] = current + 1
        return domain

    def snapshot(self) -> dict:
        return {
            "limit_per_domain": self.limit,
            "counts": dict(sorted(self.counts.items())),
            "exceeded": bool(self.exceeded),
            "reason": self.last_error,
        }


# ---------------------------------------------------------------------------
# 统一取数闸门
# ---------------------------------------------------------------------------
class EmFetchGuard:
    """包一层 ``_astock_generated.em_get``：熔断检查 + 预算 + 444/FIN 识别。

    用法::

        guard = EmFetchGuard()
        with guard:
            rows, url = _em_clist_all(...)
        guard.requests, guard.block_reason, guard.budget.snapshot()

    为什么包在 ``em_get`` 这一层
    ----------------------------
    上游 ``_em_clist_all`` 会吞掉主域错误再换 ``push2delay`` 重试，
    ``stock_fund_flow_120d`` 把网络异常吞成空列表。只有在 HTTP 入口记录原始
    状态码 / 异常文案，才能做到「444 只发一次、命中即熔断」。

    ``enforce_budget=False`` 用于预计算自己按「逻辑请求」计数并在调用前
    ``budget.reserve`` 的场景（资金流）：此时闸门仍共享同一个
    :class:`RequestBudget` 做报告，但不重复扣减。

    ``surface``（2026-11-16）指定本闸门所守护的域名：冷却**读**取
    「顶层共享封锁 ∪ 本域条目」（任一命中都 0 请求），**写**只写本域条目，
    因此 datacenter 被封不会让 push2 的任务整轮停摆。不给 ``surface`` 时
    行为与既有实现完全一致（只读写顶层共享封锁）。

    ``escalate``（issue #130）只在带 ``surface`` 时有意义：``None``（默认）
    交给 :func:`mark_cooldown` 自动判定 —— 同一冷却窗口内 ≥2 个不同域命中
    444 才升级为顶层共享封锁；``True`` 强制升级，``False`` 永不升级。
    """

    def __init__(self, budget=None, path=None, cooldown_seconds_override=None,
                 block_exc=AntiScrapeBlocked, enforce_budget=True,
                 now_fn=None, mark=True, respect_cooling=True, surface=None,
                 escalate=None):
        if isinstance(budget, RequestBudget):
            self.budget = budget
        else:
            self.budget = RequestBudget(budget)
        self.path = path or state_path()
        self.cooling_seconds = (cooldown_seconds() if cooldown_seconds_override
                                is None else float(cooldown_seconds_override))
        self.block_exc = block_exc
        self.enforce_budget = bool(enforce_budget)
        self._now_fn = now_fn or time.time
        self._mark = bool(mark)
        self.respect_cooling = bool(respect_cooling)
        self.surface = surface_of(surface) if surface else None
        self.escalate = escalate
        self.requests = 0
        self.status_codes: list[int] = []
        self.block_reason: str | None = None
        self.block_detail: str | None = None
        self.blocked = False
        self.shared_blocked = False
        self._module = None
        self._original = None

    # -- 冷却 -------------------------------------------------------------
    def _now(self):
        try:
            return self._now_fn()
        except Exception:  # pragma: no cover - now_fn 不应影响取数
            return time.time()

    def cooling(self):
        if not self.respect_cooling:
            return None
        return scoped_cooldown_state(now=self._now(), path=self.path,
                                     surface=self.surface)

    @staticmethod
    def cooling_message(state) -> str:
        remaining = int(round(float(state.get("remaining_seconds") or 0)))
        scope = state.get("scope")
        where = f"本域 {state.get('surface')}" if scope == "surface" else "共享封锁"
        return (f"东财反爬冷却中（{where}，剩余 {remaining}s，原因 "
                f"{state.get('reason') or 'unknown'}），本次未发起请求")

    # -- 阻断 -------------------------------------------------------------
    def _record_block(self, reason: str, detail: str) -> None:
        if self.block_reason is None:
            self.block_reason = reason
            self.block_detail = detail
        self.blocked = True
        if self._mark:
            try:
                written = mark_cooldown(
                    reason, detail, seconds=self.cooling_seconds,
                    now=self._now(), path=self.path,
                    surface=self.surface, escalate=self.escalate)
                self.shared_blocked = bool(
                    written.get("escalated_shared")
                    or (self.surface is None and written.get("blocked_until")))
            except OSError as exc:
                # 冷却写失败不能掩盖取数失败：异常照抛，但必须出声。
                print(f"[WARN] 东财冷却写盘失败（{self.path}）：{exc}",
                      file=sys.stderr)

    def _block_message(self, reason: str, detail: str) -> str:
        return (f"东财反爬阻断（{reason}）：{detail[:200]}；"
                "本轮不换域重试，已记录冷却状态")

    # -- 上下文管理 -------------------------------------------------------
    def __enter__(self):
        cooling = self.cooling()
        if cooling:
            raise EastmoneyCooling(self.cooling_message(cooling))

        from stock_analysis.data_sources import _astock_generated as _g

        self._module = _g
        self._original = getattr(_g, "em_get", None)
        if self._original is None:
            return self

        def wrapped(url, params=None, headers=None, timeout=15, **kwargs):
            # 每个请求前再查一次：同一轮里别的进程可能刚写下冷却。
            cooling = self.cooling()
            if cooling:
                raise EastmoneyCooling(self.cooling_message(cooling))
            if self.enforce_budget:
                self.budget.reserve(url)
            self.requests += 1
            try:
                response = self._original(
                    url, params=params, headers=headers, timeout=timeout,
                    **kwargs)
            except Exception as exc:
                reason = classify_exception(exc)
                if reason:
                    self._record_block(reason, f"{type(exc).__name__}: {exc}")
                    raise self.block_exc(
                        self._block_message(reason, f"{type(exc).__name__}: {exc}")
                    ) from exc
                raise
            status = _status_of(response)
            if status is not None:
                self.status_codes.append(status)
            reason = classify_response(response)
            if reason:
                detail = (f"HTTP {status}（nginx 反爬）" if reason == "http_444"
                          else f"HTTP {status or '?'} 返回 0 字节")
                self._record_block(reason, detail)
                raise self.block_exc(self._block_message(reason, detail))
            return response

        _g.em_get = wrapped
        return self

    def __exit__(self, exc_type, exc, tb):
        if self._module is not None and self._original is not None:
            self._module.em_get = self._original
        return False