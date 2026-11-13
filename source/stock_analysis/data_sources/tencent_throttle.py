# -*- coding: utf-8 -*-
"""腾讯 K 线限流：跨进程冷却（issue #13，P0）。

背景（``docs/P1_EVIDENCE_V2.md`` §2.2 / §7）
-------------------------------------------
R5 抓 300 只日线时**真实触发腾讯整域限流**：第 47 只后三个入口同时失败，
全轮 22 次 ``[backoff] 连续 N 只失败，冷却 120s``，32 只失败，最后靠 baostock
备胎补齐 57 只。根因是 300 只 × 8 年 ≈ 5700 次请求，落在上游「腾讯 K 线
5000+ 次后返回空」的区间，且一度有两个抓取进程并发。

上游（``vendor/a-stock-data/SKILL.md`` §1.2，第 1234 行起）用
``_tencent_host_down_until`` 做**进程内**冷却：进程退出即丢失，第二个进程
完全不知道前一个刚撞过限流，于是重复撞墙。

本模块只做一件事：把那本进程内账本镜像到磁盘。
------------------------------------------------
* :func:`sync_process_state` —— 每次取数前把磁盘冷却并入进程内字典。
  生成物 ``_tencent_kline_call`` 本来就跳过 ``_tencent_host_down_until`` 里
  未过期的入口，因此**不改生成物**也能实现「进程启动读取，未过期入口直接跳过」；
* :func:`flush_process_state` —— 把进程内新产生的冷却原子写回磁盘
  （tmp + ``os.replace``），下一个进程启动即可见；
* :func:`all_hosts_cooling` —— 三个入口全在冷却时让调用方**直接失败**，
  不再逐入口空转（每次空转都要付一个 HTTP 超时的代价）。

状态文件
--------
默认 ``source/stock_analysis/data/state/tencent_cooldown.json``
（``source/stock_analysis/data/`` 已在 ``.gitignore``，不入库）；
测试用环境变量 ``TENCENT_COOLDOWN_PATH`` 指向临时目录。

并发进程用 ``fcntl.flock`` 串行化「读-改-写」：否则后写的进程会用自己那份
（可能更旧的）字典覆盖先写者的冷却，等于没冷却。没有 ``fcntl`` 的平台
（Windows）退化为无锁，仍靠 ``os.replace`` 保证文件本身不会被读到半截。
"""
from __future__ import annotations

import contextlib
import json
import os
import time
from datetime import datetime, timezone

__all__ = [
    "STATE_PATH_ENV",
    "BUDGET_ENV",
    "DEFAULT_COOLDOWN_SECONDS",
    "DEFAULT_FAILURE_BUDGET_SECONDS",
    "MAX_BATCH_SIZE",
    "MIN_BATCH_PAUSE_SECONDS",
    "SCHEMA_VERSION",
    "FailureBudget",
    "TencentBudgetExceeded",
    "state_path",
    "load_state",
    "active_hosts",
    "process_hosts",
    "mark_hosts",
    "sync_process_state",
    "flush_process_state",
    "all_hosts_cooling",
    "cooling_summary",
    "clear_state",
    "failure_budget",
    "run_with_deadline",
    "http_timeout_for_budget",
    "DEFAULT_HTTP_CONNECT_TIMEOUT",
    "DEFAULT_HTTP_READ_TIMEOUT",
    "count_requests",
    "normalize_batch_args",
]

#: 测试/运维用：覆盖冷却状态文件路径。
STATE_PATH_ENV = "TENCENT_COOLDOWN_PATH"

#: 与上游 ``_TENCENT_HOST_COOLDOWN`` 保持一致（SKILL.md 第 1234 行）。
DEFAULT_COOLDOWN_SECONDS = 120.0

SCHEMA_VERSION = 1


def _now(now=None) -> float:
    return time.time() if now is None else float(now)


def state_path() -> str:
    """冷却状态文件路径（``TENCENT_COOLDOWN_PATH`` 优先，测试用）。"""
    override = str(os.getenv(STATE_PATH_ENV, "") or "").strip()
    if override:
        return override
    base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, "data", "state", "tencent_cooldown.json")


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


