#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""R10 报告生成：把 summary.json 渲染成 docs/STRATEGY_REDESIGN_V2.md 的表格。

用法：
    .venv/bin/python tools/render_v2_report.py --data-dir ~/data/stock-universe
"""
from __future__ import annotations

import argparse
import json
import os

R6 = {
    "trend_donchian_h1": dict(ann=-11.35, sr=-0.41, mdd=69.4, turn=7242, dsr=0.006,
                              oos=-8.79, k=2147),
    "trend_donchian_h5": dict(ann=9.20, sr=0.50, mdd=36.6, turn=2597, dsr=0.531,
                              oos=12.35, k=429),
    "trend_donchian_h20": dict(ann=14.32, sr=0.69, mdd=32.0, turn=913, dsr=0.723,
                               oos=13.12, k=107),
    "trend_donchian_h60": dict(ann=12.24, sr=0.60, mdd=40.3, turn=344, dsr=0.588,
                               oos=26.79, k=35),
    "trend_donchian_h120": dict(ann=16.86, sr=0.77, mdd=37.7, turn=170, dsr=0.737,
                                oos=34.20, k=17),
    "xsmom_12_1_h20": dict(ann=12.76, sr=0.53, mdd=50.9, turn=309, dsr=0.525,
                           oos=6.55, k=107),
    "xsmom_12_1_h60": dict(ann=17.88, sr=0.68, mdd=41.4, turn=180, dsr=0.713,
                           oos=34.51, k=35),
    "xsmom_12_1_h120": dict(ann=22.64, sr=0.84, mdd=44.0, turn=119, dsr=0.834,
                            oos=45.66, k=17),
    "mr_rsi2_h5": dict(ann=-6.15, sr=-0.17, mdd=58.0, turn=4182, dsr=0.026,
                       oos=-15.72, k=429),
    "mr_boll_h5": dict(ann=-7.35, sr=-0.23, mdd=54.2, turn=3656, dsr=0.020,
                       oos=-11.00, k=429),
    "lowvol60_h20": dict(ann=5.41, sr=0.44, mdd=24.4, turn=262, dsr=0.449,
                         oos=11.19, k=107),
    "regime_trend_h20": dict(ann=10.29, sr=0.63, mdd=34.6, turn=516, dsr=0.694,
                             oos=10.06, k=107),
    "value_trend_h20": dict(ann=9.26, sr=0.55, mdd=32.5, turn=797, dsr=0.551,
                            oos=6.04, k=107),
}
R6_EW = dict(ann=14.26, sr=0.77, mdd=27.6)
R6_IDX = dict(ann=-0.13, sr=0.09, mdd=45.6)


def pct(x, digits=2):
    return "n/a" if x is None else f"{x * 100:.{digits}f}%"


def pp(x, digits=2):
    return "n/a" if x is None else f"{x * 100:+.{digits}f}pp"


def num(x, digits=2):
    return "n/a" if x is None else f"{x:.{digits}f}"


def mark(ok):
    return "✓" if ok else "✗"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-dir", default=os.path.expanduser("~/data/stock-universe"))
    args = ap.parse_args()
    summary = json.load(open(os.path.join(args.data_dir, "out", "summary.json")))
    verify_path = os.path.join(args.data_dir, "verify-report.json")
    verify = json.load(open(verify_path)) if os.path.exists(verify_path) else None

    meta = summary["meta"]
    base = summary["baselines"]
    ew = base["ew_buy_hold_unbiased"]
    idx = base["csi300_index"]
    surv = base["survivor_subset_ew"]
    csi = base.get("current_constituent_ew")
    bias = base["bias_magnitude"]
    cands = summary["candidates"]
    out = []

    out.append("<!-- 由 tools/render_v2_report.py 生成；数字来源 "
               f"`{args.data_dir}/out/summary.json`（{meta['generated']}） -->")
    out.append("")

    # ---- 数据 ----
    out.append("### 数据规模与校验\n")
    out.append("| 项 | 值 |")
    out.append("|---|---|")
    out.append(f"| 全池（manifest status=ok） | {meta['n_universe']} 只 |")
    out.append(f"| 排除 `exit_reason=merger` | {meta['n_excluded_merger']} 只 |")
    out.append(f"| 实际参与回测 | {meta['n_codes']} 只 |")
    out.append(f"| 其中退市票（`price` 类，已结算） | {meta['n_delisted_used']} 只 |")
    dq = meta["data_quality"]
    out.append(f"| 动态池规模（日均 / min / max） | {dq['dynamic_pool_mean']:.0f} / "
               f"{dq['dynamic_pool_min']} / {dq['dynamic_pool_max']} 只 |")
    out.append(f"| 日历 | {meta['date_range'][0]} ~ {meta['date_range'][1]}"
               f"（{dq['dates']} 天） |")
    out.append(f"| 评估起点 | {meta['eval_start']} |")
    if verify:
        out.append(f"| md5 与 R9 manifest 一致 | **{verify['md5_match']}** |")
        out.append(f"| md5 不一致 | **{verify['md5_mismatch']}** |")
        out.append(f"| 行数合计 | {verify['rows_total']:,} |")
    out.append("")

    # ---- 偏差 ----
    out.append("### 幸存者偏差量级\n")
    out.append("| 口径 | 年化 | Sharpe | maxDD | 与无偏池等权之差 |")
    out.append("|---|---:|---:|---:|---:|")
    out.append(f"| 无偏池等权 | {pct(ew['ann_return'])} | {num(ew['sharpe'])} | "
               f"{pct(ew['max_drawdown'], 1)} | — |")
    out.append(f"| 幸存者子集等权（{surv['n_alive']} 只未退市） | "
               f"{pct(surv['ann_return'])} | {num(surv['sharpe'])} | "
               f"{pct(surv['max_drawdown'], 1)} | "
               f"{pp(bias['survivor_minus_unbiased_pp'] / 100)} |")
    if csi:
        out.append(f"| 当前沪深 300 成分等权（300 只） | {pct(csi['ann_return'])} | "
                   f"{num(csi['sharpe'])} | {pct(csi['max_drawdown'], 1)} | "
                   f"{pp(bias['current_constituent_minus_unbiased_pp'] / 100)} |")
    out.append(f"| 沪深 300 指数 | {pct(idx['ann_return'])} | {num(idx['sharpe'])} | "
               f"{pct(idx['max_drawdown'], 1)} | "
               f"{pp(bias['index_minus_unbiased_pp'] / 100)} |")
    out.append("")
    out.append(f"> R6 口径：当前成分等权 {R6_EW['ann']}% vs 指数 {R6_IDX['ann']}%，"
               f"差 {R6_EW['ann'] - R6_IDX['ann']:.1f}pp/年。")
    out.append("")

    # ---- 候选 ----
    out.append("### 13 个候选（扣费、组合层面）\n")
    out.append("| 候选 | 族 | H | 年化 | Sharpe | maxDD | 换手/年 | 命中率 | 暴露 | K |")
    out.append("|---|---|---:|---:|---:|---:|---:|---:|---:|---:|")
    for c in cands:
        out.append(f"| {c['id']} | {c['family']} | {c['horizon']} | "
                   f"{pct(c['ann_return'])} | {num(c['sharpe'])} | "
                   f"{pct(c['max_drawdown'], 1)} | "
                   f"{c['turnover_annual'] * 100:.0f}% | {pct(c['hit_rate'], 1)} | "
                   f"{num(c['exposure'])} | {c['k_periods']} |")
    out.append("")

    # ---- 推断 ----
    out.append("### 推断：Bonferroni / DSR / OOS / 功效\n")
    out.append("| 候选 | p(周期) | Bonferroni | DSR(日) | DSR(周期) | OOS | IS | "
               "MDE年化 | p随机(iid) | p随机(换手) | 推荐 |")
    out.append("|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|:---:|")
    for c in cands:
        out.append(f"| {c['id']} | {num(c['p_period_one_sided'], 3)} | "
                   f"{num(c['p_bonferroni'], 3)} | {num(c['dsr'], 3)} | "
                   f"{num(c['dsr_period'], 3)} | {pct(c['oos_ann'])} | "
                   f"{pct(c['is_ann'])} | {pct(c['mde_ann_return'], 1)} | "
                   f"{num(c['random_p_ge_ann_iid'], 3)} | "
                   f"{num(c['random_p_ge_ann_turnover'], 3)} | "
                   f"{'是' if c['recommend'] else '否'} |")
    out.append("")

    # ---- R6 对比 ----
    out.append("### 与 R6 逐候选对比\n")
    out.append("| 候选 | R6 年化 | R10 年化 | Δ | R6 maxDD | R10 maxDD | Δ | "
               "R6 Sharpe | R10 Sharpe | Δ | R6 DSR(周期) | R10 DSR(周期) |")
    out.append("|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|")
    for c in cands:
        r = R6.get(c["id"])
        if not r:
            continue
        out.append(
            f"| {c['id']} | {r['ann']:.2f}% | {pct(c['ann_return'])} | "
            f"{c['ann_return'] * 100 - r['ann']:+.2f}pp | "
            f"{r['mdd']:.1f}% | {pct(c['max_drawdown'], 1)} | "
            f"{c['max_drawdown'] * 100 - r['mdd']:+.1f}pp | "
            f"{r['sr']:.2f} | {num(c['sharpe'])} | "
            f"{c['sharpe'] - r['sr']:+.2f} | "
            f"{r['dsr']:.3f} | {num(c['dsr_period'], 3)} |")
    out.append(f"| ew_buy_hold | {R6_EW['ann']:.2f}% | {pct(ew['ann_return'])} | "
               f"{ew['ann_return'] * 100 - R6_EW['ann']:+.2f}pp | "
               f"{R6_EW['mdd']:.1f}% | {pct(ew['max_drawdown'], 1)} | "
               f"{ew['max_drawdown'] * 100 - R6_EW['mdd']:+.1f}pp | "
               f"{R6_EW['sr']:.2f} | {num(ew['sharpe'])} | "
               f"{ew['sharpe'] - R6_EW['sr']:+.2f} | — | — |")
    out.append(f"| csi300_index | {R6_IDX['ann']:.2f}% | {pct(idx['ann_return'])} | "
               f"{idx['ann_return'] * 100 - R6_IDX['ann']:+.2f}pp | "
               f"{R6_IDX['mdd']:.1f}% | {pct(idx['max_drawdown'], 1)} | "
               f"{idx['max_drawdown'] * 100 - R6_IDX['mdd']:+.1f}pp | "
               f"{R6_IDX['sr']:.2f} | {num(idx['sharpe'])} | "
               f"{idx['sharpe'] - R6_IDX['sr']:+.2f} | — | — |")
    out.append("")

    # ---- X/Y/Z ----
    out.append("### X / Y / Z 判定\n")
    out.append("| 候选 | X | Δ年化 vs EW | Y | ΔmaxDD vs EW | Z | 随机 p |")
    out.append("|---|---|---:|---|---:|---|---:|")
    for c in cands:
        xyz = c["xyz"]
        out.append(f"| {c['id']} | {xyz['X']['verdict']} | "
                   f"{pp(xyz['Y']['delta_ann_vs_ew'])} | {xyz['Y']['verdict']} | "
                   f"{pp(xyz['Z']['delta_maxdd_vs_ew'])} | "
                   f"{xyz['Z']['verdict']} | {num(c['random_p_ge_ann'], 3)} |")
    out.append("")

    # ---- 年度 ----
    out.append("### 年度稳定性\n")
    years = sorted({int(y) for c in cands for y in c["yearly"]})
    out.append("| 候选 | " + " | ".join(str(y) for y in years) + " |")
    out.append("|---|" + "---:|" * len(years))
    for c in cands:
        cells = []
        for y in years:
            v = c["yearly"].get(str(y))
            cells.append(pct(v["return"], 1) if v else "—")
        out.append(f"| {c['id']} | " + " | ".join(cells) + " |")
    out.append("")

    # ---- walk-forward ----
    out.append("### Walk-forward（3 年训练 → 1 年测试）\n")
    out.append("| 测试年 | 选中候选 | 训练 Sharpe | 测试收益 |")
    out.append("|---:|---|---:|---:|")
    for y in summary["walk_forward"]["years"]:
        out.append(f"| {y['test_year']} | {y['chosen']} | "
                   f"{num(y['train_sharpe'])} | {pct(y['test_return'])} |")
    out.append(f"\n复合 OOS 年化 **{pct(summary['walk_forward']['composite_ann'])}**"
               f"（{summary['walk_forward']['n_years']} 年）。")
    out.append("")

    # ---- 判定逐项 ----
    out.append("### 9 条预注册判定逐项\n")
    keys = ["beats_ew", "beats_index", "beats_random", "bonferroni", "dsr",
            "oos_positive", "dd_no_worse", "yearly_positive", "power_ok"]
    out.append("| 候选 | " + " | ".join(keys) + " |")
    out.append("|---|" + ":---:|" * len(keys))
    for c in cands:
        out.append(f"| {c['id']} | " + " | ".join(
            mark(c["checks"][k]) for k in keys) + " |")
    out.append("")

    print("\n".join(out))


if __name__ == "__main__":
    main()
