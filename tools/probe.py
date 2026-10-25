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
import re
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
    # v3.0：有效期契约（G47 校验其内部自洽）
    "signal", "target_position", "review_date", "side_expectancy",
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
    # v3.0：买入不再要求全样本 alpha 显著（那是"策略值不值得信"的检验，
    # 不是"这笔能不能做"的门槛）。买入改由 side_expectancy.ok 决定，见 G47。
    if rec.get("action") == "buy":
        exp = rec.get("side_expectancy") or {}
        if exp.get("ok") is not True:
            problems.append("action=buy 但 side_expectancy.ok 不为 true（买侧期望不达标）")
    if rec.get("action") == "sell":
        txt = " ".join(rec.get("action_reason") or [])
        if not any(k in txt for k in ("止损", "止盈", "时间止损", "风控")):
            problems.append("action=sell 但文案未说明风控类型（趋势卖出已废弃）")
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
    """G18：significant / no_edge 必须与超额收益 CI 自洽。

    F27 收紧：`significant` 只表示"统计上可证明有正超额优势"（CI **下界**>0 且 DSR>0），
    不再是"不能排除正收益"（CI 上界>0）——后者让 002415 在 p_positive=3.5% 时仍报显著。
    """
    if stock_data is None:
        return {"status": "BLOCKED", "reason": "无股票输出"}
    ev = stock_data.get("evidence") or {}
    rec = stock_data.get("recommendation") or {}
    exc = ev.get("excess_vs_buy_hold") or {}
    ci = exc.get("ci_95") or [0.0, 0.0]
    dsr = ev.get("deflated_sharpe")
    sig = ev.get("significant")
    problems = []
    expect_sig = bool((dsr or 0) > 0 and ci[0] > 0)
    if bool(sig) != expect_sig:
        problems.append(f"significant={sig} 与 (dsr>0 and ci_lo>0)={expect_sig} 矛盾"
                        f"（dsr={dsr}, ci_lo={ci[0]}）")
    if rec.get("no_edge") != (rec.get("status") == "no_edge"):
        problems.append(f"no_edge={rec.get('no_edge')} 与 status={rec.get('status')} 矛盾")
    if rec.get("status") == "ok" and ci[1] <= 0:
        problems.append(f"status=ok 但超额收益 CI 上界 {ci[1]} <= 0（B8 要求 no_edge）")
    return {"status": "PASS" if not problems else "FAIL",
            "reason": "; ".join(problems),
            "observed": {"significant": sig, "dsr": dsr, "ci_lo": ci[0], "ci_hi": ci[1],
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
        # 2026-10-18：取数已统一走 data_loader.meta.resolve_and_fetch()
        # （cli.py 不再直接调用 get_stock_daily/get_fund_daily），
        # 因此打桩点必须跟着改，否则注入无效、G33 会误报 stale=False。
        import stock_analysis.data_loader.meta as _meta
        orig_resolve = _meta.resolve_and_fetch
        try:
            cli.build_strategy_signals = lambda: {k: full[k] for k in picks}
            _meta.resolve_and_fetch = lambda code, type_hint="auto", start_date=None: (
                df, "stock", None)
            meta._record_fetch("cache", True, degraded=True,
                               degraded_reason="probe:simulated stale cache",
                               cache_last_date=info.get("cache_last_date"),
                               lag_days=info.get("lag_days"),
                               expected_date=info.get("expected_date"))
            resp = cli.advise_internal("600519", fast=True)
        finally:
            cli.build_strategy_signals = orig_build
            _meta.resolve_and_fetch = orig_resolve
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
    """B6/G06（F22 修复）：三窗口必须真的不同，且决策输入必须稳定。

    2026-10-25 修复：原实现打桩 `cli.get_stock_daily`，但取数早已改走
    `meta.resolve_and_fetch`（cli.py:241-242）。`cli.get_stock_daily` 只剩 import、零调用点，
    于是三个窗口跑的都是同一份全量数据 —— 实测 drop120 应 3808 根、实得 3928 根。
    G06 的 PASS 当时只证明了"同一份数据跑三次结果相同"，与稳定性无关。

    新判据（契约 v2.0 §8）：
      L1 `action` 必须一致 —— 这是下游真正消费的东西
      L2 `position_state` / `family` 允许不同（换训练窗口后可能换胜出策略），
         但 `stability.*_stable` 不得谎称稳定
      L3 三窗口 `bars` 必须互不相同（打桩生效的证据）

    **2026-10-25 设计更正**：原实现从**末尾**砍 60/120 根，等于把决策日期也一起
    往前挪了 60/120 个交易日 —— 不同日期的仓位本来就该不同，这不是稳定性问题。
    正确做法是**固定决策日、只改训练窗口**（从头砍），否则测的是"仓位随时间变化"。
    """
    cli, df = _load_cli_and_full_df(code)
    if df is None or len(df) < 300:
        return {"status": "BLOCKED", "reason": "本地缓存不足，无法切三窗口"}
    # 注意：import 必须在 _load_cli_and_full_df() 之后 —— 它会 os.chdir(SRC)，
    # 否则在仓库根目录跑 probe 时 `stock_analysis` 不可导入。
    from stock_analysis.data_loader import meta
    orig = meta.resolve_and_fetch
    results = {}
    try:
        for label, drop in (("full", 0), ("drop_head60", 60), ("drop_head120", 120)):
            # 从头砍：保留同一决策日（最后一根），只改训练窗口长度
            sub = (df.iloc[drop:] if drop else df).copy()
            expected = len(sub)
            meta.resolve_and_fetch = (lambda d: (lambda *a, **k: (d, "stock", None)))(sub)
            one = cli.advise_internal(code, start=None, fast=True)
            got = (one.get("data") or {}).get("bars")
            # 打桩生效自检：只有打桩真的被调用到，bars 才可能等于 expected
            if got != expected:
                return {"status": "FAIL",
                        "reason": (f"{label}: 打桩未生效，期望 bars={expected}，实得 {got}。"
                                   "取数入口可能又变了，请检查 meta.resolve_and_fetch"),
                        "observed": {"label": label, "expected_bars": expected,
                                     "actual_bars": got}}
            rec = one.get("recommendation") or {}
            results[label] = {
                "bars": got,
                "end": str((one.get("data") or {}).get("end"))[:10],
                "action": rec.get("action"),
                "position_state": (rec.get("signal") or {}).get("position_state"),
                "strategy": (rec.get("signal") or {}).get("strategy"),
                "family": (one.get("evidence") or {}).get("consensus", {}).get("family"),
                "status": rec.get("status"),
                "claimed_stability": (one.get("evidence") or {}).get("stability"),
            }
    finally:
        meta.resolve_and_fetch = orig

    bars = [v["bars"] for v in results.values()]
    ends = {v["end"] for v in results.values()}
    states = {v["position_state"] for v in results.values()}
    families = {v["family"] for v in results.values()}
    actions = {v["action"] for v in results.values()}
    strategies = {v["strategy"] for v in results.values()}
    problems = []
    if len(set(bars)) != 3:
        problems.append(f"三窗口 bars 未互不相同 {bars}（打桩未生效）")
    if len(ends) != 1:
        problems.append(f"决策日不一致 {sorted(ends)}（窗口必须固定同一决策日）")
    if len(actions) != 1:
        problems.append(f"action 跨训练窗口不一致: {sorted(map(str, actions))}")
    # position_state / family 允许变化，但不得谎称稳定
    if len(states) != 1 or len(families) != 1:
        for v in results.values():
            st = v.get("claimed_stability") or {}
            if st.get("action_stable") is True or st.get("family_stable") is True:
                problems.append("stability 声称稳定，但实测 position_state/family 跨窗口不一致"
                                "（自评与实际不符）")
                break
    return {"status": "PASS" if not problems else "FAIL",
            "reason": "; ".join(problems),
            "observed": {"windows": results, "bars": bars,
                         "actions": sorted(map(str, actions)),
                         "position_state": sorted(map(str, states)),
                         "families": sorted(map(str, families)),
                         "strategies": sorted(map(str, strategies)),
                         "position_drift": len(states) != 1,
                         "family_drift": len(families) != 1}}


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


def check_window_note():
    """G37：`data.window_note` 必须如实反映 `--start` 截取，且两条路径都不能恒为 null。

    背景（2026-10-18）：`window_note` 这条键在成功与错误路径都存在，
    但 `create_error_response` 里被**硬编码成 None** —— 即使带 `--start` 也不说明窗口。
    G35 只比对"键是否存在"，值被写死时它查不出来，于是这个洞一直没人发现。

    判据（三档）：
      1. 不带 --start        → window_note is None（没有截取，不该编造说明）
      2. 带 --start 且成功   → window_note 是字符串，含 --start 的日期与真实根数
      3. 带 --start 且数据太短 → 同样必须是字符串，且根数与 data.bars 一致
    """
    import re

    problems, obs = [], {}

    # 1) 不带 --start
    rec0 = run_cli_json(["600519", "--json", "--fast"])
    d0 = rec0.get("data")
    if not d0:
        return {"status": "BLOCKED", "reason": "无 JSON 输出"}
    obs["no_start"] = d0["data"].get("window_note")
    if d0["data"].get("window_note") is not None:
        problems.append(f"未带 --start 却给了 window_note={d0['data'].get('window_note')!r}")

    # 2) 带 --start 且成功（2025-06-01 起约 334 根，足够回测）
    rec1 = run_cli_json(["600519", "--json", "--fast", "--start", "20250601"], cache=False)
    d1 = rec1.get("data")
    if not d1:
        problems.append("--start 成功路径无 JSON")
    else:
        note = d1["data"].get("window_note")
        obs["ok_path"] = {"status": d1["recommendation"]["status"],
                          "bars": d1["data"]["bars"], "note": note}
        if not isinstance(note, str) or not note:
            problems.append(f"--start 成功路径 window_note={note!r}（应为带截取说明的字符串）")
        else:
            if "20250601" not in note:
                problems.append(f"window_note 未包含请求日期：{note!r}")
            m = re.search(r"(\d+)\s*根", note)
            if not m or int(m.group(1)) != int(d1["data"]["bars"]):
                problems.append(
                    f"window_note 根数与 data.bars 不一致：{note!r} vs bars={d1['data']['bars']}")

    # 3) 带 --start 且数据太短（近 40 天 → insufficient_data）
    from datetime import date, timedelta
    near = (date.today() - timedelta(days=40)).strftime("%Y%m%d")
    rec2 = run_cli_json(["600519", "--json", "--fast", "--start", near], cache=False)
    d2 = rec2.get("data")
    if not d2:
        problems.append("--start 错误路径无 JSON")
    else:
        note2 = d2["data"].get("window_note")
        st2 = d2["recommendation"]["status"]
        obs["err_path"] = {"status": st2, "bars": d2["data"]["bars"], "note": note2}
        if st2 != "insufficient_data":
            problems.append(f"近端 --start 未触发 insufficient_data（实际 {st2}），用例前提不成立")
        elif not isinstance(note2, str) or not note2:
            problems.append(
                f"错误路径 window_note={note2!r}（硬编码 None 会让调用方无从知道截了窗口）")
        else:
            m2 = re.search(r"(\d+)\s*根", note2)
            if not m2 or int(m2.group(1)) != int(d2["data"]["bars"]):
                problems.append(
                    f"错误路径 window_note 根数与 bars 不一致：{note2!r} vs bars={d2['data']['bars']}")

    return {"status": "PASS" if not problems else "FAIL",
            "reason": "; ".join(problems), "observed": obs}

def check_entry_zone_valid():
    """G38：`recommendation.entry_zone` 必须是**有效区间**（lower <= upper）。

    背景（2026-10-18，Hermes 实测暴露）：`derive_entry_zone` 旧实现做
    `lower = max(last-0.5ATR, buy_price)` 却不调整上沿，当策略的买入触发价
    高于 `last+0.5ATR` 时输出**倒置区间**。实测：
        600522 last=26.62 → [44.71, 27.39]   （差 +17.32）
        002415 last=33.66 → [35.18, 33.98]   （差 +1.20）
        300476 last=182.0 → [195.16, 188.49] （差 +6.67）

    判据：
      1. entry_zone 为 None 时跳过（不适用）；
      2. 否则必须是长度为 2 的数值列表，且 lower <= upper；
      3. 区间应围绕 last_close（不做硬性阈值，仅要求两端均为正数）。
    """
    problems, obs = [], {}
    codes = ["600522", "002415", "300476", "002270", "600519"]
    for code in codes:
        rec = run_cli_json([code, "--json", "--fast"])
        d = rec.get("data")
        if not d:
            obs[code] = "无JSON"
            continue
        r = d["recommendation"]
        ez = r.get("entry_zone")
        lc = (d.get("data") or {}).get("last_close")
        obs[code] = {"entry_zone": ez, "last_close": lc}
        if ez is None:
            continue
        if not isinstance(ez, list) or len(ez) != 2:
            problems.append(f"{code} entry_zone 不是长度 2 的列表: {ez!r}")
            continue
        try:
            lo, hi = float(ez[0]), float(ez[1])
        except (TypeError, ValueError):
            problems.append(f"{code} entry_zone 含非数值: {ez!r}")
            continue
        if lo > hi:
            problems.append(f"{code} entry_zone 倒置: [{lo}, {hi}]（lower > upper）")
        if lo <= 0 or hi <= 0:
            problems.append(f"{code} entry_zone 含非正值: [{lo}, {hi}]")
    return {"status": "PASS" if not problems else "FAIL",
            "reason": "; ".join(problems), "observed": obs}


def check_otc_fund():
    """G39：场外基金（OTC 净值）必须可用。

    背景（2026-10-18，Hermes 实测暴露）：`get_fund_daily` 曾**只有 ETF 通道**
    （`ak.fund_etf_hist_sina`），4 只场外基金
    （023298/006195/001917/001564）全部 0 行 → data_source_error，
    而同一批基金在 OTC 通道（`ak.fund_open_fund_info_em`）上有 366~2689 行。

    同时锁定 auto 判定：这些 0/1 段代码必须被识别为 fund（不能被当成 stock）。

    判据（每条）：
      - exit ∈ {0, 10}（ok / no_edge 都算可用；30=取不到 才算失败）
      - data.bars >= 100
      - request.type == 'fund'（auto 判定正确）
    """
    problems, obs = [], {}
    for code in ("023298", "001917", "510300"):
        rec = run_cli_json([code, "--json", "--fast", "--type", "fund"], cache=False)
        d = rec.get("data")
        if not d:
            problems.append(f"{code} 无 JSON（exit={rec.get('exit')}）")
            obs[code] = {"exit": rec.get("exit")}
            continue
        bars = (d.get("data") or {}).get("bars", 0)
        typ = (d.get("request") or {}).get("type")
        st = (d.get("recommendation") or {}).get("status")
        obs[code] = {"exit": rec.get("exit"), "bars": bars,
                     "type": typ, "status": st,
                     "source": (d.get("data") or {}).get("source")}
        if rec.get("exit") not in (0, 10):
            problems.append(f"{code} --type fund 退出码 {rec.get('exit')}（期望 0 或 10）")
        if bars < 100:
            problems.append(f"{code} --type fund 仅 {bars} 根")
    # auto 判定：0/1 段场外基金必须识别为 fund
    for code in ("001917", "023298"):
        rec = run_cli_json([code, "--json", "--fast"], cache=False)
        d = rec.get("data")
        typ = ((d or {}).get("request") or {}).get("type")
        obs.setdefault("auto", {})[code] = {"type": typ, "exit": rec.get("exit")}
        if typ != "fund":
            problems.append(f"{code} auto 判定为 {typ}（期望 fund）")
    return {"status": "PASS" if not problems else "FAIL",
            "reason": "; ".join(problems), "observed": obs}


def check_signal_semantics(stock_data=None, fund_data=None):
    """G40：动作 / 状态 / 文案 / 指标值必须同源（F23/F24/F25）。

    对应 2026-10-25 事故：`action=hold` + `action_reason='...: 卖出信号 (32.42)'`，
    而 32.42 是当日收盘价、死叉发生在两天前、10-23 大涨当天仍在报卖出。

    四条判据：
      1. `position_state=flat` 时不得出现 `near_sell` / `sell_triggered`；
      2. `action=hold` 时文案不得含"卖出信号"/"买入信号"；
      3. `action=sell` 时文案必须含 `signal.as_of`（今日触发）；
      4. `trigger.current` 非空时不得等于 `last_close`（指标值被价格顶替）。
    """
    problems, obs = [], {}
    for label, data in (("stock", stock_data), ("fund", fund_data)):
        if not data:
            continue
        rec = data.get("recommendation") or {}
        sig = rec.get("signal") or {}
        trig = (data.get("evidence") or {}).get("triggers") or []
        action = rec.get("action")
        reason = " ".join(rec.get("action_reason") or [])
        ps, state = sig.get("position_state"), sig.get("state")
        obs[label] = {"action": action, "position_state": ps, "state": state,
                      "reason": reason[:80], "n_triggers": len(trig)}

        if not sig:
            problems.append(f"{label} 缺 recommendation.signal")
            continue
        if ps == "flat" and state in ("near_sell", "sell_triggered"):
            problems.append(f"{label} position_state=flat 却 state={state}（F24：空仓不得报卖出侧状态）")
        if action == "hold" and re.search("卖出信号|买入信号", reason):
            problems.append(f"{label} action=hold 但文案含'卖出信号/买入信号'（F23）")
        if action == "sell" and sig.get("as_of") and sig["as_of"] not in reason:
            problems.append(f"{label} action=sell 但文案未写今日日期 {sig.get('as_of')}（F23）")
        for t in trig:
            cur, lc = t.get("current"), t.get("last_close")
            if cur is None or lc in (None, 0):
                continue
            try:
                if abs(float(cur) - float(lc)) < 1e-9:
                    problems.append(
                        f"{label} trigger.current={cur} 等于 last_close（F25：指标值被收盘价顶替）")
                    break
            except (TypeError, ValueError):
                problems.append(f"{label} trigger.current 非数值: {cur!r}")
                break
    if not obs:
        return {"status": "BLOCKED", "reason": "无股票/基金输出"}
    return {"status": "PASS" if not problems else "FAIL",
            "reason": "; ".join(problems), "observed": obs}


def check_edge(stock_data=None):
    """G41：`evidence.edge` 三档必须与 CI 符号、p_positive 分位数自洽（F27）。

    002415 曾出现 `p_positive=0.035`（96.5% 样本跑输）却 `significant=true`，
    因为旧判据只看 CI 上界>0。这里锁死：只有 CI **下界**>0 才算正优势。
    """
    if stock_data is None:
        return {"status": "BLOCKED", "reason": "无股票输出"}
    ev = stock_data.get("evidence") or {}
    g = ev.get("edge") or {}
    verdict = g.get("verdict")
    ci = g.get("ci_95") or [None, None]
    lo, hi = (ci + [None, None])[:2]
    p = g.get("p_positive")
    problems = []

    if verdict not in ("positive_significant", "inconclusive", "negative_significant"):
        problems.append(f"edge.verdict 非法: {verdict!r}")
    if lo is not None and hi is not None:
        expect = ("positive_significant" if lo > 0
                  else ("negative_significant" if hi < 0 else "inconclusive"))
        if verdict != expect:
            problems.append(f"verdict={verdict} 与 CI=[{lo}, {hi}] 不一致（期望 {expect}）")
    if p is not None and lo is not None and hi is not None:
        if (float(p) >= 0.975) != (lo > 0):
            problems.append(f"p_positive={p} 与 ci_lo={lo} 分位数矛盾")
        if (float(p) <= 0.025) != (hi < 0):
            problems.append(f"p_positive={p} 与 ci_hi={hi} 分位数矛盾")
    expect_sig = bool(verdict == "positive_significant" and g.get("dsr_ok") is True)
    if bool(ev.get("significant")) != expect_sig:
        problems.append(f"significant={ev.get('significant')} 与 "
                        f"(verdict=positive 且 dsr_ok)={expect_sig} 矛盾（F27）")
    return {"status": "PASS" if not problems else "FAIL",
            "reason": "; ".join(problems),
            "observed": {"verdict": verdict, "ci_95": [lo, hi], "p_positive": p,
                         "dsr_ok": g.get("dsr_ok"), "significant": ev.get("significant")}}


def check_signal_stats(stock_data=None):
    """G42：`evidence.signal_stats` 齐备且内部自洽（F28）。"""
    if stock_data is None:
        return {"status": "BLOCKED", "reason": "无股票输出"}
    s = (stock_data.get("evidence") or {}).get("signal_stats") or {}
    need = ("strategy", "horizons", "n_buy", "n_sell", "buy", "sell",
            "sell_false_alarm_rate_5d", "note")
    problems = [f"缺 {k}" for k in need if k not in s]
    if problems:
        return {"status": "FAIL", "reason": "; ".join(problems), "observed": s}
    if s.get("horizons") != [1, 3, 5, 10, 20]:
        problems.append(f"horizons={s.get('horizons')}（期望 [1,3,5,10,20]）")
    n_sell, n_buy = s.get("n_sell"), s.get("n_buy")
    if not isinstance(n_buy, int) or not isinstance(n_sell, int):
        problems.append(f"n_buy/n_sell 非整数: {n_buy!r}/{n_sell!r}")
    for side in ("buy", "sell"):
        block = s.get(side) or {}
        for field in ("mean_fwd", "hit_rate"):
            vals = block.get(field)
            if vals is None:
                continue
            if not isinstance(vals, dict) or len(vals) != 5:
                problems.append(f"{side}.{field} 不是 5 档字典: {vals!r}")
                continue
            for h, v in vals.items():
                if v is None:
                    continue
                if field == "hit_rate" and not (0.0 <= float(v) <= 1.0):
                    problems.append(f"{side}.hit_rate[{h}]={v} 越界")
    far = s.get("sell_false_alarm_rate_5d")
    if far is not None and not (0.0 <= float(far) <= 1.0):
        problems.append(f"sell_false_alarm_rate_5d={far} 越界")
    return {"status": "PASS" if not problems else "FAIL",
            "reason": "; ".join(problems),
            "observed": {"strategy": s.get("strategy"), "n_buy": n_buy, "n_sell": n_sell,
                         "sell_false_alarm_rate_5d": far,
                         "sell_hit_rate_5d": ((s.get("sell") or {}).get("hit_rate") or {}).get("5")}}


def check_execution(stock_data=None):
    """G43：`data.execution` 与 `BacktestConfig` 同源，且成交价真的随口径变化（F29）。

    旧实现固定用 T+1 **收盘**成交，而 Hermes 拿到建议最早只能 T+1 开盘动作，
    差一整个交易日；输出里也没有任何字段声明这件事。
    """
    if stock_data is None:
        return {"status": "BLOCKED", "reason": "无股票输出"}
    ex = (stock_data.get("data") or {}).get("execution") or {}
    need = ("signal_bar", "fill", "slippage_bps", "commission_bps")
    problems = [f"data.execution 缺 {k}" for k in need if k not in ex]
    if ex.get("fill") not in ("next_open", "next_close"):
        problems.append(f"execution.fill={ex.get('fill')!r} 非法")

    os.chdir(SRC)
    try:
        import numpy as np
        import pandas as pd
        from stock_analysis.backtest.engine import BacktestConfig, BacktestEngine

        if ex.get("fill") != BacktestConfig().exec_price:
            problems.append(f"execution.fill={ex.get('fill')} 与 BacktestConfig().exec_price="
                            f"{BacktestConfig().exec_price} 不一致（两处各写一份）")

        n = 300
        idx = pd.date_range("2020-01-01", periods=n, freq="B")
        close = np.linspace(10, 20, n)
        df = pd.DataFrame({"open": close * 0.98, "high": close * 1.01,
                           "low": close * 0.97, "close": close, "volume": 1e6}, index=idx)
        buy = pd.Series(False, index=idx)
        buy.iloc[10] = True
        sell = pd.Series(False, index=idx)
        sell.iloc[100] = True

        def _entry(mode):
            eng = BacktestEngine(BacktestConfig(exec_price=mode))
            r = eng.run_backtest(df, lambda d: {"buy": buy, "sell": sell})
            return (r.trade_count, float(r.trades[0].entry_price)) if r.trades else (0, None)

        cnt_o, px_o = _entry("next_open")
        cnt_c, px_c = _entry("next_close")
        if cnt_o != cnt_c:
            problems.append(f"两种成交口径交易数不同: {cnt_o} vs {cnt_c}")
        if px_o is None or px_c is None or abs(px_o - px_c) < 1e-9:
            problems.append(f"两种成交口径成交价相同（{px_o} vs {px_c}）：exec_price 未生效")
    except Exception as e:
        problems.append(f"成交口径行为验证异常: {type(e).__name__}: {e}")
        px_o = px_c = None

    return {"status": "PASS" if not problems else "FAIL",
            "reason": "; ".join(problems),
            "observed": {"execution": ex, "entry_next_open": px_o, "entry_next_close": px_c}}


def check_as_of(code="002415"):
    """G45：`--as-of` 必须真的截断，且不得泄露未来数据（F31）。"""
    os.chdir(SRC)
    problems, obs = [], {}
    try:
        import pandas as pd
        csv = os.path.join(SRC, "stock_analysis", "data", f"stock_{code}_qfq.csv")
        full = pd.read_csv(csv, index_col=0, parse_dates=True)
    except Exception as e:
        return {"status": "BLOCKED", "reason": f"读不到本地缓存: {type(e).__name__}: {e}"}

    for day in ("2026-10-22", "2026-10-23"):
        rec = run_cli_json([code, "--json", "--fast", "--as-of", day], cache=False)
        d = rec.get("data") or {}
        dd = d.get("data") or {}
        sub = full.loc[full.index <= pd.Timestamp(day)]
        bars, end = dd.get("bars"), str(dd.get("end"))[:10]
        lc = dd.get("last_close")
        obs[day] = {"bars": bars, "expect_bars": len(sub), "end": end,
                    "last_close": lc, "echo": (d.get("request") or {}).get("as_of"),
                    "exit": rec.get("exit")}
        if end != day:
            problems.append(f"{day}: data.end={end}（应等于 as-of，不得含未来数据）")
        if bars != len(sub):
            problems.append(f"{day}: bars={bars} != 缓存截断 {len(sub)}")
        expect_close = float(sub["close"].iloc[-1])
        if lc is None or abs(float(lc) - expect_close) > 1e-6:
            problems.append(f"{day}: last_close={lc} != 该日收盘 {expect_close:.4f}")
        if (d.get("request") or {}).get("as_of") != day:
            problems.append(f"{day}: request.as_of 未回显")
        if not str(dd.get("window_note") or "").startswith("--as-of"):
            problems.append(f"{day}: window_note={dd.get('window_note')!r} 未说明 as-of")
        if rec.get("exit") not in (0, 10):
            problems.append(f"{day}: 退出码 {rec.get('exit')}（期望 0 或 10）")
    return {"status": "PASS" if not problems else "FAIL",
            "reason": "; ".join(problems), "observed": obs}


def check_replay(stock_data=None):
    """G46：`evidence.decision_replay` 必须真按**策略引擎**回放且内部自洽（v3.0）。

    v3.0 的回放对象是"买点+条件期望入场、止损/止盈/时间止损出场"，
    不再是"原始信号买点买、卖点卖"。
    """
    if stock_data is None:
        return {"status": "BLOCKED", "reason": "无股票输出"}
    ev = stock_data.get("evidence") or {}
    r = ev.get("decision_replay") or {}
    rec = stock_data.get("recommendation") or {}
    need = ("method", "trade_count", "annual_return", "max_drawdown",
            "exposure", "vs_buy_hold", "exit_reasons", "note")
    problems = [f"缺 {k}" for k in need if k not in r]
    if problems:
        return {"status": "FAIL", "reason": "; ".join(problems), "observed": r}

    if not str(r.get("method") or "").startswith("policy_h"):
        problems.append(f"method={r.get('method')!r} 未声明为策略引擎口径")
    if "walk" in str(r.get("method") or "").lower():
        problems.append("method 不得声称 walk-forward")
    exp = r.get("exposure")
    if exp is None or not (0.0 <= float(exp) <= 1.0):
        problems.append(f"exposure={exp} 越界")
    reasons = r.get("exit_reasons") or {}
    bad = set(reasons) - {"stop", "target", "time"}
    if bad:
        problems.append(f"exit_reasons 出现非风控类型 {sorted(bad)}（趋势卖出已废弃）")
    if int(r.get("trade_count") or 0) == 0 and not r.get("note"):
        problems.append("零交易却没有 note 说明原因")
    # 买侧期望可用且有足够历史时，引擎必须真的产生过交易
    if (rec.get("side_expectancy") or {}).get("ok") is True and (stock_data.get("data") or {}).get("bars", 0) > 400:
        if int(r.get("trade_count") or 0) == 0:
            problems.append("买侧期望可用却零交易（引擎未生效）")
    return {"status": "PASS" if not problems else "FAIL",
            "reason": "; ".join(problems),
            "observed": {"method": r.get("method"), "trade_count": r.get("trade_count"),
                         "exposure": exp, "vs_buy_hold": r.get("vs_buy_hold"),
                         "exit_reasons": reasons}}


def check_horizon_contract(stock_data=None):
    """G47：有效期契约 —— target_position / review_date / horizon_days 自洽（v3.0）。"""
    if stock_data is None:
        return {"status": "BLOCKED", "reason": "无股票输出"}
    rec = stock_data.get("recommendation") or {}
    sig = rec.get("signal") or {}
    problems, obs = [], {}
    tp = rec.get("target_position")
    hd = rec.get("horizon_days") or []
    rd = rec.get("review_date")
    exp = rec.get("side_expectancy") or {}
    obs = {"action": rec.get("action"), "target_position": tp,
           "horizon_days": hd, "review_date": rd,
           "position_state": sig.get("position_state")}

    if tp is None or float(tp) not in (0.0, 0.5, 1.0):
        problems.append(f"target_position={tp!r}（应为 0/0.5/1.0）")
    if not (isinstance(hd, list) and len(hd) == 2 and hd[0] == hd[1] and int(hd[0]) == 20):
        problems.append(f"horizon_days={hd}（v3.0 固定为 [20, 20]）")
    if not rd or not isinstance(rd, str) or len(rd) != 10:
        problems.append(f"review_date={rd!r} 缺失或格式非法")
    if exp.get("horizon") != 20:
        problems.append(f"side_expectancy.horizon={exp.get('horizon')}（应为 20）")
    # 动作与目标仓位必须一致
    act = rec.get("action")
    if act == "buy" and float(tp) != 1.0:
        problems.append(f"action=buy 但 target_position={tp}")
    if act == "sell" and float(tp) != 0.0:
        problems.append(f"action=sell 但 target_position={tp}")
    if act == "hold" and sig.get("position_state") == "holding" and float(tp) != 1.0:
        problems.append("持有中且 hold，但 target_position 不是 1.0")
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
    # 本轮新增：G40 语义同源 / G41 证据等级 / G42 信号级统计
    run("signal_semantics", lambda: check_signal_semantics(stock_data, fund_data))
    run("edge", lambda: check_edge(stock_data))
    run("signal_stats", lambda: check_signal_stats(stock_data))
    # 本轮新增：G43 成交口径 / G45 历史复盘
    run("execution", lambda: check_execution(stock_data))
    run("as_of", check_as_of)
    # F30：决策回放（v3.0 起为策略引擎口径）
    run("replay", lambda: check_replay(stock_data))
    # v3.0：有效期契约
    run("horizon", lambda: check_horizon_contract(stock_data))
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
    run("window_note", check_window_note)
    run("entry_zone", check_entry_zone_valid)
    run("otc_fund", check_otc_fund)

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
