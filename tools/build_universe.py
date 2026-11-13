#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""R9 / issue #83：建无偏日线池（全 A + 退市，串行、可校验、可续跑）。

任务书：GitHub issue #83；可行性依据：``docs/UNIVERSE_FEASIBILITY.md``（R8 / #81）。

本工具只做**研究基础设施**：不写入 ``source/``，不改生产 CLI / 决策逻辑 /
``actions.py`` / ``requirements.txt``，不引入新依赖（baostock 已在
``requirements-research.txt``）。

三个子命令
----------
``fetch``     联网，baostock **单会话、严格串行**取沪深全 A（含退市）日线。
              逐票校验、失败重试、断点续跑；落盘 ``evidence/universe/``
              （``evidence/`` 已 gitignore，不入库）。
``validate``  默认离线：复核每票 CSV、行数分布、日期覆盖、退市退出价；
              ``--reconcile N`` 再联网抽 N 票与 baostock 直查对账。
``selftest``  纯合成数据，验证取数校验与退市分类逻辑（不联网）。

为什么必须串行
--------------
R8 §5.2 实测：baostock 并发取数会在**正好 2,000 行**处静默截断
（``error_code=10001001``，末根日期倒退数月；4 并发多轮中招、2 并发 1/6）。
本文件**没有任何线程 / 多进程**；``_assert_single_thread()`` 在入口再兜一次。
提速只能靠断点续跑。

逐请求校验（任一不满足 → 整票重试，仍失败则记录并继续）
------------------------------------------------------
1. ``error_code == "0"``；
2. 非空、日期唯一、严格升序、不越出请求区间；
3. 行数不超过区间工作日数（+2 容差）且不超过 ``--max-rows``；
4. **正好 2,000 行一律判可疑截断**（R8 实测的失效形态）；
5. 末根日期与预期结束日（在市 = 窗口末，退市 = ``outDate``）的工作日间隔
   超过 60 个工作日判失败、超过 5 个工作日记 warning。

防挂死（2026-11-13 事故后加）
-----------------------------
* 查询 / 登出 / 登录分别有 90s / 30s / 45s 的 SIGALRM 硬超时；
  ``socket.setdefaulttimeout(90)`` 兜底（baostock 不暴露 socket timeout）。
* 任何失败都**强制关闭 socket** 再重登 —— baostock 的 ``send_msg`` 会吞掉
  SIGALRM 异常，绝不能指望它的 ``logout()`` 自己返回。
* 任一次阻塞失败后 ``bs.logout`` + ``bs.login`` + 重试该票（最多 3 次）。
* 进度看门狗：``--watchdog-seconds``（默认 600s）内没有任何票完成，就保存
  manifest 并以退出码 4 退出，让外层直接重跑续跑，**不再静默挂死**。

退市退出口径（R8 §6，预注册写死）
--------------------------------
* 退出价 = 最后一个 ``tradestatus == 1`` 交易日的收盘价（前复权），
  **禁止默认记 −100%**（双重计亏）。
* ``exit_reason``：
  - ``merger``  —— 命中人工例外表（``KNOWN_MERGERS``），或末段 30 个可交易日
    跌幅 < 10%（R8 的半自动换股/并购判据，记 ``flat_last30_suspect_merger``）；
  - ``price``   —— 其余可用价格序列（默认规则 1）；
  - ``unknown`` —— 没有任何 ``tradestatus == 1`` 行，无法定价。
* 另记 ``out_date``、``last_tradestatus1_date/close``、最后一行、
  冻结期（最后可交易日 → ``outDate``）长度。

用法
----
    python tools/build_universe.py selftest
    python tools/build_universe.py fetch --limit 50          # 阶段 1
    python tools/build_universe.py fetch                     # 阶段 2（约 6h）
    python tools/build_universe.py validate --reconcile 10

退出码：0 = 成功/无校验错误；1 = 校验发现错误；2 = 参数或依赖错误；
        3 = 重新登录失败（断点已保存，可重跑续）；
        4 = 进度看门狗触发（断点已保存，可重跑续）；
        130 = 被中断（断点已保存，重跑同一命令即可续）。
