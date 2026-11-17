#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""R12 取数器：把 #146 需要的公开数据串行落盘到 ``~/data/r12/``。

设计纪律
--------
* **串行**：没有线程 / 进程池；每个请求之间 ``time.sleep(SLEEP)``。
* **可续跑**：已存在的缓存默认跳过（``--force`` 才重拉）。
* **可审计**：每次落盘写 ``manifest.json``（来源 URL、抓取时间、行数、首末日期）。
* **不触网进测试**：本文件只被 CLI 手动运行；测试只 import 纯函数。
* 不写入 ``source/``，不改任何产品逻辑。

数据源与理由
------------
=============  ================================  ==================================
概念           来源                               说明
=============  ================================  ==================================
沪深300日线    腾讯指数 K 线（仓库已有通道）       复用 ``astock.fetch_index_daily``
恒生指数       腾讯 ``newfqkline``                hkHSI
S&P 500        腾讯 ``newfqkline``                us.INX（腾讯美股指数代码）
USDCNH（离岸） 东方财富 push2his                  133.USDCNH，2012 起
USDCNY（在岸） FRED DEXCHUS                       美联储 H.10，1981 起
美债 10Y       FRED DGS10                        1962 起，日频
M2 同比        akshare macro_china_money_supply   2008-01 起；FRED 同序列 2019-08 停更
SHIBOR         akshare macro_china_shibor_all     2017-03 起日频（DR007 无公开日频接口）
两融余额       东方财富 RPTA_RZRQ_LSHJ            RZYE+RQYE
北向净流入     东方财富 RPT_MUTUAL_DEAL_HISTORY    **2024-08-19 起停止披露**，历史值为空
股权质押        东方财富（akshare 封装）            逐票质押比例，按月末采样
美股个股        Yahoo chart v8                    仅用于 H2 美国对照组
=============  ================================  ==================================

用法
----
    .venv/bin/python tools/r12_fetch.py                 # 补齐全部缓存
    .venv/bin/python tools/r12_fetch.py --only core     # 只拉 H1 需要的核心序列
    .venv/bin/python tools/r12_fetch.py --force
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "source"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

CACHE = Path(os.environ.get("R12_CACHE", Path.home() / "data" / "r12"))
SLEEP = 1.0                     # 串行节流：每次 HTTP 之间
TIMEOUT = 25
UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) r12-research/1.0"}
TENCENT = "https://web.ifzq.gtimg.cn/appstock/app/newfqkline/get"
FRED = "https://fred.stlouisfed.org/graph/fredgraph.csv"
EM_KLINE = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
EM_DATA = "https://datacenter-web.eastmoney.com/api/data/v1/get"

START = "2004-01-01"            # 沪深300 发布前一年即可
US_STOCKS = [
    # 道琼斯 30 + 20 只大市值非道指成分（公开指数/大市值名单，不涉及任何持仓）
    "AAPL", "AMGN", "AMZN", "AXP", "BA", "CAT", "CRM", "CSCO", "CVX", "DIS",
    "GS", "HD", "HON", "IBM", "JNJ", "JPM", "KO", "MCD", "MMM", "MRK",
    "MSFT", "NKE", "PG", "SHW", "TRV", "UNH", "V", "VZ", "WMT", "INTC",
    "GOOGL", "META", "NVDA", "TSLA", "AVGO", "COST", "NFLX", "AMD", "QCOM", "TXN",
    "ORCL", "ADBE", "PEP", "ABBV", "LLY", "XOM", "BAC", "WFC", "TMO", "ACN",
]


# ---------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------
def _log(msg: str) -> None:
    print(f"[r12] {msg}", flush=True)


def _get(url: str, *, params=None, retries: int = 3) -> requests.Response:
    last = None
    for i in range(retries):
        try:
            resp = requests.get(url, params=params, headers=UA, timeout=TIMEOUT)
            resp.raise_for_status()
            return resp
        except Exception as exc:                       # noqa: BLE001 - 研究脚本
            last = exc
            time.sleep(1.5 * (i + 1))
    raise RuntimeError(f"GET {url} 失败: {last}")


