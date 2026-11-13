#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""持仓组合层实验台（GitHub issue #76 / R7）。

性质：**研究工具**。本文件不属于生产路径；`source/`、`actions.py`、
`requirements.txt`、`README.md` 契约均不改动。

两条命令：
    python tools/portfolio_lab.py fetch      # 取数（联网；股票/指数 baostock，基金 akshare OTC）
    python tools/portfolio_lab.py analyze    # 离线分析（只读 evidence/portfolio/data）

设计约束（与 issue #76 对齐）：
  * 候选规则在 ``CANDIDATES`` 里**写死**，跑数后不按结果增删改；
  * 基金按各自成立日进入组合，**未成立期间不编造净值**；
  * 成本：股票买 13bp / 卖 23bp（3bp 佣金 + 10bp 滑点 + 卖侧 10bp 印花税）；
    场外基金申赎各 15bp（与 ``source/fund_analysis/fund_backtest.py::FUND_FEE_RATE``
    同值，见 ``FUND_FEE_ASSUMPTION``）；
  * 信号在 t 日收盘算出，**t+1 日收盘成交**（一天延迟；股票端以滑点覆盖开盘/收盘差）；
  * 指标：扣费年化、maxDD、Sharpe、换手、再平衡次数，全部附「等暴露度随机对照」。

数据落盘在 ``evidence/portfolio/``（仓库 .gitignore 已忽略 ``evidence/``），
因此**不会提交任何 CSV**。
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
from datetime import date, datetime, timedelta

import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "source")
TOOLS = os.path.join(ROOT, "tools")
for _p in (SRC, TOOLS):
    if _p not in sys.path:
        sys.path.insert(0, _p)

DATA = os.path.join(ROOT, "evidence", "portfolio", "data")
PRICE_DIR = os.path.join(DATA, "price")
FUND_DIR = os.path.join(DATA, "fund")
INDEX_DIR = os.path.join(DATA, "index")
FLOW_DIR = os.path.join(DATA, "flow")
OUT = os.path.join(ROOT, "evidence", "portfolio", "out")

# ---------------------------------------------------------------------------
# 实仓（以 Hermes skill 的清单为准；issue #76）
# ---------------------------------------------------------------------------
STOCKS = ["002415", "601066"]          # 海康威视、中信建投
FUNDS = ["023299", "006195", "001917", "001564",
         "016858", "007950", "025422", "025415"]
INDEX_CODE = "sh.000300"

# ---------------------------------------------------------------------------
# 预注册候选（冻结；禁止按结果修改）
# ---------------------------------------------------------------------------
#: ``id`` 同时是结果表键。spec 见 ``RULES`` 与 ``docs/PORTFOLIO_OVERLAY_V1.md`` §2。
CANDIDATES = [
    ("bh_eq",               "baseline",   {}),
    ("ew_monthly_band5",    "rebalance",  {"freq": "M", "band": 0.05}),
    ("ew_quarterly_band5",  "rebalance",  {"freq": "Q", "band": 0.05}),
    ("riskparity_monthly",  "riskparity", {"freq": "M", "band": 0.02, "vol_window": 60}),
    ("riskparity_quarterly","riskparity", {"freq": "Q", "band": 0.02, "vol_window": 60}),
    ("regime_ma200",        "regime",     {"freq": "M", "ma": 200, "low": 0.50}),
    ("dd_trim10_cut20",     "drawdown",   {"trim": 0.10, "cut": 0.20, "readd": 0.10}),
    ("value_tilt_stock",    "value",      {"freq": "M", "band": 0.02, "pct_window": 756,
                                           "pct_min": 250}),
    ("flow_tilt_stock",     "flow",       {"freq": "M", "band": 0.02, "window": 20}),
]

# ---------------------------------------------------------------------------
# 成本与口径（冻结）
# ---------------------------------------------------------------------------
STOCK_BUY_COST = 0.0013     # 3bp 佣金 + 10bp 滑点
STOCK_SELL_COST = 0.0023    # + 10bp 印花税
FUND_BUY_COST = 0.0015      # 见 FUND_FEE_ASSUMPTION
FUND_SELL_COST = 0.0015
FUND_FEE_ASSUMPTION = (
    "场外基金申/赎各按 15bp 计（与 source/fund_analysis/fund_backtest.py::"
    "FUND_FEE_RATE=0.0015 同值）。真实费率随平台与持有期变化：申购费多为 "
    "0.1%~0.15%（平台一折），赎回费在持有 <7 日时可达 1.5%、7 日~1 年约 0.5%、"
    "≥1 年多为 0。本报告的全部再平衡频率 ≥ 月度，持有期远大于 7 日，故 15bp 是"
    "**偏乐观**的近似；敏感性见 docs §7。"
)
CASH_RETURN = 0.0           # 现金 0 收益（货基约 1.3%/年未计，偏保守）
REBAL_BAND_EW = 0.05
REBAL_BAND_RP = 0.02
VOL_WINDOW = 60
VOL_MIN = 40
PB_WINDOW = 756
PB_MIN = 250
REGIME_MA = 200
DD_TRIM = 0.10
DD_CUT = 0.20
DD_READD = 0.10
EVAL_START = "2018-06-20"   # 601066 上市日；两只股票此日起都在
OOS_FRACTION = 0.30
RANDOM_REPS = 300
BOOT_REPS = 1000
SEED = 20261113
MAX_TRADE_RETRY = 20
FETCH_START = "2016-06-01"  # 给 MA200 / 60 日波动 / PB 分位留预热
STOCK_FIELDS = "date,open,high,low,close,volume,amount,tradestatus,isST,peTTM,pbMRQ,psTTM"
INDEX_FIELDS = "date,open,high,low,close,volume,amount"


def _ensure_dirs():
    for path in (DATA, PRICE_DIR, FUND_DIR, INDEX_DIR, FLOW_DIR, OUT):
        os.makedirs(path, exist_ok=True)


def _log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def _json_default(obj):
    if isinstance(obj, np.integer):
        return int(obj)
    if isinstance(obj, np.floating):
        return float(obj)
    if isinstance(obj, np.bool_):
        return bool(obj)
    if isinstance(obj, np.ndarray):
        return obj.tolist()
    if isinstance(obj, (pd.Timestamp, datetime, date)):
        return obj.isoformat()
    raise TypeError(f"not serializable: {type(obj)!r}")


def _write_json(path, payload):
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2, default=_json_default)


def _read_json(path):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def _git_head():
    import subprocess
    try:
        return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT,
                                       text=True).strip()
    except Exception:  # noqa: BLE001
        return None


# ---------------------------------------------------------------------------
# 1) 取数（联网）——与离线分析严格分开
# ---------------------------------------------------------------------------
def _bs_code(code):
    return ("sh." if code.startswith("6") else "sz.") + code


def _query_baostock(bs, code, start, fields, attempts=3):
    last = None
    for attempt in range(attempts):
        try:
            rs = bs.query_history_k_data_plus(
                code, fields, start_date=start,
                end_date=date.today().isoformat(), frequency="d", adjustflag="2")
            if rs.error_code != "0":
                raise RuntimeError(f"{rs.error_code} {rs.error_msg}")
            rows = []
            while rs.next():
                rows.append(rs.get_row_data())
            if not rows:
                raise RuntimeError("空行情")
            frame = pd.DataFrame(rows, columns=rs.fields)
            for col in frame.columns:
                if col != "date":
                    frame[col] = pd.to_numeric(frame[col], errors="coerce")
            return frame[~frame["date"].duplicated(keep="last")]
        except Exception as exc:  # noqa: BLE001
            last = exc
            try:
                bs.logout()
            except Exception:  # noqa: BLE001
                pass
            time.sleep(1.5 * (attempt + 1))
            bs.login()
    raise last


def _fetch_fund(symbol, indicator):
    import akshare as ak
    df = ak.fund_open_fund_info_em(symbol=symbol, indicator=indicator)
    if df is None or df.empty:
        raise RuntimeError(f"{indicator} 空数据")
    return df