"""
from __future__ import annotations

import argparse
import glob
import hashlib
import json
import os
import signal
import sys
import threading
import time
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from typing import Any

import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT_DIR = os.path.join(ROOT, "evidence", "universe")
CSV_DIR = os.path.join(OUT_DIR, "csv")
UNIVERSE_PATH = os.path.join(OUT_DIR, "universe.json")
MANIFEST_PATH = os.path.join(OUT_DIR, "manifest.json")
REPORT_PATH = os.path.join(OUT_DIR, "validate-report.json")
LOG_PATH = os.path.join(OUT_DIR, "fetch.log")

DEFAULT_START = "2018-01-01"
DEFAULT_END = "2026-11-12"
FIELDS = "date,code,open,high,low,close,volume,amount,tradestatus,pctChg,isST"
ADJUST = "2"  # 前复权；R8 §2.3 实测退市票同样可用
MAX_ROWS = 4000  # 2018–2026 约 2,150 个交易日；重复页缺陷会放大到百万行

# R8 已实测的退市样本（按 outDate 覆盖 2020/2021/2023/2025/2026），
# 阶段 1 的 ``--limit 50`` 默认带上这 5 只。
DEFAULT_DELISTED_SAMPLE = (
    "sz.002450",  # 康得退 2021-05-31
    "sz.000939",  # 凯迪退 2020-12-17
    "sh.600077",  # *ST宋都 2023-07-25
    "sh.600070",  # *ST富润 2025-04-30
    "sz.301139",  # 元道退 2026-10-28（新式：末根在 outDate 前一日）
)

# R8 §6 规则 2 的人工例外表：换股/吸收合并，价格序列不反映真实退出价值。
KNOWN_MERGERS = {
    "sh.600005": "武钢股份：宝钢换股吸收合并（末段平收 3.71，不能当作真实退出价）",
}

RETRY_ATTEMPTS = 3
RETRY_BACKOFF = (2.0, 4.0, 8.0)
# 每次阻塞式网络原语（查询 / 登出 / 登录）都有独立的进程内硬超时。
# 2026-11-13 事故：baostock 的 send_msg 会吞掉 SIGALRM 异常，若随后调用
# 未加超时的 bs.logout()，recv() 会永久阻塞（5h43m / 31s CPU / ESTAB 0 字节）。
REQUEST_TIMEOUT_S = 90
LOGOUT_TIMEOUT_S = 30
LOGIN_TIMEOUT_S = 45
WATCHDOG_SECONDS = 600  # 10 分钟没有任何票完成 → 保存断点并以非 0 退出
GAP_HARD_WEEKDAYS = 60
GAP_WARN_WEEKDAYS = 5
TRUNCATION_ROWS = 2000  # R8 实测的并发截断形态

_FETCH_STATE = {"requests": 0, "request_seconds": 0.0}


# ---------------------------------------------------------------------------
# 通用小工具
# ---------------------------------------------------------------------------
def log(msg: str) -> None:
    line = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    print(line, flush=True)
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        pass


def _parse_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def weekdays_between(start: date, end: date) -> int:
    """闭区间内的工作日数（不扣除法定节假日，因此是保守上界）。"""
    if end < start:
        return 0
    n = 0
    cur = start
    one = timedelta(days=1)
    while cur <= end:
        if cur.weekday() < 5:
            n += 1
        cur += one
    return n


def _md5(path: str) -> str:
    h = hashlib.md5()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def write_json(path: str, payload: Any) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=1, sort_keys=True)
        fh.write("\n")
    os.replace(tmp, path)


def read_json(path: str) -> Any:
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def _assert_single_thread() -> None:
    """硬约束：本工具禁止任何并发（R8 §5.2 的 2,000 行静默截断）。"""
    if threading.active_count() != 1:
        raise RuntimeError(
            f"检测到 {threading.active_count()} 个线程；本工具禁止并发取数")


class RequestTimeout(RuntimeError):
    pass


class WatchdogFired(RuntimeError):
    pass


class SessionError(RuntimeError):
    """重新登录失败：保存断点并让外层续跑，而不是原地卡死。"""


class QueryError(RuntimeError):
    def __init__(self, code: str, msg: str):
        super().__init__(f"baostock error_code={code} {msg}")
        self.code = code


class SuspiciousData(RuntimeError):
    pass


@contextmanager
def _deadline(seconds: float):
    """进程内单请求超时（baostock 不暴露 socket timeout，见 R8 §2.4）。"""
    if seconds <= 0 or not hasattr(signal, "SIGALRM"):
        yield
        return

    def _handler(signum, frame):  # noqa: ARG001
        raise RequestTimeout(f"单请求超过 {seconds:g}s")

    old = signal.signal(signal.SIGALRM, _handler)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, old)


def _watchdog_check(last_progress_ts: float, limit_s: float,
                    where: str = "") -> None:
    """进度看门狗：超过 ``limit_s`` 没有任何票完成就抛 WatchdogFired。

    在**主循环**里检查（不引入线程，保持单线程取数）；配合下面的逐原语
    硬超时，任何一次阻塞最长也只有数十秒，主循环必然能重新拿到控制权。
    """
    if limit_s <= 0:
        return
    idle = time.time() - last_progress_ts
    if idle > limit_s:
        raise WatchdogFired(
            f"{idle / 60:.1f} 分钟没有任何票完成（阈值 {limit_s / 60:.1f} 分钟）"
            + (f"，位置：{where}" if where else ""))


# ---------------------------------------------------------------------------
# socket 兜底超时 / baostock 会话修复
#
# why：baostock 不暴露 socket timeout；``util.socketutil.send_msg`` 里的
# ``while True: recv()`` 没有任何超时，且它会 ``except Exception`` 吞掉
# SIGALRM 抛出的 RequestTimeout。事故路径正是：查询超时被吞 → 未包装的
# ``bs.logout()`` 在坏 socket 上 ``recv()`` 永久阻塞。
# 对策：① 全局 socket 默认超时；② 每次 login 后给生产 socket 显式设超时；
# ③ login / logout 本身也用 SIGALRM 硬超时；④ 任何失败都强制关 socket。
# ---------------------------------------------------------------------------
def _install_socket_default_timeout(seconds: float = REQUEST_TIMEOUT_S) -> None:
    import socket
    socket.setdefaulttimeout(seconds)


def _bs_context():
    try:
        import baostock.common.context as conx
    except ImportError:
        return None
    return conx


def _set_bs_socket_timeout(seconds: float = REQUEST_TIMEOUT_S) -> None:
    conx = _bs_context()
    sock = getattr(conx, "default_socket", None) if conx is not None else None
    if sock is not None:
        try:
            sock.settimeout(seconds)
        except OSError:
            pass


def _close_bs_socket() -> None:
    """坏会话的 socket 直接关掉（不依赖 baostock 的优雅 logout）。"""
    conx = _bs_context()
    if conx is None:
        return
    sock = getattr(conx, "default_socket", None)
    if sock is not None:
        try:
            sock.close()
        except OSError:
            pass
    try:
        conx.default_socket = None
    except Exception:  # noqa: BLE001
        pass


# ---------------------------------------------------------------------------
# selftest 覆盖的纯校验逻辑
# ---------------------------------------------------------------------------
def validate_series(
    fields: list[str],
    rows: list[list[str]],
    *,
    code: str,
    req_start: str,
    req_end: str,
    ipo_date: str | None,
    out_date: str | None,
    max_rows: int = MAX_ROWS,
) -> tuple[list[str], list[str], dict[str, Any]]:
    """单票取数结果校验；返回 ``(errors, warnings, info)``。

    纯函数，不联网、不写盘，``selftest`` 直接调它。
    """
    errors: list[str] = []
    warnings: list[str] = []
    info: dict[str, Any] = {"code": code, "rows": len(rows)}
    if not rows:
        errors.append("empty_series")
        return errors, warnings, info

    di = fields.index("date")
    dates = [str(r[di]) for r in rows]
    info.update({"first": dates[0], "last": dates[-1], "request_start": req_start,
                 "request_end": req_end, "ipo_date": ipo_date, "out_date": out_date})

    if len(rows) > max_rows:
        errors.append(f"rows_{len(rows)}>max_{max_rows}")
    if len(rows) == TRUNCATION_ROWS:
        errors.append("exactly_2000_rows_suspect_truncation")
    if len(set(dates)) != len(dates):
        errors.append("duplicate_dates")
    if dates != sorted(dates):
        errors.append("dates_not_sorted")

    d_start, d_end = _parse_date(req_start), _parse_date(req_end)
    first, last = _parse_date(dates[0]), _parse_date(dates[-1])
    if d_start and first and first < d_start:
        errors.append(f"first_date_{dates[0]}_before_request_start_{req_start}")
    if d_end and last and last > d_end:
        errors.append(f"last_date_{dates[-1]}_after_request_end_{req_end}")

    expected_end = d_end
    if out_date:
        d_out = _parse_date(out_date)
        if d_out and (expected_end is None or d_out < expected_end):
            expected_end = d_out
    if last and expected_end:
        gap = weekdays_between(last + timedelta(days=1), expected_end)
        info["last_gap_weekdays"] = gap
        if gap > GAP_HARD_WEEKDAYS:
            errors.append(f"last_date_gap_{gap}_weekdays")
        elif gap > GAP_WARN_WEEKDAYS:
            warnings.append(f"last_date_gap_{gap}_weekdays")

    if first and last:
        weekdays = weekdays_between(first, last)
        info["weekdays"] = weekdays
        if len(rows) > weekdays + 2:
            errors.append(f"rows_{len(rows)}>weekdays_{weekdays}+2")

    return errors, warnings, info


def delisted_meta(
    fields: list[str],
    rows: list[list[str]],
    out_date: str | None,
) -> dict[str, Any] | None:
    """退市票退出口径（R8 §6）：最后 ``tradestatus==1`` 收盘 + 退出原因分类。"""
    if not out_date or not rows:
        return None
    di, ti, ci, si = (fields.index("date"), fields.index("tradestatus"),
                      fields.index("close"), fields.index("isST"))
    ts1: list[tuple[str, float]] = []
    for r in rows:
        if str(r[ti]) != "1":
            continue
        try:
            ts1.append((str(r[di]), float(r[ci])))
        except (TypeError, ValueError):
            continue
    last_row = rows[-1]
    last_row_close = _to_float(last_row[ci])
    d_out = _parse_date(out_date)
    meta: dict[str, Any] = {
        "out_date": out_date,
        "last_row_date": str(last_row[di]),
        "last_row_close": last_row_close,
        "last_row_tradestatus": str(last_row[ti]),
        "last_row_isST": str(last_row[si]),
        "exit_price": None,
        "last_tradestatus1_date": None,
        "last_tradestatus1_close": None,
        "last30_return": None,
        "freeze_calendar_days": None,
        "freeze_weekdays": None,
        "exit_reason": "unknown",
        "exit_reason_basis": "no_tradestatus1_row",
        "exit_reason_suspect": False,
    }
    if not ts1:
        return meta

    exit_date, exit_price = ts1[-1]
    meta.update({
        "exit_price": exit_price,
        "last_tradestatus1_date": exit_date,
        "last_tradestatus1_close": exit_price,
    })
    if len(ts1) >= 2:
        base_price = ts1[max(0, len(ts1) - 30)][1]
        if base_price > 0:
            meta["last30_return"] = round(exit_price / base_price - 1.0, 6)

    d_exit = _parse_date(exit_date)
    if d_exit and d_out:
        meta["freeze_calendar_days"] = (d_out - d_exit).days
        meta["freeze_weekdays"] = weekdays_between(
            d_exit + timedelta(days=1), d_out)

    code = str(rows[0][fields.index("code")])
    if code in KNOWN_MERGERS:
        meta["exit_reason"] = "merger"
        meta["exit_reason_basis"] = "manual_exception_table"
        meta["exit_reason_note"] = KNOWN_MERGERS[code]
    elif meta["last30_return"] is not None and meta["last30_return"] >= -0.10:
        meta["exit_reason"] = "merger"
        meta["exit_reason_basis"] = "flat_last30_suspect_merger"
        meta["exit_reason_suspect"] = True
    else:
        meta["exit_reason"] = "price"
        meta["exit_reason_basis"] = "last_tradestatus1_close"
    return meta


def _to_float(value: Any) -> float | None:
    try:
        if value is None or value == "":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# baostock 取数（单会话 + 掉线重登）
# ---------------------------------------------------------------------------
def bs_login(bs, timeout_s: float = LOGIN_TIMEOUT_S) -> None:
    """登录；有硬超时，失败时强制关掉半开 socket。"""
    try:
        with _deadline(timeout_s):
            result = bs.login()
        if str(getattr(result, "error_code", "")) != "0":
            raise RuntimeError(
                f"baostock 登录失败 {getattr(result, 'error_code', '?')} "
                f"{getattr(result, 'error_msg', '')}")
    except BaseException:
        _close_bs_socket()
        raise
    # 兜底：即使 defaulttimeout 被库绕开，生产 socket 也必须有超时。
    _set_bs_socket_timeout(REQUEST_TIMEOUT_S)


def bs_logout(bs, timeout_s: float = LOGOUT_TIMEOUT_S) -> None:
    """登出；**永不挂死**。超时/异常都强制关闭 socket，随后可重新登录。"""
    try:
        with _deadline(timeout_s):
            bs.logout()
    except Exception as exc:  # noqa: BLE001
        log(f"  [session] logout 未完成（{type(exc).__name__}: {exc}），强制关闭 socket")
    finally:
        _close_bs_socket()


def _fetch_once(bs, code: str, start: str, end: str, adjust: str,
                max_rows: int, timeout_s: float = REQUEST_TIMEOUT_S,
                ) -> tuple[list[str], list[list[str]], float]:
    """一次查询（含读行）；返回 ``(fields, rows, seconds)``。

    ``query_history_k_data_plus`` 与后续每一页 ``rs.next()`` 都在同一个
    SIGALRM 窗口内，任何一次网络阻塞超过 ``timeout_s`` 都会抛
    RequestTimeout（注意：baostock 内部可能把它吞成 error_code）。
    """
    began = time.time()
    with _deadline(timeout_s):
        rs = bs.query_history_k_data_plus(
            code, FIELDS, start_date=start, end_date=end,
            frequency="d", adjustflag=adjust)
        if str(rs.error_code) != "0":
            raise QueryError(str(rs.error_code), str(rs.error_msg))
        fields = list(rs.fields)
        rows: list[list[str]] = []
        while rs.next():
            rows.append(rs.get_row_data())
            if len(rows) > max_rows:
                raise SuspiciousData(f"行数超过上限 {max_rows}（疑似重复页缺陷）")
        if str(rs.error_code) != "0":
            raise QueryError(str(rs.error_code), str(rs.error_msg))
    return fields, rows, time.time() - began


def query_stock_basic(bs) -> list[dict[str, Any]]:
    """证券主表（``type=1`` 股票），带超时与重登。"""
    last: Exception | None = None
    for attempt in range(RETRY_ATTEMPTS):
        try:
            with _deadline(REQUEST_TIMEOUT_S):
                rs = bs.query_stock_basic()
                if str(rs.error_code) != "0":
                    raise QueryError(str(rs.error_code), str(rs.error_msg))
                fields = list(rs.fields)
                rows = []
                while rs.next():
                    rows.append(rs.get_row_data())
                    if len(rows) > 20000:
                        raise SuspiciousData("主表行数异常")
            break
        except Exception as exc:  # noqa: BLE001
            last = exc
            log(f"  [retry {attempt + 1}/{RETRY_ATTEMPTS}] 主表查询失败："
                f"{type(exc).__name__}: {exc}")
            bs_logout(bs)
            time.sleep(RETRY_BACKOFF[min(attempt, len(RETRY_BACKOFF) - 1)])
            bs_login(bs)
    else:
        raise RuntimeError(f"主表查询连续失败：{last}")

    idx = {f: i for i, f in enumerate(fields)}
    stocks = []
    for r in rows:
        if str(r[idx["type"]]) != "1":
            continue
        out = str(r[idx["outDate"]]) or ""
        stocks.append({
            "code": str(r[idx["code"]]),
            "name": str(r[idx["code_name"]]),
            "ipo": str(r[idx["ipoDate"]]),
            "out": out,
            "status": str(r[idx["status"]]),
        })
    stocks.sort(key=lambda s: s["code"])
    return stocks


def _fetch_with_retry(bs, stock: dict[str, Any], req_start: str, req_end: str,
                      adjust: str, max_rows: int,
                      timeout_s: float = REQUEST_TIMEOUT_S,
                      backoff: tuple[float, ...] = RETRY_BACKOFF,
                      on_attempt=None,
                      ) -> tuple[list[str], list[list[str]], list[str], dict[str, Any]]:
    """逐票取数：超时/校验失败 → 关 socket → 重登 → 重试，最多 RETRY_ATTEMPTS 次。

    ``on_attempt`` 在每次尝试开始时调用（主循环用它跑进度看门狗）。
    """
    last: Exception | None = None
    for attempt in range(RETRY_ATTEMPTS):
        if on_attempt is not None:
            on_attempt()
        _FETCH_STATE["requests"] += 1
        try:
            fields, rows, seconds = _fetch_once(
                bs, stock["code"], req_start, req_end, adjust, max_rows,
                timeout_s=timeout_s)
            _FETCH_STATE["request_seconds"] += seconds
            errors, warnings, info = validate_series(
                fields, rows, code=stock["code"], req_start=req_start,
                req_end=req_end, ipo_date=stock["ipo"], out_date=stock["out"] or None,
                max_rows=max_rows)
            info["seconds"] = round(seconds, 3)
            info["adjustflag"] = adjust
            if errors:
                raise SuspiciousData("; ".join(errors))
            return fields, rows, warnings, info
        except Exception as exc:  # noqa: BLE001
            last = exc
            log(f"  [retry {attempt + 1}/{RETRY_ATTEMPTS}] {stock['code']}："
                f"{type(exc).__name__}: {exc}")
            bs_logout(bs)
            time.sleep(backoff[min(attempt, len(backoff) - 1)])
            try:
                bs_login(bs)
            except Exception as login_exc:  # noqa: BLE001
                raise SessionError(
                    f"重新登录失败（{type(login_exc).__name__}: {login_exc}）") from login_exc
    raise RuntimeError(f"{type(last).__name__}: {last}")


# ---------------------------------------------------------------------------
# 落盘
# ---------------------------------------------------------------------------
def csv_path(code: str) -> str:
    return os.path.join(CSV_DIR, f"{code}.csv")


def save_series(code: str, fields: list[str], rows: list[list[str]]) -> tuple[int, int, str]:
    frame = pd.DataFrame(rows, columns=fields)
    for col in fields:
        if col not in ("date", "code"):
            frame[col] = pd.to_numeric(frame[col], errors="coerce")
    frame = frame.drop_duplicates(subset=["date"], keep="last")
    frame = frame.sort_values("date").reset_index(drop=True)
    path = csv_path(code)
    tmp = path + ".tmp"
    frame.to_csv(tmp, index=False)
    os.replace(tmp, path)
    return len(frame), os.path.getsize(path), _md5(path)


def config_hash(start: str, end: str, adjust: str, max_rows: int) -> str:
    payload = f"{start}|{end}|{adjust}|{max_rows}|{FIELDS}"
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


def empty_manifest(start: str, end: str, adjust: str, max_rows: int) -> dict[str, Any]:
    return {
        "schema": 1,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "updated_at": None,
        "config": {"start": start, "end": end, "adjustflag": adjust,
                   "fields": FIELDS, "max_rows": max_rows,
                   "request_timeout_s": REQUEST_TIMEOUT_S,
                   "retry_attempts": RETRY_ATTEMPTS},
        "config_hash": config_hash(start, end, adjust, max_rows),
        "universe": {},
        "selection": {},
        "stats": {},
        "entries": {},
    }


def load_manifest() -> dict[str, Any]:
    if os.path.exists(MANIFEST_PATH):
        try:
            return read_json(MANIFEST_PATH)
        except (OSError, json.JSONDecodeError) as exc:
            log(f"[WARN] manifest 读取失败（将重建）：{exc}")
    return {}


def save_manifest(manifest: dict[str, Any]) -> None:
    manifest["updated_at"] = datetime.now().isoformat(timespec="seconds")
    write_json(MANIFEST_PATH, manifest)


def entry_is_done(manifest: dict[str, Any], code: str) -> bool:
    entry = manifest.get("entries", {}).get(code)
    if not entry or entry.get("status") != "ok":
        return False
    path = csv_path(code)
    if not os.path.exists(path):
        return False
    size = entry.get("csv_bytes")
    return bool(size) and os.path.getsize(path) == size


def _adopt_existing_csv(stock: dict[str, Any], req_start_s: str, req_end_s: str,
                        adjust: str, max_rows: int) -> dict[str, Any] | None:
    """把「CSV 已落盘但 manifest 没记住」的票接管为 ok，避免重复抓取。

    2026-11-13 事故：进程挂死时 manifest 每 25 票才落一次盘，磁盘上多出 17 个
    已完成 CSV（sh.600692–sh.600710）却没有 entry。接管前用与 fetch 相同的
    ``validate_series`` 复核；不合规就返回 None，让主循环重新抓。
    """
    path = csv_path(stock["code"])
    if not os.path.exists(path):
        return None
    try:
        frame = pd.read_csv(path, dtype={"date": str, "code": str})
    except Exception:  # noqa: BLE001
        return None
    if list(frame.columns) != FIELDS.split(",") or len(frame) == 0:
        return None
    fields = list(frame.columns)
    rows = frame.astype(object).where(pd.notna(frame), "").values.tolist()
    errors, warnings, _info = validate_series(
        fields, rows, code=stock["code"], req_start=req_start_s, req_end=req_end_s,
        ipo_date=stock["ipo"], out_date=stock["out"] or None, max_rows=max_rows)
    if errors:
        return None
    dates = [str(r[fields.index("date")]) for r in rows]
    return {
        "status": "ok",
        "adopted": True,
        "name": stock["name"],
        "ipo_date": stock["ipo"],
        "out_date": stock["out"] or None,
        "request_start": req_start_s,
        "request_end": req_end_s,
        "adjustflag": adjust,
        "rows": len(frame),
        "first": dates[0],
        "last": dates[-1],
        "csv_bytes": os.path.getsize(path),
        "csv_md5": _md5(path),
        "fetch_seconds": None,
        "fetched_at": None,
        "adopted_at": datetime.now().isoformat(timespec="seconds"),
        "warnings": warnings,
        "delisted": delisted_meta(fields, rows, stock["out"] or None),
    }


# ---------------------------------------------------------------------------
# fetch
# ---------------------------------------------------------------------------
def _window_stocks(stocks: list[dict[str, Any]], start: str, end: str) -> list[dict[str, Any]]:
    d_start, d_end = _parse_date(start), _parse_date(end)
    out = []
    for s in stocks:
        d_ipo = _parse_date(s["ipo"])
        if d_ipo and d_ipo > d_end:
            continue
        if s["out"]:
            d_out = _parse_date(s["out"])
            if d_out and d_out < d_start:
                continue
        out.append(s)
    return out


def select_stocks(stocks: list[dict[str, Any]], args) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    by_code = {s["code"]: s for s in stocks}
    if args.codes:
        wanted = [c.strip() for c in args.codes.split(",") if c.strip()]
        selected, missing = [], []
        for code in wanted:
            if code in by_code:
                selected.append(by_code[code])
            else:
                missing.append(code)
        return selected, {"method": "explicit_codes", "codes": wanted,
                          "missing": missing}

    if not args.limit:
        return list(stocks), {"method": "full", "count": len(stocks)}

    limit = max(1, args.limit)
    n_delisted = args.delisted_sample
    if n_delisted is None:
        n_delisted = min(5, limit) if limit >= 10 else 0

    chosen_delisted: list[dict[str, Any]] = []
    for code in DEFAULT_DELISTED_SAMPLE:
        if len(chosen_delisted) >= n_delisted:
            break
        if code in by_code and by_code[code]["out"]:
            chosen_delisted.append(by_code[code])
    if len(chosen_delisted) < n_delisted:
        chosen_codes = {s["code"] for s in chosen_delisted}
        fallback = sorted(
            (s for s in stocks if s["out"] and s["code"] not in chosen_codes),
            key=lambda s: (s["out"], s["code"]), reverse=True)
        chosen_delisted.extend(fallback[: n_delisted - len(chosen_delisted)])

    delisted_codes = {s["code"] for s in chosen_delisted}
    n_active = max(0, limit - len(chosen_delisted))
    active = [s for s in stocks if not s["out"] and s["code"] not in delisted_codes]
    selected = active[:n_active] + chosen_delisted
    return selected, {"method": "limit", "limit": limit,
                      "delisted_sample": [s["code"] for s in chosen_delisted],
                      "active_count": len(active[:n_active]),
                      "delisted_count": len(chosen_delisted)}


def cmd_fetch(args) -> int:
    _assert_single_thread()
    os.makedirs(CSV_DIR, exist_ok=True)
    try:
        import baostock as bs
    except ImportError as exc:
        print(f"缺少 baostock：{exc}（pip install -r requirements-research.txt）",
              file=sys.stderr)
        return 2

    started = time.time()
    began_at = datetime.now().isoformat(timespec="seconds")
    log(f"==== fetch 开始 start={args.start} end={args.end} "
        f"limit={args.limit} adjust={args.adjustflag} ====")

    manifest = load_manifest()
    if manifest.get("config_hash") != config_hash(args.start, args.end,
                                                 args.adjustflag, args.max_rows):
        if manifest.get("entries"):
            log("[WARN] 取数配置与既有 manifest 不同，忽略旧断点（旧 CSV 仍留盘）")
        manifest = empty_manifest(args.start, args.end, args.adjustflag, args.max_rows)
    manifest["run"] = {"started_at": began_at, "pid": os.getpid(),
                       "argv": sys.argv[1:]}

    _install_socket_default_timeout(REQUEST_TIMEOUT_S)
    bs_login(bs)
    try:
        all_stocks = query_stock_basic(bs)
        stocks = _window_stocks(all_stocks, args.start, args.end)
        selected, selection = select_stocks(stocks, args)
        log(f"主表 type=1 共 {len(all_stocks)} 只；窗口内曾上市 "
            f"{len(stocks)} 只；本次选中 {len(selected)} 只（{selection['method']}）")
        if not selected:
            log("[ERR] 选中票数为 0")
            return 2

        manifest["universe"] = {
            "snapshot_date": date.today().isoformat(),
            "source": "baostock.query_stock_basic()（type=1）",
            "window": {"start": args.start, "end": args.end},
            "type1_total": len(all_stocks),
            "in_window": len(stocks),
            "active_in_window": sum(1 for s in stocks if not s["out"]),
            "delisted_in_window": sum(1 for s in stocks if s["out"]),
        }
        manifest["selection"] = selection
        write_json(UNIVERSE_PATH, {
            "snapshot_date": date.today().isoformat(),
            "source": "baostock.query_stock_basic()（type=1）",
            "window": {"start": args.start, "end": args.end},
            "counts": manifest["universe"],
            "selection": selection,
            "selected_codes": [s["code"] for s in selected],
            "stocks": stocks,
        })

        entries = manifest.setdefault("entries", {})
        failures = manifest.setdefault("failures", [])
        skipped = adopted = ok = failed = 0
        selected_codes = {s["code"] for s in selected}

        # 只保留本次选中的失败记录，避免多次 --limit 演练互相污染。
        manifest["failures"] = [f for f in failures if f.get("code") in selected_codes]
        failures = manifest["failures"]

        last_progress_ts = time.time()

        def _tick(where: str = "") -> None:
            _watchdog_check(last_progress_ts, args.watchdog_seconds, where)

        try:
            for i, stock in enumerate(selected, 1):
                code = stock["code"]
                _tick(f"开始处理 {code}")
                if not args.force and entry_is_done(manifest, code):
                    skipped += 1
                    last_progress_ts = time.time()
                    if i % 25 == 0 or i == len(selected):
                        log(f"  进度 {i}/{len(selected)}（跳过已完成 {skipped}，"
                            f"新取 {ok}，失败 {failed}）")
                    continue

                req_start = max(_parse_date(args.start), _parse_date(stock["ipo"]) or _parse_date(args.start))
                req_end = _parse_date(args.end)
                if stock["out"]:
                    d_out = _parse_date(stock["out"])
                    if d_out and d_out < req_end:
                        req_end = d_out
                req_start_s, req_end_s = req_start.isoformat(), req_end.isoformat()
                if req_start > req_end:
                    entries[code] = {"status": "skipped_out_of_window",
                                     "ipo": stock["ipo"], "out_date": stock["out"]}
                    last_progress_ts = time.time()
                    continue

                # 磁盘上已有合规 CSV、但 manifest 没记住（挂死时的孤儿文件）→ 接管，不重抓。
                if not args.force:
                    try:
                        adopted_entry = _adopt_existing_csv(
                            stock, req_start_s, req_end_s, args.adjustflag, args.max_rows)
                    except Exception as exc:  # noqa: BLE001
                        log(f"  [warn] {code} 接管既有 CSV 失败，改走重新抓取："
                            f"{type(exc).__name__}: {exc}")
                        adopted_entry = None
                    if adopted_entry is not None:
                        entries[code] = adopted_entry
                        adopted += 1
                        last_progress_ts = time.time()
                        if i % 25 == 0 or i == len(selected):
                            save_manifest(manifest)
                            log(f"  进度 {i}/{len(selected)}（跳过 {skipped}，"
                                f"接管 {adopted}，成功 {ok}，失败 {failed}，"
                                f"请求 {_FETCH_STATE['requests']}）")
                        if args.pause:
                            time.sleep(args.pause)
                        continue

                try:
                    fields, rows, warnings, info = _fetch_with_retry(
                        bs, stock, req_start_s, req_end_s, args.adjustflag,
                        args.max_rows,
                        on_attempt=lambda c=code: _tick(f"重试 {c}"))
                    n_rows, n_bytes, digest = save_series(code, fields, rows)
                    exit_meta = delisted_meta(fields, rows, stock["out"] or None)
                    entry = {
                        "status": "ok",
                        "name": stock["name"],
                        "ipo_date": stock["ipo"],
                        "out_date": stock["out"] or None,
                        "request_start": req_start_s,
                        "request_end": req_end_s,
                        "adjustflag": args.adjustflag,
                        "rows": n_rows,
                        "first": rows[0][fields.index("date")],
                        "last": rows[-1][fields.index("date")],
                        "csv_bytes": n_bytes,
                        "csv_md5": digest,
                        "fetch_seconds": info.get("seconds"),
                        "fetched_at": datetime.now().isoformat(timespec="seconds"),
                        "warnings": warnings,
                        "delisted": exit_meta,
                    }
                    entries[code] = entry
                    failures[:] = [f for f in failures if f.get("code") != code]
                    ok += 1
                    if warnings:
                        log(f"  [warn] {code}: {'; '.join(warnings)}")
                except WatchdogFired:
                    raise
                except SessionError as exc:
                    log(f"[FATAL] {code}: {exc}；保存断点后以退出码 3 退出")
                    manifest["run"]["session_error_at"] = datetime.now().isoformat(
                        timespec="seconds")
                    manifest["run"]["session_error"] = str(exc)
                    _finalize_stats(manifest, started, selected, skipped, ok,
                                    failed, adopted)
                    save_manifest(manifest)
                    return 3
                except Exception as exc:  # noqa: BLE001
                    failed += 1
                    entries[code] = {
                        "status": "failed",
                        "name": stock["name"],
                        "ipo_date": stock["ipo"],
                        "out_date": stock["out"] or None,
                        "request_start": req_start_s,
                        "request_end": req_end_s,
                        "error": f"{type(exc).__name__}: {exc}",
                        "failed_at": datetime.now().isoformat(timespec="seconds"),
                    }
                    failures.append({"code": code, "name": stock["name"],
                                     "error": f"{type(exc).__name__}: {exc}"})
                    log(f"[FAIL] {code} {stock['name']}: {type(exc).__name__}: {exc}")

                last_progress_ts = time.time()
                if i % 25 == 0 or i == len(selected):
                    save_manifest(manifest)
                    log(f"  进度 {i}/{len(selected)}（跳过 {skipped}，接管 {adopted}，"
                        f"成功 {ok}，失败 {failed}，请求 {_FETCH_STATE['requests']}）")
                if args.pause:
                    time.sleep(args.pause)
        except WatchdogFired as exc:
            log(f"[WATCHDOG] {exc}；保存断点后以退出码 4 退出（重跑同一命令即可续）")
            manifest["run"]["watchdog_at"] = datetime.now().isoformat(timespec="seconds")
            manifest["run"]["watchdog_reason"] = str(exc)
            _finalize_stats(manifest, started, selected, skipped, ok, failed, adopted)
            save_manifest(manifest)
            return 4
        except KeyboardInterrupt:
            log("[INTERRUPT] 收到 Ctrl-C，保存断点后退出")
            manifest["run"]["interrupted_at"] = datetime.now().isoformat(timespec="seconds")
            _finalize_stats(manifest, started, selected, skipped, ok, failed, adopted)
            save_manifest(manifest)
            return 130

        _finalize_stats(manifest, started, selected, skipped, ok, failed, adopted)
        save_manifest(manifest)
        wall = time.time() - started
        manifest["run"]["finished_at"] = datetime.now().isoformat(timespec="seconds")
        log(f"==== fetch 结束：选中 {len(selected)}，跳过 {skipped}，接管 {adopted}，"
            f"成功 {ok}，失败 {failed}，请求 {_FETCH_STATE['requests']}，"
            f"墙钟 {wall / 60:.1f} 分钟 ====")
        if failures:
            log("失败票：")
            for f in failures:
                log(f"  - {f['code']} {f.get('name', '')}: {f['error']}")
        return 0 if (not failures or not args.strict) else 1
    finally:
        # 退出路径也必须用带 30s 超时的 logout：坏 socket 上裸 bs.logout()
        # 会在 recv() 永久阻塞（见 §0.1 的事故路径）。
        bs_logout(bs)


def _finalize_stats(manifest: dict[str, Any], started: float,
                    selected: list[dict[str, Any]], skipped: int, ok: int,
                    failed: int, adopted: int = 0) -> None:
    entries = manifest.get("entries", {})
    ok_entries = [e for e in entries.values() if e.get("status") == "ok"]
    durations = [e.get("fetch_seconds") for e in ok_entries
                 if isinstance(e.get("fetch_seconds"), (int, float))]
    manifest["stats"] = {
        "selected": len(selected),
        "skipped_resume": skipped,
        "adopted_existing": adopted,
        "fetched_ok": ok,
        "failed": failed,
        "entries_ok_total": len(ok_entries),
        "requests": _FETCH_STATE["requests"],
        "request_seconds_total": round(_FETCH_STATE["request_seconds"], 1),
        "request_seconds_mean": round(sum(durations) / len(durations), 2) if durations else None,
        "wall_seconds": round(time.time() - started, 1),
        "rows_total": sum(int(e.get("rows") or 0) for e in ok_entries),
        "bytes_total": sum(int(e.get("csv_bytes") or 0) for e in ok_entries),
        "failed_codes": [f.get("code") for f in manifest.get("failures", [])],
    }


# ---------------------------------------------------------------------------
# validate
# ---------------------------------------------------------------------------
def cmd_validate(args) -> int:
    if not os.path.exists(UNIVERSE_PATH) or not os.path.exists(MANIFEST_PATH):
        print("缺少 evidence/universe/{universe,manifest}.json；先跑 fetch",
              file=sys.stderr)
        return 2
    universe = read_json(UNIVERSE_PATH)
    manifest = read_json(MANIFEST_PATH)
    selected = universe.get("selection", {})
    selected_codes = universe.get("selected_codes") or []
    entries = manifest.get("entries", {})
    if not selected_codes:
        selected_codes = sorted(entries)

    stock_by_code = {s["code"]: s for s in universe.get("stocks", [])}
    unregistered: list[str] = []
    if args.existing_only:
        disk_codes = sorted(os.path.basename(p)[:-4]
                            for p in glob.glob(os.path.join(CSV_DIR, "*.csv")))
        ok_codes = [c for c in selected_codes
                    if entries.get(c, {}).get("status") == "ok"]
        codes_to_check = sorted(set(disk_codes) | set(ok_codes))
    else:
        codes_to_check = selected_codes

    errors: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    missing: list[str] = []
    checked = 0
    rows_total = 0
    rows_per_code: list[int] = []
    first_dates: list[str] = []
    last_dates: list[str] = []
    year_dates: dict[str, set[str]] = {}
    delisted_checked = 0
    exit_reason_counts: dict[str, int] = {}

    for code in codes_to_check:
        entry = entries.get(code)
        path = csv_path(code)
        if not entry:
            if args.existing_only and os.path.exists(path):
                # 磁盘上有 CSV 但 manifest 没有记录（挂死时 last-write 之前落盘的孤儿）
                unregistered.append(code)
                stock = stock_by_code.get(code, {})
                window = universe.get("window") or {}
                req_start = max(_parse_date(window.get("start") or DEFAULT_START),
                                _parse_date(stock.get("ipo")) or _parse_date(DEFAULT_START))
                req_end = _parse_date(window.get("end") or DEFAULT_END)
                d_out = _parse_date(stock.get("out"))
                if d_out and d_out < req_end:
                    req_end = d_out
                entry = {"status": "ok", "unregistered": True,
                         "request_start": req_start.isoformat(),
                         "request_end": req_end.isoformat(),
                         "out_date": stock.get("out") or None,
                         "rows": None, "first": None, "last": None}
            else:
                missing.append(code)
                continue
        if entry.get("status") != "ok":
            if entry.get("status") == "failed":
                errors.append({"code": code, "kind": "fetch_failed",
                               "detail": entry.get("error")})
            continue
        if not os.path.exists(path):
            errors.append({"code": code, "kind": "csv_missing", "detail": path})
            continue
        try:
            frame = pd.read_csv(path, dtype={"date": str, "code": str})
        except Exception as exc:  # noqa: BLE001
            errors.append({"code": code, "kind": "csv_unreadable",
                           "detail": f"{type(exc).__name__}: {exc}"})
            continue
        checked += 1
        n = len(frame)
        rows_total += n
        rows_per_code.append(n)
        if n == 0:
            errors.append({"code": code, "kind": "empty_csv"})
            continue
        dates = frame["date"].astype(str).tolist()
        first_dates.append(dates[0])
        last_dates.append(dates[-1])
        for d in dates:
            year_dates.setdefault(d[:4], set()).add(d)
        if len(set(dates)) != len(dates):
            errors.append({"code": code, "kind": "duplicate_dates"})
        if dates != sorted(dates):
            errors.append({"code": code, "kind": "dates_not_sorted"})
        if entry.get("rows") is not None and n != int(entry["rows"]):
            errors.append({"code": code, "kind": "row_count_mismatch",
                           "detail": f"csv={n} manifest={entry.get('rows')}"})
        if entry.get("first") is not None and (
                dates[0] != entry.get("first") or dates[-1] != entry.get("last")):
            errors.append({"code": code, "kind": "boundary_mismatch",
                           "detail": f"csv={dates[0]}..{dates[-1]} "
                                     f"manifest={entry.get('first')}..{entry.get('last')}"})
        if entry.get("csv_bytes") and os.path.getsize(path) != entry["csv_bytes"]:
            warnings.append({"code": code, "kind": "csv_bytes_changed",
                             "detail": f"{os.path.getsize(path)} != {entry['csv_bytes']}"})
        # 末根日期 / 退市退出价复算
        expected_end = min(_parse_date(entry["request_end"]),
                           _parse_date(entry.get("out_date")) or _parse_date(entry["request_end"]))
        gap = weekdays_between(_parse_date(dates[-1]) + timedelta(days=1), expected_end)
        if gap > GAP_HARD_WEEKDAYS:
            errors.append({"code": code, "kind": "last_date_gap",
                           "detail": f"{gap} weekdays"})
        elif gap > GAP_WARN_WEEKDAYS:
            warnings.append({"code": code, "kind": "last_date_gap",
                             "detail": f"{gap} weekdays"})
        if entry.get("out_date"):
            delisted_checked += 1
            fields = list(frame.columns)
            rows = frame.astype(object).where(pd.notna(frame), "").values.tolist()
            recomputed = delisted_meta(fields, rows, entry["out_date"])
            old = entry.get("delisted") or {}
            reason = (recomputed or {}).get("exit_reason")
            exit_reason_counts[reason] = exit_reason_counts.get(reason, 0) + 1
            if old:
                for key in ("exit_price", "last_tradestatus1_date", "exit_reason"):
                    if (recomputed or {}).get(key) != old.get(key):
                        errors.append({"code": code, "kind": "delisted_mismatch",
                                       "detail": f"{key}: csv={recomputed.get(key) if recomputed else None} "
                                                 f"manifest={old.get(key)}"})

    # 覆盖率（对照 universe.json 的窗口内清单）
    universe_codes = {s["code"] for s in universe.get("stocks", [])}
    selected_set = set(selected_codes)
    not_selected = sorted(universe_codes - selected_set)
    missing_rows = [{"code": c, "kind": "no_manifest_entry"} for c in missing]
    errors.extend(missing_rows)

    def _pct(values: list[int], q: float) -> int:
        if not values:
            return 0
        ordered = sorted(values)
        return ordered[min(len(ordered) - 1, int(q * (len(ordered) - 1)))]

    report = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "manifest_updated_at": manifest.get("updated_at"),
        "selection": selected,
        "checked_codes": checked,
        "codes_considered": len(codes_to_check),
        "selected_codes": len(selected_codes),
        "unregistered_csv": unregistered,
        "universe_in_window": len(universe_codes),
        "not_selected_count": len(not_selected),
        "rows_total": rows_total,
        "rows_per_code": {
            "min": min(rows_per_code) if rows_per_code else 0,
            "p10": _pct(rows_per_code, 0.10),
            "median": _pct(rows_per_code, 0.50),
            "p90": _pct(rows_per_code, 0.90),
            "max": max(rows_per_code) if rows_per_code else 0,
        },
        "date_coverage": {
            "first": min(first_dates) if first_dates else None,
            "last": max(last_dates) if last_dates else None,
            "window": universe.get("window"),
            "distinct_dates_by_year": {y: len(v) for y, v in sorted(year_dates.items())},
        },
        "delisted_checked": delisted_checked,
        "exit_reason_counts": exit_reason_counts,
        "errors": errors,
        "warnings": warnings,
    }

    if args.reconcile:
        report["reconcile"] = _reconcile(selected_codes, entries, args.reconcile)
        report["errors"].extend(report["reconcile"]["errors"])

    write_json(args.report or REPORT_PATH, report)

    print(f"校验票数 {checked}/{len(codes_to_check)}（选中 {len(selected_codes)}）；"
          f"窗口内全池 {len(universe_codes)}，未选中 {len(not_selected)}")
    if unregistered:
        print(f"磁盘有 CSV 但 manifest 无记录：{len(unregistered)} 只"
              f"（{' '.join(unregistered[:10])}{' …' if len(unregistered) > 10 else ''}）")
    print(f"行数合计 {rows_total:,}；每票行数 min={report['rows_per_code']['min']} "
          f"p10={report['rows_per_code']['p10']} median={report['rows_per_code']['median']} "
          f"p90={report['rows_per_code']['p90']} max={report['rows_per_code']['max']}")
    print(f"日期覆盖 {report['date_coverage']['first']} .. "
          f"{report['date_coverage']['last']}")
    print(f"退市票 {delisted_checked} 只，exit_reason={exit_reason_counts}")
    if "reconcile" in report:
        rec = report["reconcile"]
        print(f"抽样对账 {rec['checked']} 票：一致 {rec['matched']}，"
              f"不一致 {len(rec['errors'])}")
        for item in rec["errors"]:
            print(f"  [RECONCILE-MISMATCH] {item['code']}: {item['detail']}")
    print(f"错误 {len(report['errors'])}，警告 {len(report['warnings'])}；报告 {args.report or REPORT_PATH}")
    for item in report["errors"][:50]:
        print(f"  [ERROR] {item['code']} {item['kind']}: {item.get('detail', '')}")
    for item in report["warnings"][:20]:
        print(f"  [WARN] {item['code']} {item['kind']}: {item.get('detail', '')}")
    return 0 if not report["errors"] else 1


def _reconcile(selected_codes: list[str], entries: dict[str, Any],
               n: int) -> dict[str, Any]:
    """串行重查 N 票，与已落盘 CSV 逐值对账（date/close）。"""
    try:
        import baostock as bs
    except ImportError as exc:
        return {"checked": 0, "matched": 0,
                "errors": [{"code": "-", "detail": f"baostock 不可用：{exc}"}]}

    candidates = [c for c in selected_codes
                  if entries.get(c, {}).get("status") == "ok"]
    if not candidates:
        return {"checked": 0, "matched": 0, "errors": []}
    # 确定性抽样：均匀取点；退市票优先补一个。
    step = max(1, len(candidates) // n)
    picks = candidates[::step][:n]
    delisted = [c for c in candidates if entries[c].get("out_date")]
    if delisted and not any(c in delisted for c in picks):
        picks[-1] = delisted[len(delisted) // 2]

    matched = 0
    mismatches: list[dict[str, Any]] = []
    _install_socket_default_timeout(REQUEST_TIMEOUT_S)
    bs_login(bs)
    try:
        for code in picks:
            entry = entries[code]
            _FETCH_STATE["requests"] += 1
            try:
                fields, rows, _seconds = _fetch_once(
                    bs, code, entry["request_start"], entry["request_end"],
                    entry.get("adjustflag", ADJUST), MAX_ROWS * 2)
            except Exception as exc:  # noqa: BLE001
                mismatches.append({"code": code,
                                   "detail": f"重查失败 {type(exc).__name__}: {exc}"})
                log(f"  [reconcile] {code} 重查失败：{exc}")
                continue
            frame = pd.DataFrame(rows, columns=fields)
            frame = frame.drop_duplicates(subset=["date"], keep="last").sort_values("date")
            local = pd.read_csv(csv_path(code), dtype={"date": str, "code": str})
            if len(frame) != len(local):
                mismatches.append({"code": code,
                                   "detail": f"行数 {len(frame)} != {len(local)}"})
                continue
            if frame["date"].tolist() != local["date"].astype(str).tolist():
                mismatches.append({"code": code, "detail": "日期序列不同"})
                continue
            a = pd.to_numeric(frame["close"], errors="coerce").to_numpy(dtype=float)
            b = pd.to_numeric(local["close"], errors="coerce").to_numpy(dtype=float)
            bad = ~((pd.isna(a) & pd.isna(b)) | (abs(a - b) < 1e-6))
            if bad.any():
                i = int(bad.argmax())
                mismatches.append({"code": code,
                                   "detail": f"close 不一致 @ {frame['date'].iloc[i]}: "
                                             f"remote={a[i]} local={b[i]}"})
                continue
            matched += 1
            log(f"  [reconcile] {code} OK rows={len(frame)} "
                f"{frame['date'].iloc[0]}..{frame['date'].iloc[-1]}")
    finally:
        bs_logout(bs)

    return {"checked": len(picks), "matched": matched,
            "codes": picks, "errors": mismatches}


# ---------------------------------------------------------------------------
# selftest（纯合成数据，不联网）
# ---------------------------------------------------------------------------
def _synthetic_rows(dates: list[str], *, statuses: list[str] | None = None,
                    closes: list[float] | None = None, code: str = "sh.600000") -> tuple[list[str], list[list[str]]]:
    fields = FIELDS.split(",")
    rows = []
    n = len(dates)
    for i, d in enumerate(dates):
        status = statuses[i] if statuses else "1"
        close = closes[i] if closes else 10.0 + i
        rows.append([d, code, str(close), str(close), str(close), str(close),
                     "1000", "10000", status, "0.0", "0"])
    return fields, rows


def _business_days(start: str, end: str, limit: int | None = None) -> list[str]:
    out = []
    cur, last = date.fromisoformat(start), date.fromisoformat(end)
    while cur <= last:
        if cur.weekday() < 5:
            out.append(cur.isoformat())
        cur += timedelta(days=1)
    return out[:limit] if limit else out


def cmd_selftest(args) -> int:  # noqa: ARG001
    checks: list[tuple[str, bool, str]] = []

    def check(name: str, cond: bool, detail: str = "") -> None:
        checks.append((name, bool(cond), detail))

    dates = _business_days("2024-01-01", "2024-12-31")
    fields, rows = _synthetic_rows(dates)

    # 1. 完整序列通过
    errs, warns, info = validate_series(
        fields, rows, code="sh.600000", req_start="2024-01-01",
        req_end="2024-12-31", ipo_date="2000-01-01", out_date=None)
    check("complete_series_ok", not errs and not warns, f"errs={errs} warns={warns} "
                                                       f"info={info}")
    check("complete_series_rows", info["rows"] == len(dates), str(info["rows"]))

    # 2. 疑似截断：正好 2000 行 + 末根日期倒退数月
    long_dates = _business_days("2010-01-01", "2024-12-31")
    long_fields, long_rows = _synthetic_rows(long_dates)
    trunc_dates = long_dates[:2000]
    trunc_rows = long_rows[:len(trunc_dates)]
    errs, _warns, _info = validate_series(
        long_fields, trunc_rows, code="sh.600000", req_start="2010-01-01",
        req_end="2024-12-31", ipo_date="2000-01-01", out_date=None)
    check("truncation_detected",
          any("last_date_gap" in e for e in errs)
          and any("exactly_2000" in e for e in errs), str(errs))

    # 2000 行但日期不缺口（人为构造行数哨兵）
    rows_2000 = [list(r) for r in long_rows[:2000]]
    errs, _warns, _info = validate_series(
        long_fields, rows_2000, code="sh.600000", req_start="2010-01-01",
        req_end=long_dates[1999], ipo_date="2000-01-01", out_date=None)
    check("exactly_2000_rows_flagged",
          any("exactly_2000" in e for e in errs), str(errs))

    # 3. 重复日期 / 乱序 / 越界
    dup = [rows[0], rows[1], rows[1]] + rows[2:]
    errs, _w, _i = validate_series(fields, dup, code="sh.600000",
                                   req_start="2024-01-01", req_end="2024-12-31",
                                   ipo_date=None, out_date=None)
    check("duplicate_dates_detected", "duplicate_dates" in errs, str(errs))

    shuffled = [rows[1], rows[0]] + rows[2:]
    errs, _w, _i = validate_series(fields, shuffled, code="sh.600000",
                                   req_start="2024-01-01", req_end="2024-12-31",
                                   ipo_date=None, out_date=None)
    check("unsorted_detected", "dates_not_sorted" in errs, str(errs))

    out_of_range = rows + [["2025-01-02", "sh.600000", "1", "1", "1", "1",
                            "1", "1", "1", "0", "0"]]
    errs, _w, _i = validate_series(fields, out_of_range, code="sh.600000",
                                   req_start="2024-01-01", req_end="2024-12-31",
                                   ipo_date=None, out_date=None)
    check("after_request_end_detected",
          any("after_request_end" in e for e in errs), str(errs))

    # 4. 空序列
    errs, _w, _i = validate_series(fields, [], code="sh.600000",
                                   req_start="2024-01-01", req_end="2024-12-31",
                                   ipo_date=None, out_date=None)
    check("empty_series_detected", errs == ["empty_series"], str(errs))

    # 5. 停牌长缺口 >5bd 但 <=60bd → warning
    gap_dates = _business_days("2024-01-01", "2024-06-28")
    _f, gap_rows = _synthetic_rows(gap_dates)
    errs, warns, _i = validate_series(_f, gap_rows, code="sh.600000",
                                      req_start="2024-01-01", req_end="2024-07-10",
                                      ipo_date=None, out_date=None)
    check("gap_warning_not_error",
          not errs and any("last_date_gap" in w for w in warns),
          f"errs={errs} warns={warns}")

    # 6. 退市：末根在 outDate 前一日（R8 新式形态）
    del_dates = _business_days("2024-01-01", "2024-06-27")
    _f, del_rows = _synthetic_rows(
        del_dates, closes=[10.0 - 0.05 * i for i in range(len(del_dates))],
        code="sz.300379")
    errs, _w, info = validate_series(_f, del_rows, code="sz.300379",
                                     req_start="2024-01-01", req_end="2024-06-28",
                                     ipo_date="2020-01-01", out_date="2024-06-28")
    check("delisted_last_row_before_outdate_ok", not errs, str(errs))
    meta = delisted_meta(_f, del_rows, "2024-06-28")
    check("delisted_price_reason", meta["exit_reason"] == "price",
          str(meta))
    check("delisted_exit_price_is_last_ts1",
          abs(meta["exit_price"] - (10.0 - 0.05 * (len(del_dates) - 1))) < 1e-9,
          str(meta["exit_price"]))

    # 7. 退市：末行 tradestatus=0（老票形态），退出价取最后 tradestatus==1
    statuses = ["1"] * (len(del_dates) - 2) + ["0", "0"]
    closes = [10.0 + i for i in range(len(del_dates))]
    _f, old_rows = _synthetic_rows(del_dates, statuses=statuses, closes=closes,
                                   code="sh.600005")
    meta = delisted_meta(_f, old_rows, "2024-06-28")
    check("delisted_old_style_exit_price",
          meta["last_tradestatus1_date"] == del_dates[-3]
          and abs(meta["exit_price"] - closes[-3]) < 1e-9
          and meta["freeze_calendar_days"] is not None,
          str(meta))
    check("delisted_known_merger",
          meta["exit_reason"] == "merger"
          and meta["exit_reason_basis"] == "manual_exception_table",
          str(meta))

    # 8. 平收退市 → 疑似换股
    flat_closes = [10.0] * len(del_dates)
    _f, flat_rows = _synthetic_rows(del_dates, closes=flat_closes, code="sz.000024")
    meta = delisted_meta(_f, flat_rows, "2024-06-28")
    check("delisted_flat_suspect_merger",
          meta["exit_reason"] == "merger" and meta["exit_reason_suspect"],
          str(meta))

    # 9. 崩盘退市 → price
    crash_closes = [10.0] * (len(del_dates) - 1) + [0.5]
    _f, crash_rows = _synthetic_rows(del_dates, closes=crash_closes, code="sz.000939")
    meta = delisted_meta(_f, crash_rows, "2024-06-28")
    check("delisted_crash_price", meta["exit_reason"] == "price", str(meta))

    # 10. 无可交易行 → unknown
    _f, dead_rows = _synthetic_rows(del_dates, statuses=["0"] * len(del_dates))
    meta = delisted_meta(_f, dead_rows, "2024-06-28")
    check("delisted_no_ts1_unknown",
          meta["exit_reason"] == "unknown" and meta["exit_price"] is None, str(meta))

    # 11. 行数不可能超过区间工作日
    too_many = rows + rows
    errs, _w, _i = validate_series(fields, too_many, code="sh.600000",
                                   req_start="2024-01-01", req_end="2024-12-31",
                                   ipo_date=None, out_date=None)
    check("too_many_rows_detected",
          any("duplicate_dates" in e or "weekdays" in e for e in errs), str(errs))

    # 12. 配置哈希稳定
    check("config_hash_stable",
          config_hash("2018-01-01", "2026-11-12", "2", MAX_ROWS)
          == config_hash("2018-01-01", "2026-11-12", "2", MAX_ROWS))

    # 13. 单请求硬超时真的能打断阻塞调用（SIGALRM）
    if hasattr(signal, "SIGALRM"):
        began = time.time()
        timed_out = False
        try:
            with _deadline(0.3):
                time.sleep(5)
        except RequestTimeout:
            timed_out = True
        elapsed = time.time() - began
        check("deadline_fires", timed_out and elapsed < 2.0,
              f"timed_out={timed_out} elapsed={elapsed:.2f}s")
    else:
        check("deadline_fires", True, "无 SIGALRM，跳过")

    # 14. 进度看门狗
    watchdog_raised = ""
    try:
        _watchdog_check(time.time() - 700, 600, "selftest")
    except WatchdogFired as exc:
        watchdog_raised = str(exc)
    quiet_ok = True
    try:
        _watchdog_check(time.time(), 600)
        _watchdog_check(time.time() - 10 ** 6, 0)  # 0 = 关闭
    except WatchdogFired:
        quiet_ok = False
    check("watchdog_fires", bool(watchdog_raised), watchdog_raised)
    check("watchdog_quiet_and_disable", quiet_ok, "")

    # 15. 查询超时 → 强制 logout → 重新登录 → 重试 3 次后抛错（桩 bs，不联网）
    class _HangBS:
        def __init__(self):
            self.logins = 0
            self.logouts = 0

        def login(self):
            self.logins += 1
            return type("R", (), {"error_code": "0", "error_msg": ""})()

        def logout(self):
            self.logouts += 1

        def query_history_k_data_plus(self, *a, **k):  # noqa: ARG002
            time.sleep(5)

    hang = _HangBS()
    began = time.time()
    raised = ""
    try:
        _fetch_with_retry(hang, {"code": "sh.000001", "ipo": "2000-01-01", "out": ""},
                          "2024-01-01", "2024-12-31", "2", MAX_ROWS,
                          timeout_s=0.2, backoff=(0.0, 0.0, 0.0))
    except Exception as exc:  # noqa: BLE001
        raised = type(exc).__name__
    elapsed = time.time() - began
    check("ticket_timeout_relogin_retry",
          raised == "RuntimeError" and hang.logins == RETRY_ATTEMPTS
          and hang.logouts == RETRY_ATTEMPTS and elapsed < 3.0,
          f"raised={raised} logins={hang.logins} logouts={hang.logouts} "
          f"elapsed={elapsed:.2f}s")

    passed = sum(1 for _n, ok, _d in checks if ok)
    for name, ok, detail in checks:
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  ({detail})" if not ok else ""))
    print(f"selftest：{passed}/{len(checks)} passed")
    return 0 if passed == len(checks) else 1


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="R9 / issue #83：无偏日线池（沪深全 A + 退市，串行）")
    sub = parser.add_subparsers(dest="command", required=True)

    fetch = sub.add_parser("fetch", help="联网串行取数（断点续跑）")
    fetch.add_argument("--start", default=DEFAULT_START)
    fetch.add_argument("--end", default=DEFAULT_END)
    fetch.add_argument("--limit", type=int, default=None,
                       help="只取前 N 只（含默认退市样本）；缺省 = 窗口内全池")
    fetch.add_argument("--codes", default=None,
                       help="显式代码列表，逗号分隔（如 sh.600519,sz.000002）")
    fetch.add_argument("--delisted-sample", type=int, default=None,
                       help="--limit 时包含的退市票数（默认 5；0 = 不含）")
    fetch.add_argument("--adjustflag", default=ADJUST, choices=("1", "2", "3"),
                       help="复权：2=前复权（默认，研究用）")
    fetch.add_argument("--max-rows", type=int, default=MAX_ROWS)
    fetch.add_argument("--force", action="store_true", help="忽略断点全部重取")
    fetch.add_argument("--pause", type=float, default=0.0,
                       help="每票之间额外休眠秒数（默认 0）")
    fetch.add_argument("--watchdog-seconds", type=float, default=WATCHDOG_SECONDS,
                       help="超过 N 秒没有任何票完成就保存断点并非 0 退出（0=关闭）")
    fetch.add_argument("--strict", action="store_true",
                       help="有任何失败票时返回非零退出码")
    fetch.set_defaults(func=cmd_fetch)

    validate = sub.add_parser("validate", help="离线复核（可抽样联网对账）")
    validate.add_argument("--reconcile", type=int, default=0,
                          help="联网抽 N 票与 baostock 直查对账（默认 0）")
    validate.add_argument("--existing-only", action="store_true",
                          help="只校验磁盘上已存在的 CSV（续跑中复核用；不把未取票记为错误）")
    validate.add_argument("--report", default=REPORT_PATH)
    validate.set_defaults(func=cmd_validate)

    selftest = sub.add_parser("selftest", help="合成数据自检（不联网）")
    selftest.set_defaults(func=cmd_selftest)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    os.makedirs(OUT_DIR, exist_ok=True)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
