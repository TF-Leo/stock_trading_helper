#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P1-2（issue #36/#39）：日级资金流**收盘后预计算**（东财优先，新浪备胎）。

为什么必须预计算
----------------
东财 ``push2his`` 有 IP 级风控（上游「防封铁律」：串行、相邻请求 ≥1s）。
如果在 CLI / 9:15 盘前 cron 主路径里逐票同步拉取，既挤占盘前窗口，也容易把
出口 IP 打进黑名单。因此本工具只在**收盘后**运行一次，把结果落到

    source/stock_analysis/data/flow/<code>.json   （data/ 已 gitignore）

CLI 只读这份缓存（``stock_analysis/data_loader/flow.py``），**绝不触网**。

两种口径（不可混用）
--------------------
* 东财 §4.5：``source=eastmoney_push2his``、``caliber=eastmoney_main_net``，
  行字段为 ``main_net``（主力净流入 = 超大单 + 大单）等。
* 新浪「备用源速查」``fund_flow_backup``：``source=sina_fund_flow_backup``、
  ``caliber=sina_total_net``，行字段只有 ``net_amount``（**总净流入**，
  没有主力/大单拆分）。降级时 ``main_net_5d/20d`` 在 CLI 侧必须为 null。

用法
----
    python tools/precompute_flow.py                     # 默认 14 只自选
    python tools/precompute_flow.py 600519 002415       # 指定票池
    python tools/precompute_flow.py --codes 600519,002415 --json
    python tools/precompute_flow.py --interval 1.2 --retries 3
    python tools/precompute_flow.py --budget 60        # 单域名单轮请求硬上限

行为
----
* 逐票串行；**相邻两次请求**（含重试、含备胎）之间至少间隔 ``--interval`` 秒，
  默认 1.0s，且不允许调到 1.0 以下（防封铁律，不是可调参数）。
* 东财优先。**首票即探测**：第一次东财请求失败（整域 TLS RST / 空响应）后
  立刻把本轮标记为东财不可用，后续票不再请求东财（不做逐票重试轰炸），
  直接走新浪备胎；探测成功则后续票仍按 ``--retries`` 重试。
* 单票在某一源上失败后重试 ``--retries`` 次（默认 2），东财仍失败则改走
  新浪备胎；两者都失败才记失败并继续下一票。
* 只有 ≥ ``MIN_ROWS``（20）行且字段完整的响应才写缓存；失败**不覆盖**旧缓存。
* 缓存 JSON 记录 ``source`` / ``caliber`` / ``attempts``（各源实际请求次数）；
  降级到新浪时另写 ``degraded_reason``（issue #108，CLI 据此出声告警）。
* 输出成功/失败票、总请求数、墙钟；``--json`` 同时给出机读统计。

反爬熔断与请求预算（issue #108）
-------------------------------
* 进入本轮先读 ``data/state/eastmoney_cooldown.json``（与 ST 名单共用）；
  冷却未到期 → **0 请求**，``skipped_cooldown=True``，退出码 3；
* ``em_get`` 命中 HTTP 444 / 零字节 FIN → 立即熔断并写冷却，本轮剩余东财
  请求一个都不发，改走新浪，并在 stderr 打印 ``DEGRADED 已降级到
  sina_total_net，原因 …``；
* 每个域名本轮请求数硬上限 ``--budget``（默认 60），超限在发请求前抛出，
  提前退出并打印 ``BUDGET …``，剩余票不写缓存。

退出码：0 = 全部成功；1 = 有票失败 / 请求预算超限；2 = 参数/配置错误；
3 = 东财反爬冷却中（未发请求）。
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

from stock_analysis.data_sources import eastmoney_guard as em_guard  # noqa: E402

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

