#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ST 名单**收盘后预计算**（PR #107 / P1，2026-11-15）。

为什么必须预计算
----------------
A5500 实测：东财 ``push2`` / ``push2delay`` 对本机出口 IP 触发 nginx **444**
反爬（3 分钟内约 10 次请求即触发）。ST 名单走 clist 全市场翻页；盘前若对每只
票各跑一次 CLI，等于每天把全市场名单反复拉 N 次，是封禁的直接原因。而且旧
缓存 ``data/status/st_list.json`` 从未落盘，于是每天盘前退化成按名称猜 ST。

本工具只在收盘后（18:30–18:40 时段）运行**一次**，把全市场名单落到

    source/stock_analysis/data/state/st_list.json   （``data/`` 已 gitignore）

``cli.py`` 主路径只读这份缓存（``status.get_st_list_cached``），对
push2/push2delay 的请求数为 **0**。

缓存结构（schema 2）
--------------------
::

    {
      "schema": 2,
      "source": "eastmoney",
      "coverage": "沪深京",
      "fetched_at": "2026-11-15T18:35:00",   # 本次取数时间
      "session_date": "2026-11-14",          # 快照所属交易日
      "count": 123,
      "sha256": "<代码集合的 SHA-256>",       # 见 status.codes_sha256
      "codes": ["000002", "600001", ...],   # 权威名单（排序）
      "stocks": {"600001": {"name": ..., ...}},  # 名称/价格留档
      "state": "ok"
    }

三态与覆盖规则（沿用 #107）
--------------------------
* 空表 / 重复代码：按 #106 重试一次；仍异常 → **不写缓存**，只告警；
* 网络失败：不重试，**不写缓存**，只告警；
* 只有 ``state='ok'`` 且名单非空才原子覆盖缓存 —— 坏结果永远不覆盖一份好缓存。

反爬熔断
--------
取数经 :class:`status.StListFetchGuard` 包一层 ``em_get``：命中 HTTP 444 或
零字节 FIN 时**立即中止**（不换域、不重试），写
``data/state/st_list_cooldown.json``，默认冷却 1800 秒。冷却期内再次运行直接
跳过（退出码 3），不会产生任何请求。人工确认封锁解除后可用 ``--force``
或 ``--clear-cooldown``。

cron（实际接线由协调者安排）
----------------------------
::

    # 工作日 18:35（收盘后、数据落定后）
    35 18 * * 1-5  cd $REPO && /usr/bin/env python3 tools/precompute_st_list.py \
        >> $HOME/logs/st_list_precompute.log 2>&1

退出码
------
* 0 = 缓存已更新；
* 2 = 参数错误；
* 3 = 冷却中跳过（未发请求）；
* 4 = 取数失败 / 坏结果 / 写盘失败（缓存未被覆盖）。

用法
----
::

    python tools/precompute_st_list.py
    python tools/precompute_st_list.py --json
    python tools/precompute_st_list.py --state-dir /tmp/st-state
    python tools/precompute_st_list.py --now 2026-11-15T18:35:00
    python tools/precompute_st_list.py --force            # 忽略冷却（人工解除后）
    python tools/precompute_st_list.py --clear-cooldown   # 只删冷却文件
    python tools/precompute_st_list.py --dry-run          # 只说明计划，不触网
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "source")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from stock_analysis.data_loader import status as st  # noqa: E402

EXIT_OK = 0
EXIT_ARGS = 2
EXIT_COOLDOWN = 3
EXIT_FAILED = 4


def _parse_now(value) -> _dt.datetime:
    if value is None:
        return _dt.datetime.now()
    try:
        return _dt.datetime.fromisoformat(str(value))
    except ValueError as exc:
        raise ValueError(f"--now 需要 ISO 时间（如 2026-11-15T18:35:00）：{exc}")


