# -*- coding: utf-8 -*-
"""P1-2（issue #36）：日级资金面上下文 —— 预计算缓存 + 只读展示。

覆盖：
  * ``tools/precompute_flow.py``：串行间隔 ≥1s、失败重试后继续、空响应不写缓存；
  * ``data_loader.flow``：缓存命中 / 缺失 / 过期、``--as-of`` 的 pub_lag_days=1、
    摘要数值**手算**断言、陈旧判定走本地日历；
  * CLI：``evidence.flow`` additive，``recommendation`` 逐键不变，全程不触网；
  * 错误路径与成功路径的 ``flow`` 子块键同构（G35）；
  * 生成物确实包含上游 §4.5 的 ``stock_fund_flow_120d``。

所有网络入口在本文件里都被打桩；``socket`` 在 CLI 用例里被显式禁用。
"""
from __future__ import annotations

import json
import socket
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

from stock_analysis.data_loader import flow as flow_mod  # noqa: E402


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


def _full_row(day: date, main_net: float) -> dict:
    return {"date": day.isoformat(), "main_net": main_net, "small_net": 0.0,
            "mid_net": 0.0, "large_net": 0.0, "super_net": 0.0}


def _write_cache(directory: Path, code: str, rows, fetched_at="2026-11-12T16:30:00"):
    directory.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": "1.0",
        "code": code,
        "source": "eastmoney_push2his",
        "upstream": "a-stock-data v3.10.4 §4.5 stock_fund_flow_120d",
        "fetched_at": fetched_at,
        "as_of": rows[-1]["date"],
        "rows": rows,
    }
    path = directory / f"{code}.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def _fix_expected(monkeypatch, day=date(2026, 11, 12), source="calendar_cache"):
    monkeypatch.setattr(flow_mod, "_expected_trade_date",
                        lambda **kwargs: (day, source))


def _summary_rows(n=20, end=date(2026, 11, 12)):
    """main_net = 1..n（按日期升序）；便于手算 5/20 日窗口。"""
    return [_full_row(d, float(i + 1)) for i, d in enumerate(_bdays(end, n))]


# ---------------------------------------------------------------------------
# 摘要：手算断言
# ---------------------------------------------------------------------------
def test_summary_hand_computed(monkeypatch, tmp_path):
    _fix_expected(monkeypatch)
    rows = _summary_rows(20)
    _write_cache(tmp_path, "600519", rows)

    block = flow_mod.build_flow_block("600519", flow_dir=str(tmp_path))

    # 最近 5 行 = 16,17,18,19,20 → 90；全部 20 行 = 210
    assert block["main_net_5d"] == pytest.approx(90.0)
    assert block["main_net_20d"] == pytest.approx(210.0)
    assert block["direction"] == "inflow"
    assert block["direction_basis"] == "main_net_5d_sign"
    assert block["available"] is True
    assert block["rows"] == 20
    assert block["as_of"] == "2026-11-12"
    assert block["source"] == "eastmoney_push2his"
    assert block["pub_lag_days"] == 1
    assert block["stale"] is False
    assert set(block) == set(flow_mod.FLOW_KEYS)


def test_summary_short_cache_does_not_fake_20d(monkeypatch, tmp_path):
    _fix_expected(monkeypatch)
    rows = _summary_rows(8)          # 只有 8 行
    rows[-1]["main_net"] = -10.0
    _write_cache(tmp_path, "600519", rows)

    block = flow_mod.build_flow_block("600519", flow_dir=str(tmp_path))
    assert block["main_net_20d"] is None, "不足 20 行不得用更短窗口冒充"
    assert block["main_net_5d"] is not None
    # 最近 5 行 = 4,5,6,7,-10 → 12（仍为正）
    assert block["main_net_5d"] == pytest.approx(12.0)


def test_summary_direction_outflow(monkeypatch, tmp_path):
    _fix_expected(monkeypatch)
    rows = _summary_rows(20)
    for row in rows[-5:]:
        row["main_net"] = -100.0
    _write_cache(tmp_path, "600519", rows)
    block = flow_mod.build_flow_block("600519", flow_dir=str(tmp_path))
    assert block["main_net_5d"] == pytest.approx(-500.0)
    assert block["direction"] == "outflow"


