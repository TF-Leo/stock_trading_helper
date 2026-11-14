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
    "AntiScrapeBlocked",
    "EastmoneyCooling",
    "EastmoneyBudgetExceeded",
    "cooldown_seconds",
    "request_budget",
    "state_path",
    "cooldown_state",
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
def cooldown_state(now=None, path=None) -> dict | None:
    """读取冷却状态；仍在生效才返回（附带 ``remaining_seconds``），否则 None。

    文件不存在 / 损坏 / 已过期一律返回 ``None`` —— 冷却文件是**可选加速器**，
    不能因为它坏了把整条取数路径卡死。
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
    until = payload.get("blocked_until")
    try:
        until_ts = datetime.fromisoformat(str(until)).timestamp()
    except (TypeError, ValueError):
        return None
    now_ts = _now_ts(now)
    remaining = until_ts - now_ts
    if remaining <= 0:
        return None
    result = dict(payload)
    result["remaining_seconds"] = remaining
    return result


def mark_cooldown(reason: str, detail=None, seconds=None, now=None,
                  path=None) -> dict:
    """写入跨进程冷却（原子替换），返回合并后的冷却状态。

    「读-改-写」在文件锁内完成：并发进程各自只追加自己知道的封锁，不会用
    旧状态覆盖对方刚写下的更晚到期时间（取 ``max``）。
    """
    now_ts = _now_ts(now)
    wait = cooldown_seconds() if seconds is None else max(0.0, float(seconds))
    until_ts = now_ts + wait
    target = path or state_path()
    with _FileLock(_lock_path(target)):
        existing = cooldown_state(now=now_ts, path=target)
        if existing:
            try:
                existing_until = datetime.fromisoformat(
                    str(existing.get("blocked_until"))).timestamp()
            except (TypeError, ValueError):
                existing_until = 0.0
            until_ts = max(until_ts, existing_until)
        payload = {
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
        os.makedirs(os.path.dirname(target) or ".", exist_ok=True)
        tmp = f"{target}.{os.getpid()}.tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=1,
                      sort_keys=True)
            handle.write("\n")
        os.replace(tmp, target)
    return payload


def clear_cooldown(path=None) -> None:
    """删除冷却状态（人工确认封锁解除后复跑预计算时用）。"""
    try:
        os.unlink(path or state_path())
    except FileNotFoundError:
        pass


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
    """

    def __init__(self, budget=None, path=None, cooldown_seconds_override=None,
                 block_exc=AntiScrapeBlocked, enforce_budget=True,
                 now_fn=None, mark=True, respect_cooling=True):
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
        self.requests = 0
        self.status_codes: list[int] = []
        self.block_reason: str | None = None
        self.block_detail: str | None = None
        self.blocked = False
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
        return cooldown_state(now=self._now(), path=self.path)

    @staticmethod
    def cooling_message(state) -> str:
        remaining = int(round(float(state.get("remaining_seconds") or 0)))
        return (f"东财反爬冷却中（剩余 {remaining}s，原因 "
                f"{state.get('reason') or 'unknown'}），本次未发起请求")

    # -- 阻断 -------------------------------------------------------------
    def _record_block(self, reason: str, detail: str) -> None:
        if self.block_reason is None:
            self.block_reason = reason
            self.block_detail = detail
        self.blocked = True
        if self._mark:
            try:
                mark_cooldown(reason, detail, seconds=self.cooling_seconds,
                              now=self._now(), path=self.path)
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