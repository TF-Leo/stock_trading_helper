#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""R12 / #146 —— H3：一致性检验（**不是**因果检验）。

命题：合谋资金 → 高管参与 → 质押加杠杆 → 批量收割。

本脚本只做两件事：
1. 市场层面质押比例变化 vs 沪深300 后续 20/60 日最大回撤；
2. 两融余额增速 vs 沪深300 后续 20 日实现波动。

判据：只报相关系数与置换检验 p 值，**不声称因果**。相关不等于因果；
本报告不能证实「收割链条」。详见 docs/R12_EXTERNAL_DRIVERS.md 的
「本报告不能证明什么」。

数据：``~/data/r12/pledge_monthly.csv``（月末采样，2020-2025）、
``~/data/r12/margin_daily.csv``、``~/data/r12/csi300_daily.csv``。

用法
----
    .venv/bin/python tools/r12_h3.py
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
import r12_lib as L


def _perm_pvalue(x: np.ndarray, y: np.ndarray, n: int = 20000, seed: int = 7) -> float:
    """Spearman ρ 的双侧置换检验 p 值（不依赖 scipy）。"""
    if len(x) < 5:
        return float("nan")
    rho = L.spearman_rho(x, y)
    rng = np.random.default_rng(seed)
    cnt = 0
    for _ in range(n):
        perm = rng.permutation(y)
        if abs(L.spearman_rho(x, perm)) >= abs(rho):
            cnt += 1
    return float((cnt + 1) / (n + 1))


def _nearest_index(dates: pd.DatetimeIndex, targets: pd.Series) -> np.ndarray:
    """把目标日期映射到 >= 该日期的第一个交易日（避免用未来信息）。"""
    pos = np.searchsorted(dates.values, targets.values, side="left")
    return np.clip(pos, 0, len(dates) - 1)


def pledge_vs_drawdown() -> dict:
    close = L.load_daily("csi300_daily")
    fwd20 = L.forward_drawdown(close, 20)
    fwd60 = L.forward_drawdown(close, 60)
    p = pd.read_csv(L.CACHE / "pledge_monthly.csv")
    p["date"] = pd.to_datetime(p["date"])
    p = p.drop_duplicates("date").sort_values("date").reset_index(drop=True)
    p["d_median"] = p["median_ratio"].diff()
    p["d_pct30"] = p["pct_ge_30"].diff()
    pos = _nearest_index(close.index, p["date"])
    p["date_on"] = close.index[pos]
    p["dd20"] = fwd20.to_numpy()[pos]
    p["dd60"] = fwd60.to_numpy()[pos]
    out = {"n_snapshots": int(len(p)), "first": str(p["date"].min().date()),
           "last": str(p["date"].max().date()), "pairs": {}}
    pairs = [("d_median", "dd20"), ("d_median", "dd60"),
             ("d_pct30", "dd20"), ("d_pct30", "dd60"),
             ("median_ratio", "dd20"), ("median_ratio", "dd60")]
    for a, b in pairs:
        m = p[a].notna() & p[b].notna()
        rho, n = L.spearman(p.loc[m, a], p.loc[m, b])
        out["pairs"][f"{a}~{b}"] = {
            "spearman": rho, "n": n,
            "perm_p": _perm_pvalue(p.loc[m, a].to_numpy(), p.loc[m, b].to_numpy()),
        }
    out["table"] = p[["date", "median_ratio", "d_median", "pct_ge_30", "dd20", "dd60"]] \
        .tail(12).to_dict(orient="records")
    return out


def margin_vs_vol() -> dict:
    close = L.load_daily("csi300_daily")
    ret = np.log(close).diff()
    margin = L.load_daily("margin_daily", "total")
    out = {}
    # 周频
    m_w = L.period_end(margin, "W")
    g_w = np.log(m_w).diff()
    v_w = L.period_end(L.forward_vol(ret, 20), "W")
    d = pd.concat([g_w, v_w], axis=1, keys=["g", "v"]).dropna()
    rho, n = L.spearman(d["g"], d["v"])
    out["weekly_growth_vs_fwd20_vol"] = {
        "spearman": rho, "n": n,
        "perm_p": _perm_pvalue(d["g"].to_numpy(), d["v"].to_numpy()),
    }
    # 月频
    m_m = L.period_end(margin, "M")
    g_m = np.log(m_m).diff()
    v_m = L.period_end(L.forward_vol(ret, 20), "M")
    d2 = pd.concat([g_m, v_m], axis=1, keys=["g", "v"]).dropna()
    rho2, n2 = L.spearman(d2["g"], d2["v"])
    out["monthly_growth_vs_fwd20_vol"] = {
        "spearman": rho2, "n": n2,
        "perm_p": _perm_pvalue(d2["g"].to_numpy(), d2["v"].to_numpy()),
    }
    # 两融增速 vs 后续 20 日回撤（附加）
    dd20 = L.forward_drawdown(close, 20)
    dd_m = L.period_end(dd20, "M")
    d3 = pd.concat([g_m, dd_m], axis=1, keys=["g", "dd"]).dropna()
    rho3, n3 = L.spearman(d3["g"], d3["dd"])
    out["monthly_growth_vs_fwd20_drawdown"] = {
        "spearman": rho3, "n": n3,
        "perm_p": _perm_pvalue(d3["g"].to_numpy(), d3["dd"].to_numpy()),
    }
    return out


def main() -> int:
    if not (L.CACHE / "pledge_monthly.csv").exists():
        print("缺少 pledge_monthly.csv，先运行 tools/r12_fetch.py --only h3")
        return 1
    res = {
        "pledge_vs_drawdown": pledge_vs_drawdown(),
        "margin_vs_vol": margin_vs_vol(),
        "causal_caveat": "相关不等于因果；本检验不能识别合谋、高管参与或收割链条。",
    }
    print("== 质押比例 vs 后续回撤 ==")
    pv = res["pledge_vs_drawdown"]
    print(f"快照 {pv['n_snapshots']} 个（{pv['first']} → {pv['last']}）")
    for k, v in pv["pairs"].items():
        print(f"  {k:28s} rho={v['spearman']:+.3f} n={v['n']:3d} perm_p={v['perm_p']:.4f}")
    print("\n== 两融增速 vs 后续波动 ==")
    for k, v in res["margin_vs_vol"].items():
        print(f"  {k:38s} rho={v['spearman']:+.3f} n={v['n']:4d} perm_p={v['perm_p']:.4f}")
    print(f"\n结果已写入 {L.save_json('h3_results.json', res)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