def test_summary_as_of_respects_pub_lag_one_day(monkeypatch, tmp_path):
    """§3.3：回放 d 日只使用 date < d 的资金流（当日盘后才发布）。"""
    _fix_expected(monkeypatch)
    rows = _summary_rows(20)         # 最后一根 2026-11-12
    _write_cache(tmp_path, "600519", rows)

    block = flow_mod.build_flow_block("600519", as_of="2026-11-12",
                                      flow_dir=str(tmp_path))
    assert block["as_of"] == "2026-11-11", "as_of=11-12 时不得使用 11-12 当日资金流"
    # 用到的是第 1..19 行的最近 5 行：15,16,17,18,19 → 85
    assert block["main_net_5d"] == pytest.approx(85.0)
    assert block["rows"] == 19


def test_summary_no_rows_before_as_of(monkeypatch, tmp_path):
    _fix_expected(monkeypatch)
    rows = _summary_rows(10, end=date(2026, 11, 12))
    _write_cache(tmp_path, "600519", rows)
    block = flow_mod.build_flow_block("600519", as_of="2026-10-01",
                                      flow_dir=str(tmp_path))
    assert block["available"] is False
    assert block["reason"] == "no_rows_as_of"


# ---------------------------------------------------------------------------
# 缓存缺失 / 过期
# ---------------------------------------------------------------------------
def test_missing_cache_returns_none_and_null_block(monkeypatch, tmp_path):
    _fix_expected(monkeypatch)
    assert flow_mod.summarize_flow("600519", flow_dir=str(tmp_path)) is None

    block = flow_mod.build_flow_block("600519", flow_dir=str(tmp_path))
    assert block["available"] is False
    assert block["reason"] == "cache_missing"
    assert set(block) == set(flow_mod.FLOW_KEYS)
    assert all(block[k] is None for k in
               ("source", "as_of", "fetched_at", "main_net_5d", "main_net_20d",
                "direction", "direction_basis", "stale"))


def test_invalid_cache_is_reported_not_crashed(tmp_path):
    (tmp_path / "600519.json").write_text("{not json", encoding="utf-8")
    assert flow_mod.read_cache("600519", flow_dir=str(tmp_path)) is None
    block = flow_mod.build_flow_block("600519", flow_dir=str(tmp_path))
    assert block["reason"] == "cache_invalid"


def test_stale_cache_warns_and_does_not_change_availability(monkeypatch, tmp_path):
    _fix_expected(monkeypatch, day=date(2026, 11, 12))
    rows = _summary_rows(20, end=date(2026, 11, 10))     # 缓存落后 2 个交易日
    _write_cache(tmp_path, "600519", rows)

    block, notes = flow_mod.flow_evidence("600519", "stock",
                                          flow_dir=str(tmp_path), now=None)
    assert block["available"] is True
    assert block["stale"] is True
    assert block["expected_date"] == "2026-11-12"
    assert any("资金面缓存过期" in note for note in notes)


def test_fresh_cache_has_no_warning(monkeypatch, tmp_path):
    _fix_expected(monkeypatch, day=date(2026, 11, 12))
    _write_cache(tmp_path, "600519", _summary_rows(20))
    block, notes = flow_mod.flow_evidence("600519", "stock",
                                          flow_dir=str(tmp_path))
    assert block["stale"] is False
    assert notes == []


def test_fund_is_explicitly_unsupported_without_warning(tmp_path):
    block, notes = flow_mod.flow_evidence("510300", "fund", flow_dir=str(tmp_path))
    assert block["available"] is False
    assert block["reason"] == "fund_not_supported"
    assert notes == [], "基金每次运行都告警会变成噪声"


# ---------------------------------------------------------------------------
# 陈旧判定：本地日历（不触网）
# ---------------------------------------------------------------------------
def test_expected_trade_date_uses_disk_calendar_only(monkeypatch):
    calendar = {"2026-11-09": True, "2026-11-10": False,
                "2026-11-11": True, "2026-11-12": True}
    monkeypatch.setattr(flow_mod, "_read_disk_calendar",
                        lambda y, m: calendar if (y, m) == (2026, 11) else None)

    # 收盘前（<15:00）从昨天找；11-11 是交易日
    assert flow_mod._expected_trade_date(now=datetime(2026, 11, 12, 10, 0)) == \
        (date(2026, 11, 11), "calendar_cache")
    # 收盘后当天算已收盘
    assert flow_mod._expected_trade_date(now=datetime(2026, 11, 12, 16, 0)) == \
        (date(2026, 11, 12), "calendar_cache")
    # 回放语义：直接以 as_of 日为起点
    assert flow_mod._expected_trade_date(as_of="2026-11-12") == \
        (date(2026, 11, 12), "calendar_cache")


def test_expected_trade_date_falls_back_to_weekday(monkeypatch):
    monkeypatch.setattr(flow_mod, "_read_disk_calendar", lambda y, m: None)
    # 2026-11-15 是周日 → 回退到 11-13（周五）
    expected, source = flow_mod._expected_trade_date(now=datetime(2026, 11, 15, 16, 0))
    assert source == "estimate"
    assert expected == date(2026, 11, 13)


