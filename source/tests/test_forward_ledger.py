# -*- coding: utf-8 -*-
"""P1 前向台账（issue #56）：每日记录 + 20 交易日到期结算 + 汇总校准。

覆盖：
  * ``forward_ledger.build_record``：字段齐全、缺失字段 null + reason、键集合固定；
  * ``record_day``：幂等（同日同 code 覆盖不追加）、按 ``data.end`` 分文件、
    断网 / 无缓存（CLI 无 JSON）时仍落盘；
  * ``compute_settlement`` / ``settle_all``：``open_{d+1+20}/open_{d+1} − 1``
    与手算一致、未到期跳过、价格缺失 null + reason；
  * ``build_report``：分箱 / 分层计数、空台账不崩；
  * ``tools/forward_log.py``：旧 ``record`` / ``evaluate`` 子命令保持可用。

全部离线：CLI 一律用假 runner，行情缓存写临时 CSV，不触网。
"""
from __future__ import annotations

import csv
import importlib.util
import json
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd
import pytest

SRC = Path(__file__).resolve().parents[1]
ROOT = SRC.parent
TOOLS = ROOT / "tools"
for path in (SRC, TOOLS):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from stock_analysis import forward_ledger as fl  # noqa: E402


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _bdays(end: date, n: int):
    days, cursor = [], end
    while len(days) < n:
        if cursor.weekday() < 5:
            days.append(cursor)
        cursor -= timedelta(days=1)
    return list(reversed(days))


def _cli_payload(code="002415", as_of="2026-11-12", action="buy",
                 confidence=0.62, flow=None, valuation=None,
                 market=None, index=None, status="ok", type_hint="stock"):
    """构造一份与 ``cli.py --json`` 同构的最小输出。

    ``type_hint`` 写进 ``request.type``：issue #95 起台账按它选结算价源。
    """
    return {
        "request": {"code": code, "type": type_hint, "type_hint": "auto"},
        "data": {"end": as_of, "last_close": 30.5, "bars": 1600},
        "market_regime": {
            "label": "Bull", "confidence": 0.71, "as_of": as_of,
            "definition": "ma20_ma60_return_volatility",
            "index": index if index is not None else {
                "label": "Shock", "confidence": 0.44, "as_of": as_of,
                "definition": "ma20_ma60_return_volatility",
                "source": "tencent", "bars": 600},
            **(market or {}),
        },
        "recommendation": {
            "status": status, "action": action,
            "confidence": confidence, "position_fraction": 0.4,
            "signal": {"position_state": "flat"},
            "target_position": 1.0, "horizon_days": [20, 20],
            "entry_zone": [29.8, 30.2], "stop_loss": 27.1, "take_profit": 35.4,
        },
        "evidence": {
            "flow": flow if flow is not None else {
                "available": True, "source": "sina_fund_flow_backup",
                "caliber": "sina_total_net", "as_of": as_of,
                "main_net_5d": None, "main_net_20d": None,
                "net_5d": -1.2e7, "net_20d": 3.4e7, "reason": None},
            "valuation": valuation if valuation is not None else {
                "available": True, "pe_percentile_3y": 12.5,
                "pb_percentile_3y": 22.0, "ps_percentile_3y": 31.0,
                "as_of": as_of, "reason": None},
        },
    }


def _runner(payload, exit_code=0, elapsed=9.8):
    def run(_code):
        return payload, exit_code, elapsed
    return run


def _write_bars(data_dir: Path, code: str, days, open_prices, name=None):
    data_dir.mkdir(parents=True, exist_ok=True)
    frame = pd.DataFrame({
        "open": open_prices,
        "high": [p * 1.01 for p in open_prices],
        "low": [p * 0.99 for p in open_prices],
        "close": [p * 1.005 for p in open_prices],
        "volume": [1000] * len(days),
    }, index=pd.to_datetime([d.isoformat() for d in days]))
    frame.index.name = "date"
    frame.to_csv(data_dir / (name or f"stock_{code}_qfq.csv"))


def _write_nav(data_dir: Path, code: str, days, navs, name=None):
    """场外基金缓存：``open == high == low == close == 单位净值``，无成交额列。

    与 ``meta.get_fund_daily`` 的 OTC 路径同构（净值没有日内波动）。
    """
    data_dir.mkdir(parents=True, exist_ok=True)
    frame = pd.DataFrame({
        "open": navs, "high": navs, "low": navs, "close": navs,
        "volume": [0] * len(days),
    }, index=pd.to_datetime([d.isoformat() for d in days]))
    frame.index.name = "date"
    frame.to_csv(data_dir / (name or f"fund_{code}.csv"))


@pytest.fixture()
def ledger_tmp(tmp_path):
    return tmp_path / "forward"


# ---------------------------------------------------------------------------
# 记录
# ---------------------------------------------------------------------------
def test_build_record_has_every_contract_field(ledger_tmp):
    rec = fl.build_record("002415", _cli_payload(), 0, 9.8, "2026-11-12",
                          now=datetime(2026, 11, 12, 18, 50))
    for key in ("as_of", "code", "action", "status", "confidence",
                "position_fraction", "entry_zone", "stop_loss", "take_profit",
                "horizon_days", "target_position", "cli_exit", "elapsed_s"):
        assert key in rec, key
    assert rec["signal"]["position_state"] == "flat"
    assert set(rec["flow"]) == set(fl.FLOW_KEYS + fl.FLOW_EXTRA_KEYS)
    assert set(rec["valuation"]) == set(fl.VALUATION_KEYS)
    assert set(rec["market_regime"]) == set(fl.REGIME_KEYS)
    assert set(rec["index_regime"]) == set(fl.REGIME_KEYS)
    assert rec["market_regime"] == {"label": "Bull", "confidence": 0.71}
    assert rec["index_regime"] == {"label": "Shock", "confidence": 0.44}
    assert rec["flow"]["caliber"] == "sina_total_net"
    assert rec["flow"]["net_5d"] == -1.2e7
    assert rec["valuation"]["pb_percentile_3y"] == 22.0
    assert rec["settled"] is None and rec["settle_reason"] is None
    # 新浪口径没有 main_net_*，必须显式 null + reason（口径隔离，不冒充）
    assert rec["flow"]["main_net_5d"] is None
    assert rec["missing"]["flow.main_net_5d"] == "field_absent_in_cli_output"


def test_default_runner_cwd_contains_cli():
    """回归：``default_runner`` 的工作目录必须真有 ``cli.py``。

    曾经的 bug：多上溯一级到仓库根，子进程 ``python cli.py`` 报 argparse
    usage error（退出码 2），台账只剩 27 个 null 字段。
    """
    src = Path(fl.source_dir())
    assert src.name == "source"
    assert (src / "cli.py").is_file()


