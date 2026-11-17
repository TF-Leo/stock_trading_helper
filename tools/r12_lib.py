# -*- coding: utf-8 -*-
"""R12 公共工具：面板构造、扩展窗口 OOS、预测评价、前视风险度量。

纯函数优先，便于离线单测；本模块不联网、不读仓库产品代码。
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import numpy as np
import pandas as pd

CACHE = Path(os.environ.get("R12_CACHE", Path.home() / "data" / "r12"))
UNIVERSE = Path(os.environ.get("R12_UNIVERSE", Path.home() / "data" / "stock-universe"))


# ---------------------------------------------------------------------------
# 读数
# ---------------------------------------------------------------------------
def load_daily(name: str, value_col: str = "close") -> pd.Series:
    path = CACHE / f"{name}.csv"
    df = pd.read_csv(path)
    df["date"] = pd.to_datetime(df["date"])
    s = df.set_index("date")[value_col].astype(float).sort_index()
    return s[~s.index.duplicated(keep="last")]


def load_optional(name: str, value_col: str = "close") -> pd.Series | None:
    return load_daily(name, value_col) if (CACHE / f"{name}.csv").exists() else None


def _period_key(index: pd.DatetimeIndex, freq: str) -> pd.PeriodIndex:
    return index.to_period("W-FRI") if freq == "W" else index.to_period("M")


def _period_series(daily: pd.Series, freq: str) -> pd.Series:
    """日频 → 期间最后一根，索引保留 Period（便于跨市场按周/月对齐）。"""
    key = _period_key(daily.index, freq)
    return daily.groupby(key).last().sort_index()


def period_end(daily: pd.Series, freq: str) -> pd.Series:
    """日频 → 周/月频（期间最后一根），索引为该期最后一个交易日。"""
    s = _period_series(daily, freq)
    last_date = daily.index.to_series().groupby(_period_key(daily.index, freq)).max()
    return pd.Series(s.to_numpy(), index=pd.DatetimeIndex(last_date.to_numpy()),
                     name=daily.name).sort_index()


def last_close_before(series: pd.Series, end_dates: pd.Series,
                      strict: bool = True) -> pd.Series:
    """把外盘序列映射到 A 股期间末：取 period-end 前最后一个外盘收盘。

    ``end_dates`` 的索引是 A 股期间（Period），值是该期 A 股最后交易日
    （DatetimeIndex）。``strict=True`` 表示 ``date < D``（信息可得口径：
    A 股 15:00 收盘时，当日恒生 16:00 / 美股夜盘 / 美债美东收盘都还没发生）；
    ``strict=False`` 表示 ``date <= D``（同一期间口径，含收盘后重叠）。
    """
    vals = series.to_numpy(dtype=float)
    dates = series.index.values
    side = "left" if strict else "right"
    pos = np.searchsorted(dates, end_dates.to_numpy(), side=side) - 1
    out = np.full(len(end_dates), np.nan)
    ok = pos >= 0
    out[ok] = vals[pos[ok]]
    return pd.Series(out, index=end_dates.index)


def log_ret(s: pd.Series) -> pd.Series:
    return np.log(s).diff()


# ---------------------------------------------------------------------------
# 面板
# ---------------------------------------------------------------------------
def build_panel(freq: str, align: str = "period") -> pd.DataFrame:
    """构造 H1 面板：目标为沪深300 期间对数收益，预测变量见 README/报告。

    对齐规则（防前视）：
    * ``align="period"``（默认，PR 原口径）：各市场按自己的周/月 Period
      取期间最后一根，再按 Period 对齐。**注意**：该口径下第 t 周的 S&P500
      收益包含周五美股夜盘（周六 04:00 CST 才收盘），恒生包含周五
      15:00–16:00（A 股 15:00 已收盘），属于同一期间内的收盘后重叠。
    * ``align="same"``：把外盘映射到 A 股期间末 D，取 ``date <= D`` 的最后
      一根（比 ``period`` 更严格地去掉「外盘在本周最后交易日晚于 A 股收盘」
      的部分）。
    * ``align="samegrid"``：各外盘先按自己的 Period 取最后一根，再对齐到
      A 股 Period 网格后取差分。这是复验脚本的「同一期间」重算口径
      （与原实现的区别只在 A 股节假日截断周：原实现先差分后对齐）。
    * ``align="avail"``：取 ``date < D`` 的最后一根（**信息可得**口径）：
      A 股期间末 15:00 收盘时，恒生当日 16:00、美股夜盘、美债美东收盘
      均尚未发生，故只能用前一个外盘收盘。
    * 两融、北向为收盘后披露 → 同期值 shift(1)。
    * M2 月度同比按「次月可得」处理 → shift(1) 后再前向填充。
    * SHIBOR 为日内可得 → 同期。
    * 面板最终索引为该期沪深300 最后一个交易日。
    """
    if align not in ("period", "samegrid", "same", "avail"):
        raise ValueError(f"align 必须是 period/samegrid/same/avail，收到 {align!r}")
    csi_daily = load_daily("csi300_daily")
    csi = _period_series(csi_daily, freq)
    idx = csi.index
    csi_dates = pd.DatetimeIndex(
        csi_daily.index.to_series()
        .groupby(_period_key(csi_daily.index, freq)).max()
        .reindex(idx).to_numpy())
    end_dates = pd.Series(csi_dates.values, index=idx)

    delta = lambda s: s.diff()                                    # noqa: E731
    lr = lambda s: np.log(s).diff()                               # noqa: E731

    def _ext(name: str, col: str = "close") -> pd.Series:
        s = load_daily(name, col)
        if align == "period":
            return _period_series(s, freq)
        if align == "samegrid":
            return _period_series(s, freq).reindex(idx)
        return last_close_before(s, end_dates, strict=(align == "avail"))

    out = pd.DataFrame(index=idx)
    out["ret"] = lr(csi)
    out["spx"] = lr(_ext("spx_daily"))
    out["hsi"] = lr(_ext("hsi_daily"))
    out["fx_off"] = lr(_ext("usdcnh_daily"))
    out["fx_on"] = lr(_ext("usdcny_onshore_daily", "value"))
    out["d_us10y"] = delta(_ext("us10y_daily", "value"))
    # 国内
    m2 = load_daily("m2_monthly", "m2_yoy").shift(1)
    if freq == "M":
        m2b = m2.groupby(m2.index.to_period("M")).last().reindex(idx, method="ffill")
    else:
        m2b = pd.Series(m2.reindex(csi_dates, method="ffill").to_numpy(), index=idx)
    out["d_m2_yoy"] = m2b.diff()
    out["d_shibor"] = delta(_period_series(load_daily("shibor_daily", "m3"), freq))
    out["margin_g"] = lr(_period_series(load_daily("margin_daily", "total"), freq)).shift(1)
    # 北向是**流量**：周/月内求和；停止披露后保持 NaN，绝不用 ffill 把旧值延伸到未来。
    # 单位说明：东财 NET_DEAL_AMT 与两融余额单位不同（前者百万元、后者元），
    # 统一折算为「占两融余额的万分之（bp）」，避免系数量级失真。
    north = load_daily("northbound_daily", "net_flow")
    nkey = _period_key(north.index, freq)
    north_p = north.groupby(nkey).sum()
    margin_p = _period_series(load_daily("margin_daily", "total"), freq)
    out["north_norm"] = ((north_p / (margin_p / 1e6)) * 1e4).reindex(idx).shift(1)
    # 严格滞后版（全部外部因子再落后一期）
    for col in ("spx", "hsi", "fx_off", "fx_on", "d_us10y"):
        out[f"{col}_lag"] = out[col].shift(1)
    out.index = csi_dates
    out = out[~out.index.duplicated(keep="last")]
    return out


EXTERNAL = ["fx_on", "spx", "hsi", "d_us10y"]
DOMESTIC = ["d_m2_yoy", "d_shibor", "margin_g", "north_norm"]


# ---------------------------------------------------------------------------
# 估计与评价
# ---------------------------------------------------------------------------
def expanding_forecast(y: pd.Series, X: pd.DataFrame, first_oos: str) -> pd.Series:
    """扩展窗口 OLS：训练集严格早于预测期，逐期重估。"""
    first = pd.Timestamp(first_oos)
    idx = y.index
    preds = pd.Series(np.nan, index=idx, dtype=float)
    start = int(np.searchsorted(idx.values, np.datetime64(first)))
    for i in range(start, len(idx)):
        ytr, Xtr = y.iloc[:i], X.iloc[:i]
        mask = ytr.notna() & Xtr.notna().all(axis=1)
        if mask.sum() < X.shape[1] + 5:
            continue
        A = np.column_stack([np.ones(mask.sum()), Xtr[mask].to_numpy()])
        b = np.linalg.lstsq(A, ytr[mask].to_numpy(), rcond=None)[0]
        x = np.r_[1.0, X.iloc[i].to_numpy(dtype=float)]
        if np.isfinite(x).all():
            preds.iloc[i] = float(x @ b)
    return preds


def oos_r2(y: pd.Series, pred: pd.Series, bench: pd.Series) -> float:
    """1 - SSE(model)/SSE(bench)，仅用三者共同非空样本。"""
    m = y.notna() & pred.notna() & bench.notna()
    if m.sum() < 3:
        return float("nan")
    sse = float(((y[m] - pred[m]) ** 2).sum())
    sse0 = float(((y[m] - bench[m]) ** 2).sum())
    if sse0 <= 0:
        return float("nan")
    return 1.0 - sse / sse0


def direction_hit(y: pd.Series, pred: pd.Series) -> float:
    m = y.notna() & pred.notna() & (y != 0) & (pred != 0)
    if m.sum() == 0:
        return float("nan")
    return float((np.sign(y[m]) == np.sign(pred[m])).mean())


def dm_test(e1: pd.Series, e2: pd.Series, h: int = 1) -> tuple[float, float]:
    """Diebold-Mariano（平方损失，Newey-West 修正）。正统计量 = 模型1更差。"""
    m = e1.notna() & e2.notna()
    d = (e1[m] ** 2 - e2[m] ** 2).to_numpy()
    n = len(d)
    if n < 10:
        return float("nan"), float("nan")
    mean = d.mean()
    var = d.var(ddof=0)
    for lag in range(1, max(h, 1)):
        cov = np.cov(d[lag:], d[:-lag], ddof=0)[0, 1]
        var += 2 * (1 - lag / max(h, 1)) * cov
    if var <= 0:
        return float("nan"), float("nan")
    stat = mean / np.sqrt(var / n)
    from math import erf, sqrt
    p = 2 * (1 - 0.5 * (1 + erf(abs(stat) / sqrt(2))))
    return float(stat), float(p)


def ols_hac(y, x, lags: int = 0) -> dict:
    """一元回归 y = α + βx + ε，返回 alpha/se/t 与 beta。

    ``lags > 0`` 时用 Newey-West (Bartlett) HAC 标准误；组合层 alpha 的
    时间序列自相关/异方差由此修正。纯 numpy，不依赖 statsmodels。
    """
    y = np.asarray(y, dtype=float)
    x = np.asarray(x, dtype=float)
    m = np.isfinite(y) & np.isfinite(x)
    y, x = y[m], x[m]
    n = len(y)
    if n < 3:
        return {"n": n, "alpha": float("nan"), "se_alpha": float("nan"),
                "t_alpha": float("nan"), "beta": float("nan")}
    A = np.column_stack([np.ones(n), x])
    b, *_ = np.linalg.lstsq(A, y, rcond=None)
    e = y - A @ b
    XtX = np.linalg.pinv(A.T @ A)
    if lags > 0:
        S = (A * e[:, None]).T @ (A * e[:, None])
        for lag in range(1, lags + 1):
            w = 1.0 - lag / (lags + 1.0)
            G = (A[lag:] * e[lag:, None]).T @ (A[:-lag] * e[:-lag, None])
            S += w * (G + G.T)
        cov = XtX @ S @ XtX
    else:
        s2 = float(e @ e) / max(n - 2, 1)
        cov = s2 * XtX
    se = np.sqrt(np.clip(np.diag(cov), 0, None))
    return {
        "n": n,
        "alpha": float(b[0]),
        "se_alpha": float(se[0]),
        "t_alpha": float(b[0] / se[0]) if se[0] > 0 else float("nan"),
        "beta": float(b[1]),
    }


def alpha_diagnostics(alphas, ses, tstats) -> dict:
    """个股 alpha 的横截面诊断（H2 的 alpha 判据）。

    * ``alpha_lambda``：α̂ 的横截面标准差 / 估计噪声 RMS(SE)。
      噪声主导时 λ<1（估计量分不清 alpha 与噪声）；λ>1 表示存在超出
      抽样噪声的横截面离散。
    * ``alpha_tau_ann``：随机效应方差分解 τ = sqrt(max(Var(α̂)−mean(SE²),0))
      的年化值，即扣掉估计噪声后的真实 alpha 离散度。
    * ``alpha_mean_ann``：等权平均 alpha 年化。
    * ``t_mean`` / ``t_sd``：个股 t 统计量的横截面均值/标准差。
    * ``share_abs_t_gt_2``：旧判据（原样保留，仅作对照，不再用于分级）。
    """
    a = np.asarray(alphas, dtype=float)
    s = np.asarray(ses, dtype=float)
    t = np.asarray(tstats, dtype=float)
    m = np.isfinite(a) & np.isfinite(s)
    n = int(m.sum())
    out = {"n": n, "alpha_mean_ann": float("nan"), "alpha_sd_ann": float("nan"),
           "alpha_noise_ann": float("nan"), "alpha_tau_ann": float("nan"),
           "alpha_lambda": float("nan"), "t_mean": float("nan"),
           "t_sd": float("nan"), "share_abs_t_gt_2": float("nan")}
    if n < 3:
        return out
    var = float(np.var(a[m], ddof=1))
    noise = float(np.mean(s[m] ** 2))
    tau2 = max(var - noise, 0.0)
    out.update({
        "alpha_mean_ann": float(np.mean(a[m]) * 250.0),
        "alpha_sd_ann": float(np.sqrt(var) * 250.0),
        "alpha_noise_ann": float(np.sqrt(noise) * 250.0),
        "alpha_tau_ann": float(np.sqrt(tau2) * 250.0),
        "alpha_lambda": float(np.sqrt(var) / np.sqrt(noise)) if noise > 0 else float("nan"),
    })
    tm = np.isfinite(t)
    if tm.sum() >= 2:
        out["t_mean"] = float(np.mean(t[tm]))
        out["t_sd"] = float(np.std(t[tm], ddof=1))
        out["share_abs_t_gt_2"] = float(np.mean(np.abs(t[tm]) > 2))
    return out


def lead_lag_daily(lookback: str = "2017-03-01") -> dict:
    """日频 lead-lag 佐证：A 股对数收益与同日/前一日/后一日外盘收益的相关。

    ``SPX_{t-1}`` / ``HSI_{t-1}`` 用外盘自己的上一交易日收益（而不是
    A 股日历），因为它才是 A 股开盘前已知的信息。默认样本自 2017-03 起，
    与 H1 周频主设定的起点一致；另给全样本相关系数说明区间敏感性。
    """
    csi = np.log(load_daily("csi300_daily")).diff()
    spx = np.log(load_daily("spx_daily")).diff()
    hsi = np.log(load_daily("hsi_daily")).diff()

    def _corr(a: pd.Series, b: pd.Series, start: str | None = None) -> float:
        d = pd.concat([a.rename("a"), b.rename("b")], axis=1).dropna()
        if start:
            d = d.loc[start:]
        if len(d) < 30:
            return float("nan")
        return float(d["a"].corr(d["b"]))

    return {
        "CSI_t~SPX_t": _corr(csi, spx, lookback),
        "CSI_t~SPX_tm1": _corr(csi, spx.shift(1), lookback),
        "CSI_t~HSI_t": _corr(csi, hsi, lookback),
        "CSI_t~HSI_tm1": _corr(csi, hsi.shift(1), lookback),
        "CSI_t~HSI_tp1": _corr(csi, hsi.shift(-1), lookback),
        "SPX_tp1~CSI_t": _corr(spx.shift(-1), csi, lookback),
        "HSI_tp1~CSI_t": _corr(hsi.shift(-1), csi, lookback),
        "CSI_t~HSI_t_full": _corr(csi, hsi),
        "CSI_t~SPX_t_full": _corr(csi, spx),
    }


# ---------------------------------------------------------------------------
# H3 前视度量
# ---------------------------------------------------------------------------
def forward_drawdown(close: pd.Series, horizon: int = 20) -> pd.Series:
    """每个时点之后 horizon 个交易日内，相对当前收盘的最大回撤（负数）。"""
    vals = close.to_numpy(dtype=float)
    out = np.full(len(vals), np.nan)
    for i in range(len(vals) - 1):
        end = min(i + horizon + 1, len(vals))
        window = vals[i:end]
        out[i] = float(window.min() / vals[i] - 1.0)
    return pd.Series(out, index=close.index)


def forward_vol(rets: pd.Series, horizon: int = 20) -> pd.Series:
    """未来 horizon 期实现波动（年化前，按日/周频原单位）。"""
    vals = rets.to_numpy(dtype=float)
    out = np.full(len(vals), np.nan)
    for i in range(len(vals) - horizon):
        out[i] = float(np.nanstd(vals[i + 1:i + 1 + horizon], ddof=0))
    return pd.Series(out, index=rets.index)


def spearman_rho(x, y) -> float:
    """Spearman ρ = 秩的 Pearson 相关；不依赖 scipy（pandas 的 method='spearman' 会拉 scipy）。"""
    xs, ys = pd.Series(np.asarray(x, dtype=float)), pd.Series(np.asarray(y, dtype=float))
    m = xs.notna() & ys.notna()
    if m.sum() < 3:
        return float("nan")
    return float(xs[m].rank().corr(ys[m].rank(), method="pearson"))


def spearman(x: pd.Series, y: pd.Series) -> tuple[float, int]:
    m = x.notna() & y.notna()
    if m.sum() < 5:
        return float("nan"), int(m.sum())
    return spearman_rho(x[m], y[m]), int(m.sum())


# ---------------------------------------------------------------------------
# 单位根 / 自相关（不引入 statsmodels 的等价实现）
# ---------------------------------------------------------------------------
ADF_CRIT = {"1%": -3.43, "5%": -2.86, "10%": -2.57}   # 含常数、大样本 MacKinnon


def adf_tstat(series: pd.Series, lags: int | None = None) -> dict:
    """增广 Dickey-Fuller：返回 β 的 t 值与近似 5% 结论。

    ``lags=None`` 时按 BIC 在 0..4 中选滞后阶数。
    """
    y = series.dropna().to_numpy(dtype=float)
    if len(y) < 30:
        return {"t": float("nan"), "lags": None, "stationary_5pct": None}
    dy = np.diff(y)

    def _fit(p: int):
        n = len(dy) - p
        Y = dy[p:]
        cols = [np.ones(n), y[p:-1]]
        for i in range(1, p + 1):
            cols.append(dy[p - i:-i])
        A = np.column_stack(cols)
        b, *_ = np.linalg.lstsq(A, Y, rcond=None)
        resid = Y - A @ b
        dof = max(n - A.shape[1], 1)
        s2 = float(resid @ resid) / dof
        cov = s2 * np.linalg.pinv(A.T @ A)
        se = float(np.sqrt(max(cov[1, 1], 0.0)))
        t = float(b[1] / se) if se > 0 else float("nan")
        bic = n * np.log(float(resid @ resid) / n) + A.shape[1] * np.log(n)
        return t, bic, p

    candidates = [_fit(p) for p in (range(5) if lags is None else [lags])]
    t, _bic, p = min(candidates, key=lambda r: r[1]) if lags is None else candidates[0]
    return {"t": t, "lags": p, "stationary_5pct": bool(t < ADF_CRIT["5%"])}


def acf1(series: pd.Series) -> float:
    s = series.dropna()
    if len(s) < 10:
        return float("nan")
    return float(s.autocorr(lag=1))



def save_json(name: str, payload: dict) -> Path:
    path = CACHE / name
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str),
                    encoding="utf-8")
    return path
