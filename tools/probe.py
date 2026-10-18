# -*- coding: utf-8 -*-
"""门禁取数探针：所有"必须真跑才能判定"的验收项都放这里。

由 tools/check_gates.ps1 调用，输出单个 JSON 文档到 stdout。
设计原则（针对本项目历史失败模式）：
  * 只用**真实执行结果**判定，禁止用 grep 源码 / 常量字符串冒充通过；
  * 无法验证的项必须显式 BLOCKED 并给出原因，不得静默算通过；
  * 同一份 CLI 输出在多条门禁间复用（避免把门禁跑成十分钟）。
"""
from __future__ import annotations

import copy
import json
import os
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "source")

# Phase C 契约字段（C3）：股票与基金两侧必须一致存在
REQUIRED_TOP_KEYS = [
    "schema_version", "generated_at", "request", "instrument", "data",
    "market_regime", "recommendation", "evidence", "candidates",
    "diagnostics", "disclaimer",
]

# stop_method（F12）纳入必备字段：它标明止损价是 ATR 推导还是固定百分比。
# no_edge（2026-10-18 补登）：读取方用它做"是否可交易"的二分判断，
#   与 status 必须自洽（G18 已断言 no_edge == (status == "no_edge")）。
#   此前它实际一直输出，但没进必备清单 —— 意味着可以被静默移除而不被发现。
REQUIRED_REC_KEYS = [
    "status", "action", "action_reason", "confidence", "position_fraction",
    "horizon_days", "entry_zone", "stop_loss", "take_profit",
    "distance_to_trigger", "invalidation", "stop_method", "no_edge",
]
# F06f/F20 的数据新鲜度字段同样锁定，防止"降级不可见"回归。
# window_note（2026-10-18 补登）：`--start` 生效时的截取说明，两条路径都输出。
# 注意：错误响应路径（create_error_response）已与成功路径同构（G35 锁定），
# 因此该列表在两侧都应成立。
REQUIRED_DATA_KEYS = [
    "bars", "start", "end", "last_close", "source", "from_cache",
    "stale", "cache_last_date", "lag_days", "expected_date", "requested_source",
    "window_note",
]

EXIT_CODES = {"ok": 0, "no_edge": 10, "insufficient_data": 20,
              "data_source_error": 30, "untradeable": 40}

_CLI_CACHE = {}


def sh(args, timeout=300):
    t0 = time.time()
    p = subprocess.run(
        [sys.executable, "cli.py"] + args,
        capture_output=True, cwd=SRC, timeout=timeout,
    )
    return {
        "exit": p.returncode,
        "out": p.stdout.decode("utf-8", "replace"),
        "err": p.stderr.decode("utf-8", "replace"),
        "sec": round(time.time() - t0, 1),
    }


def run_cli_json(args, cache=True, timeout=300):
    """真跑 CLI 并要求 stdout 是纯 JSON（C1 契约）。退出码必须是契约五码之一。"""
    key = tuple(args)
    if cache and key in _CLI_CACHE:
        # 必须返回深拷贝：各 check 会往 rec 里写 status/reason，
        # 若按引用返回，后一个 check 会覆盖前一个 check 的结论（实测 r1 is r2 == True），
        # 且当某 check 提前 return 而未设 status 时会沿用上一个 check 的 status → 假 PASS 隐患。
        return copy.deepcopy(_CLI_CACHE[key])
    r = sh(args, timeout=timeout)
    rec = {"cmd": "python cli.py " + " ".join(args), "exit": r["exit"], "sec": r["sec"]}
    if r["exit"] not in EXIT_CODES.values():
        rec["status"] = "FAIL"
        rec["reason"] = f"退出码 {r['exit']} 不在契约集合 {{0,10,20,30,40}}: {r['err'].strip()[:300]}"
        rec["data"] = None
    else:
        try:
            data = json.loads(r["out"])
            rec["data"] = data
        except Exception as e:
            rec["status"] = "FAIL"
            rec["reason"] = (f"stdout 不是纯 JSON（C1 违规）：{e}；"
                             f"stdout 前 200 字: {r['out'][:200]!r}")
            rec["data"] = None
    if cache:
        _CLI_CACHE[key] = copy.deepcopy(rec)
    return rec


def cli_json(args, **kw):
    """向后兼容包装：返回 (rec, data)。"""
    rec = run_cli_json(args, **kw)
    return rec, rec.get("data")


def _fail(rec, msg):
    rec["status"] = "FAIL"
    rec["reason"] = msg
    return rec


# ---------------------------------------------------------------------------
# 基础契约
# ---------------------------------------------------------------------------
def check_json_contract(code="600519", fast=True):
    """G02（F21 已收紧）：纯 JSON + 字段齐备 + **真数据** + 退出码与 status 一致。"""
    args = [code, "--json"] + (["--fast"] if fast else [])
    rec = run_cli_json(args)
    data = rec.get("data")
    if data is None:
        rec["elapsed_sec"] = rec["sec"]
        return rec
    missing_top = [k for k in REQUIRED_TOP_KEYS if k not in data]
    missing_rec = [k for k in REQUIRED_REC_KEYS if k not in data.get("recommendation", {})]
    problems = []
    if data.get("schema_version") != "1.0":
        problems.append(f"schema_version={data.get('schema_version')!r} != '1.0'")
    if missing_top:
        problems.append(f"缺顶层字段 {missing_top}")
    if missing_rec:
        problems.append(f"缺 recommendation 字段 {missing_rec}")
    missing_data = [k for k in REQUIRED_DATA_KEYS if k not in (data.get("data") or {})]
    if missing_data:
        problems.append(f"缺 data 字段 {missing_data}")
    # F21：空壳文档不得判 PASS
    bars = (data.get("data") or {}).get("bars", 0)
    if not bars or bars <= 0:
        problems.append(f"股票 bars={bars}（无有效数据）")
    if not data.get("candidates"):
        problems.append("股票 candidates 为空")
    status = (data.get("recommendation") or {}).get("status")
    if status not in EXIT_CODES:
        problems.append(f"status={status!r} 不在契约枚举内")
    elif rec["exit"] != EXIT_CODES[status]:
        problems.append(f"退出码 {rec['exit']} 与 status={status} 不一致（应 {EXIT_CODES[status]}）")
    rec["status"] = "PASS" if not problems else "FAIL"
    rec["reason"] = "; ".join(problems)
    rec["elapsed_sec"] = rec["sec"]
    rec["keys"] = sorted(data.keys())
    return rec


def check_semantic_consistency(data):
    """C4：输出不得自相矛盾（历史上 StochRSI=100 却给"买入触发上穿 25"）。"""
    problems = []
    rec = data.get("recommendation", {})
    cands = data.get("candidates") or []
    triggers = (data.get("evidence") or {}).get("triggers") or []
    top_score = cands[0].get("score") if cands else None
    top_trades = cands[0].get("trade_count") if cands else None

    if not cands:
        problems.append("candidates 为空")
    if not triggers:
        problems.append("evidence.triggers 为空（触发器渲染退化）")
    if top_score is not None and top_score <= -100:
        if rec.get("status") == "ok":
            problems.append(f"首选候选评分 {top_score}（-100 判定为无效策略）却报 status=ok")
        if rec.get("action") == "buy":
            problems.append(f"首选候选评分 {top_score} 却给 action=buy")
    if rec.get("action") == "buy" and (rec.get("confidence") or 0) <= 0:
        problems.append("action=buy 但 confidence<=0")
    if rec.get("action") == "buy" and (data.get("evidence") or {}).get("significant") is False:
        problems.append("action=buy 但 evidence.significant=False（B4 要求不显著时不得给出买入）")
    return {
        "status": "FAIL" if problems else "PASS",
        "reason": "; ".join(problems),
        "observed": {
            "status": rec.get("status"), "action": rec.get("action"),
            "confidence": rec.get("confidence"), "top_score": top_score,
            "top_trade_count": top_trades, "triggers": len(triggers),
        },
    }