def test_build_record_keeps_keys_when_cli_failed(ledger_tmp):
    """断网 / 无缓存：CLI 无 JSON（exit=30）仍必须落盘，且每个键都在。"""
    rec = fl.build_record("002415", None, 30, 3.1, "2026-11-12")
    assert rec["cli_exit"] == 30
    assert rec["action"] is None and rec["confidence"] is None
    assert set(rec["flow"]) == set(fl.FLOW_KEYS + fl.FLOW_EXTRA_KEYS)
    assert set(rec["valuation"]) == set(fl.VALUATION_KEYS)
    assert rec["missing"]["action"] == "cli_failed"
    assert rec["missing"]["flow.net_5d"] == "cli_failed"
    assert rec["missing"]["index_regime.label"] == "cli_failed"


def test_build_record_missing_field_gets_null_and_reason():
    payload = _cli_payload(flow={"available": False, "source": None,
                                 "caliber": None, "as_of": None, "net_5d": None,
                                 "net_20d": None, "reason": "cache_missing"},
                           valuation={"available": False, "pe_percentile_3y": None,
                                      "pb_percentile_3y": None,
                                      "ps_percentile_3y": None, "as_of": None,
                                      "reason": "cache_missing"})
    payload["market_regime"]["index"] = {"label": None, "confidence": None,
                                         "as_of": None, "definition": None,
                                         "source": None, "bars": None}
    payload["recommendation"]["entry_zone"] = None
    rec = fl.build_record("601066", payload, 0, 5.0, "2026-11-12")
    assert rec["entry_zone"] is None
    assert rec["missing"]["entry_zone"] == "field_absent_in_cli_output"
    assert rec["flow"]["available"] is False
    # 子字段的 null 原因继承块级 reason（cache_missing），不是笼统的 field_absent
    assert rec["missing"]["flow.caliber"] == "cache_missing"
    assert rec["missing"]["flow.net_5d"] == "cache_missing"
    assert rec["missing"]["valuation.pb_percentile_3y"] == "cache_missing"
    assert rec["index_regime"]["label"] is None
    assert rec["missing"]["index_regime.label"] == "field_absent_in_cli_output"
    # 块级 reason 本身不写进记录（台账键集合固定），只通过 missing 追溯
    assert "reason" not in rec["flow"] and "reason" not in rec["valuation"]


def test_record_day_is_idempotent_and_overwrites(ledger_tmp):
    payload = _cli_payload()
    fl.record_day(["002415"], ledger_dir=ledger_tmp, runner=_runner(payload))
    path = fl.day_path("2026-11-12", ledger_tmp)
    assert Path(path).exists()
    first = json.loads(Path(path).read_text(encoding="utf-8"))
    assert list(first["records"]) == ["002415"]

    # 同日重跑：覆盖，不追加（records 仍是 1 条；内容更新为新结果）
    payload2 = _cli_payload(action="hold", confidence=0.3)
    fl.record_day(["002415"], ledger_dir=ledger_tmp, runner=_runner(payload2))
    second = json.loads(Path(path).read_text(encoding="utf-8"))
    assert list(second["records"]) == ["002415"]
    assert second["records"]["002415"]["action"] == "hold"
    assert second["records"]["002415"]["confidence"] == 0.3


def test_record_day_splits_by_cli_data_end(ledger_tmp):
    """盘后跑到的 as_of 是 data.end，不是运行日：不同票可落不同文件。"""
    def runner(code):
        as_of = "2026-11-11" if code == "600519" else "2026-11-12"
        return _cli_payload(code=code, as_of=as_of), 0, 8.0
    written = fl.record_day(["600519", "002415"], ledger_dir=ledger_tmp,
                            runner=runner)
    assert sorted(written) == ["2026-11-11", "2026-11-12"]
    assert list(written["2026-11-11"]) == ["600519"]


def test_record_day_writes_even_when_runner_raises(ledger_tmp):
    def boom(_code):
        raise RuntimeError("network down")
    written = fl.record_day(["002415"], ledger_dir=ledger_tmp, runner=boom,
                            now=datetime(2026, 11, 12, 18, 50))
    assert len(written) == 1
    rec = list(written.values())[0]["002415"]
    assert rec["action"] is None
    assert rec["missing"]["action"].startswith("runner_error:")
    path = fl.day_path(rec["as_of"], ledger_tmp)
    assert Path(path).exists()


def test_record_day_keeps_other_codes_in_same_file(ledger_tmp):
    fl.record_day(["002415"], ledger_dir=ledger_tmp, runner=_runner(_cli_payload()))
    fl.record_day(["601066"], ledger_dir=ledger_tmp,
                  runner=_runner(_cli_payload(code="601066", action="hold")))
    payload = fl.load_day("2026-11-12", ledger_tmp)
    assert sorted(payload["records"]) == ["002415", "601066"]


# ---------------------------------------------------------------------------
# 结算
# ---------------------------------------------------------------------------
def _bars_with_hand_math():
    """as_of=2026-11-02（周一）；20 个交易日后是 2026-12-01。

    手算：entry = 2026-11-03 开盘 10.00，exit = 2026-12-01 开盘 12.00
          ret = 12.00/10.00 − 1 = +0.20

    交易日序列从 2026-12-01 倒推 29 根（2026-10-21 … 12-01）：
    ``as_of`` 下标 7、entry 下标 8、exit 下标 28（= 最后一根）。
    """
    days = _bdays(date(2026, 12, 1), 29)
    opens = [9.5] * 8 + [10.0] + [10.2] * 19 + [12.0]
    assert len(days) == len(opens) == 29
    assert days[7].isoformat() == "2026-11-02"
    assert days[8].isoformat() == "2026-11-03" and days[-1].isoformat() == "2026-12-01"
    return days, opens


def test_compute_settlement_matches_hand_calculation():
    days, opens = _bars_with_hand_math()
    frame = pd.DataFrame({"open": opens}, index=pd.to_datetime(
        [d.isoformat() for d in days]))
    record = {"as_of": "2026-11-02", "action": "buy"}
    settled, reason = fl.compute_settlement(record, frame)
    assert reason is None
    assert settled["entry_date"] == "2026-11-03"
    assert settled["settle_date"] == "2026-12-01"
    assert settled["entry_open"] == 10.0
    assert settled["exit_open"] == 12.0
    assert settled["ret_20d"] == 0.2
    assert settled["hit"] is True
    assert settled["horizon_days"] == 20
    assert settled["price_source"] == "local_qfq_cache"


def test_compute_settlement_uses_open_not_close():
    """用收盘价会得到不同答案：确保取的是 open。"""
    days, opens = _bars_with_hand_math()
    frame = pd.DataFrame({"open": opens, "close": [p * 2 for p in opens]},
                         index=pd.to_datetime([d.isoformat() for d in days]))
    settled, _ = fl.compute_settlement({"as_of": "2026-11-02", "action": "buy"},
                                       frame)
    assert settled["ret_20d"] == 0.2  # 2x 在比值里抵消


def test_compute_settlement_not_matured_is_skipped():
    days, opens = _bars_with_hand_math()
    frame = pd.DataFrame({"open": opens}, index=pd.to_datetime(
        [d.isoformat() for d in days]))
    settled, reason = fl.compute_settlement({"as_of": "2026-11-20",
                                             "action": "buy"}, frame)
    assert settled is None and reason == "not_matured"


