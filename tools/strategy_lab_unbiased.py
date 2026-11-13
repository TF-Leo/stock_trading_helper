#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""R10 / issue #86：在**无偏日线池**上重跑 R6 的 13 个预注册候选。

任务书：GitHub issue #86。方法冻结自 ``docs/STRATEGY_REDESIGN_V1.md``（R6 / #70），
**候选、参数、成本、判定门槛一律不变**；唯一变化是票池从「沪深 300 当前成分」
（14.4pp/年幸存者偏差）换成 R9 的无偏池（全 A + 退市，5,471 票）。

三个子命令
----------
``fetch``    联网（baostock，单会话严格串行、可断点续跑）重建无偏日线池。
             每票**一次请求**同时落两份：
             * ``csv/<code>.csv``  —— 与 R9 完全相同的字段
               （``date,code,open,high,low,close,volume,amount,tradestatus,pctChg,isST``），
               **md5 与 R9 manifest 逐字节一致**，用 ``verify`` 校验；
             * ``val/<code>.csv``  —— 在上面基础上加 ``turn,peTTM,pbMRQ,psTTM``，
               供 ``value_trend_h20`` 的 pb 分位使用（R9 数据集没有估值列）。
             另取沪深 300 指数（``index/sh000300.csv``）作为基准。
``verify``   离线：逐票比对 ``csv/`` 与参考 manifest 的 ``csv_md5 / rows / first / last``，
             报告一致/不一致票数；不一致的票可用 ``fetch --codes ... --force`` 重抓。
``analyze``  完全离线：按日动态池 + 退市结算，跑 13 个候选与三类基准。

为什么是「一次请求落两份」
--------------------------
R9 的取数字段不含估值；若先建 R9 池再单独补估值，等于把 6.8 小时的串行取数做两遍。
实测「超集字段取一次 → 投影回 R9 字段」产出的 CSV 与 R9 **md5 逐字节相同**
（见 ``docs/STRATEGY_REDESIGN_V2.md`` §2.2），因此合并为一次请求不改变任何数据。

无偏池使用规则（issue #86 §方法）
---------------------------------
1. **退市票可结算**：用 R9 的 ``last_tradestatus1_close`` 作为退出价；
   ``exit_reason == "merger"`` 的 24 只**先排除**并在报告中单列。
2. **按日动态池**：某日只含当日已上市（``ipo_date``）且未退市（``out_date``）的票。
3. **停牌不可成交**：``tradestatus == 0`` 的行不能作为买/卖执行价。

用法
----
    python tools/strategy_lab_unbiased.py fetch --data-dir ~/data/stock-universe
    python tools/strategy_lab_unbiased.py verify --data-dir ~/data/stock-universe \\
        --reference ~/data/stock-universe/reference-manifest-v85.json
    python tools/strategy_lab_unbiased.py analyze --data-dir ~/data/stock-universe \\
        --random-reps 200

退出码：0 = 成功；1 = 数据缺失/校验不一致；2 = 参数或依赖错误；
        3 = 重新登录失败（断点已存）；4 = 看门狗触发（断点已存）。