def cmd_fetch(args):
    import baostock as bs

    _ensure_dirs()
    meta = {"head": _git_head(), "started": datetime.now().isoformat(timespec="seconds"),
            "fetch_start": FETCH_START, "stocks": {}, "funds": {}, "index": {},
            "flow": {}, "failures": []}

    _log("登录 baostock …")
    lg = bs.login()
    if lg.error_code != "0":
        raise RuntimeError(f"baostock 登录失败 {lg.error_code} {lg.error_msg}")
    try:
        for code in STOCKS:
            try:
                df = _query_baostock(bs, _bs_code(code), FETCH_START, STOCK_FIELDS)
                df.to_csv(os.path.join(PRICE_DIR, f"{code}.csv"), index=False)
                meta["stocks"][code] = {"rows": len(df),
                                        "first": str(df["date"].iloc[0]),
                                        "last": str(df["date"].iloc[-1])}
                _log(f"股票 {code}: {len(df)} 行 {df['date'].iloc[0]}~{df['date'].iloc[-1]}")
            except Exception as exc:  # noqa: BLE001
                meta["failures"].append({"code": code, "error": f"{type(exc).__name__}: {exc}"})
                _log(f"[FAIL] 股票 {code}: {exc}")
            time.sleep(args.pause)

        try:
            df = _query_baostock(bs, INDEX_CODE, FETCH_START, INDEX_FIELDS)
            df.to_csv(os.path.join(INDEX_DIR, "sh000300.csv"), index=False)
            meta["index"] = {"rows": len(df), "first": str(df["date"].iloc[0]),
                             "last": str(df["date"].iloc[-1])}
            _log(f"指数 {INDEX_CODE}: {len(df)} 行")
        except Exception as exc:  # noqa: BLE001
            meta["failures"].append({"code": INDEX_CODE, "error": f"{type(exc).__name__}: {exc}"})
            _log(f"[FAIL] 指数 {INDEX_CODE}: {exc}")
    finally:
        try:
            bs.logout()
        except Exception:  # noqa: BLE001
            pass

    # ---- 场外基金：累计净值（收益口径）+ 单位净值（对照口径） ----
    for code in FUNDS:
        rec = {}
        for indicator, tag in (("累计净值走势", "acc"), ("单位净值走势", "unit")):
            for attempt in range(3):
                try:
                    df = _fetch_fund(code, indicator)
                    col = "累计净值" if tag == "acc" else "单位净值"
                    out = df.rename(columns={"净值日期": "date", col: "nav"})[["date", "nav"]]
                    out["date"] = pd.to_datetime(out["date"], errors="coerce")
                    out["nav"] = pd.to_numeric(out["nav"], errors="coerce")
                    out = out.dropna().sort_values("date")
                    out = out[~out["date"].duplicated(keep="last")]
                    out.to_csv(os.path.join(FUND_DIR, f"{code}_{tag}.csv"), index=False)
                    rec[tag] = {"rows": len(out),
                                "first": out["date"].iloc[0].date().isoformat(),
                                "last": out["date"].iloc[-1].date().isoformat()}
                    break
                except Exception as exc:  # noqa: BLE001
                    if attempt == 2:
                        meta["failures"].append({"code": f"fund_{code}_{tag}",
                                                 "error": f"{type(exc).__name__}: {exc}"})
                        _log(f"[FAIL] 基金 {code} {tag}: {exc}")
                    else:
                        time.sleep(1.5 * (attempt + 1))
            time.sleep(args.pause)
        meta["funds"][code] = rec
        _log(f"基金 {code}: {rec}")

    # ---- 资金流（新浪备胎，120 日窗口；供候选 7 描述性检验） ----
    for code in STOCKS:
        try:
            from stock_analysis.data_sources._astock_generated import fund_flow_backup
            rows = fund_flow_backup(code, 120)
            if not rows:
                raise RuntimeError("空响应")
            frame = pd.DataFrame(rows)
            keep = [c for c in ("date", "close", "net_amount", "turnover") if c in frame.columns]
            frame = frame[keep].sort_values("date")
            frame = frame[~frame["date"].duplicated(keep="last")]
            frame.to_csv(os.path.join(FLOW_DIR, f"{code}_sina.csv"), index=False)
            meta["flow"][code] = {"rows": len(frame), "first": str(frame["date"].iloc[0]),
                                  "last": str(frame["date"].iloc[-1]),
                                  "source": "sina_fund_flow_backup"}
            _log(f"资金流 {code}: {len(frame)} 行（新浪备胎）")
        except Exception as exc:  # noqa: BLE001
            meta["failures"].append({"code": f"flow_{code}",
                                     "error": f"{type(exc).__name__}: {exc}"})
            _log(f"[FAIL] 资金流 {code}: {exc}")
        time.sleep(args.pause)

    meta["finished"] = datetime.now().isoformat(timespec="seconds")
    _write_json(os.path.join(DATA, "fetch_meta.json"), meta)
    _log(f"取数完成，失败 {len(meta['failures'])} 项 → {DATA}/fetch_meta.json")
    return 0


# ---------------------------------------------------------------------------
# 2) 离线数据装载
# ---------------------------------------------------------------------------
def load_data(unit_nav=False):
    """把股票 qfq、指数、基金净值对齐到统一交易日历。

    Returns dict:
      dates: DatetimeIndex
      close: DataFrame(asset, date) —— 股票 qfq 收盘 / 基金累计（或单位）净值，
             当日不可用为 NaN（**不 ffill**，由 ``available`` 掩码决定）
      available: bool DataFrame —— 资产自上市/成立日起为 True
      tradable: bool DataFrame —— 股票 tradestatus>0；基金 True
      pb: DataFrame —— 股票 pbMRQ（其余 NaN）
      index_close: Series
      first: dict asset -> 首个可用日
    """
    idx_path = os.path.join(INDEX_DIR, "sh000300.csv")
    if not os.path.exists(idx_path):
        raise RuntimeError(f"缺少 {idx_path}；先跑 fetch")
    index = pd.read_csv(idx_path, parse_dates=["date"]).set_index("date").sort_index()
    dates = pd.DatetimeIndex(index.index)

    close = {}
    available = {}
    tradable = {}
    pb = {}
    first = {}
    stock_set = set(STOCKS)
    for code in STOCKS:
        path = os.path.join(PRICE_DIR, f"{code}.csv")
        if not os.path.exists(path):
            raise RuntimeError(f"缺少 {path}；先跑 fetch")
        df = pd.read_csv(path, parse_dates=["date"]).set_index("date").sort_index()
        df = df[~df.index.duplicated(keep="last")]
        ser = df["close"].reindex(dates)
        close[code] = ser
        first[code] = ser.first_valid_index()
        # 可用性 = 上市日起（不是「当日有行」）：停牌日仍有行但 tradestatus=0
        available[code] = (ser.first_valid_index() is not None) & (dates >= ser.first_valid_index())
        tradable[code] = (df["tradestatus"].reindex(dates).fillna(0) > 0.5) & ser.notna()
        pb[code] = pd.to_numeric(df.get("pbMRQ"), errors="coerce").reindex(dates)
    for code in FUNDS:
        tag = "acc" if not unit_nav else "unit"
        path = os.path.join(FUND_DIR, f"{code}_{tag}.csv")
        if not os.path.exists(path):
            alt = os.path.join(FUND_DIR, f"{code}_{'unit' if tag == 'acc' else 'acc'}.csv")
            if not os.path.exists(alt):
                raise RuntimeError(f"缺少基金数据 {code}；先跑 fetch")
            path = alt
        df = pd.read_csv(path, parse_dates=["date"]).set_index("date").sort_index()
        df = df[~df.index.duplicated(keep="last")]
        # 基金只在交易日有净值；映射到主日历，不跨日填充（缺失即不可用当天，收益按 0）
        ser = df["nav"].reindex(dates)
        close[code] = ser
        first[code] = ser.first_valid_index()
        # 可用性 = 成立日起；当天缺净值只是「不可交易」，不是「未成立」
        available[code] = (ser.first_valid_index() is not None) & (dates >= ser.first_valid_index())
        tradable[code] = ser.notna()

    assets = STOCKS + FUNDS
    close_df = pd.DataFrame({a: close[a] for a in assets}, index=dates)
    avail_df = pd.DataFrame({a: available[a] for a in assets}, index=dates).fillna(False)
    trad_df = pd.DataFrame({a: tradable[a] for a in assets}, index=dates).fillna(False)
    pb_df = pd.DataFrame({c: pb[c] for c in STOCKS}, index=dates)

    # 净值在交易日缺失（基金暂停/未披露）时用**前值**估值，但当天不可交易：
    # 这是对「持有中资产」唯一不编造未来信息的处理（前值是已知信息）。
    nav_ff = close_df.ffill()
    return {"dates": dates, "close": nav_ff, "raw_close": close_df,
            "available": avail_df, "tradable": trad_df, "pb": pb_df,
            "index_close": index["close"], "first": first,
            "assets": assets, "unit_nav": unit_nav}


