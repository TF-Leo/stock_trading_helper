# -*- coding: utf-8 -*-
"""P1 前向台账（issue #56）：每日记录 → 20 个交易日后结算 → 汇总校准。

为什么需要它
------------
R3/R4 的结论（``docs/P1_EVIDENCE_V1.md`` §8.2、``docs/P1_EVIDENCE_PLAN.md`` §3.6.3）
指出：资金流与置信度校准**无法靠回放历史得出结论**。回放 581 组锦标赛单票单日约
8s，成本不可行；而资金流的公开源只有 120 日窗口，历史也补不回来。唯一出路是
**从今天起把建议冻住**，20 个交易日后用真实行情结算，积累真实样本外记录。
R4 的 C 路径（校准，Brier 改善 ≥0.02）要求 **≥200 条已到期记录**。

本模块做三件事，全部**离线可测**：

``record_day``
    对每只票调用一次 CLI（默认子进程 ``python cli.py <code> --json --fast``），
    把当次 ``recommendation`` / ``evidence.flow`` / ``evidence.valuation`` /
    ``market_regime`` 的关键字段冻结进
    ``source/stock_analysis/data/forward/<YYYY-MM-DD>.json``（``data/`` 已 gitignore）。
    **同一天重复跑覆盖同 code 的记录，不追加**（幂等）。
    任何字段取不到都写 ``null`` 并把原因记进 ``missing``，**不省略键**；
    CLI 整体失败（断网 / 无缓存 / 退出码 30）时同样落盘一条全 null 的记录。

``settle_all``
    对已到期（as_of 之后第 ``HORIZON_DAYS`` 个交易日存在）的记录计算
    ``open_{d+1+20} / open_{d+1} − 1``，写进同一条记录的 ``settled``。
    价格取不到时 ``settled=null`` + ``settle_reason``，**不猜**。
    未到期的记录不结算，只标 ``not_matured``。

``build_report``
    已到期条数、方向命中率、按 ``confidence`` 分箱的校准（含 Brier）、
    按 ``flow.caliber`` / ``valuation`` 分层的命中率 —— 为 R4 的 X/C 路径准备。

口径说明（写死在这里，供 README 与测试共用）
--------------------------------------------
* ``as_of`` = CLI 返回的最后一根行情日期（``data.end``），不是运行日。盘后跑
  得到的是当日；周末/节假日跑得到的是上一个交易日，因此同一交易日的重复运行
  天然落在同一个文件里。
* 收益用**前复权**开盘价（``stock_{code}_qfq.csv`` 的 ``open``）。前复权让分红
  送转在比值里自然抵消；用未复权价会把除权当成亏损。
* 「交易日」以**该票自己的行情索引**为准：停牌日没有开盘价，本来就不能成交，
  把它算成一个交易日会凭空多出一格。行情缓存缺失时结算直接给
  ``settled=null, settle_reason="no_price_data"``，**不退回日历推算、不猜价格**。
  （交易日历只用于记录阶段在拿不到 ``data.end`` 时决定落哪个文件。）
* ``hit``：``buy`` 记 ``ret > 0``，``sell`` 记 ``ret < 0``；``hold`` / ``avoid`` /
  ``insufficient_data`` **没有方向**，不参与命中率与校准（只在 ``by_action`` 里
  计数），避免把「不动作」当成猜对。

场外基金（issue #95）
---------------------
基金净值不在 ``stock_*_qfq.csv`` 里，而在 ``fund_*.csv``（OTC 场外基金只有单位
净值，``meta.get_fund_daily`` 把 ``open`` 补成净值；场内 ETF 是真实 OHLC）。
``load_price_bars`` 因此**按标的类型选源**，取价口径不变
（``open_{d+1+20} / open_{d+1} − 1``）：

* 类型判定优先用记录里的 ``instrument_type``（记录阶段从 CLI 的
  ``request.type`` 落下，additive 字段）；老记录没有该字段时退回文件探测 ——
  **先找 ``stock_*_qfq.csv``，再找 ``fund_*.csv``**，与修复前行为一致
  （``510300`` 这类场内 ETF 两份缓存都有，老记录仍走个股路径，不改变历史口径）。
* 结算里的 ``price_source`` 写真实来源：个股 ``local_qfq_cache``、
  基金 ``fund_nav``。
* ``settle_status`` 把「没到期 / 数据未到」和「真失败」分开：
  ``settled`` / ``not_matured``（还没攒够 20 根）/ ``pending``
  （``no_bars_after_as_of``：as_of 之后一根都没有，通常是缓存还没更新）/
  ``failed``（``no_price_data`` / ``open_missing`` / ``price_parse_error``）。
  ``pending`` 与 ``not_matured`` **不计入失败率**。``price_source`` 取
  ``load_price_bars`` 实际读到的文件（``bars.attrs``），老记录探测到
  ``fund_*.csv`` 也会如实标 ``fund_nav``。
* ``confidence`` 当作「该方向判断正确的概率」用于校准（Brier）。这是 C 路径的
  操作性定义；它回答的是「置信度可不可信」，不回答「买点好不好」。
"""
from __future__ import annotations

