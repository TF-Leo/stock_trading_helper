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
    python tools/forward_log.py record --allow-non-trading-day # 休市/日历不可用时强制写入
    python tools/forward_log.py record --overwrite             # 显式允许覆盖同 (日期,标的) 既有记录
    python tools/forward_log.py record --as-of 2026-11-12      # 历史回放：透传 cli.py --as-of
    python tools/forward_log.py evaluate                       # CSV + JSON 台账结算
    python tools/forward_log.py report                         # 汇总（可加 --json）

产出（均在 evidence/ 下，已 gitignore）
    forward_log.csv     每次 record 追加一行（同一标的同一天只记一次）
    forward_eval.csv    每次 evaluate 覆盖写，含已到期与未到期两种状态

JSON 台账（``source/stock_analysis/data/forward/``，``data/`` 已 gitignore）
    <date>.json         当日全部记录。**文件日期 = 建议日期**（``--as-of`` >
                        ``request.as_of`` > 运行日；issue #134），不是
                        ``data.end``。同一 (日期, 标的) 已有记录时**默认拒绝**
                        并退出 6，需显式 ``--overwrite`` 才覆盖；覆盖时旧记录
                        摘要在新记录的 ``superseded`` 里留痕（不丢历史）。
                        ``data.end`` 落后于建议日期时记录里带
                        ``data_lag_calendar_days``，并在输出与 report 中可见。
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from datetime import date, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "source"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from stock_analysis import forward_ledger as ledger  # noqa: E402
from market_state import MARKET_CLOSED_EXIT_CODE  # noqa: E402

EVID = ROOT / "evidence"
LOG = EVID / "forward_log.csv"
EVAL = EVID / "forward_eval.csv"

# issue #119：休市日 CLI 返回 status=market_closed，其 as_of 仍是上一交易日，
# 直接写入会覆盖那一天的 CSV / JSON 台账（含已结算数据）。退出码只在这里定义
# 一次，且与 ``market_state.MARKET_CLOSED_EXIT_CODE`` 同源（P3-3）。
EXIT_NON_TRADING_DAY = MARKET_CLOSED_EXIT_CODE
# 日历不可用时同样拒绝写入，复用同一退出码（PR #120 复验 P2）。
EXIT_MARKET_STATE_UNKNOWN = EXIT_NON_TRADING_DAY
# issue #134：同 (日期, 标的) 已有记录、未加 --overwrite 时拒绝写入。必须用
# **独立退出码 6**：复用 1 会和「未捕获异常 / 崩溃」混同，消费脚本（
# forward_ledger_daily.py）只能看到 ``record exit=1``，事后无法区分「已记录」
# 与「真崩了」。10/20/30/40/50 是 cli.py 的状态码，6 落在它们之外且不与
# 仓库既有约定冲突。
EXIT_DUPLICATE_RECORD = 6


class NonTradingDayError(RuntimeError):
    """官方交易日历确认今日休市时，``record`` 拒绝写入。"""

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


def _market_day_state(day=None):
    """今日交易日状态（issue #119）。测试通过 monkeypatch 本函数离线注入。

    生产路径只认官方交易日历（``status.market_day_state``：先读磁盘缓存，
    缺失时才请求深交所整月文件），不回退「周一至周五 = 交易日」。
    """
    from stock_analysis.data_loader import status as _status
    return _status.market_day_state(day or datetime.now().date())


def _as_date(value):
    """``date`` / ``datetime`` / ``YYYYMMDD`` / ``YYYY-MM-DD`` → ``date``。

    只用于交易日历闸门（``market_day_state`` 要 ``date``）。``--as-of`` 回放时
    校验的是**目标写入日**，不是运行日：周末回放一个历史交易日不应被拒。
    """
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value)
    if len(text) == 8 and text.isdigit():
        return datetime.strptime(text, "%Y%m%d").date()
    return datetime.strptime(text[:10], "%Y-%m-%d").date()