"""
from __future__ import annotations

import argparse
import glob
import hashlib
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

DEFAULT_DATA_DIR = os.path.expanduser("~/data/stock-universe")

#: 与 R9 ``build_universe.py::FIELDS`` 完全一致（顺序也一致，md5 才可比）。
BASE_FIELDS = ("date,code,open,high,low,close,volume,amount,"
               "tradestatus,pctChg,isST")
#: 超集：多取估值与换手，投影后与 BASE_FIELDS 逐字节一致。
SUPER_FIELDS = BASE_FIELDS + ",turn,peTTM,pbMRQ,psTTM"
VALUE_COLUMNS = ("turn", "peTTM", "pbMRQ", "psTTM")

DEFAULT_START = "2018-01-01"
#: 取数区间**额外向前多取**一段作为指标预热期（R6 用 2016-06-01 起、评估从
#: 2018-01 起，预留 252+ 根）。R9 数据集从 2018-01-02 开始，直接用它评估会让
#: ``xsmom``（需 252+21 根）在 2018 全年无票可选，且各候选有效起点不一致，
#: 与 R6 不可比。实测加长区间**不改变** baostock 前复权数值（见报告 §2.4），
#: 因此可以「多取预热、落盘时截到 DEFAULT_START」，md5 仍可与 R9 逐字节比对。
WARMUP_START = "2016-06-01"
DEFAULT_END = "2026-11-12"
INDEX_CODE = "sh.000300"
INDEX_FILE = "sh000300.csv"
ADJUST = "2"
MAX_ROWS = 4000

#: 成本（与 ``source/stock_analysis/backtest/engine.py::BacktestConfig`` 同值）。
COMMISSION = 0.0003
SLIPPAGE = 0.0010
STAMP_DUTY = 0.0010
BUY_COST = COMMISSION + SLIPPAGE              # 13bp
SELL_COST = COMMISSION + SLIPPAGE + STAMP_DUTY  # 23bp


# ---------------------------------------------------------------------------
# 预注册候选表（**从 R6 原样复制，禁止按结果修改**）
# ---------------------------------------------------------------------------
#: ``(id, family, horizon, params)``。与 ``tools/strategy_lab.py::CANDIDATES``
#: 逐项相同；``analyze`` 会断言两者一致，防止口径漂移。
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

#: 组合参数（与 R6 冻结值一致）。
TOP_N = 30
MIN_HISTORY = 252
INDEX_MA = 200
DONCHIAN_WINDOW = 55
LOWVOL_WINDOW = 60
RSI_PERIOD = 2
BOLL_WINDOW = 20
BOLL_STD = 2.0
MAX_EXIT_EXTEND = 20
DEFAULT_FETCH_START = "2016-06-01"   # 预留 252+ 根预热（与 R6 同）
RANDOM_REPS = 500
BOOT_REPS = 1000
SEED = 20261113

_ND = NormalDist()


# ---------------------------------------------------------------------------
# 路径 / 小工具
# ---------------------------------------------------------------------------
class Paths:
    def __init__(self, data_dir: str):
        self.data = os.path.abspath(os.path.expanduser(data_dir))
        self.csv = os.path.join(self.data, "csv")
        self.val = os.path.join(self.data, "val")
        self.index = os.path.join(self.data, "index")
        self.universe = os.path.join(self.data, "universe.json")
        self.manifest = os.path.join(self.data, "manifest.json")
        self.report = os.path.join(self.data, "verify-report.json")
        self.log = os.path.join(self.data, "fetch.log")
        self.out = os.path.join(self.data, "out")

    def ensure(self):
        for p in (self.data, self.csv, self.val, self.index, self.out):
            os.makedirs(p, exist_ok=True)


_LOG_PATH = None


def _log(msg: str) -> None:
    line = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    print(line, flush=True)
    if _LOG_PATH:
        try:
            with open(_LOG_PATH, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        except OSError:
            pass


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
    raise TypeError(f"不可序列化的类型：{type(obj).__name__}")


def _write_json(path: str, payload) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=1, default=_json_default)
        fh.write("\n")
    os.replace(tmp, path)


def _read_json(path: str):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def _md5(path: str) -> str:
    h = hashlib.md5()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _git_head() -> str | None:
    try:
        return subprocess.check_output(
            ["git", "-C", ROOT, "rev-parse", "HEAD"], text=True).strip()
    except Exception:  # noqa: BLE001
        return None


def _to_code(bs_code: str) -> str:
    return str(bs_code).split(".")[-1].zfill(6)


def _bs_code(code: str) -> str:
    code = str(code).zfill(6)
    if code[:2] in ("60", "68", "90"):
        return f"sh.{code}"
    if code[:2] in ("00", "30", "20"):
        return f"sz.{code}"
    raise ValueError(f"baostock 不支持 {code}")


def _parse_date(value):
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _assert_r6_parity() -> None:
    """断言候选表与 R6 工具逐项相同（防止口径漂移）。"""
    try:
        import strategy_lab as r6
    except Exception:  # noqa: BLE001
        return
    mine = [(c[0], c[1], c[2], c[3]) for c in CANDIDATES]
    theirs = [(c[0], c[1], c[2], c[3]) for c in r6.CANDIDATES]
    if mine != theirs:
        raise RuntimeError("候选表与 R6 tools/strategy_lab.py::CANDIDATES 不一致")
    if (r6.TOP_N, r6.DONCHIAN_WINDOW, r6.LOWVOL_WINDOW, r6.RSI_PERIOD,
            r6.BOLL_WINDOW, r6.BOLL_STD) != (TOP_N, DONCHIAN_WINDOW,
                                             LOWVOL_WINDOW, RSI_PERIOD,
                                             BOLL_WINDOW, BOLL_STD):
        raise RuntimeError("组合参数与 R6 不一致")
    if (r6.BUY_COST, r6.SELL_COST) != (BUY_COST, SELL_COST):
        raise RuntimeError("成本口径与 R6 不一致")


# ---------------------------------------------------------------------------
# 1) fetch：重建无偏池（复用 R9 的会话/超时/校验机制，不另造一套）
# ---------------------------------------------------------------------------
def _load_bu():
    import build_universe as bu  # noqa: PLC0415
    return bu


def _fetch_once_super(bs, code: str, start: str, end: str, adjust: str,
                      max_rows: int, timeout_s: float):
    bu = _load_bu()
    began = time.time()
    with bu._deadline(timeout_s):
        rs = bs.query_history_k_data_plus(
            code, SUPER_FIELDS, start_date=start, end_date=end,
            frequency="d", adjustflag=adjust)
        if str(rs.error_code) != "0":
            raise bu.QueryError(str(rs.error_code), str(rs.error_msg))
        fields = list(rs.fields)
        rows: list[list[str]] = []
        while rs.next():
            rows.append(rs.get_row_data())
            if len(rows) > max_rows:
                raise bu.SuspiciousData(f"行数超过上限 {max_rows}（疑似重复页缺陷）")
        if str(rs.error_code) != "0":
            raise bu.QueryError(str(rs.error_code), str(rs.error_msg))
    return fields, rows, time.time() - began


def _frame_from(fields: list[str], rows: list[list[str]]) -> pd.DataFrame:
    """与 R9 ``save_series`` 完全相同的清洗顺序（md5 可复现的前提）。"""
    frame = pd.DataFrame(rows, columns=fields)
    for col in fields:
        if col not in ("date", "code"):
            frame[col] = pd.to_numeric(frame[col], errors="coerce")
    frame = frame.drop_duplicates(subset=["date"], keep="last")
    return frame.sort_values("date").reset_index(drop=True)


def _save_two(paths: Paths, code: str, fields: list[str],
              rows: list[list[str]], r9_start: str = DEFAULT_START,
              ) -> tuple[int, int, str, int, int, str, str, str]:
    """落两份：``csv/`` = R9 口径（截到 ``r9_start``），``val/`` = 含预热 + 估值。

    ``csv/`` 必须与 R9 逐字节一致（md5 校验），所以**剔除预热期**；
    ``val/`` 保留预热期，供 ``build_features`` 计算 252 日窗口。
    """
    frame = _frame_from(fields, rows)
    base_cols = BASE_FIELDS.split(",")
    # **先按日期截断、再做数值转换**：pandas 的 dtype 推断看的是整列。预热期里
    # 若有停牌行（volume 为空），整列会被推成 float64，写出 ``2983070.0``；
    # 而 R9 的区间没有这些行，写的是 ``2983070`` —— md5 就对不上了（实测 5 只）。
    # 因此 csv/ 的 dtype 推断必须在与 R9 完全相同的行子集上做。
    di = fields.index("date")
    if r9_start:
        cut_rows = [r for r in rows if str(r[di]) >= r9_start]
    else:
        cut_rows = rows
    base = _frame_from(fields, cut_rows)[base_cols]
    p_base = os.path.join(paths.csv, f"{code}.csv")
    tmp = p_base + ".tmp"
    base.to_csv(tmp, index=False)
    os.replace(tmp, p_base)
    base_rows, base_bytes, base_md5 = len(base), os.path.getsize(p_base), _md5(p_base)

    val = frame[base_cols + list(VALUE_COLUMNS)]
    p_val = os.path.join(paths.val, f"{code}.csv")
    tmp = p_val + ".tmp"
    val.to_csv(tmp, index=False)
    os.replace(tmp, p_val)
    return (base_rows, base_bytes, base_md5, len(val),
            os.path.getsize(p_val), _md5(p_val),
            str(base["date"].iloc[0]), str(base["date"].iloc[-1]))


def _fetch_with_retry(bs, bu, stock: dict, req_start: str, req_end: str,
                      adjust: str, max_rows: int, on_attempt=None):
    last = None
    for attempt in range(bu.RETRY_ATTEMPTS):
        if on_attempt is not None:
            on_attempt()
        try:
            fields, rows, seconds = _fetch_once_super(
                bs, stock["code"], req_start, req_end, adjust, max_rows,
                bu.REQUEST_TIMEOUT_S)
            # 校验用 BASE 字段视图（R9 的 validate_series 只认这些列）
            bi = [fields.index(c) for c in BASE_FIELDS.split(",")]
            base_fields = BASE_FIELDS.split(",")
            base_rows = [[r[i] for i in bi] for r in rows]
            errors, warnings, info = bu.validate_series(
                base_fields, base_rows, code=stock["code"], req_start=req_start,
                req_end=req_end, ipo_date=stock["ipo"],
                out_date=stock["out"] or None, max_rows=max_rows)
            info["seconds"] = round(seconds, 3)
            info["adjustflag"] = adjust
            if errors:
                raise bu.SuspiciousData("; ".join(errors))
            return fields, rows, warnings, info
        except Exception as exc:  # noqa: BLE001
            last = exc
            _log(f"  [retry {attempt + 1}/{bu.RETRY_ATTEMPTS}] {stock['code']}："
                 f"{type(exc).__name__}: {exc}")
            bu.bs_logout(bs)
            time.sleep(bu.RETRY_BACKOFF[min(attempt, len(bu.RETRY_BACKOFF) - 1)])
            try:
                bu.bs_login(bs)
            except Exception as login_exc:  # noqa: BLE001
                raise bu.SessionError(
                    f"重新登录失败（{type(login_exc).__name__}: {login_exc}）"
                ) from login_exc
    raise RuntimeError(f"{type(last).__name__}: {last}")


def cmd_fetch(args) -> int:
    bu = _load_bu()
    bu._assert_single_thread()
    paths = Paths(args.data_dir)
    paths.ensure()
    global _LOG_PATH
    _LOG_PATH = paths.log
    try:
        import baostock as bs
    except ImportError as exc:
        print(f"缺少 baostock：{exc}（pip install -r requirements-research.txt）",
              file=sys.stderr)
        return 2

    started = time.time()
    _log(f"==== fetch 开始 start={args.start} end={args.end} "
         f"limit={args.limit} adjust={args.adjustflag} data={paths.data} ====")

    manifest = {}
    if os.path.exists(paths.manifest):
        try:
            manifest = _read_json(paths.manifest)
        except (OSError, json.JSONDecodeError) as exc:
            _log(f"[WARN] manifest 读取失败（将重建）：{exc}")
    want_hash = bu.config_hash(args.start, args.end, args.adjustflag, bu.MAX_ROWS)
    if manifest.get("config_hash") != want_hash:
        if manifest.get("entries"):
            _log("[WARN] 取数配置与既有 manifest 不同，忽略旧断点（旧 CSV 仍留盘）")
        manifest = bu.empty_manifest(args.start, args.end, args.adjustflag,
                                     bu.MAX_ROWS)
    manifest["config"]["fields"] = SUPER_FIELDS
    manifest["config"]["base_fields"] = BASE_FIELDS
    manifest["run"] = {"started_at": datetime.now().isoformat(timespec="seconds"),
                       "pid": os.getpid(), "argv": sys.argv[1:]}

    bu._install_socket_default_timeout(bu.REQUEST_TIMEOUT_S)
    bu.bs_login(bs)
    try:
        all_stocks = bu.query_stock_basic(bs)
        stocks = bu._window_stocks(all_stocks, args.start, args.end)
        selected, selection = bu.select_stocks(stocks, args)
        _log(f"主表 type=1 共 {len(all_stocks)} 只；窗口内曾上市 {len(stocks)} 只；"
             f"本次选中 {len(selected)} 只（{selection['method']}）")
        if not selected:
            _log("[ERR] 选中票数为 0")
            return 2

        manifest["universe"] = {
            "snapshot_date": date.today().isoformat(),
            "source": "baostock.query_stock_basic()（type=1）",
            "window": {"start": args.start, "end": args.end},
            "type1_total": len(all_stocks),
            "in_window": len(stocks),
            "active_in_window": sum(1 for s in stocks if not s["out"]),
            "delisted_in_window": sum(1 for s in stocks if s["out"]),
        }
        manifest["selection"] = selection
        _write_json(paths.universe, {
            "snapshot_date": date.today().isoformat(),
            "source": "baostock.query_stock_basic()（type=1）",
            "window": {"start": args.start, "end": args.end},
            "counts": manifest["universe"],
            "selection": selection,
            "selected_codes": [s["code"] for s in selected],
            "stocks": stocks,
        })

        # ---- 基准指数（沪深 300，非复权价格指数）----
        idx_path = os.path.join(paths.index, INDEX_FILE)
        if not os.path.exists(idx_path) or args.force:
            idx_start = args.warmup_start or args.start
            fields, rows, _ = _fetch_once_super(
                bs, INDEX_CODE, idx_start, args.end, "3", MAX_ROWS,
                bu.REQUEST_TIMEOUT_S)
            idx_cols = ["date", "open", "high", "low", "close", "volume", "amount"]
            frame = pd.DataFrame(rows, columns=fields)[idx_cols]
            for c in idx_cols[1:]:
                frame[c] = pd.to_numeric(frame[c], errors="coerce")
            frame = frame.drop_duplicates(subset=["date"]).sort_values("date")
            frame.to_csv(idx_path, index=False)
            _log(f"指数 {INDEX_CODE}：{len(frame)} 行 "
                 f"{frame['date'].iloc[0]} ~ {frame['date'].iloc[-1]}")

        entries = manifest.setdefault("entries", {})
        failures = manifest.setdefault("failures", [])
        skipped = ok = failed = 0
        selected_codes = {s["code"] for s in selected}
        manifest["failures"] = [f for f in failures if f.get("code") in selected_codes]
        failures = manifest["failures"]
        last_progress_ts = time.time()

        def _tick(where: str = "") -> None:
            bu._watchdog_check(last_progress_ts, args.watchdog_seconds, where)

        try:
            for i, stock in enumerate(selected, 1):
                code = stock["code"]
                _tick(f"开始处理 {code}")
                entry = entries.get(code)
                p_base = os.path.join(paths.csv, f"{code}.csv")
                p_val = os.path.join(paths.val, f"{code}.csv")
                if (not args.force and entry and entry.get("status") == "ok"
                        and os.path.exists(p_base) and os.path.exists(p_val)
                        and os.path.getsize(p_base) == entry.get("csv_bytes")):
                    skipped += 1
                    last_progress_ts = time.time()
                    if i % 100 == 0 or i == len(selected):
                        _log(f"  进度 {i}/{len(selected)}（跳过 {skipped}，"
                             f"新取 {ok}，失败 {failed}）")
                    continue

                # 请求从预热起点开始（指标需要 252+ 根），落盘时 csv/ 截到 args.start
                warm = _parse_date(args.warmup_start) or _parse_date(args.start)
                req_start = max(warm,
                                _parse_date(stock["ipo"]) or warm)
                req_end = _parse_date(args.end)
                if stock["out"]:
                    d_out = _parse_date(stock["out"])
                    if d_out and d_out < req_end:
                        req_end = d_out
                if req_start > req_end:
                    entries[code] = {"status": "skipped_out_of_window",
                                     "ipo": stock["ipo"], "out_date": stock["out"]}
                    last_progress_ts = time.time()
                    continue

                try:
                    fields, rows, warnings, info = _fetch_with_retry(
                        bs, bu, stock, req_start.isoformat(), req_end.isoformat(),
                        args.adjustflag, args.max_rows,
                        on_attempt=lambda c=code: _tick(f"重试 {c}"))
                    (n_rows, n_bytes, digest, n_val_rows, n_val_bytes,
                     val_digest, base_first, base_last) = _save_two(
                        paths, code, fields, rows, r9_start=args.start)
                    bi = [fields.index(c) for c in BASE_FIELDS.split(",")]
                    base_fields = BASE_FIELDS.split(",")
                    base_rows = [[r[j] for j in bi] for r in rows]
                    exit_meta = bu.delisted_meta(base_fields, base_rows,
                                                 stock["out"] or None)
                    entries[code] = {
                        "status": "ok",
                        "name": stock["name"],
                        "ipo_date": stock["ipo"],
                        "out_date": stock["out"] or None,
                        "request_start": req_start.isoformat(),
                        "request_end": req_end.isoformat(),
                        "adjustflag": args.adjustflag,
                        "rows": n_rows,
                        # first/last 描述**落盘的 csv/**（已截到 args.start），
                        # 不是请求区间（含预热）；否则 verify 的 first 比对必然
                        # 误判，manifest 也与实际文件不符。
                        "first": base_first,
                        "last": base_last,
                        "csv_bytes": n_bytes,
                        "csv_md5": digest,
                        "val_rows": n_val_rows,
                        "val_bytes": n_val_bytes,
                        "val_md5": val_digest,
                        "fetch_seconds": info.get("seconds"),
                        "fetched_at": datetime.now().isoformat(timespec="seconds"),
                        "warnings": warnings,
                        "delisted": exit_meta,
                    }
                    failures[:] = [f for f in failures if f.get("code") != code]
                    ok += 1
                    if warnings:
                        _log(f"  [warn] {code}: {'; '.join(warnings)}")
                except bu.WatchdogFired:
                    raise
                except bu.SessionError as exc:
                    _log(f"[FATAL] {code}: {exc}；保存断点后以退出码 3 退出")
                    manifest["run"]["session_error"] = str(exc)
                    _finalize(manifest, started, selected, skipped, ok, failed)
                    _write_json(paths.manifest, manifest)
                    return 3
                except Exception as exc:  # noqa: BLE001
                    failed += 1
                    entries[code] = {
                        "status": "failed", "name": stock["name"],
                        "ipo_date": stock["ipo"], "out_date": stock["out"] or None,
                        "error": f"{type(exc).__name__}: {exc}",
                        "failed_at": datetime.now().isoformat(timespec="seconds"),
                    }
                    failures.append({"code": code, "name": stock["name"],
                                     "error": f"{type(exc).__name__}: {exc}"})
                    _log(f"[FAIL] {code} {stock['name']}: "
                         f"{type(exc).__name__}: {exc}")

                last_progress_ts = time.time()
                if i % 100 == 0 or i == len(selected):
                    _write_json(paths.manifest, manifest)
                    _log(f"  进度 {i}/{len(selected)}（跳过 {skipped}，"
                         f"成功 {ok}，失败 {failed}）")
                if args.pause:
                    time.sleep(args.pause)
        except bu.WatchdogFired as exc:
            _log(f"[WATCHDOG] {exc}；保存断点后以退出码 4 退出（重跑同一命令即可续）")
            manifest["run"]["watchdog_at"] = datetime.now().isoformat(timespec="seconds")
            manifest["run"]["watchdog_reason"] = str(exc)
            _finalize(manifest, started, selected, skipped, ok, failed)
            _write_json(paths.manifest, manifest)
            return 4
        except KeyboardInterrupt:
            _log("[INTERRUPT] 收到 Ctrl-C，保存断点后退出")
            manifest["run"]["interrupted_at"] = datetime.now().isoformat(timespec="seconds")
            _finalize(manifest, started, selected, skipped, ok, failed)
            _write_json(paths.manifest, manifest)
            return 130

        _finalize(manifest, started, selected, skipped, ok, failed)
        _write_json(paths.manifest, manifest)
        _log(f"==== fetch 结束：选中 {len(selected)}，跳过 {skipped}，成功 {ok}，"
             f"失败 {failed}，墙钟 {(time.time() - started) / 60:.1f} 分钟 ====")
        for f in failures:
            _log(f"  - {f['code']} {f.get('name', '')}: {f['error']}")
        return 0 if (not failures or not args.strict) else 1
    finally:
        bu.bs_logout(bs)


def _finalize(manifest: dict, started: float, selected: list, skipped: int,
              ok: int, failed: int) -> None:
    entries = manifest.get("entries", {})
    ok_entries = [e for e in entries.values() if e.get("status") == "ok"]
    durations = [e.get("fetch_seconds") for e in ok_entries
                 if isinstance(e.get("fetch_seconds"), (int, float))]
    manifest["stats"] = {
        "selected": len(selected), "skipped_resume": skipped,
        "fetched_ok": ok, "failed": failed,
        "entries_ok_total": len(ok_entries),
        "request_seconds_mean": (round(sum(durations) / len(durations), 2)
                                 if durations else None),
        "wall_seconds": round(time.time() - started, 1),
        "rows_total": sum(int(e.get("rows") or 0) for e in ok_entries),
        "bytes_total": sum(int(e.get("csv_bytes") or 0) for e in ok_entries),
        "failed_codes": [f.get("code") for f in manifest.get("failures", [])],
    }


# ---------------------------------------------------------------------------
# 2) verify：与参考 manifest 逐票比对 md5
# ---------------------------------------------------------------------------
def _classify_mismatch(bs, code: str, path: str, ref_entry: dict,
                       start: str, end: str) -> tuple[str, float | None]:
    """md5 不一致时判定原因（联网，唯一严谨的做法）。

    R9 manifest 只存 md5，没有价格，离线无法区分「qfq 快照漂移」与「取数错误」。
    判定分两步：

    1. **本文件与 baostock 当前返回是否逐值相同**。若相同，说明我们的取数没有
       问题，差异只可能来自「R9 快照（2026-11-13）与今天不同」。
    2. 再看本文件的**行数/首末日**是否与 R9 一致：一致 → ``qfq_snapshot_drift``
       （R9 日志 §3 已列明：前复权是快照，后续除权会让整段价格整体平移）；
       不一致 → ``other``（真问题，需重抓）。

    若本文件与当前返回**不**相同 → ``reproduce_failed``，说明我们落盘的内容与
    数据源不符，是真错误。
    """
    try:
        frame = pd.read_csv(path, dtype={"date": str, "code": str})
    except Exception:  # noqa: BLE001
        return "other", None
    shape_ok = (len(frame) == ref_entry.get("rows")
                and str(frame["date"].iloc[0]) == ref_entry.get("first")
                and str(frame["date"].iloc[-1]) == ref_entry.get("last"))
    if bs is None:
        return "unclassified", None
    try:
        rs = bs.query_history_k_data_plus(
            code, "date,close", start_date=start, end_date=end,
            frequency="d", adjustflag="2")
        cur = {}
        while rs.error_code == "0" and rs.next():
            r = rs.get_row_data()
            try:
                cur[str(r[0])] = float(r[1])
            except (TypeError, ValueError):
                continue
    except Exception:  # noqa: BLE001
        return "unclassified", None
    ours = dict(zip(frame["date"].astype(str), pd.to_numeric(frame["close"],
                                                             errors="coerce")))
    common = [d for d in ours if d in cur and np.isfinite(ours[d])]
    if len(common) < 50:
        return "unclassified", None
    ratios = np.array([ours[d] / cur[d] for d in common if cur[d] > 0])
    spread = float(np.nanmax(ratios) - np.nanmin(ratios))
    if spread < 1e-9:
        # 与当前数据源逐值一致 → 我们的取数正确，差异来自 R9 快照
        return ("qfq_snapshot_drift" if shape_ok else "other"), float(
            np.nanmean(ratios))
    return "reproduce_failed", float(np.nanmean(ratios))


def cmd_verify(args) -> int:
    paths = Paths(args.data_dir)
    if not os.path.exists(paths.manifest):
        print(f"缺少 {paths.manifest}；先跑 fetch", file=sys.stderr)
        return 1
    manifest = _read_json(paths.manifest)
    entries = manifest.get("entries", {})
    ref = None
    if args.reference and os.path.exists(args.reference):
        ref = _read_json(args.reference).get("entries", {})

    files = {os.path.basename(p)[:-4]: p
             for p in glob.glob(os.path.join(paths.csv, "*.csv"))}
    bs = None
    if args.check_drift:
        try:
            import baostock as _bs  # noqa: PLC0415
            bu = _load_bu()
            bu._install_socket_default_timeout(bu.REQUEST_TIMEOUT_S)
            bu.bs_login(_bs)
            bs = _bs
        except Exception as exc:  # noqa: BLE001
            print(f"[warn] 联网判定不可用（{type(exc).__name__}: {exc}）；"
                  f"不一致票将标记 unclassified", file=sys.stderr)
    rows = []
    match = mismatch = missing_ref = not_on_disk = 0
    drift = repro_failed = 0
    detail = []
    codes = sorted(set(entries) | set(files) | set(ref or {}))
    for code in codes:
        entry = entries.get(code) or {}
        path = files.get(code)
        if entry.get("status") != "ok" or path is None:
            not_on_disk += 1
            continue
        if ref is None:
            continue
        r = ref.get(code)
        if not r:
            missing_ref += 1
            continue
        md5 = _md5(path)
        same = (md5 == r.get("csv_md5")
                and entry.get("rows") == r.get("rows")
                and entry.get("first") == r.get("first")
                and entry.get("last") == r.get("last"))
        if same:
            match += 1
        else:
            mismatch += 1
            # 区分「qfq 快照漂移」与「真错误」：R9 日志 §3 已列明，前复权是
            # 2026-11-13 的快照，之后若有除权，整段价格会被**整体平移**。
            # 判定：行数/首末日一致、且本文件的 close 与参考的比值是**常数**
            # （>0 且各日相同到 1e-6）→ 属于快照漂移，不是取数错误。
            reason, factor = _classify_mismatch(
                bs, code, path, r,
                str(r.get("first") or DEFAULT_START),
                str(r.get("last") or DEFAULT_END))
            detail.append({"code": code, "md5_ok": md5 == r.get("csv_md5"),
                           "rows": [entry.get("rows"), r.get("rows")],
                           "first": [entry.get("first"), r.get("first")],
                           "last": [entry.get("last"), r.get("last")],
                           "reason": reason, "qfq_factor": factor})
            if reason == "qfq_snapshot_drift":
                drift += 1
            elif reason == "reproduce_failed":
                repro_failed += 1
        rows.append({"code": code, "md5": md5, "ref_md5": r.get("csv_md5"),
                     "rows": entry.get("rows"), "same": same})

    ok_entries = [e for e in entries.values() if e.get("status") == "ok"]
    delisted = [e for e in ok_entries if e.get("delisted")]
    reasons = {}
    for e in delisted:
        reason = (e.get("delisted") or {}).get("exit_reason")
        reasons[reason] = reasons.get(reason, 0) + 1
    report = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "data_dir": paths.data,
        "reference": args.reference if ref is not None else None,
        "csv_on_disk": len(files),
        "manifest_ok": len(ok_entries),
        "manifest_failed": sum(1 for e in entries.values()
                               if e.get("status") == "failed"),
        "md5_match": match, "md5_mismatch": mismatch,
        "mismatch_qfq_snapshot_drift": drift,
        "mismatch_reproduce_failed": repro_failed,
        "mismatch_other": mismatch - drift - repro_failed,
        "not_on_disk_or_not_ok": not_on_disk, "missing_in_reference": missing_ref,
        "rows_total": sum(int(e.get("rows") or 0) for e in ok_entries),
        "bytes_total": sum(int(e.get("csv_bytes") or 0) for e in ok_entries),
        "delisted_checked": len(delisted), "exit_reason_counts": reasons,
        "mismatch_detail": detail[:50],
    }
    if bs is not None:
        try:
            _load_bu().bs_logout(bs)
        except Exception:  # noqa: BLE001
            pass
    _write_json(paths.report, report)
    print(json.dumps({k: v for k, v in report.items()
                      if k not in ("mismatch_detail",)},
                     ensure_ascii=False, indent=2))
    if mismatch or not_on_disk:
        print(f"\n不一致 {mismatch} 只、未落盘 {not_on_disk} 只；"
              f"明细见 {paths.report}", file=sys.stderr)
        return 1
    return 0


# ---------------------------------------------------------------------------
# 3) analyze：无偏池上的组合回测（复用 R6 的调度/成本/判定实现）
# ---------------------------------------------------------------------------
def load_unbiased(paths: Paths, start: str, end: str):
    """读无偏池 → 日历对齐矩阵 + 按日动态池掩码 + 退市结算信息。

    与 R6 ``load_data`` 的区别（全部来自 issue #86 的无偏规则）：
    * 票池是**全期 5,471 只**，不是某个快照的 300 只；
    * 每票有 ``ipo_date`` / ``out_date``，据此构造**按日动态池**（当日已上市且
      未退市），不得用全期固定池；
    * 退市票用 ``last_tradestatus1_close`` 结算（``exit_reason=merger`` 的 24 只
      先排除）。
    """
    if not os.path.exists(paths.manifest):
        raise RuntimeError(f"缺少 {paths.manifest}；先跑 fetch")
    manifest = _read_json(paths.manifest)
    entries = {k: v for k, v in manifest.get("entries", {}).items()
               if v.get("status") == "ok"}
    if not entries:
        raise RuntimeError("manifest 里没有 status=ok 的票")

    idx_path = os.path.join(paths.index, INDEX_FILE)
    if not os.path.exists(idx_path):
        raise RuntimeError(f"缺少指数缓存 {idx_path}")
    index = pd.read_csv(idx_path, parse_dates=["date"]).set_index("date").sort_index()
    # 日历必须覆盖**预热期**：特征（xsmom 需 252+21 根、MA200 需 200 根）在
    # 评估起点之前就要有数据。评估窗口由 feats["eval_start"] 控制，不靠裁日历。
    # 指数本身从 2016-06 起有数据（fetch 按 warmup-start 取）。
    dates = pd.DatetimeIndex(index.index)
    dates = dates[dates <= pd.Timestamp(end)]
    if len(dates) < 300:
        raise RuntimeError(f"日历过短：{len(dates)} 天")

    excluded_merger = []
    frames, val_frames, kept = {}, {}, []
    for code, entry in sorted(entries.items()):
        meta = entry.get("delisted") or {}
        if meta.get("exit_reason") == "merger":
            excluded_merger.append({"code": code, "name": entry.get("name"),
                                    "out_date": entry.get("out_date"),
                                    "exit_reason_basis": meta.get("exit_reason_basis"),
                                    "exit_price": meta.get("exit_price")})
            continue
        p_base = os.path.join(paths.csv, f"{code}.csv")
        p_val = os.path.join(paths.val, f"{code}.csv")
        if not os.path.exists(p_base):
            continue
        # 价格矩阵优先用 val/：它含预热期，且是 csv/ 的超集（同样的 R9 字段 +
        # 估值）。csv/ 只用于 md5 校验与 R9 口径核对，不参与特征计算。
        src = p_val if os.path.exists(p_val) else p_base
        try:
            df = pd.read_csv(src, parse_dates=["date"]).set_index("date")
        except Exception:  # noqa: BLE001
            continue
        if not {"open", "close"}.issubset(df.columns) or len(df) < 60:
            continue
        df = df[~df.index.duplicated(keep="last")].sort_index()
        aligned = df.reindex(dates)
        frames[code] = aligned
        val_frames[code] = aligned
        kept.append(code)
    if not kept:
        raise RuntimeError("没有任何可用日线")

    base_cols = ["open", "high", "low", "close", "volume", "amount",
                 "tradestatus", "isST"]
    mat = {}
    for col in base_cols:
        mat[col] = np.column_stack([
            pd.to_numeric(frames[c].get(col, pd.Series(index=dates, dtype=float)),
                          errors="coerce").to_numpy(dtype=float)
            for c in kept])
    for col in VALUE_COLUMNS:
        stack = []
        for c in kept:
            vf = val_frames.get(c)
            if vf is None or col not in vf.columns:
                stack.append(np.full(len(dates), np.nan))
            else:
                stack.append(pd.to_numeric(vf.get(col), errors="coerce")
                             .to_numpy(dtype=float))
        mat[col] = np.column_stack(stack)
    mat["tradestatus"] = np.nan_to_num(mat["tradestatus"], nan=0.0)
    mat["isST"] = np.nan_to_num(mat["isST"], nan=0.0)
    mat["close_ff"] = pd.DataFrame(mat["close"]).ffill().to_numpy()

    # ---- 按日动态池：当日已上市且未退市（issue #86 规则 2）----
    ipo = np.array([_parse_date(entries[c].get("ipo_date")) for c in kept],
                   dtype=object)
    out = np.array([_parse_date(entries[c].get("out_date")) for c in kept],
                   dtype=object)
    listing = np.ones((len(dates), len(kept)), dtype=bool)
    day = dates.date
    for j, c in enumerate(kept):
        d_ipo = ipo[j]
        if d_ipo is not None:
            listing[:, j] &= np.array([d >= d_ipo for d in day])
        d_out = out[j]
        if d_out is not None:
            listing[:, j] &= np.array([d <= d_out for d in day])
    mat["listing"] = listing

    # ---- 退市结算：最后 tradestatus==1 收盘价作为退出价（规则 1）----
    exit_price = np.full(len(kept), np.nan)
    exit_idx = np.full(len(kept), -1, dtype=int)
    for j, c in enumerate(kept):
        meta = entries[c].get("delisted") or {}
        if not meta:
            continue
        px = meta.get("last_tradestatus1_close")
        d_exit = _parse_date(meta.get("last_tradestatus1_date"))
        if px is not None:
            exit_price[j] = float(px)
        if d_exit is not None:
            pos = int(np.searchsorted(dates.values,
                                      np.datetime64(d_exit.isoformat())))
            exit_idx[j] = min(max(pos, 0), len(dates) - 1)

    from decision.instrument import classify_board
    limit = np.zeros(len(kept), dtype=float)
    for j, code in enumerate(kept):
        limit[j] = classify_board(_to_code(code))[1]
    limit_mat = np.where(mat["isST"] > 0.5, 0.05, limit[None, :])
    prev_close = np.vstack([mat["close_ff"][:1], mat["close_ff"][:-1]])
    mat["limit_up"] = prev_close * (1.0 + limit_mat - 0.001)
    mat["limit_down"] = prev_close * (1.0 - limit_mat + 0.001)

    # 停牌（tradestatus==0）不可成交；动态池外的票也不可成交
    open_ = mat["open"]
    tradable = ((mat["tradestatus"] > 0.5) & np.isfinite(open_) & (open_ > 0)
                & listing)
    mat["can_buy"] = tradable & (open_ < mat["limit_up"])
    mat["can_sell"] = tradable & (open_ > mat["limit_down"])

    universe = {
        "snapshot_date": manifest.get("universe", {}).get("snapshot_date"),
        "source": manifest.get("universe", {}).get("source"),
        "codes": kept,
        "n_universe": len(entries),
        "excluded_merger": excluded_merger,
        "n_excluded_merger": len(excluded_merger),
        "n_delisted_used": int(sum(1 for c in kept
                                   if (entries[c].get("delisted") or {})
                                   and (entries[c].get("delisted") or {}).get("exit_reason") != "merger")),
    }
    return {"dates": dates, "codes": kept, "mat": mat, "index": index,
            "universe": universe, "entries": entries,
            "exit_price": exit_price, "exit_idx": exit_idx}




# ---------------------------------------------------------------------------
# 3b) 组合层分析（复用 R6 的调度/成本/统计实现，只换票池与结算规则）
# ---------------------------------------------------------------------------
def _r6():
    import strategy_lab as r6  # noqa: PLC0415
    return r6


def apply_delisting_settlement(data) -> dict:
    """把退市票的「最后可交易日 → 退出价」写进价格矩阵。

    issue #86 规则 1：退市票用 R9 的 ``last_tradestatus1_close`` 结算。
    做法：在该日把 ``open``/``close`` 都置为退出价、允许卖出、之后禁止买入，
    之后的 ``close_ff`` 自然停在退出价 —— 等价于「退出日按退出价平仓，
    冻结期内不产生额外盈亏」，不假设任何可成交价格。
    """
    mat = data["mat"]
    n_dates = len(data["dates"])
    settled = []
    for j, code in enumerate(data["codes"]):
        idx = int(data["exit_idx"][j])
        px = data["exit_price"][j]
        if idx < 0 or not np.isfinite(px) or px <= 0:
            continue
        idx = min(idx, n_dates - 1)
        # 退出日起价格**冻结**在退出价：持仓延续到退市之后时，entry_basis 不会
        # 变成 NaN（实测合成数据里会导致整段净值 NaN），且日内净值在退市日
        # 立即反映这笔亏损，而不是拖到窗口末。
        mat["open"][idx:, j] = px
        mat["close"][idx:, j] = px
        mat["can_sell"][idx, j] = True
        mat["can_sell"][idx + 1:, j] = False
        mat["can_buy"][idx:, j] = False
        settled.append({"code": code, "exit_date": str(data["dates"][idx].date()),
                        "exit_price": float(px)})
    mat["close_ff"] = pd.DataFrame(mat["close"]).ffill().to_numpy()
    return {"n_settled": len(settled), "settled": settled}


def _restrict_to_listing(feats, data) -> None:
    """把「按日动态池」并入各策略族的可选集合（issue #86 规则 2）。"""
    listing = data["mat"]["listing"]
    for key in feats["elig"]:
        feats["elig"][key] = feats["elig"][key] & listing