# 口径常量：与 data_loader/flow.py 完全一致；改这里必须同步改那边与 README。
SOURCE_EASTMONEY = "eastmoney_push2his"
SOURCE_SINA = "sina_fund_flow_backup"
CALIBER_EASTMONEY = "eastmoney_main_net"
CALIBER_SINA = "sina_total_net"
UPSTREAM_EASTMONEY = "a-stock-data v3.10.4 §4.5 stock_fund_flow_120d"
UPSTREAM_SINA = "a-stock-data v3.10.4 备用源速查 fund_flow_backup"
SINA_DAYS = 120
SINA_ROW_FIELDS = ("date", "net_amount")

# issue #108：预计算 run 级别的请求预算按域名分桶（不实际解 URL，只做计数键）。
DOMAIN_EASTMONEY = "push2his.eastmoney.com"
DOMAIN_SINA = "vip.stock.finance.sina.com.cn"


def normalize_code(code) -> str:
    """任意受支持写法 → 6 位纯数字（东财 secid 用纯码拼，不能带 sh/sz 前缀）。"""
    from stock_analysis.data_sources._astock_generated import norm_ticker
    return norm_ticker(str(code))


def fetch_fund_flow(code: str):
    """东财 §4.5 个股日级资金流（120 个交易日）。异常由调用方处理。"""
    from stock_analysis.data_sources._astock_generated import stock_fund_flow_120d
    return stock_fund_flow_120d(code)


def fetch_sina_fund_flow(code: str):
    """新浪「备用源速查」日度资金流（总净流入）。异常由调用方处理。

    只返回 ``date`` / ``close`` / ``net_amount`` / ``turnover``；**没有**主力或
    大单拆分，故绝不能写进东财口径的 ``main_net``。
    """
    from stock_analysis.data_sources._astock_generated import fund_flow_backup
    return fund_flow_backup(code, SINA_DAYS)