def load_state(now=None) -> dict:
    """读磁盘冷却状态；**过期条目一律剔除**，损坏/缺失返回空结构（不抛）。

    返回 ``{"hosts": {host: until_epoch}, "last_reason": str | None,
    "updated_at": str | None, "pid": int | None}``。
    """
    now = _now(now)
    try:
        with open(state_path(), "r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except Exception:
        return {"hosts": {}, "last_reason": None, "updated_at": None, "pid": None}
    if not isinstance(payload, dict):
        return {"hosts": {}, "last_reason": None, "updated_at": None, "pid": None}
    raw_hosts = payload.get("hosts")
    hosts = {}
    if isinstance(raw_hosts, dict):
        for host, until in raw_hosts.items():
            try:
                value = float(until)
            except (TypeError, ValueError):
                continue
            if value > now:
                hosts[str(host)] = value
    return {
        "hosts": hosts,
        "last_reason": payload.get("last_reason"),
        "updated_at": payload.get("updated_at"),
        "pid": payload.get("pid"),
    }


def _write_state(hosts, reason=None, now=None) -> None:
    """原子写：同目录 tmp 文件 + ``os.replace``（读者永远看不到半截 JSON）。"""
    now = _now(now)
    path = state_path()
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    payload = {
        "schema_version": SCHEMA_VERSION,
        "updated_at": datetime.fromtimestamp(now, timezone.utc).isoformat(
            timespec="seconds"),
        "hosts": {host: until for host, until in sorted(hosts.items())},
        "last_reason": reason,
        "pid": os.getpid(),
    }
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=1, sort_keys=True)
        handle.write("\n")
    os.replace(tmp, path)


def active_hosts(now=None) -> dict:
    """当前仍在冷却的入口 → 到期时间戳（磁盘口径）。"""
    return load_state(now)["hosts"]


def _process_dict():
    """生成物的进程内冷却字典（延迟导入，避免包内循环依赖）。"""
    from . import _astock_generated as generated

    return generated._tencent_host_down_until


def process_hosts(now=None) -> dict:
    """进程内仍在冷却的入口 → 到期时间戳。"""
    now = _now(now)
    alive = {}
    for host, until in list(_process_dict().items()):
        try:
            value = float(until)
        except (TypeError, ValueError):
            continue
        if value > now:
            alive[str(host)] = value
    return alive


def mark_hosts(hosts, seconds=DEFAULT_COOLDOWN_SECONDS, reason=None, now=None) -> dict:
    """把若干入口冷却 ``seconds`` 秒（磁盘 + 进程内），返回合并后的有效冷却。

    「读-改-写」在文件锁内完成：并发进程各自只追加自己知道的冷却，
    不会用旧字典覆盖对方刚写下的条目。
    """
    now = _now(now)
    until = now + float(seconds)
    names = [str(host) for host in (hosts or [])]
    process = _process_dict()
    for host in names:
        if float(process.get(host, 0.0)) < until:
            process[host] = until
    path = state_path()
    with _FileLock(_lock_path(path)):
        merged = load_state(now)["hosts"]
        for host in names:
            merged[host] = max(float(merged.get(host, 0.0)), until)
        _write_state(merged, reason=reason, now=now)
    return merged


def sync_process_state(now=None) -> dict:
    """磁盘冷却 → 进程内字典；返回合并后的有效冷却。

    调用点：``astock.fetch_daily`` / ``fetch_index_daily`` 每次取数前。
    只做「取较晚者」，不会把进程内更早的条目改晚，也不会复活已过期条目。
    """
    now = _now(now)
    process = _process_dict()
    for host, until in load_state(now)["hosts"].items():
        if float(process.get(host, 0.0)) < until:
            process[host] = until
    return process_hosts(now)


def flush_process_state(reason=None, now=None) -> dict:
    """进程内冷却 → 磁盘（原子）。没有有效冷却时直接返回，不触碰磁盘。"""
    now = _now(now)
    alive = process_hosts(now)
    if not alive:
        return {}
    path = state_path()
    with _FileLock(_lock_path(path)):
        merged = load_state(now)["hosts"]
        changed = False
        for host, until in alive.items():
            if float(merged.get(host, 0.0)) < until:
                merged[host] = until
                changed = True
        if changed:
            _write_state(merged, reason=reason, now=now)
        return dict(merged)


def all_hosts_cooling(now=None) -> bool:
    """三个 K 线入口是否**全部**在冷却中（磁盘 + 进程内合并后判定）。"""
    now = _now(now)
    from . import _astock_generated as generated

    hosts = [str(host) for host in
             (getattr(generated, "TENCENT_KLINE_HOSTS", ()) or ())]
    if not hosts:
        return False
    active = sync_process_state(now)
    return all(float(active.get(host, 0.0)) > now for host in hosts)


def cooling_summary(now=None) -> str:
    """人读的冷却摘要，例如 ``web.ifzq.gtimg.cn 剩余 87s；…``。"""
    now = _now(now)
    active = sync_process_state(now)
    if not active:
        return "无冷却"
    parts = []
    for host, until in sorted(active.items(), key=lambda item: -item[1]):
        name = host.split("//", 1)[-1]
        parts.append(f"{name} 剩余 {max(0, int(round(until - now)))}s")
    return "；".join(parts)


