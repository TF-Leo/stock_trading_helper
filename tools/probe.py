# -*- coding: utf-8 -*-
"""门禁取数探针：所有"必须真跑才能判定"的验收项都放这里。

由 tools/check_gates.ps1 调用，输出单个 JSON 文档到 stdout。
设计原则（针对本项目历史失败模式）：
  * 只用**真实执行结果**判定，禁止用 grep 源码 / 常量字符串冒充通过；
  * 无法验证的项必须显式 BLOCKED 并给出原因，不得静默算通过。
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
REQUIRED_REC_KEYS = [
    "status", "action", "action_reason", "confidence", "position_fraction",
    "horizon_days", "entry_zone", "stop_loss", "take_profit",
    "distance_to_trigger", "invalidation",
]


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


def cli_json(args, **kw):
    """真跑 CLI 并要求 stdout 是纯 JSON（C1 契约）。"""
    r = sh(args, **kw)
    rec = {"cmd": "python cli.py " + " ".join(args), "exit": r["exit"], "sec": r["sec"]}
    if r["exit"] not in (0, 10):
        rec["status"] = "FAIL"
        rec["reason"] = f"退出码 {r['exit']}（期望 0=ok 或 10=no_edge）: {r['err'].strip()[:300]}"
        return rec, None
    try:
        data = json.loads(r["out"])
    except Exception as e:
        rec["status"] = "FAIL"
        rec["reason"] = (f"stdout 不是纯 JSON（C1 违规）：{e}；"
                         f"stdout 前 200 字: {r['out'][:200]!r}")
        return rec, None
    rec["data"] = data
    return rec, data


def check_json_contract(code="600519", fast=True):
    args = [code, "--json"] + (["--fast"] if fast else [])
    rec, data = cli_json(args)
    if data is None:
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
    args = [code, "--json"] + (["--fast"] if fast else [])
    rec, data = cli_json(args)
    if data is None:
        if rec.get("exit") == 20:
            rec["status"] = "BLOCKED"
            rec["reason"] = "基金缓存缺失（退出码 20=insufficient_data），需先准备离线缓存"
        return rec
    problems = []
    if data.get("schema_version") != "1.0":
        problems.append("schema_version != '1.0'")
    missing = [k for k in REQUIRED_TOP_KEYS if k not in data]
    if missing:
        problems.append(f"缺字段 {missing}")
    rec["status"] = "PASS" if not problems else "FAIL"
    rec["reason"] = "; ".join(problems)
    rec["keys"] = sorted(data.keys())
    return rec


def check_idempotent(code="600519"):
    """C8：同一缓存数据下两次运行，剔除时间戳/耗时后必须逐字节相同。"""
    r1, d1 = cli_json([code, "--json", "--fast"])
    if d1 is None:
        return {"status": "FAIL", "reason": "首次运行失败: " + r1.get("reason", "")}
    r2, d2 = cli_json([code, "--json", "--fast"])
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


def main():
    which = sys.argv[1] if len(sys.argv) > 1 else "all"
    out = {}
    jobs = {
        "json": lambda: check_json_contract(),
        "semantic": None,
        "fund": lambda: check_fund_contract(),
        "idempotent": lambda: check_idempotent(),
        "windows": lambda: check_windows(),
        "a1": lambda: check_a1_invariant(),
        "advise": lambda: check_advise_keys(),
    }
    if which in ("all", "all-quick"):
        quick = which == "all-quick"
        jc = check_json_contract()
        out["json"] = jc
        if jc.get("data") is not None:
            out["semantic"] = check_semantic_consistency(jc["data"])
        else:
            out["semantic"] = {"status": "BLOCKED", "reason": "CLI JSON 未产出"}
        jc.pop("data", None)
        for name in (("a1", "advise") if quick else ("fund", "idempotent", "windows", "a1", "advise")):
            try:
                out[name] = jobs[name]()
            except Exception as e:
                out[name] = {"status": "FAIL", "reason": f"探针异常: {type(e).__name__}: {e}"}
        if quick:
            for name in ("fund", "idempotent", "windows"):
                out[name] = {"status": "SKIP", "reason": "-Quick 跳过（完成声明前必须跑完整门禁）"}
    else:
        try:
            out[which] = jobs[which]()
        except Exception as e:
            out[which] = {"status": "FAIL", "reason": f"探针异常: {type(e).__name__}: {e}"}
    for v in out.values():
        v.pop("data", None)
    print(json.dumps(out, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()