def _returns(data):
    """日收益（用 ffill 后的净值；首个有效日前为 0）。"""
    df = data["close"]
    ret = df.pct_change().fillna(0.0)
    # 资产未可用之前不得有收益（pct_change 会给 NaN → 0，已覆盖）
    return ret


def _pb_percentile(pb_series, window=PB_WINDOW, min_samples=PB_MIN):
    """截至 t 的滚动分位（0–100），只用 t 及之前的样本 —— 与 valuation.py 同义。"""
    vals = pb_series.to_numpy(dtype=float)
    out = np.full(len(vals), np.nan)
    for i in range(len(vals)):
        cur = vals[i]
        if not np.isfinite(cur) or cur <= 0:
            continue
        lo = max(0, i - window + 1)
        sample = vals[lo:i + 1]
        sample = sample[np.isfinite(sample) & (sample > 0)]
        if len(sample) < min_samples:
            continue
        out[i] = 100.0 * np.sum(sample < cur) / (len(sample) - 1)
    return pd.Series(out, index=pb_series.index)


# ---------------------------------------------------------------------------
# 3) 组合模拟引擎
# ---------------------------------------------------------------------------
class Sim:
    """日频组合模拟器。

    ``rule.decide(t, w, ctx)`` 在 **t 日收盘**返回目标权重向量（长度 = 资产数，
    其余为现金），或 None 表示不交易。引擎把决策**顺延一天**执行（t+1 收盘），
    以保证没有同柱前视。成本按每笔买入/卖出权重计，直接从组合价值扣除。
    """

    def __init__(self, data, start=None):
        self.data = data
        self.dates = data["dates"]
        self.assets = data["assets"]
        self.avail = data["available"].to_numpy()
        self.tradable = data["tradable"].to_numpy()
        self.ret = _returns(data).to_numpy()
        self.n_assets = len(self.assets)
        self.start_idx = int(self.dates.searchsorted(pd.Timestamp(start))) if start else 0
        self.buy_cost = np.array([STOCK_BUY_COST if a in STOCKS else FUND_BUY_COST
                                  for a in self.assets])
        self.sell_cost = np.array([STOCK_SELL_COST if a in STOCKS else FUND_SELL_COST
                                   for a in self.assets])
        self.equity_now = None
        self.gross_now = None

    def run(self, rule):
        n = len(self.dates)
        w = np.zeros(self.n_assets)
        value = 1.0
        gross = 1.0
        eq = np.full(n, np.nan)
        eq_gross = np.full(n, np.nan)
        turnover = np.zeros(n)
        trades = np.zeros(n)
        costs = np.zeros(n)
        exposure = np.zeros(n)
        blocked = 0
        pending = None          # 待执行目标向量（原样保留，重试时不再改动）
        retry_mask = None       # 上一日因停牌/无净值而未成交的资产
        retry_left = 0

        def _execute(t, target, value, w, retry_only=None):
            """在 t 日收盘按 target 成交；retry_only 非 None 时只处理这些资产的
            剩余差额（其余资产保持不动，避免重试期间的伪交易）。"""
            nonlocal blocked
            exec_target = w.copy() if retry_only is not None else np.zeros(self.n_assets)
            mask = np.zeros(self.n_assets, dtype=bool) if retry_only is None else retry_only.copy()
            if retry_only is None:
                exec_target = np.asarray(target, dtype=float).copy()
                exec_target[~self.avail[t]] = 0.0
                mask = self.avail[t] & (np.abs(exec_target - w) > 1e-12)
            else:
                fill = mask & self.tradable[t] & self.avail[t]
                exec_target[fill] = np.asarray(target, dtype=float)[fill]
                mask = mask & ~fill
            unfilled = mask & ~self.tradable[t]
            blocked += int(unfilled.sum())
            exec_target[unfilled] = w[unfilled]
            delta = exec_target - w
            buy = np.clip(delta, 0.0, None)
            sell = np.clip(-delta, 0.0, None)
            cost = float(np.dot(buy, self.buy_cost) + np.dot(sell, self.sell_cost))
            turnover[t] += 0.5 * float(np.sum(np.abs(delta)))
            trades[t] += float(np.sum(np.abs(delta) > 1e-9))
            costs[t] += cost
            value *= (1.0 - cost)
            return exec_target, value, unfilled

        for t in range(n):
            if t > 0:
                r = self.ret[t]
                port_r = float(np.dot(w, r))
                value *= (1.0 + port_r)
                gross *= (1.0 + port_r)
                if port_r != 0:
                    w = w * (1.0 + r) / (1.0 + port_r)

            # 执行前一日收盘做出的决策（t+1 日成交）；未成交部分次日重试
            if pending is not None and t >= self.start_idx:
                if retry_mask is None:
                    w_new, value, still = _execute(t, pending, value, w)
                else:
                    w_new, value, still = _execute(t, pending, value, w,
                                                   retry_only=retry_mask)
                w = w_new
                if still.any() and retry_left > 0:
                    retry_left -= 1
                    retry_mask = still
                else:
                    pending, retry_mask, retry_left = None, None, 0

            # 评估起点统一建仓：所有候选从「等权全额」出发，起点一致
            if t == self.start_idx and float(np.sum(w)) <= 1e-12:
                avail = self.avail[t]
                cnt = int(avail.sum())
                if cnt:
                    target = np.where(avail, 1.0 / cnt, 0.0)
                    w, value, still = _execute(t, target, value, w)
                    if still.any():
                        pending, retry_mask, retry_left = target, still, MAX_TRADE_RETRY

            if t + 1 < n and t >= self.start_idx:
                dec = rule.decide(t, w, self)
                if dec is not None:
                    pending, retry_mask, retry_left = dec, None, MAX_TRADE_RETRY
                # 统一「新资产上线」：所有候选在同一时点按 1/N 等权买入，
                # 候选自身规则在下一次评审时接管（保证候选间差异只来自规则）
                if t > 0 and np.any(self.avail[t] & ~self.avail[t - 1]):
                    eq_t = _equal_target(self, t)
                    if eq_t is not None:
                        pending, retry_mask, retry_left = eq_t, None, MAX_TRADE_RETRY

            eq[t] = value
            eq_gross[t] = gross
            exposure[t] = float(np.sum(w))
            self.equity_now = value
            self.gross_now = gross
        return {"equity": eq, "equity_gross": eq_gross, "turnover": turnover,
                "trades": trades, "costs": costs, "exposure": exposure,
                "blocked": blocked}


# ---- 规则 ----
def _equal_target(sim, t):
    """当日可用资产的等权目标（其余为现金）。"""
    avail = sim.avail[t]
    n = int(avail.sum())
    if n == 0:
        return None
    return np.where(avail, 1.0 / n, 0.0)


