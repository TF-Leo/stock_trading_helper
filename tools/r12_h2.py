#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""R12 / #146 —— H2：A 股个股是否「Beta 主导、Alpha 很弱」。

预注册判据（原文）
------------------
* 成立：全 A 个股滚动 2 年日频市场模型 R² 中位数 ≥ 0.50，
        且个股 alpha 的 |t| > 2 比例 ≤ 10%；
* 同时给出美国样本的同方法对照，说明「Beta 主导」是否 A 股特有。

复验（本文件，2026-11-17 修订）
--------------------------------
1. **可比池**：全 A（含小盘/低流动性）与美股 50 只大市值不可比。新增
   沪深300 当前成分、成交额 top300/top50 三个可比池；全 A 保留为补充。
   流动性排名用 2022-12-31→2024-12-31 的中位日成交额（复验口径），另给
   逐窗排名（不依赖未来流动性）作稳健性。
2. **alpha 判据**：旧判据「|t|>2 比例 ≤10%」在零假设下的期望约为 5%，
   且打乱市场的安慰剂只有约 0.8%——它把「检验没功效」误当成「alpha 弱」。
   改为两个有零分布的统计量：
     * λ = SD(α̂) / RMS(SE(α̂))（横截面 alpha 离散 / 估计噪声）；
     * τ = sqrt(max(Var(α̂) − mean(SE²), 0))（扣噪声后的真实 alpha 离散，年化）；
     * 组合层 alpha（等权日度再平衡）的 Newey-West t，检验共同 alpha。
   零分布用残差块 bootstrap；另做安慰剂（打乱市场）与注入信号（5/10/20% 年化，
   共同 alpha 与横截面离散）功效测试。**只有新判据通过功效测试后，才用它改写结论。**

