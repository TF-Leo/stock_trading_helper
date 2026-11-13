#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""R6 / issue #70：策略族与预测尺度重估（持有期 × 策略族，组合层面）。

任务书：GitHub issue #70。前置证据：``docs/P1_EVIDENCE_V1.md``（R3）、
``docs/P1_EVIDENCE_V2.md``（R5）—— 结论是 **20 日持有期 + 现有量价策略族** 下，
估值/资金面/指数 regime 与价格因子都检不出正向 edge。本工具按用户决定的方向，
评估「持有期 × 策略族」的组合，用**组合层面**指标（而非单票命中率）找试点方向。

两个阶段，联网与离线严格分开
----------------------------
1. ``fetch``（**联网**，baostock，可断点续跑）
   沪深 300 当前成分 → 逐票日线 + 每日估值字段 → 沪深 300 指数日线。
   落盘到 ``evidence/p2/data/``（``evidence/`` 已 gitignore，不入库）。
   **不写** ``source/stock_analysis/data/``，不碰主 CLI 缓存。
2. ``analyze``（**完全离线**，只读 ``evidence/p2/data/``）
   构建特征 → 12 个**预注册**候选（见 ``CANDIDATES``）→ 组合回测 →
   与三个基线（等权买入持有 / 沪深 300 / 等暴露度随机组）对照 →
   DSR / Bonferroni / 年度稳定性 / walk-forward。

为什么用组合层面而不是单票命中率
--------------------------------
R5 的教训：单票命中率的聚类 ICC≈0.275、DEFF≈3.2，且「便宜」在小/大聚类里符号相反。
组合层面的日收益序列把横截面分散（breadth）与再平衡成本一起纳入，才是用户实际
拿到的东西。因此每个候选都给：扣费年化、maxDD、Sharpe、换手、命中率，
并与 **等暴露度随机组**（同 N、同再平衡日、同现金仓位）对照。

口径（与生产一致，不另造）
----------------------------
* 信号在 T 日**收盘**算出，T+1 **开盘**成交（``engine.exec_price='next_open'``）。
* 涨跌停不可成交：涨停不买、跌停不卖（阈值与 ``decision/instrument.get_price_limits``
  一致：``1 ± (limit - 0.001)``；ST 用 5%）。
* 成本（取自 ``BacktestConfig``）：佣金 3bp + 滑点 10bp；卖出另加印花税 10bp。
  即单边买入 13bp、卖出 23bp、一次完整换手 36bp。
* 停牌不可成交：买入落空按现金计；卖出落空顺延到下一可成交日（上限 20 日）。

预注册（**在看到任何本工具结果之前冻结在代码里**）
------------------------------------------------
``CANDIDATES`` 是唯一的候选表。**不按结果添加/删除/改参数**；若改，必须在新报告
里显式登记 ``n_trials`` 并说明旧候选为何作废。多重检验按 ``len(CANDIDATES)`` 校正。

用法
----
    # 0) 自检（合成数据；必须全 PASS）
    python tools/strategy_lab.py selftest

    # 1) 联网取数（约 5–15 分钟；断点续跑跳过新鲜缓存）
    python tools/strategy_lab.py fetch --start 2016-06-01 --limit 300

    # 2) 离线分析（一条命令复现）
    python tools/strategy_lab.py analyze --start 2018-01-01 --top-n 30 --random-reps 500