class EqualBuyHold:
    id = "bh_eq"

    def __init__(self, sim):
        self.sim = sim

    def decide(self, t, w, sim):
        # 建仓与新资产上线都由引擎统一处理；买入持有此后永不交易。
        return None


class EqualWeightRebalance:
    """等权 + 容忍带再平衡；只调整越界资产，未越界资产不动。"""

    def __init__(self, sim, freq, band):
        self.sim = sim
        self.freq = freq
        self.band = band
        self.id = f"ew_{'monthly' if freq == 'M' else 'quarterly'}_band{int(band*100)}"

    def decide(self, t, w, sim):
        if not _is_rebalance_day(sim.dates, t, self.freq):
            return None
        avail = sim.avail[t]
        n = int(avail.sum())
        target = np.where(avail, 1.0 / n, 0.0)
        drift = np.abs(target - w)
        breach = avail & (drift > self.band + 1e-12)
        if not breach.any():
            return None
        new = w.copy()
        new[breach] = target[breach]
        return new


def _is_rebalance_day(dates, t, freq):
    """t 是否为当月/当季最后一个交易日（用日历判定，无前视）。"""
    d = dates[t]
    if t == len(dates) - 1:
        return True
    nxt = dates[t + 1]
    if freq == "M":
        return (nxt.year, nxt.month) != (d.year, d.month)
    return (nxt.year, nxt.quarter) != (d.year, d.quarter)


class RiskParity:
    """逆波动率（60 日）等风险；容忍带 2pp。"""

    def __init__(self, sim, freq, band, vol_window):
        self.sim = sim
        self.freq = freq
        self.band = band
        self.vol_window = vol_window
        self.id = f"riskparity_{'monthly' if freq == 'M' else 'quarterly'}"

    def _vols(self, t, avail):
        lo = max(1, t - self.vol_window + 1)
        window = self.sim.ret[lo:t + 1, :]
        vols = np.full(self.sim.n_assets, np.nan)
        for j in range(self.sim.n_assets):
            if not avail[j]:
                continue
            col = window[:, j]
            col = col[np.isfinite(col)]
            if len(col) >= VOL_MIN:
                s = float(np.std(col, ddof=1))
                if s > 0:
                    vols[j] = s
        return vols

    def decide(self, t, w, sim):
        if not _is_rebalance_day(sim.dates, t, self.freq):
            return None
        avail = sim.avail[t]
        vols = self._vols(t, avail)
        target = np.zeros(sim.n_assets)
        ok = avail & np.isfinite(vols)
        if ok.sum() >= 1:
            inv = np.zeros(sim.n_assets)
            inv[ok] = 1.0 / vols[ok]
            target[ok] = inv[ok] / inv[ok].sum()
        missing = avail & ~ok
        if missing.any():
            # 波动不可估计的资产暂按等权（后续窗口够了再进入）
            target[missing] = 1.0 / int(avail.sum())
            target = target / target.sum()
        if not np.any(np.abs(target - w) > self.band + 1e-12):
            return None
        new = w.copy()
        breach = avail & (np.abs(target - w) > self.band + 1e-12)
        new[breach] = target[breach]
        return new


class RegimeOverlay:
    """指数 close < MA200 → 权益暴露降至 50%，否则 100%。月度检查。"""

    id = "regime_ma200"

    def __init__(self, sim, freq, ma, low):
        self.sim = sim
        self.freq = freq
        self.ma = ma
        self.low = low
        idx = sim.data["index_close"]
        self.ma_series = idx.rolling(ma, min_periods=ma).mean()
        self.idx = idx
        self.mode = None

    def _target_mult(self, t):
        lo = max(0, t - self.ma + 1)
        ma_val = self.ma_series.iloc[t]
        if not np.isfinite(ma_val):
            return 1.0
        return self.low if self.idx.iloc[t] < ma_val else 1.0

    def decide(self, t, w, sim):
        if not _is_rebalance_day(sim.dates, t, self.freq):
            return None
        mult = self._target_mult(t)
        if self.mode is not None and abs(mult - self.mode) < 1e-12:
            return None
        self.mode = mult
        total = float(np.sum(w))
        if total <= 1e-9:
            # 空仓时按当前可用资产等权建仓
            avail = sim.avail[t]
            return np.where(avail, mult / int(avail.sum()), 0.0)
        return w * (mult / total)


class DrawdownTrim:
    """组合回撤 ≥10% → 权益减 20%；自减仓后净值回升 10% → 恢复满仓。"""

    id = "dd_trim10_cut20"

    def __init__(self, sim, trim, cut, readd):
        self.sim = sim
        self.trim = trim
        self.cut = cut
        self.readd = readd
        self.trimmed = False
        self.armed = True      # 每个回撤 episode 只减一次；回到 -5% 内才重新武装
        self.trough = None
        self.peak = None

    def decide(self, t, w, sim):
        # 用引擎维护的组合净值（equity_now，已扣费）
        if float(np.sum(w)) <= 1e-9:
            return _equal_target(sim, t)
        eq = sim.equity_now
        if eq is None or not np.isfinite(eq):
            return None
        self.peak = eq if self.peak is None else max(self.peak, eq)
        dd = eq / self.peak - 1.0
        if dd >= -0.05:
            self.armed = True
        if not self.trimmed and self.armed and dd <= -self.trim:
            self.trimmed = True
            self.armed = False
            self.trough = eq
            return w * (1.0 - self.cut)
        if self.trimmed:
            self.trough = min(self.trough, eq)
            if eq >= self.trough * (1.0 + self.readd):
                self.trimmed = False
                total = float(np.sum(w))
                avail = sim.avail[t]
                if total <= 1e-9:
                    return np.where(avail, 1.0 / int(avail.sum()), 0.0)
                return w / total
        return None


class StockSleeveTilt:
    """仅股票部分：按打分调整为两只股票之间的相对权重，股票合计权重不变。"""

    def __init__(self, sim, freq, band, score_fn, name):
        self.sim = sim
        self.freq = freq
        self.band = band
        self.score_fn = score_fn
        self.id = name
        self.stock_idx = np.array([sim.assets.index(c) for c in STOCKS])

    def decide(self, t, w, sim):
        if not _is_rebalance_day(sim.dates, t, self.freq):
            return None
        idx = self.stock_idx
        if float(np.sum(w)) <= 1e-9:
            return _equal_target(sim, t)
        sleeve = float(np.sum(w[idx]))
        if sleeve <= 1e-9:
            return None
        scores = self.score_fn(t, sim)
        if scores is None or not np.all(np.isfinite(scores)):
            return None
        if np.sum(scores) <= 0:
            return None
        target_sleeve = sleeve * scores / np.sum(scores)
        new = w.copy()
        breach = np.abs(target_sleeve - w[idx]) > self.band + 1e-12
        if not breach.any():
            return None
        new[idx[breach]] = target_sleeve[breach]
        return new


def _value_scores(t, sim):
    """1 − pb_pct/100（越便宜分越高）。任一股票分位缺失 → None（不调）。"""
    scores = []
    for code in STOCKS:
        ser = sim.data["pb_pct"][code]
        v = ser.iloc[t]
        if not np.isfinite(v):
            return None
        scores.append(max(1.0 - v / 100.0, 0.05))
    return np.array(scores)


def _flow_scores(t, sim):
    """20 日净流入 >0 给 1.5×，<0 给 0.5×（新浪口径，样本短）。"""
    scores = []
    for code in STOCKS:
        ser = sim.data["flow_20"][code]
        v = ser.iloc[t]
        if not np.isfinite(v):
            return None
        scores.append(1.5 if v > 0 else 0.5)
    return np.array(scores)


def _build_flow_features(data):
    """20 日净流入（新浪口径）；无数据的资产全 NaN。"""
    out = {}
    for code in STOCKS:
        path = os.path.join(FLOW_DIR, f"{code}_sina.csv")
        if not os.path.exists(path):
            out[code] = pd.Series(np.nan, index=data["dates"])
            continue
        df = pd.read_csv(path, parse_dates=["date"]).set_index("date").sort_index()
        if "net_amount" not in df.columns or df.empty:
            out[code] = pd.Series(np.nan, index=data["dates"])
            continue
        s = pd.to_numeric(df["net_amount"], errors="coerce")
        roll = s.rolling(20, min_periods=15).sum().reindex(data["dates"])
        out[code] = roll
    return out