def test_compute_settlement_missing_price_is_null_with_reason():
    settled, reason = fl.compute_settlement({"as_of": "2026-11-02",
                                             "action": "buy"}, None)
    assert settled is None and reason == "no_price_data"
    empty = pd.DataFrame({"open": []})
    assert fl.compute_settlement({"as_of": "2026-11-02"}, empty) == (
        None, "no_price_data")


def test_compute_settlement_no_bars_after_as_of():
    days = _bdays(date(2026, 11, 2), 3)
    frame = pd.DataFrame({"open": [10.0, 10.0, 10.0]},
                         index=pd.to_datetime([d.isoformat() for d in days]))
    settled, reason = fl.compute_settlement({"as_of": "2026-11-02"}, frame)
    assert settled is None and reason == "no_bars_after_as_of"


def test_settlement_hit_direction_for_sell():
    days, opens = _bars_with_hand_math()
    frame = pd.DataFrame({"open": opens}, index=pd.to_datetime(
        [d.isoformat() for d in days]))
    settled, _ = fl.compute_settlement({"as_of": "2026-11-02", "action": "sell"},
                                       frame)
    assert settled["ret_20d"] == 0.2
    assert settled["hit"] is False


def test_settlement_hold_has_no_direction():
    days, opens = _bars_with_hand_math()
    frame = pd.DataFrame({"open": opens}, index=pd.to_datetime(
        [d.isoformat() for d in days]))
    settled, _ = fl.compute_settlement({"as_of": "2026-11-02", "action": "hold"},
                                       frame)
    assert settled["ret_20d"] == 0.2
    assert settled["hit"] is None


def test_settle_all_writes_and_is_idempotent(ledger_tmp):
    days, opens = _bars_with_hand_math()
    frame = pd.DataFrame({"open": opens}, index=pd.to_datetime(
        [d.isoformat() for d in days]))
    fl.record_day(["002415"], ledger_dir=ledger_tmp,
                  runner=_runner(_cli_payload(as_of="2026-11-02")))
    stats = fl.settle_all(ledger_dir=ledger_tmp,
                          price_loader=lambda _code: frame)
    assert stats["settled"] == 1 and stats["not_matured"] == 0
    payload = fl.load_day("2026-11-02", ledger_tmp)
    settled = payload["records"]["002415"]["settled"]
    assert settled["ret_20d"] == 0.2 and settled["hit"] is True
    # 再跑一次：结果相同（幂等）
    again = fl.settle_all(ledger_dir=ledger_tmp,
                          price_loader=lambda _code: frame)
    assert again["settled"] == 1
    assert fl.load_day("2026-11-02", ledger_tmp)["records"]["002415"][
        "settled"] == settled


def test_settle_all_missing_price_keeps_null_with_reason(ledger_tmp):
    fl.record_day(["002415"], ledger_dir=ledger_tmp,
                  runner=_runner(_cli_payload(as_of="2026-11-02")))
    stats = fl.settle_all(ledger_dir=ledger_tmp, price_loader=lambda _code: None)
    assert stats["failed"] == 1
    assert stats["reasons"] == {"no_price_data": 1}
    record = fl.load_day("2026-11-02", ledger_tmp)["records"]["002415"]
    assert record["settled"] is None
    assert record["settle_reason"] == "no_price_data"


def test_settle_all_skips_not_matured(ledger_tmp):
    days, opens = _bars_with_hand_math()
    frame = pd.DataFrame({"open": opens}, index=pd.to_datetime(
        [d.isoformat() for d in days]))
    fl.record_day(["002415"], ledger_dir=ledger_tmp,
                  runner=_runner(_cli_payload(as_of="2026-11-20")))
    stats = fl.settle_all(ledger_dir=ledger_tmp,
                          price_loader=lambda _code: frame)
    assert stats["not_matured"] == 1 and stats["settled"] == 0
    record = fl.load_day("2026-11-20", ledger_tmp)["records"]["002415"]
    assert record["settled"] is None and record["settle_reason"] == "not_matured"
    # issue #95：未到期是「数据未到」，归非失败态，不计入 failed
    assert record["settle_status"] == "not_matured"
    assert stats["failed"] == 0


# ---------------------------------------------------------------------------
# 基金结算（issue #95 / PR #96 复验）
#
# 基金净值不在 ``stock_*_qfq.csv``，而在 ``fund_*.csv``。夹具
# ``tests/fixtures/fund/fund_999901.csv`` 是**真实公开 ETF 日线**的 140 个交易日
# 切片，代码已匿名化为 999901 —— 仓库里不出现任何真实基金/持仓代码。
#
# ⚠️ 它带 ``amount``/``postVol`` 且 ``open != close``，是**场内交易所 OHLC**，
# 不是场外净值（复验 P2-3）。所以它只用来验证「按来源选文件」这一层；场外
# NAV-to-NAV 的结算路径由 ``_write_nav`` 造的净值形态夹具单独覆盖
# （``test_settle_fund_record_success``）。
# ---------------------------------------------------------------------------
FUND_FIXTURE = Path(__file__).resolve().parent / "fixtures" / "fund" / "fund_999901.csv"
FUND_CODE = "999901"  # 虚构代码，不对应任何真实标的


def _fund_fixture_frame():
    frame = pd.read_csv(FUND_FIXTURE, index_col=0, parse_dates=True)
    return frame.sort_index()


def test_fund_file_kind_distinguishes_etf_ohlc_from_nav(tmp_path):
    """``fund_*.csv`` 的内容决定它是不是净值（复验 P1-3 / P2-3）。

    真实公开 ETF 日线夹具（``open != close``、带 ``amount``/``postVol``）必须
    被判成 ``etf``；``open == close`` 的净值形态必须被判成 ``fund``。两者都写
    在 ``fund_*.csv`` 里，只看文件名无法区分 —— 这正是原实现把 ETF 标成
    ``fund_nav`` 的原因。
    """
    data_dir = tmp_path / "kinds"
    data_dir.mkdir()
    (data_dir / f"fund_{FUND_CODE}.csv").write_text(
        FUND_FIXTURE.read_text(encoding="utf-8"), encoding="utf-8")
    assert fl._fund_file_kind(str(data_dir / f"fund_{FUND_CODE}.csv")) == "etf"

    nav_dir = tmp_path / "nav"
    _write_nav(nav_dir, FUND_CODE, _bdays(date(2026, 12, 1), 5),
               [1.0, 1.01, 1.02, 1.03, 1.04])
    assert fl._fund_file_kind(str(nav_dir / f"fund_{FUND_CODE}.csv")) == "fund"


