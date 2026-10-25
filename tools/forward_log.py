# -*- coding: utf-8 -*-
"""前向验证台账：今天记下建议，20 个交易日后用真实行情结算。

为什么需要它
------------
现有全部结论都是**样本内**的（14 只票、2018 起）。唯一能证伪"20 日有效期"的办法
是把今天的建议冻住，等 20 个交易日后再看真实结果。这个脚本只做两件事：

    record   记录当前建议（每个标的：动作/目标仓位/复审日/当时收盘/买侧期望）
    evaluate 结算已到期的记录（horizon 个交易日后的实际收益，按方向记对错）

用法
----
    python tools/forward_log.py record
    python tools/forward_log.py record --codes 002415,600519   # 只记两只
    python tools/forward_log.py evaluate

产出（均在 evidence/ 下，已 gitignore）
    forward_log.csv     每次 record 追加一行（同一标的同一天只记一次）
    forward_eval.csv    每次 evaluate 覆盖写，含已到期与未到期两种状态
"""
from __future__ import annotations

import argparse
import csv
import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "source"
EVID = ROOT / "evidence"
LOG = EVID / "forward_log.csv"
EVAL = EVID / "forward_eval.csv"

DEFAULT_CODES = ["000858", "002270", "002371", "002415", "002594", "300476",
                 "300750", "600036", "600519", "600522", "601318", "601398",
                 "603259", "688981"]

FIELDS = ["recorded_at", "as_of", "code", "action", "target_position",
          "position_state", "review_date", "horizon", "close", "exp_mean",
          "exp_hit", "exp_n", "exp_t", "exp_ok", "status"]


def _cli_json(code):
    p = subprocess.run([sys.executable, "cli.py", code, "--json", "--fast"],
                       cwd=str(SRC), capture_output=True, text=True,
                       encoding="utf-8", errors="replace",
                       env={**__import__("os").environ, "PYTHONIOENCODING": "utf-8"})
    try:
        return json.loads(p.stdout), p.returncode
    except Exception:  # noqa: BLE001
        return None, p.returncode


def record(codes):
    EVID.mkdir(exist_ok=True)
    existing = set()
    if LOG.exists():
        with LOG.open(encoding="utf-8", newline="") as f:
            for row in csv.DictReader(f):
                existing.add((row["as_of"], row["code"]))
    new_file = not LOG.exists()
    rows = []
    for code in codes:
        d, rc = _cli_json(code)
        if not d:
            print(f"  {code}: 无 JSON（exit={rc}），跳过")
            continue
        rec, dd = d["recommendation"], d["data"]
        key = (str(dd.get("end"))[:10], code)
        if key in existing:
            print(f"  {code}: {key[0]} 已记录，跳过")
            continue
        e = rec.get("side_expectancy") or {}
        rows.append({
            "recorded_at": datetime.now().isoformat(timespec="seconds"),
            "as_of": key[0], "code": code,
            "action": rec.get("action"),
            "target_position": rec.get("target_position"),
            "position_state": (rec.get("signal") or {}).get("position_state"),
            "review_date": rec.get("review_date"),
            "horizon": (rec.get("horizon_days") or [None])[0],
            "close": dd.get("last_close"),
            "exp_mean": e.get("mean"), "exp_hit": e.get("hit_rate"),
            "exp_n": e.get("n"), "exp_t": e.get("t_stat"), "exp_ok": e.get("ok"),
            "status": rec.get("status"),
        })
        print(f"  {code}: {rec.get('action')} target={rec.get('target_position')} "
              f"close={dd.get('last_close')} review={rec.get('review_date')}")
    if rows:
        with LOG.open("a", encoding="utf-8", newline="") as f:
            w = csv.DictWriter(f, fieldnames=FIELDS)
            if new_file:
                w.writeheader()
            w.writerows(rows)
    print(f"新增 {len(rows)} 行 → {LOG}")


def _load_close(code):
    import pandas as pd
    p = SRC / "stock_analysis" / "data" / f"stock_{code}_qfq.csv"
    if not p.exists():
        return None
    return pd.read_csv(p, index_col=0, parse_dates=True)["close"]


def evaluate():
    if not LOG.exists():
        print(f"没有台账：{LOG}")
        return
    import pandas as pd
    out = []
    with LOG.open(encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            close = _load_close(row["code"])
            if close is None:
                continue
            try:
                start = float(row["close"])
                as_of = pd.Timestamp(row["as_of"])
                h = int(row["horizon"] or 20)
                after = close.loc[close.index > as_of]
            except Exception:  # noqa: BLE001
                continue
            realized = (float(after.iloc[-1]) / start - 1.0) if len(after) else None
            matured = len(after) >= h
            h_ret = (float(after.iloc[h - 1]) / start - 1.0) if matured else None
            hit = None
            if h_ret is not None:
                if row["action"] == "buy":
                    hit = h_ret > 0
                elif row["action"] == "sell":
                    hit = h_ret < 0
            out.append({**row, "bars_elapsed": len(after), "matured": matured,
                        "realized_return": None if realized is None else round(realized, 6),
                        "horizon_return": None if h_ret is None else round(h_ret, 6),
                        "hit": hit})
    EVAL.parent.mkdir(exist_ok=True)
    if out:
        with EVAL.open("w", encoding="utf-8", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(out[0]))
            w.writeheader()
            w.writerows(out)
    done = [r for r in out if r["matured"]]
    hits = [r for r in done if r["hit"] is True]
    print(f"共 {len(out)} 条，已到期 {len(done)} 条，方向命中 {len(hits)} 条 → {EVAL}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=["record", "evaluate"])
    ap.add_argument("--codes", default="", help="逗号分隔；默认 14 只")
    a = ap.parse_args()
    codes = [c.strip() for c in a.codes.split(",") if c.strip()] or DEFAULT_CODES
    record(codes) if a.mode == "record" else evaluate()


if __name__ == "__main__":
    main()
