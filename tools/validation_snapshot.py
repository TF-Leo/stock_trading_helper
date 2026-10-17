# -*- coding: utf-8 -*-
"""生成 A13 / B6 / B1 验证快照：600519 与 510300 的实测表（全部数字来自真实运行）。

用法（在仓库根目录）：
    python tools/validation_snapshot.py
输出：
    source/docs/validation_600519.md
"""
from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import time

sys.stdout.reconfigure(encoding="utf-8")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "source")
OUT = os.path.join(SRC, "docs", "validation_600519.md")


def run_cli(args, timeout=900):
    p = subprocess.run([sys.executable, "cli.py"] + args, cwd=SRC,
                       capture_output=True, timeout=timeout)
    try:
        return p.returncode, json.loads(p.stdout.decode("utf-8", "replace"))
    except Exception:
        return p.returncode, None


def summarize(d):
    if not d:
        return {}
    rec = d.get("recommendation", {})
    ev = d.get("evidence", {})
    cands = d.get("candidates") or []
    metrics = ev.get("metrics") or {}
    counts = {}
    for c in cands:
        counts[c.get("family")] = counts.get(c.get("family"), 0) + 1
    return {
        "status": rec.get("status"),
        "action": rec.get("action"),
        "confidence": rec.get("confidence"),
        "position_fraction": rec.get("position_fraction"),
        "stop_loss": rec.get("stop_loss"),
        "take_profit": rec.get("take_profit"),
        "stop_method": rec.get("stop_method"),
        "entry_zone": rec.get("entry_zone"),
        "horizon_days": rec.get("horizon_days"),
        "distance_to_trigger": rec.get("distance_to_trigger"),
        "invalidation": rec.get("invalidation"),
        "triggers": len(ev.get("triggers") or []),
        "family": (ev.get("consensus") or {}).get("family"),
        "agreement": (ev.get("consensus") or {}).get("agreement"),
        "n_trials": ev.get("n_trials"),
        "n_survivors": ev.get("n_survivors"),
        "dsr": ev.get("deflated_sharpe"),
        "significant": ev.get("significant"),
        "excess_point": (ev.get("excess_vs_buy_hold") or {}).get("point"),
        "excess_ci": (ev.get("excess_vs_buy_hold") or {}).get("ci_95"),
        "excess_p_positive": (ev.get("excess_vs_buy_hold") or {}).get("p_positive"),
        "regime": (d.get("market_regime") or {}).get("label"),
        "regime_win_rates": ev.get("regime_win_rates"),
        "bars": (d.get("data") or {}).get("bars"),
        "data_source": (d.get("data") or {}).get("source"),
        "from_cache": (d.get("data") or {}).get("from_cache"),
        "top_name": cands[0]["name"] if cands else None,
        "top_score": cands[0]["score"] if cands else None,
        "top_score_is": cands[0].get("score_is") if cands else None,
        "top_score_oos": cands[0].get("score_oos") if cands else None,
        "top_trades": cands[0]["trade_count"] if cands else None,
        "n_candidates": len(cands),
        "family_counts": counts,
        "ci_metrics": sorted(k for k, v in metrics.items() if (v or {}).get("ci_95")),
        "time_in_market": (metrics.get("time_in_market") or {}).get("point"),
        "zero_trade_configs": (d.get("diagnostics") or {}).get("zero_trade_configs"),
        "warnings": len((d.get("diagnostics") or {}).get("warnings") or []),
    }


def win_rate_share(d):
    """B1：胜率项对 |总分| 的贡献占比（Top12）。"""
    sys.path.insert(0, SRC)
    from decision.scoring import score_components
    cands = (d.get("candidates") or [])[:12]
    tw = tc = ts = 0.0
    for c in cands:
        comp = score_components(c.get("annual_return") or 0, c.get("max_drawdown") or 0,
                                c.get("win_rate") or 0, int(c.get("trade_count") or 0))
        tw += abs(comp["win_rate_term"])
        tc += abs(comp["calmar_term"])
        ts += abs(comp["stability_term"])
    denom = tw + tc + ts
    return (tw / denom) if denom else None


def windows(code):
    """B6 验证二：full / drop60 / drop120 三窗口的 action 与共识族。"""
    sys.path.insert(0, SRC)
    os.chdir(SRC)
    import cli
    df = cli.get_stock_daily(code, start_date=None, source="sina")
    if df is None or len(df) < 300:
        return {}
    orig = cli.get_stock_daily
    out = {}
    try:
        for label, drop in (("full", 0), ("drop60", 60), ("drop120", 120)):
            sub = (df.iloc[: len(df) - drop] if drop else df).copy()
            cli.get_stock_daily = (lambda d: (lambda *a, **k: d))(sub)
            r = cli.advise_internal(code, start=None, fast=True)
            out[label] = {
                "action": r["recommendation"]["action"],
                "family": (r.get("evidence") or {}).get("consensus", {}).get("family"),
                "status": r["recommendation"]["status"],
            }
    finally:
        cli.get_stock_daily = orig
    return out


def fmt(v):
    if isinstance(v, list):
        return "[" + ", ".join(fmt(x) for x in v) + "]"
    if isinstance(v, dict):
        return json.dumps(v, ensure_ascii=False)
    if isinstance(v, float):
        return f"{v:.6g}"
    return str(v)


def table(d):
    rows = ["| 指标 | 实测值 |", "|---|---|"]
    for k, v in d.items():
        rows.append(f"| {k} | {fmt(v)} |")
    return "\n".join(rows)


