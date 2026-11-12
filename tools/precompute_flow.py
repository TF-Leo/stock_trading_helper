#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P1-2（issue #36）：东财日级资金流**收盘后预计算**。

为什么必须预计算
----------------
东财 ``push2his`` 有 IP 级风控（上游「防封铁律」：串行、相邻请求 ≥1s）。
如果在 CLI / 9:15 盘前 cron 主路径里逐票同步拉取，既挤占盘前窗口，也容易把
出口 IP 打进黑名单。因此本工具只在**收盘后**运行一次，把结果落到

    source/stock_analysis/data/flow/<code>.json   （data/ 已 gitignore）

CLI 只读这份缓存（``stock_analysis/data_loader/flow.py``），**绝不触网**。

用法
----
    python tools/precompute_flow.py                     # 默认 14 只自选
    python tools/precompute_flow.py 600519 002415       # 指定票池
    python tools/precompute_flow.py --codes 600519,002415 --json
    python tools/precompute_flow.py --interval 1.2 --retries 3

行为
----
* 逐票串行；**相邻两次请求**（含重试）之间至少间隔 ``--interval`` 秒，
  默认 1.0s，且不允许调到 1.0 以下（防封铁律，不是可调参数）。
* 单票失败后重试 ``--retries`` 次（默认 2），仍失败则记录原因并继续下一票。
* 只有 ≥ ``MIN_ROWS``（20）行且字段完整的响应才写缓存；失败**不覆盖**旧缓存。
* 输出成功/失败票、总请求数、墙钟；``--json`` 同时给出机读统计。

退出码：0 = 全部成功；1 = 有票失败；2 = 参数/配置错误。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "source")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

OUT_DIR = os.path.join(SRC, "stock_analysis", "data", "flow")

# 与 tools/forward_log.py 的 14 只自选保持一致（同一批标的，便于交叉核对）。
DEFAULT_CODES = [
    "000858", "002270", "002371", "002415", "002594", "300476", "300750",
    "600036", "600519", "600522", "601318", "601398", "603259", "688981",
]

DEFAULT_INTERVAL = 1.0
DEFAULT_RETRIES = 2
DEFAULT_RETRY_WAIT = 2.0
MIN_ROWS = 20           # 与 data_loader.MIN_CACHE_ROWS 同口径
ROW_FIELDS = ("date", "main_net", "small_net", "mid_net", "large_net", "super_net")


def normalize_code(code) -> str:
    """任意受支持写法 → 6 位纯数字（东财 secid 用纯码拼，不能带 sh/sz 前缀）。"""
    from stock_analysis.data_sources._astock_generated import norm_ticker
    return norm_ticker(str(code))


def fetch_fund_flow(code: str):
    """东财 §4.5 个股日级资金流（120 个交易日）。异常由调用方处理。"""
    from stock_analysis.data_sources._astock_generated import stock_fund_flow_120d
    return stock_fund_flow_120d(code)


def validate_rows(rows):
    """校验并归一化上游返回；返回按日期升序的行列表，失败抛 ``ValueError``。

    上游对网络异常返回 ``[]``（并只打印一行 ``[WARN]``），因此空列表在这里
    与「格式错误」一样视为失败，避免把空壳写进缓存。
    """
    if not isinstance(rows, list) or not rows:
        raise ValueError("空响应（上游 §4.5 在请求失败时也返回空列表）")
    clean = []
    seen = set()
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("行不是对象")
        missing = [k for k in ROW_FIELDS if k not in row]
        if missing:
            raise ValueError(f"缺字段 {','.join(missing)}")
        day = str(row["date"])[:10]
        try:
            datetime.fromisoformat(day)
            item = {"date": day}
            for key in ROW_FIELDS[1:]:
                item[key] = float(row[key])
        except Exception as exc:
            raise ValueError(f"行格式错误（{day!r}）：{exc}") from exc
        if day in seen:
            continue
        seen.add(day)
        clean.append(item)
    clean.sort(key=lambda r: r["date"])
    if len(clean) < MIN_ROWS:
        raise ValueError(f"仅 {len(clean)} 行 < {MIN_ROWS}，不写缓存")
    return clean