def test_classify_instrument_etf_cache_only_does_not_claim_fund(tmp_path,
                                                                monkeypatch):
    """只有未复权的 ETF ``fund_*.csv`` 时不得判成 ``fund``（复验 P1-3）。

    口径无法确定 → 按 brief 走原 stock/qfq 路径，结算时缺文件即 ``no_price_data``。
    """
    data_dir = tmp_path / "etf_only"
    data_dir.mkdir()
    (data_dir / f"fund_{FUND_CODE}.csv").write_text(
        FUND_FIXTURE.read_text(encoding="utf-8"), encoding="utf-8")
    monkeypatch.setattr(fl, "DATA_DIR", str(data_dir))

    inst_type, source, caliber, basis = fl.classify_instrument(FUND_CODE)
    assert inst_type != "fund"
    assert source == "local_qfq_cache"
    assert caliber == "qfq_open_to_open"
    assert "未复权" in basis
    assert fl.load_price_bars(FUND_CODE, price_source=source) is None


def test_load_price_bars_reads_fund_csv_for_fund(tmp_path, monkeypatch):
    """``instrument_type='fund'`` 必须读 ``fund_*.csv``，不是 ``stock_*_qfq.csv``。"""
    data_dir = tmp_path / "data"
    days = _bdays(date(2026, 12, 1), 5)
    _write_nav(data_dir, FUND_CODE, days, [1.0, 1.01, 1.02, 1.03, 1.04])
    monkeypatch.setattr(fl, "DATA_DIR", str(data_dir))

    bars = fl.load_price_bars(FUND_CODE, "fund")
    assert bars is not None and len(bars) == 5
    assert float(bars["open"].iloc[0]) == 1.0
    # 明确指 fund 时不去找 stock_*：只有 fund_*.csv 存在也能读到
    assert not (data_dir / f"stock_{FUND_CODE}_qfq.csv").exists()


def test_load_price_bars_stock_path_unchanged(tmp_path, monkeypatch):
    """个股路径不回归：仍然只读 ``stock_*_qfq.csv``。"""
    data_dir = tmp_path / "data"
    _write_bars(data_dir, FUND_CODE, _bdays(date(2026, 12, 1), 5),
                [10.0, 10.1, 10.2, 10.3, 10.4])
    monkeypatch.setattr(fl, "DATA_DIR", str(data_dir))

    bars = fl.load_price_bars(FUND_CODE, "stock")
    assert bars is not None and len(bars) == 5
    assert float(bars["open"].iloc[0]) == 10.0


def test_load_price_bars_falls_back_to_fund_when_type_unknown(tmp_path, monkeypatch):
    """``load_price_bars`` 自身不再跨源探测：两个都为空时按 stock/qfq 只读一份。

    老记录「要不要读 ``fund_*.csv``」由 :func:`fl.resolve_price_plan` 在结算前
    一次性定好（它才看得到文件内容是不是净值）；loader 只负责读**那一个**来源。
    """
    data_dir = tmp_path / "data"
    days = _bdays(date(2026, 12, 1), 5)
    _write_nav(data_dir, FUND_CODE, days, [1.0, 1.01, 1.02, 1.03, 1.04])
    monkeypatch.setattr(fl, "DATA_DIR", str(data_dir))

    # 无 instrument_type / price_source → 默认 stock/qfq；stock 缓存不存在 → None
    assert fl.load_price_bars(FUND_CODE) is None
    assert fl.load_price_bars(FUND_CODE, None) is None
    # 明确指 fund 才读 fund_*.csv
    assert fl.load_price_bars(FUND_CODE, "fund") is not None
    assert fl.load_price_bars(FUND_CODE, price_source="fund_nav") is not None


def test_load_price_bars_never_switches_source_on_corrupt_file(tmp_path,
                                                               monkeypatch):
    """指定来源存在但损坏 → ``None``，**不滑到另一个口径**（复验 P1-1）。"""
    data_dir = tmp_path / "corrupt"
    data_dir.mkdir()
    (data_dir / f"stock_{FUND_CODE}_qfq.csv").write_bytes(b"\x00\xff junk\n")
    (data_dir / f"fund_{FUND_CODE}.csv").write_text(
        FUND_FIXTURE.read_text(encoding="utf-8"), encoding="utf-8")
    monkeypatch.setattr(fl, "DATA_DIR", str(data_dir))

    assert fl.load_price_bars(FUND_CODE, price_source="local_qfq_cache") is None
    assert fl.load_price_bars(FUND_CODE, "stock") is None

def _nav_frame(days, navs):
    """场外净值序列：``open == high == low == close``（OTC 无日内波动）。"""
    return pd.DataFrame({"open": navs, "high": navs, "low": navs,
                         "close": navs, "volume": [0] * len(days)},
                        index=pd.to_datetime([d.isoformat() for d in days]))


def test_settle_fund_record_success(ledger_tmp, tmp_path, monkeypatch):
    """场外基金（NAV 口径）结算成功，``price_source=fund_nav``、口径 ``nav_to_nav``。

    用**净值形态**缓存（``open == high == low == close``，无成交额列）离线回归：
    ``as_of=2026-09-01`` → entry 2026-09-02 净值 1.0250 → exit 2026-10-08
    净值 0.9745，手算 ``0.9745/1.0250 − 1 = -0.049268``。

    这条用例覆盖 OTC 路径（净值对净值），与场内 ETF 的交易所 OHLC 不同 ——
    PR #96 原夹具 ``fund_999901.csv`` 是 ETF 行情（``open != close``、带
    ``amount``/``postVol``），只覆盖了 ETF 路径（复验 P2-3）。
    """
    data_dir = tmp_path / "nav_data"
    days = _bdays(date(2026, 12, 1), 29)
    navs = [1.0] * 8 + [1.0250] + [1.01] * 19 + [0.9745]
    _write_nav(data_dir, FUND_CODE, days, navs)
    monkeypatch.setattr(fl, "DATA_DIR", str(data_dir))

    fl.record_day([FUND_CODE], ledger_dir=ledger_tmp,
                  runner=_runner(_cli_payload(code=FUND_CODE, as_of="2026-11-02")))
    record = fl.load_day("2026-11-02", ledger_tmp)["records"][FUND_CODE]
    # 只有 fund_*.csv 且是净值形态 → 记录阶段就判定为场外基金
    assert record["instrument_type"] == "fund"
    assert record["price_source"] == "fund_nav"
    assert record["price_caliber"] == "nav_to_nav"

    stats = fl.settle_all(ledger_dir=ledger_tmp)
    assert stats["settled"] == 1 and stats["failed"] == 0

    record = fl.load_day("2026-11-02", ledger_tmp)["records"][FUND_CODE]
    assert record["settle_status"] == "settled"
    settled = record["settled"]
    assert settled["entry_date"] == "2026-11-03"
    assert settled["settle_date"] == "2026-12-01"
    assert settled["entry_open"] == pytest.approx(1.0250)
    assert settled["exit_open"] == pytest.approx(0.9745)
    assert settled["ret_20d"] == pytest.approx(round(0.9745 / 1.0250 - 1, 6))
    assert settled["ret_20d"] == pytest.approx(-0.049268)
    assert settled["hit"] is False  # buy 且收益为负
    assert settled["price_source"] == "fund_nav"
    assert record["price_caliber"] == "nav_to_nav"