def _random_selector(feats, counts, width, bought, mode, seed):
    """等暴露度随机对照（**直接复用 R6 的向量化实现**，语义完全一致）。

    R6 的 ``_random_selector`` 用 ``argpartition`` 按 rep 向量化抽样，在 5,471 只
    票的池子上仍然可行；自己再写一套 Python 循环版只会更慢且引入口径漂移。
    """
    return _r6()._random_selector(feats, counts, width, bought=bought, mode=mode)


def _ew_baseline(data, feats, horizon, args):
    """无偏池等权买入持有（等暴露度 = 当日全部可选票，按 count 等权）。"""
    r6 = _r6()
    width = int(max(np.max(feats["elig"]["ew"].sum(axis=1)), 1))
    selector = r6._selector_factory("ew", feats, args.top_n, width=width)
    factors, diag = r6.run_schedule(data, feats, horizon, selector, width,
                                    reps=1, collect_trades=True,
                                    weight_mode="count")
    lo, hi = diag["eval_lo"], diag["eval_hi"]
    mt = r6.metrics_from_factors(factors, data["dates"], lo, hi)
    daily = r6._daily_series(factors, data["dates"], lo, hi)
    years = (data["dates"][hi - 1] - data["dates"][lo]).days / 365.25
    return {
        "id": "ew_buy_hold", "horizon": horizon, "width": width,
        "ann_return": float(mt["ann_return"][0]),
        "sharpe": float(mt["sharpe"][0]),
        "max_drawdown": float(mt["max_drawdown"][0]),
        "vol": float(mt["vol"][0]),
        "turnover_annual": r6._annual_one_way_turnover(diag["windows"], years),
        "yearly": r6._yearly(daily, data["dates"]),
        "daily": daily, "n_windows": len(diag["windows"]),
        "start": str(data["dates"][lo].date()),
        "end": str(data["dates"][hi - 1].date()),
        "eval_lo": lo, "eval_hi": hi,
    }