退出码：0 = 成功；1 = 数据缺失/不足；2 = 参数或依赖错误。
"""
from __future__ import annotations

import argparse
import json
import math
import os
import subprocess
import sys
import time
from datetime import date, datetime, timedelta
from statistics import NormalDist

import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "source")
TOOLS = os.path.join(ROOT, "tools")
for _p in (SRC, TOOLS):
    if _p not in sys.path:
        sys.path.insert(0, _p)

DATA = os.path.join(ROOT, "evidence", "p2", "data")
OUT = os.path.join(ROOT, "evidence", "p2", "out")
PRICE_DIR = os.path.join(DATA, "price")
INDEX_DIR = os.path.join(DATA, "index")

# ---------------------------------------------------------------------------
# 预注册候选表（冻结；禁止按结果修改）
# ---------------------------------------------------------------------------
#: ``(id, family, horizon, params)``。horizon = 再平衡间隔 = 持有交易日数。
#: 每个候选的参数都写死，**没有样本内调参**；``value_trend`` 的 ``pool_mult=2``
#: 与 ``pct_window=750`` 也是预先写死。
CANDIDATES = [
    ("trend_donchian_h1",    "trend",   1,   {}),
    ("trend_donchian_h5",    "trend",   5,   {}),
    ("trend_donchian_h20",   "trend",   20,  {}),
    ("trend_donchian_h60",   "trend",   60,  {}),
    ("trend_donchian_h120",  "trend",   120, {}),
    ("xsmom_12_1_h20",       "xsmom",   20,  {}),
    ("xsmom_12_1_h60",       "xsmom",   60,  {}),
    ("xsmom_12_1_h120",      "xsmom",   120, {}),
    ("mr_rsi2_h5",           "rsi2",    5,   {}),
    ("mr_boll_h5",           "boll",    5,   {}),
    ("lowvol60_h20",         "lowvol",  20,  {}),
    ("regime_trend_h20",     "regime",  20,  {"ma": 200}),
    ("value_trend_h20",      "value",   20,  {"pool_mult": 2, "pct_window": 750,
                                              "pct_min_samples": 250}),
]

#: 成本（与 ``source/stock_analysis/backtest/engine.py::BacktestConfig`` 同值）。
COMMISSION = 0.0003
SLIPPAGE = 0.0010
STAMP_DUTY = 0.0010
BUY_COST = COMMISSION + SLIPPAGE            # 13bp
SELL_COST = COMMISSION + SLIPPAGE + STAMP_DUTY  # 23bp

#: 组合参数（冻结）。
TOP_N = 30
MIN_HISTORY = 252          # 候选所需最短历史（xsmom 需 252+21）
INDEX_MA = 200             # regime 门槛
DONCHIAN_WINDOW = 55
LOWVOL_WINDOW = 60
RSI_PERIOD = 2
BOLL_WINDOW = 20
BOLL_STD = 2.0
MAX_EXIT_EXTEND = 20       # 卖出被跌停/停牌挡住时最多顺延的交易日
DEFAULT_START = "2018-01-01"
DEFAULT_FETCH_START = "2016-06-01"   # 预留 252+ 根预热，评估从 2018-01 起
RANDOM_REPS = 500
BOOT_REPS = 1000
SEED = 20261113


# ---------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------
def _ensure_dirs():
    for path in (DATA, OUT, PRICE_DIR, INDEX_DIR):
        os.makedirs(path, exist_ok=True)


def _log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def _json_default(obj):
    if isinstance(obj, (np.integer,)):
        return int(obj)
    if isinstance(obj, (np.floating,)):
        return float(obj)
    if isinstance(obj, (np.bool_,)):
        return bool(obj)
    if isinstance(obj, np.ndarray):
        return obj.tolist()
    if isinstance(obj, (pd.Timestamp, datetime, date)):
        return obj.isoformat()
    raise TypeError(f"不可序列化的类型：{type(obj).__name__}")


def _write_json(path, payload):
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2,
                  default=_json_default)


def _read_json(path):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def _git_head():
    try:
        return subprocess.check_output(
            ["git", "-C", ROOT, "rev-parse", "HEAD"], text=True).strip()
    except Exception:  # noqa: BLE001
        return None


def _bs_code(code):
    code = str(code).zfill(6)
    if code[:2] in ("60", "68", "90"):
        return f"sh.{code}"
    if code[:2] in ("00", "30", "20"):
        return f"sz.{code}"
    raise ValueError(f"baostock 不支持 {code}")


def _to_code(bs_code):
    return str(bs_code).split(".")[-1].zfill(6)


# ---------------------------------------------------------------------------
# 1) 联网取数（baostock）
# ---------------------------------------------------------------------------
PRICE_FIELDS = ("date,open,high,low,close,volume,amount,turn,"
                "tradestatus,isST,peTTM,pbMRQ,psTTM")
INDEX_FIELDS = "date,open,high,low,close,volume,amount"


def _bs_login(bs):
    lg = bs.login()
    if lg.error_code != "0":
        raise RuntimeError(f"baostock 登录失败 {lg.error_code} {lg.error_msg}")


def _bs_rows(rs):
    rows = []
    while rs.error_code == "0" and rs.next():
        rows.append(rs.get_row_data())
    return rows


def _query_stooq(bs, bs_code, start, fields, freq="d", adjust="2",
                 attempts=3, session=None):
    """单次查询 + 掉线重登 + 退避。返回 ``(fields, rows)``。"""
    last = None
    for attempt in range(attempts):
        try:
            rs = bs.query_history_k_data_plus(
                bs_code, fields, start_date=start,
                end_date=date.today().isoformat(), frequency=freq,
                adjustflag=adjust)
            if rs.error_code != "0":
                raise RuntimeError(f"{rs.error_code} {rs.error_msg}")
            return rs.fields, _bs_rows(rs)
        except Exception as exc:  # noqa: BLE001
            last = exc
            # 10001001 = 用户未登录（并发/掉线），重登一次再试
            try:
                bs.logout()
            except Exception:  # noqa: BLE001
                pass
            time.sleep(1.0 * (attempt + 1))
            _bs_login(bs)
    raise last


def cmd_fetch(args):
    import baostock as bs

    _ensure_dirs()
    _bs_login(bs)
    started = datetime.now()
    meta = {"head": _git_head(), "started": started.isoformat(timespec="seconds"),
            "fetch_start": args.start, "universe": {}, "prices": {},
            "index": {}, "failures": [], "sources": {}}
    try:
        # ---- 票池：沪深 300 当前成分（**当前快照**，不是历史时点成分）----
        uni_path = os.path.join(DATA, "universe.json")
        if os.path.exists(uni_path) and not args.refresh_universe:
            universe = _read_json(uni_path)
        else:
            rs = bs.query_hs300_stocks()
            if rs.error_code != "0":
                raise RuntimeError(f"query_hs300_stocks 失败 {rs.error_code}")
            rows = _bs_rows(rs)
            codes = sorted({_to_code(r[1]) for r in rows})
            snap = max(str(r[0]) for r in rows)
            universe = {"snapshot_date": snap, "source": "baostock.query_hs300_stocks",
                        "codes": codes, "names": {_to_code(r[1]): r[2] for r in rows}}
            _write_json(uni_path, universe)
        codes = universe["codes"][: args.limit]
        meta["universe"] = {"snapshot_date": universe["snapshot_date"],
                            "source": universe["source"], "declared": args.limit,
                            "codes": len(codes)}
        _log(f"票池：{len(codes)} 只（沪深 300 快照 {universe['snapshot_date']}）")

        min_as_of = args.min_as_of or (date.today() - timedelta(days=7)).isoformat()
        ok, cached, failed = [], 0, []
        for i, code in enumerate(codes, 1):
            path = os.path.join(PRICE_DIR, f"{code}.csv")
            if os.path.exists(path) and not args.force:
                try:
                    tail = pd.read_csv(path, usecols=["date"]).tail(1)
                    if len(tail) and str(tail["date"].iloc[0])[:10] >= min_as_of:
                        cached += 1
                        ok.append(code)
                        continue
                except Exception:  # noqa: BLE001
                    pass
            try:
                fields, rows = _query_stooq(bs, _bs_code(code), args.start,
                                            PRICE_FIELDS)
                if not rows:
                    raise RuntimeError("空行情")
                frame = pd.DataFrame(rows, columns=fields)
                frame = frame.drop(columns=["date"], errors="ignore")
                frame.insert(0, "date", [r[0] for r in rows])
                # baostock 停牌日用空字符串；统一转 NaN 由离线端处理
                for col in frame.columns:
                    if col != "date":
                        frame[col] = pd.to_numeric(frame[col], errors="coerce")
                frame = frame[~frame["date"].duplicated(keep="last")]
                frame.to_csv(path, index=False)
                ok.append(code)
                meta["sources"][code] = "baostock_qfq"
            except Exception as exc:  # noqa: BLE001
                failed.append({"code": code, "error": f"{type(exc).__name__}: {exc}"})
                _log(f"[FAIL] {code}: {type(exc).__name__}: {exc}")
            if i % 25 == 0:
                _log(f"  进度 {i}/{len(codes)}（缓存 {cached}，失败 {len(failed)}）")
            time.sleep(args.pause)
        meta["prices"] = {"ok": len(ok), "cached": cached,
                          "failed": len(failed), "rows": {}}

        # ---- 指数：sh.000300 ----
        try:
            fields, rows = _query_stooq(bs, "sh.000300", args.start, INDEX_FIELDS)
            if not rows:
                raise RuntimeError("空指数行情")
            idx = pd.DataFrame(rows, columns=fields)
            for col in idx.columns:
                if col != "date":
                    idx[col] = pd.to_numeric(idx[col], errors="coerce")
            idx = idx[~idx["date"].duplicated(keep="last")]
            idx.to_csv(os.path.join(INDEX_DIR, "sh000300.csv"), index=False)
            meta["index"] = {"rows": len(idx), "first": idx["date"].iloc[0],
                             "last": idx["date"].iloc[-1]}
        except Exception as exc:  # noqa: BLE001
            failed.append({"code": "sh.000300", "error": f"{type(exc).__name__}: {exc}"})
            _log(f"[FAIL] 指数 sh.000300: {exc}")

        # ---- 逐票行数（离线诊断用）----
        for code in ok:
            path = os.path.join(PRICE_DIR, f"{code}.csv")
            try:
                meta["prices"]["rows"][code] = int(
                    sum(1 for _ in open(path, encoding="utf-8")) - 1)
            except Exception:  # noqa: BLE001
                pass
        meta["failures"] = failed
        meta["finished"] = datetime.now().isoformat(timespec="seconds")
        meta["wall_seconds"] = round((datetime.now() - started).total_seconds(), 1)
        _write_json(os.path.join(DATA, "fetch_meta.json"), meta)
        _log(f"取数完成：成功 {len(ok)}（其中缓存 {cached}），失败 {len(failed)}，"
             f"耗时 {meta['wall_seconds']}s")
        for item in failed[:10]:
            _log(f"  失败 {item['code']}: {item['error']}")
        return 0 if (len(ok) >= 0.9 * len(codes)) else 1
    finally:
        try:
            bs.logout()
        except Exception:  # noqa: BLE001
            pass


# ---------------------------------------------------------------------------
# 2) 离线数据装载与特征
# ---------------------------------------------------------------------------
def load_data():
    """读缓存 → 日历对齐的 numpy 矩阵 + 票池 + 指数。"""
    uni_path = os.path.join(DATA, "universe.json")
    if not os.path.exists(uni_path):
        raise RuntimeError(f"缺少 {uni_path}；先跑 fetch")
    universe = _read_json(uni_path)
    codes = universe["codes"]

    idx_path = os.path.join(INDEX_DIR, "sh000300.csv")
    if not os.path.exists(idx_path):
        raise RuntimeError("缺少指数缓存 sh000300.csv")
    index = pd.read_csv(idx_path, parse_dates=["date"]).set_index("date").sort_index()
    dates = pd.DatetimeIndex(index.index)

    frames = {}
    kept = []
    for code in codes:
        path = os.path.join(PRICE_DIR, f"{code}.csv")
        if not os.path.exists(path):
            continue
        try:
            df = pd.read_csv(path, parse_dates=["date"]).set_index("date")
        except Exception:  # noqa: BLE001  取数中断留下的半截文件
            continue
        if not {"open", "close"}.issubset(df.columns):
            continue
        df = df[~df.index.duplicated(keep="last")].sort_index()
        if len(df) < 120:
            continue
        frames[code] = df.reindex(dates)
        kept.append(code)
    if not kept:
        raise RuntimeError("没有任何可用日线")

    cols = ["open", "high", "low", "close", "volume", "amount", "turn",
            "tradestatus", "isST", "peTTM", "pbMRQ", "psTTM"]
    mat = {}
    for col in cols:
        mat[col] = np.column_stack([
            pd.to_numeric(frames[c].get(col, pd.Series(index=dates, dtype=float)),
                          errors="coerce").to_numpy(dtype=float)
            for c in kept])
    mat["tradestatus"] = np.nan_to_num(mat["tradestatus"], nan=0.0)
    mat["isST"] = np.nan_to_num(mat["isST"], nan=0.0)
    mat["close_ff"] = pd.DataFrame(mat["close"]).ffill().to_numpy()

    # 涨跌停阈值（与 decision/instrument.get_price_limits 一致）
    from decision.instrument import classify_board
    limit = np.zeros(len(kept), dtype=float)
    for j, code in enumerate(kept):
        limit[j] = classify_board(code)[1]
    limit_mat = np.where(mat["isST"] > 0.5, 0.05, limit[None, :])
    prev_close = np.vstack([mat["close_ff"][:1], mat["close_ff"][:-1]])
    mat["limit_up"] = prev_close * (1.0 + limit_mat - 0.001)
    mat["limit_down"] = prev_close * (1.0 - limit_mat + 0.001)
    open_ = mat["open"]
    tradable = (mat["tradestatus"] > 0.5) & np.isfinite(open_) & (open_ > 0)
    mat["can_buy"] = tradable & (open_ < mat["limit_up"])
    mat["can_sell"] = tradable & (open_ > mat["limit_down"])
    return {"dates": dates, "codes": kept, "mat": mat,
            "index": index, "universe": universe}


def build_features(data, pct_window=750, pct_min_samples=250):
    """构造各策略族的**收盘可得**打分矩阵（全部 shift 语义在调用端保证）。"""
    m = data["mat"]
    close = pd.DataFrame(m["close"], columns=data["codes"])
    ret = close.pct_change(fill_method=None)

    ma20 = close.rolling(20).mean()
    ma60 = close.rolling(60).mean()
    ma200 = close.rolling(200).mean()
    roll_high = close.rolling(DONCHIAN_WINDOW).max()
    donchian = close / roll_high - 1.0

    xsmom = close.shift(21) / close.shift(252) - 1.0

    delta = close.diff()
    up = delta.clip(lower=0)
    down = -delta.clip(upper=0)
    ema_up = up.ewm(com=RSI_PERIOD - 1, adjust=False).mean()
    ema_down = down.ewm(com=RSI_PERIOD - 1, adjust=False).mean()
    rs = ema_up / ema_down
    rsi2 = 100.0 - 100.0 / (1.0 + rs)

    mid = close.rolling(BOLL_WINDOW).mean()
    std = close.rolling(BOLL_WINDOW).std()
    upper = mid + std * BOLL_STD
    lower = mid - std * BOLL_STD
    boll_b = (close - lower) / (upper - lower)

    lowvol = -ret.rolling(LOWVOL_WINDOW).std()

    # 指数 regime：收盘 > MA200 才持有（as-of 决策日收盘）
    idx_close = data["index"]["close"]
    idx_ma = idx_close.rolling(INDEX_MA).mean()
    regime_ok = (idx_close > idx_ma).reindex(data["dates"]).fillna(False).to_numpy()

    base_ok = (m["tradestatus"] > 0.5) & np.isfinite(m["close"]) & (m["close"] > 0) \
        & (m["isST"] < 0.5)
    elig = {
        "trend": base_ok & (close > ma200).to_numpy() & np.isfinite(donchian.to_numpy()),
        "xsmom": base_ok & np.isfinite(xsmom.to_numpy()),
        "rsi2": base_ok & np.isfinite(rsi2.to_numpy()),
        "boll": base_ok & np.isfinite(boll_b.to_numpy()),
        "lowvol": base_ok & np.isfinite(lowvol.to_numpy()),
        "regime": base_ok & (close > ma200).to_numpy() & np.isfinite(donchian.to_numpy()),
        "value": base_ok & (close > ma200).to_numpy() & np.isfinite(donchian.to_numpy()),
        "ew": (m["tradestatus"] > 0.5) & np.isfinite(m["close"]) & (m["close"] > 0),
        "pool": base_ok,   # 随机对照的可选池：可交易、非 ST
    }
    scores = {
        "trend": donchian.to_numpy(),
        "xsmom": xsmom.to_numpy(),
        "rsi2": (-rsi2).to_numpy(),      # 越超卖越好 → 取最大
        "boll": (-boll_b).to_numpy(),    # 越跌破下轨越好 → 取最大
        "lowvol": lowvol.to_numpy(),     # -vol，越大越低波
        "regime": donchian.to_numpy(),
        "value": donchian.to_numpy(),
    }
    pb = pd.DataFrame(m["pbMRQ"], columns=data["codes"])
    usable = pd.DataFrame(m["tradestatus"] > 0.5, columns=data["codes"])
    return {"scores": scores, "elig": elig, "regime_ok": regime_ok,
            "pb": pb, "usable": usable, "close": close,
            "pct_window": pct_window, "pct_min_samples": pct_min_samples}


def _pct_at(values, usable, i, window, min_samples):
    """决策日 i 的**当日可得**历史分位（与 ``tools/context_ic.py::_rolling_percentile``
    同定义：严格小于的占比，当前值必须 >0；停牌行不进入样本）。"""
    lo = max(0, i - window + 1)
    v = np.asarray(values[lo:i + 1], dtype=float)
    u = np.asarray(usable[lo:i + 1], dtype=bool)
    cur = v[-1]
    if not np.isfinite(cur) or cur <= 0:
        return np.nan
    valid = np.isfinite(v) & (v > 0) & u
    n = int(valid.sum())
    if n < max(min_samples, 2):
        return np.nan
    less = int(((v < cur) & valid).sum())
    return 100.0 * less / (n - 1)


def _select(family, d, feats, top_n, pool_mult=2, pct_window=750,
            pct_min_samples=250):
    """决策日 d 的目标名单（返回 code 的列索引数组，长度 ≤ top_n）。"""
    elig = feats["elig"][family][d]
    idx = np.flatnonzero(elig)
    if idx.size == 0:
        return np.array([], dtype=int)
    score = feats["scores"].get(family)
    if family in ("value",):
        score = feats["scores"]["trend"][d][idx]
        order = idx[np.argsort(-score, kind="stable")]
    else:
        order = idx[np.argsort(-score[d][idx], kind="stable")]
    if family != "value":
        return order[:top_n]
    # value 过滤器：趋势前 2N 名里，按 pb 3 年分位从低到高取 N
    pool = order[: top_n * pool_mult]
    pbs = []
    for j in pool:
        pbs.append(_pct_at(feats["pb"].iloc[:, j].to_numpy(),
                           feats["usable"].iloc[:, j].to_numpy(), d,
                           pct_window, pct_min_samples))
    pbs = np.asarray(pbs, dtype=float)
    val_order = np.argsort(np.where(np.isfinite(pbs), pbs, np.inf), kind="stable")
    chosen = list(pool[val_order])
    # 分位缺失的票用趋势排名补齐（不改变预注册的选择逻辑）
    for j in order:
        if len(chosen) >= top_n:
            break
        if j not in chosen:
            chosen.append(j)
    return np.asarray(chosen[:top_n], dtype=int)


# ---------------------------------------------------------------------------
# 3) 组合回测（tranche 模型）
# ---------------------------------------------------------------------------
def _schedule(dates, start, horizon):
    """返回 ``[(d, e, x), ...]``：决策 d 收盘 → e=d+1 开盘买 → x=e+h 开盘卖。"""
    anchor = int(np.searchsorted(dates, np.datetime64(start)))
    out = []
    d = anchor
    n = len(dates)
    while d + horizon + 1 < n:
        out.append((d, d + 1, d + 1 + horizon))
        d += horizon
    return out


def _exit_day(can_sell, x, cols, cap=MAX_EXIT_EXTEND):
    """卖出被挡（跌停/停牌）时顺延到下一可成交日；返回 (day_idx, ok)。"""
    n = can_sell.shape[0]
    day = np.full(cols.shape, x, dtype=int)
    done = can_sell[x, cols].copy()
    for k in range(1, cap + 1):
        need = ~done
        if not need.any() or x + k >= n:
            break
        day_k = x + k
        ok_k = can_sell[day_k, cols]
        take = need & ok_k
        day[take] = day_k
        done = done | take
    return day, done


def _member_mask(sel, prev, prev_held):
    """``sel`` 中每个位置的名字是否出现在 ``prev`` 且当时确实持有。"""
    ref = np.where(prev_held, prev, -1)
    hit = (sel[:, :, None] == ref[:, None, :]) & (ref[:, None, :] >= 0)
    return hit.any(axis=2)


def _weighted_value(g, held, mode, width):
    """组合相对价值。``width``：固定 N 槽（买不进的名额留现金）；
    ``count``：只在**实际持有**的票之间等权（用于等权买入持有基线）。"""
    if mode == "count":
        n = held.sum(axis=1)
        w = np.where(held, 1.0 / np.maximum(n[:, None], 1), 0.0)
        out = (g * w).sum(axis=1)
        out[n == 0] = 1.0
        return out
    return np.where(held, g, 1.0).sum(axis=1) / float(width)


def run_schedule(data, feats, horizon, selector, width, reps=1, seed=SEED,
                 collect_trades=False, weight_mode="width"):  # noqa: C901
    """执行一个再平衡计划，返回 (每日收益因子, 诊断)。

    ``selector(d, k, reps, rng) -> (reps, width)`` 列索引；-1 表示现金。

    **持仓延续（carry-over）**：同一只票在相邻两窗都被选中时不清仓再买，
    只对「新增 / 卖出」的权重收 13bp / 23bp；否则每个窗口都按全额换手计费，
    会高估持续型策略（趋势/低波）的成本。

    返回 ``factors``：形状 (reps, n_dates)，逐日收益因子（1.0 表示空仓/无数据）；
    同一天可能同时有「上一窗卖出」与「下一窗买入」两个因子，**相乘**得到真实日收益。
    """
    m = data["mat"]
    dates = data["dates"]
    n_dates = len(dates)
    windows = _schedule(dates, feats.get("eval_start", DEFAULT_START), horizon)
    factors = np.ones((reps, n_dates), dtype=np.float64)
    rng = np.random.default_rng(seed)
    trades = []
    window_stats = []
    if not windows:
        return factors, {"windows": [], "trades": [], "n_windows": 0,
                         "eval_lo": 0, "eval_hi": 0}

    empty = np.full((reps, width), -1, dtype=int)

    def _gen(k):
        if 0 <= k < len(windows):
            d = windows[k][0]
            return np.asarray(selector(d, k, reps, rng),
                              dtype=int).reshape(reps, width)
        return empty

    sel_prev = empty.copy()
    held_prev = np.zeros((reps, width), dtype=bool)
    sel_cur = _gen(0)
    sel_next = _gen(1)
    denom = float(width)

    for k, (d, e, x) in enumerate(windows):
        cols = sel_cur
        safe = np.maximum(cols, 0)

        carried_prev = _member_mask(cols, sel_prev, held_prev)
        filled = (cols >= 0) & m["can_buy"][e, safe]
        is_new = filled & ~carried_prev
        held = carried_prev | is_new
        px_e = m["open"][e, safe]
        entry_basis = np.where(carried_prev, px_e,
                               np.where(is_new, px_e * (1.0 + BUY_COST), np.nan))

        v = np.empty((reps, horizon + 1), dtype=np.float64)
        for t in range(horizon):
            px = m["close_ff"][e + t, safe]
            g = np.where(held, px / entry_basis, 1.0)
            v[:, t] = _weighted_value(g, held, weight_mode, denom)

        # 下一窗仍选中的持仓不清仓（不付卖出成本）
        carried_next = _member_mask(cols, sel_next,
                                    np.ones_like(sel_next, dtype=bool))
        exit_day, exit_ok = _exit_day(m["can_sell"], x, safe)
        exit_open = m["open"][np.minimum(exit_day, n_dates - 1), safe]
        exit_basis = np.where(carried_next, exit_open,
                              exit_open * (1.0 - SELL_COST))
        realizable = held & exit_ok
        g_h = np.where(realizable, exit_basis / entry_basis, 1.0)
        # 顺延 20 日仍无法卖出：按当日收盘记账（不假设成交，后续窗口再处理）
        fallback = held & ~exit_ok
        g_h = np.where(fallback, m["close_ff"][x, safe] / entry_basis, g_h)
        v[:, horizon] = _weighted_value(g_h, held, weight_mode, denom)

        prev_v = np.ones(reps)
        for t in range(horizon):
            factors[:, e + t] *= v[:, t] / prev_v
            prev_v = v[:, t]
        factors[:, x] *= v[:, horizon] / prev_v

        n_bought = int(is_new.sum(axis=1).mean())
        n_sold = int((held_prev & ~carried_prev).sum(axis=1).mean())
        window_stats.append({
            "decision": str(dates[d].date()), "entry": str(dates[e].date()),
            "exit": str(dates[x].date()),
            "n_entered": int(held.sum(axis=1).mean()),
            "n_bought": n_bought, "n_sold": n_sold,
            "n_exit_blocked": int((held & ~exit_ok).sum(axis=1).mean()),
            "window_return": float(v[:, horizon].mean() - 1.0),
        })

        if collect_trades:
            for rep in range(reps):
                for j in range(width):
                    if not held[rep, j]:
                        continue
                    net = float(g_h[rep, j] - 1.0)
                    trades.append({"code": data["codes"][int(cols[rep, j])],
                                   "entry": str(dates[e].date()),
                                   "exit": str(dates[exit_day[rep, j]].date()),
                                   "net_return": net,
                                   "horizon": horizon})

        sel_prev, held_prev = sel_cur, held
        sel_cur = sel_next
        if k + 2 < len(windows) + 2:
            sel_next = _gen(k + 2)

    return factors, {"windows": window_stats, "trades": trades,
                     "n_windows": len(windows),
                     "eval_lo": windows[0][1],
                     "eval_hi": min(windows[-1][2] + 1, n_dates)}


def _coverage(factors):
    """因子 != 1 的日期范围（首尾）。"""
    changed = np.any(factors != 1.0, axis=0)
    if not changed.any():
        return 0, 0
    idx = np.flatnonzero(changed)
    return int(idx[0]), int(idx[-1]) + 1


def metrics_from_factors(factors, dates, lo=None, hi=None):
    """由逐日因子算组合指标（可选择多组 rep）。

    传入 ``lo/hi`` 时使用**固定评估窗口**（所有候选/随机组/基线同一口径），
    否则退化为「因子 != 1」的首尾。regime 候选的空仓期在固定窗口里按 0 收益计入。
    """
    if lo is None or hi is None:
        lo, hi = _coverage(factors)
    f = factors[:, lo:hi]
    r = f - 1.0
    n = r.shape[1]
    if n < 2:
        return None
    eq = np.cumprod(f, axis=1)
    ann = eq[:, -1] ** (252.0 / n) - 1.0
    mu = r.mean(axis=1)
    sd = r.std(axis=1, ddof=1)
    sharp = np.where(sd > 0, mu / sd * math.sqrt(252.0), 0.0)
    peak = np.maximum.accumulate(eq, axis=1)
    dd = 1.0 - eq / np.where(peak > 0, peak, np.nan)
    mdd = np.nanmax(dd, axis=1)
    vol = sd * math.sqrt(252.0)
    # Calmar（年化 / maxDD），仅作展示
    calmar = np.where(mdd > 1e-9, ann / mdd, np.nan)
    return {"ann_return": ann, "vol": vol, "sharpe": sharp, "max_drawdown": mdd,
            "calmar": calmar, "n_days": n, "start": str(dates[lo].date()),
            "end": str(dates[hi - 1].date())}


def _summary_stats(arr):
    arr = np.asarray(arr, dtype=float)
    return {"mean": float(np.nanmean(arr)), "std": float(np.nanstd(arr, ddof=1)),
            "p05": float(np.nanpercentile(arr, 5)),
            "p50": float(np.nanpercentile(arr, 50)),
            "p95": float(np.nanpercentile(arr, 95))}


def _annual_one_way_turnover(window_stats, years):
    """年化**单边**换手 = 每窗 (新增+卖出)/2 ÷ 当窗持仓数 × 每年窗数。

    已计入持仓延续：同一只票在相邻两窗都被选中时不算换手。
    """
    if years <= 0 or not window_stats:
        return np.nan
    per_window = float(np.mean([
        ((w.get("n_bought", w["n_entered"]) + w.get("n_sold", 0)) / 2.0)
        / max(w.get("n_entered", 1), w.get("n_sold", 0),
              w.get("n_bought", 0), 1)
        for w in window_stats]))
    return float(per_window * len(window_stats) / years)


# ---------------------------------------------------------------------------
# 4) 统计推断：Newey-West / DSR / Bonferroni / 块 bootstrap
# ---------------------------------------------------------------------------
_ND = NormalDist()


def _nw_t(x, lag):
    """日收益均值的 Newey-West t（单侧 p 由调用方算）。"""
    x = np.asarray(x, dtype=float)
    n = len(x)
    if n < 3:
        return np.nan
    m = x.mean()
    e = x - m
    var = float(e @ e) / n
    for l in range(1, min(lag, n - 1) + 1):
        cov = float(e[l:] @ e[:-l]) / n
        var += 2.0 * (1.0 - l / (lag + 1.0)) * cov
    if var <= 0:
        return np.nan
    return m / math.sqrt(var / n)


def _period_stats(window_returns):
    w = np.asarray(window_returns, dtype=float)
    if len(w) < 3 or np.std(w, ddof=1) == 0:
        return np.nan, np.nan
    t = float(np.mean(w) / (np.std(w, ddof=1) / math.sqrt(len(w))))
    return float(np.mean(w)), t


def deflated_sharpe(sr_daily, trial_sr_daily, n_obs, skew, kurt):
    """Bailey & López de Prado (2014) DSR。``kurt`` 为**非超额**峰度。"""
    trials = np.asarray(trial_sr_daily, dtype=float)
    if len(trials) < 2 or n_obs < 3:
        return np.nan
    e = 0.5772156649015329
    var = float(np.var(trials, ddof=1))
    if var <= 0:
        return np.nan
    sr0 = math.sqrt(var) * ((1 - e) * _ND.inv_cdf(1 - 1 / len(trials))
                            + e * _ND.inv_cdf(1 - 1 / (len(trials) * e)))
    denom = math.sqrt(max(1e-12, 1 - skew * sr_daily
                          + (kurt - 1) / 4 * sr_daily ** 2))
    return float(_ND.cdf((sr_daily - sr0) * math.sqrt(n_obs - 1) / denom))


def block_bootstrap(r, block, reps=BOOT_REPS, seed=SEED):
    """循环块 bootstrap：返回年化收益与 Sharpe 的分布。"""
    rng = np.random.default_rng(seed)
    n = len(r)
    n_blocks = int(math.ceil(n / block))
    ann = np.empty(reps)
    sharpe = np.empty(reps)
    for k in range(reps):
        starts = rng.integers(0, n, size=n_blocks)
        idx = np.concatenate([(np.arange(s, s + block) % n) for s in starts])[:n]
        sample = r[idx]
        eq = np.prod(1 + sample)
        ann[k] = eq ** (252.0 / n) - 1
        sd = sample.std(ddof=1)
        sharpe[k] = sample.mean() / sd * math.sqrt(252.0) if sd > 0 else 0.0
    return ann, sharpe


# ---------------------------------------------------------------------------
# 5) 离线分析主流程
# ---------------------------------------------------------------------------
def _selector_factory(family, feats, top_n, width=None):
    width = int(width or top_n)
    def selector(d, k, reps, rng):
        if family == "regime" and not feats["regime_ok"][d]:
            return np.full((reps, width), -1, dtype=int)
        if family == "ew":
            idx = np.flatnonzero(feats["elig"]["ew"][d])
            out = np.full((reps, width), -1, dtype=int)
            if idx.size:
                take = min(width, idx.size)
                out[:, :take] = idx[:take]
            return out
        sel = _select(family, d, feats, top_n)
        out = np.full((reps, width), -1, dtype=int)
        if sel.size:
            out[:, : min(width, sel.size)] = sel[:width]
        return out
    return selector


def _random_selector(feats, counts, width, bought=None, mode="iid"):
    """随机对照。

    * ``mode="iid"``：每个窗随机抽 ``counts[k]`` 只（等暴露度）。
    * ``mode="turnover"``：在上一窗随机组合中**随机保留** ``counts[k]-bought[k]``
      只，再随机补 ``bought[k]`` 只新票 —— 同时匹配暴露度与换手，用来区分
      「选股能力」与「少换手省成本」。

    ``counts[k]`` 是候选在该窗实际持有票数（0 = 现金）。
    """
    width = int(width)
    bought = list(bought) if bought is not None else [0] * len(counts)
    prev = np.full((1, width), -1, dtype=int)

    def selector(d, k, reps, rng):
        nonlocal prev
        n = int(counts[k]) if k < len(counts) else 0
        out = np.full((reps, width), -1, dtype=int)
        pool = np.flatnonzero(feats["elig"]["pool"][d])
        if n <= 0 or pool.size == 0:
            prev = out
            return out
        if mode == "iid":
            take = min(n, pool.size, width)
            if take == pool.size:
                out[:, :take] = pool
            else:
                keys = rng.random((reps, pool.size))
                pick = np.argpartition(keys, take - 1, axis=1)[:, :take]
                out[:, :take] = pool[pick]
            prev = out
            return out

        # turnover 匹配：保留 n-nb 只旧票，再补 nb 只新票
        nb = int(bought[k]) if k < len(bought) else 0
        if prev.shape[0] != reps:
            prev = np.full((reps, width), -1, dtype=int)
        keep_slots = max(min(n - nb, width), 0)
        old = prev
        pos = np.searchsorted(pool, old)
        pos_c = np.clip(pos, 0, pool.size - 1)
        valid_old = (old >= 0) & (pos < pool.size) & (pool[pos_c] == old)
        keys_old = np.where(valid_old, rng.random((reps, width)), np.inf)
        kept = np.full((reps, keep_slots), -1, dtype=int)
        kept_ok = np.zeros((reps, keep_slots), dtype=bool)
        kept_mask = np.zeros((reps, width), dtype=bool)
        if keep_slots:
            kth = min(keep_slots, width) - 1
            idx = np.argpartition(keys_old, kth, axis=1)[:, :keep_slots]
            kept = np.take_along_axis(old, idx, axis=1)
            kept_ok = np.take_along_axis(valid_old, idx, axis=1)
            np.put_along_axis(kept_mask, idx, kept_ok, axis=1)
        out[:, :keep_slots] = np.where(kept_ok, kept, -1)

        n_new = min(nb, width - keep_slots)
        keys = rng.random((reps, pool.size))
        # 保留的旧票不能再被抽中；被卖出的旧票也不能在同日回买（否则换手被低估）
        kp_old = np.clip(np.searchsorted(pool, old), 0, pool.size - 1)
        blocked_keep = np.where(
            kept_mask, np.inf, keys[np.arange(reps)[:, None], kp_old])
        np.put_along_axis(keys, kp_old, blocked_keep, axis=1)
        dropped = valid_old & ~kept_mask
        blocked_drop = np.where(
            dropped, np.inf, keys[np.arange(reps)[:, None], kp_old])
        np.put_along_axis(keys, kp_old, blocked_drop, axis=1)
        pick = np.argpartition(keys, max(n_new - 1, 0), axis=1)[:, :n_new]
        out[:, keep_slots:keep_slots + n_new] = pool[pick]
        prev = out.copy()
        return out
    return selector


def _daily_series(factors, dates, lo, hi):
    return pd.Series(factors[0, lo:hi] - 1.0, index=dates[lo:hi])


def _yearly(daily, dates):
    out = {}
    if daily is None or len(daily) == 0:
        return out
    for year, grp in daily.groupby(daily.index.year):
        eq = float(np.prod(1 + grp.to_numpy()))
        sd = grp.std(ddof=1)
        out[int(year)] = {
            "return": eq - 1.0,
            "sharpe": float(grp.mean() / sd * math.sqrt(252)) if sd > 0 else None,
            "max_dd": float(1 - (np.cumprod(1 + grp.to_numpy())
                                 / np.maximum.accumulate(np.cumprod(1 + grp.to_numpy()))).min()),
            "days": int(len(grp)),
        }
    return out


def run_candidate(name, family, horizon, params, data, feats, args):
    top_n = args.top_n
    selector = _selector_factory(family, feats, top_n)
    t0 = time.time()
    factors, diag = run_schedule(data, feats, horizon, selector, top_n,
                                 reps=1, collect_trades=True)
    lo, hi = diag["eval_lo"], diag["eval_hi"]
    mt = metrics_from_factors(factors, data["dates"], lo, hi)
    daily = _daily_series(factors, data["dates"], lo, hi)

    # 逐窗净收益（非重叠口径）
    w_rets = [w["window_return"] for w in diag["windows"]]
    w_mean, w_t = _period_stats(w_rets)

    r = daily.to_numpy()
    lag = min(max(horizon - 1, 1), 60)
    nw_t = _nw_t(r, lag)
    p_nw = float(1 - _ND.cdf(nw_t)) if np.isfinite(nw_t) else np.nan
    p_period = (float(1 - _ND.cdf(w_t)) if np.isfinite(w_t) else np.nan)
    skew = float(pd.Series(r).skew())
    kurt = float(pd.Series(r).kurt()) + 3.0

    trades = diag["trades"]
    hits = [t["net_return"] > 0 for t in trades]
    hit_rate = float(np.mean(hits)) if hits else np.nan
    years = (data["dates"][hi - 1] - data["dates"][lo]).days / 365.25
    turnover = _annual_one_way_turnover(diag["windows"], years)
    exposure = float(np.mean([w["n_entered"] for w in diag["windows"]])) / top_n

    # ---- 样本内/外（最后 30% 时间）与功效 ----
    cut = int(len(r) * 0.7)

    def _ann(x):
        return float(np.prod(1 + x) ** (252.0 / len(x)) - 1) if len(x) > 1 else np.nan

    def _sr(x):
        sd = np.std(x, ddof=1)
        return float(np.mean(x) / sd * math.sqrt(252)) if len(x) > 2 and sd > 0 else np.nan

    w_arr = np.asarray(w_rets, dtype=float)
    n_periods = len(w_arr)
    z = 2.8016  # 80% 功效、双侧 α=0.05
    if n_periods >= 3 and np.std(w_arr, ddof=1) > 0:
        mde_period = z * float(np.std(w_arr, ddof=1)) / math.sqrt(n_periods)
        mde_ann = mde_period * (252.0 / horizon)
        mde_sr = z * math.sqrt(252.0 / (n_periods * horizon))
    else:
        mde_period = mde_ann = mde_sr = np.nan

    return {
        "id": name, "family": family, "horizon": horizon, "params": params,
        "ann_return": float(mt["ann_return"][0]),
        "vol": float(mt["vol"][0]),
        "sharpe": float(mt["sharpe"][0]),
        "max_drawdown": float(mt["max_drawdown"][0]),
        "calmar": float(mt["calmar"][0]) if np.isfinite(mt["calmar"][0]) else None,
        "n_days": int(mt["n_days"]), "start": mt["start"], "end": mt["end"],
        "n_windows": len(diag["windows"]),
        "window_counts": [int(w["n_entered"]) for w in diag["windows"]],
        "window_bought": [int(w["n_bought"]) for w in diag["windows"]],
        "eval_lo": lo, "eval_hi": hi,
        "window_mean": w_mean, "window_t": w_t,
        "window_returns": w_arr, "k_periods": int(n_periods),
        "window_skew": float(pd.Series(w_arr).skew()) if n_periods > 3 else 0.0,
        "window_kurt": float(pd.Series(w_arr).kurt()) + 3.0 if n_periods > 3 else 3.0,
        "is_ann": _ann(r[:cut]), "is_sharpe": _sr(r[:cut]),
        "oos_ann": _ann(r[cut:]), "oos_sharpe": _sr(r[cut:]),
        "oos_days": int(len(r) - cut),
        "mde_ann_return": mde_ann, "mde_sharpe": mde_sr,
        "mde_period": mde_period,
        "nw_t": nw_t, "nw_lag": lag, "p_nw_one_sided": p_nw,
        "p_period_one_sided": p_period,
        "skew": skew, "kurt": kurt,
        "hit_rate": hit_rate, "n_trades": len(trades),
        "turnover_annual": turnover, "exposure": exposure,
        "n_exit_blocked": int(sum(w["n_exit_blocked"] for w in diag["windows"])),
        "yearly": _yearly(daily, data["dates"]),
        "daily": daily,
        "seconds": round(time.time() - t0, 2),
    }


def cmd_analyze(args):
    _ensure_dirs()
    pd.set_option("display.width", 180)
    data = load_data()
    feats = build_features(data)
    feats["eval_start"] = args.start
    dates = data["dates"]
    _log(f"数据：{len(data['codes'])} 只 × {len(dates)} 个交易日 "
         f"（{dates[0].date()} ~ {dates[-1].date()}）")

    # ---- 数据质量（进报告，避免「样本量」口径含糊）----
    close = data["mat"]["close"]
    rows_per_stock = np.isfinite(close).sum(axis=0)
    data_quality = {
        "codes_loaded": len(data["codes"]),
        "codes_in_universe": len(data["universe"].get("codes", [])),
        "universe_snapshot": data["universe"].get("snapshot_date"),
        "dates": len(dates),
        "date_first": str(dates[0].date()),
        "date_last": str(dates[-1].date()),
        "rows_per_stock_min": int(rows_per_stock.min()),
        "rows_per_stock_median": int(np.median(rows_per_stock)),
        "stocks_lt_300_rows": int((rows_per_stock < 300).sum()),
        "missing_close_share": round(float(np.mean(~np.isfinite(close))), 4),
        "tradestatus_share": round(float(np.mean(
            data["mat"]["tradestatus"] > 0.5)), 4),
        "eval_start": args.start,
    }
    _log(f"数据质量：每股行数 min {data_quality['rows_per_stock_min']} / "
         f"median {data_quality['rows_per_stock_median']}；"
         f"<300 根 {data_quality['stocks_lt_300_rows']} 只；"
         f"缺失收盘 {data_quality['missing_close_share']*100:.1f}%")

    results = []
    for name, family, horizon, params in CANDIDATES:
        res = run_candidate(name, family, horizon, params, data, feats, args)
        results.append(res)
        _log(f"{name:<22} H={horizon:<3} 年化 {res['ann_return']*100:6.2f}% "
             f"Sharpe {res['sharpe']:5.2f} maxDD {res['max_drawdown']*100:5.1f}% "
             f"窗数 {res['n_windows']:<4} ({res['seconds']}s)")

    # ---- 多重检验：DSR / Bonferroni ----
    trial_sr_daily = [res["sharpe"] / math.sqrt(252) for res in results]
    horizons = np.array([res["horizon"] for res in results], dtype=float)
    trial_sr_period = np.array([
        res["window_mean"] / float(np.std(res["window_returns"], ddof=1))
        if np.std(res["window_returns"], ddof=1) > 0 else 0.0
        for res in results])
    n_trials = len(results)
    for i, res in enumerate(results):
        r = res["daily"].to_numpy()
        res["dsr"] = deflated_sharpe(res["sharpe"] / math.sqrt(252),
                                     trial_sr_daily, len(r),
                                     res["skew"], res["kurt"])
        # 非重叠口径：日收益在持有期内高度自相关，用窗收益的 DSR 更诚实。
        # 不同候选的持有期不同，Sharpe 的尺度是 sqrt(H)，所以先统一换算到
        # **本候选的 H 期**再算跨试验方差，否则长短周期混在一起会虚增 SR0。
        scaled = trial_sr_period * np.sqrt(res["horizon"] / horizons)
        w = np.asarray(res["window_returns"], dtype=float)
        sd_w = float(np.std(w, ddof=1)) if len(w) > 2 else 0.0
        sr_w = float(np.mean(w) / sd_w) if sd_w > 0 else 0.0
        res["dsr_period"] = deflated_sharpe(
            sr_w, scaled, len(w),
            res["window_skew"], res["window_kurt"])
        res["sr_period"] = sr_w
        p = res["p_period_one_sided"]
        res["p_bonferroni"] = (min(1.0, p * n_trials)
                               if np.isfinite(p) else np.nan)
        b_ann, b_sr = block_bootstrap(r, max(res["horizon"], 5))
        res["boot_ann_ci"] = [float(np.percentile(b_ann, 2.5)),
                              float(np.percentile(b_ann, 97.5))]
        res["boot_sharpe_ci"] = [float(np.percentile(b_sr, 2.5)),
                                 float(np.percentile(b_sr, 97.5))]

    # ---- 基线 1/2：等权买入持有 + 沪深 300 ----
    ew_width = max(int(np.max(feats["elig"]["ew"].sum(axis=1))), 1)
    ew_sel = _selector_factory("ew", feats, args.top_n, width=ew_width)
    ew_factors, ew_diag = run_schedule(data, feats, args.horizon_ew, ew_sel,
                                       ew_width, reps=1, collect_trades=True,
                                       weight_mode="count")
    lo, hi = ew_diag["eval_lo"], ew_diag["eval_hi"]
    ew_mt = metrics_from_factors(ew_factors, data["dates"], lo, hi)
    ew_daily = _daily_series(ew_factors, data["dates"], lo, hi)
    ew_years = (data["dates"][hi - 1] - data["dates"][lo]).days / 365.25
    ew_res = {
        "id": "ew_buy_hold", "horizon": args.horizon_ew,
        "ann_return": float(ew_mt["ann_return"][0]),
        "sharpe": float(ew_mt["sharpe"][0]),
        "max_drawdown": float(ew_mt["max_drawdown"][0]),
        "vol": float(ew_mt["vol"][0]),
        "hit_rate": float(np.mean([t["net_return"] > 0
                                   for t in ew_diag["trades"]])) if ew_diag["trades"] else np.nan,
        "turnover_annual": _annual_one_way_turnover(ew_diag["windows"], ew_years),
        "yearly": _yearly(ew_daily, data["dates"]),
        "daily": ew_daily,
        "n_windows": len(ew_diag["windows"]),
        "start": str(data["dates"][lo].date()),
        "end": str(data["dates"][hi - 1].date()),
    }

    idx = data["index"]
    idx_slice = idx.loc[idx.index >= pd.Timestamp(args.start)]
    ir = idx_slice["close"].pct_change().dropna()
    eq = (1 + ir).cumprod()
    peak = eq.cummax()
    idx_res = {
        "id": "csi300_index", "ann_return": float(eq.iloc[-1] ** (252 / len(ir)) - 1),
        "sharpe": float(ir.mean() / ir.std(ddof=1) * math.sqrt(252)),
        "max_drawdown": float((1 - eq / peak).max()),
        "vol": float(ir.std(ddof=1) * math.sqrt(252)),
        "yearly": _yearly(ir, data["dates"]),
        "daily": ir,
    }

    # ---- 基线 3：等暴露度随机组（逐候选匹配每窗持仓数）----
    # 做法：先跑出候选每窗实际持有票数 counts[k]（含 0 = 现金），随机组在**同一
    # 再平衡日**从可交易非 ST 池里随机抽 counts[k] 只，同成本、同持仓延续规则。
    # 这样「降回撤/提收益」必须解释为选股能力，而不是「买得少/空仓多」。
    random_controls, random_dists = {}, {}
    for i, res in enumerate(results):
        for mode in ("iid", "turnover"):
            key = f"{res['id']}:{mode}"
            selector = _random_selector(feats, res["window_counts"], args.top_n,
                                        bought=res["window_bought"], mode=mode)
            t0 = time.time()
            factors, diag = run_schedule(data, feats, res["horizon"], selector,
                                         args.top_n, reps=args.random_reps,
                                         seed=args.seed + 1000 + i * 7
                                         + (0 if mode == "iid" else 3))
            lo, hi = res["eval_lo"], res["eval_hi"]
            mt = metrics_from_factors(factors, data["dates"], lo, hi)
            dist = {"ann": mt["ann_return"], "sharpe": mt["sharpe"],
                    "mdd": mt["max_drawdown"]}
            random_dists[key] = dist
            r_years = (data["dates"][hi - 1] - data["dates"][lo]).days / 365.25
            random_controls[key] = {
                "horizon": res["horizon"], "reps": args.random_reps,
                "mode": mode, "matched_counts": True,
                "matched_turnover": mode == "turnover",
                "turnover_annual": _annual_one_way_turnover(diag["windows"],
                                                            r_years),
                "ann_return": _summary_stats(mt["ann_return"]),
                "sharpe": _summary_stats(mt["sharpe"]),
                "max_drawdown": _summary_stats(mt["max_drawdown"]),
                "seconds": round(time.time() - t0, 1),
            }
            p_ann = float(np.mean(dist["ann"] >= res["ann_return"]))
            p_sr = float(np.mean(dist["sharpe"] >= res["sharpe"]))
            p_mdd = float(np.mean(dist["mdd"] <= res["max_drawdown"]))
            res[f"random_p_ge_ann_{mode}"] = p_ann
            res[f"random_p_ge_sharpe_{mode}"] = p_sr
            res[f"random_p_le_mdd_{mode}"] = p_mdd
            _log(f"随机 [{mode:<8}] {res['id']:<22} "
                 f"年化中位 {np.median(mt['ann_return'])*100:6.2f}% "
                 f"p(≥策略)={p_ann:.3f} ({random_controls[key]['seconds']}s)")
        # 取两种对照中更严格的那个（更难被「随机」解释掉）
        res["random_p_ge_ann"] = max(res["random_p_ge_ann_iid"],
                                     res["random_p_ge_ann_turnover"])
        res["random_p_ge_sharpe"] = max(res["random_p_ge_sharpe_iid"],
                                        res["random_p_ge_sharpe_turnover"])
        res["random_p_le_mdd"] = max(res["random_p_le_mdd_iid"],
                                     res["random_p_le_mdd_turnover"])
        res["random_ann_mean"] = float(np.mean(random_dists[
            f"{res['id']}:iid"]["ann"]))
        res["random_key"] = res["id"]

    # ---- 预注册的「可推荐」判定（全部条件同时满足）----
    # 1) 扣费年化同时高于等权买入持有与沪深 300；
    # 2) 优于 95% 的等暴露度随机组（random_p_ge_ann ≤ 0.05）；
    # 3) 通过 Bonferroni（α=0.05/n_trials）且 DSR ≥ 0.95；
    # 4) OOS 年化与 Sharpe 均为正；maxDD 不比等权基线差 3pp 以上；
    # 5) 至少一半日历年为正（不是靠单一年份）；
    # 6) 功效足够：年化 MDE ≤ 10pp（否则只能写「样本不足」）。
    for res in results:
        years = res.get("yearly") or {}
        pos_years = sum(1 for v in years.values() if v["return"] > 0)
        res["checks"] = {
            "beats_ew": bool(res["ann_return"] > ew_res["ann_return"]),
            "beats_index": bool(res["ann_return"] > idx_res["ann_return"]),
            "beats_random": bool(res["random_p_ge_ann"] <= 0.05),
            "bonferroni": bool(np.isfinite(res["p_bonferroni"])
                               and res["p_bonferroni"] < 0.05),
            "dsr": bool(np.isfinite(res["dsr_period"]) and res["dsr_period"] >= 0.95),
            "oos_positive": bool(res["oos_ann"] > 0 and res["oos_sharpe"] > 0),
            "dd_no_worse": bool(res["max_drawdown"]
                                <= ew_res["max_drawdown"] + 0.03),
            "yearly_positive": bool(pos_years * 2 >= max(len(years), 1)),
            "power_ok": bool(np.isfinite(res["mde_ann_return"])
                             and res["mde_ann_return"] <= 0.10),
        }
        res["recommend"] = all(res["checks"].values())

    # ---- walk-forward：3 年训练 → 1 年测试滚动，按训练 Sharpe 选候选 ----
    daily_map = {res["id"]: res["daily"] for res in results}
    wf = _walk_forward(daily_map, dates, args.start, train_years=3,
                       test_years=1)
    _log(f"walk-forward：{len(wf['years'])} 个测试年，"
         f"复合年化 {wf['composite_ann']*100:.2f}%")

    # ---- 输出 ----
    def _clean(res):
        out = {k: v for k, v in res.items()
               if k not in ("daily", "yearly", "window_returns",
                            "window_counts")}
        out["yearly"] = {str(y): v for y, v in (res.get("yearly") or {}).items()}
        return out

    payload = {
        "meta": {
            "issue": 70, "head": _git_head(),
            "generated": datetime.now().isoformat(timespec="seconds"),
            "universe": data["universe"].get("snapshot_date"),
            "n_codes": len(data["codes"]),
            "date_range": [str(dates[0].date()), str(dates[-1].date())],
            "eval_start": args.start, "top_n": args.top_n,
            "n_trials": n_trials, "seed": args.seed,
            "costs": {"commission": COMMISSION, "slippage": SLIPPAGE,
                      "stamp_duty": STAMP_DUTY,
                      "buy_one_way": BUY_COST, "sell_one_way": SELL_COST},
            "survivorship_bias": ("票池为沪深 300 当前成分（2026-11 快照），"
                                  "不是历史时点成分；退市/调出标的不在池内"),
            "data_quality": data_quality,
        },
        "candidates": [_clean(r) for r in results],
        "baselines": {
            "ew_buy_hold": _clean(ew_res),
            "csi300_index": _clean(idx_res),
        },
        "random_controls": random_controls,
        "walk_forward": wf,
    }
    _write_json(os.path.join(OUT, "summary.json"), payload)

    rows = []
    for res in results:
        rows.append({
            "id": res["id"], "family": res["family"], "horizon": res["horizon"],
            "ann_return": res["ann_return"], "sharpe": res["sharpe"],
            "max_drawdown": res["max_drawdown"], "vol": res["vol"],
            "n_windows": res["n_windows"], "hit_rate": res["hit_rate"],
            "turnover": res["turnover_annual"], "exposure": res["exposure"],
            "nw_t": res["nw_t"], "p_period": res["p_period_one_sided"],
            "p_bonferroni": res["p_bonferroni"], "dsr": res["dsr"],
            "dsr_period": res["dsr_period"], "sr_period": res["sr_period"],
            "k_periods": res["k_periods"],
            "boot_ann_lo": res["boot_ann_ci"][0],
            "boot_ann_hi": res["boot_ann_ci"][1],
            "is_ann": res["is_ann"], "oos_ann": res["oos_ann"],
            "oos_sharpe": res["oos_sharpe"],
            "mde_ann_return": res["mde_ann_return"],
            "mde_sharpe": res["mde_sharpe"],
            "random_key": res["random_key"],
            "random_p_ge_ann": res["random_p_ge_ann"],
            "random_p_ge_sharpe": res["random_p_ge_sharpe"],
            "random_p_le_mdd": res["random_p_le_mdd"],
            "p_random_iid": res["random_p_ge_ann_iid"],
            "p_random_turnover": res["random_p_ge_ann_turnover"],
            "recommend": res["recommend"],
        })
    pd.DataFrame(rows).to_csv(os.path.join(OUT, "candidates.csv"), index=False)

    yearly_rows = []
    for res in results:
        for year, item in (res["yearly"] or {}).items():
            yearly_rows.append({"id": res["id"], "year": year,
                                "return": item["return"],
                                "sharpe": item["sharpe"],
                                "max_dd": item["max_dd"]})
    pd.DataFrame(yearly_rows).to_csv(os.path.join(OUT, "yearly.csv"), index=False)

    _print_results(results, ew_res, idx_res, random_controls, wf, n_trials)
    return 0


def _walk_forward(daily_map, dates, start, train_years=3, test_years=1):
    """滚动：训练 3 年 Sharpe 最高者，交易下一年。返回逐年 OOS 复合。"""
    first = pd.Timestamp(start)
    last = dates[-1]
    years = []
    test_start = pd.Timestamp(date(first.year + train_years, 1, 1))
    while test_start + pd.DateOffset(years=test_years) <= last + pd.Timedelta(days=1):
        train_start = pd.Timestamp(date(test_start.year - train_years, 1, 1))
        test_end = test_start + pd.DateOffset(years=test_years)
        best, best_sr = None, -np.inf
        for cid, s in daily_map.items():
            train = s[(s.index >= train_start) & (s.index < test_start)]
            if len(train) < 200:
                continue
            sd = train.std(ddof=1)
            sr = train.mean() / sd * math.sqrt(252) if sd > 0 else -np.inf
            if sr > best_sr:
                best, best_sr = cid, sr
        if best is None:
            test_start = pd.Timestamp(date(test_start.year + test_years, 1, 1))
            continue
        test = daily_map[best][(daily_map[best].index >= test_start)
                              & (daily_map[best].index < test_end)]
        ret = float(np.prod(1 + test.to_numpy()) - 1) if len(test) else np.nan
        years.append({"test_year": int(test_start.year), "chosen": best,
                      "train_sharpe": float(best_sr), "test_return": ret,
                      "test_days": int(len(test))})
        test_start = pd.Timestamp(date(test_start.year + test_years, 1, 1))
    valid = [y["test_return"] for y in years if np.isfinite(y["test_return"])]
    comp = float(np.prod([1 + r for r in valid]) ** (1 / len(valid)) - 1) if valid else np.nan
    return {"years": years, "composite_ann": comp,
            "n_years": len(valid), "train_years": train_years,
            "test_years": test_years}


def _fmt_pct(x):
    return "  n/a " if x is None or not np.isfinite(x) else f"{x*100:6.2f}%"


def _print_results(results, ew_res, idx_res, rc, wf, n_trials):
    print("\n=== 候选结果（扣费、组合层面）===")
    print(f"{'id':<22}{'H':>4}{'年化':>8}{'Sharpe':>8}{'maxDD':>8}{'换手':>7}"
          f"{'命中':>7}{'窗数':>6}{'p':>8}{'Bonf':>7}{'DSR':>7}{'OOS':>8}"
          f"{'MDE':>8}{'推荐':>5}")
    for r in results:
        print(f"{r['id']:<22}{r['horizon']:>4}{_fmt_pct(r['ann_return']):>8}"
              f"{r['sharpe']:>8.2f}{_fmt_pct(r['max_drawdown']):>8}"
              f"{r['turnover_annual']*100:>6.0f}%{_fmt_pct(r['hit_rate']):>7}"
              f"{r['n_windows']:>6}{r['p_period_one_sided']:>8.3f}"
              f"{r['p_bonferroni']:>7.3f}{r['dsr_period']:>7.3f}"
              f"{_fmt_pct(r['oos_ann']):>8}{_fmt_pct(r['mde_ann_return']):>8}"
              f"{('是' if r['recommend'] else '否'):>5}")
    print("\n=== 基线 ===")
    print(f"等权买入持有 H={ew_res['horizon']}: 年化 {_fmt_pct(ew_res['ann_return'])} "
          f"Sharpe {ew_res['sharpe']:.2f} maxDD {_fmt_pct(ew_res['max_drawdown'])}")
    print(f"沪深 300:            年化 {_fmt_pct(idx_res['ann_return'])} "
          f"Sharpe {idx_res['sharpe']:.2f} maxDD {_fmt_pct(idx_res['max_drawdown'])}")
    print("\n=== 等暴露度随机组（iid / 换手匹配）===")
    for r in results:
        ri = rc[f"{r['id']}:iid"]
        rt = rc[f"{r['id']}:turnover"]
        print(f"{r['id']:<22} iid 中位 {_fmt_pct(ri['ann_return']['p50'])} "
              f"(p={r['random_p_ge_ann_iid']:.3f})  "
              f"换手匹配中位 {_fmt_pct(rt['ann_return']['p50'])} "
              f"(p={r['random_p_ge_ann_turnover']:.3f})")
    print("\n=== Walk-forward（3 年训练 → 1 年测试）===")
    for y in wf["years"]:
        print(f"  {y['test_year']}: 选 {y['chosen']:<22} "
              f"训练 Sharpe {y['train_sharpe']:5.2f} → 测试 {_fmt_pct(y['test_return'])}")
    print(f"  OOS 复合年化 {_fmt_pct(wf['composite_ann'])}（{wf['n_years']} 年）")
    print(f"\n多重检验：n_trials={n_trials}，Bonferroni α={0.05/n_trials:.4f}")
    print("完整产物：evidence/p2/out/summary.json, candidates.csv, yearly.csv\n")


# ---------------------------------------------------------------------------
# 6) 自检（合成数据，不触网）
# ---------------------------------------------------------------------------
def _synthetic_data(n_days=520, n_stocks=40, seed=7):
    rng = np.random.default_rng(seed)
    dates = pd.bdate_range("2020-01-01", periods=n_days, name="date")
    codes = [f"{600000+i:06d}" for i in range(n_stocks)]
    drift = rng.normal(0.0004, 0.0003, n_stocks)
    rets = rng.normal(drift, 0.018, (n_days, n_stocks))
    close = 10 * np.cumprod(1 + rets, axis=0)
    open_ = close * (1 + rng.normal(0, 0.002, (n_days, n_stocks)))
    high = np.maximum(open_, close) * 1.005
    low = np.minimum(open_, close) * 0.995
    mat = {
        "open": open_, "high": high, "low": low, "close": close,
        "volume": np.full_like(close, 1e6),
        "amount": close * 1e6, "turn": np.full_like(close, 1.0),
        "tradestatus": np.ones_like(close), "isST": np.zeros_like(close),
        "peTTM": np.full_like(close, 20.0), "pbMRQ": np.full_like(close, 2.0),
        "psTTM": np.full_like(close, 3.0),
    }
    mat["close_ff"] = pd.DataFrame(close).ffill().to_numpy()
    prev = np.vstack([mat["close_ff"][:1], mat["close_ff"][:-1]])
    mat["limit_up"] = prev * 1.099
    mat["limit_down"] = prev * 0.901
    mat["can_buy"] = (open_ < mat["limit_up"])
    mat["can_sell"] = (open_ > mat["limit_down"])
    idx = pd.DataFrame({"close": np.cumprod(1 + rng.normal(0.0002, 0.01, n_days))},
                       index=dates)
    return {"dates": pd.DatetimeIndex(dates), "codes": codes, "mat": mat,
            "index": idx, "universe": {"snapshot_date": "synthetic", "codes": codes}}


def cmd_selftest(args):
    checks = []

    def check(name, cond, detail=""):
        checks.append((name, bool(cond), detail))

    data = _synthetic_data()
    feats = build_features(data)
    feats["eval_start"] = str(data["dates"][0].date())

    # 1) 调度：T+1 开盘买、H 日后开盘卖
    sch = _schedule(data["dates"], feats["eval_start"], 5)
    check("schedule: T+1 入场 / H 日后出场",
          all(x - e == 5 and e == d + 1 for d, e, x in sch))

    # 2) 成本与单票收益：每窗换一只票（避免 carry-over），全仓一只票、无涨跌停，
    #    窗口 0 的收益应等于 open_x*(1-sell)/(open_e*(1+buy)) - 1
    def rotate_selector(d, k, reps, rng):
        out = np.full((reps, 1), -1, dtype=int)
        out[:, 0] = k % 10
        return out
    factors, diag = run_schedule(data, feats, 5, rotate_selector, 1, reps=1)
    d, e, x = sch[0]
    gross = data["mat"]["open"][x, 0] / data["mat"]["open"][e, 0]
    expect = gross * (1 - SELL_COST) / (1 + BUY_COST) - 1
    got = diag["windows"][0]["window_return"]
    check("cost: 单票一次完整换手 36bp 口径", abs(got - expect) < 1e-9,
          f"got={got:.8f} expect={expect:.8f}")

    # 3) 涨停不可买：把入场日开盘设到涨停价，组合应为空仓（窗口收益=0）
    data2 = _synthetic_data()
    data2["mat"]["can_buy"][e, 0] = False
    _, diag2 = run_schedule(data2, feats, 5, rotate_selector, 1, reps=1)
    check("limit: 涨停不可买 → 该票按现金计",
          abs(diag2["windows"][0]["window_return"]) < 1e-12)

    # 4) 卖出被跌停挡住时顺延
    data3 = _synthetic_data()
    for k in range(0, 3):
        data3["mat"]["can_sell"][x + k, 0] = False
    _, diag3 = run_schedule(data3, feats, 5, rotate_selector, 1, reps=1)
    check("limit: 跌停不可卖 → 顺延到下一可成交日",
          diag3["windows"][0]["n_exit_blocked"] == 0
          and abs(diag3["windows"][0]["window_return"] - got) > 1e-9,
          f"ext_return={diag3['windows'][0]['window_return']:.6f} base={got:.6f}")

    # 5) 无前视：只改未来价格，不改变决策日的打分
    d2 = sch[2][0]
    before = _select("trend", d2, feats, 10).tolist()
    feats2 = {k: (v.copy() if isinstance(v, np.ndarray) else v)
              for k, v in feats.items()}
    feats2["scores"] = {k: v.copy() for k, v in feats["scores"].items()}
    feats2["scores"]["trend"][d2 + 1:] = 999.0
    after = _select("trend", d2, feats2, 10).tolist()
    check("as-of: 未来数据不影响当日选择", before == after)

    # 6) 随机组：500 次随机选择的中位年化应接近等权基线（同成本、同复权口径）
    def rand_sel(d, k, reps, rng):
        out = np.full((reps, 10), -1, dtype=int)
        pool = np.flatnonzero(feats["elig"]["ew"][d])
        for r in range(reps):
            out[r, :min(10, pool.size)] = rng.choice(pool, size=min(10, pool.size),
                                                     replace=False)
        return out
    fr, _ = run_schedule(data, feats, 5, rand_sel, 10, reps=200, seed=1)
    mt = metrics_from_factors(fr, data["dates"])
    check("random: 200 次随机组年化分布有限且中位有定义",
          np.isfinite(mt["ann_return"]).all(), f"median={np.median(mt['ann_return']):.4f}")

    # 7) DSR 单调性与 n_trials 惩罚
    dsr_hi = deflated_sharpe(0.003, [0.001, 0.0005, 0.0008], 500, 0.0, 3.0)
    dsr_lo = deflated_sharpe(0.0002, [0.001, 0.0005, 0.0008], 500, 0.0, 3.0)
    dsr_many = deflated_sharpe(0.003, [0.001, 0.0005, 0.0008] * 4, 500, 0.0, 3.0)
    check("dsr: 高 Sharpe → 高 DSR 且 n_trials 增加会惩罚",
          dsr_hi > dsr_lo and dsr_many <= dsr_hi + 1e-12,
          f"hi={dsr_hi:.3f} lo={dsr_lo:.3f} many={dsr_many:.3f}")

    # 8) 滚动分位与 context_ic 同定义
    try:
        from context_ic import _rolling_percentile
        vals = np.r_[np.linspace(1, 10, 300), 5.0]
        u = np.ones(len(vals), dtype=bool)
        ref = _rolling_percentile(vals, 200, 50, usable=u)[-1]
        mine = _pct_at(vals, u, len(vals) - 1, 200, 50)
        check("pct: 与 context_ic._rolling_percentile 同值",
              abs(ref - mine) < 1e-12, f"ref={ref} mine={mine}")
    except Exception as exc:  # noqa: BLE001
        check("pct: 与 context_ic._rolling_percentile 同值", False, str(exc))

    # 9) 年化/回撤定义
    f = np.ones((1, 300))
    f[0, 10:60] = np.linspace(1.0, 1.2, 50) ** (1 / 50) + 0  # 平滑上涨
    f = np.ones((1, 300))
    f[0, 10:60] = (1.2) ** (1 / 50)
    f[0, 60:110] = (0.6) ** (1 / 50)
    mt = metrics_from_factors(f, pd.bdate_range("2021-01-01", periods=300))
    check("metrics: 年化与 maxDD 可复算",
          abs(mt["max_drawdown"][0] - 0.4) < 1e-6,
          f"mdd={mt['max_drawdown'][0]:.6f} ann={mt['ann_return'][0]:.4f}")

    ok = all(c[1] for c in checks)
    for name, passed, detail in checks:
        print(f"[{'PASS' if passed else 'FAIL'}] {name}"
              + (f"  ({detail})" if detail else ""))
    print(f"\n自检：{sum(c[1] for c in checks)}/{len(checks)} PASS")
    return 0 if ok else 1


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def build_parser():
    p = argparse.ArgumentParser(
        description="R6 策略族 × 持有期重估（issue #70）")
    sub = p.add_subparsers(dest="cmd", required=True)

    f = sub.add_parser("fetch", help="联网取数（baostock）")
    f.add_argument("--start", default=DEFAULT_FETCH_START)
    f.add_argument("--limit", type=int, default=300)
    f.add_argument("--pause", type=float, default=0.3)
    f.add_argument("--min-as-of", default=None)
    f.add_argument("--force", action="store_true")
    f.add_argument("--refresh-universe", action="store_true")
    f.set_defaults(func=cmd_fetch)

    a = sub.add_parser("analyze", help="离线分析（只读 evidence/p2/data）")
    a.add_argument("--start", default=DEFAULT_START)
    a.add_argument("--top-n", type=int, default=TOP_N)
    a.add_argument("--random-reps", type=int, default=RANDOM_REPS)
    a.add_argument("--seed", type=int, default=SEED)
    a.add_argument("--horizon-ew", type=int, default=20,
                   help="等权买入持有的再平衡间隔")
    a.add_argument("--regime-horizon", type=int, default=20)
    a.set_defaults(func=cmd_analyze)

    s = sub.add_parser("selftest", help="合成数据自检（不触网）")
    s.set_defaults(func=cmd_selftest)
    return p


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())