def _check_trading_day(allow_non_trading_day=False, day=None):
    """休市**或日历不可用**时拒绝 ``record``；显式放行时打印警告。

    * ``closed is True``  → 抛 :class:`NonTradingDayError`（退出码 50）；
    * ``closed is None``  → 官方日历不可用，**无法确认是否交易日**：同样默认
      拒绝（退出码 50）；无法确认建议日期落在哪一天时，宁可零落盘；
    * ``closed is False`` → 交易日，正常写入。

    只有显式 ``--allow-non-trading-day`` 才放行。issue #134 起写入日期是
    **建议日期**（``--as-of`` / ``request.as_of``，缺省运行日），休市日写入
    不再借用 ``data.end`` 落到上一交易日；但非交易日仍会写出一个
    ``market_closed`` 记录文件，故保留 #119 的默认拒绝。``day`` 是待校验的
    **目标写入日**（``--as-of`` 优先），不是「今天」。放行时打印醒目警告，
    并说明覆盖保护仍由 (日期, 标的) 去重把关。
    """
    state = _market_day_state(day)
    if state.closed is False:
        return state
    if not allow_non_trading_day:
        if state.closed is True:
            raise NonTradingDayError(
                "今日休市（官方交易日历：非交易日"
                f"，来源 {state.source}），record 已拒绝写入：休市日产生的 "
                "status=market_closed 记录不应写进任何交易日的台账。如确需"
                "写入，请显式加 --allow-non-trading-day。")
        raise NonTradingDayError(
            "日历不可用，无法确认是否交易日"
            f"（{state.warning or state.source}），record 已拒绝写入：无法确认 "
            "建议日期会落在哪一天，继续写可能污染既有台账。如确需写入，请显式"
            "加 --allow-non-trading-day。")

    write_day = str(day or datetime.now().date())[:10]
    reason = ("官方交易日历确认今日休市"
              if state.closed is True
              else f"交易日历不可用（{state.warning or state.source}）")
    print(f"[forward_log] !!! 警告：{reason}，已按 --allow-non-trading-day "
          f"强制写入；写入日期为建议日期（--as-of / payload.request.as_of "
          f"优先，缺省为运行日 {write_day}）。同一 (日期, 标的) 已有记录时"
          "默认仍会拒绝，需显式加 --overwrite。", file=sys.stderr)
    return state