def _write_csv(path: Path, header, rows) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(header)
        w.writerows(rows)
    tmp.replace(path)


def _update_manifest(name: str, source: str, rows: int, first, last) -> None:
    path = CACHE / "manifest.json"
    data = {}
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            data = {}
    data[name] = {
        "source": source,
        "fetched_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "rows": rows,
        "first": str(first),
        "last": str(last),
    }
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _done(name: str, force: bool) -> bool:
    path = CACHE / f"{name}.csv"
    if path.exists() and not force:
        _log(f"{name}: 缓存已存在，跳过（--force 重拉）")
        return True
    return False


# ---------------------------------------------------------------------------
# 腾讯指数
# ---------------------------------------------------------------------------
def _tencent_pages(code: str, start: str = START) -> list[list]:
    """腾讯 newfqkline **向前翻页**；返回 [date, open, close, high, low, volume]。"""
    seen: dict[str, list] = {}
    end = ""
    for page in range(40):
        params = {"param": f"{code},day,,{end},640,"}
        payload = _get(TENCENT, params=params).json()
        node = (payload.get("data") or {}).get(code) or {}
        items = node.get("day") or []
        if not items:
            break
        for item in items:
            seen[str(item[0])] = item
        earliest = min(seen)
        _log(f"{code} 第 {page + 1} 页：{len(items)} 根，最早 {earliest}")
        if len(items) < 640 or earliest <= start:
            break
        end = earliest
        time.sleep(SLEEP)
    rows = []
    for day in sorted(seen):
        it = seen[day]
        rows.append([day, float(it[1]), float(it[2]), float(it[3]), float(it[4]), float(it[5])])
    return [r for r in rows if r[0] >= start]


def fetch_csi300(force: bool = False) -> None:
    if _done("csi300_daily", force):
        return
    from stock_analysis.data_sources import astock
    df = astock.fetch_index_daily("sh000300", start_date=START)
    rows = [[d.strftime("%Y-%m-%d"), r.open, r.high, r.low, r.close, r.volume]
            for d, r in df.iterrows()]
    _write_csv(CACHE / "csi300_daily.csv",
               ["date", "open", "high", "low", "close", "volume"], rows)
    _update_manifest("csi300_daily", "tencent newfqkline sh000300", len(rows), rows[0][0], rows[-1][0])
    _log(f"csi300_daily: {len(rows)} 行 {rows[0][0]} → {rows[-1][0]}")


def fetch_allshare(force: bool = False) -> None:
    """中证全指（000985）——H2 的市场代理稳健性检验。"""
    if _done("allshare_daily", force):
        return
    from stock_analysis.data_sources import astock
    df = astock.fetch_index_daily("sh000985", start_date=START)
    rows = [[d.strftime("%Y-%m-%d"), r.open, r.high, r.low, r.close, r.volume]
            for d, r in df.iterrows()]
    _write_csv(CACHE / "allshare_daily.csv",
               ["date", "open", "high", "low", "close", "volume"], rows)
    _update_manifest("allshare_daily", "tencent newfqkline sh000985", len(rows),
                     rows[0][0], rows[-1][0])
    _log(f"allshare_daily: {len(rows)} 行 {rows[0][0]} → {rows[-1][0]}")


def fetch_tencent_index(name: str, code: str, force: bool = False) -> None:
    if _done(name, force):
        return
    rows = _tencent_pages(code)
    _write_csv(CACHE / f"{name}.csv",
               ["date", "open", "close", "high", "low", "volume"], rows)
    _update_manifest(name, f"tencent newfqkline {code}", len(rows), rows[0][0], rows[-1][0])
    _log(f"{name}: {len(rows)} 行 {rows[0][0]} → {rows[-1][0]}")