def test_settle_etf_keeps_qfq_caliber(ledger_tmp, tmp_path, monkeypatch):
    """场内 ETF 维持 qfq 口径（复验 P1-3）：不因为 CLI auto 判 fund 就换源。

    ``stock_510300_qfq.csv`` 与 ``fund_510300.csv`` 同时存在时，记录阶段必须
    判成 ``etf`` 并锁 ``local_qfq_cache`` / ``qfq_open_to_open``；即使 CLI 的
    ``request.type`` 写着 ``fund``（``auto`` 回落的典型结果）也不采纳。
    """
    data_dir = tmp_path / "etf_data"
    days, opens = _bars_with_hand_math()
    _write_bars(data_dir, "510300", days, opens)
    # fund_*.csv 放一份**未复权**交易所行情（与 qfq 不同值），诱使实现换源
    _write_bars(data_dir, "510300", days, [p * 0.9 for p in opens],
                name="fund_510300.csv")
    monkeypatch.setattr(fl, "DATA_DIR", str(data_dir))

    fl.record_day(["510300"], ledger_dir=ledger_tmp,
                  runner=_runner(_cli_payload(code="510300", type_hint="fund",
                                              as_of="2026-11-02")))
    record = fl.load_day("2026-11-02", ledger_tmp)["records"]["510300"]
    assert record["instrument_type"] == "etf"
    assert record["price_source"] == "local_qfq_cache"
    assert record["price_caliber"] == "qfq_open_to_open"

    stats = fl.settle_all(ledger_dir=ledger_tmp)
    assert stats["settled"] == 1
    record = fl.load_day("2026-11-02", ledger_tmp)["records"]["510300"]
    settled = record["settled"]
    # qfq 序列手算 +0.20；若误读 fund_510300.csv（0.9 倍）仍是 +0.20（比值不变），
    # 故同时断言来源与口径，并单独验证绝对值来自 qfq 文件。
    assert settled["ret_20d"] == pytest.approx(0.2)
    assert settled["price_source"] == "local_qfq_cache"
    assert record["price_caliber"] == "qfq_open_to_open"


def test_settle_etf_does_not_switch_to_unadjusted_fund_cache(
        ledger_tmp, tmp_path, monkeypatch):
    """ETF 的 qfq 缓存缺失时**不得**换用未复权的 ``fund_*.csv``。

    记录里锁的是 ``local_qfq_cache``；该文件缺失 → ``no_price_data``（failed），
    即使旁边有 ``fund_*.csv`` 也不读（复验 P1-1 / P1-3）。
    """
    data_dir = tmp_path / "etf_missing_qfq"
    days, opens = _bars_with_hand_math()
    _write_bars(data_dir, "510300", days, opens)
    monkeypatch.setattr(fl, "DATA_DIR", str(data_dir))

    fl.record_day(["510300"], ledger_dir=ledger_tmp,
                  runner=_runner(_cli_payload(code="510300", as_of="2026-11-02")))
    record = fl.load_day("2026-11-02", ledger_tmp)["records"]["510300"]
    assert record["price_source"] == "local_qfq_cache"

    # 记录之后 qfq 缓存消失、只剩未复权的 fund_*.csv
    (data_dir / "stock_510300_qfq.csv").unlink()
    _write_bars(data_dir, "510300", days, [p * 0.9 for p in opens],
                name="fund_510300.csv")

    stats = fl.settle_all(ledger_dir=ledger_tmp)
    assert stats["settled"] == 0
    assert stats["failed"] == 1
    assert stats["failed_reasons"] == {"no_price_data": 1}
    record = fl.load_day("2026-11-02", ledger_dir=ledger_tmp)["records"]["510300"]
    assert record["settled"] is None
    assert record["settle_reason"] == "no_price_data"
    # 拒绝换源这件事必须可见
    assert stats["warnings"] and "拒绝换源" in stats["warnings"][0]


def test_stock_corrupt_cache_does_not_fall_through_to_fund(
        ledger_tmp, tmp_path, monkeypatch):
    """个股缓存损坏 → ``failed``，**不得**静默滑到 ``fund_*.csv`` 结算（P1-1）。

    复验实测的回归：stock 缓存写成二进制垃圾时旧实现会继续读 fund 缓存，
    用未复权价算出收益并标 ``fund_nav``，在汇总里看起来完全正常。
    """
    data_dir = tmp_path / "corrupt_data"
    days, opens = _bars_with_hand_math()
    _write_bars(data_dir, "600519", days, opens)
    _write_bars(data_dir, "600519", days, [p * 0.85 for p in opens],
                name="fund_600519.csv")
    (data_dir / "stock_600519_qfq.csv").write_bytes(b"\x00\xff\xfe binary junk\n")
    monkeypatch.setattr(fl, "DATA_DIR", str(data_dir))

    fl.record_day(["600519"], ledger_dir=ledger_tmp,
                  runner=_runner(_cli_payload(as_of="2026-11-02")))
    record = fl.load_day("2026-11-02", ledger_tmp)["records"]["600519"]
    assert record["instrument_type"] == "stock"
    assert record["price_source"] == "local_qfq_cache"

    stats = fl.settle_all(ledger_dir=ledger_tmp)
    assert stats["settled"] == 0, "损坏的 stock 缓存不得变成 settled"
    assert stats["failed"] == 1
    assert stats["failed_reasons"] == {"no_price_data": 1}
    record = fl.load_day("2026-11-02", ledger_tmp)["records"]["600519"]
    assert record["settled"] is None
    assert record["settle_reason"] == "no_price_data"
    assert record["settle_status"] == "failed"


def test_stock_missing_cache_does_not_fall_through_to_fund(
        ledger_tmp, tmp_path, monkeypatch):
    """个股缓存缺失（只剩 ``fund_*.csv``）→ ``failed``，同样不换源。"""
    data_dir = tmp_path / "missing_stock"
    days, opens = _bars_with_hand_math()
    _write_bars(data_dir, "600519", days, [p * 0.85 for p in opens],
                name="fund_600519.csv")
    monkeypatch.setattr(fl, "DATA_DIR", str(data_dir))

    fl.record_day(["600519"], ledger_dir=ledger_tmp,
                  runner=_runner(_cli_payload(as_of="2026-11-02")))
    record = fl.load_day("2026-11-02", ledger_tmp)["records"]["600519"]
    # 只有 fund_*.csv，但内容是交易所 OHLC（不是净值）→ 仍按 stock/qfq 记录，
    # 且**不**把它当成基金净值来源。
    assert record["price_source"] == "local_qfq_cache"

    stats = fl.settle_all(ledger_dir=ledger_tmp)
    assert stats["settled"] == 0 and stats["failed"] == 1