def record(codes, ledger_dir=None, ledger_only=False, runner=None,
           allow_non_trading_day=False, day=None, overwrite=False,
           as_of=None):
    """记录当日建议：JSON 台账（issue #56）+ CSV（旧行为，同一份 CLI 输出派生）。

    每只票只跑一次 CLI：``collect_record`` 同时返回台账记录与原始 payload，
    CSV 行从 payload 取旧字段，台账行取新字段。CSV 仍按 ``(as_of, code)`` 去重。

    issue #134：落盘日期 = **建议日期**（``as_of`` > ``request.as_of`` >
    运行日），不再取 ``data.end``；``data.end`` 更早时记录带
    ``data_lag_calendar_days`` 并在输出里打印。同一 ``(日期, 标的)`` 已有记录
    时默认抛 :class:`forward_ledger.DuplicateRecordError`（不写任何文件），
    只有 ``overwrite=True``（CLI ``--overwrite``）才替换；被替换的旧记录摘要
    保留在 ``superseded``，已结算数值也不丢。

    ``as_of``（CLI ``--as-of``）同时透传给 ``cli.py --as-of``，让回放真正按
    该日期截断行情，而不只是改文件名。

    issue #119：默认先查官方交易日历，``closed is True``（休市）或
    ``closed is None``（日历不可用、无法确认）都抛
    :class:`NonTradingDayError`（不开文件、不跑 CLI）；只有
    ``allow_non_trading_day`` 显式放行并打印警告。
    ``day`` 仅测试注入（同时作为建议日期的兜底），生产路径不得传。
    """
    # 闸门校验的是**目标写入日**：`--as-of` 回放优先，否则 `day`（测试注入）
    # 或运行日。回放历史交易日不应因为「今天恰好是周末」被拒。
    _check_trading_day(allow_non_trading_day,
                       _as_date(as_of) if as_of else day)
    existing = set()
    if LOG.exists():
        with LOG.open(encoding="utf-8", newline="") as f:
            for row in csv.DictReader(f):
                existing.add((row["as_of"], row["code"]))
    if not ledger_only:
        EVID.mkdir(exist_ok=True)

    grouped, payloads = {}, {}
    for code in codes:
        rec, payload = ledger.collect_record(code, as_of=as_of, runner=runner,
                                             now=day)
        grouped.setdefault(rec["as_of"], {})[rec["code"]] = rec
        payloads[rec["code"]] = payload

    # issue #134：先整体检查冲突，再写任何文件。默认拒绝；显式 --overwrite
    # 时对「已有已结算记录」逐条告警 —— 旧结算数值会保留进 superseded[]，
    # 但仍应让操作员知道本次替换掉了已结算结论。
    conflicts = ledger.find_conflicts(grouped, ledger_dir=ledger_dir)
    if conflicts and not overwrite:
        raise ledger.DuplicateRecordError(conflicts)
    if conflicts:
        for day_key, code in conflicts:
            old = ((ledger.load_day(day_key, ledger_dir) or {}).get("records")
                   or {}).get(code) or {}
            if ledger.has_settlement(old):
                print(f"[forward_log] !!! 警告：{day_key}/{code} 已有已结算记录，"
                      "--overwrite 将用本次结果替换；旧记录（含结算数值）"
                      "已保留在该记录的 superseded[]。", file=sys.stderr)

    written = ledger.write_records(grouped, ledger_dir=ledger_dir,
                                   overwrite=overwrite)
    ledger_codes = {code for recs in written.values() for code in recs}
    print(f"JSON 台账：{len(ledger_codes)} 条 → "
          f"{', '.join(sorted(written)) or '（无）'} @ "
          f"{ledger.ledger_dir_path(ledger_dir)}")
    lagged = []
    for day_key, recs in sorted(written.items()):
        for code, rec in sorted(recs.items()):
            missing = len(rec.get("missing") or {})
            lag = rec.get("data_lag_calendar_days")
            lag_note = ""
            if isinstance(lag, int) and lag > 0:
                lag_note = (f"（data_end={rec.get('data_end')}，"
                            f"data_lag_calendar_days={lag}）")
                lagged.append((day_key, code, lag))
            print(f"  {code}: {rec.get('action')} status={rec.get('status')} "
                  f"confidence={rec.get('confidence')} as_of={day_key}"
                  + (f"（{missing} 个字段 null + reason）" if missing else "")
                  + lag_note)
    if lagged:
        # issue #134：数据滞后必须可见 —— 否则「基金净值还没入库」只能靠
        # 事后翻台账发现（这正是这次误报成「只记了 2/10」的原因）。
        print(f"[forward_log] 数据滞后：{len(lagged)} 条记录的 data.end 早于建议"
              f"日期（最大 {max(lag for _, _, lag in lagged)} 个自然日）—— "
              "记录仍按建议日期落盘，不写进前一交易日。")
    if ledger_only:
        return written

    added = 0
    new_file = not LOG.exists()
    for day, recs in sorted(written.items()):
        for code, rec in sorted(recs.items()):
            if (day, code) in existing:
                # CSV 是 append-only 的旧口径：即使 --overwrite 也只跳过追加，
                # 不回写历史行（JSON 台账才是正式口径）。
                print(f"  {code}: {day} CSV 已有该行，跳过追加"
                      "（JSON 台账已按建议日期写入）")
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
    # issue #124：行情缓存目录必须可注入（测试把 ``forward_ledger.DATA_DIR``
    # 指向临时目录）。直接拼 ``SRC/stock_analysis/data`` 会让测试读到/写到
    # 开发者本机的真实缓存；``ledger.DATA_DIR`` 的默认值就是同一个目录。
    p = Path(ledger.DATA_DIR) / f"stock_{code}_qfq.csv"
    if not p.exists():
        return None
    return pd.read_csv(p, index_col=0, parse_dates=True)["close"]