import datetime as _dt
import inspect
import json
import os

SCHEMA_VERSION = "1.0"

#: 台账目录：``source/stock_analysis/data/forward/``（``data/`` 已 gitignore）。
LEDGER_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          "data", "forward")
#: 行情缓存目录（与 ``data_loader.meta.DATA_DIR`` 同一处，前复权日线）。
DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")

#: 持有期（交易日）。与 CLI 的 ``HORIZON`` 一致，但这里写死为台账口径：
#: 改这个数会让新旧记录不可比，必须同时改 ``docs/P1_EVIDENCE_PLAN.md``。
HORIZON_DAYS = 20

DIRECTIONAL_ACTIONS = ("buy", "sell")

#: 标的类型（issue #95）。记录阶段从 CLI 的 ``request.type`` 落下；
#: 决定结算时读 ``stock_*_qfq.csv`` 还是 ``fund_*.csv``。
INSTRUMENT_TYPES = ("stock", "fund")

#: 结算来源标注（写进 ``settled.price_source``，issue #95 要求真实来源）。
PRICE_SOURCE_STOCK = "local_qfq_cache"
PRICE_SOURCE_FUND = "fund_nav"

#: ``settle_status`` 取值。``pending`` / ``not_matured`` 都**不算失败**：
#: 前者是「as_of 之后还没有行情」（缓存没更新），后者是「还没攒够 20 根」。
SETTLE_STATUS_SETTLED = "settled"
SETTLE_STATUS_NOT_MATURED = "not_matured"
SETTLE_STATUS_PENDING = "pending"
SETTLE_STATUS_FAILED = "failed"

#: ``settle_reason`` → ``settle_status`` 的**非失败**映射。未列出的
#: （``no_price_data`` / ``open_missing`` / ``price_parse_error``）一律 ``failed``。
#: ``not_matured``（未攒够 20 根）与 ``no_bars_after_as_of``（数据未到）都不算失败。
_NON_FAILURE_STATUS = {
    "not_matured": SETTLE_STATUS_NOT_MATURED,
    "no_bars_after_as_of": SETTLE_STATUS_PENDING,
}

#: ``confidence`` 校准分箱（左闭右开，最后一箱右闭）。
CONFIDENCE_BINS = ((0.0, 0.2), (0.2, 0.4), (0.4, 0.6), (0.6, 0.8), (0.8, 1.0))

#: ``valuation`` 分层：3 年分位（0–100）的三档 + 不可用。
PB_BUCKETS = (("cheap", 0.0, 30.0), ("mid", 30.0, 70.0), ("rich", 70.0, 100.0))

#: 记录里 ``flow`` 子块的键（issue #56 指定）。
FLOW_KEYS = ("available", "source", "caliber", "net_5d", "net_20d", "as_of")
#: 追加键：东财主力口径下 ``net_5d`` / ``net_20d`` 恒为 null（口径隔离，
#: 见 ``data_loader/flow.py``），不记 ``main_net_*`` 就等于把当日资金面信号丢了。
FLOW_EXTRA_KEYS = ("main_net_5d", "main_net_20d")
#: ``valuation`` 子块的键。
VALUATION_KEYS = ("available", "pe_percentile_3y", "pb_percentile_3y",
                  "ps_percentile_3y", "as_of")
#: ``market_regime`` / ``index_regime`` 子块的键。
REGIME_KEYS = ("label", "confidence")


# ---------------------------------------------------------------------------
# 存储
# ---------------------------------------------------------------------------
def ledger_dir_path(ledger_dir=None):
    return str(ledger_dir or LEDGER_DIR)


def day_path(day, ledger_dir=None):
    """``<ledger>/<YYYY-MM-DD>.json``；``day`` 可以是字符串 / date / datetime。"""
    text = str(day)[:10]
    return os.path.join(ledger_dir_path(ledger_dir), f"{text}.json")


def list_days(ledger_dir=None):
    """台账里已有的日期（升序）。目录不存在时返回空列表。"""
    root = ledger_dir_path(ledger_dir)
    if not os.path.isdir(root):
        return []
    days = []
    for name in os.listdir(root):
        if name.endswith(".json") and len(name) == 15:  # YYYY-MM-DD.json
            days.append(name[:-5])
    return sorted(days)