def test_instrument_type_not_taken_from_cli_request_type(ledger_tmp, tmp_path,
                                                         monkeypatch):
    """``request.type='fund'`` 不得把个股标成 fund（复验 P1-2）。

    复验实测：个股通道失败后 ``auto`` 回落到基金通道，``request.type`` 变成
    ``fund``，旧实现直接落 ``instrument_type='fund'`` → 此后永远按未复权结算。
    类型必须来自标的身份（数据文件类型 + 代码段），不是取数通道。
    """
    data_dir = tmp_path / "type_data"
    days, opens = _bars_with_hand_math()
    _write_bars(data_dir, "600519", days, opens)
    monkeypatch.setattr(fl, "DATA_DIR", str(data_dir))

    payload = _cli_payload(as_of="2026-11-02", type_hint="fund")
    payload["request"]["type"] = "fund"
    fl.record_day(["600519"], ledger_dir=ledger_tmp, runner=_runner(payload))

    record = fl.load_day("2026-11-02", ledger_tmp)["records"]["600519"]
    assert record["instrument_type"] == "stock"
    assert record["price_source"] == "local_qfq_cache"
    assert record["price_caliber"] == "qfq_open_to_open"
    assert "stock_600519_qfq.csv" in record["instrument_basis"]

    stats = fl.settle_all(ledger_dir=ledger_tmp)
    assert stats["settled"] == 1
    record = fl.load_day("2026-11-02", ledger_tmp)["records"]["600519"]
    assert record["settled"]["price_source"] == "local_qfq_cache"
    assert record["price_caliber"] == "qfq_open_to_open"


def test_settle_fund_missing_price_is_failed(ledger_tmp, tmp_path, monkeypatch):
    """基金缺价：``no_price_data`` 仍是 failed（真拿不到价，不是数据未到）。"""
    empty_dir = tmp_path / "empty_data"
    empty_dir.mkdir()
    monkeypatch.setattr(fl, "DATA_DIR", str(empty_dir))
    fl.record_day([FUND_CODE], ledger_dir=ledger_tmp,
                  runner=_runner(_cli_payload(code=FUND_CODE, type_hint="fund",
                                              as_of="2026-09-01")))
    stats = fl.settle_all(ledger_dir=ledger_tmp)
    assert stats["settled"] == 0 and stats["failed"] == 1
    assert stats["reasons"] == {"no_price_data": 1}
    record = fl.load_day("2026-09-01", ledger_tmp)["records"][FUND_CODE]
    assert record["settled"] is None and record["settle_reason"] == "no_price_data"
    assert record["settle_status"] == "failed"


def test_settle_no_bars_after_as_of_is_pending(ledger_tmp):
    """``no_bars_after_as_of`` 归 pending（数据未到），不计入 failed（issue #95）。"""
    days = _bdays(date(2026, 11, 2), 3)
    frame = pd.DataFrame({"open": [10.0, 10.0, 10.0]},
                         index=pd.to_datetime([d.isoformat() for d in days]))
    fl.record_day(["600519"], ledger_dir=ledger_tmp,
                  runner=_runner(_cli_payload(as_of="2026-11-02")))
    stats = fl.settle_all(ledger_dir=ledger_tmp,
                          price_loader=lambda _code: frame)
    assert stats["pending"] == 1
    assert stats["failed"] == 0
    assert stats["settled"] == 0
    assert stats["reasons"] == {"no_bars_after_as_of": 1}
    record = fl.load_day("2026-11-02", ledger_tmp)["records"]["600519"]
    assert record["settled"] is None
    assert record["settle_reason"] == "no_bars_after_as_of"
    assert record["settle_status"] == "pending"


def test_settle_fund_record_without_price_source_uses_nav(ledger_tmp, tmp_path,
                                                          monkeypatch):
    """老记录（无 ``price_source``）：只有**净值形态**的 ``fund_*.csv`` 才判 fund。

    brief 的默认是 stock/qfq；只有当 ``fund_*.csv`` 明确是单位净值
    （``open == close``、无交易所成交额列）且没有 ``stock_*_qfq.csv`` 时才判
    场外基金，且判定依据必须可追溯。
    """
    data_dir = tmp_path / "legacy_nav"
    days, opens = _bars_with_hand_math()
    _write_nav(data_dir, FUND_CODE, days, opens)
    monkeypatch.setattr(fl, "DATA_DIR", str(data_dir))

    _write_settled_day(ledger_tmp, "2026-11-02", {
        FUND_CODE: {"as_of": "2026-11-02", "code": FUND_CODE,
                    "action": "buy", "confidence": 0.5,
                    "settled": None, "settle_reason": None},
    })
    stats = fl.settle_all(ledger_dir=ledger_tmp)
    assert stats["settled"] == 1
    assert "单位净值" in stats["basis"][FUND_CODE]
    record = fl.load_day("2026-11-02", ledger_tmp)["records"][FUND_CODE]
    assert record["settled"]["price_source"] == "fund_nav"
    # 老记录没有 price_caliber 键；汇总按价源推断为净值对净值
    assert fl._record_price_caliber(record) == "nav_to_nav"


def test_settle_legacy_record_defaults_to_stock_when_fund_cache_is_etf_ohlc(
        ledger_tmp, tmp_path, monkeypatch):
    """老记录 + 只有**交易所 OHLC** 的 ``fund_*.csv`` → 按 stock/qfq，不标 fund_nav。

    ``fund_510300.csv`` 这类未复权交易所行情不是净值；把它当 ``fund_nav`` 结算
    就是假话（复验 P1-3）。老记录默认 stock/qfq，该文件缺失即 ``no_price_data``。
    """
    data_dir = tmp_path / "legacy_etf"
    days, opens = _bars_with_hand_math()
    _write_bars(data_dir, "510300", days, opens, name="fund_510300.csv")
    monkeypatch.setattr(fl, "DATA_DIR", str(data_dir))

    _write_settled_day(ledger_tmp, "2026-11-02", {
        "510300": {"as_of": "2026-11-02", "code": "510300",
                   "action": "buy", "confidence": 0.5,
                   "settled": None, "settle_reason": None},
    })
    stats = fl.settle_all(ledger_dir=ledger_tmp)
    assert stats["settled"] == 0
    assert stats["failed_reasons"] == {"no_price_data": 1}
    record = fl.load_day("2026-11-02", ledger_tmp)["records"]["510300"]
    assert record["settled"] is None
    assert record["settle_reason"] == "no_price_data"


def test_settle_stock_price_source_stays_local_qfq(ledger_tmp):
    """个股路径不回归：``price_source`` 仍是 ``local_qfq_cache``。"""
    days, opens = _bars_with_hand_math()
    frame = pd.DataFrame({"open": opens}, index=pd.to_datetime(
        [d.isoformat() for d in days]))
    fl.record_day(["600519"], ledger_dir=ledger_tmp,
                  runner=_runner(_cli_payload(as_of="2026-11-02")))
    fl.settle_all(ledger_dir=ledger_tmp, price_loader=lambda _code: frame)
    record = fl.load_day("2026-11-02", ledger_tmp)["records"]["600519"]
    assert record["instrument_type"] == "stock"
    assert record["settled"]["price_source"] == "local_qfq_cache"