def write_cache(code, rows, out_dir=None, limit=120):
    """原子写入 ``<out_dir>/<code>.json``；返回落盘路径。"""
    out_dir = out_dir or OUT_DIR
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, f"{code}.json")
    payload = {
        "schema_version": "1.0",
        "code": code,
        "source": "eastmoney_push2his",
        "upstream": "a-stock-data v3.10.4 §4.5 stock_fund_flow_120d",
        "fetched_at": datetime.now().isoformat(),
        "as_of": rows[-1]["date"],
        "rows": rows[-int(limit):],
    }
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=1, sort_keys=True)
        handle.write("\n")
    os.replace(tmp, path)
    return path


def precompute(codes, out_dir=None, interval=DEFAULT_INTERVAL,
               retries=DEFAULT_RETRIES, retry_wait=DEFAULT_RETRY_WAIT,
               fetch_fn=None, sleep_fn=time.sleep, now_fn=datetime.now):
    """逐票串行预计算；返回统计字典（不抛异常，失败逐票记录）。"""
    if interval < 1.0:
        raise ValueError("interval 不得小于 1.0 秒（东财防封铁律）")
    if retries < 0:
        raise ValueError("retries 不得为负")
    fetch_fn = fetch_fn or fetch_fund_flow

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

    stats = {"out_dir": out_dir or OUT_DIR, "interval_seconds": float(interval),
             "retries": int(retries), "requests": 0, "ok": [], "failed": []}
    started = now_fn()
    last_request_at = None

    for raw, code, parse_error in ordered:
        if code is None:
            stats["failed"].append({"code": raw, "attempts": 0,
                                    "reason": parse_error})
            continue
        attempts = 0
        reason = None
        rows = None
        for attempt in range(int(retries) + 1):
            attempts += 1
            # 相邻请求（含重试）之间强制间隔；第一次请求不等待。
            if last_request_at is not None:
                due = last_request_at + interval
                wait = due - now_fn().timestamp()
                if wait > 0:
                    sleep_fn(wait)
            last_request_at = now_fn().timestamp()
            stats["requests"] += 1
            try:
                rows = validate_rows(fetch_fn(code))
                reason = None
                break
            except Exception as exc:
                reason = f"{type(exc).__name__}: {exc}"
                if attempt < retries:
                    sleep_fn(retry_wait)
        if rows is None:
            stats["failed"].append({"code": code, "attempts": attempts,
                                    "reason": reason})
            print(f"FAIL {code}: attempts={attempts} reason={reason}", file=sys.stderr)
            continue
        try:
            path = write_cache(code, rows, out_dir=out_dir)
        except Exception as exc:
            stats["failed"].append({"code": code, "attempts": attempts,
                                    "reason": f"写缓存失败: {exc}"})
            print(f"FAIL {code}: 写缓存失败 {exc}", file=sys.stderr)
            continue
        stats["ok"].append({"code": code, "rows": min(len(rows), 120),
                            "as_of": rows[-1]["date"], "path": path,
                            "attempts": attempts})
        print(f"OK   {code}: {min(len(rows), 120)} 行 as_of={rows[-1]['date']} -> {path}")

    stats["wall_seconds"] = round(now_fn().timestamp() - started.timestamp(), 2)
    return stats


def build_parser():
    parser = argparse.ArgumentParser(
        description="东财日级资金流预计算（写 source/stock_analysis/data/flow/）")
    parser.add_argument("codes", nargs="*", help="股票代码（默认 14 只自选）")
    parser.add_argument("--codes", dest="codes_csv", default=None,
                        help="逗号分隔的票池（与位置参数合并去重）")
    parser.add_argument("--out-dir", default=OUT_DIR, help=f"缓存目录（默认 {OUT_DIR}）")
    parser.add_argument("--interval", type=float, default=DEFAULT_INTERVAL,
                        help="相邻请求最小间隔秒数，不得 <1.0（默认 1.0）")
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
        stats = precompute(codes, out_dir=args.out_dir, interval=args.interval,
                           retries=args.retries, retry_wait=args.retry_wait)
    except ValueError as exc:
        print(f"ERROR {exc}", file=sys.stderr)
        return 2

    print(f"requests={stats['requests']} ok={len(stats['ok'])} "
          f"failed={len(stats['failed'])} wall={stats['wall_seconds']}s "
          f"interval={stats['interval_seconds']}s")
    if stats["failed"]:
        for item in stats["failed"]:
            print(f"  - {item['code']}: {item['reason']}", file=sys.stderr)
    if args.json:
        print(json.dumps(stats, ensure_ascii=False, indent=2))
    return 0 if not stats["failed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