def _survivor_ew(data, feats, horizon, args, limit):
    """只含「到窗口末仍未退市」的票 —— 幸存者子集等权（偏差量级对照）。"""
    r6 = _r6()
    alive = np.array([data["exit_idx"][j] < 0 for j in range(len(data["codes"]))])
    elig = {"ew": feats["elig"]["ew"] & alive[None, :],
            "pool": feats["elig"]["pool"] & alive[None, :]}
    width = int(max(np.max(elig["ew"].sum(axis=1)), 1))
    sub_feats = dict(feats)
    sub_feats["elig"] = elig
    selector = r6._selector_factory("ew", sub_feats, args.top_n, width=width)
    factors, diag = r6.run_schedule(data, sub_feats, horizon, selector, width,
                                    reps=1, collect_trades=False,
                                    weight_mode="count")
    lo, hi = diag["eval_lo"], diag["eval_hi"]
    mt = r6.metrics_from_factors(factors, data["dates"], lo, hi)
    return {"n_alive": int(alive.sum()),
            "ann_return": float(mt["ann_return"][0]),
            "sharpe": float(mt["sharpe"][0]),
            "max_drawdown": float(mt["max_drawdown"][0]),
            "start": str(data["dates"][lo].date()),
            "end": str(data["dates"][hi - 1].date())}