def check_fund_contract(code="510300", fast=True):
    """G04（F21 已收紧）：基金必须返回**有效**输出，空壳文档不得判 PASS。"""
    args = [code, "--json"] + (["--fast"] if fast else [])
    rec = run_cli_json(args)
    data = rec.get("data")
    if data is None:
        return rec
    problems = []
    if data.get("schema_version") != "1.0":
        problems.append("schema_version != '1.0'")
    missing = [k for k in REQUIRED_TOP_KEYS if k not in data]
    if missing:
        problems.append(f"缺字段 {missing}")
    missing_rec = [k for k in REQUIRED_REC_KEYS if k not in (data.get("recommendation") or {})]
    if missing_rec:
        problems.append(f"基金缺 recommendation 字段 {missing_rec}")
    missing_data = [k for k in REQUIRED_DATA_KEYS if k not in (data.get("data") or {})]
    if missing_data:
        problems.append(f"基金缺 data 字段 {missing_data}")
    bars = (data.get("data") or {}).get("bars", 0)
    if not bars or bars <= 0:
        problems.append(f"基金 bars={bars}（无有效数据）")
    if not data.get("candidates"):
        problems.append("基金 candidates 为空")
    ev = data.get("evidence") or {}
    if len(ev) < 10:
        problems.append(f"基金 evidence 仅 {len(ev)} 键")
    status = (data.get("recommendation") or {}).get("status")
    if status not in ("ok", "no_edge"):
        problems.append(f"基金 status={status}（期望 ok/no_edge；缓存缺失请先联网预热，见 README）")
    elif rec["exit"] != EXIT_CODES[status]:
        problems.append(f"基金退出码 {rec['exit']} 与 status={status} 不一致")
    rec["status"] = "PASS" if not problems else "FAIL"
    rec["reason"] = "; ".join(problems)
    rec["keys"] = sorted(data.keys())
    return rec


def check_idempotent(code="600519"):
    """C8：同一缓存数据下两次运行，剔除时间戳/耗时后必须逐字节相同。"""
    r1, d1 = cli_json([code, "--json", "--fast"], cache=False)
    if d1 is None:
        return {"status": "FAIL", "reason": "首次运行失败: " + r1.get("reason", "")}
    r2, d2 = cli_json([code, "--json", "--fast"], cache=False)
    if d2 is None:
        return {"status": "FAIL", "reason": "第二次运行失败: " + r2.get("reason", "")}
    for d in (d1, d2):
        d.pop("generated_at", None)
        d.get("diagnostics", {}).pop("elapsed_seconds", None)
    same = json.dumps(d1, sort_keys=True, ensure_ascii=False) == json.dumps(d2, sort_keys=True, ensure_ascii=False)
    diff = ""
    if not same:
        keys = set(d1) | set(d2)
        bad = [k for k in keys if json.dumps(d1.get(k), sort_keys=True, ensure_ascii=False)
               != json.dumps(d2.get(k), sort_keys=True, ensure_ascii=False)]
        diff = f"差异字段: {bad[:8]}"
    return {"status": "PASS" if same else "FAIL",
            "reason": "" if same else f"两次运行输出不同。{diff}", "sec": round(r1["sec"] + r2["sec"], 1)}


# ---------------------------------------------------------------------------
# F01 / F15 网格隔离
# ---------------------------------------------------------------------------
def check_fund_grid(fund_data=None):
    """G15：基金候选必须全部来自基金网格（F01）。"""
    sys.path.insert(0, SRC)
    os.chdir(SRC)
    from fund_analysis.fund_strategy.registry import build_fund_strategy_signals
    fund = set(build_fund_strategy_signals())
    if fund_data is None:
        rec, fund_data = cli_json(["510300", "--json", "--fast"])
        if fund_data is None:
            return {"status": "FAIL", "reason": "基金 CLI 无输出: " + rec.get("reason", "")}
    names = [c.get("name") for c in fund_data.get("candidates") or []]
    outside = [n for n in names if n not in fund]
    ok = bool(names) and not outside
    return {"status": "PASS" if ok else "FAIL",
            "reason": "" if ok else f"candidates={names[:5]}；不在基金网格: {outside[:5]}",
            "observed": {"n": len(names), "fund_grid": len(fund), "outside": len(outside)}}


def check_fund_grid_consistency():
    """G31：基金网格三方自洽（grid_size == _grid_defs 组合数 == signals 数 == 44，6 族）。"""
    sys.path.insert(0, SRC)
    os.chdir(SRC)
    from fund_analysis.fund_strategy import registry as r
    g = r._grid_defs()
    total = sum(len(x[2]) for x in g)
    signals = r.build_fund_strategy_signals()
    fams = {x[0] for x in g}
    problems = []
    if not (r.grid_size() == total == len(signals)):
        problems.append(f"三方不一致 grid_size={r.grid_size()} defs={total} signals={len(signals)}")
    if total != 44:
        problems.append(f"组合数 {total} != 契约 44")
    if len(fams) != 6:
        problems.append(f"族数 {len(fams)} != 契约 6")
    return {"status": "PASS" if not problems else "FAIL",
            "reason": "; ".join(problems),
            "observed": {"grid_size": r.grid_size(), "signals": len(signals),
                         "families": sorted(fams)}}


def check_family_map(stock_data=None):
    """G21：族名必须来自注册表 prefix（F14），且输出里的 family 都在族集合内。"""
    sys.path.insert(0, SRC)
    os.chdir(SRC)
    from stock_analysis.strategy.registry import family_map, _grid_defs
    m = family_map()
    fams = {x[0] for x in _grid_defs()}
    problems = []
    if len(m) != 581:
        problems.append(f"family_map 大小 {len(m)} != 581")
    unknown = [k for k, v in m.items() if v == "unknown"]
    if unknown:
        problems.append(f"{len(unknown)} 个策略族未知，如 {unknown[:3]}")
    if stock_data is not None:
        bad = [c.get("family") for c in stock_data.get("candidates") or []
               if c.get("family") not in fams]
        if bad:
            problems.append(f"candidates 出现非注册表族: {bad[:5]}")
    return {"status": "PASS" if not problems else "FAIL",
            "reason": "; ".join(problems),
            "observed": {"map_size": len(m), "families": len(fams)}}


# ---------------------------------------------------------------------------
# F03 / F04 统计口径
# ---------------------------------------------------------------------------
def check_n_trials(stock_data=None, fund_data=None):
    """G17：n_trials 必须 ≥ 实际参与评分的配置数（股票 581 / 基金 44）。"""
    problems = []
    obs = {}
    if stock_data is not None:
        n = (stock_data.get("evidence") or {}).get("n_trials")
        obs["stock_n_trials"] = n
        if not isinstance(n, int) or n < 500:
            problems.append(f"股票 n_trials={n} < 500（疑似仍在用候选数）")
    if fund_data is not None:
        n = (fund_data.get("evidence") or {}).get("n_trials")
        obs["fund_n_trials"] = n
        if not isinstance(n, int) or n < 30:
            problems.append(f"基金 n_trials={n} < 30")
    if not obs:
        return {"status": "BLOCKED", "reason": "无可用输出"}
    return {"status": "PASS" if not problems else "FAIL",
            "reason": "; ".join(problems), "observed": obs}


