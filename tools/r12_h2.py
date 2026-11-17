#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""R12 / #146 —— H2：A 股个股是否「Beta 主导、Alpha 很弱」。

预注册判据
----------
* 成立：全 A 个股滚动 2 年日频市场模型 R² 中位数 ≥ 0.50，
        且个股 alpha 的 |t| > 2 比例 ≤ 10%；
* 同时给出美国样本的同方法对照，说明「Beta 主导」是否 A 股特有。

数据
----
* A 股：``~/data/stock-universe/csv/*.csv``（R9 无偏池，baostock 日线，含退市；
  2018-01 起，前复权）。**全 A 个股，不做幸存者筛选**（池内同时有在市与退市）。
* 市场：沪深300（腾讯指数通道）；稳健性另用**中证全指 000985**
  （更接近「全 A」的市场代理）。
* 美国：50 只道指/大市值公开名单，Yahoo 后复权收盘；市场 = S&P 500。
  这是「同方法对照」，不是全美股样本——只在报告里这样写。

窗口
----
2 年（约 500 交易日），窗口终点取 2021..2026 每年年末，互相重叠。
阈值：窗口内有效交易日 ≥ 400，否则跳过该股该窗口。

只用公开指数/公开大市值代码，不涉及任何真实持仓。
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
import r12_lib as L

WINDOW_ENDS = ["2021-12-31", "2022-12-31", "2023-12-31", "2024-12-31",
               "2025-12-31", "2026-11-14"]
MIN_OBS = 400
RET_CLIP = 0.5          # 单日 |收益| > 50% 视为脏数据剔除


def _market_returns() -> pd.Series:
    csi = L.load_daily("csi300_daily")
    return csi.pct_change().dropna()


def _load_stock(path: Path) -> pd.Series | None:
    try:
        df = pd.read_csv(path, usecols=["date", "close", "tradestatus"])
    except (ValueError, pd.errors.ParserError):
        return None
    if df.empty:
        return None
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    df["close"] = pd.to_numeric(df["close"], errors="coerce")
    df["tradestatus"] = pd.to_numeric(df["tradestatus"], errors="coerce")
    df = df.dropna(subset=["date", "close"])
    df = df[(df["tradestatus"] == 1) & (df["close"] > 0)]
    if len(df) < MIN_OBS:
        return None
    s = df.set_index("date")["close"].sort_index()
    r = s.pct_change()
    # 长期停牌：复牌首日的 pct_change 会横跨整段停牌，按「单日收益」计入会污染回归。
    # 经验规则：相邻有效交易日间隔 > 10 个自然日 → 该收益置 NaN。
    gap = s.index.to_series().diff().dt.days
    r[gap > 10] = np.nan
    return r


def _market_model(r: pd.Series, rm: pd.Series, start, end) -> dict | None:
    d = pd.concat([r, rm], axis=1, keys=["ri", "rm"]).loc[start:end].dropna()
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
        "alpha_d": float(b[0]), "t_alpha": float(b[0] / se[0]) if se[0] > 0 else float("nan"),
        "beta": float(b[1]),
    }


def _summarize(rows: list[dict], label: str) -> dict:
    if not rows:
        return {"label": label, "n_windows": 0}
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
        "alpha_share_abs_t_gt_2": float((ta.abs() > 2).mean()),
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
    out["verdict_supported"] = bool(out["r2_median"] >= 0.50
                                    and out["alpha_share_abs_t_gt_2"] <= 0.10)
    return out


def run_a_shares(markets: dict[str, pd.Series]) -> dict:
    rows = {name: [] for name in markets}
    files = sorted((L.UNIVERSE / "csv").glob("*.csv"))
    print(f"A股：扫描 {len(files)} 只（无偏池）；市场代理={list(markets)}")
    for i, path in enumerate(files, 1):
        r = _load_stock(path)
        if r is None:
            continue
        code = path.stem.replace("sh.", "").replace("sz.", "").replace("bj.", "")
        for end in WINDOW_ENDS:
            start = pd.Timestamp(end) - pd.DateOffset(years=2)
            for name, rm in markets.items():
                res = _market_model(r, rm, start, pd.Timestamp(end))
                if res:
                    res.update({"code": code, "window_end": end})
                    rows[name].append(res)
        if i % 1000 == 0:
            print(f"  {i}/{len(files)} ...", flush=True)
    return {name: _summarize(rs, f"A股全A（无偏池）vs {name}")
            for name, rs in rows.items()}


def run_us() -> tuple[list[dict], dict]:
    # 与 A 股口径一致：简单收益
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


def main() -> int:
    markets = {"沪深300": _market_returns()}
    allshare = L.load_optional("allshare_daily")
    if allshare is not None:
        markets["中证全指"] = allshare.pct_change().dropna()
    a_sums = run_a_shares(markets)
    us_rows, us_sum = run_us()
    # 分窗对比表
    for name, a_sum in a_sums.items():
        print(f"\n== A 股 vs {name} ==")
        for k, v in a_sum["by_window"].items():
            print(f"  窗口止 {k}: n={v['n']:5d} R2中位={v['r2_median']:.3f} "
                  f"|t_alpha|>2={v['alpha_abs_t_gt2']:.3f}")
        print(f"  合并: n={a_sum['n_stock_windows']} 股票={a_sum['n_stocks']} "
              f"R2 中位={a_sum['r2_median']:.3f} "
              f"IQR=[{a_sum['r2_q25']:.3f},{a_sum['r2_q75']:.3f}] "
              f"R2≥0.5 占比={a_sum['r2_share_ge_050']:.3f} "
              f"alpha|t|>2={a_sum['alpha_share_abs_t_gt_2']:.3f} "
              f"beta中位={a_sum['beta_median']:.3f}")
    print("\n== 美股对照 ==")
    for k, v in us_sum["by_window"].items():
        print(f"  窗口止 {k}: n={v['n']:5d} R2中位={v['r2_median']:.3f} "
              f"|t_alpha|>2={v['alpha_abs_t_gt2']:.3f}")
    print(f"  合并: n={us_sum['n_stock_windows']} 股票={us_sum['n_stocks']} "
          f"R2 中位={us_sum['r2_median']:.3f} "
          f"IQR=[{us_sum['r2_q25']:.3f},{us_sum['r2_q75']:.3f}] "
          f"R2≥0.5 占比={us_sum['r2_share_ge_050']:.3f} "
          f"alpha|t|>2={us_sum['alpha_share_abs_t_gt_2']:.3f} "
          f"beta中位={us_sum['beta_median']:.3f}")
    payload = {"a_shares": a_sums, "us_control": us_sum,
               "windows": WINDOW_ENDS, "min_obs": MIN_OBS,
               "us_rows_sample": us_rows[:5]}
    print(f"\n结果已写入 {L.save_json('h2_results.json', payload)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