def load_day(day, ledger_dir=None):
    """读取一天的文件；不存在 / 损坏时返回 ``None``（不抛异常）。"""
    path = day_path(day, ledger_dir)
    try:
        with open(path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except Exception:
        return None
    if not isinstance(payload, dict):
        return None
    payload.setdefault("as_of", str(day)[:10])
    payload.setdefault("records", {})
    if not isinstance(payload["records"], dict):
        payload["records"] = {}
    return payload


def _atomic_write(path, payload):
    """先写 ``.tmp`` 再 ``os.replace``：半截 JSON 不会被当成有效台账。"""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=1, sort_keys=False)
        handle.write("\n")
    os.replace(tmp, path)


def _now_iso(now=None):
    return (now or _dt.datetime.now()).isoformat(timespec="seconds")


def _local_expected_trade_date(now=None):
    """**纯本地**的「最近一个交易日」；日历缺失时退回周末推算。"""
    try:
        from stock_analysis.data_loader.flow import _expected_trade_date
        day, _source = _expected_trade_date(now=now)
        return day.isoformat()
    except Exception:
        day = (now or _dt.datetime.now()).date()
        while day.weekday() >= 5:
            day -= _dt.timedelta(days=1)
        return day.isoformat()


# ---------------------------------------------------------------------------
# 记录
# ---------------------------------------------------------------------------
def _normalize_code(code):
    text = str(code).strip()
    digits = "".join(ch for ch in text if ch.isdigit())
    return digits[-6:] if len(digits) >= 6 else text


def _sub_block(source, keys, missing, prefix):
    """从 CLI 子块里摘出 ``keys``；源块缺失或键缺失都记进 ``missing``。

    不可用块（``available=False``）自带 ``reason``（``cache_missing`` /
    ``no_rows_as_of`` / ``fund_not_supported`` 等）：子字段的 null 原因要
    继承它，否则「为什么没有资金流」会被压成一句无信息量的
    ``field_absent_in_cli_output``。块级 ``reason`` 本身不写进记录（台账
    契约的键集合固定），只通过 ``missing`` 追溯。
    """
    block_reason = source.get("reason") if isinstance(source, dict) else None
    out = {}
    for key in keys:
        value = None
        if isinstance(source, dict):
            value = source.get(key)
        out[key] = value
        if value is None:
            if not isinstance(source, dict):
                missing[f"{prefix}.{key}"] = "cli_block_absent"
            else:
                missing[f"{prefix}.{key}"] = (
                    block_reason or "field_absent_in_cli_output")
    return out


def build_record(code, payload, cli_exit, elapsed_s, as_of, now=None):
    """把一次 CLI 输出折成一条台账记录（键集合固定，缺失写 null + reason）。"""
    code = _normalize_code(code)
    rec = {
        "as_of": str(as_of)[:10],
        "code": code,
        "action": None,
        "status": None,
        "confidence": None,
        "position_fraction": None,
        "entry_zone": None,
        "stop_loss": None,
        "take_profit": None,
        "horizon_days": None,
        "signal": {"position_state": None},
        "target_position": None,
        "flow": {key: None for key in FLOW_KEYS + FLOW_EXTRA_KEYS},
        "valuation": {key: None for key in VALUATION_KEYS},
        "market_regime": {key: None for key in REGIME_KEYS},
        "index_regime": {key: None for key in REGIME_KEYS},
        "cli_exit": cli_exit,
        "elapsed_s": elapsed_s,
        "recorded_at": _now_iso(now),
        # issue #95 additive：结算时按类型选价源（``stock_*_qfq.csv`` / ``fund_*.csv``）。
        # 老记录没有这个键，``load_price_bars`` 退回文件探测。
        "instrument_type": None,
        "missing": {},
        "settled": None,
        "settle_reason": None,
        # issue #95 additive：``settled`` / ``not_matured`` / ``pending`` / ``failed``。
        # ``no_bars_after_as_of`` 归 ``pending``（数据未到），不再算失败。
        "settle_status": None,
    }
    missing = rec["missing"]

    if not isinstance(payload, dict):
        reason = ("cli_failed" if cli_exit not in (0, None) else "cli_no_json")
        for path in ("action", "status", "confidence", "position_fraction",
                     "entry_zone", "stop_loss", "take_profit", "horizon_days",
                     "signal.position_state", "target_position"):
            missing[path] = reason
        for key in FLOW_KEYS + FLOW_EXTRA_KEYS:
            missing[f"flow.{key}"] = reason
        for key in VALUATION_KEYS:
            missing[f"valuation.{key}"] = reason
        for prefix in ("market_regime", "index_regime"):
            for key in REGIME_KEYS:
                missing[f"{prefix}.{key}"] = reason
        return rec

    rec_block = payload.get("recommendation") if isinstance(payload, dict) else None
    if not isinstance(rec_block, dict):
        rec_block = {}
        missing["recommendation"] = "cli_block_absent"

    for key in ("action", "status", "confidence", "position_fraction",
                "entry_zone", "stop_loss", "take_profit", "horizon_days",
                "target_position"):
        value = rec_block.get(key)
        rec[key] = value
        if value is None:
            missing[key] = "field_absent_in_cli_output"

    signal = rec_block.get("signal")
    state = signal.get("position_state") if isinstance(signal, dict) else None
    rec["signal"]["position_state"] = state
    if state is None:
        missing["signal.position_state"] = "field_absent_in_cli_output"

    evidence = payload.get("evidence") if isinstance(payload.get("evidence"), dict) else {}
    rec["flow"] = _sub_block(evidence.get("flow"),
                             FLOW_KEYS + FLOW_EXTRA_KEYS, missing, "flow")
    rec["valuation"] = _sub_block(evidence.get("valuation"),
                                  VALUATION_KEYS, missing, "valuation")

    # issue #95：CLI 的 ``request.type`` 是唯一可靠的标的类型来源（``board``
    # 只看 5 开头，场外基金 0/1 段会被判成 unknown，不能用来选价源）。
    request = payload.get("request") if isinstance(payload.get("request"), dict) else {}
    inst_type = request.get("type")
    if inst_type in INSTRUMENT_TYPES:
        rec["instrument_type"] = inst_type
    else:
        missing["instrument_type"] = (
            "cli_block_absent" if not request else "field_absent_in_cli_output")

    market = payload.get("market_regime")
    rec["market_regime"] = _sub_block(market, REGIME_KEYS, missing, "market_regime")
    index = market.get("index") if isinstance(market, dict) else None
    rec["index_regime"] = _sub_block(index, REGIME_KEYS, missing, "index_regime")
    return rec