def check_significance(stock_data=None):
    """G18：significant / no_edge 必须与超额收益 CI 自洽（F04/B8）。"""
    if stock_data is None:
        return {"status": "BLOCKED", "reason": "无股票输出"}
    ev = stock_data.get("evidence") or {}
    rec = stock_data.get("recommendation") or {}
    exc = ev.get("excess_vs_buy_hold") or {}
    ci = exc.get("ci_95") or [0.0, 0.0]
    dsr = ev.get("deflated_sharpe")
    sig = ev.get("significant")
    problems = []
    expect_sig = bool((dsr or 0) > 0 and ci[1] > 0)
    if bool(sig) != expect_sig:
        problems.append(f"significant={sig} 与 (dsr>0 and ci_hi>0)={expect_sig} 矛盾"
                        f"（dsr={dsr}, ci_hi={ci[1]}）")
    if rec.get("no_edge") != (rec.get("status") == "no_edge"):
        problems.append(f"no_edge={rec.get('no_edge')} 与 status={rec.get('status')} 矛盾")
    if rec.get("status") == "ok" and ci[1] <= 0:
        problems.append(f"status=ok 但超额收益 CI 上界 {ci[1]} <= 0（B8 要求 no_edge）")
    return {"status": "PASS" if not problems else "FAIL",
            "reason": "; ".join(problems),
            "observed": {"significant": sig, "dsr": dsr, "ci_hi": ci[1],
                         "status": rec.get("status"), "no_edge": rec.get("no_edge")}}


def check_placeholders(stock_data=None, fund_data=None):
    """G20：禁止占位常量（F06）。跨标的完全相同 = 占位；score_oos/score_is 不得恒为 0.9。"""
    if stock_data is None or fund_data is None:
        return {"status": "BLOCKED", "reason": "缺少股票或基金输出"}
    problems = []
    obs = {}
    for label, data in (("stock", stock_data), ("fund", fund_data)):
        cands = data.get("candidates") or []
        ratios = []
        for c in cands:
            s_is = c.get("score_is")
            s_oos = c.get("score_oos")
            if s_is and s_oos is not None:
                ratios.append(s_oos / s_is)
        obs[f"{label}_ratios"] = [round(r, 4) for r in ratios[:3]]
        bad = [r for r in ratios if abs(r - 0.9) < 1e-9]
        if bad:
            problems.append(f"{label} 有 {len(bad)} 个候选 score_oos/score_is 精确等于 0.9（伪造 OOS）")

    def _sig(data):
        rec = data.get("recommendation") or {}
        ev = data.get("evidence") or {}
        g = data.get("diagnostics") or {}
        dist = rec.get("distance_to_trigger") or {}
        return {
            "dist": (dist.get("buy_pct"), dist.get("sell_pct")),
            "regime_wr": ev.get("regime_win_rates"),
            "zero_trade": g.get("zero_trade_configs"),
            "invalidation": rec.get("invalidation"),
        }

    s_sig, f_sig = _sig(stock_data), _sig(fund_data)
    obs["stock"] = s_sig
    obs["fund"] = f_sig
    if s_sig["dist"] == f_sig["dist"]:
        problems.append(f"distance_to_trigger 跨标的完全相同 {s_sig['dist']}（占位常量）")
    if s_sig["regime_wr"] == f_sig["regime_wr"]:
        problems.append(f"regime_win_rates 跨标的完全相同 {s_sig['regime_wr']}（占位常量）")
    if s_sig["invalidation"] == f_sig["invalidation"]:
        problems.append("invalidation 跨标的完全相同（占位常量）")
    return {"status": "PASS" if not problems else "FAIL",
            "reason": "; ".join(problems), "observed": obs}


# ---------------------------------------------------------------------------
# F10 / F11 指标
# ---------------------------------------------------------------------------
def check_metrics_ci(stock_data=None):
    """G23：metrics 中 ≥5/9 项有 ci_95，且点估计落在区间内。"""
    if stock_data is None:
        return {"status": "BLOCKED", "reason": "无股票输出"}
    metrics = (stock_data.get("evidence") or {}).get("metrics") or {}
    have = {k: v for k, v in metrics.items() if (v or {}).get("ci_95")}
    problems = []
    if len(have) < 5:
        problems.append(f"仅 {len(have)}/{len(metrics)} 项有 ci_95（要求 ≥5）")
    for k, v in have.items():
        lo, hi = v["ci_95"]
        if k == "max_drawdown":
            continue
        if not (lo <= v["point"] <= hi):
            problems.append(f"{k}: point={v['point']} 不在 ci_95={v['ci_95']} 内")
    return {"status": "PASS" if not problems else "FAIL",
            "reason": "; ".join(problems),
            "observed": {"with_ci": sorted(have.keys()), "total": len(metrics)}}


def check_time_in_market(stock_data=None):
    """G24：time_in_market 不得恒 0（F11/A4）。"""
    if stock_data is None:
        return {"status": "BLOCKED", "reason": "无股票输出"}
    tim = ((stock_data.get("evidence") or {}).get("metrics") or {}).get("time_in_market") or {}
    point = tim.get("point")
    ok = isinstance(point, (int, float)) and point > 0
    return {"status": "PASS" if ok else "FAIL",
            "reason": "" if ok else f"time_in_market.point={point}（恒 0 说明 holding_bar_count 未累加）",
            "observed": {"time_in_market": point}}


def check_win_rate_contribution(stock_data=None):
    """G26：胜率项对 |总分| 的贡献占比 ≤ 40%（B1，before=72.8%）。"""
    if stock_data is None:
        return {"status": "BLOCKED", "reason": "无股票输出"}
    sys.path.insert(0, SRC)
    os.chdir(SRC)
    from decision.scoring import score_components
    cands = (stock_data.get("candidates") or [])[:12]
    if not cands:
        return {"status": "FAIL", "reason": "candidates 为空"}
    tot_win = tot_cal = tot_stab = 0.0
    for c in cands:
        comp = score_components(c.get("annual_return") or 0.0,
                                c.get("max_drawdown") or 0.0,
                                c.get("win_rate") or 0.0,
                                int(c.get("trade_count") or 0))
        tot_win += abs(comp["win_rate_term"])
        tot_cal += abs(comp["calmar_term"])
        tot_stab += abs(comp["stability_term"])
    denom = tot_win + tot_cal + tot_stab
    share = (tot_win / denom) if denom else 1.0
    ok = share <= 0.40
    return {"status": "PASS" if ok else "FAIL",
            "reason": "" if ok else f"胜率项占比 {share:.1%} > 40%（B1 未生效）",
            "observed": {"win_share": round(share, 4), "n": len(cands)}}