def clear_state() -> None:
    """删除状态文件与锁文件（测试 / 运维手动解除冷却用）。"""
    for path in (state_path(), _lock_path(state_path())):
        try:
            os.remove(path)
        except FileNotFoundError:
            pass
        except Exception:
            pass


# ---------------------------------------------------------------------------
# 请求预算（issue #13 第 2/3 条）
# ---------------------------------------------------------------------------
#: 单票「全通道失败」的总耗时上限（秒）。issue #13 第 5 条：超时立刻回退缓存并
#: 标 degraded，绝不让一只票把整轮拖成 3 入口 × 19 分页窗口 × 20s 超时。
BUDGET_ENV = "DATA_SOURCE_TICKER_BUDGET_SECONDS"
DEFAULT_FAILURE_BUDGET_SECONDS = 20.0

#: 批量抓取的硬上限（issue #13 第 2 条）：`batch-size ≤ 20` + `batch-pause ≥ 20s`。
#: R5 的 300 只 × 8 年 ≈ 5700 次请求正是撞上「5000+ 次后返回空」的区间。
MAX_BATCH_SIZE = 20
MIN_BATCH_PAUSE_SECONDS = 20.0


class TencentBudgetExceeded(RuntimeError):
    """单票取数超出时间预算（不是「该票无数据」，调用方应回退缓存并标 degraded）。"""


def failure_budget(default=None) -> float:
    """单票失败预算秒数：``DATA_SOURCE_TICKER_BUDGET_SECONDS`` 优先。"""
    if default is None:
        raw = str(os.getenv(BUDGET_ENV, "") or "").strip()
        try:
            return float(raw) if raw else DEFAULT_FAILURE_BUDGET_SECONDS
        except ValueError:
            return DEFAULT_FAILURE_BUDGET_SECONDS
    return float(default)


def run_with_deadline(func, timeout, *args, **kwargs):
    """在 ``timeout`` 秒内跑 ``func``；超时抛 :class:`TencentBudgetExceeded`。

    为什么用线程而不是 ``signal.alarm``：``signal`` 只在主线程可用，而取数在
    CLI 与脚本里都可能在非主线程调用；``requests`` 的超时只覆盖**单次**读写，
    无法约束「3 入口 × 19 分页窗口」这种乘法级耗时。

    超时后**放弃的线程仍可能在跑**（Python 无法强杀线程），因此调用方必须
    **不再发起新的请求**（本项目的用法：超时即终止该票，回退缓存），
    这样进程内最多只有一个遗留请求在飞，不违反上游「串行」的防封铁律。

    使用点（issue #67 D2）：研究路径按**整票**套一次；生产路径
    ``data_loader/bars.py::_DataSourcePool.fetch`` 按**单通道**套一次
    （剩余预算），保证「单通道阻塞」也吃不满整票预算。
    """
    import threading

    timeout = float(timeout)
    if timeout <= 0:
        raise TencentBudgetExceeded(f"时间预算已耗尽（剩余 {timeout:.1f}s）")

    box = {}

    def _target():
        try:
            box["value"] = func(*args, **kwargs)
        except BaseException as exc:  # noqa: BLE001 - 原样回传给调用线程
            box["error"] = exc

    worker = threading.Thread(target=_target, name="tencent-fetch", daemon=True)
    worker.start()
    worker.join(timeout)
    if worker.is_alive():
        raise TencentBudgetExceeded(f"取数超过 {timeout:.1f}s 预算，已放弃本次取数")
    if "error" in box:
        raise box["error"]
    return box.get("value")


# ---------------------------------------------------------------------------
# 单次 HTTP 超时 × 预算（issue #67 D2）
# ---------------------------------------------------------------------------
#: 生成物 ``_astock_generated._ASTOCK_HTTP_TIMEOUT`` 的默认 (connect, read)。
#: 预算充足时保持原值不变（20s 预算下 5+15 正好用满一个请求的最坏耗时）。
DEFAULT_HTTP_CONNECT_TIMEOUT = 5.0
DEFAULT_HTTP_READ_TIMEOUT = 15.0


