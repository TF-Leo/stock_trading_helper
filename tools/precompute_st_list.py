#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ST 名单**收盘后预计算**（PR #107 / P1，2026-11-15；多源备胎同日起）。

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

多源取数（2026-11-15 起）
-------------------------
东财 444 已持续约 40 小时，ST 是硬风险，不能长期靠名称猜。预计算按固定顺序
**串行**取数，每源一次逻辑取数、不重试：

1. **东财**（既有主源，覆盖沪深京）：冷却中则跳过（对东财 0 请求），否则
   ``status.fetch_st_list()``；
2. **交易所官方备胎**（``data_sources/st_exchange.py``）：
   * 上交所 ``query.sse.com.cn``（1 个 HTTP 请求，``COMPANY_STATUS=7``）；
   * 深交所 ``szse.cn/api/report/ShowReport/data``（按官方 ``pagecount``
     串行翻页，当前 6 页×20 条）；
   * 两所任一失败 → 官方名单**不完整**，不得单独作为沪深全量名单落盘；
3. **本地缓存**：以上都失败时**不写缓存**，盘前继续用旧缓存（可能
   ``stale=true``），绝不把空表/半截名单覆盖好缓存。

合并规则
--------
* 东财可用：以东财为基准（含北交所），把官方名单按代码**取并集**——
  任一源说是 ST 就是 ST（ST 假阴性会让人误买风险警示股）；
* 东财不可用、两所官方都可用：只用官方名单（覆盖沪深 A 股 + 深市 B 股，
  不含北交所；缓存 warning 明确写出覆盖缺口）；
* 同名代码名称不一致 / 只在一边出现（沪深重叠范围）→ 取并集并告警；
  北交所代码不在官方覆盖内，不算冲突。
* 缓存 ``source`` 记录主源（``eastmoney`` / ``exchange``），
  ``merged_from`` 记录本轮实际合并的源（``eastmoney`` / ``sse`` / ``szse``）。

反爬熔断
--------
取东财经 :class:`status.StListFetchGuard` 包一层 ``em_get``：命中 HTTP 444 或
零字节 FIN 时**立即中止东财本轮**（不换域、不重试），写
``data/state/eastmoney_cooldown.json``（issue #108 起与资金流预计算**共用
同一份冷却文件**），默认冷却 1800 秒（``EASTMONEY_COOLDOWN_SECONDS`` 可覆盖）。
issue #130 起冷却**按域隔离**：只写 ``surfaces["push2 家族"]``
（``push2`` / ``push2delay`` / ``push2his`` 同一风控面，归并成一个键），
不写顶层共享块 —— 事件（``datacenter-web``）或监控池被 444 不会让 ST 名单
整轮停摆，反之亦然。顶层共享块只在同一冷却窗口内 ≥2 个不同域都命中 444
时自动升级。冷却期内**不再请求东财**，但仍会尝试官方备胎（官方是不同主机，
不受东财冷却影响）。每轮对东财同一域名的请求数有硬上限（默认 60，
``--budget`` / ``EASTMONEY_REQUEST_BUDGET`` 可覆盖）。

cron（实际接线由协调者安排）
----------------------------
::

    # 工作日 18:35（收盘后、数据落定后）
    35 18 * * 1-5  cd $REPO && /usr/bin/env python3 tools/precompute_st_list.py \
        >> $HOME/logs/st_list_precompute.log 2>&1

退出码
------
* 0 = 缓存已更新（东财或官方备胎，至少一源完整）；
* 2 = 参数错误；
* 3 = 东财冷却中且官方备胎被显式禁用（``--no-official``），本轮 0 请求；
* 4 = 所有可用源都失效 / 坏结果 / 写盘失败（缓存未被覆盖）。

用法
----
::

    python tools/precompute_st_list.py
    python tools/precompute_st_list.py --json
    python tools/precompute_st_list.py --state-dir /tmp/st-state
    python tools/precompute_st_list.py --now 2026-11-15T18:35:00
    python tools/precompute_st_list.py --force            # 忽略东财冷却
    python tools/precompute_st_list.py --clear-cooldown   # 只删冷却文件
    python tools/precompute_st_list.py --dry-run          # 只说明计划，不触网
    python tools/precompute_st_list.py --no-official      # 只用东财（维护/对照）
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
from stock_analysis.data_sources import eastmoney_guard as em_guard  # noqa: E402
from stock_analysis.data_sources import st_exchange  # noqa: E402

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


def _snapshot_stats(snapshot, requests_made=0) -> dict:
    return {
        "state": snapshot.state,
        "available": bool(snapshot.available),
        "count": len(snapshot.stocks),
        "requests": int(requests_made),
        "warning": snapshot.warning,
        "reason": snapshot.reason,
    }