# ---------------------------------------------------------------------------
# F05 / F02 / F16 接口
# ---------------------------------------------------------------------------
def check_exit_codes(stock_rec=None):
    """G19：退出码与 status 一一对应（0 / 30 两个分支必须实测到）。"""
    problems = []
    obs = {}
    if stock_rec is not None:
        data = stock_rec.get("data") or {}
        st = (data.get("recommendation") or {}).get("status")
        obs["600519"] = {"exit": stock_rec["exit"], "status": st}
        if st in EXIT_CODES and stock_rec["exit"] != EXIT_CODES[st]:
            problems.append(f"600519 exit={stock_rec['exit']} 与 status={st} 不符")
    rec = run_cli_json(["999999", "--json", "--fast"], cache=False, timeout=180)
    data = rec.get("data") or {}
    st = (data.get("recommendation") or {}).get("status")
    obs["999999"] = {"exit": rec["exit"], "status": st}
    if rec["exit"] != 30:
        problems.append(f"999999 退出码 {rec['exit']}（期望 30=data_source_error），status={st}")
    if st != "data_source_error":
        problems.append(f"999999 status={st}（期望 data_source_error）")
    return {"status": "PASS" if not problems else "FAIL",
            "reason": "; ".join(problems), "observed": obs}


def check_type_hint():
    """G16：--type 必须生效（600519 --type fund 的 request.type 必须为 fund）。"""
    rec = run_cli_json(["600519", "--type", "fund", "--json", "--fast"], cache=False, timeout=180)
    data = rec.get("data")
    if data is None:
        return {"status": "FAIL", "reason": "无 JSON 输出: " + rec.get("reason", "")}
    req = data.get("request") or {}
    ok = req.get("type") == "fund" and req.get("type_hint") == "fund"
    return {"status": "PASS" if ok else "FAIL",
            "reason": "" if ok else f"request.type={req.get('type')} type_hint={req.get('type_hint')}",
            "observed": {"type": req.get("type"), "type_hint": req.get("type_hint"),
                         "exit": rec["exit"]}}


def check_old_args():
    """G29：D6 旧参数**端到端真跑**（subprocess，非进程内 parse_args）。

    契约 D6 要求"真跑并断言 returncode ∈ {0,10,20,30,40}"。旧实现只做进程内
    `build_parser().parse_args()` —— 只能证明 argparse **认识**该参数，不能证明参数
    被接线到行为（argparse 收下却无人使用也照样 PASS），是判定强度的实质弱化。

    现改为：每个旧参数组合都启动**真实子进程**。用 `--start <近端日期>` 把窗口压到
    <100 根，使其在"数据不足"分支秒级返回（rc=20），从而以极低成本同时验证
    「参数被接受」+「端到端跑通」+「退出码合法且落在契约集合内」。
    另外两条行为断言，防止"参数被解析但未接线"：
      * `--detail` 的 stdout 行数必须多于基准（真有效应）；
      * `--json` 的 stdout 必须能被 json.loads 解析（纯 JSON，C1）。
    """
    sys.path.insert(0, SRC)
    py = sys.executable
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    base = ["600519", "--start", "20260701"]
    cases = [["--detail"], ["--no-regime"], ["--opt-iters", "8"], ["--jobs", "2"],
             ["--opt", "grid"], ["-t", "stock"], ["--json"], ["--fast"]]
    allowed = {0, 10, 20, 30, 40}
    problems, rc_map, stdout_map = [], {}, {}
    for extra in cases:
        key = " ".join(extra)
        try:
            p = subprocess.run([py, "cli.py"] + base + extra, cwd=SRC,
                               capture_output=True, text=True, encoding="utf-8",
                               errors="replace", env=env, timeout=180)
        except subprocess.TimeoutExpired:
            problems.append(f"{key} 超时(>180s)")
            continue
        rc_map[key] = p.returncode
        stdout_map[key] = p.stdout
        if p.returncode == 2:
            problems.append(f"{key} usage error(2): {(p.stderr or '').strip()[:120]}")
        elif p.returncode not in allowed:
            problems.append(f"{key} 退出码 {p.returncode} 不在 {sorted(allowed)}")
        elif p.returncode != 20:
            problems.append(f"{key} 退出码 {p.returncode}（72 根应判 20=insufficient_data）")

    # --detail 必须真的有行为差异
    base_lines = len((stdout_map.get("--fast") or "").splitlines())
    detail_lines = len((stdout_map.get("--detail") or "").splitlines())
    if base_lines and detail_lines <= base_lines:
        problems.append(f"--detail 未增加输出（{detail_lines} 行 vs 基准 {base_lines} 行）")

    # --json 必须是纯 JSON
    try:
        json.loads(stdout_map.get("--json") or "")
    except Exception as e:
        problems.append(f"--json stdout 非纯 JSON: {e}")

    return {"status": "PASS" if not problems else "FAIL",
            "reason": "; ".join(problems),
            "observed": {"returncodes": rc_map, "detail_lines": detail_lines,
                         "base_lines": base_lines, "cases": len(cases)}}


def check_start_window():
    """G34：`--start` 必须真正生效，且不得把缓存文件截断。

    本轮查出的两个真缺陷：
      1. 缓存命中路径完全不看 start_date —— `--start 20260701` 与不带 start
         返回逐字节相同的全量数据，参数形同虚设；
      2. 拉取路径把 `df[df.index >= start]` 的结果写回缓存 —— 一次带 --start 的
         调用就会把更早的历史永久削掉。
    判据：短窗口行数必须 < 全量行数、窗口首日 >= start、缓存文件行数不得减少。
    """
    sys.path.insert(0, SRC)
    os.chdir(SRC)
    import pandas as pd
    from stock_analysis.data_loader import get_stock_daily
    cache = os.path.join(SRC, "stock_analysis", "data", "stock_600519_qfq.csv")

    def cache_rows():
        if not os.path.exists(cache):
            return None
        with open(cache, "r", encoding="utf-8", errors="replace") as f:
            return max(sum(1 for _ in f) - 1, 0)

    before = cache_rows()
    full = get_stock_daily("600519", start_date="20200101", source="sina")
    win = get_stock_daily("600519", start_date="20260701", source="sina")
    after = cache_rows()

    problems = []
    obs = {"cache_rows_before": before, "cache_rows_after": after}
    if full is None or win is None:
        problems.append("取数为空")
    else:
        obs["full_bars"], obs["win_bars"] = len(full), len(win)
        if len(win) >= len(full):
            problems.append(f"--start 未生效：窗口 {len(win)} 根 >= 全量 {len(full)} 根")
        if len(win) == 0:
            problems.append("窗口内 0 根")
        elif pd.Timestamp(win.index[0]) < pd.Timestamp("2026-07-01"):
            problems.append(f"窗口首日 {win.index[0]} 早于 --start 2026-07-01")
    if before is not None and after is not None and after < before:
        problems.append(f"缓存被 --start 截断：{before} 行 → {after} 行")
    return {"status": "PASS" if not problems else "FAIL",
            "reason": "; ".join(problems), "observed": obs}


def _shape_paths(obj, path=""):
    """把 JSON 文档展开成路径集合（列表取首个元素的形状）。"""
    out = set()
    if isinstance(obj, dict):
        for k, v in obj.items():
            p = f"{path}/{k}"
            out.add(p)
            out |= _shape_paths(v, p)
    elif isinstance(obj, list) and obj:
        out |= _shape_paths(obj[0], path + "[]")
    return out