def test_report_counts_pending_separately(ledger_tmp):
    """汇总里 pending 单独计数，不进 failed。"""
    _write_settled_day(ledger_tmp, "2026-11-02", {
        "000001": {"as_of": "2026-11-02", "action": "buy", "confidence": 0.5,
                   "settled": None, "settle_reason": "no_bars_after_as_of",
                   "settle_status": "pending"},
        "000002": {"as_of": "2026-11-02", "action": "buy", "confidence": 0.5,
                   "settled": None, "settle_reason": "no_price_data",
                   "settle_status": "failed"},
        "000003": {"as_of": "2026-11-02", "action": "buy", "confidence": 0.5,
                   "settled": None, "settle_reason": "not_matured",
                   "settle_status": "not_matured"},
    })
    report = fl.build_report(ledger_dir=ledger_tmp)
    assert report["pending"] == 1
    assert report["failed"] == 1
    assert report["settle_status"]["not_matured"] == 1
    text = fl.format_report(report)
    assert "pending 1" in text and "结算失败 1" in text


def test_report_infers_pending_for_legacy_records(ledger_tmp):
    """老台账没有 ``settle_status``：按 ``settle_reason`` 反推，口径一致。"""
    _write_settled_day(ledger_tmp, "2026-11-02", {
        "000001": {"as_of": "2026-11-02", "action": "buy", "confidence": 0.5,
                   "settled": None, "settle_reason": "no_bars_after_as_of"},
    })
    report = fl.build_report(ledger_dir=ledger_tmp)
    assert report["pending"] == 1 and report["failed"] == 0


def test_report_never_evaluated_records_are_pending_not_settled(ledger_tmp):
    """只 ``record`` 没 ``evaluate`` 的记录**不得**算成 ``settled``（复验 P2-1）。

    复验实测：``settled=None / settle_reason=None / settle_status=None`` 时旧
    反推逻辑 ``settle_status_for(None) == "settled"``，汇总里凭空多出一条已结算。
    """
    _write_settled_day(ledger_tmp, "2026-11-12", {
        "000001": {"as_of": "2026-11-12", "code": "000001", "action": "buy",
                   "confidence": 0.5, "flow": {}, "valuation": {},
                   "settled": None, "settle_reason": None},
    })
    report = fl.build_report(ledger_dir=ledger_tmp)
    assert report["records"] == 1 and report["matured"] == 0
    assert report["settle_status"]["settled"] == 0
    assert report["pending"] == 1
    assert report["failed"] == 0
    text = fl.format_report(report)
    assert "待结算 pending 1 条" in text and "结算失败 0 条" in text


def test_record_day_then_report_does_not_claim_settled(ledger_tmp):
    """真实记录路径同样不得在未 evaluate 时声称已结算。"""
    fl.record_day(["600519"], ledger_dir=ledger_tmp,
                  runner=_runner(_cli_payload(as_of="2026-11-02")))
    report = fl.build_report(ledger_dir=ledger_tmp)
    assert report["matured"] == 0
    assert report["settle_status"]["settled"] == 0
    assert report["pending"] == 1


# ---------------------------------------------------------------------------
# 汇总
# ---------------------------------------------------------------------------
def _write_settled_day(ledger_dir, day, records):
    payload = {"schema_version": fl.SCHEMA_VERSION, "as_of": day,
               "records": records}
    fl._atomic_write(fl.day_path(day, ledger_dir), payload)


def _settled(action, confidence, ret, hit, flow_caliber="sina_total_net",
             pb_pct=50.0, valuation_available=True):
    return {
        "as_of": "2026-11-02", "code": "002415", "action": action,
        "confidence": confidence,
        "flow": {"available": True, "source": "sina_fund_flow_backup",
                 "caliber": flow_caliber, "net_5d": None, "net_20d": None,
                 "as_of": "2026-11-02", "main_net_5d": None, "main_net_20d": None},
        "valuation": {"available": valuation_available, "pe_percentile_3y": None,
                      "pb_percentile_3y": pb_pct, "ps_percentile_3y": None,
                      "as_of": "2026-11-02"},
        "settled": {"ret_20d": ret, "hit": hit, "settle_date": "2026-12-01",
                    "price_source": "local_qfq_cache"},
        "settle_reason": None,
    }


def test_report_empty_ledger_does_not_crash(ledger_tmp):
    report = fl.build_report(ledger_dir=ledger_tmp)
    assert report["days"] == 0 and report["records"] == 0
    assert report["hit_rate"] is None and report["matured"] == 0
    assert report["calibration"]["bins"] == []
    assert report["calibration"]["brier"] is None
    text = fl.format_report(report)
    assert "前向台账" in text


def test_report_bins_and_layers(ledger_tmp):
    _write_settled_day(ledger_tmp, "2026-11-02", {
        "000001": _settled("buy", 0.85, 0.10, True),
        "000002": _settled("buy", 0.85, -0.05, False),
        "000003": _settled("buy", 0.55, 0.03, True, pb_pct=10.0),
        "000004": _settled("sell", 0.35, 0.04, False, pb_pct=90.0),
        "000005": _settled("hold", 0.30, 0.02, None,
                           valuation_available=False),
        "000006": _settled("buy", 0.10, 0.01, True, flow_caliber=None),
    })
    report = fl.build_report(ledger_dir=ledger_tmp)
    assert report["days"] == 1 and report["records"] == 6
    assert report["matured"] == 6
    assert report["directional_matured"] == 5  # hold 没有方向
    assert report["hit_rate"] == pytest.approx(3 / 5)  # True, False, True, False, True

    bins = {row["bin"]: row for row in report["calibration"]["bins"]}
    assert bins["[0.8,1.0)"]["n"] == 2
    assert bins["[0.8,1.0)"]["hit_rate"] == pytest.approx(0.5)
    assert bins["[0.8,1.0)"]["mean_confidence"] == pytest.approx(0.85)
    assert bins["[0.4,0.6)"]["n"] == 1
    assert bins["[0.4,0.6)"]["hit_rate"] == pytest.approx(1.0)
    assert bins["[0.2,0.4)"]["n"] == 1  # sell 0.35，未命中
    assert bins["[0.2,0.4)"]["hit_rate"] == pytest.approx(0.0)
    assert bins["[0.0,0.2)"]["n"] == 1
    assert report["calibration"]["n"] == 5

    # Brier 手算：0.85 对 1 → 0.0225；0.85 对 0 → 0.7225；0.55 对 1 → 0.2025；
    # 0.35 对 0 → 0.1225；0.10 对 1 → 0.81；均值 = 1.88/5 = 0.376
    assert report["calibration"]["brier"] == pytest.approx(0.376)

    actions = report["by_action"]
    assert actions["hold"]["matured"] == 1 and actions["hold"]["n"] == 0
    assert actions["buy"]["n"] == 4
    assert actions["buy"]["hit_rate"] == pytest.approx(0.75)

    caliber = {row["key"]: row for row in report["by_flow_caliber"]}
    assert caliber["sina_total_net"]["records"] == 5
    assert caliber["sina_total_net"]["n"] == 4  # hold 不算方向
    assert caliber["unavailable"]["records"] == 1

    pb = {row["key"]: row for row in report["by_valuation_pb"]}
    assert pb["cheap"]["records"] == 1
    assert pb["mid"]["records"] == 3  # 000001 / 000002 / 000006（pb=50）
    assert pb["rich"]["records"] == 1
    assert pb["unavailable"]["records"] == 1

    text = fl.format_report(report)
    assert "Brier" in text and "按 flow.caliber" in text