def _merge_east_with_official(east: st.StListSnapshot,
                              official_lists):
    """东财为基准 + 官方名单取并集；冲突取「是 ST」并告警。

    返回 ``(snapshot, conflicts)``。``conflicts`` 是**结构化条目**列表
    （issue #123）：``{"kind": "east_only" / "official_only" /
    "name_mismatch", "count": n, "codes": [...], "message": "..."}``；
    没有任何冲突时为空列表。``snapshot.warning`` 仍是给人看的整段文案。
    """
    stocks = {code: dict(info) for code, info in east.stocks.items()}
    official_codes = set()
    name_conflicts = []
    for snapshot in official_lists:
        for row in snapshot.rows:
            code = row["code"]
            official_codes.add(code)
            info = {"name": row["name"], "st_type": row["st_type"],
                    "market": row["market"]}
            if code not in stocks:
                stocks[code] = info
                continue
            old = stocks[code]
            old_name = str(old.get("name") or "").strip()
            if old_name and old_name != row["name"]:
                name_conflicts.append(code)
                if row["st_type"] == "*ST" and str(old.get("st_type")) != "*ST":
                    old.update(info)
    covered_markets = {item.source for item in official_lists}
    covered_markets = {"sh" if source == "sse" else "sz"
                       for source in covered_markets}
    # 只在**成功取到的官方源覆盖的市场**内比较冲突：若只有上交所成功，
    # 不能把全部深市东财代码误报成「东财独有」。
    east_overlap = {code for code in east.stocks
                    if st_exchange.market_of(code) in covered_markets}
    east_only = sorted(east_overlap - official_codes)
    official_only = sorted(official_codes - set(east.stocks))
    conflicts = []
    if east_only:
        conflicts.append({
            "kind": "east_only", "count": len(east_only), "codes": east_only,
            "message": f"东财独有 {len(east_only)}（样例 {east_only[:8]}）"})
    if official_only:
        conflicts.append({
            "kind": "official_only", "count": len(official_only),
            "codes": official_only,
            "message": f"官方独有 {len(official_only)}"
                       f"（样例 {official_only[:8]}）"})
    if name_conflicts:
        unique = sorted(set(name_conflicts))
        conflicts.append({
            "kind": "name_mismatch", "count": len(unique), "codes": unique,
            "message": f"名称不一致 {len(unique)}（样例 {unique[:8]}）"})
    warning = east.warning
    if conflicts:
        note = ("两源冲突：" + "；".join(item["message"] for item in conflicts) +
                "；按「是 ST」取并集（不会漏报）")
        warning = f"{warning}；{note}" if warning else note
    snapshot = st.StListSnapshot(
        available=True, stocks=stocks, coverage=east.coverage,
        source="eastmoney", fetched_at=east.fetched_at,
        session_date=east.session_date, degraded=east.degraded,
        warning=warning, state="ok", stale=east.stale, retried=east.retried,
        cleaned_duplicates=east.cleaned_duplicates, reason=east.reason,
        merged_from=["eastmoney"] + [item.source for item in official_lists])
    return snapshot, conflicts


def _official_only_snapshot(official_lists):
    """两所官方名单合并成快照（覆盖沪深 A 股 + 深市 B 股，不含北交所）。

    返回 ``(snapshot, conflicts)``；``conflicts`` 语义同
    :func:`_merge_east_with_official`。
    """
    frame = st_exchange.merge_to_frame(official_lists)
    snapshot = st.snapshot_from_frame(frame)
    snapshot.source = "exchange"
    snapshot.coverage = str(frame.attrs.get("coverage") or snapshot.coverage)
    snapshot.merged_from = [item.source for item in official_lists]
    note = ("东财不可用，回退交易所官方风险警示名单（"
            f"{snapshot.coverage}）；官方名单不含北交所，"
            "北交所标的仍需实时报价名称兜底")
    frame_warning = frame.attrs.get("warning")
    snapshot.warning = f"{note}；{frame_warning}" if frame_warning else note
    return snapshot, list(frame.attrs.get("conflicts") or [])