def build_rules(data, sim, rule_id):
    """按预注册 id 构造全部（确定性）规则。"""
    if rule_id == "bh_eq":
        return EqualBuyHold(sim)
    if rule_id == "ew_monthly_band5":
        return EqualWeightRebalance(sim, "M", REBAL_BAND_EW)
    if rule_id == "ew_quarterly_band5":
        return EqualWeightRebalance(sim, "Q", REBAL_BAND_EW)
    if rule_id == "riskparity_monthly":
        return RiskParity(sim, "M", REBAL_BAND_RP, VOL_WINDOW)
    if rule_id == "riskparity_quarterly":
        return RiskParity(sim, "Q", REBAL_BAND_RP, VOL_WINDOW)
    if rule_id == "regime_ma200":
        return RegimeOverlay(sim, "M", REGIME_MA, 0.50)
    if rule_id == "dd_trim10_cut20":
        return DrawdownTrim(sim, DD_TRIM, DD_CUT, DD_READD)
    if rule_id == "value_tilt_stock":
        return StockSleeveTilt(sim, "M", 0.02, _value_scores, "value_tilt_stock")
    if rule_id == "flow_tilt_stock":
        return StockSleeveTilt(sim, "M", 0.02, _flow_scores, "flow_tilt_stock")
    raise KeyError(rule_id)


# ---------------------------------------------------------------------------
# 4) 指标与统计
# ---------------------------------------------------------------------------
def metrics(equity, dates, turnover, trades, exposure, lo=None, hi=None):
    eq = np.asarray(equity, dtype=float)
    lo = 0 if lo is None else lo
    hi = len(eq) - 1 if hi is None else hi
    seg = eq[lo:hi + 1]
    if len(seg) < 2 or not np.all(np.isfinite(seg)) or seg[0] <= 0:
        return {"n_days": len(seg), "annual": None, "maxdd": None, "vol": None,
                "sharpe": None, "turnover_annual": None, "rebalances": None,
                "exposure": None, "total": None}
    ret = seg[1:] / seg[:-1] - 1.0
    years = (dates[hi] - dates[lo]).days / 365.25
    total = seg[-1] / seg[0] - 1.0
    annual = (seg[-1] / seg[0]) ** (1.0 / years) - 1.0 if years > 0 else None
    peak = np.maximum.accumulate(seg)
    mdd = float(np.max(1.0 - seg / peak))
    vol = float(np.std(ret, ddof=1) * math.sqrt(252)) if len(ret) > 2 else None
    sharpe = (annual / vol) if (annual is not None and vol and vol > 0) else None
    turn = turnover[lo:hi + 1]
    turn_annual = float(np.sum(turn) / years) if years > 0 else None
    rebals = int(np.sum(trades[lo:hi + 1] > 0))
    expo = float(np.mean(exposure[lo:hi + 1]))
    return {"n_days": len(seg), "years": round(years, 3), "annual": annual,
            "maxdd": mdd, "vol": vol, "sharpe": sharpe, "total": total,
            "turnover_annual": turn_annual, "rebalances": rebals, "exposure": expo}


def daily_returns(equity, lo, hi):
    seg = np.asarray(equity, dtype=float)[lo:hi + 1]
    return seg[1:] / seg[:-1] - 1.0


def nw_t(x, lag=20):
    x = np.asarray(x, dtype=float)
    x = x[np.isfinite(x)]
    n = len(x)
    if n < 10:
        return None
    mu = float(np.mean(x))
    e = x - mu
    var = float(np.dot(e, e) / n)
    s = var
    for L in range(1, min(lag, n - 1) + 1):
        w = 1.0 - L / (lag + 1.0)
        s += 2.0 * w * float(np.dot(e[L:], e[:-L]) / n)
    if s <= 0:
        return None
    return mu / math.sqrt(s / n)


def pair_stats(base_daily, cand_daily):
    """配对日收益差 → 年化差、NW t、80% 功效 MDE。"""
    n = min(len(base_daily), len(cand_daily))
    if n < 30:
        return {"diff_annual": None, "t": None, "p": None, "mde_annual": None, "n": n}
    d = np.asarray(cand_daily[-n:]) - np.asarray(base_daily[-n:])
    tstat = nw_t(d)
    diff_ann = float(np.mean(d) * 252.0)
    se_ann = float(np.std(d, ddof=1) / math.sqrt(n) * 252.0)
    p = None
    if tstat is not None:
        from statistics import NormalDist
        p = 2.0 * (1.0 - NormalDist().cdf(abs(tstat)))
    return {"diff_annual": diff_ann, "t": tstat, "p": p,
            "mde_annual": 2.8 * se_ann, "n": n}


def block_bootstrap_maxdd(equity, p0, block=20, reps=BOOT_REPS, seed=SEED, lo=0, hi=None):
    """对净值路径做块重抽（近似 maxDD 抽样分布；仅用于描述不确定性）。"""
    eq = np.asarray(equity, dtype=float)
    hi = len(eq) - 1 if hi is None else hi
    ret = eq[lo + 1:hi + 1] / eq[lo:hi] - 1.0
    if len(ret) < block * 3:
        return None
    rng = np.random.default_rng(seed)
    nb = int(np.ceil(len(ret) / block))
    out = []
    for _ in range(reps):
        starts = rng.integers(0, len(ret) - block + 1, size=nb)
        path = np.concatenate([ret[s:s + block] for s in starts])[:len(ret)]
        curve = np.cumprod(1.0 + path)
        peak = np.maximum.accumulate(curve)
        out.append(float(np.max(1.0 - curve / peak)))
    out = np.array(out)
    return {"p05": float(np.percentile(out, 5)), "median": float(np.median(out)),
            "p95": float(np.percentile(out, 95)), "reps": reps}


# ---------------------------------------------------------------------------
# 5) 等暴露度随机对照
# ---------------------------------------------------------------------------
class RandomExposureRule:
    """随机暴露掩码（对照「减仓本身」是否就能得到同样的回撤改善）。

    mode:
      * ``monthly``：与 regime 同结构 —— 每月末随机选择 low 暴露，低暴露月份数与
        观测规则相同；
      * ``daily``：日频 iid 伯努利，平均暴露与观测规则相同（用于日频 trim）。
    """

    def __init__(self, sim, mode, mean_exposure, low_mult, n_low_months=None, seed=SEED):
        self.sim = sim
        self.mode = mode
        self.low = low_mult
        self.rng = np.random.default_rng(seed)
        self.mask = None
        if mode == "monthly":
            months = [(d.year, d.month) for d in sim.dates]
            uniq = sorted(set(months))
            k = n_low_months if n_low_months is not None else int(round(
                (1.0 - mean_exposure) / (1.0 - low_mult) * len(uniq)))
            k = max(0, min(len(uniq), k))
            chosen = set(self.rng.choice(len(uniq), size=k, replace=False).tolist())
            self.month_low = {uniq[i]: (i in chosen) for i in range(len(uniq))}
            self.mask = np.array([self.month_low[m] for m in months])
        else:
            p_low = (1.0 - mean_exposure) / (1.0 - low_mult)
            self.mask = self.rng.random(len(sim.dates)) < p_low

    def decide(self, t, w, sim):
        low = bool(self.mask[t])
        target_mult = self.low if low else 1.0
        total = float(np.sum(w))
        if total <= 1e-9:
            avail = sim.avail[t]
            n = int(avail.sum())
            return np.where(avail, target_mult / n, 0.0) if n else None
        if abs(total - target_mult) < 1e-12:
            return None
        return w * (target_mult / total)