# ---------------------------------------------------------------------------
# 汇率 / 利率 / 美债
# ---------------------------------------------------------------------------
def fetch_usdcnh(force: bool = False) -> None:
    if _done("usdcnh_daily", force):
        return
    params = {
        "secid": "133.USDCNH", "fields1": "f1", "fields2": "f51,f53",
        "klt": "101", "fqt": "0", "beg": "20120101", "end": "20500101",
    }
    data = _get(EM_KLINE, params=params).json().get("data") or {}
    rows = []
    for line in data.get("klines") or []:
        day, close = line.split(",")[:2]
        rows.append([day, float(close)])
    _write_csv(CACHE / "usdcnh_daily.csv", ["date", "close"], rows)
    _update_manifest("usdcnh_daily", "eastmoney push2his 133.USDCNH", len(rows), rows[0][0], rows[-1][0])
    _log(f"usdcnh_daily: {len(rows)} 行 {rows[0][0]} → {rows[-1][0]}")


def _fred_csv(series: str) -> list[list[str]]:
    text = _get(FRED, params={"id": series}).text
    out = []
    for row in csv.reader(io.StringIO(text)):
        if not row or row[0] == "observation_date":
            continue
        out.append([row[0], row[1]])
    return out


def _fetch_fred(name: str, series: str, force: bool) -> None:
    if _done(name, force):
        return
    out = _fred_csv(series)
    rows = [[d, v] for d, v in out if v not in ("", ".")]
    _write_csv(CACHE / f"{name}.csv", ["date", "value"], rows)
    _update_manifest(name, f"FRED {series}", len(rows), rows[0][0], rows[-1][0])
    _log(f"{name}: {len(rows)} 行 {rows[0][0]} → {rows[-1][0]}")


def fetch_usdcny(force: bool = False) -> None:
    _fetch_fred("usdcny_onshore_daily", "DEXCHUS", force)


def fetch_us10y(force: bool = False) -> None:
    _fetch_fred("us10y_daily", "DGS10", force)


def fetch_m2(force: bool = False) -> None:
    """M2 存量与同比。

    **不用 FRED ``MYAGM2CNM189N``**：该序列实测 2019-08 后停更（249 行止于
    2019-08-01），做 2019 起的样本外等于用未来不存在的数据。改走 akshare
    ``macro_china_money_supply``（央行口径，2008-01 起，含同比与环比）。
    """
    if _done("m2_monthly", force):
        return
    import akshare as ak
    raw = ak.macro_china_money_supply()
    rows = []
    for _, r in raw.iterrows():
        m = str(r["月份"])                       # 形如 2026年10月份
        y, mm = m.split("年")
        rows.append([f"{int(y):04d}-{int(mm.replace('月份', '')):02d}-01",
                     float(r["货币和准货币(M2)-数量(亿元)"]),
                     float(r["货币和准货币(M2)-同比增长"])])
    rows.sort()
    _write_csv(CACHE / "m2_monthly.csv", ["date", "m2_level", "m2_yoy"], rows)
    _update_manifest("m2_monthly", "akshare macro_china_money_supply", len(rows),
                     rows[0][0], rows[-1][0])
    _log(f"m2_monthly: {len(rows)} 行 {rows[0][0]} → {rows[-1][0]}")


def fetch_shibor(force: bool = False) -> None:
    if _done("shibor_daily", force):
        return
    import akshare as ak
    raw = ak.macro_china_shibor_all()
    rows = []
    for _, r in raw.iterrows():
        on, m3 = r.get("O/N-定价"), r.get("3M-定价")
        if on is None or m3 is None:
            continue
        rows.append([str(r["日期"])[:10], float(on), float(m3)])
    rows.sort()
    _write_csv(CACHE / "shibor_daily.csv", ["date", "on", "m3"], rows)
    _update_manifest("shibor_daily", "akshare macro_china_shibor_all", len(rows), rows[0][0], rows[-1][0])
    _log(f"shibor_daily: {len(rows)} 行 {rows[0][0]} → {rows[-1][0]}")