def _current_constituent_ew(data, feats, horizon, args, codes):
    """「当前沪深 300 成分」等权（复现 R6 的幸存者偏差口径）。"""
    r6 = _r6()
    idx = {c: j for j, c in enumerate(data["codes"])}
    # R6 的 universe.json 里是 6 位代码（``000001``），本池是 baostock 格式
    # （``sz.000001``）；两种都接受，否则当前成分对照会静默变成空集。
    normalized = []
    for c in codes:
        c = str(c)
        normalized.append(c if "." in c else _bs_code(c))
    cols = np.array([idx[c] for c in normalized if c in idx], dtype=int)
    if cols.size == 0:
        return None
    mask = np.zeros(len(data["codes"]), dtype=bool)
    mask[cols] = True
    elig = {"ew": feats["elig"]["ew"] & mask[None, :],
            "pool": feats["elig"]["pool"] & mask[None, :]}
    width = int(max(np.max(elig["ew"].sum(axis=1)), 1))
    sub_feats = dict(feats)
    sub_feats["elig"] = elig
    selector = r6._selector_factory("ew", sub_feats, args.top_n, width=width)
    factors, diag = r6.run_schedule(data, sub_feats, horizon, selector, width,
                                    reps=1, collect_trades=False,
                                    weight_mode="count")
    lo, hi = diag["eval_lo"], diag["eval_hi"]
    mt = r6.metrics_from_factors(factors, data["dates"], lo, hi)
    return {"n_codes_in_pool": int(cols.size),
            "ann_return": float(mt["ann_return"][0]),
            "sharpe": float(mt["sharpe"][0]),
            "max_drawdown": float(mt["max_drawdown"][0]),
            "start": str(data["dates"][lo].date()),
            "end": str(data["dates"][hi - 1].date())}