def check_error_shape():
    """G35：C3 —— 错误响应必须与成功响应**同构**（不适用处显式 null，不得整块缺失）。

    历史问题：错误路径只输出 46 个键、成功路径 134 个，缺 88 条路径
    （evidence.metrics / n_trials / significant / triggers / data 8 字段 / stop_method …），
    Hermes 只能按 status 分支写防御性解析；且 data.bars 恒 0，与同一文档里的
    invalidation="仅 72 根 < 100" 自相矛盾。

    判据：
      1. 成功路径的每条路径（`candidates[]` / `triggers[]` 的元素路径除外，
         因为"没有候选/没有触发器"时列表为空才是正确的）在错误路径必须存在；
      2. data.bars 必须等于 invalidation 文本里的根数，且不得为 0；
      3. 不适用字段必须是显式 null（horizon_days=[null,null]、stop_method=null、
         metrics 9 项 point 全 null），不得编造 [1,5] 之类的值。
    """
    import re
    from datetime import date, timedelta

    import re
    from datetime import date, timedelta

    problems = []
    observed = {}
    # 用近端 --start 造"数据太短"场景：约 28 个交易日 → insufficient_data(20)
    start = (date.today() - timedelta(days=40)).strftime("%Y%m%d")
    # 2026-10-18 加强：原先只跑股票侧，基金侧"走同一函数"仅是结构性保证。
    # 基金的数据源/board/price_limit 与股票不同，必须独立覆盖，否则同构回归查不到。
    for code, want_board in (("600519", None), ("510300", "fund")):
        ok_rec = run_cli_json([code, "--json", "--fast"])
        ok = ok_rec.get("data")
        if not ok:
            return {"status": "BLOCKED",
                    "reason": f"{code} 成功路径无 JSON，无法比对结构"}
        err_rec = run_cli_json([code, "--json", "--fast", "--start", start],
                               cache=False)
        err = err_rec.get("data")
        if not err:
            problems.append(f"{code} 错误路径无 JSON（exit={err_rec.get('exit')}）")
            continue
        rec = err.get("recommendation") or {}
        status = rec.get("status")
        observed[code] = {"err_exit": err_rec.get("exit"), "err_status": status,
                          "ok_paths": len(_shape_paths(ok)),
                          "err_paths": len(_shape_paths(err))}
        if status != "insufficient_data":
            problems.append(
                f"{code} --start {start} 未触发 insufficient_data（实际 {status}），用例前提不成立")
            continue

        missing = sorted(_shape_paths(ok) - _shape_paths(err))
        missing = [p for p in missing
                   if not p.startswith(("/candidates[]", "/evidence/triggers[]"))]
        if missing:
            problems.append(f"{code} 错误路径缺 {len(missing)} 条路径（前 6 条）: {missing[:6]}")
        observed[code]["missing_paths"] = len(missing)

        bars = (err.get("data") or {}).get("bars")
        observed[code]["bars"] = bars
        m = re.search(r"(\d+)\s*根", str(rec.get("invalidation", "")))
        if not bars:
            problems.append(
                f"{code} data.bars 为 0（数据太短时根数是已知真实值，不得填 0）")
        elif m and int(m.group(1)) != int(bars):
            problems.append(
                f"{code} data.bars={bars} 与 invalidation 文本的 {m.group(1)} 根不一致")

        if rec.get("horizon_days") != [None, None]:
            problems.append(
                f"{code} horizon_days={rec.get('horizon_days')}（无回测时应为显式 [null,null]）")
        if rec.get("stop_method") is not None:
            problems.append(
                f"{code} stop_method={rec.get('stop_method')}（无回测时应为 null）")
        metrics = (err.get("evidence") or {}).get("metrics") or {}
        if len(metrics) != 9:
            problems.append(f"{code} evidence.metrics 应为 9 项，实际 {len(metrics)} 项")
        elif any((v or {}).get("point") is not None for v in metrics.values()):
            problems.append(
                f"{code} evidence.metrics 存在非 null 的 point（无回测时不得有数值）")
        if (err.get("evidence") or {}).get("n_trials") != 0:
            problems.append(f"{code} evidence.n_trials 应为 0（未评估任何配置）")

        # 基金侧额外断言：错误响应里的板块分类也必须走 classify_board()
        if want_board is not None:
            got_board = (err.get("instrument") or {}).get("board")
            if got_board != want_board:
                problems.append(
                    f"{code} 错误路径 instrument.board={got_board}（期望 {want_board}）")
            got_limit = (err.get("instrument") or {}).get("price_limit_pct")
            if got_limit is None:
                problems.append(f"{code} 错误路径 instrument.price_limit_pct 为 null")

    return {"status": "PASS" if not problems else "FAIL",
            "reason": "; ".join(problems),
            "observed": observed}


def check_degrade_visibility():
    """G33：数据降级（触网失败→回退旧缓存）必须对调用方可见。

    旧行为：`data.from_cache=True` 同时覆盖"新鲜命中"与"拉取失败兜底"两种情况，
    Hermes 无法判断结论是否建立在陈旧数据上 —— 这是**输出撒谎**，不是风格问题。

    两段验证：
      1. data_loader 层：让期望交易日远在未来（使缓存"过期"）+ 让 akshare 抛错，
         必然走降级回退，断言 degraded/cache_last_date/lag_days/degraded_reason 齐备；
      2. cli 层：把上述降级状态注入后跑一次 advise_internal（用 3 个策略把网格
         压到秒级），断言 data.stale=True 且 diagnostics.warnings 里出现降级说明。
    """
    sys.path.insert(0, SRC)
    os.chdir(SRC)
    import datetime
    import akshare as ak
    from stock_analysis.data_loader import meta

    orig_expected = meta._expected_latest_trade_date
    orig_pool = meta._STOCK_DATA_SOURCE_POOL
    problems, obs = [], {}
    try:
        # --- 1) 真实走一遍降级路径（不改任何文件：只让缓存"看起来过期"）
        meta._expected_latest_trade_date = lambda: datetime.date(2030, 1, 1)

        # 2026-10-18 修正：原来只让 `ak.stock_zh_a_hist`（东财）抛错来模拟断网，
        # 但 data_loader 现已接入三通道渠道池（eastmoney → sina_stock → sina_fund），
        # 东财失败后新浪会成功取到**新鲜数据**，根本不构成降级 —— 测试前提失效。
        # 断网的真实语义是"所有通道都失败"，故整体替换渠道池。
        class _DeadPool:
            _states = []

            def fetch(self, symbol, start_date, adjust, preferred=None):
                print("[probe] 模拟全部通道不可用", file=sys.stderr)
                return None, None

        meta._STOCK_DATA_SOURCE_POOL = _DeadPool()
        df = meta.get_stock_daily("600519", source="sina")
        info = meta.last_fetch_info()
    finally:
        meta._expected_latest_trade_date = orig_expected
        meta._STOCK_DATA_SOURCE_POOL = orig_pool

    obs["loader"] = {k: info.get(k) for k in
                     ("source", "from_cache", "degraded", "cache_last_date",
                      "lag_days", "expected_date")}
    if df is None or len(df) < 100:
        problems.append("降级回退未返回可用数据")
    if not info.get("degraded"):
        problems.append("degraded 未置 True（降级对调用方不可见）")
    if info.get("source") != "cache":
        problems.append(f"source={info.get('source')}（期望 cache）")
    if not info.get("cache_last_date"):
        problems.append("cache_last_date 为空")
    if not info.get("lag_days") or info.get("lag_days") <= 0:
        problems.append(f"lag_days={info.get('lag_days')}（期望 >0）")
    if not info.get("degraded_reason"):
        problems.append("degraded_reason 为空")

    # --- 2) cli 必须把降级写进 warnings 与 data.stale
    try:
        import cli
        from stock_analysis.strategy.registry import build_strategy_signals
        full = build_strategy_signals()
        # 每个族取 1 个，把 581 组压到 3 组（约 1s），只为验证接线，不影响 G22/G26
        fams, picks = {}, []
        for name in full:
            fam = name.split("(")[0]
            if fam not in fams:
                fams[fam] = name
                picks.append(name)
            if len(picks) >= 3:
                break
        orig_build = cli.build_strategy_signals
        orig_get = cli.get_stock_daily
        try:
            cli.build_strategy_signals = lambda: {k: full[k] for k in picks}
            cli.get_stock_daily = lambda *a, **k: df
            meta._record_fetch("cache", True, degraded=True,
                               degraded_reason="probe:simulated stale cache",
                               cache_last_date=info.get("cache_last_date"),
                               lag_days=info.get("lag_days"),
                               expected_date=info.get("expected_date"))
            resp = cli.advise_internal("600519", fast=True)
        finally:
            cli.build_strategy_signals = orig_build
            cli.get_stock_daily = orig_get
        data = resp.get("data") or {}
        warns = (resp.get("diagnostics") or {}).get("warnings") or []
        obs["cli"] = {"stale": data.get("stale"),
                      "lag_days": data.get("lag_days"),
                      "warn_hit": any("降级" in str(w) for w in warns)}
        if data.get("stale") is not True:
            problems.append(f"data.stale={data.get('stale')}（期望 True）")
        if not any("降级" in str(w) for w in warns):
            problems.append("diagnostics.warnings 未包含降级说明（Hermes 无从知晓数据陈旧）")
    except Exception as e:
        problems.append(f"cli 接线验证异常: {type(e).__name__}: {e}")

    return {"status": "PASS" if not problems else "FAIL",
            "reason": "; ".join(problems), "observed": obs}


