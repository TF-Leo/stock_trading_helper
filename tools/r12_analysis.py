#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""R12 / issue #146 —— H1：外部四因子能否解释并预测沪深300收益。

预注册判据（不得事后调整）
--------------------------
* 成立：样本外 R²(C) ≥ 0.20 **且** 比 max(基准A, 基准B) 高 ≥ 0.10；方向命中率 > 55%。
* 推翻：样本外 R²(C) ≤ 0.05，或不如基准 B。

口径
----
* 因变量：沪深300 期间**对数收益**（周/月），不用指数水平值；
* 基准 A：随机游走（预测 0）与扩展窗口 AR(1)；
* 基准 B：国内因子（M2 同比变化、SHIBOR 3M 变化、两融余额增速、北向净流入/两融）；
* 模型 C：外部四因子（USDCNY 变动、S&P500 收益、恒生收益、美债 10Y 变动）；
* 样本外：扩展窗口，首训 ≤ 2018-12-31，2019-01 起逐期预测；
* OOS R² 以「扩展窗口历史均值」为基准（标准 Campbell–Thompson 口径），
  另报相对 0 的口径；DM 检验用平方损失 + Newey-West。

只报告，不写产品逻辑。结果落 ``~/data/r12/h1_results.json``。

用法
----
    .venv/bin/python tools/r12_analysis.py
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
import r12_lib as L


def _ols(y: np.ndarray, X: np.ndarray) -> dict:
    n = len(y)
    A = np.column_stack([np.ones(n), X])
    b, *_ = np.linalg.lstsq(A, y, rcond=None)
    resid = y - A @ b
    sse = float(resid @ resid)
    sst = float(((y - y.mean()) ** 2).sum())
    dof = max(n - A.shape[1], 1)
    s2 = sse / dof
    try:
        cov = s2 * np.linalg.pinv(A.T @ A)
        se = np.sqrt(np.clip(np.diag(cov), 0, None))
    except np.linalg.LinAlgError:
        se = np.full(A.shape[1], np.nan)
    return {
        "n": n,
        "coef": [float(v) for v in b],
        "t": [float(v) for v in (b / np.where(se > 0, se, np.nan))],
        "r2": float(1 - sse / sst) if sst > 0 else float("nan"),
    }


def _in_sample(panel: pd.DataFrame, factors: list[str], end: str) -> dict:
    d = panel.loc[:end]
    mask = d["ret"].notna() & d[factors].notna().all(axis=1)
    if mask.sum() < len(factors) + 5:
        return {"n": int(mask.sum()), "r2": float("nan"), "coef": [], "t": []}
    res = _ols(d.loc[mask, "ret"].to_numpy(), d.loc[mask, factors].to_numpy())
    res["names"] = ["const"] + factors
    return res


def _full_sample(panel: pd.DataFrame, factors: list[str]) -> dict:
    mask = panel["ret"].notna() & panel[factors].notna().all(axis=1)
    if mask.sum() < len(factors) + 5:
        return {"n": int(mask.sum()), "r2": float("nan"), "coef": [], "t": []}
    res = _ols(panel.loc[mask, "ret"].to_numpy(), panel.loc[mask, factors].to_numpy())
    res["names"] = ["const"] + factors
    return res


def _mean_forecast(y: pd.Series) -> pd.Series:
    return y.expanding(min_periods=10).mean().shift(1)


def _ar1_forecast(y: pd.Series, first_oos: str) -> pd.Series:
    X = pd.DataFrame({"lag": y.shift(1)}, index=y.index)
    return L.expanding_forecast(y, X, first_oos)