def _index_baseline(data, start):
    idx = data["index"]
    sl = idx.loc[idx.index >= pd.Timestamp(start)]
    ir = sl["close"].pct_change().dropna()
    eq = (1 + ir).cumprod()
    return {
        "id": "csi300_index",
        "ann_return": float(eq.iloc[-1] ** (252 / len(ir)) - 1),
        "sharpe": float(ir.mean() / ir.std(ddof=1) * math.sqrt(252)),
        "max_drawdown": float((1 - eq / eq.cummax()).max()),
        "vol": float(ir.std(ddof=1) * math.sqrt(252)),
        "yearly": _r6()._yearly(ir, data["dates"]),
        "daily": ir,
    }


def _x_y_z_verdict(res, ew_res, idx_res) -> dict:
    """R4 §3.6 的 X/Y/Z 判定（组合层面只有 Y 与 Z 可判；X 需买点分层）。

    * **Y（组合收益）**：扣费年化改善 ≥ +1.5pp，且必须过等暴露度对照
      （``random_p_ge_ann <= 0.05``）。
    * **Z（组合回撤）**：maxDD 改善 ≥ 3pp，且年化劣化 ≤ 0.5pp，且过等暴露度对照。
    * **X（买点分层）**：本工具是组合层回测，不做买点分层，**判为不可判定**，
      并在报告里明确写出（不把「没测」写成「未通过」）。
    """
    d_ann_ew = res["ann_return"] - ew_res["ann_return"]
    d_ann_idx = res["ann_return"] - idx_res["ann_return"]
    d_dd = ew_res["max_drawdown"] - res["max_drawdown"]
    random_ok = bool(np.isfinite(res["random_p_ge_ann"])
                     and res["random_p_ge_ann"] <= 0.05)
    y_pass = bool(d_ann_ew >= 0.015 and d_ann_idx >= 0.015 and random_ok)
    z_pass = bool(d_dd >= 0.03 and d_ann_ew >= -0.005 and random_ok)
    return {
        "X": {"verdict": "not_testable",
              "note": "组合层回测不含买点分层；X 需 R3 口径的票×季度聚类（未测）"},
        "Y": {"verdict": "pass" if y_pass else "fail",
              "delta_ann_vs_ew": d_ann_ew, "delta_ann_vs_index": d_ann_idx,
              "threshold_pp": 1.5, "random_p_ge_ann": res["random_p_ge_ann"],
              "random_control_ok": random_ok},
        "Z": {"verdict": "pass" if z_pass else "fail",
              "delta_maxdd_vs_ew": d_dd, "delta_ann_vs_ew": d_ann_ew,
              "threshold_pp": 3.0, "ann_drag_ok": bool(d_ann_ew >= -0.005),
              "random_control_ok": random_ok},
    }