# ---------------------------------------------------------------------------
# F12 / F08 / F09 风险与优化
# ---------------------------------------------------------------------------
def check_sizing():
    """G25：sizing 单一真源 —— 模块存在且 cli.py 无硬编码止损/止盈字面量。"""
    sys.path.insert(0, SRC)
    os.chdir(SRC)
    problems = []
    try:
        from decision import sizing
    except Exception as e:
        return {"status": "FAIL", "reason": f"decision.sizing 不可导入: {e}"}
    fns = [n for n in dir(sizing) if n.startswith("derive_")]
    if len(fns) < 4:
        problems.append(f"derive_* 只有 {fns}")
    # 行为校验：ATR 模式下止损距离必须由 ATR 驱动（而非固定 5%）
    # 取 ATR=3.0/价格=100 → 2.5*3/100=7.5%，与固定 8% 不同，可区分"真用 ATR"与"硬编码"
    s_atr = sizing.derive_stops(100.0, 3.0, None)
    s_fixed = sizing.derive_stops(100.0, None, None)
    if s_atr.get("method") != "atr":
        problems.append(f"有 ATR 时 method={s_atr.get('method')}（期望 atr）")
    if abs(s_atr["stop_distance"] - s_fixed["stop_distance"]) < 1e-9:
        problems.append(f"ATR 与固定模式止损距离相同 {s_atr['stop_distance']}（ATR 未生效）")
    if abs(s_atr["stop_distance"] - 0.05) < 1e-9:
        problems.append("ATR 止损距离恰好 0.05（疑似硬编码）")
    if not (0.03 <= s_fixed["stop_distance"] <= 0.20):
        problems.append(f"无 ATR 时 stop_distance={s_fixed['stop_distance']} 越界")
    cli_path = os.path.join(SRC, "cli.py")
    src = open(cli_path, encoding="utf-8").read()
    for pat in ("last_close * 0.95", "last_close * 1.10", "stop_distance = 0.05"):
        if pat in src:
            problems.append(f"cli.py 仍含硬编码 `{pat}`")
    return {"status": "PASS" if not problems else "FAIL",
            "reason": "; ".join(problems),
            "observed": {"derive_fns": fns, "atr_stop": s_atr["stop_loss"],
                         "fixed_stop": s_fixed["stop_loss"]}}


def _synthetic_ramp(vol, n=200):
    import numpy as np
    import pandas as pd
    idx = pd.date_range("2020-01-01", periods=n, freq="B")
    close = np.concatenate([np.linspace(100, 110, 60), np.linspace(110, 70, n - 60)])
    return pd.DataFrame({"open": close, "high": close * (1 + vol),
                         "low": close * (1 - vol), "close": close,
                         "volume": 1e6}, index=idx)


def _stop_distance_for(vol, stop_mode):
    import pandas as pd
    from stock_analysis.backtest.engine import BacktestEngine, BacktestConfig
    df = _synthetic_ramp(vol)
    buy = pd.Series(False, index=df.index)
    buy.iloc[30] = True          # ATR 预热后再入场
    sell = pd.Series(False, index=df.index)
    cfg = BacktestConfig(initial_capital=100000, max_position_pct=1.0,
                         stop_mode=stop_mode, enable_take_profit=False)
    r = BacktestEngine(cfg).run_backtest(df, lambda d: {"buy": buy, "sell": sell})
    stops = [t for t in r.trades if t.exit_reason == "止损"]
    if not stops:
        return None
    t = stops[0]
    return (t.entry_price - t.exit_price) / t.entry_price


def check_atr_stop():
    """G27：ATR 自适应止损生效（高低波动段止损距离差异 > 20%）。"""
    sys.path.insert(0, SRC)
    os.chdir(SRC)
    from stock_analysis.backtest.engine import BacktestConfig
    problems = []
    cfg = BacktestConfig()
    if cfg.stop_mode != "atr":
        problems.append(f"默认 stop_mode={cfg.stop_mode}（契约要求 atr）")
    low = _stop_distance_for(0.004, "atr")
    high = _stop_distance_for(0.030, "atr")
    if low is None or high is None:
        problems.append(f"止损未触发（low={low}, high={high}）")
    elif not (high > low * 1.2):
        problems.append(f"高低波动止损距离差异不足 20%：low={low:.4f} high={high:.4f}")
    return {"status": "PASS" if not problems else "FAIL",
            "reason": "; ".join(problems),
            "observed": {"low_vol_stop": None if low is None else round(low, 4),
                         "high_vol_stop": None if high is None else round(high, 4)}}