def precompute(now=None, force=False, dry_run=False, budget=None,
               official=True) -> dict:
    """执行一次预计算，返回统计字典（不抛异常；由 main 映射退出码）。

    ``force=True`` 忽略东财冷却；``dry_run=True`` 完全不触网（也不写冷却）；
    ``budget`` 为东财单域名单轮请求硬上限（默认 60）；``official=False``
    跳过官方备胎（只用于维护/对照，正常 cron 不要开）。
    """
    now = _parse_now(now) if now is not None else _dt.datetime.now()
    stats = {
        "ok": False,
        "dry_run": bool(dry_run),
        "skipped_cooldown": False,
        "eastmoney_skipped_cooldown": False,
        "official_enabled": bool(official),
        "requests": 0,
        "state": None,
        "count": 0,
        "path": None,
        "source": None,
        "merged_from": [],
        "conflicts": [],
        "conflict_summary": None,
        "sources": {},
        "official_errors": [],
        "blocked": False,
        "block_reason": None,
        "cooldown": None,
        "budget_exceeded": False,
        "request_limits": None,
        "reason": None,
        "now": now.isoformat(),
    }

    cooling = st.cooldown_state(now)
    east_cooling = bool(cooling) and not force
    if east_cooling:
        stats["eastmoney_skipped_cooldown"] = True
        stats["cooldown"] = cooling
        stats["sources"]["eastmoney"] = {
            "state": "cooldown", "count": 0, "requests": 0,
            "reason": (f"冷却中（剩余 "
                       f"{int(round(float(cooling.get('remaining_seconds') or 0)))}s，"
                       f"原因 {cooling.get('reason') or 'unknown'}）"),
        }
        stats["reason"] = stats["sources"]["eastmoney"]["reason"]

    if dry_run:
        stats["state"] = "dry_run"
        stats["reason"] = (
            "将先取东财 clist（冷却中则 0 请求），再串行取交易所官方名单："
            "上交所 1 次、深交所按官方 pagecount 串行翻页；任一所需源异常都不"
            "覆盖好缓存")
        if not official:
            stats["reason"] += "；--no-official 已禁用官方备胎"
        return stats

    east_snapshot = None
    if not east_cooling:
        guard = st.StListFetchGuard(budget=budget,
                                    now_fn=lambda: now.timestamp(),
                                    respect_cooling=not force)
        try:
            with guard:
                east_snapshot = st.fetch_st_list()
        except em_guard.EastmoneyCooling as exc:
            # 预检查与进入闸门之间，另一进程可能刚写下冷却：东财本轮 0 请求，
            # 但官方备胎仍继续（不同主机）。
            stats["eastmoney_skipped_cooldown"] = True
            stats["sources"]["eastmoney"] = {
                "state": "cooldown", "count": 0, "requests": 0,
                "reason": str(exc)}
            stats["reason"] = str(exc)
        else:
            stats["requests"] += guard.requests
            stats["request_limits"] = guard.budget.snapshot()
            stats["state"] = east_snapshot.state
            stats["sources"]["eastmoney"] = _snapshot_stats(
                east_snapshot, guard.requests)
            if guard.budget.exceeded:
                stats["budget_exceeded"] = True
                stats["reason"] = guard.budget.last_error
                print(f"BUDGET {guard.budget.last_error}", file=sys.stderr)
            if guard.block_reason:
                stats["blocked"] = True
                stats["block_reason"] = guard.block_reason
                try:
                    stats["cooldown"] = st.mark_cooldown(
                        guard.block_reason, guard.block_detail, now=now)
                except Exception as exc:  # 冷却写失败不能掩盖取数失败
                    stats["cooldown"] = {"error": f"{type(exc).__name__}: {exc}"}

    official_result = None
    if official:
        official_result = st_exchange.fetch_official_st_lists()
        stats["requests"] += official_result.requests
        for item in official_result.lists:
            stats["sources"][item.source] = {
                "state": "ok", "count": len(item.rows),
                "requests": item.requests, "coverage": item.coverage,
                "session_date": item.session_date,
            }
        for error in official_result.errors:
            stats["official_errors"].append(error)
            stats["sources"][error["source"]] = {
                "state": "failed", "count": 0,
                "requests": error.get("requests", 0),
                "error": error["error"],
            }
    else:
        for source in ("sse", "szse"):
            stats["sources"][source] = {"state": "disabled", "requests": 0}

    east_ok = bool(east_snapshot is not None and east_snapshot.available
                   and east_snapshot.state == "ok" and east_snapshot.stocks)
    official_ok = bool(official_result is not None
                       and not official_result.errors
                       and len(official_result.lists) == 2)

    chosen = None
    conflicts = []
    if east_ok:
        lists = list(official_result.lists) if official_result else []
        chosen, conflicts = _merge_east_with_official(east_snapshot, lists)
        stats["source"] = "eastmoney"
        if official_result and official_result.errors:
            note = ("官方备胎部分失败（" + "；".join(
                f"{item['source']}: {item['error']}"
                for item in official_result.errors) +
                "），本轮以东财名单为主")
            chosen.warning = f"{chosen.warning}；{note}" if chosen.warning \
                else note
    elif official_ok:
        chosen, conflicts = _official_only_snapshot(official_result.lists)
        stats["source"] = "exchange"
    else:
        # 没有任何所需源给出完整名单：只告警，绝不覆盖已有好缓存。
        reasons = []
        if stats["reason"]:
            reasons.append(stats["reason"])
        if east_snapshot is not None and east_snapshot.state != "ok":
            reasons.append(east_snapshot.warning or east_snapshot.reason
                           or f"东财名单异常（{east_snapshot.state}）")
        if official_result:
            for error in official_result.errors:
                reasons.append(
                    f"{error['source']} 官方源失败：{error['error']}")
        stats["reason"] = "；".join(reason for reason in reasons if reason) \
            or "名单异常"
        stats["skipped_cooldown"] = bool(
            stats["eastmoney_skipped_cooldown"] and not official)
        return stats

    try:
        stats["path"] = st.write_st_list_cache(chosen, now)
    except Exception as exc:
        stats["state"] = "write_failed"
        stats["reason"] = f"写缓存失败：{type(exc).__name__}: {exc}"
        return stats

    stats["ok"] = True
    stats["state"] = "ok"
    stats["count"] = len(chosen.stocks)
    stats["merged_from"] = list(chosen.merged_from)
    # issue #123：``conflicts`` 装**结构化冲突条目**（不是整段 warning）。
    # 每条：{"kind": "east_only" / "official_only" / "name_mismatch",
    #        "count": n, "codes": [...], "message": "..."}。
    # 整段文案在 ``reason`` / 缓存 warning 里（保持原样，additive）。
    stats["conflicts"] = conflicts
    stats["conflict_summary"] = chosen.warning
    stats["reason"] = chosen.warning
    if east_ok:
        # 东财成功说明封锁已解除：清掉本域可能残留的过期/已强制跳过的条目
        # （issue #130：只清 push2 家族，不碰其它域与顶层共享封锁）。
        # 官方备胎成功**不**清东财冷却（东财可能仍在封禁）。
        try:
            st.clear_cooldown()
        except OSError:
            pass
    return stats


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="收盘后预计算全市场 ST 名单（东财优先 → 交易所官方备胎；"
                    "盘前只读缓存，零请求）")
    parser.add_argument("--state-dir", default=None,
                        help="缓存/冷却目录（默认 source/stock_analysis/data/state）")
    parser.add_argument("--now", default=None,
                        help="ISO 时间，测试/回放用（默认当前时间）")
    parser.add_argument("--force", action="store_true",
                        help="忽略东财冷却状态（人工确认封锁解除后使用）")
    parser.add_argument("--clear-cooldown", action="store_true",
                        help="只删除冷却文件后退出（不触网）")
    parser.add_argument("--dry-run", action="store_true",
                        help="只说明将做什么，不发任何请求")
    parser.add_argument("--budget", type=int, default=None,
                        help="东财单域名单轮请求硬上限（默认 60）")
    parser.add_argument("--no-official", action="store_true",
                        help="跳过交易所官方备胎（只用东财；维护/对照用）")
    parser.add_argument("--json", action="store_true", help="输出 JSON 统计")
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    if args.state_dir:
        os.environ[st.ST_LIST_STATE_DIR_ENV] = str(args.state_dir)

    if args.clear_cooldown:
        st.clear_cooldown()
        message = f"已清除本域冷却条目 {st.ST_LIST_SURFACE}（{st.cooldown_path()}）"
        print(message)
        if args.json:
            print(json.dumps({"cleared": st.ST_LIST_SURFACE,
                              "path": st.cooldown_path()},
                             ensure_ascii=False))
        return EXIT_OK

    try:
        stats = precompute(now=args.now, force=args.force, dry_run=args.dry_run,
                           budget=args.budget, official=not args.no_official)
    except ValueError as exc:
        print(f"ERROR {exc}", file=sys.stderr)
        return EXIT_ARGS

    if stats["skipped_cooldown"]:
        print(f"SKIP {stats['reason']}")
    elif stats["dry_run"]:
        print(f"DRY-RUN requests=0 {stats['reason']}")
    elif stats["ok"]:
        merged = "+".join(stats["merged_from"]) or "-"
        print(f"OK count={stats['count']} requests={stats['requests']} "
              f"state={stats['state']} source={stats['source']} "
              f"merged_from={merged} -> {stats['path']}")
        if stats["conflicts"]:
            for conflict in stats["conflicts"]:
                print(f"  CONFLICT {conflict['message']}", file=sys.stderr)
    else:
        print(f"FAIL requests={stats['requests']} state={stats['state']} "
              f"blocked={stats['block_reason'] or '-'} "
              f"budget_exceeded={stats.get('budget_exceeded')}", file=sys.stderr)
        if stats["reason"]:
            print(f"  {stats['reason']}", file=sys.stderr)
        for error in stats["official_errors"]:
            print(f"  official {error['source']}: {error['error']}",
                  file=sys.stderr)
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