def source_dir():
    """``<repo>/source``：CLI 的工作目录（``cli.py`` 就在这一层）。

    本模块位于 ``<repo>/source/stock_analysis/``，上两级即 ``source/``。
    issue #56 的回归：曾多上一级，子进程在仓库根找不到 ``cli.py``，退出码 2
    （argparse usage error），台账只剩全 null 记录。
    """
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def default_runner(code):
    """默认记录器：真实子进程跑 CLI，返回 ``(payload, exit_code, elapsed_s)``。

    用子进程而不是 ``import cli``：``cli.py`` 的退出码契约（0/10/20/30/40）
    本身就是证据的一部分，直接 import 会把它丢掉。``--fast`` 与生产用法一致
    （README「性能」一节：≈9.8s/票）。
    """
    import subprocess
    import sys
    import time

    src = source_dir()
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    start = time.time()
    proc = subprocess.run([sys.executable, "cli.py", str(code), "--json", "--fast"],
                          cwd=src, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", env=env)
    elapsed = round(time.time() - start, 2)
    try:
        payload = json.loads(proc.stdout)
    except Exception:
        payload = None
    if isinstance(payload, dict):
        diag = payload.get("diagnostics")
        if isinstance(diag, dict) and diag.get("elapsed_seconds") is not None:
            elapsed = diag.get("elapsed_seconds")
    return payload, proc.returncode, elapsed


def collect_record(code, as_of=None, runner=None, now=None):
    """跑一次 CLI 并折成 ``(record, payload)``。

    返回原始 ``payload`` 供调用方（``tools/forward_log.py`` 的 CSV 路径）复用，
    避免为了写旧格式再跑一遍 CLI。``runner`` 抛异常 / 返回非 JSON 时记录仍是
    完整键集合（全 null + ``missing`` 原因），**不会静默缺行**。
    """
    runner = runner or default_runner
    try:
        payload, exit_code, elapsed = runner(code)
    except Exception as exc:
        payload, exit_code, elapsed = None, None, None
        fallback = as_of or _local_expected_trade_date(now)
        rec = build_record(code, None, exit_code, elapsed, fallback, now=now)
        rec["missing"] = {key: f"runner_error:{type(exc).__name__}"
                          for key in rec["missing"]}
        return rec, None

    day = as_of
    if day is None and isinstance(payload, dict):
        data = payload.get("data")
        if isinstance(data, dict) and data.get("end"):
            day = str(data["end"])[:10]
    if day is None:
        day = _local_expected_trade_date(now)
    return build_record(code, payload, exit_code, elapsed, day, now=now), payload


def write_records(records_by_day, ledger_dir=None, now=None):
    """把 ``{day: {code: record}}`` 合并进台账文件（同 code 覆盖，不追加）。"""
    written = {}
    for day, recs in records_by_day.items():
        payload = load_day(day, ledger_dir) or {
            "schema_version": SCHEMA_VERSION,
            "as_of": day,
            "created_at": _now_iso(now),
            "records": {},
        }
        payload["schema_version"] = SCHEMA_VERSION
        payload["as_of"] = day
        payload["updated_at"] = _now_iso(now)
        payload["records"].update(recs)
        _atomic_write(day_path(day, ledger_dir), payload)
        written[day] = recs
    return written


def record_day(codes, as_of=None, ledger_dir=None, runner=None, now=None):
    """记录一批票的当日建议；返回 ``{as_of: {code: record}}``。

    幂等：同一天同一 code 覆盖写，不追加。按实际 ``as_of`` 分文件落盘
    （不同票可能因停牌/缓存滞后落在不同日期）。
    """
    grouped = {}
    for code in codes:
        rec, _payload = collect_record(code, as_of=as_of, runner=runner, now=now)
        grouped.setdefault(rec["as_of"], {})[rec["code"]] = rec
    return write_records(grouped, ledger_dir=ledger_dir, now=now)


# ---------------------------------------------------------------------------
# 结算
# ---------------------------------------------------------------------------
def price_source_for(instrument_type):
    """标的类型 → ``settled.price_source`` 标注（issue #95）。"""
    return PRICE_SOURCE_FUND if instrument_type == "fund" else PRICE_SOURCE_STOCK


def price_cache_path(code, instrument_type):
    """该标的的价格缓存路径；``fund`` 走 ``fund_*.csv``，其余走 ``stock_*_qfq.csv``。"""
    code = _normalize_code(code)
    if instrument_type == "fund":
        return os.path.join(DATA_DIR, f"fund_{code}.csv")
    return os.path.join(DATA_DIR, f"stock_{code}_qfq.csv")


def load_price_bars(code, instrument_type=None):
    """读该标的的日线缓存；缺失 / 不可读返回 ``None``（issue #95）。

    个股读 ``stock_*_qfq.csv``（前复权），场外基金读 ``fund_*.csv``（单位净值，
    ``open`` 由 ``data_loader.meta.get_fund_daily`` 用净值补齐）。

    ``instrument_type`` 为空（老记录没有这个键）时**文件探测**：先
    ``stock_*_qfq.csv``，再 ``fund_*.csv``。顺序是有意的 —— ``510300`` 这类
    场内 ETF 两份缓存都可能存在，先个股与修复前行为一致，历史口径不变。

    返回的 DataFrame 在 ``attrs["price_source"]`` 上标注**实际读到的**来源，
    供 :func:`compute_settlement` 写进结算记录：老记录即使没落
    ``instrument_type``，只要真读了 ``fund_*.csv`` 也不会被标成
    ``local_qfq_cache``（issue #95 要求 2）。
    """
    import pandas as pd

    if instrument_type in INSTRUMENT_TYPES:
        candidates = [(price_cache_path(code, instrument_type),
                       price_source_for(instrument_type))]
    else:
        candidates = [(price_cache_path(code, "stock"), PRICE_SOURCE_STOCK),
                      (price_cache_path(code, "fund"), PRICE_SOURCE_FUND)]
    for path, source in candidates:
        if not os.path.exists(path):
            continue
        try:
            bars = pd.read_csv(path, index_col=0, parse_dates=True)
        except Exception:
            continue
        if bars is None or len(bars) == 0 or "open" not in bars.columns:
            continue
        bars = bars.sort_index()
        try:
            bars.attrs["price_source"] = source
        except Exception:  # pragma: no cover - 老 pandas 没有 attrs
            pass
        return bars
    return None


def _call_price_loader(loader, code, instrument_type):
    """按 loader 的签名传参：新 loader 收 ``instrument_type``，旧替身只收 code。

    测试与外部调用历史上注入的都是 ``lambda code: frame``；为了不让 issue #95
    破坏这些调用方，只有 loader 显式声明了 ``instrument_type`` 参数才传。
    """
    try:
        params = inspect.signature(loader).parameters
    except (TypeError, ValueError):
        params = {}
    if "instrument_type" in params:
        return loader(code, instrument_type=instrument_type)
    return loader(code)


def settle_status_for(reason):
    """``settle_reason`` → ``settle_status``（issue #95）。

    ``not_matured``（还没攒够 20 根）与 ``no_bars_after_as_of``（as_of 之后
    一根行情都没有，通常是缓存还没更新）都是「数据未到」，分别归
    ``not_matured`` / ``pending``，**都不算失败**；只有真拿不到价才算 ``failed``。
    """
    if reason is None:
        return SETTLE_STATUS_SETTLED
    return _NON_FAILURE_STATUS.get(reason, SETTLE_STATUS_FAILED)


def compute_settlement(record, bars, horizon=HORIZON_DAYS):
    """``open_{d+1+h} / open_{d+1} − 1``；返回 ``(settled, reason)``。

    ``settled`` 为 ``None`` 时 ``reason`` 说明为什么（``not_matured`` /
    ``no_price_data`` / ``open_missing`` ...）。**不猜、不缩短持有期。**

    ``price_source`` 标注**真实来源**（issue #95）：优先用
    ``bars.attrs["price_source"]``（:func:`load_price_bars` 实际读到的文件），
    其次按记录的 ``instrument_type`` 推断 —— 个股 ``local_qfq_cache``、
    基金 ``fund_nav``。注入自定义 ``price_loader`` 时没有 attrs，走类型推断。
    """
    if bars is None or len(bars) == 0:
        return None, "no_price_data"
    try:
        import pandas as pd

        as_of = pd.Timestamp(str(record.get("as_of"))[:10])
        index = list(bars.index)
        entry_pos = None
        for pos, stamp in enumerate(index):
            if pd.Timestamp(stamp) > as_of:
                entry_pos = pos
                break
        if entry_pos is None:
            return None, "no_bars_after_as_of"
        exit_pos = entry_pos + int(horizon)
        if exit_pos >= len(index):
            return None, "not_matured"
        entry_open = float(bars["open"].iloc[entry_pos])
        exit_open = float(bars["open"].iloc[exit_pos])
    except Exception:
        return None, "price_parse_error"
    if not (entry_open > 0) or not (exit_open > 0):
        return None, "open_missing"

    ret = exit_open / entry_open - 1.0
    action = record.get("action")
    hit = None
    if action == "buy":
        hit = ret > 0
    elif action == "sell":
        hit = ret < 0
    return {
        "ret_20d": round(ret, 6),
        "hit": hit,
        "settle_date": str(index[exit_pos])[:10],
        "entry_date": str(index[entry_pos])[:10],
        "entry_open": round(entry_open, 6),
        "exit_open": round(exit_open, 6),
        "horizon_days": int(horizon),
        "price_source": _price_source_of(record, bars),
    }, None


def _price_source_of(record, bars):
    """结算来源：先看价源文件自报（``bars.attrs``），再退回记录类型。"""
    attrs = getattr(bars, "attrs", None)
    if isinstance(attrs, dict):
        source = attrs.get("price_source")
        if source in (PRICE_SOURCE_STOCK, PRICE_SOURCE_FUND):
            return source
    return price_source_for(record.get("instrument_type"))


def settle_all(ledger_dir=None, codes=None, price_loader=None, horizon=HORIZON_DAYS,
               now=None):
    """结算所有已到期记录。

    返回 ``{"days", "settled", "not_matured", "pending", "failed", "reasons"}``。
    ``not_matured``（未攒够 20 根）与 ``pending``（``no_bars_after_as_of``，
    数据未到）**都不算失败**，``failed`` 只统计真拿不到价的情况。

    每次重算（缓存更新后可用新数据修正），幂等。价源按记录类型选：
    ``price_loader`` 若接受 ``instrument_type`` 就传，否则按 code 调用（兼容
    旧的单参测试替身）。缓存按 ``(code, instrument_type)`` 分键 —— 同一代码
    在个股与基金两侧都可能有记录，混用缓存会拿错价。
    """
    price_loader = price_loader or load_price_bars
    wanted = None if not codes else {_normalize_code(c) for c in codes}
    stats = {"days": 0, "settled": 0, "not_matured": 0, "pending": 0,
             "failed": 0, "reasons": {}}
    cache = {}
    for day in list_days(ledger_dir):
        payload = load_day(day, ledger_dir)
        if not payload:
            continue
        stats["days"] += 1
        changed = False
        for code, record in payload["records"].items():
            if wanted is not None and code not in wanted:
                continue
            if not isinstance(record, dict):
                continue
            inst_type = record.get("instrument_type")
            cache_key = (code, inst_type)
            if cache_key not in cache:
                cache[cache_key] = _call_price_loader(price_loader, code, inst_type)
            settled, reason = compute_settlement(record, cache[cache_key],
                                                 horizon=horizon)
            status = settle_status_for(reason)
            if (record.get("settled") != settled
                    or record.get("settle_reason") != reason
                    or record.get("settle_status") != status):
                changed = True
            record["settled"] = settled
            record["settle_reason"] = reason
            record["settle_status"] = status
            if settled is not None:
                stats["settled"] += 1
            elif status == SETTLE_STATUS_NOT_MATURED:
                stats["not_matured"] += 1
            elif status == SETTLE_STATUS_PENDING:
                stats["pending"] += 1
                stats["reasons"][reason] = stats["reasons"].get(reason, 0) + 1
            else:
                stats["failed"] += 1
                stats["reasons"][reason] = stats["reasons"].get(reason, 0) + 1
        if changed:
            payload["updated_at"] = _now_iso(now)
            _atomic_write(day_path(day, ledger_dir), payload)
    return stats


# ---------------------------------------------------------------------------
# 汇总
# ---------------------------------------------------------------------------
def _hit_rate(records):
    hits = [r for r in records if r.get("hit") is not None]
    if not hits:
        return None, 0
    return round(sum(1 for r in hits if r["hit"]) / len(hits), 6), len(hits)


def _confidence_bin(confidence):
    """分箱标签。除最后一箱外左闭右开；标签里**不写闭区间符号**，避免
    ``[0.8,1.0)`` 与 ``[0.8,1.0]`` 两种写法混用（1.0 归最后一箱）。"""
    try:
        value = float(confidence)
    except Exception:
        return None
    for pos, (low, high) in enumerate(CONFIDENCE_BINS):
        last = pos == len(CONFIDENCE_BINS) - 1
        if low <= value < high or (last and value == high):
            return f"[{low:.1f},{high:.1f})"
    return None


def _pb_bucket(record):
    valuation = record.get("valuation") or {}
    if not valuation.get("available"):
        return "unavailable"
    try:
        value = float(valuation.get("pb_percentile_3y"))
    except Exception:
        return "unknown"
    for name, low, high in PB_BUCKETS:
        if low <= value < high:
            return name
    return "rich" if value >= 100 else "unknown"


def iter_records(ledger_dir=None):
    """逐条产出 ``(day, code, record)``。"""
    for day in list_days(ledger_dir):
        payload = load_day(day, ledger_dir)
        if not payload:
            continue
        for code, record in payload["records"].items():
            if isinstance(record, dict):
                yield day, code, record


def build_report(ledger_dir=None):
    """汇总报告（纯字典，便于测试与 ``--json`` 复用）。空台账返回零值，不崩。

    issue #95：``pending``（``no_bars_after_as_of``，数据未到）单独计数，
    不进 ``failed``、也不进失败率分母；``failed`` 只数真拿不到价的记录。
    """
    days = list_days(ledger_dir)
    all_records = list(iter_records(ledger_dir))
    matured = [(day, code, r) for day, code, r in all_records
               if isinstance(r.get("settled"), dict)]
    directional = [(day, code, r) for day, code, r in matured
                   if r["settled"].get("hit") is not None]

    settle_counts = {SETTLE_STATUS_SETTLED: 0, SETTLE_STATUS_NOT_MATURED: 0,
                     SETTLE_STATUS_PENDING: 0, SETTLE_STATUS_FAILED: 0}
    for _day, _code, record in all_records:
        status = record.get("settle_status")
        if status is None:
            # 老台账没有这个键：从 settled / settle_reason 反推，口径与现在一致。
            status = settle_status_for(record.get("settle_reason"))
        if status in settle_counts:
            settle_counts[status] += 1
    pending = settle_counts[SETTLE_STATUS_PENDING]
    failed = settle_counts[SETTLE_STATUS_FAILED]

    hit_rate, hit_n = _hit_rate([r["settled"] for _, _, r in matured])

    by_action = {}
    for _day, _code, record in all_records:
        action = record.get("action")
        bucket = by_action.setdefault(str(action), {"records": 0, "matured": 0,
                                                    "hit_rate": None, "n": 0})
        bucket["records"] += 1
        settled = record.get("settled")
        if isinstance(settled, dict):
            bucket["matured"] += 1
            if settled.get("hit") is not None:
                bucket["_hits"] = bucket.get("_hits", 0) + (1 if settled["hit"] else 0)
                bucket["n"] += 1
    for bucket in by_action.values():
        if bucket["n"]:
            bucket["hit_rate"] = round(bucket.pop("_hits", 0) / bucket["n"], 6)
        else:
            bucket.pop("_hits", None)

    # 校准：只在有方向且 confidence 可用的已到期记录上做
    bins = {}
    for _day, _code, record in directional:
        label = _confidence_bin(record.get("confidence"))
        if label is None:
            continue
        bucket = bins.setdefault(label, {"bin": label, "n": 0, "hits": 0,
                                         "confidence_sum": 0.0, "brier_sum": 0.0})
        confidence = float(record["confidence"])
        hit = 1.0 if record["settled"]["hit"] else 0.0
        bucket["n"] += 1
        bucket["hits"] += int(hit)
        bucket["confidence_sum"] += confidence
        bucket["brier_sum"] += (confidence - hit) ** 2
    calibration_bins = []
    for label in sorted(bins):
        bucket = bins[label]
        calibration_bins.append({
            "bin": label,
            "n": bucket["n"],
            "mean_confidence": round(bucket["confidence_sum"] / bucket["n"], 6),
            "hit_rate": round(bucket["hits"] / bucket["n"], 6),
            "brier": round(bucket["brier_sum"] / bucket["n"], 6),
        })
    total_n = sum(b["n"] for b in calibration_bins)
    brier = (round(sum(b["brier"] * b["n"] for b in calibration_bins) / total_n, 6)
             if total_n else None)

    def _layer(key_fn):
        groups = {}
        for _day, _code, record in matured:
            key = key_fn(record)
            bucket = groups.setdefault(key, {"records": 0, "n": 0, "hits": 0})
            bucket["records"] += 1
            settled = record.get("settled") or {}
            if settled.get("hit") is not None:
                bucket["n"] += 1
                bucket["hits"] += 1 if settled["hit"] else 0
        rows = []
        for key in sorted(groups, key=lambda item: str(item)):
            bucket = groups[key]
            rows.append({
                "key": key,
                "records": bucket["records"],
                "n": bucket["n"],
                "hit_rate": (round(bucket["hits"] / bucket["n"], 6)
                             if bucket["n"] else None),
            })
        return rows

    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": _now_iso(),
        "ledger_dir": ledger_dir_path(ledger_dir),
        "days": len(days),
        "first_day": days[0] if days else None,
        "last_day": days[-1] if days else None,
        "records": len(all_records),
        "matured": len(matured),
        # issue #95：pending（数据未到）与 failed（真拿不到价）分开计数。
        "settle_status": settle_counts,
        "pending": pending,
        "failed": failed,
        "directional_matured": hit_n,
        "hit_rate": hit_rate,
        "horizon_days": HORIZON_DAYS,
        "by_action": by_action,
        "calibration": {"bins": calibration_bins, "brier": brier, "n": total_n},
        "by_flow_caliber": _layer(
            lambda r: (r.get("flow") or {}).get("caliber") or "unavailable"),
        "by_flow_available": _layer(
            lambda r: bool((r.get("flow") or {}).get("available"))),
        "by_valuation_pb": _layer(_pb_bucket),
    }


