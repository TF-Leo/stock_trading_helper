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


def log_ret(s: pd.Series) -> pd.Series:
    return np.log(s).diff()


# ---------------------------------------------------------------------------
# 面板
# ---------------------------------------------------------------------------
def build_panel(freq: str) -> pd.DataFrame:
    """构造 H1 面板：目标为沪深300 期间对数收益，预测变量见 README/报告。

    对齐规则（防前视）：
    * 外部收益类（S&P/HSI、USDCNY 变动、美债变动）为**同期**，这是 #146
      预注册的「拟合」口径；另提供 ``_lag`` 严格可交易版本。
    * 两融、北向为收盘后披露 → 同期值 shift(1)。
    * M2 月度同比按「次月可得」处理 → shift(1) 后再前向填充。
    * SHIBOR 为日内可得 → 同期。
    * 不同市场节假日不同：先各自按周/月聚合（Period 索引），再按 Period
      对齐；面板最终索引为该期沪深300 最后一个交易日。
    """
    csi = _period_series(load_daily("csi300_daily"), freq)
    idx = csi.index
    csi_dates = pd.DatetimeIndex(
        load_daily("csi300_daily").index.to_series()
        .groupby(_period_key(load_daily("csi300_daily").index, freq)).max()
        .reindex(idx).to_numpy())

    delta = lambda s: s.diff()                                    # noqa: E731
    lr = lambda s: np.log(s).diff()                               # noqa: E731
    out = pd.DataFrame(index=idx)
    out["ret"] = lr(csi)
    out["spx"] = lr(_period_series(load_daily("spx_daily"), freq))
    out["hsi"] = lr(_period_series(load_daily("hsi_daily"), freq))
    out["fx_off"] = lr(_period_series(load_daily("usdcnh_daily"), freq))
    out["fx_on"] = lr(_period_series(load_daily("usdcny_onshore_daily", "value"), freq))
    out["d_us10y"] = delta(_period_series(load_daily("us10y_daily", "value"), freq))
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