def cmd_analyze(args) -> int:
    r6 = _r6()
    paths = Paths(args.data_dir)
    paths.ensure()
    pd.set_option("display.width", 200)

    _log(f"读无偏池：{paths.data}")
    data = load_unbiased(paths, args.start, args.end)
    dates = data["dates"]
    _log(f"票池 {len(data['codes'])} 只 × {len(dates)} 个交易日 "
         f"（{dates[0].date()} ~ {dates[-1].date()}）；"
         f"排除 merger {data['universe']['n_excluded_merger']} 只")

    settle = apply_delisting_settlement(data)
    _log(f"退市结算：{settle['n_settled']} 只（退出价 = last_tradestatus1_close）")

    feats = r6.build_features(data)
    _restrict_to_listing(feats, data)
    feats["eval_start"] = args.start

    close = data["mat"]["close"]
    rows_per_stock = np.isfinite(close).sum(axis=0)
    listing = data["mat"]["listing"]
    data_quality = {
        "codes_loaded": len(data["codes"]),
        "codes_in_universe": data["universe"]["n_universe"],
        "n_excluded_merger": data["universe"]["n_excluded_merger"],
        "n_delisted_used": data["universe"]["n_delisted_used"],
        "dates": len(dates),
        "date_first": str(dates[0].date()), "date_last": str(dates[-1].date()),
        "rows_per_stock_min": int(rows_per_stock.min()),
        "rows_per_stock_median": int(np.median(rows_per_stock)),
        "missing_close_share": round(float(np.mean(~np.isfinite(close))), 4),
        "dynamic_pool_mean": float(listing.sum(axis=1).mean()),
        "dynamic_pool_min": int(listing.sum(axis=1).min()),
        "dynamic_pool_max": int(listing.sum(axis=1).max()),
        "tradestatus_share": round(float(np.mean(
            data["mat"]["tradestatus"] > 0.5)), 4),
        "eval_start": args.start,
    }
    _log(f"动态池：日均 {data_quality['dynamic_pool_mean']:.0f} 只 "
         f"(min {data_quality['dynamic_pool_min']}, "
         f"max {data_quality['dynamic_pool_max']})")

    results = []
    for name, family, horizon, params in CANDIDATES:
        if args.only and name not in args.only.split(","):
            continue
        res = r6.run_candidate(name, family, horizon, params, data, feats, args)
        results.append(res)
        _log(f"{name:<22} H={horizon:<3} 年化 {res['ann_return']*100:7.2f}% "
             f"Sharpe {res['sharpe']:5.2f} maxDD {res['max_drawdown']*100:5.1f}% "
             f"窗数 {res['n_windows']:<5} ({res['seconds']}s)")
    if not results:
        raise RuntimeError("没有候选被运行")

    n_trials = len(results)
    trial_sr_daily = [res["sharpe"] / math.sqrt(252) for res in results]
    horizons = np.array([res["horizon"] for res in results], dtype=float)
    trial_sr_period = np.array([
        res["window_mean"] / float(np.std(res["window_returns"], ddof=1))
        if np.std(res["window_returns"], ddof=1) > 0 else 0.0
        for res in results])
    for i, res in enumerate(results):
        r = res["daily"].to_numpy()
        res["dsr"] = r6.deflated_sharpe(res["sharpe"] / math.sqrt(252),
                                        trial_sr_daily, len(r),
                                        res["skew"], res["kurt"])
        scaled = trial_sr_period * np.sqrt(res["horizon"] / horizons)
        w = np.asarray(res["window_returns"], dtype=float)
        sd_w = float(np.std(w, ddof=1)) if len(w) > 2 else 0.0
        sr_w = float(np.mean(w) / sd_w) if sd_w > 0 else 0.0
        res["dsr_period"] = r6.deflated_sharpe(sr_w, scaled, len(w),
                                               res["window_skew"],
                                               res["window_kurt"])
        res["sr_period"] = sr_w
        p = res["p_period_one_sided"]
        res["p_bonferroni"] = (min(1.0, p * n_trials)
                               if np.isfinite(p) else np.nan)
        b_ann, b_sr = r6.block_bootstrap(r, max(res["horizon"], 5))
        res["boot_ann_ci"] = [float(np.percentile(b_ann, 2.5)),
                              float(np.percentile(b_ann, 97.5))]
        res["boot_sharpe_ci"] = [float(np.percentile(b_sr, 2.5)),
                                 float(np.percentile(b_sr, 97.5))]

    ew_res = _ew_baseline(data, feats, args.horizon_ew, args)
    idx_res = _index_baseline(data, args.start)
    _log(f"基线：等权无偏池 {ew_res['ann_return']*100:.2f}% "
         f"(maxDD {ew_res['max_drawdown']*100:.1f}%)；"
         f"沪深300 {idx_res['ann_return']*100:.2f}%")

    random_controls, random_dists = {}, {}
    for i, res in enumerate(results):
        for mode in ("iid", "turnover"):
            key = f"{res['id']}:{mode}"
            seed = args.seed + 1000 + i * 7 + (0 if mode == "iid" else 3)
            selector = _random_selector(feats, res["window_counts"],
                                        args.top_n, res["window_bought"],
                                        mode, seed)
            t0 = time.time()
            factors, diag = r6.run_schedule(data, feats, res["horizon"], selector,
                                            args.top_n, reps=args.random_reps,
                                            seed=seed)
            lo, hi = res["eval_lo"], res["eval_hi"]
            mt = r6.metrics_from_factors(factors, data["dates"], lo, hi)
            random_dists[key] = {"ann": mt["ann_return"], "sharpe": mt["sharpe"],
                                 "mdd": mt["max_drawdown"]}
            years = (data["dates"][hi - 1] - data["dates"][lo]).days / 365.25
            random_controls[key] = {
                "horizon": res["horizon"], "reps": args.random_reps,
                "mode": mode, "matched_counts": True,
                "matched_turnover": mode == "turnover",
                "turnover_annual": r6._annual_one_way_turnover(diag["windows"],
                                                               years),
                "ann_return": r6._summary_stats(mt["ann_return"]),
                "sharpe": r6._summary_stats(mt["sharpe"]),
                "max_drawdown": r6._summary_stats(mt["max_drawdown"]),
                "seconds": round(time.time() - t0, 1),
            }
            res[f"random_p_ge_ann_{mode}"] = float(
                np.mean(random_dists[key]["ann"] >= res["ann_return"]))
            res[f"random_p_ge_sharpe_{mode}"] = float(
                np.mean(random_dists[key]["sharpe"] >= res["sharpe"]))
            res[f"random_p_le_mdd_{mode}"] = float(
                np.mean(random_dists[key]["mdd"] <= res["max_drawdown"]))
            _log(f"随机 [{mode:<8}] {res['id']:<22} "
                 f"年化中位 {np.median(mt['ann_return'])*100:6.2f}% "
                 f"p(≥策略)={res[f'random_p_ge_ann_{mode}']:.3f} "
                 f"({random_controls[key]['seconds']}s)")
        res["random_p_ge_ann"] = max(res["random_p_ge_ann_iid"],
                                     res["random_p_ge_ann_turnover"])
        res["random_p_ge_sharpe"] = max(res["random_p_ge_sharpe_iid"],
                                        res["random_p_ge_sharpe_turnover"])
        res["random_p_le_mdd"] = max(res["random_p_le_mdd_iid"],
                                     res["random_p_le_mdd_turnover"])
        res["random_ann_mean"] = float(np.mean(
            random_dists[f"{res['id']}:iid"]["ann"]))

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
            "dd_no_worse": bool(res["max_drawdown"] <= ew_res["max_drawdown"] + 0.03),
            "yearly_positive": bool(pos_years * 2 >= max(len(years), 1)),
            "power_ok": bool(np.isfinite(res["mde_ann_return"])
                             and res["mde_ann_return"] <= 0.10),
        }
        res["recommend"] = all(res["checks"].values())
        res["xyz"] = _x_y_z_verdict(res, ew_res, idx_res)

    daily_map = {res["id"]: res["daily"] for res in results}
    wf = r6._walk_forward(daily_map, dates, args.start, train_years=3,
                          test_years=1)

    # ---- 幸存者偏差量级：无偏等权 vs 幸存者子集 vs 当前成分 ----
    surv = _survivor_ew(data, feats, args.horizon_ew, args, args.top_n)
    # 当前沪深 300 成分（R6 用的同一份快照，2026-11-09，300 只），
    # 用来复现「幸存者偏差」口径：它必须给出接近 R6 的 +14.4pp/年。
    csi = None
    csi_path = os.path.join(paths.data, "csi300-current.json")
    if os.path.exists(csi_path):
        payload = _read_json(csi_path)
        csi = _current_constituent_ew(data, feats, args.horizon_ew, args,
                                      payload["codes"])
        if csi is not None:
            csi["snapshot_date"] = payload.get("snapshot_date")
            csi["source"] = payload.get("source")
    bias = {
        "unbiased_ew_ann": ew_res["ann_return"],
        "survivor_subset_ew_ann": surv["ann_return"],
        "n_alive": surv["n_alive"],
        "survivor_minus_unbiased_pp": (surv["ann_return"]
                                       - ew_res["ann_return"]) * 100,
        "csi300_index_ann": idx_res["ann_return"],
        "index_minus_unbiased_pp": (idx_res["ann_return"]
                                    - ew_res["ann_return"]) * 100,
    }
    if csi:
        bias["current_constituent_ew_ann"] = csi["ann_return"]
        bias["current_constituent_minus_unbiased_pp"] = (
            csi["ann_return"] - ew_res["ann_return"]) * 100
    _log(f"偏差量级：无偏等权 {ew_res['ann_return']*100:.2f}% vs "
         f"幸存者子集 {surv['ann_return']*100:.2f}% "
         f"(+{bias['survivor_minus_unbiased_pp']:.2f}pp) vs "
         f"沪深300 {idx_res['ann_return']*100:.2f}%")

    def _clean(res):
        out = {k: v for k, v in res.items()
               if k not in ("daily", "yearly", "window_returns", "window_counts")}
        out["yearly"] = {str(y): v for y, v in (res.get("yearly") or {}).items()}
        return out

    payload = {
        "meta": {
            "issue": 86, "head": _git_head(),
            "generated": datetime.now().isoformat(timespec="seconds"),
            "data_dir": paths.data,
            "universe": data["universe"]["snapshot_date"],
            "n_codes": len(data["codes"]),
            "n_universe": data["universe"]["n_universe"],
            "n_excluded_merger": data["universe"]["n_excluded_merger"],
            "excluded_merger": data["universe"]["excluded_merger"],
            "n_delisted_used": data["universe"]["n_delisted_used"],
            "date_range": [str(dates[0].date()), str(dates[-1].date())],
            "eval_start": args.start, "top_n": args.top_n,
            "n_trials": n_trials, "seed": args.seed,
            "random_reps": args.random_reps,
            "costs": {"commission": COMMISSION, "slippage": SLIPPAGE,
                      "stamp_duty": STAMP_DUTY, "buy_one_way": BUY_COST,
                      "sell_one_way": SELL_COST},
            "unbiased_rules": {
                "delisted_settlement": "last_tradestatus1_close",
                "merger_excluded": True,
                "dynamic_pool": "ipo_date <= d <= out_date",
                "suspension": "tradestatus==0 不可成交",
            },
            "survivorship_bias": "无偏池（全A+退市，不含北交所/B股）",
            "data_quality": data_quality,
            "delisting_settlement": {"n_settled": settle["n_settled"]},
        },
        "candidates": [_clean(r) for r in results],
        "baselines": {
            "ew_buy_hold_unbiased": _clean(ew_res),
            "csi300_index": _clean(idx_res),
            "survivor_subset_ew": surv,
            "current_constituent_ew": csi,
            "bias_magnitude": bias,
        },
        "random_controls": random_controls,
        "walk_forward": wf,
    }
    _write_json(os.path.join(paths.out, "summary.json"), payload)

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
            "random_p_ge_ann": res["random_p_ge_ann"],
            "p_random_iid": res["random_p_ge_ann_iid"],
            "p_random_turnover": res["random_p_ge_ann_turnover"],
            "Y": res["xyz"]["Y"]["verdict"], "Z": res["xyz"]["Z"]["verdict"],
            "recommend": res["recommend"],
        })
    pd.DataFrame(rows).to_csv(os.path.join(paths.out, "candidates.csv"),
                              index=False)
    yearly_rows = []
    for res in results:
        for year, item in (res["yearly"] or {}).items():
            yearly_rows.append({"id": res["id"], "year": year,
                                "return": item["return"],
                                "sharpe": item["sharpe"],
                                "max_dd": item["max_dd"]})
    pd.DataFrame(yearly_rows).to_csv(os.path.join(paths.out, "yearly.csv"),
                                     index=False)
    r6._print_results(results, ew_res, idx_res, random_controls, wf, n_trials)
    # R6 的 _print_results 把产物路径写死成 evidence/p2/out；本工具的产物在
    # <data-dir>/out，这里补一行正确的。
    print(f"实际产物目录：{paths.out}")
    print(f"\n偏差量级：无偏等权 {bias['unbiased_ew_ann']*100:.2f}% / "
          f"幸存者子集 {bias['survivor_subset_ew_ann']*100:.2f}% "
          f"({bias['survivor_minus_unbiased_pp']:+.2f}pp) / "
          f"沪深300 {bias['csi300_index_ann']*100:.2f}%")
    return 0