# ---------------------------------------------------------------------------
# 两融 / 北向
# ---------------------------------------------------------------------------
def _em_report(report: str, *, page_size: int = 500, max_pages: int = 40,
               sort: str = "DIM_DATE") -> list[dict]:
    out: list[dict] = []
    for page in range(1, max_pages + 1):
        params = {
            "reportName": report, "columns": "ALL", "source": "WEB",
            "sortColumns": sort, "sortTypes": "1",
            "pageSize": page_size, "pageNumber": page,
        }
        js = _get(EM_DATA, params=params).json()
        res = js.get("result") or {}
        batch = res.get("data") or []
        if not batch:
            break
        out.extend(batch)
        if len(batch) < page_size:
            break
        time.sleep(SLEEP * 0.5)
    return out


def fetch_margin(force: bool = False) -> None:
    if _done("margin_daily", force):
        return
    raw = _em_report("RPTA_RZRQ_LSHJ")
    by_day: dict[str, list] = {}
    for rec in raw:
        day = str(rec.get("DIM_DATE"))[:10]
        rzye = rec.get("RZYE") or 0.0
        rqye = rec.get("RQYE") or 0.0
        by_day[day] = [day, rzye, rqye, rzye + rqye]
    rows = [by_day[d] for d in sorted(by_day)]
    rows = [r for r in rows if r[0] >= "2010-01-01"]
    _write_csv(CACHE / "margin_daily.csv",
               ["date", "rzye", "rqye", "total"], rows)
    _update_manifest("margin_daily", "eastmoney RPTA_RZRQ_LSHJ", len(rows), rows[0][0], rows[-1][0])
    _log(f"margin_daily: {len(rows)} 行 {rows[0][0]} → {rows[-1][0]}")


def fetch_northbound(force: bool = False) -> None:
    if _done("northbound_daily", force):
        return
    raw = _em_report("RPT_MUTUAL_DEAL_HISTORY", page_size=500, max_pages=30,
                     sort="TRADE_DATE")
    by_day: dict[str, float] = {}
    for rec in raw:
        if str(rec.get("MUTUAL_TYPE")) not in ("001", "003"):     # 沪股通 + 深股通
            continue
        net = rec.get("NET_DEAL_AMT")
        if net is None:
            continue
        day = str(rec.get("TRADE_DATE"))[:10]
        by_day[day] = by_day.get(day, 0.0) + float(net)
    rows = [[d, by_day[d]] for d in sorted(by_day)]
    _write_csv(CACHE / "northbound_daily.csv", ["date", "net_flow"], rows)
    _update_manifest("northbound_daily",
                     "eastmoney RPT_MUTUAL_DEAL_HISTORY (001+003); 停止披露后为空",
                     len(rows), rows[0][0], rows[-1][0])
    _log(f"northbound_daily: {len(rows)} 行 {rows[0][0]} → {rows[-1][0]}")


# ---------------------------------------------------------------------------
# 股权质押（H3，月末采样）
# ---------------------------------------------------------------------------
def month_ends(start: str = "2020-01", end: str = "2025-12") -> list[str]:
    """返回每月最后一个自然日的 YYYY-MM-DD（周末由上游取最近交易日）。"""
    import calendar
    y0, m0 = map(int, start.split("-"))
    y1, m1 = map(int, end.split("-"))
    out = []
    for y in range(y0, y1 + 1):
        for m in range(1, 13):
            if (y, m) < (y0, m0) or (y, m) > (y1, m1):
                continue
            out.append(f"{y:04d}-{m:02d}-{calendar.monthrange(y, m)[1]:02d}")
    return out


def _pledge_snapshot(day: str) -> list:
    import akshare as ak
    df = ak.stock_gpzy_pledge_ratio_em(date=day.replace("-", ""))
    if df is None or df.empty:
        raise RuntimeError("空返回")
    cols = {c: c for c in df.columns}
    ratio_col = "质押比例" if "质押比例" in cols else None
    if ratio_col is None:
        raise RuntimeError(f"列缺失: {df.columns.tolist()}")
    return [day, len(df),
            float(df[ratio_col].median()),
            float(df[ratio_col].mean()),
            float((df[ratio_col] >= 30).mean() * 100)]