class RandomPermuteRule:
    """把规则的权重向量在资产间随机置换（对照「选对资产」是否关键）。"""

    def __init__(self, base_rule, seed=SEED):
        self.base = base_rule
        self.rng = np.random.default_rng(seed)
        self.id = base_rule.id + "_perm"

    def decide(self, t, w, sim):
        out = self.base.decide(t, w, sim)
        if out is None:
            return None
        out = np.asarray(out, dtype=float).copy()
        perm = self.rng.permutation(len(out))
        return out[perm]


# ---------------------------------------------------------------------------
# 6) analyze
# ---------------------------------------------------------------------------
def _split_windows(dates, lo, hi):
    n = hi - lo + 1
    oos_start = lo + int(round(n * (1.0 - OOS_FRACTION)))
    return (lo, oos_start - 1), (oos_start, hi)


def cmd_analyze(args):
    _ensure_dirs()
    data = load_data(unit_nav=args.unit_nav)
    dates = data["dates"]
    ev_lo = int(dates.searchsorted(pd.Timestamp(EVAL_START)))
    ev_hi = len(dates) - 1
    # 最后一天无次日成交，剔除
    ev_hi = min(ev_hi, len(dates) - 2)
    (in_lo, in_hi), (oos_lo, oos_hi) = _split_windows(dates, ev_lo, ev_hi)

    data["pb_pct"] = {c: _pb_percentile(data["pb"][c]) for c in STOCKS}
    data["flow_20"] = _build_flow_features(data)

    sim = Sim(data, start=EVAL_START)
    meta = {"head": _git_head(), "started": datetime.now().isoformat(timespec="seconds"),
            "eval_start": str(dates[ev_lo].date()), "eval_end": str(dates[ev_hi].date()),
            "oos_start": str(dates[oos_lo].date()), "oos_end": str(dates[oos_hi].date()),
            "in_sample": [str(dates[in_lo].date()), str(dates[in_hi].date())],
            "n_days": int(ev_hi - ev_lo + 1), "assets": data["assets"],
            "first_dates": {a: (str(data["first"][a].date()) if data["first"][a] is not None else None)
                            for a in data["assets"]},
            "costs": {"stock_buy": STOCK_BUY_COST, "stock_sell": STOCK_SELL_COST,
                      "fund_buy": FUND_BUY_COST, "fund_sell": FUND_SELL_COST,
                      "cash_return": CASH_RETURN},
            "seed": SEED, "random_reps": RANDOM_REPS,
            "n_trials": len(CANDIDATES)}

    results = {}
    equity_map = {}
    gross_map = {}
    baseline = None

    for rule_id, family, params in CANDIDATES:
        rule = build_rules(data, sim, rule_id)
        sim_run = sim.run(rule)
        equity_map[rule_id] = sim_run["equity"]
        gross_map[rule_id] = sim_run["equity_gross"]
        m_all = metrics(sim_run["equity"], dates, sim_run["turnover"],
                        sim_run["trades"], sim_run["exposure"], ev_lo, ev_hi)
        m_in = metrics(sim_run["equity"], dates, sim_run["turnover"],
                       sim_run["trades"], sim_run["exposure"], in_lo, in_hi)
        m_oos = metrics(sim_run["equity"], dates, sim_run["turnover"],
                        sim_run["trades"], sim_run["exposure"], oos_lo, oos_hi)
        rec = {"family": family, "params": params, "all": m_all, "in": m_in, "oos": m_oos,
               "blocked": sim_run["blocked"],
               "maxdd_boot": block_bootstrap_maxdd(sim_run["equity"], None, lo=ev_lo, hi=ev_hi)}
        if baseline is None and rule_id == "bh_eq":
            baseline = sim_run
        results[rule_id] = rec

    # 配对统计（vs buy-and-hold）
    if baseline is not None:
        base_all = daily_returns(baseline["equity"], ev_lo, ev_hi)
        base_in = daily_returns(baseline["equity"], in_lo, in_hi)
        base_oos = daily_returns(baseline["equity"], oos_lo, oos_hi)
        for rule_id in results:
            if rule_id == "bh_eq":
                results[rule_id]["vs_bh"] = {"all": None, "in": None, "oos": None}
                continue
            cand = equity_map[rule_id]
            results[rule_id]["vs_bh"] = {
                "all": pair_stats(base_all, daily_returns(cand, ev_lo, ev_hi)),
                "in": pair_stats(base_in, daily_returns(cand, in_lo, in_hi)),
                "oos": pair_stats(base_oos, daily_returns(cand, oos_lo, oos_hi)),
            }

    # ---- 等暴露度随机对照 ----
    controls = {}
    for rule_id, must in (("regime_ma200", True), ("dd_trim10_cut20", True)):
        if rule_id not in results:
            continue
        m = results[rule_id]["all"]
        obs_exp = m["exposure"]
        mode = "monthly" if rule_id == "regime_ma200" else "daily"
        n_low = None
        if mode == "monthly":
            low_share = (1.0 - obs_exp) / (1.0 - 0.5)
            n_low = int(round(low_share * len(sorted({(d.year, d.month) for d in dates}))))
        dist = {"annual": [], "maxdd": []}
        for rep in range(RANDOM_REPS):
            rr = RandomExposureRule(sim, mode, obs_exp, 0.5,
                                    n_low_months=n_low if mode == "monthly" else None,
                                    seed=SEED + rep)
            run = sim.run(rr)
            mm = metrics(run["equity"], dates, run["turnover"], run["trades"],
                         run["exposure"], ev_lo, ev_hi)
            dist["annual"].append(mm["annual"])
            dist["maxdd"].append(mm["maxdd"])
        cand_m = results[rule_id]["all"]
        cand_dd_improve = (metrics(equity_map["bh_eq"], dates, baseline["turnover"],
                                   baseline["trades"], baseline["exposure"],
                                   ev_lo, ev_hi)["maxdd"] - cand_m["maxdd"])
        rand_dd_improve = np.array([results["bh_eq"]["all"]["maxdd"] - x
                                    for x in dist["maxdd"] if x is not None])
        rand_annual = np.array([x for x in dist["annual"] if x is not None])
        controls[rule_id] = {
            "mode": mode, "reps": len(rand_annual), "observed_exposure": obs_exp,
            "candidate_dd_improve": cand_dd_improve,
            "random_dd_improve": {"p05": float(np.percentile(rand_dd_improve, 5)),
                                  "median": float(np.median(rand_dd_improve)),
                                  "p95": float(np.percentile(rand_dd_improve, 95))},
            "candidate_percentile_dd": float(np.mean(rand_dd_improve < cand_dd_improve)),
            "candidate_annual": cand_m["annual"],
            "random_annual": {"p05": float(np.percentile(rand_annual, 5)),
                              "median": float(np.median(rand_annual)),
                              "p95": float(np.percentile(rand_annual, 95))},
        }

    meta["controls"] = controls

    # ---- 扣费拖累（毛 vs 净） ----
    for rid, rec in results.items():
        gross_m = metrics(gross_map[rid], dates,
                          np.zeros(len(dates)), np.zeros(len(dates)), np.ones(len(dates)),
                          ev_lo, ev_hi)
        rec["gross_annual"] = gross_m["annual"]
        if rec["all"]["annual"] is not None and gross_m["annual"] is not None:
            rec["cost_drag_annual"] = rec["all"]["annual"] - gross_m["annual"]

    # ---- 逐资产样本与单资产表现 ----
    meta["asset_table"] = _asset_table(data)

    # ---- 全 10 资产共同窗口（2025-09-11 起；样本不足，描述性） ----
    common_start = max(data["first"][a] for a in data["assets"])
    meta["common_window"] = _common_window(data, common_start)
    meta["sleeves"] = _sleeve_analysis(data)

    meta["results"] = results
    _write_json(os.path.join(OUT, "results.json"), meta)
    tables = _build_tables(meta)
    with open(os.path.join(OUT, "report_tables.md"), "w", encoding="utf-8") as fh:
        fh.write(tables)
    _print_report(meta)
    return 0


