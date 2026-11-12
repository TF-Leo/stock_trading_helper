#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P1 证据 v1（issue #51）：估值 / 资金面 / 指数 regime 的 IC 与分层检验。

任务书：GitHub issue #51；口径：``docs/P1_EVIDENCE_PLAN.md`` §3.1–§3.4。

两个阶段，联网与离线严格分开
----------------------------
1. ``fetch``（**联网**，可断点续跑）
   构建样本池 → 腾讯日线 → baostock 估值（复用 ``tools/precompute_valuation.py``）
   → 新浪资金流备胎（复用 ``tools/precompute_flow.py``）→ 沪深 300 指数日线。
   全部落盘到 ``evidence/p1/data/``（``evidence/`` 已 gitignore，不入库）。
   **不写** ``source/stock_analysis/data/``，避免污染主 CLI 的缓存。

2. ``analyze``（**完全离线**，只读上面落盘的缓存）
   构建 as-of 面板 → 横截面秩 IC（含非重叠与 Newey-West 口径）→ 分层 →
   买点确认 → 校准（样本不足时如实写「不足」）→ 价格因子基线对照。
   产出 ``evidence/p1/out/*.csv`` 与 stdout 表格。

3. ``check-asof``（离线）与 ``selftest``（离线，含合成数据）
   证明面板无前视、IC/分层实现正确。二者是分析结果的**前置门槛**。

为什么标签用「次日开盘」而不是收盘
----------------------------------
§3.1.1：``r_{d,h} = open_{d+1+h} / open_{d+1} - 1``。若用 ``close_{d+h}/close_d``，
就等于假设「看到 d 日收盘价、再按 d 日收盘价成交」，是前视。本脚本全部用开盘口径；
``tools/ic_report.py`` 用的是收盘口径，故基线在同一开盘口径下**重算一遍**再比较
增量，不直接引用它自己的数字。

用法
----
    # 联网预计算（约 10–20 分钟，逐票串行；断点续跑会跳过已有缓存）
    python tools/context_ic.py fetch --start 2018-01-01 --limit 100

    # 离线分析（秒级）
    python tools/context_ic.py analyze --horizon 20

    # 前置校验
    python tools/context_ic.py check-asof
    python tools/context_ic.py selftest

退出码：0 = 成功；1 = 有票取数失败或数据不足；2 = 参数/依赖错误。
"""
from __future__ import annotations

import argparse
import glob
import json
import math
import os
import sys
import time
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "source")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

DATA = os.path.join(ROOT, "evidence", "p1", "data")
OUT = os.path.join(ROOT, "evidence", "p1", "out")
PRICE_DIR = os.path.join(DATA, "price")
VAL_DIR = os.path.join(DATA, "valuation")
FLOW_DIR = os.path.join(DATA, "flow")
INDEX_DIR = os.path.join(DATA, "index")

#: 与 ``tools/forward_log.py:38-40`` / ``tools/precompute_flow.py:63-66`` 同一批自选。
WATCHLIST = ["000858", "002270", "002371", "002415", "002594", "300476", "300750",
             "600036", "600519", "600522", "601318", "601398", "603259", "688981"]
#: issue #51 指定追加的标的。
EXTRA = ["601066"]

DEFAULT_START = "2018-01-01"
HORIZON = 20
#: 非重叠口径的最少点数。少于该数不给 t（4 个点算出的 t 无意义）。
MIN_EFF_POINTS = 8
#: §3.2：过去 756 个交易日（≈3 年）分位；与 ``data_loader/valuation.py:59`` 同口径。
PCT_WINDOW = 756
PCT_MIN_SAMPLES = 250

#: 预注册：每个上下文只允许一个主因子（§3.1.4 多重检验约束）。
MAIN_FACTOR = {"valuation": "pb_pct_3y", "flow": "flow_net_20d_pct",
               "regime": "index_label"}

#: 买点确认用的策略（**预注册 3 个**，各代表一个族；不按结果挑选）。
BUY_STRATEGIES = ["均线趋势(5-20-60)", "MACD(12-26-9)", "量价突破"]


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------
def _ensure_dirs():
    for path in (DATA, OUT, PRICE_DIR, VAL_DIR, FLOW_DIR, INDEX_DIR):
        os.makedirs(path, exist_ok=True)


def _log(msg):
    print(msg, flush=True)


def _write_json(path, payload):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=1, sort_keys=True)
        handle.write("\n")
    os.replace(tmp, path)


def _read_json(path):
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


# ---------------------------------------------------------------------------
# 阶段 1：联网预计算
# ---------------------------------------------------------------------------
def build_pool(limit, use_csi300=True):
    """样本池：自选 14 + 601066 + 沪深 300 当前成分（按代码升序补齐到 limit）。

    返回 ``(codes, provenance)``。成分是 **2026-11-11 快照**，不是历史时点成分，
    报告必须标注 current-constituent（§3.1.3 幸存者偏差声明）。
    """
    codes, provenance = [], {"watchlist": list(WATCHLIST), "extra": list(EXTRA),
                             "csi300_snapshot_date": None, "csi300_added": 0}
    for code in WATCHLIST + EXTRA:
        if code not in codes:
            codes.append(code)
    if not use_csi300:
        return codes[:limit], provenance

    try:
        import akshare as ak
        frame = ak.index_stock_cons_csindex(symbol="000300")
    except Exception as exc:  # noqa: BLE001
        _log(f"[WARN] 沪深 300 成分获取失败（{type(exc).__name__}: {exc}），"
             f"样本池退化为自选 {len(codes)} 只")
        return codes[:limit], provenance

    provenance["csi300_snapshot_date"] = str(frame["日期"].iloc[0])[:10]
    cons = sorted({str(c).zfill(6) for c in frame["成分券代码"].tolist()})
    for code in cons:
        if len(codes) >= limit:
            break
        if code in codes:
            continue
        if code[0] in ("4", "8", "9"):      # 北交所：baostock §6.5 不支持
            continue
        codes.append(code)
        provenance["csi300_added"] += 1
    return codes[:limit], provenance


def fetch_price(code, start):
    """腾讯日线（前复权，比例口径）→ ``evidence/p1/data/price/<code>.csv``。

    直接调 ``astock.fetch_daily``（1–2 次 HTTP），不走 ``get_stock_daily`` 的全量
    分页，也不写主 CLI 的缓存目录。
    """
    import pandas as pd
    from stock_analysis.data_sources import astock

    path = os.path.join(PRICE_DIR, f"{code}.csv")
    if os.path.exists(path):
        try:
            cached = pd.read_csv(path, index_col=0, parse_dates=True)
            if len(cached) > 0 and str(cached.index[-1].date()) >= "2026-11-10":
                return {"code": code, "rows": len(cached), "cached": True,
                        "first": str(cached.index[0].date()),
                        "last": str(cached.index[-1].date())}
        except Exception:  # noqa: BLE001
            pass
    last_exc = None
    # 腾讯对连续请求会限流（三个入口同时 501/空响应）。批量抓 100 只时实测
    # 在第 73 只后整段失败，隔几分钟重试即恢复 —— 因此这里对单票做退避重试，
    # 避免把限流当成「该票无数据」。
    for attempt in range(3):
        try:
            frame = astock.fetch_daily(code, start_date=start, adjust="qfq")
            if frame is None or frame.empty:
                raise RuntimeError("空行情")
            frame = frame[["open", "high", "low", "close", "volume"]].copy()
            frame.index.name = "date"
            frame.to_csv(path)
            return {"code": code, "rows": int(len(frame)), "cached": False,
                    "first": str(frame.index[0].date()),
                    "last": str(frame.index[-1].date())}
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            if attempt < 2:
                time.sleep(5.0 * (attempt + 1))
    raise last_exc


def fetch_index(start):
    """沪深 300（sh000300）日线 → ``evidence/p1/data/index/sh000300.csv``。"""
    import pandas as pd
    from stock_analysis.data_sources import astock

    path = os.path.join(INDEX_DIR, "sh000300.csv")
    frame = astock.fetch_index_daily("sh000300", start_date=start)
    if frame is None or frame.empty:
        raise RuntimeError("空指数行情")
    frame.index.name = "date"
    frame.to_csv(path)
    return {"rows": int(len(frame)), "first": str(frame.index[0].date()),
            "last": str(frame.index[-1].date())}


def cmd_fetch(args):
    _ensure_dirs()
    import precompute_valuation as pv      # tools/ 下的既有预计算入口
    import precompute_flow as pf

    codes, provenance = build_pool(args.limit, use_csi300=not args.no_csi300)
    _log(f"样本池 {len(codes)} 只（自选 {len(WATCHLIST)} + 追加 {len(EXTRA)} + "
         f"沪深300 补齐 {provenance['csi300_added']}，快照 "
         f"{provenance['csi300_snapshot_date']}）")

    price_ok, price_fail = [], []
    for i, code in enumerate(codes, 1):
        try:
            info = fetch_price(code, args.start)
            price_ok.append(info)
            _log(f"[price {i}/{len(codes)}] {code} {info['rows']} 根 "
                 f"{info['first']}~{info['last']}{' (cache)' if info['cached'] else ''}")
        except Exception as exc:  # noqa: BLE001
            price_fail.append({"code": code, "reason": f"{type(exc).__name__}: {exc}"})
            _log(f"[price {i}/{len(codes)}] {code} FAIL {exc}")

    _log("估值预计算（baostock §6.5，逐票串行）...")
    val_stats = pv.precompute(codes, out_dir=VAL_DIR, start=args.start,
                              end=args.end, interval=0.2, retries=2)
    _log(f"  估值 ok={len(val_stats['ok'])} failed={len(val_stats['failed'])} "
         f"login={val_stats['login']} wall={val_stats['wall_seconds']}s")

    _log("资金流预计算（东财优先，新浪备胎；防封铁律 ≥1.0s/请求）...")
    flow_stats = pf.precompute(codes, out_dir=FLOW_DIR, interval=1.0, retries=1)
    _log(f"  资金流 ok={len(flow_stats['ok'])} failed={len(flow_stats['failed'])} "
         f"requests={flow_stats['requests']} wall={flow_stats['wall_seconds']}s "
         f"sources={flow_stats['ok_sources']}")

    idx = fetch_index(args.start)
    _log(f"指数 sh000300 {idx['rows']} 根 {idx['first']}~{idx['last']}")

    manifest = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "git_head": os.popen("git -C %s rev-parse HEAD" % ROOT).read().strip(),
        "python": sys.version.split()[0],
        "start": args.start, "end": args.end,
        "pool": codes, "pool_size": len(codes), "provenance": provenance,
        "price_ok": len(price_ok), "price_failed": price_fail,
        "valuation": {"ok": len(val_stats["ok"]), "failed": val_stats["failed"],
                      "login": val_stats["login"],
                      "wall_seconds": val_stats["wall_seconds"]},
        "flow": {"ok": len(flow_stats["ok"]), "failed": flow_stats["failed"],
                 "sources": flow_stats["ok_sources"],
                 "requests": flow_stats["requests"],
                 "wall_seconds": flow_stats["wall_seconds"]},
        "index": idx,
        "paths": {"price": PRICE_DIR, "valuation": VAL_DIR, "flow": FLOW_DIR,
                  "index": INDEX_DIR},
    }
    _write_json(os.path.join(DATA, "manifest.json"), manifest)
    _log(f"\n清单 → {os.path.join(DATA, 'manifest.json')}")
    return 0 if (not price_fail and not val_stats["failed"]) else 1


# ---------------------------------------------------------------------------
# 面板构建（离线）
# ---------------------------------------------------------------------------
def _rolling_percentile(values, window, min_samples, usable=None):
    """滚动分位：对每个 t，取 ``values[t-window+1 : t+1]`` 的**有效样本**，
    计算 ``100 × count(v < v_t) / (n - 1)``。

    与 ``data_loader/valuation.py::percentile_rank`` 定义**完全一致**（并列不计入
    「严格小于」），且与 ``_metric_percentile`` 的样本过滤同口径：

    * 窗口长度按**估值行数**（含停牌日）计，与生产 ``rows[-756:]`` 一致；
    * ``usable`` 为 False 的行（停牌 / ``trade_status=0``）**不进入样本**，但仍占窗口位置；
    * 当前值必须有限且 ``> 0``，有效样本数 ≥ ``min_samples`` 才给分位，否则 NaN。

    用 numpy 滑窗实现，避免 2100×100 次 Python 循环。
    """
    import numpy as np
    from numpy.lib.stride_tricks import sliding_window_view

    arr = np.asarray(values, dtype=float)
    n = len(arr)
    out = np.full(n, np.nan)
    if n < 2:
        return out
    if usable is None:
        usable = np.ones(n, dtype=bool)
    else:
        usable = np.asarray(usable, dtype=bool)
    w = min(window, n)
    pad = np.full(w - 1, np.nan)
    padded = np.concatenate([pad, arr])
    usable_pad = np.concatenate([np.zeros(w - 1, dtype=bool), usable])
    windows = sliding_window_view(padded, w)          # 形状 (n, w)
    usable_windows = sliding_window_view(usable_pad, w)
    valid = np.isfinite(windows) & (windows > 0) & usable_windows
    counts = valid.sum(axis=1)
    current = windows[:, -1]
    less = (windows < current[:, None]) & valid
    n_less = less.sum(axis=1)
    ok = (np.isfinite(current) & (current > 0) & (counts >= min_samples)
          & (counts >= 2))
    out[ok] = 100.0 * n_less[ok] / (counts[ok] - 1)
    return out


def _load_price(code):
    import pandas as pd
    path = os.path.join(PRICE_DIR, f"{code}.csv")
    if not os.path.exists(path):
        return None
    frame = pd.read_csv(path, index_col=0, parse_dates=True)
    frame = frame[~frame.index.duplicated(keep="last")].sort_index()
    return frame if len(frame) else None


def _load_valuation(code):
    """复用生产解析器 ``data_loader.valuation.read_cache``（含字段校验）。"""
    from stock_analysis.data_loader import valuation as val_mod
    return val_mod.read_cache(code, valuation_dir=VAL_DIR)


def _load_flow(code):
    """复用生产解析器 ``data_loader.flow.read_cache``（含口径分支）。"""
    from stock_analysis.data_loader import flow as flow_mod
    return flow_mod.read_cache(code, flow_dir=FLOW_DIR)


def build_panel(codes, start, horizon=HORIZON):
    """逐票构建 as-of 特征面板，返回 (long DataFrame, diagnostics)。"""
    import numpy as np
    import pandas as pd

    rows, diag = [], {"stocks_used": [], "stocks_skipped": [],
                      "valuation_missing": [], "flow_missing": [],
                      "flow_calibers": {}, "flow_dates": []}
    for code in codes:
        price = _load_price(code)
        if price is None or len(price) < 300:
            diag["stocks_skipped"].append({"code": code, "reason": "price 不足 300 根"})
            continue
        price = price.loc[start:].copy()
        if len(price) < 300:
            diag["stocks_skipped"].append({"code": code, "reason": "区间内不足 300 根"})
            continue
        idx = price.index

        # ---- 价格因子（与 tools/ic_report.py:81-88 同定义）----
        close, vol = price["close"], price["volume"]
        feat = pd.DataFrame(index=idx)
        feat["mom5"] = close / close.shift(5) - 1
        feat["mom20"] = close / close.shift(20) - 1
        feat["mom60"] = close / close.shift(60) - 1
        feat["dev20"] = close / close.rolling(20).mean() - 1
        feat["lowvol20"] = -close.pct_change().rolling(20).std()
        feat["volratio"] = vol / vol.rolling(20).mean()

        # ---- 标签：§3.1.1 开盘口径 r = open[d+1+h]/open[d+1] - 1 ----
        opn = price["open"]
        feat["fwd"] = opn.shift(-(horizon + 1)) / opn.shift(-1) - 1.0

        # ---- 估值（as-of：date <= d，pub_lag=0）----
        payload = _load_valuation(code)
        if payload is None:
            diag["valuation_missing"].append(code)
        else:
            vrows = pd.DataFrame(payload["rows"])
            vrows["date"] = pd.to_datetime(vrows["date"])
            vrows = vrows.set_index("date").sort_index()
            vrows = vrows[~vrows.index.duplicated(keep="last")]
            # ffill 只用过去行 → as-of 成立；不允许出现 date > d 的行
            for col in ("pe_ttm", "pb", "ps", "pcf", "turnover", "close"):
                feat[col] = vrows[col].reindex(idx, method="ffill")
            feat["trade_status"] = vrows["trade_status"].reindex(idx, method="ffill")
            feat["is_st"] = vrows["is_st"].reindex(idx, method="ffill")
            feat["close_bs"] = vrows["close"].reindex(idx, method="ffill")
            # 3 年滚动分位：**在估值行日历上**算（与生产 ``rows[-756:]`` 逐行一致），
            # 再 ffill 到价格交易日。停牌日（trade_status=0）占窗口位置但不进入样本。
            usable = vrows["trade_status"].fillna(0).astype(bool).to_numpy()
            for metric, col in (("pe", "pe_ttm"), ("pb", "pb"), ("ps", "ps")):
                pct = _rolling_percentile(vrows[col].to_numpy(), PCT_WINDOW,
                                          PCT_MIN_SAMPLES, usable=usable)
                feat[f"{metric}_pct_3y"] = pd.Series(
                    pct, index=vrows.index).reindex(idx, method="ffill")
            # 流通股本（股）= volume / (turn%/100)；turn 与 volume 都是原始量纲，
            # 实测 600519 2026-11-12: 3237300 / 0.00259 = 1.25e9 股，与总股本一致。
            turn = feat["turnover"].where(feat["turnover"] > 0)
            float_shares = (price["volume"] / (turn / 100.0)).rolling(20).median()
            feat["float_mcap"] = feat["close_bs"] * float_shares
            feat["log_float_mcap"] = np.log(feat["float_mcap"].where(
                feat["float_mcap"] > 0))

        # ---- 资金流（as-of：date < d，pub_lag=1；口径按缓存标注分支）----
        fpayload = _load_flow(code)
        if fpayload is None:
            diag["flow_missing"].append(code)
        else:
            caliber = fpayload["caliber"]
            field = "net_amount" if caliber == "sina_total_net" else "main_net"
            diag["flow_calibers"][caliber] = diag["flow_calibers"].get(caliber, 0) + 1
            frows = pd.DataFrame(fpayload["rows"])
            frows["date"] = pd.to_datetime(frows["date"])
            frows = frows.set_index("date").sort_index()
            diag["flow_dates"].append([str(frows.index[0].date()),
                                       str(frows.index[-1].date())])
            # 逐日「严格早于 d」的 5/20 行求和
            s = frows[field].astype(float)
            shifted = s.shift(1)                    # 让 date==d 的行落在 d 之后
            sum5 = shifted.rolling(5, min_periods=5).sum().reindex(idx, method="ffill")
            sum20 = shifted.rolling(20, min_periods=20).sum().reindex(idx, method="ffill")
            mcap = feat.get("float_mcap")
            if mcap is not None:
                feat["flow_net_5d_pct"] = sum5 / mcap * 100.0
                feat["flow_net_20d_pct"] = sum20 / mcap * 100.0
            feat["flow_net_5d_raw"] = sum5
            feat["flow_net_20d_raw"] = sum20
            feat["flow_caliber"] = caliber

        feat["code"] = code
        rows.append(feat.reset_index().rename(columns={"index": "date"}))

    if not rows:
        return None, diag
    panel = pd.concat(rows, ignore_index=True)
    panel["date"] = pd.to_datetime(panel["date"])
    diag["stocks_used"] = sorted(panel["code"].unique().tolist())
    return panel, diag


def add_index_regime(panel):
    """把沪深 300 regime 拼到面板上（时序因子，as-of d，pub_lag=0）。"""
    import numpy as np
    import pandas as pd
    from stock_analysis.advisor.analyzer import _regime_from_row

    path = os.path.join(INDEX_DIR, "sh000300.csv")
    if not os.path.exists(path):
        return panel, None
    idx = pd.read_csv(path, index_col=0, parse_dates=True).sort_index()
    close = idx["close"].astype(float)
    ma20 = close.rolling(20).mean()
    ma60 = close.rolling(60).mean()
    ret20 = (close / close.shift(20) - 1) * 100
    ret60 = (close / close.shift(60) - 1) * 100
    vol = close.pct_change().rolling(20).std() * np.sqrt(252)

    labels = []
    for c, m20, m60, r20, r60, v in zip(close, ma20, ma60, ret20, ret60, vol):
        if any(x != x for x in (c, m20, m60)):
            labels.append(None)
            continue
        labels.append(_regime_from_row(c, m20, m60,
                                       0.0 if r20 != r20 else float(r20),
                                       0.0 if r60 != r60 else float(r60),
                                       None if v != v else float(v))[0])
    index_frame = pd.DataFrame({"index_label": labels, "index_ret20": ret20 / 100.0,
                                "index_vol20": vol}, index=idx.index)
    index_frame["index_fwd20"] = (close.shift(-(HORIZON + 1))
                                  / close.shift(-1) - 1.0)
    merged = panel.merge(index_frame, left_on="date", right_index=True, how="left")
    return merged, index_frame


# ---------------------------------------------------------------------------
# 统计
# ---------------------------------------------------------------------------
def _newey_west_t(values, lag):
    """Newey-West(lag) t 值（Bartlett 核）。"""
    import numpy as np
    x = np.asarray(values, dtype=float)
    x = x[np.isfinite(x)]
    n = len(x)
    if n < 3:
        return None
    mean = float(x.mean())
    demeaned = x - mean
    gamma0 = float(demeaned @ demeaned) / n
    total = gamma0
    for lag_i in range(1, min(lag, n - 1) + 1):
        weight = 1.0 - lag_i / (lag + 1.0)
        cov = float(demeaned[lag_i:] @ demeaned[:-lag_i]) / n
        total += 2.0 * weight * cov
    if total <= 0:
        return None
    return mean / math.sqrt(total / n)


def _spearman_ic(panel, factor, horizon_col="fwd", min_names=10):
    """每日横截面 Spearman；返回 (Series of daily IC, 每日样本数 Series)。"""
    import numpy as np
    import pandas as pd

    sub = panel[["date", "code", factor, horizon_col]].dropna()
    if sub.empty:
        return pd.Series(dtype=float), pd.Series(dtype=int)
    ics, ns, days = [], [], []
    for day, block in sub.groupby("date", sort=True):
        if len(block) < min_names:
            continue
        a = block[factor].rank()
        b = block[horizon_col].rank()
        if a.std() == 0 or b.std() == 0:
            continue
        ics.append(float(np.corrcoef(a, b)[0, 1]))
        ns.append(len(block))
        days.append(day)
    if not ics:
        return pd.Series(dtype=float), pd.Series(dtype=int)
    index = pd.DatetimeIndex(days, name="date")
    return pd.Series(ics, index=index), pd.Series(ns, index=index)


def ic_summary(panel, factor, horizon_col="fwd", nw_lag=HORIZON - 1,
               min_names=10):
    """单因子横截面 IC 汇总（§3.1.4 预注册指标）。

    返回 dict：ic_mean / ic_std / t_iid / n_days / t_nw / n_eff / t_eff /
    icir / pos_ratio / yearly（按年 IC 同号占比） / oos_*（最后 30% 时间）。

    三种 t 值都给，是因为 20 日重叠窗口下 iid t 会虚高（同一收益被重复计数
    20 次）；**主口径是 t_nw**，t_eff 是更保守的非重叠对照。
    """
    import numpy as np
    import pandas as pd

    sub = panel[["date", "code", factor, horizon_col]].dropna()
    if sub.empty:
        return {"factor": factor, "ic_mean": None, "n_days": 0,
                "note": "无有效样本"}
    series, _names = _spearman_ic(panel, factor, horizon_col, min_names)
    if len(series) < 10:
        return {"factor": factor, "ic_mean": None, "n_days": len(series),
                "note": f"有效交易日仅 {len(series)} < 10"}
    mean = float(series.mean())
    std = float(series.std(ddof=1))
    n = int(len(series))
    t_iid = mean / (std / math.sqrt(n)) if std > 0 else None
    t_nw = _newey_west_t(series.to_numpy(), nw_lag)
    # 非重叠口径：每隔 horizon 个交易日取一天（同一 20 日窗口不重复计数）。
    # **门槛**：少于 MIN_EFF_POINTS（8）个非重叠点时不给 t —— 4 个点算出的 t 没有
    # 意义（实测资金流只有 79 天 → 4 个点，t=-8.6 完全是假象）。
    step = max(1, HORIZON)
    sub_sample = series.iloc[::step]
    n_eff = int(len(sub_sample))
    t_eff = None
    if n_eff >= MIN_EFF_POINTS and sub_sample.std(ddof=1) > 0:
        t_eff = float(sub_sample.mean() / (sub_sample.std(ddof=1) / math.sqrt(n_eff)))
    years = series.groupby(series.index.year)
    yearly = {int(y): {"ic_mean": round(float(g.mean()), 4), "n": int(len(g))}
              for y, g in years}
    same_sign = sum(1 for v in yearly.values()
                    if (v["ic_mean"] > 0) == (mean > 0))
    # §3.1.4「改 action」门槛要求：样本外（**最后 30% 时间**）|IC|≥0.02 且 |t|≥2。
    # 这里按时间切分，样本外同样给 NW t（重叠窗口必须做自相关修正）。
    cut = int(len(series) * 0.7)
    oos = series.iloc[cut:]
    oos_mean = float(oos.mean()) if len(oos) else None
    oos_t_nw = _newey_west_t(oos.to_numpy(), nw_lag) if len(oos) >= 10 else None
    oos_eff = oos.iloc[::step]
    oos_t_eff = None
    if len(oos_eff) >= MIN_EFF_POINTS and oos_eff.std(ddof=1) > 0:
        oos_t_eff = float(oos_eff.mean()
                          / (oos_eff.std(ddof=1) / math.sqrt(len(oos_eff))))
    return {
        "factor": factor,
        "ic_mean": round(mean, 6),
        "ic_std": round(std, 6),
        "icir": round(mean / std, 4) if std > 0 else None,
        "t_iid": round(float(t_iid), 3) if t_iid is not None else None,
        "t_nw": round(float(t_nw), 3) if t_nw is not None else None,
        "n_days": n,
        "n_eff": n_eff,
        "t_eff": round(float(t_eff), 3) if t_eff is not None else None,
        "pos_ratio": round(float((series > 0).mean()), 4),
        "year_same_sign": f"{same_sign}/{len(yearly)}",
        "oos_from": str(oos.index[0].date()) if len(oos) else None,
        "oos_ic": round(oos_mean, 6) if oos_mean is not None else None,
        "oos_t_nw": round(float(oos_t_nw), 3) if oos_t_nw is not None else None,
        "oos_t_eff": round(float(oos_t_eff), 3) if oos_t_eff is not None else None,
        "oos_same_sign": (bool(oos_mean is not None and mean != 0
                               and (oos_mean > 0) == (mean > 0))),
        "yearly": yearly,
    }


def stratify(panel, factor, groups=5, horizon_col="fwd", min_names=20):
    """按每日横截面分位分 ``groups`` 组，返回逐组统计与 Top-Bottom 价差。"""
    import numpy as np
    import pandas as pd

    sub = panel[["date", "code", factor, horizon_col]].dropna()
    if sub.empty:
        return None
    records = []
    spreads = []
    for day, block in sub.groupby("date", sort=True):
        if len(block) < min_names:
            continue
        ranks = block[factor].rank(method="first")
        q = pd.qcut(ranks, groups, labels=False, duplicates="drop")
        if q.nunique() < groups:
            continue
        for g in range(groups):
            sel = block[horizon_col][q == g]
            if len(sel) == 0:
                continue
            records.append({"date": day, "group": g, "ret": float(sel.mean()),
                            "n": int(len(sel))})
        top = block[horizon_col][q == groups - 1].mean()
        bot = block[horizon_col][q == 0].mean()
        spreads.append(float(top - bot))
    if not records:
        return None
    frame = pd.DataFrame(records)
    table = []
    for g in range(groups):
        sel = frame[frame["group"] == g]
        rets = sel["ret"]
        table.append({
            "group": g + 1,
            "mean_fwd": round(float(rets.mean()), 6),
            "hit_rate": round(float((rets > 0).mean()), 4),
            "n_days": int(len(rets)),
            "avg_names": round(float(sel["n"].mean()), 1),
        })
    sp = pd.Series(spreads)
    t_sp = float(sp.mean() / (sp.std(ddof=1) / math.sqrt(len(sp)))) if len(sp) > 2 and sp.std(ddof=1) > 0 else None
    # 重叠 20 日窗口会让 iid t 严重虚高；同时给非重叠抽样与 NW 口径。
    sp_eff = sp.iloc[::max(1, HORIZON)]
    t_sp_eff = None
    if len(sp_eff) >= MIN_EFF_POINTS and sp_eff.std(ddof=1) > 0:
        t_sp_eff = float(sp_eff.mean() / (sp_eff.std(ddof=1) / math.sqrt(len(sp_eff))))
    return {"table": table,
            "top_minus_bottom": round(float(sp.mean()), 6),
            "spread_t": round(float(t_sp), 3) if t_sp is not None else None,
            "spread_t_eff": round(float(t_sp_eff), 3) if t_sp_eff is not None else None,
            "spread_t_nw": (round(_newey_west_t(sp.to_numpy(), HORIZON - 1), 3)
                            if _newey_west_t(sp.to_numpy(), HORIZON - 1) is not None
                            else None),
            "spread_win": round(float((sp > 0).mean()), 4),
            "n_days": int(len(sp)),
            "n_eff": int(len(sp_eff))}


def _two_prop_z(k1, n1, k2, n2):
    """两比例检验 z 值与双侧 p。"""
    import numpy as np
    if n1 == 0 or n2 == 0:
        return None, None
    p1, p2 = k1 / n1, k2 / n2
    pooled = (k1 + k2) / (n1 + n2)
    se = math.sqrt(pooled * (1 - pooled) * (1 / n1 + 1 / n2))
    if se == 0:
        return None, None
    z = (p1 - p2) / se
    p = float(2 * (1 - 0.5 * (1 + math.erf(abs(z) / math.sqrt(2)))))
    return float(z), p


def buy_point_check(panel, horizon=HORIZON):
    """买点确认：现有策略买点上比较「有/无上下文」的 20 日命中率与均值。

    买点集合 = 预注册 3 个策略的买点并集（每票每策略独立生成，不联网）。
    上下文门槛（每个只测一次，不按结果挑选）：
      * 估值：``pb_pct_3y`` ≤ **截至 d 的池内横截面中位数**（expanding，只用
        已实现的历史；不能用全样本中位数 —— 那是前视）
      * 资金：``flow_net_20d_pct`` > 0
      * regime：``index_label`` != 'Bear'
    """
    import numpy as np
    import pandas as pd
    from stock_analysis.strategy.registry import build_strategy_signals
    from decision.expectancy import trailing_expectancy_ok
    from decision.policy import run_policy

    signals = build_strategy_signals()
    available = [name for name in BUY_STRATEGIES if name in signals]
    missing = [name for name in BUY_STRATEGIES if name not in signals]

    # as-of 横截面中位数：每个交易日只用**当天**的池内分位算中位数。
    # 所有 pb_pct_3y 本身已是 date<=d 的滚动分位，故该中位数不含未来信息。
    med_by_day = (panel.dropna(subset=["pb_pct_3y"])
                  .groupby("date")["pb_pct_3y"].median())

    trades = []
    policy_rows = []
    for code, block in panel.groupby("code"):
        price = _load_price(code)
        if price is None:
            continue
        price = price.loc[price.index >= block["date"].min()].copy()
        price = price.reindex(block["date"]).dropna(how="all")
        if len(price) < 300:
            continue
        block = block.set_index("date").reindex(price.index)
        buy_any = pd.Series(False, index=price.index)
        for name in available:
            try:
                sig = signals[name](price)
                buy_any |= sig["buy"].astype(bool)
            except Exception:  # noqa: BLE001
                continue
        fwd = (price["open"].shift(-(horizon + 1)) / price["open"].shift(-1) - 1.0)
        med = med_by_day.reindex(price.index)
        pb_cheap = (block["pb_pct_3y"].notna() & med.notna()
                    & (block["pb_pct_3y"] <= med))
        for day in price.index[buy_any.fillna(False)]:
            if day not in block.index:
                continue
            row = block.loc[day]
            ret = fwd.get(day)
            if ret is None or not np.isfinite(ret):
                continue
            trades.append({
                "code": code, "date": day, "ret": float(ret),
                "pb_pct_3y": row.get("pb_pct_3y"),
                "pb_cheap": bool(pb_cheap.get(day, False)),
                "flow_net_20d_pct": row.get("flow_net_20d_pct"),
                "index_label": row.get("index_label"),
            })
        # policy 回放（有/无上下文门槛各一次）。
        # **关键**：资金流只有 120 日、估值分位需 250 样本预热，若拿「全历史基线」
        # 与「只交易近 5 个月的门槛版」比较，差异来自**样本窗口**而不是门槛本身。
        # 因此每个门槛的基线都在「该门槛有定义的那段区间」内重跑（同窗口对照）。
        try:
            exp_ok = trailing_expectancy_ok(price["close"], buy_any, horizon=horizon)
            gates = {
                "pb_cheap": pb_cheap.to_numpy(),
                "flow_positive": (block["flow_net_20d_pct"] > 0
                                  ).fillna(False).to_numpy(),
                "not_bear": (block["index_label"].notna()
                             & (block["index_label"] != "Bear")).to_numpy(),
            }
            defined = {
                "pb_cheap": block["pb_pct_3y"].notna().to_numpy(),
                "flow_positive": block["flow_net_20d_pct"].notna().to_numpy(),
                "not_bear": block["index_label"].notna().to_numpy(),
            }
            for gate_name, mask in gates.items():
                window = defined[gate_name]
                if window.sum() < 300:
                    continue
                sub = price.iloc[np.flatnonzero(window)]
                if len(sub) < 300:
                    continue
                sub_buy = buy_any.iloc[np.flatnonzero(window)]
                sub_ok = exp_ok[np.flatnonzero(window)]
                sub_gate = mask[np.flatnonzero(window)]
                base = run_policy(sub, sub_buy, sub_ok, horizon=horizon,
                                  exec_price="next_open", slippage=0.001)
                gated = run_policy(sub, sub_buy, sub_ok & sub_gate, horizon=horizon,
                                   exec_price="next_open", slippage=0.001)
                policy_rows.append({
                    "code": code, "gate": gate_name,
                    "window_bars": int(len(sub)),
                    "window_from": str(sub.index[0].date()),
                    "window_to": str(sub.index[-1].date()),
                    "base_trades": base["trade_count"],
                    "base_annual": base["annual_return"],
                    "base_mdd": base["max_drawdown"],
                    "gated_trades": gated["trade_count"],
                    "gated_annual": gated["annual_return"],
                    "gated_mdd": gated["max_drawdown"],
                })
        except Exception:  # noqa: BLE001
            continue

    if not trades:
        return {"n_trades": 0, "note": "无有效买点", "missing_strategies": missing}
    frame = pd.DataFrame(trades)
    n_all = len(frame)
    hit_all = int((frame["ret"] > 0).sum())

    def _split(col, predicate, label):
        valid = frame[frame[col].notna()]
        yes = valid[predicate(valid[col])]
        no = valid[~predicate(valid[col])]
        if len(yes) == 0 or len(no) == 0:
            return {"gate": label, "n_with": len(yes), "n_without": len(no),
                    "note": "分组为空"}
        z, p = _two_prop_z(int((yes["ret"] > 0).sum()), len(yes),
                           int((no["ret"] > 0).sum()), len(no))
        return {
            "gate": label,
            "n_with": int(len(yes)), "n_without": int(len(no)),
            "hit_with": round(float((yes["ret"] > 0).mean()), 4),
            "hit_without": round(float((no["ret"] > 0).mean()), 4),
            "delta_hit": round(float((yes["ret"] > 0).mean()
                                     - (no["ret"] > 0).mean()), 4),
            "mean_with": round(float(yes["ret"].mean()), 6),
            "mean_without": round(float(no["ret"].mean()), 6),
            "delta_mean": round(float(yes["ret"].mean() - no["ret"].mean()), 6),
            "z": round(z, 3) if z is not None else None,
            "p": round(p, 4) if p is not None else None,
        }

    checks = [
        _split("pb_cheap", lambda s: s, "估值分位≤当日池内中位数"),
        _split("flow_net_20d_pct", lambda s: s > 0, "资金净流入>0"),
        _split("index_label", lambda s: s != "Bear", "指数regime≠Bear"),
    ]
    pol = pd.DataFrame(policy_rows) if policy_rows else pd.DataFrame()
    policy_summary = []
    skipped_gates = []
    for gate_name in ("pb_cheap", "flow_positive", "not_bear"):
        if pol.empty or gate_name not in set(pol["gate"]):
            skipped_gates.append({
                "gate": gate_name,
                "reason": "该门槛有定义的窗口不足 300 根（run_policy 需 ≥60 根，"
                          "且过短的回放不可比），不做回放对照",
            })
    if not pol.empty:
        for gate, block in pol.groupby("gate"):
            policy_summary.append({
                "gate": gate, "stocks": int(len(block)),
                "window_bars_median": int(block["window_bars"].median()),
                "window_from_min": str(block["window_from"].min()),
                "window_to_max": str(block["window_to"].max()),
                "base_trades": int(block["base_trades"].sum()),
                "gated_trades": int(block["gated_trades"].sum()),
                "base_annual_mean": round(float(block["base_annual"].mean()), 4),
                "gated_annual_mean": round(float(block["gated_annual"].mean()), 4),
                "base_mdd_mean": round(float(block["base_mdd"].mean()), 4),
                "gated_mdd_mean": round(float(block["gated_mdd"].mean()), 4),
                "mdd_improved": int((block["gated_mdd"] < block["base_mdd"]).sum()),
                "annual_improved": int((block["gated_annual"] > block["base_annual"]).sum()),
            })
    return {"n_trades": n_all,
            "hit_all": round(hit_all / n_all, 4),
            "mean_all": round(float(frame["ret"].mean()), 6),
            "strategies": available, "missing_strategies": missing,
            "checks": checks, "policy": policy_summary,
            "policy_skipped": skipped_gates}


def regime_timeseries(index_frame, panel):
    """§3.4：指数 regime 的时序检验 + 与个股 regime 的一致率。

    除指数自身的分标签统计外，还给出**现有个股 regime 的同一组统计**作为对照：
    只有当指数 regime 的分标签区分度**优于**个股 regime 时，才有理由说「指数
    regime 更能代表大盘环境」（§3.4 假设）。两者都只是时序（同一决策日全池共用
    一个标签），因此这里的 n 是「决策日 × 票」，存在横截面相关，t 仅作描述。
    """
    import numpy as np
    import pandas as pd
    from stock_analysis.advisor.analyzer import market_regime_series

    if index_frame is None:
        return None
    frame = index_frame.dropna(subset=["index_label"]).copy()

    def _by_label(label_series, fwd_series):
        rows = []
        for label, block in label_series.groupby(label_series):
            fwd = fwd_series.reindex(block.index).dropna()
            if len(fwd) == 0:
                continue
            t = None
            if len(fwd) > 2 and fwd.std(ddof=1) > 0:
                t = float(fwd.mean() / (fwd.std(ddof=1) / math.sqrt(len(fwd))))
            rows.append({"label": str(label), "n_days": int(len(block)),
                         "mean_fwd20": round(float(fwd.mean()), 6),
                         "hit_rate": round(float((fwd > 0).mean()), 4),
                         "t": round(t, 3) if t is not None else None})
        return rows

    rows = _by_label(frame["index_label"], frame["index_fwd20"])

    # 与个股 regime 的标签一致率 + 个股 regime 的同口径分标签统计
    agree_n = agree_same = 0
    per_stock = {}
    stock_label_rows = []
    stock_fwd_rows = []
    for code in sorted(panel["code"].unique()):
        price = _load_price(code)
        if price is None or len(price) < 100:
            continue
        series = market_regime_series(price)
        if series is None:
            continue
        fwd = price["open"].shift(-(HORIZON + 1)) / price["open"].shift(-1) - 1.0
        merged = pd.DataFrame({"stock": series, "fwd": fwd}).join(
            index_frame[["index_label"]], how="inner").dropna(subset=["stock"])
        if merged.empty:
            continue
        same = int((merged["stock"] == merged["index_label"]).sum())
        per_stock[code] = {"n": int(len(merged)),
                           "agree": round(same / len(merged), 4)}
        agree_n += len(merged)
        agree_same += same
        stock_label_rows.append(merged["stock"])
        stock_fwd_rows.append(merged["fwd"])
    stock_rows = []
    if stock_label_rows:
        labels = pd.concat(stock_label_rows, ignore_index=True)
        fwds = pd.concat(stock_fwd_rows, ignore_index=True)
        stock_rows = _by_label(labels, fwds)
    return {"by_label": rows,
            "stock_regime_by_label": stock_rows,
            "agreement_rate": round(agree_same / agree_n, 4) if agree_n else None,
            "agreement_n": agree_n,
            "per_stock_agreement": per_stock}


def calibration(panel, horizon_col="fwd"):
    """置信度校准：历史 ``confidence`` 无法离线复算，如实返回「不足」。

    ``confidence`` 由 ``cli.py:416-421`` 的 DSR/agreement/stability/交易数加权得出，
    依赖**当日**的 581 组网格锦标赛（单票单日约 8s）。要得到 n 个决策日 × m 只票的
    历史置信度，需要 8s × n × m 的回放成本；本脚本不承担该成本，也不伪造分箱。
    """
    return {"status": "insufficient",
            "reason": "历史 confidence 需逐日重跑 581 组锦标赛，成本约 8s/票/日；"
                      "本版不伪造分箱",
            "next": "用 tools/forward_log.py record 每日落盘 confidence，"
                    "20 个交易日后 evaluate 结算，积累 ≥100 条后再做可靠性表"}


# ---------------------------------------------------------------------------
# 阶段 2：离线分析
# ---------------------------------------------------------------------------
def cmd_analyze(args):
    _ensure_dirs()
    import numpy as np
    import pandas as pd

    manifest_path = os.path.join(DATA, "manifest.json")
    if not os.path.exists(manifest_path):
        print(f"ERROR 缺 {manifest_path}，请先运行 fetch", file=sys.stderr)
        return 2
    manifest = _read_json(manifest_path)
    codes = manifest["pool"]
    _log(f"样本池 {len(codes)} 只；区间 {args.start}~（缓存末日）")

    panel, diag = build_panel(codes, args.start, horizon=args.horizon)
    if panel is None:
        print("ERROR 面板为空", file=sys.stderr)
        return 1
    panel, index_frame = add_index_regime(panel)

    n_stocks = panel["code"].nunique()
    n_days = panel["date"].nunique()
    _log(f"面板：{n_stocks} 只 × {n_days} 个交易日 = {len(panel)} 行；"
         f"估值覆盖 {len(diag['stocks_used']) - len(diag['valuation_missing'])} 只，"
         f"资金流覆盖 {len(diag['stocks_used']) - len(diag['flow_missing'])} 只，"
         f"口径 {diag['flow_calibers']}")

    os.makedirs(OUT, exist_ok=True)
    panel.to_csv(os.path.join(OUT, "panel.csv"), index=False)

    factors = {
        "valuation": ["pb_pct_3y", "pe_pct_3y", "ps_pct_3y", "pb", "pe_ttm",
                      "log_float_mcap"],
        "flow": ["flow_net_5d_pct", "flow_net_20d_pct"],
        "price_baseline": ["mom5", "mom20", "mom60", "dev20", "lowvol20",
                           "volratio"],
    }

    ic_rows = []
    strat_rows = []
    for group, names in factors.items():
        for name in names:
            if name not in panel.columns:
                continue
            summary = ic_summary(panel, name, horizon_col="fwd",
                                 nw_lag=args.horizon - 1)
            summary["group"] = group
            summary["main"] = (MAIN_FACTOR.get(group) == name)
            ic_rows.append(summary)
            st = stratify(panel, name)
            if st:
                for row in st["table"]:
                    row.update({"factor": name, "group_name": group})
                    strat_rows.append(row)

    ic_frame = pd.DataFrame(ic_rows)
    ic_frame.to_csv(os.path.join(OUT, "ic.csv"), index=False)
    strat_frame = pd.DataFrame(strat_rows)
    if not strat_frame.empty:
        strat_frame.to_csv(os.path.join(OUT, "stratify.csv"), index=False)

    _print_ic_table(ic_frame)

    # 分层表（只打主因子 + 基线）
    for name in [MAIN_FACTOR["valuation"], MAIN_FACTOR["flow"], "mom60"]:
        if name not in panel.columns:
            continue
        st = stratify(panel, name)
        if st:
            _print_stratify(name, st)

    # regime 时序
    regime = regime_timeseries(index_frame, panel)
    if regime:
        _log("\n=== 指数 regime（时序，单因子；§3.4）===")
        _log(f"{'label':8s}{'n_days':>8s}{'mean_fwd20':>13s}{'hit_rate':>10s}{'t':>8s}")
        for row in regime["by_label"]:
            _log(f"{row['label']:8s}{row['n_days']:>8d}{row['mean_fwd20']:>13.4%}"
                 f"{row['hit_rate']:>10.4f}{(row['t'] if row['t'] is not None else 0):>8.2f}")
        _log(f"与个股 regime 标签一致率：{regime['agreement_rate']:.2%} "
             f"(n={regime['agreement_n']})")
        if regime.get("stock_regime_by_label"):
            _log("对照：个股自身 regime 的同口径分标签统计（§3.4 假设：指数应更优）")
            _log(f"{'label':8s}{'n_days':>8s}{'mean_fwd20':>13s}{'hit_rate':>10s}{'t':>8s}")
            for row in regime["stock_regime_by_label"]:
                _log(f"{row['label']:8s}{row['n_days']:>8d}{row['mean_fwd20']:>13.4%}"
                     f"{row['hit_rate']:>10.4f}"
                     f"{(row['t'] if row['t'] is not None else 0):>8.2f}")

    # 买点确认
    _log("\n=== 买点确认（预注册 3 策略并集）===")
    buy = buy_point_check(panel, horizon=args.horizon)
    if buy.get("n_trades"):
        _log(f"买点总数 {buy['n_trades']}，20 日命中率 {buy['hit_all']:.2%}，"
             f"均值 {buy['mean_all']:+.4%}")
        for row in buy["checks"]:
            if "hit_with" in row:
                _log(f"  {row['gate']}: 有 {row['hit_with']:.2%}(n={row['n_with']}) "
                     f"vs 无 {row['hit_without']:.2%}(n={row['n_without']}) "
                     f"Δ={row['delta_hit']:+.2%} p={row['p']}")
        if buy.get("policy"):
            _log("  policy 回放（run_policy, next_open, slippage=10bp；"
                 "基线与门槛版**同窗口**）：")
            for row in buy["policy"]:
                _log(f"    {row['gate']}: 窗口 {row['window_from_min']}~{row['window_to_max']}"
                     f"（中位 {row['window_bars_median']} 根），"
                     f"交易 {row['base_trades']}→{row['gated_trades']}，"
                     f"年化 {row['base_annual_mean']:+.2%}→{row['gated_annual_mean']:+.2%}"
                     f"（改善 {row['annual_improved']}/{row['stocks']} 票），"
                     f"maxDD {row['base_mdd_mean']:.2%}→{row['gated_mdd_mean']:.2%} "
                     f"（改善 {row['mdd_improved']}/{row['stocks']} 票）")
        for row in buy.get("policy_skipped") or []:
            _log(f"    {row['gate']}: 未回放 —— {row['reason']}")
    else:
        _log(f"  无有效买点：{buy.get('note')}")

    calib = calibration(panel)
    _log(f"\n=== 置信度校准 ===\n  {calib['status']}：{calib['reason']}")

    summary = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "git_head": manifest.get("git_head"),
        "horizon": args.horizon,
        "start": args.start,
        "pool_size": int(n_stocks),
        "pool_declared": len(codes),
        "trading_days": int(n_days),
        "panel_rows": int(len(panel)),
        "flow_calibers": diag["flow_calibers"],
        "flow_date_range": [min(d[0] for d in diag["flow_dates"]),
                            max(d[1] for d in diag["flow_dates"])]
        if diag["flow_dates"] else None,
        "stocks_skipped": diag["stocks_skipped"],
        "ic": ic_rows,
        "regime": regime,
        "buy_points": buy,
        "calibration": calib,
    }
    _write_json(os.path.join(OUT, "summary.json"), summary)
    _log(f"\n结果 → {OUT}")
    return 0


def _print_ic_table(ic_frame):
    _log("\n=== 横截面秩 IC（label = 次日开盘买入、20 日开盘卖出）===")
    _log(f"{'factor':18s}{'group':16s}{'IC':>9s}{'t_iid':>8s}{'t_nw':>8s}"
         f"{'t_eff':>8s}{'n':>6s}{'n_eff':>7s}{'ICIR':>8s}{'同号年':>8s}"
         f"{'OOS_IC':>9s}{'OOS_t':>8s}")
    for _, row in ic_frame.iterrows():
        def _f(v, fmt):
            return fmt.format(v) if isinstance(v, (int, float)) and v == v else "n/a"
        _log(f"{str(row['factor']):18s}{str(row['group']):16s}"
             f"{_f(row.get('ic_mean'), '{:+.4f}'):>9s}"
             f"{_f(row.get('t_iid'), '{:+.2f}'):>8s}"
             f"{_f(row.get('t_nw'), '{:+.2f}'):>8s}"
             f"{_f(row.get('t_eff'), '{:+.2f}'):>8s}"
             f"{_f(row.get('n_days'), '{:d}'):>6s}"
             f"{_f(row.get('n_eff'), '{:d}'):>7s}"
             f"{_f(row.get('icir'), '{:+.3f}'):>8s}"
             f"{str(row.get('year_same_sign') or 'n/a'):>8s}"
             f"{_f(row.get('oos_ic'), '{:+.4f}'):>9s}"
             f"{_f(row.get('oos_t_nw'), '{:+.2f}'):>8s}")
    _log("注：t_iid=按日 iid；t_nw=Newey-West(lag=19)；t_eff=非重叠 20 日抽样；"
         "OOS=最后 30% 时间（NW t）。")


def _print_stratify(name, st):
    _log(f"\n--- 分层（{name}，5 组）---")
    _log(f"{'group':>6s}{'mean_fwd':>11s}{'hit_rate':>10s}{'n_days':>8s}{'avg_names':>11s}")
    for row in st["table"]:
        _log(f"{row['group']:>6d}{row['mean_fwd']:>11.4%}{row['hit_rate']:>10.4f}"
             f"{row['n_days']:>8d}{row['avg_names']:>11.1f}")
    _log(f"Top-Bottom = {st['top_minus_bottom']:+.4%}  "
         f"t(iid)={st['spread_t']}  t(非重叠)={st['spread_t_eff']}  "
         f"t(NW)={st['spread_t_nw']}  胜率={st['spread_win']:.2%}  "
         f"n={st['n_days']}（非重叠 {st['n_eff']}）")


# ---------------------------------------------------------------------------
# 前置校验
# ---------------------------------------------------------------------------
def cmd_check_asof(args):
    """证明面板无前视：用截断历史重算特征，与完整面板逐值比对。

    对随机抽样的 (code, date)，只用 ``date <= d``（估值）/``date < d``（资金流）的
    原始行重算特征，必须与完整面板完全一致。任何不一致 = 存在前视。
    """
    import numpy as np
    import pandas as pd

    manifest = _read_json(os.path.join(DATA, "manifest.json"))
    codes = manifest["pool"][:args.sample_stocks]
    panel, diag = build_panel(codes, args.start, horizon=args.horizon)
    if panel is None:
        print("ERROR 面板为空", file=sys.stderr)
        return 1
    rng = np.random.default_rng(20261113)
    checks = failures = 0
    details = []
    for code in sorted(panel["code"].unique()):
        block = panel[panel["code"] == code].sort_values("date")
        valid = block.dropna(subset=["pb_pct_3y"])
        if valid.empty:
            continue
        picks = valid.sample(min(3, len(valid)), random_state=int(code) % 10000)
        payload = _load_valuation(code)
        if payload is None:
            continue
        vrows = pd.DataFrame(payload["rows"])
        vrows["date"] = pd.to_datetime(vrows["date"])
        for _, row in picks.iterrows():
            day = row["date"]
            past = vrows[vrows["date"] <= day]        # as-of：只允许 date <= d
            tail = past.tail(PCT_WINDOW)
            sample = [v for v in tail.loc[
                tail["trade_status"].fillna(0).astype(bool), "pb"].tolist()
                if v is not None and v == v and v > 0]
            if len(sample) < PCT_MIN_SAMPLES or row["pb"] <= 0:
                continue
            expected = 100.0 * sum(1 for v in sample if v < row["pb"]) / (len(sample) - 1)
            got = float(row["pb_pct_3y"])
            checks += 1
            if abs(expected - got) > 1e-9:
                failures += 1
                details.append({"code": code, "date": str(day.date()),
                                "expected": round(expected, 6),
                                "panel": round(got, 6)})
    # 资金流：断言面板的 flow_net_20d_pct 只由 date < d 的行算出（pub_lag=1）。
    # 做法：对每个抽样 (code, d)，只用 date < d 的行重算 20 日和，必须与面板值相等；
    # 同时证明「若把 date==d 的行也纳入，结果会不同」——否则该日无法区分两种口径。
    flow_checks = flow_failures = flow_indistinguishable = 0
    for code in sorted(panel["code"].unique()):
        payload = _load_flow(code)
        if payload is None:
            continue
        field = ("net_amount" if payload["caliber"] == "sina_total_net"
                 else "main_net")
        frows = pd.DataFrame(payload["rows"])
        frows["date"] = pd.to_datetime(frows["date"])
        frows = frows.sort_values("date")
        block = panel[(panel["code"] == code)
                      & panel["flow_net_20d_pct"].notna()
                      & panel["float_mcap"].notna()]
        if block.empty:
            continue
        for _, row in block.sample(min(3, len(block)),
                                   random_state=int(code) % 9973).iterrows():
            day = row["date"]
            strict = frows[frows["date"] < day]
            incl = frows[frows["date"] <= day]
            if len(strict) < 20 or len(incl) < 20:
                continue
            expected = (strict[field].astype(float).tail(20).sum()
                        / row["float_mcap"] * 100.0)
            flow_checks += 1
            if abs(expected - float(row["flow_net_20d_pct"])) > 1e-6:
                flow_failures += 1
                details.append({"code": code, "date": str(day.date()),
                                "flow_expected": round(float(expected), 6),
                                "flow_panel": round(float(row["flow_net_20d_pct"]), 6)})
            with_today = (incl[field].astype(float).tail(20).sum()
                          / row["float_mcap"] * 100.0)
            if abs(with_today - expected) < 1e-12:
                flow_indistinguishable += 1

    _log(f"估值分位 as-of 复核：{checks} 个抽样点，{failures} 个不一致")
    _log(f"资金流 pub_lag 复核：{flow_checks} 个抽样点，{flow_failures} 个不一致"
         f"（{flow_indistinguishable} 个点当天无资金流行，无法区分两种口径）")
    if details:
        for item in details[:5]:
            _log(f"  MISMATCH {item}")
    ok = failures == 0 and flow_failures == 0
    _log("结论：" + ("无前视 ✅" if ok else "存在前视 ❌"))
    return 0 if ok else 1


def cmd_selftest(args):
    """合成数据自检：验证 IC / 分层 / NW t 的实现正确。"""
    import numpy as np
    import pandas as pd

    rng = np.random.default_rng(7)
    n_days, n_stocks = 400, 60
    dates = pd.bdate_range("2020-01-01", periods=n_days)
    codes = [f"{i:06d}" for i in range(n_stocks)]
    rows = []
    for day in dates:
        factor = rng.normal(size=n_stocks)
        noise = rng.normal(size=n_stocks)
        # 构造已知关系：fwd = 0.05 * z(factor) + 0.10 * noise
        fwd = 0.05 * factor + 0.10 * noise
        for i, code in enumerate(codes):
            rows.append({"date": day, "code": code, "f": factor[i], "fwd": fwd[i]})
    panel = pd.DataFrame(rows)
    summary = ic_summary(panel, "f", nw_lag=19)
    ok = summary["ic_mean"] is not None and 0.35 < summary["ic_mean"] < 0.50
    _log(f"[1] 合成 IC（理论 ≈0.45）：{summary['ic_mean']}  "
         f"t_nw={summary['t_nw']}  → {'PASS' if ok else 'FAIL'}")

    # 反号因子必须给出负 IC
    panel["neg"] = -panel["f"]
    neg = ic_summary(panel, "neg", nw_lag=19)
    ok2 = neg["ic_mean"] is not None and neg["ic_mean"] < -0.35
    _log(f"[2] 反号因子 IC：{neg['ic_mean']}  → {'PASS' if ok2 else 'FAIL'}")

    # 分层：Top 组均值必须高于 Bottom 组
    st = stratify(panel, "f")
    ok3 = bool(st) and st["table"][-1]["mean_fwd"] > st["table"][0]["mean_fwd"]
    _log(f"[3] 分层单调：g1={st['table'][0]['mean_fwd']:+.4f} "
         f"g5={st['table'][-1]['mean_fwd']:+.4f} "
         f"spread_t={st['spread_t']}  → {'PASS' if ok3 else 'FAIL'}")

    # 纯噪声因子：|IC| 应接近 0
    panel["noise"] = rng.normal(size=len(panel))
    noise = ic_summary(panel, "noise", nw_lag=19)
    ok4 = noise["ic_mean"] is not None and abs(noise["ic_mean"]) < 0.05
    _log(f"[4] 噪声因子 IC：{noise['ic_mean']}  → {'PASS' if ok4 else 'FAIL'}")

    # 滚动分位：与 percentile_rank 定义一致
    from stock_analysis.data_loader.valuation import percentile_rank
    values = np.array([np.nan, 3.0, 1.0, 2.0, 5.0, 4.0, 0.0, 6.0])
    got = _rolling_percentile(values, window=8, min_samples=2)
    manual = percentile_rank(6.0, [3.0, 1.0, 2.0, 5.0, 4.0, 6.0])
    ok5 = abs(got[-1] - manual) < 1e-9
    _log(f"[5] 滚动分位：{got[-1]:.4f} vs percentile_rank={manual:.4f} "
         f"→ {'PASS' if ok5 else 'FAIL'}")

    passed = sum([ok, ok2, ok3, ok4, ok5])
    _log(f"\n自检 {passed}/5 通过")
    return 0 if passed == 5 else 1


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def build_parser():
    parser = argparse.ArgumentParser(
        description="P1 证据 v1：估值/资金面/指数 regime 的 IC 与分层检验")
    sub = parser.add_subparsers(dest="cmd", required=True)

    fetch = sub.add_parser("fetch", help="联网预计算（日线/估值/资金流/指数）")
    fetch.add_argument("--start", default=DEFAULT_START)
    fetch.add_argument("--end", default=None)
    fetch.add_argument("--limit", type=int, default=100,
                       help="样本池上限（默认 100；自选 15 只优先）")
    fetch.add_argument("--no-csi300", action="store_true",
                       help="只用自选 15 只，不补沪深 300 成分")
    fetch.set_defaults(func=cmd_fetch)

    analyze = sub.add_parser("analyze", help="离线分析（只读 evidence/p1/data）")
    analyze.add_argument("--start", default=DEFAULT_START)
    analyze.add_argument("--horizon", type=int, default=HORIZON)
    analyze.set_defaults(func=cmd_analyze)

    asof = sub.add_parser("check-asof", help="无前视校验")
    asof.add_argument("--start", default=DEFAULT_START)
    asof.add_argument("--horizon", type=int, default=HORIZON)
    asof.add_argument("--sample-stocks", type=int, default=12)
    asof.set_defaults(func=cmd_check_asof)

    selftest = sub.add_parser("selftest", help="合成数据自检")
    selftest.set_defaults(func=cmd_selftest)
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