def test_flow_module_never_opens_a_socket(monkeypatch, tmp_path):
    def boom(*args, **kwargs):
        raise AssertionError("资金流读取不得触网")

    monkeypatch.setattr(socket, "socket", boom)
    monkeypatch.setattr(socket, "create_connection", boom)
    assert flow_mod.summarize_flow("600519", flow_dir=str(tmp_path)) is None
    block, notes = flow_mod.flow_evidence("600519", "stock",
                                          flow_dir=str(tmp_path), now=None)
    assert block["available"] is False and notes


# ---------------------------------------------------------------------------
# precompute：串行间隔 / 失败继续 / 空响应不写缓存
# ---------------------------------------------------------------------------
class _Clock:
    def __init__(self, start=datetime(2026, 11, 13, 16, 0, 0)):
        self.value = start

    def now(self):
        return self.value

    def sleep(self, seconds):
        self.value += timedelta(seconds=seconds)


def test_precompute_serial_interval_and_failure_continues(tmp_path):
    import precompute_flow as tool

    clock = _Clock()
    calls = []

    def fake_fetch(code):
        calls.append((code, clock.now()))
        if code == "600519":
            raise RuntimeError("模拟东财空响应")
        return [_full_row(d, 1.0) for d in _bdays(date(2026, 11, 12), 20)]

    stats = tool.precompute(["600519", "002415"], out_dir=str(tmp_path),
                            interval=1.0, retries=1, retry_wait=2.0,
                            fetch_fn=fake_fetch, sleep_fn=clock.sleep,
                            now_fn=clock.now)

    assert [c for c, _ in calls] == ["600519", "600519", "002415"]
    gaps = [(calls[i + 1][1] - calls[i][1]).total_seconds()
            for i in range(len(calls) - 1)]
    assert all(g >= 1.0 for g in gaps), f"相邻请求间隔必须 ≥1s：{gaps}"
    assert stats["requests"] == 3
    assert [item["code"] for item in stats["ok"]] == ["002415"]
    assert stats["failed"][0]["code"] == "600519"
    assert stats["failed"][0]["attempts"] == 2
    # 失败票不落缓存；成功票落缓存且可被 flow 读取
    assert not (tmp_path / "600519.json").exists()
    assert (tmp_path / "002415.json").exists()
    assert flow_mod.summarize_flow("002415", flow_dir=str(tmp_path)) is not None


def test_precompute_rejects_below_one_second(tmp_path):
    import precompute_flow as tool

    with pytest.raises(ValueError, match="不得小于 1.0"):
        tool.precompute(["600519"], out_dir=str(tmp_path), interval=0.5,
                        fetch_fn=lambda code: [])


def test_precompute_rejects_empty_response_and_short_rows(tmp_path):
    import precompute_flow as tool

    with pytest.raises(ValueError, match="空响应"):
        tool.validate_rows([])
    with pytest.raises(ValueError, match="< 20"):
        tool.validate_rows([_full_row(date(2026, 11, 12), 1.0)] * 5)


def test_precompute_cli_merges_positional_and_codes_option(monkeypatch):
    """回归：位置参数与 ``--codes`` 曾共用 dest，``--codes`` 会被覆盖成 list。"""
    import precompute_flow as tool

    captured = {}
    monkeypatch.setattr(tool, "precompute", lambda codes, **kwargs: captured.update(
        codes=list(codes)) or {"requests": 0, "ok": [], "failed": [],
                               "wall_seconds": 0.0, "interval_seconds": 1.0})
    assert tool.main(["--codes", "600519,002415", "300750"]) == 0
    assert captured["codes"] == ["300750", "600519", "002415"]


def test_precompute_cache_payload_shape(tmp_path):
    import precompute_flow as tool

    rows = [_full_row(d, float(i)) for i, d in enumerate(_bdays(date(2026, 11, 12), 20))]
    path = tool.write_cache("600519", rows, out_dir=str(tmp_path))
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    assert payload["code"] == "600519"
    assert payload["as_of"] == rows[-1]["date"]
    assert payload["source"] == "eastmoney_push2his"
    assert "§4.5" in payload["upstream"]
    assert len(payload["rows"]) == 20


