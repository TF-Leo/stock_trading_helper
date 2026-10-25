# -*- coding: utf-8 -*-
"""横截面 IC 报告：验证"20 日尺度有信号"是否只是 14 只大盘股的样本偏差。

为什么需要外部跑
----------------
我在受限环境里只有 14 只本地缓存。2026-10-25 的架构复审据此得出
"1–5 日无信号、10–20 日动量显著"的结论；14 只票的横截面统计功效有限，
必须用更大的票池复验，否则整个 v3.0 的有效期契约就建立在小样本上。

用法
----
    # 1) 先扩充缓存（需要联网；可分批，失败会逐票列出）
    python tools/warm_cache.py 600000 600009 600016 ... 000001 000002 ...

    # 2) 生成报告（完全离线，读 source/stock_analysis/data/*.csv）
    python tools/ic_report.py
    python tools/ic_report.py --min-bars 500 --horizons 1,5,10,20,60

产出
----
    evidence/ic_report.csv     factor × horizon 的 IC / t / n
    evidence/ic_spread.csv     分 3 组的 Top-Bottom 价差与 t 值
    stdout 打印同样内容的表格
"""
from __future__ import annotations

import argparse
import glob
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "source" / "stock_analysis" / "data"
EVID = ROOT / "evidence"


def load_universe(min_bars: int):
    import pandas as pd
    close, vol, codes = {}, {}, []
    for f in sorted(glob.glob(str(DATA / "stock_*_qfq.csv"))):
        code = os.path.basename(f).split("_")[1]
        if code.startswith("5"):        # 跳过 ETF
            continue
        try:
            d = pd.read_csv(f, index_col=0, parse_dates=True)
        except Exception:               # noqa: BLE001
            continue
        if len(d) < min_bars or not {"close", "volume"} <= set(d.columns):
            continue
        close[code], vol[code] = d["close"], d["volume"]
        codes.append(code)
    if not codes:
        return None, None, []
    C = pd.DataFrame(close).sort_index()
    V = pd.DataFrame(vol).reindex(C.index)
    return C, V, codes


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--min-bars", type=int, default=500)
    ap.add_argument("--horizons", default="1,5,10,20,60")
    ap.add_argument("--start", default="2018-01-01")
    a = ap.parse_args()
    horizons = [int(x) for x in a.horizons.split(",") if x.strip()]

    import numpy as np
    import pandas as pd

    C, V, codes = load_universe(a.min_bars)
    if C is None or C.shape[1] < 8:
        print(f"可用标的不足（{0 if C is None else C.shape[1]} 只，至少需要 8 只）。"
              "请先用 tools/warm_cache.py 扩充缓存。")
        return 1
    C = C.loc[a.start:]
    V = V.loc[C.index]
    n = C.shape[1]
    print(f"票池 {n} 只，区间 {C.index[0].date()} ~ {C.index[-1].date()}，{len(C)} 个交易日\n")

    feats = {
        "mom5": C / C.shift(5) - 1,
        "mom20": C / C.shift(20) - 1,
        "mom60": C / C.shift(60) - 1,
        "dev20": C / C.rolling(20).mean() - 1,
        "lowvol20": -C.pct_change().rolling(20).std(),
        "volratio": V / V.rolling(20).mean(),
    }

    rows = []
    print(f"{'factor':10s}" + "".join(f"{('h=' + str(h)):>16s}" for h in horizons))
    for name, f in feats.items():
        cells = []
        for h in horizons:
            fwd = C.shift(-h) / C - 1
            ic = f.rank(axis=1).corrwith(fwd.rank(axis=1), axis=1).dropna()
            if len(ic) < 30 or ic.std() == 0:
                cells.append("n/a")
                rows.append({"factor": name, "horizon": h, "ic": None, "t": None, "n": len(ic)})
                continue
            mean = float(ic.mean())
            t = mean / float(ic.std()) * np.sqrt(len(ic))
            cells.append(f"{mean:+.4f}({t:+.1f})")
            rows.append({"factor": name, "horizon": h, "ic": round(mean, 6),
                         "t": round(float(t), 2), "n": int(len(ic))})
        print(f"{name:10s}" + "".join(f"{c:>16s}" for c in cells))

    print("\n注：括号内为 t 值；|t|>2 才算统计显著。")
    EVID.mkdir(exist_ok=True)
    pd.DataFrame(rows).to_csv(EVID / "ic_report.csv", index=False)

    # 分组价差：每日按 60 日动量取前/后 20%
    k = max(1, int(n * 0.2))
    score = feats["mom60"].rank(axis=1, pct=True)
    sp_rows = []
    for h in horizons:
        fwd = C.shift(-h) / C - 1
        top = fwd.where(score.rank(axis=1, ascending=False) <= k).mean(axis=1)
        bot = fwd.where(score.rank(axis=1, ascending=True) <= k).mean(axis=1)
        sp = (top - bot).dropna()
        if len(sp) < 30 or sp.std() == 0:
            continue
        t = float(sp.mean() / sp.std() * np.sqrt(len(sp)))
        sp_rows.append({"horizon": h, "top_minus_bottom": round(float(sp.mean()), 6),
                        "t": round(t, 2), "n": int(len(sp)),
                        "win_rate": round(float((sp > 0).mean()), 4)})
        print(f"mom60 前{k}-后{k}，未来{h:2d}日价差：{sp.mean():+.3%}  t={t:+.2f}  n={len(sp)}")
    pd.DataFrame(sp_rows).to_csv(EVID / "ic_spread.csv", index=False)
    print(f"\n已写入 {EVID / 'ic_report.csv'} 与 {EVID / 'ic_spread.csv'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