数据
----
* A 股：``~/data/stock-universe/csv/*.csv``（R9 无偏池，baostock 日线，含退市；
  2018-01 起，前复权）。**全 A 个股，不做幸存者筛选**（池内同时有在市与退市）。
* 沪深300 当前成分：``~/data/stock-universe/csi300-current.json``
  （baostock ``query_hs300_stocks``，快照 2026-11-09；不入库，脚本缺失时跳过该池）。
  **按当前成分回溯历史有幸存者偏差，方向抬高 R² 与 alpha；报告里注明。**
* 市场：沪深300（腾讯指数通道）；稳健性另用**中证全指 000985**；
  另构造**等权全 A 市场**（每日全池等权收益，仅用于判断组合 alpha 是否只是
  等权/规模效应）。
* 美国：50 只道指/大市值公开名单，Yahoo 后复权收盘；市场 = S&P 500。
  这是「同方法对照」，不是全美股样本。

窗口
----
2 年（约 500 交易日），窗口终点取 2021..2026 每年年末，互相重叠。
阈值：窗口内有效交易日 ≥ 400，否则跳过该股该窗口。

只用公开指数/公开大市值代码，不涉及任何真实持仓。
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
import r12_lib as L

WINDOW_ENDS = ["2021-12-31", "2022-12-31", "2023-12-31", "2024-12-31",
               "2025-12-31", "2026-11-14"]
MIN_OBS = 400
RET_CLIP = 0.5          # 单日 |收益| > 50% 视为脏数据剔除
POOL_RANK_START = "2022-12-31"
POOL_RANK_END = "2024-12-31"
POOL_TOP = (300, 50)
CAL_B = 100             # 每窗口 bootstrap 次数（可调；报告里写明）
CAL_BLOCK = 20          # 残差块长度（约一个月）
SIGNAL_ANNUAL = (0.05, 0.10, 0.20)


# ---------------------------------------------------------------------------
# 读数与回归
# ---------------------------------------------------------------------------
def _market_returns() -> pd.Series:
    csi = L.load_daily("csi300_daily")
    return csi.pct_change().dropna()


def _load_stock(path: Path, want_amount: bool = False):
    """返回 (日收益, 日成交额)；脏数据/停牌/长停复牌按既定口径处理。"""
    try:
        usecols = ["date", "close", "tradestatus"] + (["amount"] if want_amount else [])
        df = pd.read_csv(path, usecols=usecols)
    except (ValueError, pd.errors.ParserError):
        return None, None
    if df.empty:
        return None, None
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    df["close"] = pd.to_numeric(df["close"], errors="coerce")
    df["tradestatus"] = pd.to_numeric(df["tradestatus"], errors="coerce")
    if want_amount:
        df["amount"] = pd.to_numeric(df["amount"], errors="coerce")
    df = df.dropna(subset=["date", "close"])
    df = df[(df["tradestatus"] == 1) & (df["close"] > 0)]
    if len(df) < MIN_OBS:
        return None, None
    s = df.set_index("date")["close"].sort_index()
    r = s.pct_change()
    # 长期停牌：复牌首日的 pct_change 会横跨整段停牌，按「单日收益」计入会污染回归。
    # 经验规则：相邻有效交易日间隔 > 10 个自然日 → 该收益置 NaN。
    gap = s.index.to_series().diff().dt.days
    r[gap > 10] = np.nan
    amt = df.set_index("date")["amount"].sort_index() if want_amount else None
    return r, amt


def _market_model(r: pd.Series, rm: pd.Series, start, end) -> dict | None:
    d = pd.concat([r, rm], axis=1, keys=["ri", "rm"], sort=True).loc[start:end].dropna()
    d = d[(d["ri"].abs() <= RET_CLIP) & (d["rm"].abs() <= RET_CLIP)]
    if len(d) < MIN_OBS:
        return None
    y = d["ri"].to_numpy()
    x = d["rm"].to_numpy()
    A = np.column_stack([np.ones(len(x)), x])
    b, *_ = np.linalg.lstsq(A, y, rcond=None)
    resid = y - A @ b
    sse = float(resid @ resid)
    sst = float(((y - y.mean()) ** 2).sum())
    dof = max(len(y) - 2, 1)
    s2 = sse / dof
    cov = s2 * np.linalg.pinv(A.T @ A)
    se = np.sqrt(np.clip(np.diag(cov), 0, None))
    return {
        "n": int(len(y)), "r2": float(1 - sse / sst) if sst > 0 else float("nan"),
        "alpha_d": float(b[0]), "se_alpha": float(se[0]),
        "t_alpha": float(b[0] / se[0]) if se[0] > 0 else float("nan"),
        "beta": float(b[1]),
    }


def _fit_with_resid(y: np.ndarray, x: np.ndarray) -> tuple[float, float, float, np.ndarray]:
    """OLS，返回 (alpha, se_alpha, beta, 残差)。"""
    A = np.column_stack([np.ones(len(x)), x])
    b, *_ = np.linalg.lstsq(A, y, rcond=None)
    e = y - A @ b
    s2 = float(e @ e) / max(len(y) - 2, 1)
    cov = s2 * np.linalg.pinv(A.T @ A)
    se = np.sqrt(np.clip(np.diag(cov), 0, None))
    return float(b[0]), float(se[0]), float(b[1]), e


def _block_idx(n: int, block: int, rng: np.random.Generator) -> np.ndarray:
    block = min(block, n)
    nb = int(np.ceil(n / block))
    starts = rng.integers(0, n - block + 1, size=nb)
    return np.concatenate([np.arange(s, s + block) for s in starts])[:n]


# ---------------------------------------------------------------------------
# 汇总与 alpha 诊断
# ---------------------------------------------------------------------------
def _summarize(rows: list[dict], label: str) -> dict:
    if not rows:
        return {"label": label, "n_stock_windows": 0}
    df = pd.DataFrame(rows)
    r2 = df["r2"].dropna()
    ta = df["t_alpha"].dropna()
    out = {
        "label": label,
        "n_stock_windows": int(len(df)),
        "n_stocks": int(df["code"].nunique()),
        "r2_median": float(r2.median()),
        "r2_q25": float(r2.quantile(0.25)),
        "r2_q75": float(r2.quantile(0.75)),
        "r2_share_ge_050": float((r2 >= 0.50).mean()),
        "alpha_median_ann": float(df["alpha_d"].median() * 250),
        "beta_median": float(df["beta"].median()),
        "by_window": {
            str(w): {
                "n": int(len(g)),
                "r2_median": float(g["r2"].median()),
                "alpha_abs_t_gt2": float((g["t_alpha"].abs() > 2).mean()),
            }
            for w, g in df.groupby("window_end")
        },
    }
    diag = L.alpha_diagnostics(df["alpha_d"], df["se_alpha"], df["t_alpha"])
    out.update(diag)
    if "mean_amount" in df.columns:
        out["median_mean_amount"] = float(df["mean_amount"].median())
    # 旧判据原样保留，仅作对照；**不再用于分级**。
    out["legacy_alpha_share_abs_t_gt_2"] = diag["share_abs_t_gt_2"]
    out["legacy_criterion_supported"] = bool(out["r2_median"] >= 0.50
                                             and diag["share_abs_t_gt_2"] <= 0.10)
    return out


# ---------------------------------------------------------------------------
# A 股：全池 + 可比池
# ---------------------------------------------------------------------------
def _read_csi300_codes() -> set[str]:
    path = L.UNIVERSE / "csi300-current.json"
    if not path.exists():
        return set()
    try:
        return set(json.loads(path.read_text(encoding="utf-8")).get("codes", []))
    except (json.JSONDecodeError, OSError):
        return set()


def run_a_shares(markets: dict[str, pd.Series]):
    """一次扫描：全池六窗口 + 可比池定义所需的 2022–2024 中位成交额。

    返回 (按市场代理分组的汇总, 全部行, 池定义, 等权全 A 市场)。
    """
    rows = {name: [] for name in markets}
    pass1: dict[str, dict] = {}
    path_by_code: dict[str, Path] = {}
    ew_sum = pd.Series(dtype=float)
    ew_cnt = pd.Series(dtype=float)
    files = sorted((L.UNIVERSE / "csv").glob("*.csv"))
    csi_rm = markets["沪深300"]
    print(f"A股：扫描 {len(files)} 只（无偏池）；市场代理={list(markets)}")
    for i, path in enumerate(files, 1):
        r, amt = _load_stock(path, want_amount=True)
        if r is None:
            continue
        code = path.stem.replace("sh.", "").replace("sz.", "").replace("bj.", "")
        path_by_code[code] = path
        # 等权全 A 市场（用于判断组合 alpha 是否只是等权/规模效应）
        rr = r[r.abs() <= RET_CLIP]
        ew_sum = ew_sum.add(rr, fill_value=0.0)
        ew_cnt = ew_cnt.add(rr.notna().astype(float), fill_value=0.0)
        # 流动性排名的基准窗口
        p1 = _market_model(r, csi_rm, POOL_RANK_START, POOL_RANK_END)
        if p1:
            a = amt.loc[POOL_RANK_START:POOL_RANK_END] if amt is not None else pd.Series(dtype=float)
            pass1[code] = {
                "med_amt": float(a.median()) if len(a.dropna()) else float("nan"),
                **p1,
            }
        for end in WINDOW_ENDS:
            start = pd.Timestamp(end) - pd.DateOffset(years=2)
            for name, rm in markets.items():
                res = _market_model(r, rm, start, pd.Timestamp(end))
                if res:
                    a = amt.loc[start:pd.Timestamp(end)] if amt is not None else pd.Series(dtype=float)
                    res.update({
                        "code": code, "window_end": end,
                        "mean_amount": float(a.mean()) if len(a.dropna()) else float("nan"),
                    })
                    rows[name].append(res)
        if i % 1000 == 0:
            print(f"  {i}/{len(files)} ...", flush=True)

    ew = (ew_sum / ew_cnt).where(ew_cnt >= 100)
    ew.name = "ew_all_a"
    print(f"  等权全 A 市场：{int(ew.notna().sum())} 个交易日")

    csi_codes = _read_csi300_codes()
    rank = pd.Series({c: v["med_amt"] for c, v in pass1.items()}).dropna() \
        .sort_values(ascending=False)
    pool_def = {
        "rank_window": [POOL_RANK_START, POOL_RANK_END],
        "rank_metric": "median daily amount (CNY)",
        "n_pass1_stocks": int(len(pass1)),
        "csi300_snapshot_codes": len(csi_codes),
        "csi300_in_pass1": int(len(set(pass1) & csi_codes)),
        "pools": {},
    }
    pools: dict[str, set[str]] = {}
    if csi_codes:
        pools["沪深300成分"] = set(pass1) & csi_codes
    for n in POOL_TOP:
        pools[f"成交额top{n}(固定)"] = set(rank.head(n).index)
    # 逐窗排名（不依赖未来流动性）的稳健性池
    for n in POOL_TOP:
        pools[f"成交额top{n}(逐窗)"] = set()   # 占位，按窗口另行筛选

    summaries = {}
    for name in markets:
        df = pd.DataFrame(rows[name])
        summaries[f"全A vs {name}"] = _summarize(rows[name], f"A股全A（无偏池）vs {name}")
        for pool_name in list(pools) + ["全A"]:
            if pool_name == "全A":
                continue
            if pool_name.endswith("(逐窗)"):
                top_n = int(pool_name.split("top")[1].split("(")[0])
                sel = pd.concat([g.nlargest(top_n, "mean_amount")
                                 for _, g in df.groupby("window_end")])
            else:
                sel = df[df["code"].isin(pools[pool_name])]
            summaries[f"{pool_name} vs {name}"] = _summarize(
                sel.to_dict("records"), f"A股{pool_name} vs {name}")
        pool_def["pools"][name] = {
            k: len(v) for k, v in pools.items() if not k.endswith("(逐窗)")
        }

    # 安慰剂 / 校准需要的池（固定口径）
    cal_pools = {k: v for k, v in pools.items() if not k.endswith("(逐窗)")}
    return summaries, rows, pool_def, ew, cal_pools, path_by_code, pass1


# ---------------------------------------------------------------------------
# 美国对照
# ---------------------------------------------------------------------------
def run_us() -> tuple[list[dict], dict]:
    rm = L.load_daily("spx_daily").pct_change().dropna()
    rows = []
    files = sorted((L.CACHE / "us_stocks").glob("*.csv"))
    print(f"美股对照：{len(files)} 只")
    for path in files:
        s = L.load_daily(f"us_stocks/{path.stem}")
        r = s.pct_change()
        for end in WINDOW_ENDS:
            start = pd.Timestamp(end) - pd.DateOffset(years=2)
            res = _market_model(r, rm, start, pd.Timestamp(end))
            if res:
                res.update({"code": path.stem, "window_end": end})
                rows.append(res)
    return rows, _summarize(rows, "美股 50 只大市值（同方法对照）")


# ---------------------------------------------------------------------------
# 安慰剂：打乱市场（复验口径）+ 合成高斯噪声
# ---------------------------------------------------------------------------
def run_market_shuffle_placebo(codes: set[str], path_by_code: dict[str, Path],
                               rm: pd.Series, start=POOL_RANK_START,
                               end=POOL_RANK_END, seed: int = 7) -> dict:
    """同一套管道，把市场收益随机打乱（同一排列应用到所有股票）。"""
    rng = np.random.default_rng(seed)
    perm = pd.Series(rng.permutation(rm.to_numpy()), index=rm.index)
    real, fake = [], []
    for code in codes:
        path = path_by_code.get(code)
        if path is None:
            continue
        r, _ = _load_stock(path)
        if r is None:
            continue
        a = _market_model(r, rm, start, end)
        b = _market_model(r, perm, start, end)
        if a:
            real.append(a)
        if b:
            fake.append(b)
    def _pack(rows):
        if not rows:
            return {"n": 0}
        d = pd.DataFrame(rows)
        diag = L.alpha_diagnostics(d["alpha_d"], d["se_alpha"], d["t_alpha"])
        diag.update({"r2_median": float(d["r2"].median())})
        return diag
    return {"real": _pack(real), "shuffled_market": _pack(fake),
            "window": [start, end], "seed": seed}


def run_gaussian_calibration(n: int = 500, reps: int = 20000, seed: int = 11) -> dict:
    """纯高斯噪声过同一 OLS 管道：|t|>2 的期望（校准旧判据）。"""
    rng = np.random.default_rng(seed)
    cnt = 0
    ts = []
    for _ in range(reps):
        x = rng.normal(size=n)
        y = rng.normal(size=n)      # 与 x 独立，真实 alpha=0
        A = np.column_stack([np.ones(n), x])
        b, *_ = np.linalg.lstsq(A, y, rcond=None)
        e = y - A @ b
        s2 = float(e @ e) / (n - 2)
        se = np.sqrt(s2 * np.linalg.pinv(A.T @ A)[0, 0])
        t = b[0] / se
        ts.append(t)
        if abs(t) > 2:
            cnt += 1
    ts = np.asarray(ts)
    return {"n_obs": n, "reps": reps, "share_abs_t_gt_2": float(cnt / reps),
            "t_sd": float(ts.std(ddof=1)), "t_mean": float(ts.mean())}


# ---------------------------------------------------------------------------
# 新 alpha 判据：λ / τ / 组合层 alpha + bootstrap 零分布与功效
# ---------------------------------------------------------------------------
def _cal_window(Y: np.ndarray, X: np.ndarray, ew: np.ndarray,
                rng: np.random.Generator, B: int = CAL_B) -> dict:
    """一个窗口的观测值与 bootstrap 零分布/功效。

    Y: T×N 个股收益矩阵（NaN 允许）；X: T 市场收益；ew: 与 X 同网格的等权
    全 A 市场收益。
    """
    T, N = Y.shape
    fits = []
    for j in range(N):
        m = ~np.isnan(Y[:, j])
        y, x = Y[m, j], X[m]
        if len(y) < MIN_OBS:
            continue
        a, se, beta, e = _fit_with_resid(y, x)
        fits.append((y, x, a, se, beta, e))
    al = np.array([f[2] for f in fits])
    ses = np.array([f[3] for f in fits])
    if len(fits) < 3:
        return {"n_stocks": len(fits)}
    lam_obs = float(np.std(al, ddof=1) / np.sqrt(np.mean(ses ** 2)))
    # 组合层：等权日度再平衡
    cov = np.sum(~np.isnan(Y), axis=1)
    ok = cov >= max(30, N // 4)
    P = np.nanmean(Y[ok], axis=1)
    xp = X[ok]
    ew_p = ew[ok]
    csi_fit = L.ols_hac(P, xp, lags=5)
    ew_fit = L.ols_hac(P, ew_p, lags=5)
    t_csi = csi_fit["t_alpha"]
    t_ew = ew_fit["t_alpha"]

    # bootstrap：每只股票残差块 bootstrap；组合层用组合残差块 bootstrap
    lam_null = []
    lam_sig = {s: [] for s in SIGNAL_ANNUAL}
    t_null_csi, t_null_ew = [], []
    t_sig_csi = {s: [] for s in SIGNAL_ANNUAL}
    t_sig_ew = {s: [] for s in SIGNAL_ANNUAL}

    # 组合残差
    A = np.column_stack([np.ones(len(P)), xp])
    bp, *_ = np.linalg.lstsq(A, P, rcond=None)
    ep = P - A @ bp
    A2 = np.column_stack([np.ones(len(P)), ew_p])
    be, *_ = np.linalg.lstsq(A2, P, rcond=None)
    ep_ew = P - A2 @ be

    for _ in range(B):
        a_null, s_null = [], []
        a_disp = {s: [] for s in SIGNAL_ANNUAL}
        for (y, x, _a, _se, beta, e) in fits:
            idx = _block_idx(len(e), CAL_BLOCK, rng)
            ep_star = e[idx]
            y0 = beta * x + ep_star
            a0, se0, _, _ = _fit_with_resid(y0, x)
            a_null.append(a0)
            s_null.append(se0)
            for s in SIGNAL_ANNUAL:
                ystar = y0 + rng.normal(0, s / 250.0)
                a2, _, _, _ = _fit_with_resid(ystar, x)
                a_disp[s].append(a2)
        a_null = np.array(a_null)
        s_null = np.array(s_null)
        lam_null.append(np.std(a_null, ddof=1) / np.sqrt(np.mean(s_null ** 2)))
        for s in SIGNAL_ANNUAL:
            lam_sig[s].append(np.std(np.array(a_disp[s]), ddof=1)
                              / np.sqrt(np.mean(s_null ** 2)))
        # 组合层：共同 alpha 注入
        idxp = _block_idx(len(ep), CAL_BLOCK, rng)
        Pn = bp[1] * xp + ep[idxp]
        Pn_ew = be[1] * ew_p + ep_ew[idxp]
        for s in SIGNAL_ANNUAL:
            rc = L.ols_hac(Pn + s / 250.0, xp, lags=5)
            re = L.ols_hac(Pn_ew + s / 250.0, ew_p, lags=5)
            t_sig_csi[s].append(rc["t_alpha"])
            t_sig_ew[s].append(re["t_alpha"])
        t_null_csi.append(L.ols_hac(Pn, xp, lags=5)["t_alpha"])
        t_null_ew.append(L.ols_hac(Pn_ew, ew_p, lags=5)["t_alpha"])

    lam_null = np.asarray(lam_null)
    q_lam = float(np.quantile(lam_null, 0.95))
    t_null_csi = np.asarray(t_null_csi)
    t_null_ew = np.asarray(t_null_ew)
    q_t_csi = float(np.quantile(np.abs(t_null_csi), 0.95))
    q_t_ew = float(np.quantile(np.abs(t_null_ew), 0.95))

    def _power(sig_list, q):
        arr = np.asarray(sig_list)
        return float(np.mean(np.abs(arr) > q))

    out = {
        "n_stocks": len(fits),
        "lambda_obs": lam_obs,
        "lambda_null_median": float(np.median(lam_null)),
        "lambda_null_q95": q_lam,
        "p_lambda": float((np.sum(lam_null >= lam_obs) + 1) / (B + 1)),
        "power_lambda": {f"{int(s * 100)}pct": float(np.mean(np.asarray(lam_sig[s]) > q_lam))
                         for s in SIGNAL_ANNUAL},
        "portfolio_alpha_csi": {"t_obs": float(t_csi), "null_q95": q_t_csi,
                                "p": float((np.sum(np.abs(t_null_csi) >= abs(t_csi)) + 1) / (B + 1)),
                                "power": {f"{int(s * 100)}pct": _power(t_sig_csi[s], q_t_csi)
                                          for s in SIGNAL_ANNUAL}},
        "portfolio_alpha_ew": {"t_obs": float(t_ew), "null_q95": q_t_ew,
                               "p": float((np.sum(np.abs(t_null_ew) >= abs(t_ew)) + 1) / (B + 1)),
                               "power": {f"{int(s * 100)}pct": _power(t_sig_ew[s], q_t_ew)
                                         for s in SIGNAL_ANNUAL}},
        "B": B,
        "block": CAL_BLOCK,
    }
    return out


def run_calibration(pools: dict[str, set[str]], path_by_code: dict[str, Path],
                    rm: pd.Series, ew: pd.Series) -> dict:
    """对每个固定可比池，逐窗口跑 λ / 组合 alpha 的零分布与功效。"""
    # 只保留有收益数据的池成员
    rets: dict[str, pd.Series] = {}
    for code, path in path_by_code.items():
        if not any(code in codes for codes in pools.values()):
            continue
        r, _ = _load_stock(path)
        if r is not None:
            rets[code] = r
    out = {}
    for pool_name, codes in pools.items():
        codes_ok = sorted(c for c in codes if c in rets)
        if len(codes_ok) < 20:
            continue
        print(f"\n校准：{pool_name}（{len(codes_ok)} 只）")
        out[pool_name] = {"n_stocks_loaded": len(codes_ok), "windows": {}}
        for end in WINDOW_ENDS:
            start = pd.Timestamp(end) - pd.DateOffset(years=2)
            idx = rm.loc[start:pd.Timestamp(end)].dropna().index
            Y = pd.DataFrame({c: rets[c].loc[start:pd.Timestamp(end)] for c in codes_ok}).reindex(idx)
            Y = Y.mask(Y.abs() > RET_CLIP)
            Xall = rm.reindex(idx).to_numpy()
            eww = ew.reindex(idx).to_numpy()
            rng = np.random.default_rng(7)
            res = _cal_window(Y.to_numpy(), Xall, eww, rng)
            out[pool_name]["windows"][end] = res
            print(f"  {end} n={res['n_stocks']:3d} λ={res['lambda_obs']:.3f} "
                  f"(null q95={res['lambda_null_q95']:.3f}, p={res['p_lambda']:.3f}) | "
                  f"t_csi={res['portfolio_alpha_csi']['t_obs']:+.2f} "
                  f"(p={res['portfolio_alpha_csi']['p']:.3f}) | "
                  f"t_ew={res['portfolio_alpha_ew']['t_obs']:+.2f} "
                  f"(p={res['portfolio_alpha_ew']['p']:.3f})", flush=True)
        wins = out[pool_name]["windows"]
        out[pool_name]["summary"] = {
            "windows": len(wins),
            "lambda_p_lt_05": sum(1 for v in wins.values() if v["p_lambda"] < 0.05),
            "portfolio_csi_p_lt_05": sum(1 for v in wins.values()
                                         if v["portfolio_alpha_csi"]["p"] < 0.05),
            "portfolio_ew_p_lt_05": sum(1 for v in wins.values()
                                        if v["portfolio_alpha_ew"]["p"] < 0.05),
            "median_power_lambda": {
                k: float(np.median([v["power_lambda"][k] for v in wins.values()]))
                for k in wins[next(iter(wins))]["power_lambda"]
            },
            "median_power_common_csi": {
                k: float(np.median([v["portfolio_alpha_csi"]["power"][k] for v in wins.values()]))
                for k in wins[next(iter(wins))]["portfolio_alpha_csi"]["power"]
            },
            "median_power_common_ew": {
                k: float(np.median([v["portfolio_alpha_ew"]["power"][k] for v in wins.values()]))
                for k in wins[next(iter(wins))]["portfolio_alpha_ew"]["power"]
            },
        }
    return out


# ---------------------------------------------------------------------------
# 主程序
# ---------------------------------------------------------------------------
def main() -> int:
    t0 = time.time()
    csi = L.load_daily("csi300_daily")
    rm = csi.pct_change().dropna()
    markets = {"沪深300": _market_returns()}
    allshare = L.load_optional("allshare_daily")
    if allshare is not None:
        markets["中证全指"] = allshare.pct_change().dropna()

    summaries, rows, pool_def, ew, pools, path_by_code, pass1 = run_a_shares(markets)
    us_rows, us_sum = run_us()

    print("\n== A 股 ==")
    for name, s in summaries.items():
        if s.get("n_stock_windows", 0) == 0:
            print(f"  {name}: 无有效样本")
            continue
        print(f"  {s['label']}: n={s['n_stock_windows']} 股票={s['n_stocks']} "
              f"R2中位={s['r2_median']:.3f} R2≥0.5={s['r2_share_ge_050']:.3f} "
              f"|t|>2={s['share_abs_t_gt_2']:.3f} λ={s['alpha_lambda']:.3f} "
              f"τ={s['alpha_tau_ann']:.3f} meanα={s['alpha_mean_ann']:.3f}")
    print(f"\n== 美股对照 ==\n  {us_sum['label']}: n={us_sum['n_stock_windows']} "
          f"股票={us_sum['n_stocks']} R2中位={us_sum['r2_median']:.3f} "
          f"|t|>2={us_sum['share_abs_t_gt_2']:.3f} "
          f"λ={us_sum['alpha_lambda']:.3f} τ={us_sum['alpha_tau_ann']:.3f}")

    print("\n== 安慰剂 / 高斯校准 ==")
    # 复验口径：安慰剂用**全 A** 池（2022-12→2024-12 单窗），与复核报告的 0.8% 对齐。
    placebo = run_market_shuffle_placebo(set(pass1), path_by_code, rm)
    gauss = run_gaussian_calibration()
    print(f"  打乱市场（同一管道，2022-12→2024-12）："
          f"|t|>2={placebo['shuffled_market'].get('share_abs_t_gt_2', float('nan')):.3f} "
          f"λ={placebo['shuffled_market'].get('alpha_lambda', float('nan')):.3f}")
    print(f"  真实（同窗口）：|t|>2={placebo['real'].get('share_abs_t_gt_2', float('nan')):.3f} "
          f"λ={placebo['real'].get('alpha_lambda', float('nan')):.3f}")
    print(f"  纯高斯噪声（n=500，reps={gauss['reps']}）："
          f"|t|>2={gauss['share_abs_t_gt_2']:.3f} sd(t)={gauss['t_sd']:.3f}")

    print("\n== 新 alpha 判据的零分布与功效 ==")
    calibration = run_calibration(pools, path_by_code, rm, ew)

    payload = {
        "windows": WINDOW_ENDS, "min_obs": MIN_OBS, "ret_clip": RET_CLIP,
        "pool_definition": pool_def,
        "a_shares": summaries,
        "us_control": us_sum, "us_rows_sample": us_rows[:5],
        "placebo": placebo,
        "gaussian_calibration": gauss,
        "alpha_criterion": {
            "old": "share |t_alpha|>2 <= 10% (legacy; retained for comparison only)",
            "new": "lambda = SD(alpha)/RMS(SE); tau = sqrt(max(Var(alpha)-mean(SE^2),0)); "
                   "portfolio alpha HAC(5) t; null = residual block bootstrap",
            "calibration_B": CAL_B, "block": CAL_BLOCK,
            "signal_annual": SIGNAL_ANNUAL,
        },
        "calibration": calibration,
        "ew_market_days": int(ew.notna().sum()),
    }
    path = L.save_json("h2_results.json", payload)
    print(f"\n结果已写入 {path}  （用时 {time.time() - t0:.0f}s）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