def run_variant(name: str, freq: str, start: str, first_oos: str,
                factors_b: list[str], factors_c: list[str],
                note: str = "", align: str = "period") -> dict:
    panel = L.build_panel(freq, align=align).loc[start:]
    y = panel["ret"]
    mean_f = _mean_forecast(y)
    ar1_f = _ar1_forecast(y, first_oos)
    b_pred = L.expanding_forecast(y, panel[factors_b], first_oos) if factors_b else None
    c_pred = L.expanding_forecast(y, panel[factors_c], first_oos)

    m = y.index >= pd.Timestamp(first_oos)
    out = {
        "name": name, "freq": freq, "start": str(panel.index.min().date()),
        "first_oos": first_oos, "note": note, "align": align,
        "n_total": int(len(panel)), "n_oos": int((m & mean_f.notna()).sum()),
        "factors_b": factors_b, "factors_c": factors_c,
    }
    bench = mean_f
    out["bench"] = {
        "rw0_r2_vs_mean": L.oos_r2(y[m], pd.Series(0.0, index=y.index)[m], bench[m]),
        "ar1_r2_vs_mean": L.oos_r2(y[m], ar1_f[m], bench[m]),
        "ar1_hit": L.direction_hit(y[m], ar1_f[m]),
    }
    if b_pred is not None:
        out["B"] = {
            "r2_vs_mean": L.oos_r2(y[m], b_pred[m], bench[m]),
            "r2_vs_zero": L.oos_r2(y[m], b_pred[m], pd.Series(0.0, index=y.index)[m]),
            "hit": L.direction_hit(y[m], b_pred[m]),
            "dm_vs_A": L.dm_test(y[m] - b_pred[m], y[m] - bench[m]),
        }
        out["B"]["in_sample"] = _in_sample(panel, factors_b, "2018-12-31")
        out["B"]["full_sample"] = _full_sample(panel, factors_b)
    out["C"] = {
        "r2_vs_mean": L.oos_r2(y[m], c_pred[m], bench[m]),
        "r2_vs_zero": L.oos_r2(y[m], c_pred[m], pd.Series(0.0, index=y.index)[m]),
        "hit": L.direction_hit(y[m], c_pred[m]),
        "dm_vs_A": L.dm_test(y[m] - c_pred[m], y[m] - bench[m]),
        "dm_vs_B": (L.dm_test(y[m] - c_pred[m], y[m] - b_pred[m]) if b_pred is not None
                    else (float("nan"), float("nan"))),
        "in_sample": _in_sample(panel, factors_c, "2018-12-31"),
        "full_sample": _full_sample(panel, factors_c),
    }
    # 预注册判据
    best_bench = max(out["bench"]["rw0_r2_vs_mean"], out["bench"]["ar1_r2_vs_mean"],
                     out["B"]["r2_vs_mean"] if "B" in out and np.isfinite(out["B"]["r2_vs_mean"]) else -9)
    out["verdict"] = {
        "best_baseline_r2": best_bench,
        "C_minus_best_baseline": out["C"]["r2_vs_mean"] - best_bench,
        "C_minus_020_threshold": out["C"]["r2_vs_mean"] - 0.20,
        "supported": bool(out["C"]["r2_vs_mean"] >= 0.20
                          and out["C"]["r2_vs_mean"] - best_bench >= 0.10
                          and out["C"]["hit"] > 0.55),
        "rejected": bool(out["C"]["r2_vs_mean"] <= 0.05
                         or ("B" in out and out["C"]["r2_vs_mean"] < out["B"]["r2_vs_mean"])),
        "barely_above_threshold": bool(0.20 <= out["C"]["r2_vs_mean"] < 0.22),
    }
    return out


VARIANTS = [
    # 主设定（预注册）
    ("weekly_2017_primary", "W", "2017-03-01", "2019-01-01",
     L.DOMESTIC, L.EXTERNAL, "全因子；外部为同期（#146 拟合口径）"),
    ("monthly_2010_primary", "M", "2010-01-01", "2019-01-01",
     ["d_m2_yoy", "margin_g"], L.EXTERNAL,
     "月频主设定：SHIBOR 2017 前无连续日频，北向 2024-08 停披露 → B 只用 M2+两融"),
    ("monthly_2017_full", "M", "2017-03-01", "2019-01-01",
     L.DOMESTIC, L.EXTERNAL, "月频全因子（样本短，仅稳健性）"),
    # 外部因子替代口径
    ("weekly_2017_offshore", "W", "2017-03-01", "2019-01-01",
     L.DOMESTIC, ["fx_off", "spx", "hsi", "d_us10y"], "USDCNY 用离岸 USDCNH"),
    ("monthly_2010_offshore", "M", "2010-01-01", "2019-01-01",
     ["d_m2_yoy", "margin_g"], ["fx_off", "spx", "hsi", "d_us10y"], "USDCNY 用离岸"),
    # 严格滞后（可交易口径）
    ("weekly_2017_lagged", "W", "2017-03-01", "2019-01-01",
     L.DOMESTIC, [f"{c}_lag" for c in L.EXTERNAL], "外部因子滞后一期，严格可交易"),
    ("monthly_2010_lagged", "M", "2010-01-01", "2019-01-01",
     ["d_m2_yoy", "margin_g"], [f"{c}_lag" for c in L.EXTERNAL], "外部因子滞后一期"),
    # 长样本：C vs A
    ("weekly_2012_ext_only", "W", "2012-01-01", "2019-01-01", [], L.EXTERNAL,
     "长样本；无国内因子（SHIBOR/北向历史不足）"),
    ("monthly_2008_ext_only", "M", "2008-01-01", "2019-01-01", [], L.EXTERNAL,
     "月频长样本；C vs A"),
    ("weekly_2012_ext_offshore", "W", "2012-01-01", "2019-01-01", [],
     ["fx_off", "spx", "hsi", "d_us10y"], "长样本 + 离岸汇率"),
]