# ---------------------------------------------------------------------------
# CLI：additive + 不触网 + A/B 一致
# ---------------------------------------------------------------------------
def _run_cli(monkeypatch, flow_dir, as_of=None):
    import cli
    import stock_analysis.data_loader.meta as meta
    from stock_analysis.backtest.engine import BacktestResult
    from stock_analysis.data_loader.status import TradingStatus

    df = pd.DataFrame(
        {"open": [1.0] * 300, "high": [1.0] * 300, "low": [1.0] * 300,
         "close": [100.0 + i * 0.1 for i in range(300)], "volume": [1e6] * 300},
        index=pd.bdate_range("2020-01-01", periods=300),
    )
    monkeypatch.setattr(meta, "resolve_and_fetch", lambda *a, **k: (df, "stock", None))
    monkeypatch.setattr(cli, "last_fetch_info",
                        lambda: {"source": "cache", "from_cache": True,
                                 "degraded": None, "lag_days": None})
    monkeypatch.setattr(cli, "get_stock_name", lambda c: c)
    monkeypatch.setattr(cli, "resolve_trading_status",
                        lambda *a, **k: TradingStatus(is_st=False, is_suspended=False,
                                                      status_source="offline",
                                                      warnings=[]))
    monkeypatch.setattr(flow_mod, "FLOW_DIR", str(flow_dir))
    _fix_expected(monkeypatch)

    names = list(cli.build_strategy_signals().keys())[:3]
    comp = pd.DataFrame(
        [{"评分": 1.0 - i * 0.1, "评分IS": 1.0 - i * 0.1, "评分OOS": 0.9 - i * 0.1,
          "年化收益": "10.00%", "最大回撤": "5.00%", "胜率": "60.00%", "交易次数": 20}
         for i, _ in enumerate(names)],
        index=names,
    )
    res = BacktestResult(trade_count=20, win_rate=0.6, annual_return=0.1,
                         max_drawdown=0.05, unaffordable_lot_events=0)
    res.equity_curve = [100000.0] * 50
    monkeypatch.setattr(cli, "run_strategy_tournament",
                        lambda *a, **k: (names[0], res, comp))
    return cli.advise_internal("600519", fast=True, as_of=as_of)


def test_cli_flow_is_additive_and_offline(monkeypatch, tmp_path):
    _write_cache(tmp_path / "with", "600519", _summary_rows(20))

    def boom(*args, **kwargs):
        raise AssertionError("CLI 主路径不得触网")

    monkeypatch.setattr(socket, "socket", boom)
    monkeypatch.setattr(socket, "create_connection", boom)

    out_flow = _run_cli(monkeypatch, tmp_path / "with")
    out_none = _run_cli(monkeypatch, tmp_path / "without")

    assert out_flow["schema_version"] == "1.0"
    assert set(out_flow["evidence"]) >= {"flow", "consensus", "metrics"}
    assert out_flow["evidence"]["flow"]["available"] is True
    assert out_flow["evidence"]["flow"]["main_net_5d"] == pytest.approx(90.0)

    # A/B：recommendation 逐键一致；evidence 除 flow 外一致；action 不变
    assert out_flow["recommendation"] == out_none["recommendation"]
    assert {k: v for k, v in out_flow["evidence"].items() if k != "flow"} == \
        {k: v for k, v in out_none["evidence"].items() if k != "flow"}
    assert out_none["evidence"]["flow"]["available"] is False
    assert any("资金面上下文不可用" in w for w in out_none["diagnostics"]["warnings"])


def test_cli_flow_respects_as_of(monkeypatch, tmp_path):
    _write_cache(tmp_path, "600519", _summary_rows(20))
    out = _run_cli(monkeypatch, tmp_path, as_of="2026-11-12")
    block = out["evidence"]["flow"]
    assert block["as_of"] == "2026-11-11"
    assert block["main_net_5d"] == pytest.approx(85.0)


def test_error_response_flow_isomorphic():
    import cli

    doc = cli.create_error_response("data_source_error", "失败", "600519", "stock")
    block = doc["evidence"]["flow"]
    assert set(block) == set(flow_mod.FLOW_KEYS)
    assert block["available"] is False
    assert block["reason"] == "data_source_error"


# ---------------------------------------------------------------------------
# 生成物：上游 §4.5 已被提取
# ---------------------------------------------------------------------------
def test_generated_artifact_contains_flow_function():
    from stock_analysis.data_sources import _astock_generated as generated

    assert hasattr(generated, "stock_fund_flow_120d")
    assert hasattr(generated, "em_market_code")
    manifest = json.loads((SRC / "stock_analysis" / "data_sources" /
                           "_astock_manifest.json").read_text(encoding="utf-8"))
    names = {item["symbol"] for item in manifest["selections"]}
    assert {"stock_fund_flow_120d", "em_market_code"} <= names
