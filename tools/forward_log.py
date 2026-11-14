# -*- coding: utf-8 -*-
"""前向验证台账：今天记下建议，20 个交易日后用真实行情结算。

为什么需要它
------------
现有全部结论都是**样本内**的（14 只票、2018 起）。唯一能证伪"20 日有效期"的办法
是把今天的建议冻住，等 20 个交易日后再看真实结果。这个脚本只做两件事：

    record   记录当前建议（每个标的：动作/目标仓位/复审日/当时收盘/买侧期望）
    evaluate 结算已到期的记录（horizon 个交易日后的实际收益，按方向记对错）

issue #56 起，``record`` / ``evaluate`` 同时维护 **JSON 前向台账**
（``source/stock_analysis/data/forward/<YYYY-MM-DD>.json``），字段与结算口径见
``stock_analysis/forward_ledger.py``；新增 ``report`` 汇总已到期条数、命中率、
confidence 分箱校准与按 ``flow.caliber`` / 估值分位的分层命中率（R4 C 路径）。

用法
----
    python tools/forward_log.py record
    python tools/forward_log.py record --codes 002415,600519   # 只记两只
    python tools/forward_log.py record --ledger-only           # 只写 JSON 台账
    python tools/forward_log.py evaluate                       # CSV + JSON 台账结算
    python tools/forward_log.py report                         # 汇总（可加 --json）

产出（均在 evidence/ 下，已 gitignore）
    forward_log.csv     每次 record 追加一行（同一标的同一天只记一次）
    forward_eval.csv    每次 evaluate 覆盖写，含已到期与未到期两种状态

JSON 台账（``source/stock_analysis/data/forward/``，``data/`` 已 gitignore）
    <date>.json         当日全部记录；同一天重复跑**覆盖**同 code，不追加。
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "source"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from stock_analysis import forward_ledger as ledger  # noqa: E402

EVID = ROOT / "evidence"
LOG = EVID / "forward_log.csv"
EVAL = EVID / "forward_eval.csv"

DEFAULT_CODES = ["000858", "002270", "002371", "002415", "002594", "300476",
                 "300750", "600036", "600519", "600522", "601318", "601398",
                 "603259", "688981"]

FIELDS = ["recorded_at", "as_of", "code", "action", "target_position",
          "position_state", "review_date", "horizon", "close", "exp_mean",
          "exp_hit", "exp_n", "exp_t", "exp_ok", "status"]


def _append_csv(row, new_file):
    EVID.mkdir(exist_ok=True)
    with LOG.open("a", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDS)
        if new_file:
            writer.writeheader()
        writer.writerow(row)


def record(codes, ledger_dir=None, ledger_only=False, runner=None):
    """记录当日建议：JSON 台账（issue #56）+ CSV（旧行为，同一份 CLI 输出派生）。

    每只票只跑一次 CLI：``collect_record`` 同时返回台账记录与原始 payload，
    CSV 行从 payload 取旧字段，台账行取新字段。CSV 仍按 ``(as_of, code)`` 去重。
    """
    existing = set()
    if LOG.exists():
        with LOG.open(encoding="utf-8", newline="") as f:
            for row in csv.DictReader(f):
                existing.add((row["as_of"], row["code"]))
    if not ledger_only:
        EVID.mkdir(exist_ok=True)

    grouped, payloads = {}, {}
    for code in codes:
        rec, payload = ledger.collect_record(code, runner=runner)
        grouped.setdefault(rec["as_of"], {})[rec["code"]] = rec
        payloads[rec["code"]] = payload
    written = ledger.write_records(grouped, ledger_dir=ledger_dir)
    ledger_codes = {code for recs in written.values() for code in recs}
    print(f"JSON 台账：{len(ledger_codes)} 条 → "
          f"{', '.join(sorted(written)) or '（无）'} @ "
          f"{ledger.ledger_dir_path(ledger_dir)}")
    for day, recs in sorted(written.items()):
        for code, rec in sorted(recs.items()):
            missing = len(rec.get("missing") or {})
            print(f"  {code}: {rec.get('action')} status={rec.get('status')} "
                  f"confidence={rec.get('confidence')} as_of={day}"
                  + (f"（{missing} 个字段 null + reason）" if missing else ""))
    if ledger_only:
        return written

    added = 0
    new_file = not LOG.exists()
    for day, recs in sorted(written.items()):
        for code, rec in sorted(recs.items()):
            if (day, code) in existing:
                print(f"  {code}: {day} CSV 已记录，跳过 CSV（台账已覆盖）")
                continue
            payload = payloads.get(code)
            if not isinstance(payload, dict):
                print(f"  {code}: 无 CLI JSON（exit={rec.get('cli_exit')}），"
                      "台账已留痕，CSV 跳过")
                continue
            row = _csv_row_from_payload(payload, day, code, rec)
            _append_csv(row, new_file=new_file)
            new_file = False
            added += 1
    if added or not new_file:
        print(f"新增 {added} 行 CSV → {LOG}")
    else:
        # 一行都没写就不留空文件（否则下次跑会以为台账已存在）
        print("没有可写入 CSV 的行（CLI 全部失败或无 JSON），未创建 CSV 台账")
    return written


def _csv_row_from_payload(payload, day, code, rec):
    """旧 CSV 行：字段来源与 issue #56 之前逐字一致。"""
    rec_block = payload.get("recommendation") or {}
    data = payload.get("data") or {}
    e = rec_block.get("side_expectancy") or {}
    try:
        horizon = (rec_block.get("horizon_days") or [None])[0]
    except Exception:
        horizon = None
    return {
        "recorded_at": rec.get("recorded_at")
        or datetime.now().isoformat(timespec="seconds"),
        "as_of": day, "code": code,
        "action": rec_block.get("action"),
        "target_position": rec_block.get("target_position"),
        "position_state": (rec_block.get("signal") or {}).get("position_state"),
        "review_date": rec_block.get("review_date"),
        "horizon": horizon,
        "close": data.get("last_close"),
        "exp_mean": e.get("mean"), "exp_hit": e.get("hit_rate"),
        "exp_n": e.get("n"), "exp_t": e.get("t_stat"), "exp_ok": e.get("ok"),
        "status": rec_block.get("status"),
    }