# 双档报告：同一期间 vs 信息可得。samegrid / avail 用同一套期间边界，
# 差别只在「外盘窗口右端是否包含 A 股收盘之后的收盘」。
ALIGN_VARIANTS = [
    ("weekly_2017_samegrid", "W", "2017-03-01", "2019-01-01",
     L.DOMESTIC, L.EXTERNAL,
     "同一期间（同一网格重算；含周五美股夜盘与恒生 15:00–16:00）", "samegrid"),
    ("weekly_2017_avail", "W", "2017-03-01", "2019-01-01",
     L.DOMESTIC, L.EXTERNAL,
     "信息可得（外盘取 A 股期间末收盘前最后一根）", "avail"),
    ("monthly_2010_samegrid", "M", "2010-01-01", "2019-01-01",
     ["d_m2_yoy", "margin_g"], L.EXTERNAL,
     "月频同一期间（同一网格重算）", "samegrid"),
    ("monthly_2010_avail", "M", "2010-01-01", "2019-01-01",
     ["d_m2_yoy", "margin_g"], L.EXTERNAL,
     "月频信息可得", "avail"),
    ("weekly_2017_avail_offshore", "W", "2017-03-01", "2019-01-01",
     L.DOMESTIC, ["fx_off", "spx", "hsi", "d_us10y"],
     "信息可得 + 离岸汇率（稳健性）", "avail"),
]

# 因子分解：逐因子分别在「同一期间」与「信息可得」两档口径下重算
FACTOR_SETS = [
    ("四因子", L.EXTERNAL),
    ("仅恒生", ["hsi"]),
    ("仅S&P500", ["spx"]),
    ("仅USDCNY(在岸)", ["fx_on"]),
    ("仅USDCNH(离岸)", ["fx_off"]),
    ("仅美债10Y", ["d_us10y"]),
]


def factor_decomposition(freq: str, start: str, first_oos: str) -> dict:
    """逐因子在同一期间 / 信息可得两档口径下的 OOS R² 与命中率。"""
    out: dict = {}
    for align in ("samegrid", "avail"):
        panel = L.build_panel(freq, align=align).loc[start:]
        y = panel["ret"]
        bench = _mean_forecast(y)
        m = y.index >= pd.Timestamp(first_oos)
        out[align] = {}
        for label, factors in FACTOR_SETS:
            pred = L.expanding_forecast(y, panel[factors], first_oos)
            out[align][label] = {
                "r2": L.oos_r2(y[m], pred[m], bench[m]),
                "hit": L.direction_hit(y[m], pred[m]),
            }
    return out