def validate_rows(rows):
    """校验并归一化东财返回；返回按日期升序的行列表，失败抛 ``ValueError``。

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


def validate_sina_rows(rows):
    """校验新浪备胎返回；只保留 ``date`` + ``net_amount``（总净流入，元）。

    新浪的 ``netamount`` 字段是**当日总净流入**，不是东财的 ``main_net``；
    这里保留原始语义，绝不改名为 ``main_net``。
    """
    if not isinstance(rows, list) or not rows:
        raise ValueError("空响应（新浪 fund_flow_backup 返回空列表）")
    clean = []
    seen = set()
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("行不是对象")
        missing = [k for k in SINA_ROW_FIELDS if k not in row]
        if missing:
            raise ValueError(f"缺字段 {','.join(missing)}")
        day = str(row["date"])[:10]
        try:
            datetime.fromisoformat(day)
            amount = float(row["net_amount"])
        except Exception as exc:
            raise ValueError(f"行格式错误（{day!r}）：{exc}") from exc
        if day in seen:
            continue
        seen.add(day)
        clean.append({"date": day, "net_amount": amount})
    clean.sort(key=lambda r: r["date"])
    if len(clean) < MIN_ROWS:
        raise ValueError(f"仅 {len(clean)} 行 < {MIN_ROWS}，不写缓存")
    return clean


def write_cache(code, rows, out_dir=None, limit=120, source=SOURCE_EASTMONEY,
                caliber=CALIBER_EASTMONEY, attempts=None, degraded_reason=None):
    """原子写入 ``<out_dir>/<code>.json``；返回落盘路径。

    ``source`` / ``caliber`` / ``attempts`` 必须显式落盘 —— CLI 侧据此分支
    摘要口径，绝不能靠猜或靠字段名是否恰好存在。issue #108 additive：
    降级到新浪口径时把 ``degraded_reason`` 一并落盘，CLI 据此出声告警。
    """
    out_dir = out_dir or OUT_DIR
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, f"{code}.json")
    if caliber == CALIBER_SINA:
        upstream = UPSTREAM_SINA
    else:
        upstream = UPSTREAM_EASTMONEY
    payload = {
        "schema_version": "1.0",
        "code": code,
        "source": source,
        "caliber": caliber,
        "upstream": upstream,
        "fetched_at": datetime.now().isoformat(),
        "as_of": rows[-1]["date"],
        "attempts": dict(attempts or {}),
        "rows": rows[-int(limit):],
    }
    if degraded_reason:
        # 只在真的降级时写；东财口径 / 旧缓存读取方看到的是缺省（按 None）。
        payload["degraded_reason"] = str(degraded_reason)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=1, sort_keys=True)
        handle.write("\n")
    os.replace(tmp, path)
    return path


def precompute(codes, out_dir=None, interval=DEFAULT_INTERVAL,
               retries=DEFAULT_RETRIES, retry_wait=DEFAULT_RETRY_WAIT,
               fetch_fn=None, sina_fetch_fn=None, sleep_fn=time.sleep,
               now_fn=datetime.now, budget=None, force=False):
    """逐票串行预计算；返回统计字典（不抛异常，失败逐票记录）。

    东财优先。**首票单次探测**：第一次东财请求失败即把本轮标记为东财不可用
    （``eastmoney_down=True``），后续票不再请求东财，直接走新浪备胎 ——
    整域 TLS RST 时不能逐票重试轰炸（issue #39）。探测成功后，单票的东财
    失败仍按 ``retries`` 重试，再降级新浪。

    issue #108：
    * 进入本轮先查 ``eastmoney_cooldown.json``；冷却未到期 → **0 请求**，
      ``skipped_cooldown=True``（``force=True`` 可人工跳过，仅供确认封锁解除）；
    * ``em_get`` 命中 HTTP 444 / 零字节 FIN → 立即熔断、写持久冷却，剩余东财
      请求一次也不发，降级新浪并在日志 / 缓存 / ``stats`` 里写明原因；
    * 每个域名本轮请求数有硬上限（默认 60），超限**提前退出并告警**，
      剩余票不再请求，也不会落半截缓存。
    """
    if interval < 1.0:
        raise ValueError("interval 不得小于 1.0 秒（东财防封铁律）")
    if retries < 0:
        raise ValueError("retries 不得为负")
    fetch_fn = fetch_fn or fetch_fund_flow
    sina_fetch_fn = sina_fetch_fn or fetch_sina_fund_flow
    request_budget = em_guard.RequestBudget(budget)
    guard = em_guard.EmFetchGuard(
        budget=request_budget, enforce_budget=False, mark=True,
        now_fn=lambda: now_fn().timestamp(), respect_cooling=not force)

    stats = {"out_dir": out_dir or OUT_DIR, "interval_seconds": float(interval),
             "retries": int(retries), "requests": 0, "ok": [], "failed": [],
             "eastmoney_down": False, "eastmoney_probe": None,
             "blocked": False, "block_reason": None,
             "skipped_cooldown": False, "budget_exceeded": False,
             "request_limits": request_budget.snapshot(),
             "degraded_reason": None, "degraded": False,
             "skipped": [], "reason": None, "wall_seconds": 0.0,
             "ok_sources": {SOURCE_EASTMONEY: 0, SOURCE_SINA: 0}}

    cooling = guard.cooling()
    if cooling and not force:
        stats["skipped_cooldown"] = True
        stats["request_limits"] = request_budget.snapshot()
        stats["reason"] = guard.cooling_message(cooling)
        print(f"SKIP {stats['reason']}", file=sys.stderr)
        return stats

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

    started = now_fn()
    last_request_at = [None]

    def _request(fn, code, domain):
        """发一次请求：先扣预算，再强制相邻请求 ≥interval（含重试/备胎）。

        预算在**发请求之前**扣减：超限抛 :class:`EastmoneyBudgetExceeded`，
        调用方据此外层捕获并提前退出，绝不静默继续。
        """
        request_budget.reserve(domain)
        if last_request_at[0] is not None:
            due = last_request_at[0] + interval
            wait = due - now_fn().timestamp()
            if wait > 0:
                sleep_fn(wait)
        last_request_at[0] = now_fn().timestamp()
        stats["requests"] += 1
        return fn(code)

    probe_pending = True
    current_code = None

    def _restore_tail():
        """预算/阻断提前退出时，把尚未处理的票如实记入 ``skipped``。"""
        seen_current = False
        for raw, code, _err in ordered:
            if code == current_code and not seen_current:
                seen_current = True
                continue
            if seen_current:
                stats["skipped"].append(code or raw)

    guard_entered = False
    try:
        guard.__enter__()
        guard_entered = True
        for raw, code, parse_error in ordered:
            if code is None:
                stats["failed"].append({"code": raw,
                                        "attempts": {"eastmoney": 0, "sina": 0},
                                        "reason": parse_error})
                continue
            current_code = code
            em_attempts = sina_attempts = 0
            em_reason = sina_reason = None
            rows = None
            source = caliber = None
            degraded_reason = None

            if not stats["eastmoney_down"] and not guard.blocked:
                # 探测票只打一次；探测成功后恢复 --retries。
                max_retries = 0 if probe_pending else int(retries)
                for attempt in range(max_retries + 1):
                    em_attempts += 1
                    try:
                        rows = validate_rows(_request(
                            fetch_fn, code, DOMAIN_EASTMONEY))
                        em_reason = None
                        break
                    except em_guard.EastmoneyBudgetExceeded:
                        raise
                    except Exception as exc:
                        em_reason = f"{type(exc).__name__}: {exc}"
                        if attempt < max_retries:
                            sleep_fn(retry_wait)
                    # 生成物可能把闸门的异常吞成空列表；以闸门记录为准。
                    if guard.block_reason:
                        em_reason = (f"东财反爬阻断（{guard.block_reason}）："
                                     f"{guard.block_detail}")
                        rows = None
                        break
                if probe_pending:
                    probe_pending = False
                    stats["eastmoney_probe"] = {
                        "code": code, "ok": rows is not None,
                        "attempts": em_attempts, "reason": em_reason,
                    }
                    if rows is None:
                        stats["eastmoney_down"] = True
                        print(f"PROBE 东财不可达，本轮改走新浪备胎："
                              f"attempts={em_attempts} reason={em_reason}",
                              file=sys.stderr)
                if guard.block_reason and not stats["blocked"]:
                    stats["blocked"] = True
                    stats["block_reason"] = guard.block_reason
                    stats["eastmoney_down"] = True
                    print(f"BLOCKED 东财反爬命中 {guard.block_reason}："
                          f"{guard.block_detail}；已写持久冷却，"
                          "本轮剩余东财请求一个都不发",
                          file=sys.stderr)

            if rows is not None and not guard.block_reason:
                source, caliber = SOURCE_EASTMONEY, CALIBER_EASTMONEY
            else:
                rows = None
                em_display = em_reason or ("本轮已标记不可用（跳过）"
                                           if em_attempts == 0 else "未知")
                degraded_reason = (
                    f"东财反爬阻断（{guard.block_reason}）：{guard.block_detail}"
                    if guard.block_reason else f"东财不可达：{em_display}")
                for attempt in range(int(retries) + 1):
                    sina_attempts += 1
                    try:
                        rows = validate_sina_rows(_request(
                            sina_fetch_fn, code, DOMAIN_SINA))
                        sina_reason = None
                        break
                    except em_guard.EastmoneyBudgetExceeded:
                        raise
                    except Exception as exc:
                        sina_reason = f"{type(exc).__name__}: {exc}"
                        if attempt < int(retries):
                            sleep_fn(retry_wait)
                if rows is not None:
                    source, caliber = SOURCE_SINA, CALIBER_SINA

            attempts = {"eastmoney": em_attempts, "sina": sina_attempts}
            if rows is None:
                reason = f"eastmoney: {em_display}; sina: {sina_reason}"
                stats["failed"].append({"code": code, "attempts": attempts,
                                        "reason": reason})
                print(f"FAIL {code}: attempts={attempts} reason={reason}",
                      file=sys.stderr)
                continue
            if source == SOURCE_SINA and stats["degraded_reason"] is None:
                stats["degraded_reason"] = degraded_reason
                stats["degraded"] = True
                print(f"DEGRADED 已降级到 {CALIBER_SINA}，原因 {degraded_reason}",
                      file=sys.stderr)
            try:
                path = write_cache(code, rows, out_dir=out_dir, source=source,
                                   caliber=caliber, attempts=attempts,
                                   degraded_reason=degraded_reason)
            except Exception as exc:
                stats["failed"].append({"code": code, "attempts": attempts,
                                        "reason": f"写缓存失败: {exc}"})
                print(f"FAIL {code}: 写缓存失败 {exc}", file=sys.stderr)
                continue
            stats["ok"].append({"code": code, "rows": min(len(rows), 120),
                                "as_of": rows[-1]["date"], "path": path,
                                "source": source, "caliber": caliber,
                                "attempts": attempts,
                                "degraded_reason": degraded_reason})
            stats["ok_sources"][source] += 1
            print(f"OK   {code}: {min(len(rows), 120)} 行 source={source} "
                  f"caliber={caliber} attempts={attempts} "
                  f"as_of={rows[-1]['date']} -> {path}")

    except em_guard.EastmoneyCooling as exc:
        stats["skipped_cooldown"] = True
        stats["reason"] = str(exc)
        print(f"SKIP {exc}", file=sys.stderr)
        _restore_tail()
    except em_guard.EastmoneyBudgetExceeded as exc:
        stats["budget_exceeded"] = True
        stats["reason"] = str(exc)
        stats["failed"].append({"code": current_code,
                                "attempts": {"eastmoney": None, "sina": None},
                                "reason": str(exc)})
        _restore_tail()
        print(f"BUDGET {exc}", file=sys.stderr)
    finally:
        if guard_entered:
            guard.__exit__(None, None, None)
        stats["request_limits"] = request_budget.snapshot()

    stats["wall_seconds"] = round(now_fn().timestamp() - started.timestamp(), 2)
    return stats


def build_parser():
    parser = argparse.ArgumentParser(
        description="日级资金流预计算（东财优先，新浪备胎；写 "
                    "source/stock_analysis/data/flow/）")
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
    parser.add_argument("--budget", type=int, default=None,
                        help="单域名单轮请求硬上限（默认 60，环境变量 "
                             "EASTMONEY_REQUEST_BUDGET 可覆盖）")
    parser.add_argument("--force", action="store_true",
                        help="忽略东财反爬冷却（仅人工确认封锁解除后使用）")
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
                           retries=args.retries, retry_wait=args.retry_wait,
                           budget=args.budget, force=args.force)
    except ValueError as exc:
        print(f"ERROR {exc}", file=sys.stderr)
        return 2

    sources = stats.get("ok_sources") or {}
    limits = stats.get("request_limits") or {}
    print(f"requests={stats['requests']} ok={len(stats['ok'])} "
          f"failed={len(stats['failed'])} wall={stats['wall_seconds']}s "
          f"interval={stats['interval_seconds']}s "
          f"eastmoney_down={stats.get('eastmoney_down')} "
          f"eastmoney_ok={sources.get(SOURCE_EASTMONEY, 0)} "
          f"sina_ok={sources.get(SOURCE_SINA, 0)} "
          f"degraded={stats.get('degraded')} "
          f"budget={limits.get('counts') or {}}/{limits.get('limit_per_domain')}")
    if stats.get("skipped_cooldown"):
        print(f"SKIP {stats.get('reason')}", file=sys.stderr)
    if stats.get("budget_exceeded"):
        print(f"BUDGET {stats.get('reason')}", file=sys.stderr)
    if stats["failed"]:
        for item in stats["failed"]:
            print(f"  - {item['code']}: {item['reason']}", file=sys.stderr)
    if args.json:
        print(json.dumps(stats, ensure_ascii=False, indent=2))
    if stats.get("skipped_cooldown"):
        return 3
    return 0 if not stats["failed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