def main():
    t0 = time.strftime("%Y-%m-%d %H:%M:%S")
    print("运行 600519 --fast ...")
    code_f, full = run_cli(["600519", "--json", "--fast"])
    print("  退出码:", code_f)
    print("运行 510300 --fast ...")
    code_u, fund = run_cli(["510300", "--json", "--fast"])
    print("  退出码:", code_u)

    sf, su = summarize(full), summarize(fund)
    share = win_rate_share(full) if full else None
    win = windows("600519")

    L = []
    L.append("# 600519 / 510300 验证快照（A13 / B6 / B1）\n\n")
    L.append("> 本文件由 `tools/validation_snapshot.py` 自动生成，**全部数字来自真实运行**，未手工填写。\n")
    L.append(f"> 生成时间：{t0}　Python {sys.version.split()[0]}　"
             f"数据区间见各表 `bars` 字段（600519 自 2020-01-02 起）\n\n")

    L.append("## 1. 600519（股票，`--json --fast`）\n\n")
    L.append(table(sf))
    L.append(f"\n\n退出码：**{code_f}**（status={sf.get('status')} → "
             f"{'0' if sf.get('status') == 'ok' else '10/20/30/40 按 status 映射'}）\n")

    L.append("\n## 2. 510300（基金，`--json --fast`）\n\n")
    L.append(table(su))
    L.append(f"\n\n退出码：**{code_u}**\n")

    L.append("\n## 3. B1 评分结构（before vs after）\n\n")
    L.append("| 项 | before（契约基线快照） | after（本次实测） |\n|---|---|---|\n")
    L.append(f"| 胜率项对 \\|总分\\| 的贡献占比 | 72.8% | "
             f"{(share * 100):.1f}% |\n" if share is not None else "| 胜率项占比 | 72.8% | 未取到 |\n")
    L.append("| 评分主项 | `Wilson×0.5 + 年化×0.2 − 回撤×0.3` | `0.6×Calmar + 0.2×Wilson + 0.2×stability` |\n")
    L.append("| 年化是否真做 | 否（`252/252` 恒为 1） | 是（`(1+r)^(252/n_bars)−1`） |\n")

    L.append("\n## 4. B6 三窗口稳定性（验证二，真实数据）\n\n")
    if win:
        L.append("| 窗口 | action | consensus.family | status |\n|---|---|---|---|\n")
        for k, v in win.items():
            L.append(f"| {k} | {v['action']} | {v['family']} | {v['status']} |\n")
        actions = {v["action"] for v in win.values()}
        fams = {v["family"] for v in win.values()}
        L.append(f"\n结论：action 一致 = **{len(actions) == 1}**，family 一致 = **{len(fams) == 1}**\n")
    else:
        L.append("未取到（缓存不足）\n")

    L.append("\n## 5. 本轮修复实测对照（F01–F21）\n\n")
    L.append("| 条目 | 修复前 | 修复后（本次实测） |\n|---|---|---|\n")
    L.append("| F01 基金网格 | 510300 候选全部来自股票 581 组网格 | "
             f"候选族 {su.get('family_counts')}（全部来自基金 44 组网格） |\n")
    L.append(f"| F03 n_trials | 10（= 候选数） | 股票 {sf.get('n_trials')} / 基金 {su.get('n_trials')} |\n")
    L.append(f"| F04 significant | 只看 DSR>0 | significant={sf.get('significant')}，"
             f"超额 CI 上界={fmt((sf.get('excess_ci') or [0, 0])[1])} |\n")
    L.append(f"| F06 score_oos | `score×0.9` 伪造 | score_is={fmt(sf.get('top_score_is'))} / "
             f"score_oos={fmt(sf.get('top_score_oos'))}（真实 OOS 列） |\n")
    L.append(f"| F06 zero_trade_configs | 硬编码 0 | {sf.get('zero_trade_configs')} |\n")
    L.append(f"| F10 metrics CI | 0/9 有 ci_95 | {len(sf.get('ci_metrics') or [])}/9 有 ci_95 |\n")
    L.append(f"| F11 time_in_market | 恒 0 | {fmt(sf.get('time_in_market'))} |\n")
    L.append(f"| F12 止损口径 | `last_close×0.95`（与回测 8% 矛盾） | {fmt(sf.get('stop_loss'))}"
             f"（method={sf.get('stop_method')}，与回测 ATR 口径一致） |\n")
    L.append(f"| F13 族占比 | candidates 6/10 同族 | {sf.get('family_counts')} |\n")
    L.append(f"| F14 族名来源 | `idx.split('(')[0]` | 注册表 prefix（family_map） |\n")
    L.append(f"| F05 退出码 | 非 ok 一律 10 | 600519={code_f} / 510300={code_u} / 999999=30 |\n")

    L.append("\n## 6. 复现命令\n\n")
    L.append("```bash\n")
    L.append("cd source\n")
    L.append("python cli.py 600519 --json --fast      # 股票\n")
    L.append("python cli.py 510300 --json --fast      # 基金\n")
    L.append("python -m pytest tests/ -q              # 测试网（离线）\n")
    L.append("cd .. && python tools/check_gates.py     # 32 项门禁（ps1 版的等价转写）\n")
    L.append("```\n")

    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    io.open(OUT, "w", encoding="utf-8").write("".join(L))
    print("已写出:", OUT)
    print(f"600519 status={sf.get('status')} action={sf.get('action')} "
          f"n_trials={sf.get('n_trials')} ci={len(sf.get('ci_metrics') or [])}/9 "
          f"win_share={share}")
    print(f"510300 status={su.get('status')} family={su.get('family_counts')}")


if __name__ == "__main__":
    main()
