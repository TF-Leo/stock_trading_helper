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


def _write_bars(data_dir: Path, code: str, days, open_prices):
    data_dir.mkdir(parents=True, exist_ok=True)
    frame = pd.DataFrame({
        "open": open_prices,
        "high": [p * 1.01 for p in open_prices],
        "low": [p * 0.99 for p in open_prices],
        "close": [p * 1.005 for p in open_prices],
        "volume": [1000] * len(days),
    }, index=pd.to_datetime([d.isoformat() for d in days]))
    frame.index.name = "date"
    frame.to_csv(data_dir / f"stock_{code}_qfq.csv")


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
# 场外基金结算（issue #95）
#
# 基金净值不在 ``stock_*_qfq.csv``，而在 ``fund_*.csv``。夹具
# ``tests/fixtures/fund/fund_999901.csv`` 是**真实公开 ETF 日线**的 140 个交易日
# 切片，代码已匿名化为 999901 —— 仓库里不出现任何真实基金/持仓代码。
# 场外 OTC 基金走同一张表（``get_fund_daily`` 用单位净值补齐 open），
# 所以这条路径与 ETF 共用。
# ---------------------------------------------------------------------------
FUND_FIXTURE = Path(__file__).resolve().parent / "fixtures" / "fund" / "fund_999901.csv"
FUND_CODE = "999901"  # 虚构代码，不对应任何真实标的


def _fund_fixture_frame():
    frame = pd.read_csv(FUND_FIXTURE, index_col=0, parse_dates=True)
    return frame.sort_index()


def test_load_price_bars_reads_fund_csv_for_fund(tmp_path, monkeypatch):
    """``instrument_type='fund'`` 必须读 ``fund_*.csv``，不是 ``stock_*_qfq.csv``。"""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / f"fund_{FUND_CODE}.csv").write_text(
        FUND_FIXTURE.read_text(encoding="utf-8"), encoding="utf-8")
    monkeypatch.setattr(fl, "DATA_DIR", str(data_dir))

    bars = fl.load_price_bars(FUND_CODE, "fund")
    assert bars is not None and len(bars) == len(_fund_fixture_frame())
    assert list(bars.columns[:1]) == ["open"]
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
    """老记录没有 ``instrument_type``：先探 stock，再探 fund。"""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / f"fund_{FUND_CODE}.csv").write_text(
        FUND_FIXTURE.read_text(encoding="utf-8"), encoding="utf-8")
    monkeypatch.setattr(fl, "DATA_DIR", str(data_dir))

    assert fl.load_price_bars(FUND_CODE) is not None
    assert fl.load_price_bars(FUND_CODE, None) is not None


def test_settle_fund_record_success(ledger_tmp, monkeypatch):
    """基金记录结算成功，且 ``price_source`` 写真实来源 ``fund_nav``。

    用真实基金日线夹具离线回归：``as_of=2026-09-01`` → entry 2026-09-02
    开盘 4.65 → exit 2026-10-08 开盘 4.418，手算 ``4.418/4.65 − 1 = -0.049892``。
    """
    monkeypatch.setattr(fl, "DATA_DIR", str(FUND_FIXTURE.parent))
    frame = _fund_fixture_frame()
    entry_open = float(frame.loc["2026-09-02", "open"])
    exit_open = float(frame.loc["2026-10-08", "open"])

    fl.record_day([FUND_CODE], ledger_dir=ledger_tmp,
                  runner=_runner(_cli_payload(code=FUND_CODE, type_hint="fund",
                                              as_of="2026-09-01")))
    stats = fl.settle_all(ledger_dir=ledger_tmp)
    assert stats["settled"] == 1 and stats["failed"] == 0

    record = fl.load_day("2026-09-01", ledger_tmp)["records"][FUND_CODE]
    assert record["instrument_type"] == "fund"
    assert record["settle_status"] == "settled"
    settled = record["settled"]
    assert settled["entry_date"] == "2026-09-02"
    assert settled["settle_date"] == "2026-10-08"
    assert settled["entry_open"] == pytest.approx(entry_open)
    assert settled["exit_open"] == pytest.approx(exit_open)
    assert settled["ret_20d"] == pytest.approx(round(exit_open / entry_open - 1, 6))
    assert settled["ret_20d"] == pytest.approx(-0.049892)
    assert settled["hit"] is False  # buy 且收益为负
    # issue #95 要求 2：来源写真实值，不再统一写 local_qfq_cache
    assert settled["price_source"] == "fund_nav"


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
    fl.record_day(["002415"], ledger_dir=ledger_tmp,
                  runner=_runner(_cli_payload(as_of="2026-11-02")))
    stats = fl.settle_all(ledger_dir=ledger_tmp,
                          price_loader=lambda _code: frame)
    assert stats["pending"] == 1
    assert stats["failed"] == 0
    assert stats["settled"] == 0
    assert stats["reasons"] == {"no_bars_after_as_of": 1}
    record = fl.load_day("2026-11-02", ledger_tmp)["records"]["002415"]
    assert record["settled"] is None
    assert record["settle_reason"] == "no_bars_after_as_of"
    assert record["settle_status"] == "pending"


def test_settle_fund_record_without_instrument_type_still_labels_fund(ledger_tmp,
                                                                      monkeypatch):
    """老记录没有 ``instrument_type``：只要真读了 ``fund_*.csv`` 就标 ``fund_nav``。

    issue #95 要求 2 是「写真实来源」，不是「按字段猜来源」：探测到基金文件
    时若还写 ``local_qfq_cache`` 就是假话。
    """
    monkeypatch.setattr(fl, "DATA_DIR", str(FUND_FIXTURE.parent))
    _write_settled_day(ledger_tmp, "2026-09-01", {
        FUND_CODE: {"as_of": "2026-09-01", "code": FUND_CODE,
                    "action": "buy", "confidence": 0.5,
                    "settled": None, "settle_reason": None},
    })
    stats = fl.settle_all(ledger_dir=ledger_tmp)
    assert stats["settled"] == 1
    record = fl.load_day("2026-09-01", ledger_tmp)["records"][FUND_CODE]
    assert record["settled"]["price_source"] == "fund_nav"


def test_settle_stock_price_source_stays_local_qfq(ledger_tmp):
    """个股路径不回归：``price_source`` 仍是 ``local_qfq_cache``。"""
    days, opens = _bars_with_hand_math()
    frame = pd.DataFrame({"open": opens}, index=pd.to_datetime(
        [d.isoformat() for d in days]))
    fl.record_day(["002415"], ledger_dir=ledger_tmp,
                  runner=_runner(_cli_payload(as_of="2026-11-02")))
    fl.settle_all(ledger_dir=ledger_tmp, price_loader=lambda _code: frame)
    record = fl.load_day("2026-11-02", ledger_tmp)["records"]["002415"]
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