def fetch_pledge(force: bool = False) -> None:
    if _done("pledge_monthly", force):
        return
    rows, failed = [], []
    for day in month_ends():
        base = datetime.strptime(day, "%Y-%m-%d")
        ok = False
        # 月末常落在周末/节假日，上游返回 None；向前最多退 7 天找最近可用快照。
        for back in range(8):
            probe = (base - timedelta(days=back)).strftime("%Y-%m-%d")
            try:
                rows.append(_pledge_snapshot(probe))
                _log(f"pledge {day} -> {probe}: n={rows[-1][1]}")
                ok = True
                break
            except Exception as exc:                 # noqa: BLE001
                last = exc
                time.sleep(0.2)
        if not ok:
            failed.append([day, type(last).__name__, str(last)[:120]])
        time.sleep(SLEEP * 0.5)
    _write_csv(CACHE / "pledge_monthly.csv",
               ["date", "n_pledged", "median_ratio", "mean_ratio", "pct_ge_30"],
               rows)
    if failed:
        _write_csv(CACHE / "pledge_failed.csv", ["date", "error", "detail"], failed)
    _update_manifest("pledge_monthly",
                     "eastmoney via akshare stock_gpzy_pledge_ratio_em",
                     len(rows), rows[0][0] if rows else "", rows[-1][0] if rows else "")
    _log(f"pledge_monthly: {len(rows)} 行，失败 {len(failed)}")


# ---------------------------------------------------------------------------
# 美股个股（仅 H2 对照组）
# ---------------------------------------------------------------------------
def fetch_us_stocks(force: bool = False) -> None:
    outdir = CACHE / "us_stocks"
    outdir.mkdir(parents=True, exist_ok=True)
    p1 = int(datetime(2020, 1, 1).timestamp())
    p2 = int(datetime.now().timestamp())
    got, failed = 0, []
    for tick in US_STOCKS:
        path = outdir / f"{tick}.csv"
        if path.exists() and not force:
            got += 1
            continue
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{tick}"
        try:
            js = _get(url, params={"period1": p1, "period2": p2, "interval": "1d"}).json()
            res = (js.get("chart") or {}).get("result") or []
            if not res:
                raise RuntimeError("empty result")
            r = res[0]
            ts = r["timestamp"]
            closes = r["indicators"]["adjclose"][0]["adjclose"]
            rows = []
            for t, c in zip(ts, closes):
                if c is None:
                    continue
                rows.append([datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%d"), float(c)])
            if len(rows) < 300:
                raise RuntimeError(f"rows={len(rows)}")
            _write_csv(path, ["date", "close"], rows)
            got += 1
            _log(f"us {tick}: {len(rows)} 行")
        except Exception as exc:                     # noqa: BLE001
            failed.append([tick, str(exc)[:120]])
        time.sleep(SLEEP * 0.5)
    if failed:
        _write_csv(CACHE / "us_stocks_failed.csv", ["ticker", "error"], failed)
    _update_manifest("us_stocks", "yahoo chart v8 adjclose", got, "2020-01-01", "now")
    _log(f"us_stocks: {got} 只，失败 {len(failed)}")


# ---------------------------------------------------------------------------
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="R12 公开数据抓取（串行）")
    ap.add_argument("--force", action="store_true", help="忽略已有缓存")
    ap.add_argument("--only", choices=["core", "h2", "h3", "all"], default="all")
    args = ap.parse_args(argv)
    CACHE.mkdir(parents=True, exist_ok=True)

    if args.only in ("core", "all"):
        fetch_csi300(args.force)
        fetch_allshare(args.force)
        fetch_tencent_index("hsi_daily", "hkHSI", args.force)
        fetch_tencent_index("spx_daily", "us.INX", args.force)
        fetch_usdcnh(args.force)
        fetch_usdcny(args.force)
        fetch_us10y(args.force)
        fetch_m2(args.force)
        fetch_shibor(args.force)
        fetch_margin(args.force)
        fetch_northbound(args.force)
    if args.only in ("h2", "all"):
        fetch_us_stocks(args.force)
    if args.only in ("h3", "all"):
        fetch_pledge(args.force)
    _log("完成")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