def main() -> int:
    print("== 单位根 / 自相关（日频、周频、月频收益）==")
    diag = {}
    for freq, label in (("D", "daily"), ("W", "weekly"), ("M", "monthly")):
        if freq == "D":
            s = L.log_ret(L.load_daily("csi300_daily"))
        else:
            s = L.period_end(L.load_daily("csi300_daily"), freq)
            s = L.log_ret(s)
        diag[label] = {"adf": L.adf_tstat(s), "acf1": L.acf1(s), "n": int(s.notna().sum())}
        print(f"{label:8s} n={diag[label]['n']:5d} ADF t={diag[label]['adf']['t']:6.2f} "
              f"(lags={diag[label]['adf']['lags']}) ACF(1)={diag[label]['acf1']:6.3f}")

    corr = {}
    corr_model = {}
    for freq, label in (("W", "weekly"), ("M", "monthly")):
        panel = L.build_panel(freq)
        corr[label] = {k: float(v) for k, v in
                       panel[["ret"] + L.EXTERNAL + L.DOMESTIC].corr()["ret"].items()}
        panel17 = panel.loc["2017-03-01":]
        corr_model[label] = {k: float(v) for k, v in
                             panel17[["ret"] + L.EXTERNAL + L.DOMESTIC].corr()["ret"].items()}
        print(f"{label} 与沪深300收益的同期相关（全样本）："
              + ", ".join(f"{k}={v:+.3f}" for k, v in corr[label].items() if k != "ret"))
        print(f"{label} 与沪深300收益的同期相关（2017-03 起）："
              + ", ".join(f"{k}={v:+.3f}" for k, v in corr_model[label].items() if k != "ret"))

    leadlag = L.lead_lag_daily()
    print("\n== 日频 lead-lag（2017-03 起，对数收益）==")
    for k, v in leadlag.items():
        print(f"  {k:22s} {v:+.3f}")

    print("\n== 因子分解（同一期间 vs 信息可得）==")
    decomp = {
        "weekly": factor_decomposition("W", "2017-03-01", "2019-01-01"),
        "monthly": factor_decomposition("M", "2010-01-01", "2019-01-01"),
    }
    for freq_label, block in decomp.items():
        print(f"-- {freq_label}")
        for label, vals in block["samegrid"].items():
            av = block["avail"][label]
            print(f"   {label:16s} same R2={vals['r2']:+.3f} hit={vals['hit']:.3f} | "
                  f"avail R2={av['r2']:+.3f} hit={av['hit']:.3f}")

    results = {"diagnostics": diag, "correlations": corr,
               "correlations_model_sample": corr_model,
               "lead_lag_daily": leadlag,
               "factor_decomposition": decomp,
               "variants": []}
    for args in VARIANTS:
        r = run_variant(*args)
        results["variants"].append(r)
        c = r["C"]
        b = r.get("B", {})
        print(f"\n-- {r['name']} [{r['freq']}] {r['start']} → now | OOS {r['n_oos']} | {r['note']}")
        print(f"   A(rw0)  R2={r['bench']['rw0_r2_vs_mean']:+.3f} | "
              f"A(ar1) R2={r['bench']['ar1_r2_vs_mean']:+.3f} hit={r['bench']['ar1_hit']:.3f}")
        if b:
            print(f"   B       R2={b['r2_vs_mean']:+.3f} hit={b['hit']:.3f} "
                  f"DM(B,A)={b['dm_vs_A'][0]:+.2f} (p={b['dm_vs_A'][1]:.3f}) | "
                  f"IS R2={b['in_sample']['r2']:.3f}")
        print(f"   C       R2={c['r2_vs_mean']:+.3f} hit={c['hit']:.3f} "
              f"DM(C,A)={c['dm_vs_A'][0]:+.2f} (p={c['dm_vs_A'][1]:.3f}) "
              f"DM(C,B)={c['dm_vs_B'][0]:+.2f} (p={c['dm_vs_B'][1]:.3f}) | "
              f"IS R2={c['in_sample']['r2']:.3f}")

    results["align_variants"] = []
    print("\n== 双档报告：同一期间 vs 信息可得 ==")
    for args in ALIGN_VARIANTS:
        r = run_variant(*args)
        results["align_variants"].append(r)
        c = r["C"]
        v = r["verdict"]
        print(f"-- {r['name']} align={r['align']} | OOS {r['n_oos']} | {r['note']}")
        print(f"   C R2={c['r2_vs_mean']:+.4f} hit={c['hit']:.4f} "
              f"Δ(best baseline)={v['C_minus_best_baseline']:+.4f} "
              f"Δ(0.20 threshold)={v['C_minus_020_threshold']:+.4f} "
              f"supported={v['supported']} barely={v['barely_above_threshold']}")
    path = L.save_json("h1_results.json", results)
    print(f"\n结果已写入 {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