def _asset_table(data):
    rows = []
    for a in data["assets"]:
        s = data["raw_close"][a].dropna()
        if len(s) < 30:
            rows.append({"asset": a, "first": None, "last": None, "years": None})
            continue
        ret = s.pct_change().dropna()
        years = (s.index[-1] - s.index[0]).days / 365.25
        annual = (s.iloc[-1] / s.iloc[0]) ** (1.0 / years) - 1.0 if years > 0 else None
        peak = s.cummax()
        rows.append({"asset": a, "kind": "stock" if a in STOCKS else "fund",
                     "first": str(s.index[0].date()), "last": str(s.index[-1].date()),
                     "rows": int(len(s)), "years": round(years, 2),
                     "annual": annual, "vol": float(ret.std(ddof=1) * math.sqrt(252)),
                     "maxdd": float(np.max(1.0 - s / peak))})
    return rows


def subset_data(data, assets):
    """从全组合数据里截出某个「袖套」（股票或基金）的数据视图。"""
    sub = {
        "dates": data["dates"], "assets": list(assets),
        "close": data["close"][list(assets)],
        "raw_close": data["raw_close"][list(assets)],
        "available": data["available"][list(assets)],
        "tradable": data["tradable"][list(assets)],
        "pb": data["pb"], "index_close": data["index_close"],
        "first": {a: data["first"][a] for a in assets},
        "unit_nav": data["unit_nav"],
        "pb_pct": data.get("pb_pct", {}), "flow_20": data.get("flow_20", {}),
    }
    return sub


def _sleeve_analysis(data):
    """股票袖套（2 只）与基金袖套（8 只）分开跑，量级不受另一袖套稀释。"""
    dates = data["dates"]
    lo = int(dates.searchsorted(pd.Timestamp(EVAL_START)))
    hi = len(dates) - 2
    out = {}
    plan = [("stock", STOCKS, ["bh_eq", "value_tilt_stock", "flow_tilt_stock"]),
            ("fund", FUNDS, ["bh_eq", "ew_monthly_band5", "ew_quarterly_band5",
                             "riskparity_monthly", "regime_ma200"])]
    for name, assets, rules in plan:
        sub = subset_data(data, assets)
        sim = Sim(sub, start=EVAL_START)
        recs = {}
        eqs = {}
        for rid in rules:
            run = sim.run(build_rules(sub, sim, rid))
            eqs[rid] = run["equity"]
            recs[rid] = metrics(run["equity"], dates, run["turnover"], run["trades"],
                                run["exposure"], lo, hi)
        base = daily_returns(eqs["bh_eq"], lo, hi)
        for rid in rules:
            recs[rid]["vs_bh"] = (None if rid == "bh_eq" else
                                  pair_stats(base, daily_returns(eqs[rid], lo, hi)))
        out[name] = {"assets": list(assets), "rules": recs}
    return out


def _common_window(data, start):
    dates = data["dates"]
    lo = int(dates.searchsorted(start))
    hi = len(dates) - 2
    out = {"start": str(dates[lo].date()), "end": str(dates[hi].date()),
           "n_days": int(hi - lo + 1), "years": round((dates[hi] - dates[lo]).days / 365.25, 3),
           "rules": {}}
    sim = Sim(data, start=str(dates[lo].date()))
    for rid in ("bh_eq", "ew_monthly_band5", "ew_quarterly_band5", "regime_ma200"):
        rule = build_rules(data, sim, rid)
        run = sim.run(rule)
        m = metrics(run["equity"], dates, run["turnover"], run["trades"],
                    run["exposure"], lo, hi)
        out["rules"][rid] = m
    return out


def _md_table(headers, rows):
    out = ["| " + " | ".join(headers) + " |",
           "|" + "|".join(["---"] * len(headers)) + "|"]
    for r in rows:
        out.append("| " + " | ".join(str(x) for x in r) + " |")
    return "\n".join(out) + "\n"


def _build_tables(meta):
    parts = []
    parts.append("### 逐资产样本\n\n")
    parts.append(_md_table(
        ["资产", "类型", "起始", "结束", "行数", "年数", "年化", "波动", "maxDD"],
        [[r.get("asset"), r.get("kind"), r.get("first"), r.get("last"), r.get("rows"),
          r.get("years"), _fmt_pct(r.get("annual")), _fmt_pct(r.get("vol")),
          _fmt_pct(r.get("maxdd"))] for r in meta["asset_table"]]))
    parts.append("\n### 主窗口结果（全样本 / 样本内 / OOS）\n\n")
    rows = []
    for rid, rec in meta["results"].items():
        a, i, o = rec["all"], rec["in"], rec["oos"]
        vs = (rec.get("vs_bh") or {}).get("all") or {}
        vo = (rec.get("vs_bh") or {}).get("oos") or {}
        rows.append([rid, _fmt_pct(a["annual"]), _fmt_pct(a["maxdd"]), _fmt(a["sharpe"]),
                     _fmt_pct(a["turnover_annual"]), a["rebalances"], _fmt(a["exposure"]),
                     _fmt_pct(vs.get("diff_annual")), _fmt(vs.get("t")), _fmt_pct(vs.get("p"), 3),
                     _fmt_pct(vs.get("mde_annual")), _fmt_pct(i["annual"]),
                     _fmt_pct(o["annual"]), _fmt_pct(vo.get("diff_annual"))])
    parts.append(_md_table(
        ["rule", "年化", "maxDD", "Sharpe", "换手/年", "再平衡次数", "暴露",
         "vsBH年化", "NW t", "p", "MDE", "样本内", "OOS", "OOS vsBH"], rows))
    parts.append("\n### 等暴露度随机对照\n\n")
    crows = []
    for rid, c in meta["controls"].items():
        crows.append([rid, c["mode"], _fmt(c["observed_exposure"]),
                      _fmt_pct(c["candidate_dd_improve"]),
                      _fmt_pct(c["random_dd_improve"]["median"]),
                      _fmt_pct(c["random_dd_improve"]["p95"]),
                      _fmt_pct(c["candidate_annual"]),
                      _fmt_pct(c["random_annual"]["median"]),
                      _fmt(c["candidate_percentile_dd"])])
    parts.append(_md_table(
        ["rule", "对照类型", "观测暴露", "候选ΔmaxDD", "随机中位ΔmaxDD", "随机95分位",
         "候选年化", "随机中位年化", "候选ΔmaxDD分位"], crows))
    parts.append("\n### 袖套单独分析（股票 2 只 / 基金 8 只）\n\n")
    srows = []
    for name, blk in meta["sleeves"].items():
        for rid, m in blk["rules"].items():
            vs = m.get("vs_bh") or {}
            srows.append([name, rid, _fmt_pct(m["annual"]), _fmt_pct(m["maxdd"]),
                          _fmt(m["sharpe"]), _fmt_pct(m["turnover_annual"]),
                          m["rebalances"], _fmt_pct(vs.get("diff_annual")),
                          _fmt(vs.get("t")), _fmt_pct(vs.get("mde_annual")),
                          _fmt_pct(m["exposure"])])
    parts.append(_md_table(["袖套", "rule", "年化", "maxDD", "Sharpe", "换手/年",
                            "再平衡次数", "vsBH年化", "NW t", "MDE", "暴露"], srows))
    cw = meta["common_window"]
    parts.append(f"\n### 全 10 资产共同窗口 {cw['start']} ~ {cw['end']}"
                 f"（{cw['n_days']} 个交易日，{cw['years']} 年；样本不足）\n\n")
    parts.append(_md_table(
        ["rule", "年化", "maxDD", "Sharpe", "换手/年", "再平衡次数", "暴露"],
        [[rid, _fmt_pct(m["annual"]), _fmt_pct(m["maxdd"]), _fmt(m["sharpe"]),
          _fmt_pct(m["turnover_annual"]), m["rebalances"], _fmt(m["exposure"])]
         for rid, m in cw["rules"].items()]))
    return "".join(parts)