def http_timeout_for_budget(remaining, connect=DEFAULT_HTTP_CONNECT_TIMEOUT,
                            read=DEFAULT_HTTP_READ_TIMEOUT):
    """把单次 HTTP 超时收敛到「该通道剩余预算」之内。

    D2（issue #67）实测：预算 3s 时单请求仍是 ``(5, 15)``，一个挂死入口光
    connect + read 就能吃掉 20s，与预算完全错配。规则：

    * 预算充足（``remaining ≥ connect + read``）→ 原样返回默认值，不改变
      正常取数的容错能力；
    * 预算收紧 → connect 占剩余预算的 1/3、read 吃掉其余 2/3，
      保证 ``connect + read ≤ remaining``（Python 浮点误差量级）。

    返回 ``(connect, read)``，单位秒；调用方须保证 ``remaining > 0``
    （渠道池进入通道前已判定）。这是**协作式**上限：requests 对 DNS
    / 半开连接偶有不严格遵守 read timeout 的情况，所以调用方仍必须用
    :func:`run_with_deadline` 兜底墙钟（见 D2 修复说明）。
    """
    remaining = max(0.0, float(remaining))
    # 极小预算（< 0.2s）下不再施加 0.1s 下限，否则 connect + read 会超出预算。
    floor = min(0.1, remaining / 2.0)
    connect = min(float(connect), max(floor, remaining / 3.0))
    read = min(float(read), max(floor, remaining - connect))
    return (connect, read)


@contextlib.contextmanager
def count_requests():
    """统计上下文内的 HTTP 请求次数（演练 / 报告用）。

    做法：临时包一层 ``requests.request``（生成物的腾讯调用走的就是它）。
    仅用于**单线程**演练；退出时无条件还原，测试里的 monkeypatch 会直接覆盖
    这层包装（覆盖后不再计数，但不影响正确性）。
    """
    from . import _astock_generated as generated

    original = generated.requests.request
    stats = {"count": 0}

    def _wrapper(*args, **kwargs):
        stats["count"] += 1
        return original(*args, **kwargs)

    generated.requests.request = _wrapper
    try:
        yield stats
    finally:
        generated.requests.request = original


class FailureBudget:
    """连续失败预算：连续 N 只失败后，**本轮剩余票直接走备胎**。

    issue #13 第 2/3 条：R5 的旧实现是「每票都重试三入口」，在整域限流时把
    一次限流放大成 3 倍请求。这里改成「连续 3 只失败 → 本轮腾讯整段停用」，
    剩余票一次也不试腾讯，直接 baostock；下一个新轮次（新进程/新一轮）重新判定。
    """

    def __init__(self, max_consecutive_failures=3):
        self.max_consecutive_failures = max(1, int(max_consecutive_failures))
        self.consecutive_failures = 0
        self.total_failures = 0
        self.total_successes = 0
        self.primary_blocked = False
        self.blocked_reason = None

    def record_success(self):
        self.consecutive_failures = 0
        self.total_successes += 1

    def record_failure(self, reason=None):
        self.consecutive_failures += 1
        self.total_failures += 1
        if self.consecutive_failures >= self.max_consecutive_failures:
            self.primary_blocked = True
            if not self.blocked_reason:
                self.blocked_reason = (
                    f"连续 {self.consecutive_failures} 只失败，本轮剩余票直接走备胎"
                    + (f"（最后一次：{reason}）" if reason else ""))
        return self.primary_blocked

    def block(self, reason):
        """显式停用主源（例如跨进程冷却文件显示三入口全冷却）。"""
        self.primary_blocked = True
        self.blocked_reason = self.blocked_reason or reason

    def snapshot(self):
        return {
            "consecutive_failures": self.consecutive_failures,
            "total_failures": self.total_failures,
            "total_successes": self.total_successes,
            "primary_blocked": self.primary_blocked,
            "blocked_reason": self.blocked_reason,
        }


def normalize_batch_args(batch_size, batch_pause):
    """把批量参数夹到预算内；返回 ``(size, pause, notes)``。

    ``notes`` 是给调用方打印的说明（**不静默改参数**：被夹过必须让人看见）。
    """
    notes = []
    size = int(batch_size)
    if size > MAX_BATCH_SIZE:
        notes.append(f"batch-size {size} 超过上限 {MAX_BATCH_SIZE}，已夹到 {MAX_BATCH_SIZE}")
        size = MAX_BATCH_SIZE
    if size < 1:
        notes.append(f"batch-size {size} 非法，已改为 1")
        size = 1
    pause = float(batch_pause)
    if pause < MIN_BATCH_PAUSE_SECONDS:
        notes.append(f"batch-pause {pause:g}s 低于下限 {MIN_BATCH_PAUSE_SECONDS:g}s，"
                     f"已提到 {MIN_BATCH_PAUSE_SECONDS:g}s")
        pause = MIN_BATCH_PAUSE_SECONDS
    return size, pause, notes