def format_report(report):
    """把 :func:`build_report` 的结果渲染成可粘贴的文本。"""
    lines = [
        f"前向台账：{report['ledger_dir']}",
        f"  日期范围 {report['first_day']} ~ {report['last_day']}"
        f"（{report['days']} 天），记录 {report['records']} 条",
        f"  已到期 {report['matured']} 条，其中有方向 {report['directional_matured']} 条，"
        f"方向命中率 {_pct(report['hit_rate'])}（持有 {report['horizon_days']} 交易日）",
        # issue #95：pending（数据未到）单独列出，不进失败率。
        f"  未到期 {report['settle_status']['not_matured']} 条，"
        f"数据未到 pending {report['pending']} 条，"
        f"结算失败 {report['failed']} 条",
    ]
    if report["by_action"]:
        lines.append("  按 action：")
        for action in sorted(report["by_action"], key=lambda item: str(item)):
            row = report["by_action"][action]
            lines.append(f"    {action:<20} 记录 {row['records']:>4}  到期 {row['matured']:>4}"
                         f"  有方向 {row['n']:>4}  命中率 {_pct(row['hit_rate'])}")
    lines.append("  校准（confidence 分箱）：")
    if not report["calibration"]["bins"]:
        lines.append("    （无已到期且有方向的记录）")
    for row in report["calibration"]["bins"]:
        lines.append(f"    {row['bin']:<12} n={row['n']:>4}  "
                     f"平均置信度 {_pct(row['mean_confidence'])}  "
                     f"实际命中率 {_pct(row['hit_rate'])}  Brier {row['brier']:.4f}")
    if report["calibration"]["brier"] is not None:
        lines.append(f"    总体 Brier {report['calibration']['brier']:.4f}"
                     f"（n={report['calibration']['n']}；R4 C 路径门槛：改善 ≥0.02）")
    for title, key in (("按 flow.caliber", "by_flow_caliber"),
                       ("按 flow.available", "by_flow_available"),
                       ("按 valuation.pb 分位", "by_valuation_pb")):
        lines.append(f"  {title}：")
        rows = report[key]
        if not rows:
            lines.append("    （无已到期记录）")
        for row in rows:
            lines.append(f"    {str(row['key']):<12} 记录 {row['records']:>4}"
                         f"  有方向 {row['n']:>4}  命中率 {_pct(row['hit_rate'])}")
    return "\n".join(lines)


def _pct(value):
    if value is None:
        return "—"
    return f"{100.0 * float(value):.2f}%"