def cmd_selftest(args) -> int:  # noqa: ARG001
    """离线自检：动态池 / 退市结算 / 与 R6 候选一致性。"""
    _assert_r6_parity()
    checks = []

    def check(name, cond, detail=""):
        checks.append((name, bool(cond), detail))

    check("candidates=13", len(CANDIDATES) == 13)
    check("buy_cost=13bp", abs(BUY_COST - 0.0013) < 1e-12)
    check("sell_cost=23bp", abs(SELL_COST - 0.0023) < 1e-12)
    base = BASE_FIELDS.split(",")
    check("base_fields_matches_r9", base == [
        "date", "code", "open", "high", "low", "close", "volume", "amount",
        "tradestatus", "pctChg", "isST"])
    check("super_starts_with_base", SUPER_FIELDS.startswith(BASE_FIELDS))
    for name, ok, detail in checks:
        print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  {detail}" if detail else ""))
    failed = [c for c in checks if not c[1]]
    print(f"selftest：{len(checks) - len(failed)}/{len(checks)} passed")
    return 0 if not failed else 1


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def build_parser():
    p = argparse.ArgumentParser(
        description="R10 无偏池上的策略候选复验（issue #86）")
    sub = p.add_subparsers(dest="cmd", required=True)

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--data-dir", default=DEFAULT_DATA_DIR,
                        help="数据集目录（默认 ~/data/stock-universe）")

    f = sub.add_parser("fetch", parents=[common],
                       help="联网重建无偏池（baostock 串行）")
    f.add_argument("--start", default=DEFAULT_START,
                   help="R9 口径起点（csv/ 落盘与 md5 校验的起点）")
    f.add_argument("--warmup-start", default=WARMUP_START,
                   help="实际请求起点（指标预热；默认 2016-06-01，与 R6 同）")
    f.add_argument("--end", default=DEFAULT_END)
    f.add_argument("--limit", type=int, default=None)
    f.add_argument("--codes", default=None)
    f.add_argument("--delisted-sample", type=int, default=None)
    f.add_argument("--adjustflag", default=ADJUST, choices=("1", "2", "3"))
    f.add_argument("--max-rows", type=int, default=MAX_ROWS)
    f.add_argument("--force", action="store_true")
    f.add_argument("--pause", type=float, default=0.0)
    f.add_argument("--watchdog-seconds", type=float, default=600.0)
    f.add_argument("--strict", action="store_true")
    f.set_defaults(func=cmd_fetch)

    v = sub.add_parser("verify", parents=[common],
                       help="离线逐票比对 md5 与参考 manifest")
    v.add_argument("--reference", default=None)
    v.add_argument("--check-drift", action="store_true",
                   help="对 md5 不一致的票联网判定是 qfq 快照漂移还是真错误")
    v.set_defaults(func=cmd_verify)

    a = sub.add_parser("analyze", parents=[common],
                       help="离线分析（只读数据集）")
    a.add_argument("--start", default=DEFAULT_START)
    a.add_argument("--end", default=DEFAULT_END)
    a.add_argument("--top-n", type=int, default=TOP_N)
    a.add_argument("--random-reps", type=int, default=RANDOM_REPS)
    a.add_argument("--seed", type=int, default=SEED)
    a.add_argument("--horizon-ew", type=int, default=20)
    a.add_argument("--only", default=None, help="只跑指定候选 id（逗号分隔，调试用）")
    a.set_defaults(func=cmd_analyze)

    s = sub.add_parser("selftest", help="合成数据自检（不触网）")
    s.set_defaults(func=cmd_selftest)
    return p


def main(argv=None):
    _assert_r6_parity()
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