def _load_close(code):
    import pandas as pd
    p = SRC / "stock_analysis" / "data" / f"stock_{code}_qfq.csv"
    if not p.exists():
        return None
    return pd.read_csv(p, index_col=0, parse_dates=True)["close"]


def evaluate(ledger_dir=None):
    """CSV 结算（旧行为）+ JSON 台账结算（issue #56）。"""
    stats = ledger.settle_all(ledger_dir=ledger_dir)
    print(f"JSON 台账结算：{stats['days']} 天，已到期 {stats['settled']} 条，"
          f"未到期 {stats['not_matured']} 条，数据未到 pending {stats['pending']} 条，"
          f"结算失败 {stats['failed']} 条"
          + (f"（{stats['reasons']}）" if stats["reasons"] else ""))
    if not LOG.exists():
        print(f"没有 CSV 台账：{LOG}")
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


def report(ledger_dir=None, as_json=False):
    data = ledger.build_report(ledger_dir=ledger_dir)
    if as_json:
        print(json.dumps(data, ensure_ascii=False, indent=2))
    else:
        print(ledger.format_report(data))
    return data


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=["record", "evaluate", "report"])
    ap.add_argument("--codes", default="", help="逗号分隔；默认 14 只")
    ap.add_argument("--ledger-dir", default=None,
                    help="JSON 台账目录（默认 source/stock_analysis/data/forward）")
    ap.add_argument("--ledger-only", action="store_true",
                    help="record 时只写 JSON 台账，不写 evidence/forward_log.csv")
    ap.add_argument("--json", action="store_true", help="report 输出机读 JSON")
    a = ap.parse_args()
    codes = [c.strip() for c in a.codes.split(",") if c.strip()] or DEFAULT_CODES
    if a.mode == "record":
        record(codes, ledger_dir=a.ledger_dir, ledger_only=a.ledger_only)
    elif a.mode == "evaluate":
        evaluate(ledger_dir=a.ledger_dir)
    else:
        report(ledger_dir=a.ledger_dir, as_json=a.json)


if __name__ == "__main__":
    main()