def precompute(now=None, force=False, dry_run=False) -> dict:
    """执行一次预计算，返回统计字典（不抛异常；由 main 映射退出码）。

    ``force=True`` 忽略冷却；``dry_run=True`` 完全不触网（也不写冷却）。
    """
    now = _parse_now(now) if now is not None else _dt.datetime.now()
    stats = {
        "ok": False,
        "dry_run": bool(dry_run),
        "skipped_cooldown": False,
        "requests": 0,
        "state": None,
        "count": 0,
        "path": None,
        "blocked": False,
        "block_reason": None,
        "cooldown": None,
        "reason": None,
        "now": now.isoformat(),
    }

    cooling = st.cooldown_state(now)
    if cooling and not force:
        stats["skipped_cooldown"] = True
        stats["cooldown"] = cooling
        stats["reason"] = (
            f"冷却中（剩余 {int(round(float(cooling.get('remaining_seconds') or 0)))}s，"
            f"原因 {cooling.get('reason') or 'unknown'}），本轮未发请求")
        return stats

    if dry_run:
        stats["state"] = "dry_run"
        stats["reason"] = ("将串行请求东财 clist：沪深风险警示板 + 北交所全表"
                           "（一次全市场名单；冷却期内不发请求）")
        return stats

    guard = st.StListFetchGuard()
    with guard:
        snapshot = st.fetch_st_list()
    stats["requests"] = guard.requests
    stats["state"] = snapshot.state
    stats["blocked"] = bool(guard.block_reason)

    if guard.block_reason:
        # 444 / 零字节 FIN：记失败 + 冷却；即使本轮侥幸拼出了名单也不落盘，
        # 避免在封禁窗口里继续把「半截名单」当权威。
        stats["block_reason"] = guard.block_reason
        stats["reason"] = snapshot.reason or snapshot.warning
        try:
            stats["cooldown"] = st.mark_cooldown(
                guard.block_reason, guard.block_detail, now=now)
        except Exception as exc:  # 冷却写失败不能掩盖取数失败
            stats["cooldown"] = {"error": f"{type(exc).__name__}: {exc}"}
        return stats

    if snapshot.available and snapshot.state == "ok" and snapshot.stocks:
        try:
            stats["path"] = st.write_st_list_cache(snapshot, now)
        except Exception as exc:
            stats["state"] = "write_failed"
            stats["reason"] = f"写缓存失败：{type(exc).__name__}: {exc}"
            return stats
        stats["ok"] = True
        stats["count"] = len(snapshot.stocks)
        stats["reason"] = snapshot.warning
        return stats

    # 空表 / 重复代码 / 网络失败：只告警，绝不覆盖已有好缓存。
    stats["reason"] = snapshot.warning or snapshot.reason or "名单异常"
    return stats


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="收盘后预计算全市场 ST 名单（盘前只读缓存，零请求）")
    parser.add_argument("--state-dir", default=None,
                        help="缓存/冷却目录（默认 source/stock_analysis/data/state）")
    parser.add_argument("--now", default=None,
                        help="ISO 时间，测试/回放用（默认当前时间）")
    parser.add_argument("--force", action="store_true",
                        help="忽略冷却状态（人工确认封锁解除后使用）")
    parser.add_argument("--clear-cooldown", action="store_true",
                        help="只删除冷却文件后退出（不触网）")
    parser.add_argument("--dry-run", action="store_true",
                        help="只说明将做什么，不发任何请求")
    parser.add_argument("--json", action="store_true", help="输出 JSON 统计")
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    if args.state_dir:
        os.environ[st.ST_LIST_STATE_DIR_ENV] = str(args.state_dir)

    if args.clear_cooldown:
        st.clear_cooldown()
        message = f"已清除冷却文件 {st.cooldown_path()}"
        print(message)
        if args.json:
            print(json.dumps({"cleared": st.cooldown_path()}, ensure_ascii=False))
        return EXIT_OK

    try:
        stats = precompute(now=args.now, force=args.force, dry_run=args.dry_run)
    except ValueError as exc:
        print(f"ERROR {exc}", file=sys.stderr)
        return EXIT_ARGS

    if stats["skipped_cooldown"]:
        print(f"SKIP {stats['reason']}")
    elif stats["ok"]:
        print(f"OK count={stats['count']} requests={stats['requests']} "
              f"state={stats['state']} -> {stats['path']}")
    else:
        print(f"FAIL requests={stats['requests']} state={stats['state']} "
              f"blocked={stats['block_reason'] or '-'}", file=sys.stderr)
        if stats["reason"]:
            print(f"  {stats['reason']}", file=sys.stderr)
        if stats["cooldown"]:
            print(f"  cooldown -> {st.cooldown_path()}: {stats['cooldown']}",
                  file=sys.stderr)

    if args.json:
        print(json.dumps(stats, ensure_ascii=False, indent=2, sort_keys=True))

    if stats["skipped_cooldown"]:
        return EXIT_COOLDOWN
    return EXIT_OK if stats["ok"] else EXIT_FAILED


if __name__ == "__main__":
    raise SystemExit(main())