def evaluate(ledger_dir=None):
    """CSV 结算（旧行为）+ JSON 台账结算（issue #56）。"""
    stats = ledger.settle_all(ledger_dir=ledger_dir)
    # PR #96 复验 P2-2：reasons 现在同时含 pending 与 failed 的原因，直接拼在
    # 「结算失败 N 条」后面会自相矛盾（失败 0 条却挂 10 条原因）。按状态分开打印，
    # 且每一行只挂自己那一类的原因。
    line = (f"JSON 台账结算：{stats['days']} 天，已到期 {stats['settled']} 条，"
            f"未到期 {stats['not_matured']} 条，待结算 pending {stats['pending']} 条，"
            f"结算失败 {stats['failed']} 条")
    if stats["failed_reasons"]:
        line += f"（失败原因：{stats['failed_reasons']}）"
    print(line)
    if stats["pending_reasons"]:
        print(f"  pending 原因：{stats['pending_reasons']}")
    # issue #99：口径未确认 → 确认的重判必须可见，否则「记录时钉错来源又悄悄
    # 改掉」没人能复核。只列 code，依据文本在台账的 instrument_basis 里。
    if stats.get("rejudged"):
        print(f"  口径重判（记录时无可用缓存 → 已确认）：{', '.join(stats['rejudged'])}")
    for warning in stats.get("warnings") or []:
        print(f"  [WARN] {warning}")
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


def _as_of_arg(value):
    """``--as-of`` 的入参校验（与 ``cli.py`` 同两种写法：YYYYMMDD / YYYY-MM-DD）。"""
    text = str(value).strip()
    shaped = ((len(text) == 10 and text[4] == "-" and text[7] == "-")
              or (len(text) == 8 and text.isdigit()))
    if shaped:
        for fmt in ("%Y-%m-%d", "%Y%m%d"):
            try:
                datetime.strptime(text, fmt)
                return text
            except ValueError:
                continue
    raise argparse.ArgumentTypeError(
        f"--as-of 需要 YYYY-MM-DD 或 YYYYMMDD 格式的有效日期，收到 {value!r}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=["record", "evaluate", "report"])
    ap.add_argument("--codes", default="", help="逗号分隔；默认 14 只")
    ap.add_argument("--ledger-dir", default=None,
                    help="JSON 台账目录（默认 source/stock_analysis/data/forward）")
    ap.add_argument("--ledger-only", action="store_true",
                    help="record 时只写 JSON 台账，不写 evidence/forward_log.csv")
    ap.add_argument("--allow-non-trading-day", action="store_true",
                    help=("record 在休市或日历不可用时仍强制写入（默认拒绝，"
                          f"退出码 {EXIT_NON_TRADING_DAY}），并打印警告"))
    ap.add_argument("--overwrite", action="store_true",
                    help=("record 时允许覆盖同 (日期, 标的) 既有记录"
                          f"（默认拒绝，退出码 {EXIT_DUPLICATE_RECORD}）；"
                          "被替换的旧记录摘要保留在 superseded[]"))
    ap.add_argument("--as-of", dest="as_of", default=None, metavar="YYYY-MM-DD",
                    type=_as_of_arg,
                    help=("record 的历史回放日期：透传 cli.py --as-of（截断行情）"
                          "并作为台账写入日期；缺省为运行日"))
    ap.add_argument("--json", action="store_true", help="report 输出机读 JSON")
    a = ap.parse_args()
    if a.as_of and a.mode != "record":
        ap.error("--as-of 只用于 record")
    codes = [c.strip() for c in a.codes.split(",") if c.strip()] or DEFAULT_CODES
    if a.mode == "record":
        try:
            record(codes, ledger_dir=a.ledger_dir, ledger_only=a.ledger_only,
                   allow_non_trading_day=a.allow_non_trading_day,
                   overwrite=a.overwrite, as_of=a.as_of)
        except NonTradingDayError as exc:
            print(f"[forward_log] {exc}", file=sys.stderr)
            sys.exit(EXIT_NON_TRADING_DAY)
        except ledger.DuplicateRecordError as exc:
            print(f"[forward_log] {exc}", file=sys.stderr)
            sys.exit(EXIT_DUPLICATE_RECORD)
    elif a.mode == "evaluate":
        evaluate(ledger_dir=a.ledger_dir)
    else:
        report(ledger_dir=a.ledger_dir, as_json=a.json)


if __name__ == "__main__":
    main()
