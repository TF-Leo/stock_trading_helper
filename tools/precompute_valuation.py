#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P1-1（issue #46）：估值历史**收盘后预计算**（baostock，研究可选依赖）。

为什么必须预计算
----------------
``baostock`` 是 TCP 长连接客户端，只作为**研究/预计算**的可选依赖
（``requirements-research.txt``），**不进主 ``requirements.txt``**。CLI / 9:15
盘前 cron 主路径只读缓存（``stock_analysis/data_loader/valuation.py``），
绝不 import baostock、绝不触网。本工具是 ``source/`` 之外唯一允许 import
``baostock`` 的入口，缺失时给出可操作的报错。

取数口径（上游 a-stock-data v3.10.4 §6.5）
------------------------------------------
``query_history_k_data_plus``，``frequency="d"``、``adjustflag="3"``（不复权）：

* ``peTTM`` / ``pbMRQ`` / ``psTTM`` / ``pcfNcfTTM``：日频估值；
* ``turn``：换手率（百分数，0.31 = 0.31%）；
* ``tradestatus``：``1`` 正常 / ``0`` 停牌；
* ``isST``：``1`` ST/*ST / ``0`` 正常（历史逐日标记）。

一次 ``bs.login()`` 会话内逐票串行查询；单票失败重试 ``--retries`` 次后
**继续下一票**，失败票**不覆盖**旧缓存。baostock 登录/登出/查询会直接 print
到 stdout，本工具在调用期间把 stdout/stderr 重定向到内存缓冲，保证
``--json`` 的 stdout 不被 ``login success`` 之类日志污染。

写入
----
``source/stock_analysis/data/valuation/<code>.json``（``data/`` 已 gitignore），
结构见 ``write_cache``。北交所（4/8/92/920 号段）baostock 不支持，**登录前**
就报错并继续下一票。

用法
----
    python tools/precompute_valuation.py                  # 默认 14 只自选
    python tools/precompute_valuation.py 600519 002415    # 指定票池
    python tools/precompute_valuation.py --codes 600519,002415 --json
    python tools/precompute_valuation.py --start 2016-01-04 --end 2026-11-12

退出码：0 = 全部成功；1 = 有票失败；2 = 参数 / 依赖 / 登录错误。
"""
from __future__ import annotations

import argparse
import contextlib
import importlib
import io
import json
import os
import sys
import time
from datetime import datetime


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "source")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

OUT_DIR = os.path.join(SRC, "stock_analysis", "data", "valuation")

# 与 tools/precompute_flow.py、tools/forward_log.py 的 14 只自选保持一致。
DEFAULT_CODES = [
    "000858", "002270", "002371", "002415", "002594", "300476", "300750",
    "600036", "600519", "600522", "601318", "601398", "603259", "688981",
]

DEFAULT_START = "2016-01-04"     # 上游 §6.5 实测最早可从 2016-01-04 取
DEFAULT_INTERVAL = 0.5           # 逐票串行；baostock 无东财式 IP 防封铁律
DEFAULT_RETRIES = 2
DEFAULT_RETRY_WAIT = 2.0
MIN_ROWS = 1                     # 新上市标的行数可以很少；空响应才算失败
MAX_ROWS = 0                     # 0 = 不截断，保留全历史供 3 年分位/后续研究

SOURCE = "baostock_valuation"
UPSTREAM = "a-stock-data v3.10.4 §6.5 baostock_valuation_history"
SCHEMA_VERSION = "1.0"

#: 上游 §6.5 字段，顺序即 baostock 返回列顺序。
FIELDS = ("date,code,close,peTTM,pbMRQ,psTTM,pcfNcfTTM,turn,"
          "tradestatus,isST")

#: 原始字段 → 缓存字段。
FIELD_MAP = {
    "close": "close",
    "peTTM": "pe_ttm",
    "pbMRQ": "pb",
    "psTTM": "ps",
    "pcfNcfTTM": "pcf",
    "turn": "turnover",
    "tradestatus": "trade_status",
    "isST": "is_st",
}


def normalize_code(code) -> str:
    """任意受支持写法 → 6 位纯数字。"""
    from stock_analysis.data_sources._astock_generated import norm_ticker
    return norm_ticker(str(code))


def bs_code(code) -> str:
    """6 位代码 → baostock 格式；北交所在登录前就拦掉（上游 §6.5）。"""
    code = str(code).zfill(6)
    if code[:2] in ("60", "68", "90"):
        return f"sh.{code}"
    if code[:2] in ("00", "30", "20"):
        return f"sz.{code}"
    raise ValueError(
        f"baostock 不支持该代码: {code}（北交所 4/8/92/920 号段会被服务端拒绝，"
        f"报 10004011 股票代码未标识sh或sz；基金/ETF 也不在 §6.5 覆盖范围）")


def import_baostock(import_fn=None):
    """延迟导入 baostock；缺失时给出可操作的报错（主 CLI 永不走到这里）。"""
    try:
        return (import_fn or importlib.import_module)("baostock")
    except Exception as exc:
        raise RuntimeError(
            "未安装 baostock。它是**研究/预计算**可选依赖，主 CLI 不需要：\n"
            "    pip install -r requirements-research.txt\n"
            f"（原始错误：{type(exc).__name__}: {exc}）") from exc


def make_fetch(bs):
    """构造 ``fetch(code, start, end) -> list[dict]``（原始字符串字段）。"""
    def fetch(code, start, end):
        rs = bs.query_history_k_data_plus(
            bs_code(code), FIELDS, start_date=start, end_date=end,
            frequency="d", adjustflag="3")     # 3=不复权，与腾讯 K 线口径一致
        error_code = str(getattr(rs, "error_code", "0"))
        if error_code != "0":
            raise RuntimeError(
                f"baostock 查询失败: {error_code} "
                f"{getattr(rs, 'error_msg', '')}")
        fields = list(getattr(rs, "fields", FIELDS.split(",")))
        rows = []
        while rs.next():
            rows.append(dict(zip(fields, rs.get_row_data())))
        return rows
    return fetch


@contextlib.contextmanager
def _silence():
    """把调用期间的 stdout/stderr 收进内存，防止 baostock 日志污染机读输出。"""
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        yield


def _float_or_none(value):
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _int_flag(value):
    if value is None or value == "":
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def validate_rows(raw):
    """校验并归一化上游返回；返回按日期升序的行列表，失败抛 ``ValueError``。

    空列表与「格式错误」一样视为失败（上游查询失败时也会返回空表），避免把
    空壳写进缓存。停牌 / ST 必须是显式 0/1，绝不猜测。
    """
    if not isinstance(raw, list) or not raw:
        raise ValueError("空响应（baostock 未返回任何行）")
    clean, seen = [], set()
    for row in raw:
        if not isinstance(row, dict):
            raise ValueError("行不是对象")
        day = str(row.get("date") or "")[:10]
        try:
            day = datetime.fromisoformat(day).date().isoformat()
        except Exception as exc:
            raise ValueError(f"行日期格式错误（{day!r}）：{exc}") from exc
        item = {"date": day}
        for src_key, dst_key in FIELD_MAP.items():
            if src_key not in row and dst_key not in row:
                raise ValueError(f"缺字段 {src_key}")
            value = row.get(src_key, row.get(dst_key))
            if dst_key in ("trade_status", "is_st"):
                flag = _int_flag(value)
                if flag is None:
                    raise ValueError(f"字段 {src_key} 必须是 0/1，实际 {value!r}")
                item[dst_key] = flag
            else:
                item[dst_key] = _float_or_none(value)
        if day in seen:
            continue
        seen.add(day)
        clean.append(item)
    clean.sort(key=lambda r: r["date"])
    if len(clean) < MIN_ROWS:
        raise ValueError(f"仅 {len(clean)} 行 < {MIN_ROWS}，不写缓存")
    return clean


def write_cache(code, rows, out_dir=None, attempts=None, fetched_at=None):
    """原子写入 ``<out_dir>/<code>.json``；返回落盘路径。"""
    out_dir = out_dir or OUT_DIR
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, f"{code}.json")
    payload = {
        "schema_version": SCHEMA_VERSION,
        "code": code,
        "source": SOURCE,
        "upstream": UPSTREAM,
        "fetched_at": fetched_at or datetime.now().isoformat(),
        "as_of": rows[-1]["date"],
        "pub_lag_days": 0,
        "attempts": dict(attempts or {}),
        "rows": rows if not MAX_ROWS else rows[-int(MAX_ROWS):],
    }
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=1, sort_keys=True)
        handle.write("\n")
    os.replace(tmp, path)
    return path


def _ordered_codes(codes):
    seen, ordered = set(), []
    for raw in codes:
        try:
            code = normalize_code(raw)
        except Exception as exc:
            ordered.append((str(raw), None, f"代码无法解析：{exc}"))
            continue
        if code not in seen:
            seen.add(code)
            ordered.append((str(raw), code, None))
    return ordered


def precompute(codes, out_dir=None, start=DEFAULT_START, end=None,
               interval=DEFAULT_INTERVAL, retries=DEFAULT_RETRIES,
               retry_wait=DEFAULT_RETRY_WAIT, fetch_fn=None, login_fn=None,
               logout_fn=None, sleep_fn=time.sleep, now_fn=datetime.now):
    """登录一次、逐票串行预计算；返回统计字典（不抛异常，失败逐票记录）。

    ``fetch_fn`` 为空时导入 baostock 并建立默认会话（缺失即抛
    ``RuntimeError``）；测试可注入 ``fetch_fn`` / ``login_fn`` / ``logout_fn``。
    """
    if interval < 0:
        raise ValueError("interval 不得为负")
    if retries < 0:
        raise ValueError("retries 不得为负")
    if fetch_fn is None:
        bs = import_baostock()
        fetch_fn = make_fetch(bs)
        login_fn = login_fn or bs.login
        logout_fn = logout_fn or bs.logout

    stats = {"out_dir": out_dir or OUT_DIR, "start": start, "end": end,
             "interval_seconds": float(interval), "retries": int(retries),
             "requests": 0, "ok": [], "failed": [], "login": "skipped",
             "login_error": None, "wall_seconds": 0.0}
    started = now_fn()
    if end is None:
        current = now_fn()
        end = current.date().isoformat() if hasattr(current, "date") else None
        stats["end"] = end

    logout = None
    if login_fn is not None:
        try:
            with _silence():
                login_result = login_fn()
            error_code = str(getattr(login_result, "error_code", "0"))
            if error_code != "0":
                raise RuntimeError(
                    f"baostock 登录失败: {error_code} "
                    f"{getattr(login_result, 'error_msg', '')}")
        except Exception as exc:
            stats["login"] = "failed"
            stats["login_error"] = f"{type(exc).__name__}: {exc}"
            stats["failed"] = [
                {"code": code if code else raw, "attempts": 0,
                 "reason": f"登录失败：{stats['login_error']}"}
                for raw, code, _ in _ordered_codes(codes)]
            stats["wall_seconds"] = round(
                now_fn().timestamp() - started.timestamp(), 2)
            return stats
        stats["login"] = "ok"
        logout = logout_fn

    last_request_at = [None]

    def _request(code):
        if last_request_at[0] is not None:
            due = last_request_at[0] + float(interval)
            wait = due - now_fn().timestamp()
            if wait > 0:
                sleep_fn(wait)
        last_request_at[0] = now_fn().timestamp()
        stats["requests"] += 1
        with _silence():
            return fetch_fn(code, start, end)

    try:
        for raw, code, parse_error in _ordered_codes(codes):
            if code is None:
                stats["failed"].append({"code": raw, "attempts": 0,
                                        "reason": parse_error})
                print(f"FAIL {raw}: {parse_error}", file=sys.stderr)
                continue
            attempts, rows, reason = 0, None, None
            for attempt in range(int(retries) + 1):
                attempts += 1
                try:
                    rows = validate_rows(_request(code))
                    reason = None
                    break
                except Exception as exc:
                    reason = f"{type(exc).__name__}: {exc}"
                    if attempt < int(retries):
                        sleep_fn(retry_wait)
            if rows is None:
                stats["failed"].append({"code": code,
                                        "attempts": {"baostock": attempts},
                                        "reason": reason})
                print(f"FAIL {code}: attempts={attempts} reason={reason}",
                      file=sys.stderr)
                continue
            try:
                path = write_cache(code, rows, out_dir=out_dir,
                                   attempts={"baostock": attempts})
            except Exception as exc:
                stats["failed"].append({"code": code,
                                        "attempts": {"baostock": attempts},
                                        "reason": f"写缓存失败: {exc}"})
                print(f"FAIL {code}: 写缓存失败 {exc}", file=sys.stderr)
                continue
            stats["ok"].append({"code": code, "rows": len(rows),
                                "as_of": rows[-1]["date"], "path": path,
                                "attempts": {"baostock": attempts}})
            print(f"OK   {code}: {len(rows)} 行 as_of={rows[-1]['date']} "
                  f"attempts={attempts} -> {path}")
    finally:
        if logout is not None:
            try:
                with _silence():
                    logout()
            except Exception:
                pass

    stats["wall_seconds"] = round(now_fn().timestamp() - started.timestamp(), 2)
    return stats


def build_parser():
    parser = argparse.ArgumentParser(
        description="估值历史预计算（baostock §6.5；写 "
                    "source/stock_analysis/data/valuation/）")
    parser.add_argument("codes", nargs="*", help="股票代码（默认 14 只自选）")
    parser.add_argument("--codes", dest="codes_csv", default=None,
                        help="逗号分隔的票池（与位置参数合并去重）")
    parser.add_argument("--out-dir", default=OUT_DIR, help=f"缓存目录（默认 {OUT_DIR}）")
    parser.add_argument("--start", default=DEFAULT_START,
                        help=f"起始日 YYYY-MM-DD（默认 {DEFAULT_START}）")
    parser.add_argument("--end", default=None, help="结束日 YYYY-MM-DD（默认今天）")
    parser.add_argument("--interval", type=float, default=DEFAULT_INTERVAL,
                        help="相邻查询最小间隔秒数（默认 0.5）")
    parser.add_argument("--retries", type=int, default=DEFAULT_RETRIES,
                        help="单票失败重试次数（默认 2）")
    parser.add_argument("--retry-wait", type=float, default=DEFAULT_RETRY_WAIT,
                        help="重试前的额外等待秒数（默认 2.0）")
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
        stats = precompute(codes, out_dir=args.out_dir, start=args.start,
                           end=args.end, interval=args.interval,
                           retries=args.retries, retry_wait=args.retry_wait)
    except (ValueError, RuntimeError) as exc:
        print(f"ERROR {exc}", file=sys.stderr)
        return 2

    print(f"requests={stats['requests']} ok={len(stats['ok'])} "
          f"failed={len(stats['failed'])} wall={stats['wall_seconds']}s "
          f"interval={stats['interval_seconds']}s login={stats['login']}")
    if stats["failed"]:
        for item in stats["failed"]:
            print(f"  - {item['code']}: {item['reason']}", file=sys.stderr)
    if stats["login"] == "failed":
        print(f"ERROR 登录失败：{stats['login_error']}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(stats, ensure_ascii=False, indent=2))
    return 0 if not stats["failed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