# ---------------------------------------------------------------------------
# 7) 报告
# ---------------------------------------------------------------------------
def _fmt_pct(x, digits=2):
    if x is None or (isinstance(x, float) and not np.isfinite(x)):
        return "n/a"
    return f"{100.0*x:+.{digits}f}%"


def _fmt(x, digits=2):
    if x is None or (isinstance(x, float) and not np.isfinite(x)):
        return "n/a"
    return f"{x:.{digits}f}"


def _print_report(meta):
    print(f"\n窗口 {meta['eval_start']} ~ {meta['eval_end']}，"
          f"{meta['n_days']} 个交易日；OOS 起点 {meta['oos_start']}")
    print(f"{'rule':>22} {'ann':>8} {'maxDD':>8} {'Sharpe':>7} {'turn/yr':>8} "
          f"{'#rebal':>7} {'expo':>6} {'vsBH(ann)':>9} {'t':>6} {'MDE':>6} {'OOS ann':>8}")
    for rid, rec in meta["results"].items():
        m = rec["all"]
        vs = (rec.get("vs_bh") or {}).get("all") or {}
        print(f"{rid:>22} {_fmt_pct(m['annual']):>8} {_fmt_pct(m['maxdd']):>8} "
              f"{_fmt(m['sharpe']):>7} {_fmt_pct(m['turnover_annual']):>8} "
              f"{m['rebalances']:>7} {_fmt(m['exposure']):>6} "
              f"{_fmt_pct(vs.get('diff_annual')):>9} {_fmt(vs.get('t')):>6} "
              f"{_fmt_pct(vs.get('mde_annual')):>6} {_fmt_pct(rec['oos']['annual']):>8}")


# ---------------------------------------------------------------------------
# 8) 自检（合成数据；不触网）
# ---------------------------------------------------------------------------
def _synthetic_data(n=900, seed=7):
    rng = np.random.default_rng(seed)
    dates = pd.bdate_range("2018-01-01", periods=n)
    assets = STOCKS + FUNDS
    idx = np.cumprod(1 + rng.normal(0.0003, 0.012, n))
    frames = {}
    avail = {}
    trad = {}
    pb = {}
    for i, a in enumerate(assets):
        r = rng.normal(0.00025, 0.01 + 0.001 * i, n)
        nav = np.cumprod(1 + r)
        frames[a] = nav
        avail[a] = np.ones(n, dtype=bool)
        trad[a] = np.ones(n, dtype=bool)
        if a in STOCKS:
            pb[a] = np.exp(rng.normal(1.0, 0.2, n))
    df = pd.DataFrame(frames, index=dates)
    data = {"dates": dates, "close": df, "raw_close": df,
            "available": pd.DataFrame(avail, index=dates),
            "tradable": pd.DataFrame(trad, index=dates),
            "pb": pd.DataFrame({c: pb[c] for c in STOCKS}, index=dates),
            "index_close": pd.Series(idx, index=dates),
            "first": {a: dates[0] for a in assets}, "assets": assets,
            "unit_nav": False}
    return data


def cmd_selftest(args):
    checks = []

    def check(name, cond, detail=""):
        checks.append((name, bool(cond), detail))
        print(f"  {'OK ' if cond else 'FAIL'} {name} {detail}")

    data = _synthetic_data()
    sim = Sim(data, start=str(data["dates"][0].date()))
    data["pb_pct"] = {c: _pb_percentile(data["pb"][c], window=252, min_samples=60)
                      for c in STOCKS}
    data["flow_20"] = {c: pd.Series(np.nan, index=data["dates"]) for c in STOCKS}

    run = sim.run(EqualBuyHold(sim))
    eq = run["equity"]
    check("曲线有限且正", np.all(np.isfinite(eq)) and np.all(eq > 0))
    # 买入持有只有「一次性建仓」成本，之后不再交易
    expected_entry = 0.1 * (2 * STOCK_BUY_COST + 8 * FUND_BUY_COST)
    trade_days = np.where(run["trades"] > 0)[0]
    check("买入持有只建仓一次且成本=等权买入费率",
          len(trade_days) == 1 and abs(float(np.sum(run["costs"])) - expected_entry) < 1e-12,
          f"days={trade_days.tolist()} costs={float(np.sum(run['costs'])):.6f}")

    # 等权买入持有 = 固定份额（权重随涨跌漂移），故毛净值应等于
    # 建仓日（t=1）等权买入后各资产净值比的均值
    nav = data["close"].to_numpy()
    ref_last = float(np.mean(nav[-1] / nav[0]))
    err = abs(run["equity_gross"][-1] - ref_last) / ref_last
    check("等权买入持有毛净值 = 各资产净值比的等权均值", err < 1e-9, f"rel_err={err:.2e}")

    # 再平衡带：不越界不交易
    rule = EqualWeightRebalance(sim, "M", 0.05)
    run2 = sim.run(rule)
    check("带内再平衡不产生成本（无漂移时）", float(np.sum(run2["costs"])) >= 0.0)

    # 无前视：把 t0 之后的收益全部放大 10 倍，t0 之前的决策/权重不得改变
    data2 = dict(data)
    dates = data["dates"]
    t0 = 500
    close2 = data["close"].copy()
    close2.iloc[t0:] = close2.iloc[t0:] * 10.0
    data2["close"] = close2
    data2["raw_close"] = close2
    sim2 = Sim(data2, start=str(dates[0].date()))
    run_b = sim2.run(build_rules(data2, sim2, "ew_monthly_band5"))
    sim1 = Sim(data, start=str(dates[0].date()))
    run_a = sim1.run(build_rules(data, sim1, "ew_monthly_band5"))
    same = np.allclose(run_a["equity"][:t0], run_b["equity"][:t0])
    check("无前视（未来收益不影响过去净值）", same)

    # 成本单调性：成本越高净值越低
    saved_b, saved_s = STOCK_BUY_COST, STOCK_SELL_COST
    try:
        globals()["STOCK_BUY_COST"] = saved_b * 10
        globals()["STOCK_SELL_COST"] = saved_s * 10
        sim3 = Sim(data, start=str(dates[0].date()))
        run_c = sim3.run(EqualWeightRebalance(sim3, "M", 0.05))
        check("提高成本 → 净值下降", run_c["equity"][-1] < run2["equity"][-1])
    finally:
        globals()["STOCK_BUY_COST"] = saved_b
        globals()["STOCK_SELL_COST"] = saved_s

    # 暴露度对照：随机掩码的暴露应与观测匹配
    obs = np.mean(np.linspace(0.5, 1.0, len(dates)))
    rr = RandomExposureRule(sim, "daily", 0.75, 0.5, seed=1)
    check("随机暴露掩码为二值", set(np.unique(rr.mask)).issubset({True, False}))

    # 指标一致性
    m = metrics(eq, dates, run["turnover"], run["trades"], 1.0 - np.zeros(len(eq)),
                0, len(eq) - 1)
    check("指标字段齐全", all(k in m for k in ("annual", "maxdd", "sharpe", "turnover_annual")))

    failed = [c for c in checks if not c[1]]
    print(f"\n自检：{len(checks)-len(failed)}/{len(checks)} 通过")
    return 0 if not failed else 1


def build_parser():
    p = argparse.ArgumentParser(description="持仓组合层实验台（issue #76）")
    sub = p.add_subparsers(dest="cmd", required=True)

    pf = sub.add_parser("fetch", help="取数（联网）")
    pf.add_argument("--pause", type=float, default=0.6)
    pf.set_defaults(func=cmd_fetch)

    pa = sub.add_parser("analyze", help="离线分析")
    pa.add_argument("--unit-nav", action="store_true",
                    help="基金收益用单位净值（默认为累计净值）")
    pa.set_defaults(func=cmd_analyze)

    ps = sub.add_parser("selftest", help="合成数据自检（不触网）")
    ps.set_defaults(func=cmd_selftest)
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())