def check_walk_forward():
    """G28：B3 逐折重新寻优（walk_forward_optimize 可导入且各折最优不全相同）。"""
    sys.path.insert(0, SRC)
    os.chdir(SRC)
    import numpy as np
    import pandas as pd
    from stock_analysis.backtest import analyzer as az
    from stock_analysis.backtest.engine import BacktestConfig
    from stock_analysis.strategy.registry import build_strategy_signals
    if not hasattr(az, "walk_forward_optimize"):
        return {"status": "FAIL", "reason": "analyzer.walk_forward_optimize 不存在"}
    n = 600
    idx = pd.date_range("2020-01-01", periods=n, freq="B")
    close = np.concatenate([np.linspace(100, 180, n // 2),
                            180 * np.exp(-np.linspace(0, 1.2, n - n // 2))])
    df = pd.DataFrame({"open": close, "high": close * 1.01, "low": close * 0.99,
                       "close": close, "volume": 1e6}, index=idx)
    grid = build_strategy_signals()
    subset = [k for k in grid if k.split('(')[0] in
              ("唐奇安", "均线趋势", "MACD", "KAMA", "布林", "StochRSI")][:12]
    old = az.MIN_TRADE_COUNT
    az.MIN_TRADE_COUNT = 3   # 与 pytest 同口径：验证机制，不受最少交易数地板干扰
    try:
        out = az.walk_forward_optimize(df, {k: grid[k] for k in subset},
                                       config=BacktestConfig(max_position_pct=1.0), n_splits=3)
    finally:
        az.MIN_TRADE_COUNT = old
    bests = [f["best"] for f in out.get("folds") or []]
    ok = len(bests) >= 2 and len(set(bests)) > 1
    return {"status": "PASS" if ok else "FAIL",
            "reason": "" if ok else f"各折最优: {bests}（应不完全相同）",
            "observed": {"folds": bests}}


# ---------------------------------------------------------------------------
# F17 / F18 / F20 工程
# ---------------------------------------------------------------------------
def check_requirements():
    """G30：requirements pin 与实测环境一致（D3(e)/F17）。"""
    import importlib.metadata as md
    import re
    path = os.path.join(SRC, "requirements.txt")
    txt = open(path, encoding="utf-8").read()
    pins = dict(re.findall(r"^([a-zA-Z0-9_\-]+)==([\w\.]+)", txt, re.M))
    problems = []
    obs = {}
    for pkg, want in pins.items():
        try:
            have = md.version(pkg)
        except Exception as e:
            problems.append(f"{pkg} 未安装: {e}")
            continue
        obs[pkg] = {"pin": want, "actual": have}
        if have != want:
            problems.append(f"{pkg} pin={want} 实际={have}")
    for pkg in ("akshare", "numpy", "pandas", "pytest", "requests"):
        if pkg not in pins:
            problems.append(f"{pkg} 未 pin")
    return {"status": "PASS" if not problems else "FAIL",
            "reason": "; ".join(problems), "observed": obs}


def check_fund_cache_guard():
    """G32：失败拉取不得留下空壳缓存（F20）。"""
    sys.path.insert(0, SRC)
    os.chdir(SRC)
    import pandas as pd
    import akshare as ak
    from stock_analysis.data_loader import meta
    cache = os.path.join(SRC, "stock_analysis", "data", "fund_999999.csv")
    if os.path.exists(cache):
        os.remove(cache)
    orig = getattr(ak, "fund_etf_hist_sina", None)
    try:
        ak.fund_etf_hist_sina = lambda **k: pd.DataFrame()
        meta.get_fund_daily("999999")
    except Exception as e:
        return {"status": "FAIL", "reason": f"探针异常: {e}"}
    finally:
        if orig is not None:
            ak.fund_etf_hist_sina = orig
    created = os.path.exists(cache)
    size = os.path.getsize(cache) if created else 0
    if created:
        try:
            os.remove(cache)
        except Exception:
            pass
    return {"status": "PASS" if not created else "FAIL",
            "reason": "" if not created else f"失败拉取创建了空壳缓存（{size} 字节）",
            "observed": {"cache_created": created, "size": size}}


# ---------------------------------------------------------------------------
# 既有：三窗口 / A1 / C6
# ---------------------------------------------------------------------------
def _load_cli_and_full_df(code="600519"):
    sys.path.insert(0, SRC)
    os.chdir(SRC)
    import cli  # noqa
    df = cli.get_stock_daily(code, start_date=None, source="sina")
    return cli, df


def check_windows(code="600519"):
    """B6：full / drop_last_60 / drop_last_120 三个窗口的 action 与共识族必须一致。"""
    cli, df = _load_cli_and_full_df(code)
    if df is None or len(df) < 300:
        return {"status": "BLOCKED", "reason": "本地缓存不足，无法切三窗口"}
    orig = cli.get_stock_daily
    results = {}
    try:
        for label, drop in (("full", 0), ("drop60", 60), ("drop120", 120)):
            sub = (df.iloc[: len(df) - drop] if drop else df).copy()
            cli.get_stock_daily = (lambda d: (lambda *a, **k: d))(sub)
            one = cli.advise_internal(code, start=None, fast=True)
            results[label] = {
                "action": one["recommendation"]["action"],
                "family": (one.get("evidence") or {}).get("consensus", {}).get("family"),
                "status": one["recommendation"]["status"],
                "claimed_stability": (one.get("evidence") or {}).get("stability"),
            }
    finally:
        cli.get_stock_daily = orig
    actions = {v["action"] for v in results.values()}
    families = {v["family"] for v in results.values()}
    problems = []
    if len(actions) != 1:
        problems.append(f"action 跨窗口不一致: {actions}")
    if len(families) != 1:
        problems.append(f"共识族跨窗口不一致: {families}")
    claimed = [v["claimed_stability"] for v in results.values() if v["claimed_stability"]]
    if claimed and problems:
        problems.append("evidence.stability 声称稳定，但实测不稳定（自评与实际不符）")
    return {"status": "PASS" if not problems else "FAIL",
            "reason": "; ".join(problems), "observed": results}


def check_a1_invariant(code="600519"):
    """A1 结构性不变式：自适应资金下 100% 的 K 线买得起一手。"""
    sys.path.insert(0, SRC)
    os.chdir(SRC)
    from stock_analysis.backtest.analyzer import adaptive_oos_config
    from stock_analysis.backtest.engine import BacktestConfig, BacktestEngine
    import pandas as pd
    path = os.path.join(SRC, "stock_analysis", "data", f"stock_{code}_qfq.csv")
    if not os.path.exists(path):
        return {"status": "BLOCKED", "reason": f"缺缓存 {path}"}
    df = pd.read_csv(path, index_col="date", parse_dates=True)
    base = BacktestConfig(initial_capital=100000, max_position_pct=1.0)
    cfg = adaptive_oos_config(df, base)
    eng = BacktestEngine(cfg)
    bad = [float(p) for p in df["close"] if eng._calc_max_shares(cfg.initial_capital, float(p)) == 0]
    pct = 100.0 * (1 - len(bad) / max(len(df), 1))
    return {"status": "PASS" if not bad else "FAIL",
            "reason": "" if not bad else f"仅 {pct:.1f}% 可负担，{len(bad)} 根买不起一手",
            "observed": {"bars": len(df), "adaptive_capital": round(cfg.initial_capital, 2),
                         "affordable_pct": round(pct, 2)}}


def check_advise_keys(code="600519"):
    """C6：decision.advise() 的键集合必须与 --json 输出的键集合完全相同。"""
    rec, data = cli_json([code, "--json", "--fast"])
    if data is None:
        return {"status": "FAIL", "reason": "CLI 运行失败: " + rec.get("reason", "")}
    sys.path.insert(0, SRC)
    os.chdir(SRC)
    try:
        from decision import advise
    except Exception as e:
        return {"status": "FAIL", "reason": f"decision.advise 不可导入: {e}"}
    try:
        api = advise(code, fast=True)
    except Exception as e:
        return {"status": "FAIL", "reason": f"decision.advise() 调用失败: {e}"}
    a, b = set(api.keys()), set(data.keys())
    ok = a == b
    return {"status": "PASS" if ok else "FAIL",
            "reason": "" if ok else f"仅 CLI 有 {sorted(b - a)}；仅 API 有 {sorted(a - b)}"}


# ---------------------------------------------------------------------------
# 汇总
# ---------------------------------------------------------------------------
QUICK_SKIP = ("fund", "idempotent", "windows", "exit_codes", "type_hint", "fund_cache")


def check_source_failover():
    """G36：渠道池多源降级必须真实可用（东财失败→新浪接手）。

    背景（2026-10-18）：`meta.get_stock_daily` 原先**只调东财单通道**
    （`ak.stock_zh_a_hist`），而 `bars.py` 里早就实现并注册了
    `_STOCK_DATA_SOURCE_POOL`（eastmoney → sina_stock → sina_fund）却从未被调用。
    后果：Hermes 的出口 IP 被东财节点封锁后整条取数链死掉，
    尽管 `ak.stock_zh_a_daily`（新浪）完全可用（实测返回 1643 根）。

    本门禁锁定该能力：**只封东财**，断言新浪能接手并返回足量数据。
    """
    sys.path.insert(0, SRC)
    os.chdir(SRC)
    import akshare as ak
    from stock_analysis.data_loader import meta

    problems, obs = [], {}
    orig = getattr(ak, "stock_zh_a_hist", None)

    class _DeadEastmoney:
        """只让东财通道失败；新浪保持真实可用。"""

    def _blocked(*a, **k):
        raise ConnectionError("probe: simulated eastmoney blocked (IP ban)")

    pool = meta._STOCK_DATA_SOURCE_POOL
    try:
        ak.stock_zh_a_hist = _blocked
        # 清空渠道池历史状态，确保从干净状态开始（避免冷却/剔除影响判定）
        for s in pool._states:
            s.disabled = False
            s.cooldown_until = 0.0
            s.consecutive_failures = 0
            s.failure_timestamps = []
        df, used = pool.fetch("600519", "20200101", "qfq",
                              preferred=meta._SOURCE_PREFERENCE.get("akshare"))
        obs["used_source"] = used
        obs["rows"] = 0 if df is None else len(df)
        if df is None or len(df) < 100:
            problems.append(
                f"东财失败后未降级成功（rows={obs['rows']}, used={used}）—— "
                f"渠道池未接入或新浪通道不可用")
        elif used == "eastmoney":
            problems.append("东财已封锁却仍报 eastmoney 成功（打桩失效）")
        # 降级后数据必须可用于回测（含必要列）
        if df is not None and len(df):
            need = {"open", "high", "low", "close", "volume"}
            miss = sorted(need - set(df.columns))
            if miss:
                problems.append(f"降级数据缺列 {miss}")
    finally:
        if orig is not None:
            ak.stock_zh_a_hist = orig
        for s in pool._states:
            s.disabled = False
            s.cooldown_until = 0.0
            s.consecutive_failures = 0
            s.failure_timestamps = []

    return {"status": "PASS" if not problems else "FAIL",
            "reason": "; ".join(problems), "observed": obs}


def main():
    which = sys.argv[1] if len(sys.argv) > 1 else "all"
    out = {}
    if which not in ("all", "all-quick"):
        try:
            out[which] = globals()["check_" + which]()
        except Exception as e:
            out[which] = {"status": "FAIL", "reason": f"探针异常: {type(e).__name__}: {e}"}
        print(json.dumps(out, ensure_ascii=False, indent=1))
        return

    quick = which == "all-quick"

    # 1) 股票主输出（G02/G03 + 复用给 G17/G18/G20/G21/G22/G23/G24/G26）
    stock_rec = check_json_contract()
    stock_data = stock_rec.get("data")
    out["json"] = {k: v for k, v in stock_rec.items() if k != "data"}
    out["semantic"] = (check_semantic_consistency(stock_data) if stock_data
                       else {"status": "BLOCKED", "reason": "CLI JSON 未产出"})

    # 2) 基金输出（G04 + 复用）
    fund_rec = check_fund_contract()
    fund_data = fund_rec.get("data")
    out["fund"] = {k: v for k, v in fund_rec.items() if k != "data"}

    def run(key, fn):
        try:
            out[key] = fn()
        except Exception as e:
            out[key] = {"status": "FAIL", "reason": f"探针异常: {type(e).__name__}: {e}"}

    run("fund_grid", lambda: check_fund_grid(fund_data))
    run("fund_grid_consistency", check_fund_grid_consistency)
    run("family_map", lambda: check_family_map(stock_data))
    run("n_trials", lambda: check_n_trials(stock_data, fund_data))
    run("significance", lambda: check_significance(stock_data))
    run("placeholders", lambda: check_placeholders(stock_data, fund_data))
    run("family_ratio", lambda: check_family_ratio(stock_data, fund_data))
    run("metrics_ci", lambda: check_metrics_ci(stock_data))
    run("time_in_market", lambda: check_time_in_market(stock_data))
    run("win_rate_share", lambda: check_win_rate_contribution(stock_data))
    run("old_args", check_old_args)
    run("sizing", check_sizing)
    run("atr_stop", check_atr_stop)
    run("walk_forward", check_walk_forward)
    run("requirements", check_requirements)
    run("a1", check_a1_invariant)
    run("advise", check_advise_keys)
    # 本轮新增：G33 降级可见性 / G34 --start 生效且不截断缓存 / G35 错误路径同构
    run("degrade", check_degrade_visibility)
    run("start_window", check_start_window)
    run("error_shape", check_error_shape)
    run("source_failover", check_source_failover)

    heavy = {
        "idempotent": check_idempotent,
        "windows": check_windows,
        "exit_codes": lambda: check_exit_codes(stock_rec),
        "type_hint": check_type_hint,
        "fund_cache": check_fund_cache_guard,
    }
    for name, fn in heavy.items():
        if quick:
            out[name] = {"status": "SKIP", "reason": "-Quick 跳过（完成声明前必须跑完整门禁）"}
            continue
        run(name, fn)

    for v in out.values():
        v.pop("data", None)
    print(json.dumps(out, ensure_ascii=False, indent=1))


def check_family_ratio(stock_data=None, fund_data=None):
    """G22：B5 —— candidates 中任一族计数 ≤ ceil(n/2)（股票与基金都要）。"""
    import math
    problems = []
    obs = {}
    for label, data in (("stock", stock_data), ("fund", fund_data)):
        if data is None:
            problems.append(f"{label} 无输出")
            continue
        cands = data.get("candidates") or []
        if not cands:
            problems.append(f"{label} candidates 为空")
            continue
        counts = {}
        for c in cands:
            fam = c.get("family")
            counts[fam] = counts.get(fam, 0) + 1
        n = len(cands)
        limit = math.ceil(n / 2)
        obs[label] = {"n": n, "limit": limit, "counts": counts}
        over = {f: v for f, v in counts.items() if v > limit}
        if over:
            problems.append(f"{label} 族占比超 50%: {over}（n={n}, limit={limit}）")
        members = ((data.get("evidence") or {}).get("consensus") or {}).get("members")
        if members is not None and list(members) != [c.get("name") for c in cands]:
            problems.append(f"{label} candidates 与 consensus.members 不一致（F13）")
    return {"status": "PASS" if not problems else "FAIL",
            "reason": "; ".join(problems), "observed": obs}


if __name__ == "__main__":
    main()