def test_report_ignores_unsettled_records(ledger_tmp):
    _write_settled_day(ledger_tmp, "2026-11-12", {
        "000001": {"as_of": "2026-11-12", "code": "000001", "action": "buy",
                   "confidence": 0.5, "flow": {}, "valuation": {},
                   "settled": None, "settle_reason": "not_matured"},
    })
    report = fl.build_report(ledger_dir=ledger_tmp)
    assert report["records"] == 1 and report["matured"] == 0
    assert report["by_action"]["buy"]["records"] == 1
    assert report["by_action"]["buy"]["matured"] == 0


# ---------------------------------------------------------------------------
# tools/forward_log.py 兼容
# ---------------------------------------------------------------------------
def _load_forward_log():
    spec = importlib.util.spec_from_file_location(
        "forward_log_tool", TOOLS / "forward_log.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_tool_record_writes_ledger_and_legacy_csv(tmp_path, monkeypatch):
    tool = _load_forward_log()
    monkeypatch.setattr(tool, "EVID", tmp_path / "evidence")
    monkeypatch.setattr(tool, "LOG", tmp_path / "evidence" / "forward_log.csv")
    ledger_dir = tmp_path / "forward"
    written = tool.record(["002415"], ledger_dir=ledger_dir,
                          runner=_runner(_cli_payload()))
    assert list(written) == ["2026-11-12"]
    ledger_payload = fl.load_day("2026-11-12", ledger_dir)
    assert ledger_payload["records"]["002415"]["action"] == "buy"
    rows = list(csv.DictReader(tool.LOG.open(encoding="utf-8", newline="")))
    assert len(rows) == 1
    assert rows[0]["code"] == "002415" and rows[0]["action"] == "buy"
    assert rows[0]["close"] == "30.5"  # 旧字段仍然来自 CLI payload
    # 同一天重跑：CSV 不重复追加
    tool.record(["002415"], ledger_dir=ledger_dir, runner=_runner(_cli_payload()))
    rows = list(csv.DictReader(tool.LOG.open(encoding="utf-8", newline="")))
    assert len(rows) == 1


def test_tool_record_ledger_only_skips_csv(tmp_path, monkeypatch):
    tool = _load_forward_log()
    monkeypatch.setattr(tool, "EVID", tmp_path / "evidence")
    monkeypatch.setattr(tool, "LOG", tmp_path / "evidence" / "forward_log.csv")
    ledger_dir = tmp_path / "forward"
    tool.record(["002415"], ledger_dir=ledger_dir, ledger_only=True,
                runner=_runner(_cli_payload()))
    assert not tool.LOG.exists()
    assert fl.load_day("2026-11-12", ledger_dir)["records"]


def test_tool_record_survives_cli_failure(tmp_path, monkeypatch):
    """断网 / 无缓存：CLI 返回 exit=30 且 stdout 不是 JSON，台账仍落盘。"""
    tool = _load_forward_log()
    monkeypatch.setattr(tool, "EVID", tmp_path / "evidence")
    monkeypatch.setattr(tool, "LOG", tmp_path / "evidence" / "forward_log.csv")
    ledger_dir = tmp_path / "forward"
    tool.record(["002415"], ledger_dir=ledger_dir,
                runner=_runner(None, exit_code=30, elapsed=2.0))
    payload = fl.load_day(fl._local_expected_trade_date(), ledger_dir)
    assert payload is not None
    record = payload["records"]["002415"]
    assert record["action"] is None and record["cli_exit"] == 30
    assert record["missing"]["action"] == "cli_failed"
    assert not tool.LOG.exists()  # CSV 需要 payload，无 payload 时不写假行


def test_tool_evaluate_output_does_not_pair_zero_failed_with_reasons(
        tmp_path, monkeypatch, capsys):
    """``evaluate`` 不得出现「结算失败 0 条」后面挂一串 pending 原因（P2-2）。

    复验实测的原文：
    ``…数据未到 pending 10 条，结算失败 0 条（{'no_bars_after_as_of': 10}）``
    —— 计数与原因自相矛盾。pending 与 failed 的原因必须分开列。
    """
    tool = _load_forward_log()
    monkeypatch.setattr(tool, "EVID", tmp_path / "evidence")
    monkeypatch.setattr(tool, "LOG", tmp_path / "evidence" / "forward_log.csv")
    monkeypatch.setattr(tool, "EVAL", tmp_path / "evidence" / "forward_eval.csv")
    ledger_dir = tmp_path / "forward"
    days = _bdays(date(2026, 11, 2), 3)
    frame = pd.DataFrame({"open": [10.0, 10.0, 10.0]},
                         index=pd.to_datetime([d.isoformat() for d in days]))
    monkeypatch.setattr(fl, "load_price_bars", lambda _code: frame)
    tool.record(["600519"], ledger_dir=ledger_dir,
                runner=_runner(_cli_payload(as_of="2026-11-02")))
    capsys.readouterr()
    tool.evaluate(ledger_dir=ledger_dir)
    out = capsys.readouterr().out
    assert "待结算 pending 1 条，结算失败 0 条" in out
    # failed 行里不得出现 pending 的原因
    failed_line = next(line for line in out.splitlines()
                       if "JSON 台账结算" in line)
    assert "no_bars_after_as_of" not in failed_line
    assert "pending 原因" in out and "no_bars_after_as_of" in out


def test_tool_evaluate_and_report(tmp_path, monkeypatch):
    tool = _load_forward_log()
    monkeypatch.setattr(tool, "EVID", tmp_path / "evidence")
    monkeypatch.setattr(tool, "LOG", tmp_path / "evidence" / "forward_log.csv")
    monkeypatch.setattr(tool, "EVAL", tmp_path / "evidence" / "forward_eval.csv")
    ledger_dir = tmp_path / "forward"
    days, opens = _bars_with_hand_math()
    frame = pd.DataFrame({"open": opens}, index=pd.to_datetime(
        [d.isoformat() for d in days]))
    monkeypatch.setattr(fl, "load_price_bars", lambda _code: frame)
    tool.record(["002415"], ledger_dir=ledger_dir,
                runner=_runner(_cli_payload(as_of="2026-11-02")))
    tool.evaluate(ledger_dir=ledger_dir)
    record = fl.load_day("2026-11-02", ledger_dir)["records"]["002415"]
    assert record["settled"]["ret_20d"] == 0.2
    data = tool.report(ledger_dir=ledger_dir, as_json=True)
    assert data["matured"] == 1 and data["hit_rate"] == 1.0
