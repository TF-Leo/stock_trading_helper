# -*- coding: utf-8 -*-
"""P1-1（issue #46）：估值上下文 —— 预计算缓存 + 只读展示。

覆盖：
  * ``data_loader.valuation``：缓存命中 / 缺失 / 无效 / 过期、3 年分位**手算**
    断言、``as_of`` 无前视、样本不足 / 亏损 PE / 停牌日排除、基金显式不支持；
  * ``tools/precompute_valuation.py``：登录一次、逐票串行、失败重试后继续、
    失败不覆盖旧缓存、stdout 不被 baostock 日志污染、缺失依赖清晰报错；
  * CLI：``evidence.valuation`` additive，``recommendation`` 逐键不变，全程不触网；
  * 错误路径与成功路径的 ``valuation`` 子块键同构（G35）。

所有网络入口在本文件里都被打桩；``socket`` 在 CLI / 加载器用例里被显式禁用。
"""
from __future__ import annotations

import json
import socket
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

SRC = Path(__file__).resolve().parents[1]
ROOT = SRC.parent
TOOLS = ROOT / "tools"
for path in (SRC, TOOLS):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from stock_analysis.data_loader import flow as flow_mod  # noqa: E402
from stock_analysis.data_loader import valuation as val_mod  # noqa: E402


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


def _row(day: date, pe=10.0, pb=2.0, ps=3.0, pcf=5.0, turnover=1.25,
         trade_status=1, is_st=0, close=100.0) -> dict:
    return {"date": day.isoformat(), "close": close, "pe_ttm": pe, "pb": pb,
            "ps": ps, "pcf": pcf, "turnover": turnover,
            "trade_status": trade_status, "is_st": is_st}


def _write_cache(directory: Path, code: str, rows, fetched_at="2026-11-12T16:30:00",
                 source="baostock_valuation"):
    directory.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": "1.0",
        "code": code,
        "source": source,
        "upstream": val_mod.UPSTREAM,
        "fetched_at": fetched_at,
        "as_of": rows[-1]["date"],
        "pub_lag_days": 0,
        "rows": rows,
    }
    path = directory / f"{code}.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def _fix_expected(monkeypatch, day=date(2026, 11, 12), source="calendar_cache"):
    monkeypatch.setattr(val_mod, "_expected_trade_date",
                        lambda **kwargs: (day, source))


# ---------------------------------------------------------------------------
# 分位：手算断言
# ---------------------------------------------------------------------------
def test_percentile_rank_definition():
    # 100 × count(v < current) / (n − 1)；最大值恒为 100，最小值恒为 0
    assert val_mod.percentile_rank(3.0, [1.0, 2.0, 3.0, 4.0]) == pytest.approx(66.67)
    assert val_mod.percentile_rank(1.0, [1.0, 2.0, 3.0, 4.0]) == 0.0
    assert val_mod.percentile_rank(4.0, [1.0, 2.0, 3.0, 4.0]) == 100.0
    # 并列值不计入「严格小于」
    assert val_mod.percentile_rank(2.0, [1.0, 1.0, 2.0]) == 100.0
    assert val_mod.percentile_rank(5.0, [5.0]) is None


def test_summary_percentile_hand_computed(monkeypatch, tmp_path):
    _fix_expected(monkeypatch)
    days = _bdays(date(2026, 11, 12), 300)
    rows = []
    for i, day in enumerate(days):
        # pe：1..299，最后一行 150（重复值）→ 严格小于 150 的样本 = 149
        pe = float(i + 1) if i < 299 else 150.0
        # pb：全 10.0 → 严格小于 = 0
        pb = 10.0
        # ps：1..300 → 最后一行最大 → 100
        ps = float(i + 1)
        rows.append(_row(day, pe=pe, pb=pb, ps=ps,
                         turnover=1.25, is_st=1 if i == 299 else 0))
    _write_cache(tmp_path, "600519", rows)

    block = val_mod.build_valuation_block("600519",
                                          valuation_dir=str(tmp_path))

    assert block["available"] is True
    assert block["pe_ttm"] == pytest.approx(150.0)
    assert block["pb"] == pytest.approx(10.0)
    assert block["ps"] == pytest.approx(300.0)
    # n=300：pe 149/299=49.83；pb 0；ps 299/299=100
    assert block["pe_percentile_3y"] == pytest.approx(49.83)
    assert block["pb_percentile_3y"] == pytest.approx(0.0)
    assert block["ps_percentile_3y"] == pytest.approx(100.0)
    assert block["pe_percentile_reason"] is None
    assert block["percentile_samples"] == {"pe": 300, "pb": 300, "ps": 300}
    assert block["percentile_window_days"] == 756
    assert block["turnover_latest"] == pytest.approx(1.25)
    assert block["is_st_hist"] is True
    assert block["as_of"] == days[-1].isoformat()
    assert block["rows"] == 300
    assert block["source"] == "baostock_valuation"
    assert block["pub_lag_days"] == 0
    assert block["stale"] is False
    assert set(block) == set(val_mod.VALUATION_KEYS)


def test_summary_as_of_has_no_lookahead(monkeypatch, tmp_path):
    """``--as-of`` 只使用 date <= as_of 的行；未来行不得影响分位/最新值。"""
    days = _bdays(date(2026, 11, 12), 600)
    rows = []
    for i, day in enumerate(days):
        if i < 299:
            pe = float(i + 1)             # 1..299
        elif i == 299:
            pe = 150.0                    # as_of 当日
        else:
            pe = 9999.0                   # 未来行：若被误用会明显拉低分位
        rows.append(_row(day, pe=pe, pb=float(i + 1), ps=float(i + 1)))
    _write_cache(tmp_path, "600519", rows)
    _fix_expected(monkeypatch)

    as_of = days[299].isoformat()
    block = val_mod.build_valuation_block("600519", as_of=as_of,
                                          valuation_dir=str(tmp_path))

    assert block["as_of"] == as_of
    assert block["rows"] == 300
    assert block["pe_ttm"] == pytest.approx(150.0)
    # 300 个样本：严格小于 150 = 149 → 149/299 = 49.83
    # 若把 300 行未来数据算进来，分母变 599 → 24.87
    assert block["pe_percentile_3y"] == pytest.approx(49.83)
    assert block["pe_percentile_reason"] is None


def test_summary_insufficient_samples_is_null_with_reason(monkeypatch, tmp_path):
    _fix_expected(monkeypatch)
    days = _bdays(date(2026, 11, 12), 100)
    rows = [_row(day, pe=float(i + 1), pb=float(i + 1), ps=float(i + 1))
            for i, day in enumerate(days)]
    _write_cache(tmp_path, "600519", rows)

    block = val_mod.build_valuation_block("600519", valuation_dir=str(tmp_path))
    assert block["available"] is True
    assert block["pe_ttm"] == pytest.approx(100.0)
    assert block["pe_percentile_3y"] is None
    assert block["pe_percentile_reason"] == "insufficient_samples"
    assert block["percentile_samples"] == {"pe": 100, "pb": 100, "ps": 100}


def test_summary_missing_and_non_positive_metrics(monkeypatch, tmp_path):
    _fix_expected(monkeypatch)
    days = _bdays(date(2026, 11, 12), 300)
    rows = [_row(day, pe=float(i + 1), pb=float(i + 1), ps=float(i + 1))
            for i, day in enumerate(days)]
    rows[-1]["pe_ttm"] = None             # 最新 PE 缺失
    rows[-1]["pb"] = -1.0                 # 资不抵债 → 不可排序
    _write_cache(tmp_path, "600519", rows)

    block = val_mod.build_valuation_block("600519", valuation_dir=str(tmp_path))
    assert block["pe_ttm"] is None
    assert block["pe_percentile_3y"] is None
    assert block["pe_percentile_reason"] == "missing"
    assert block["pb"] == pytest.approx(-1.0)
    assert block["pb_percentile_3y"] is None
    assert block["pb_percentile_reason"] == "non_positive"
    assert block["ps_percentile_3y"] is not None


def test_summary_excludes_suspended_days_from_percentile(monkeypatch, tmp_path):
    _fix_expected(monkeypatch)
    days = _bdays(date(2026, 11, 12), 300)
    rows = []
    for i, day in enumerate(days):
        suspended = i < 50
        # 停牌日塞入极端值：若未排除，样本数会从 250 变成 300
        pe = 10000.0 if suspended else (150.0 if i == 299 else float(i + 1))
        rows.append(_row(day, pe=pe, pb=float(i + 1), ps=float(i + 1),
                         trade_status=0 if suspended else 1))
    _write_cache(tmp_path, "600519", rows)

    block = val_mod.build_valuation_block("600519", valuation_dir=str(tmp_path))
    # 有效样本 = 250 行（51..299 + 最后一行 150）；
    # 严格小于 150 的 = 51..149 共 99 个 → 100×99/(250−1) = 39.76
    assert block["percentile_samples"]["pe"] == 250
    assert block["pe_percentile_3y"] == pytest.approx(39.76)
    assert block["rows"] == 300


# ---------------------------------------------------------------------------
# 缓存缺失 / 无效 / 过期
# ---------------------------------------------------------------------------
def test_missing_cache_returns_null_block_and_warning(tmp_path):
    block, notes = val_mod.valuation_evidence(
        "600519", "stock", valuation_dir=str(tmp_path))
    assert block["available"] is False
    assert block["reason"] == "cache_missing"
    assert set(block) == set(val_mod.VALUATION_KEYS)
    assert any("估值上下文不可用" in note for note in notes)
    assert block["percentile_samples"] == {"pe": None, "pb": None, "ps": None}


def test_invalid_cache_is_reported_not_crashed(tmp_path):
    (tmp_path / "600519.json").write_text("{not json", encoding="utf-8")
    block = val_mod.build_valuation_block("600519", valuation_dir=str(tmp_path))
    assert block["available"] is False
    assert block["reason"] == "cache_invalid"


def test_wrong_source_or_bad_rows_is_invalid(tmp_path):
    rows = [_row(day) for day in _bdays(date(2026, 11, 12), 3)]
    _write_cache(tmp_path, "600519", rows, source="eastmoney")
    assert val_mod.read_cache("600519", valuation_dir=str(tmp_path)) is None

    rows[0]["trade_status"] = ""          # 未知状态不能伪装成正常
    _write_cache(tmp_path, "600519", rows)
    assert val_mod.read_cache("600519", valuation_dir=str(tmp_path)) is None


def test_stale_cache_warns_without_hiding_data(monkeypatch, tmp_path):
    days = _bdays(date(2026, 11, 10), 300)
    rows = [_row(day, pe=float(i + 1), pb=float(i + 1), ps=float(i + 1))
            for i, day in enumerate(days)]
    _write_cache(tmp_path, "600519", rows)
    _fix_expected(monkeypatch, day=date(2026, 11, 12))

    block, notes = val_mod.valuation_evidence(
        "600519", "stock", valuation_dir=str(tmp_path))
    assert block["available"] is True
    assert block["stale"] is True
    assert block["as_of"] == "2026-11-10"
    assert any("估值缓存过期" in note for note in notes)


def test_fresh_cache_has_no_warning(monkeypatch, tmp_path):
    days = _bdays(date(2026, 11, 12), 300)
    rows = [_row(day, pe=float(i + 1), pb=float(i + 1), ps=float(i + 1))
            for i, day in enumerate(days)]
    _write_cache(tmp_path, "600519", rows)
    _fix_expected(monkeypatch, day=date(2026, 11, 12))

    _, notes = val_mod.valuation_evidence(
        "600519", "stock", valuation_dir=str(tmp_path))
    assert notes == []


def test_fund_is_explicitly_unsupported_without_warning(tmp_path):
    block, notes = val_mod.valuation_evidence(
        "510300", "fund", valuation_dir=str(tmp_path))
    assert block["available"] is False
    assert block["reason"] == "fund_not_supported"
    assert notes == []
    assert set(block) == set(val_mod.VALUATION_KEYS)


def test_valuation_module_never_opens_a_socket(monkeypatch, tmp_path):
    def boom(*args, **kwargs):
        raise AssertionError("估值上下文读取不得触网")

    monkeypatch.setattr(socket, "socket", boom)
    monkeypatch.setattr(socket, "create_connection", boom)
    assert val_mod.summarize_valuation("600519", valuation_dir=str(tmp_path)) is None
    block, notes = val_mod.valuation_evidence(
        "600519", "stock", valuation_dir=str(tmp_path))
    assert block["available"] is False and notes


# ---------------------------------------------------------------------------
# precompute：登录一次 / 串行 / 失败继续 / stdout 纯净
# ---------------------------------------------------------------------------
class _Clock:
    def __init__(self, start=datetime(2026, 11, 12, 16, 0, 0)):
        self.value = start

    def now(self):
        return self.value

    def sleep(self, seconds):
        self.value += timedelta(seconds=seconds)


def _raw_row(day: date, pe="10.0") -> dict:
    """上游 baostock 的原始字符串形状。"""
    return {"date": day.isoformat(), "code": "sh.600519", "close": "100.0",
            "peTTM": pe, "pbMRQ": "2.0", "psTTM": "3.0", "pcfNcfTTM": "5.0",
            "turn": "1.25", "tradestatus": "1", "isST": "0"}


def _ok_login():
    return SimpleNamespace(error_code="0", error_msg="")


def test_precompute_serial_interval_and_failure_continues(tmp_path):
    import precompute_valuation as tool

    clock = _Clock()
    calls = []

    def fake_fetch(code, start, end):
        calls.append((code, clock.now()))
        if code == "600519":
            raise RuntimeError("模拟查询失败")
        return [_raw_row(day) for day in _bdays(date(2026, 11, 12), 20)]

    stats = tool.precompute(
        ["600519", "002415"], out_dir=str(tmp_path), interval=0.5,
        retries=1, retry_wait=2.0, fetch_fn=fake_fetch,
        login_fn=_ok_login, logout_fn=lambda: None,
        sleep_fn=clock.sleep, now_fn=clock.now)

    assert [code for code, _ in calls] == ["600519", "600519", "002415"]
    gaps = [(calls[i + 1][1] - calls[i][1]).total_seconds()
            for i in range(len(calls) - 1)]
    assert all(gap >= 0.5 for gap in gaps), f"相邻查询间隔必须 ≥ interval：{gaps}"
    assert stats["login"] == "ok"
    assert stats["requests"] == 3
    assert [item["code"] for item in stats["ok"]] == ["002415"]
    assert stats["failed"][0]["code"] == "600519"
    assert stats["failed"][0]["attempts"] == {"baostock": 2}
    # 失败票不落缓存；成功票可被只读加载器读取
    assert not (tmp_path / "600519.json").exists()
    assert (tmp_path / "002415.json").exists()
    block = val_mod.build_valuation_block("002415", valuation_dir=str(tmp_path))
    assert block["available"] is True
    assert block["rows"] == 20
    assert block["pe_ttm"] == pytest.approx(10.0)


def test_precompute_failure_does_not_overwrite_old_cache(tmp_path):
    import precompute_valuation as tool

    old_rows = tool.validate_rows([_raw_row(day) for day in _bdays(date(2026, 11, 11), 5)])
    old = Path(tool.write_cache("600519", old_rows, out_dir=str(tmp_path),
                                fetched_at="2026-11-11T16:30:00"))

    def fail(code, start, end):
        raise RuntimeError("网络不可达")

    stats = tool.precompute(
        ["600519"], out_dir=str(tmp_path), interval=0.0, retries=0,
        fetch_fn=fail, login_fn=_ok_login, logout_fn=lambda: None,
        sleep_fn=lambda seconds: None, now_fn=_Clock().now)

    assert stats["requests"] == 1
    assert stats["failed"][0]["attempts"] == {"baostock": 1}
    payload = json.loads(old.read_text(encoding="utf-8"))
    assert payload["fetched_at"] == "2026-11-11T16:30:00", "失败不得覆盖旧缓存"


def test_precompute_login_failure_reports_all_codes(tmp_path):
    import precompute_valuation as tool

    def fail_login():
        return SimpleNamespace(error_code="10001001", error_msg="登录失败")

    stats = tool.precompute(
        ["600519", "002415"], out_dir=str(tmp_path), interval=0.0,
        fetch_fn=lambda *a: pytest.fail("登录失败时不得查询"),
        login_fn=fail_login, logout_fn=lambda: None,
        sleep_fn=lambda seconds: None, now_fn=_Clock().now)

    assert stats["login"] == "failed"
    assert "10001001" in stats["login_error"]
    assert stats["requests"] == 0
    assert {item["code"] for item in stats["failed"]} == {"600519", "002415"}


def test_precompute_stdout_not_polluted_by_baostock_logs(tmp_path, capsys):
    import precompute_valuation as tool

    def noisy_login():
        print("login success!")
        print("login error to stderr", file=sys.stderr)
        return _ok_login()

    def noisy_fetch(code, start, end):
        print(f"query log for {code}")
        return [_raw_row(day) for day in _bdays(date(2026, 11, 12), 3)]

    stats = tool.precompute(
        ["600519"], out_dir=str(tmp_path), interval=0.0,
        fetch_fn=noisy_fetch, login_fn=noisy_login,
        logout_fn=lambda: print("logout success!"),
        sleep_fn=lambda seconds: None, now_fn=_Clock().now)

    captured = capsys.readouterr()
    assert stats["ok"][0]["code"] == "600519"
    assert "login success!" not in captured.out
    assert "log to stderr" not in captured.err
    assert "query log" not in captured.out
    assert "logout success!" not in captured.out


def test_precompute_missing_baostock_has_actionable_error():
    import precompute_valuation as tool

    def no_module(name):
        raise ImportError("No module named 'baostock'")

    with pytest.raises(RuntimeError, match="requirements-research.txt"):
        tool.import_baostock(import_fn=no_module)


def test_precompute_cache_payload_shape(tmp_path):
    import precompute_valuation as tool

    raw = [_raw_row(day, pe=str(i + 1))
           for i, day in enumerate(_bdays(date(2026, 11, 12), 20))]
    rows = tool.validate_rows(raw)
    path = tool.write_cache("600519", rows, out_dir=str(tmp_path),
                            attempts={"baostock": 1})
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    assert payload["code"] == "600519"
    assert payload["source"] == "baostock_valuation"
    assert payload["as_of"] == rows[-1]["date"]
    assert payload["pub_lag_days"] == 0
    assert payload["attempts"] == {"baostock": 1}
    assert "§6.5" in payload["upstream"]
    assert set(payload["rows"][0]) == {
        "date", "close", "pe_ttm", "pb", "ps", "pcf", "turnover",
        "trade_status", "is_st"}


def test_validate_rows_rejects_empty_and_bad_flags():
    import precompute_valuation as tool

    with pytest.raises(ValueError, match="空响应"):
        tool.validate_rows([])
    missing = _raw_row(date(2026, 11, 12))
    del missing["tradestatus"]
    with pytest.raises(ValueError, match="缺字段 tradestatus"):
        tool.validate_rows([missing])
    bad = _raw_row(date(2026, 11, 12))
    bad["tradestatus"] = ""               # 未知状态不能默认成正常
    with pytest.raises(ValueError, match="0/1"):
        tool.validate_rows([bad])
    bad_date = _raw_row(date(2026, 11, 12))
    bad_date["date"] = "not-a-date"
    with pytest.raises(ValueError, match="日期格式错误"):
        tool.validate_rows([bad_date])


def test_bs_code_mapping_and_bj_rejection():
    import precompute_valuation as tool

    assert tool.bs_code("600519") == "sh.600519"
    assert tool.bs_code("688981") == "sh.688981"
    assert tool.bs_code("002415") == "sz.002415"
    assert tool.bs_code("300750") == "sz.300750"
    with pytest.raises(ValueError, match="不支持该代码"):
        tool.bs_code("920001")


# ---------------------------------------------------------------------------
# CLI：additive + 不触网 + A/B 一致
# ---------------------------------------------------------------------------
def _run_cli(monkeypatch, valuation_dir, as_of=None):
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
    monkeypatch.setattr(val_mod, "VALUATION_DIR", str(valuation_dir))
    monkeypatch.setattr(flow_mod, "FLOW_DIR", str(Path(valuation_dir) / "no-flow"))
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


def _percentile_rows(n=300, end=date(2026, 11, 12)):
    rows = []
    for i, day in enumerate(_bdays(end, n)):
        pe = float(i + 1) if i < n - 1 else 150.0
        rows.append(_row(day, pe=pe, pb=10.0, ps=float(i + 1)))
    return rows


def test_cli_valuation_is_additive_and_offline(monkeypatch, tmp_path):
    _write_cache(tmp_path / "with", "600519", _percentile_rows())

    def boom(*args, **kwargs):
        raise AssertionError("CLI 主路径不得触网")

    monkeypatch.setattr(socket, "socket", boom)
    monkeypatch.setattr(socket, "create_connection", boom)

    out_val = _run_cli(monkeypatch, tmp_path / "with")
    out_none = _run_cli(monkeypatch, tmp_path / "without")

    assert out_val["schema_version"] == "1.0"
    assert set(out_val["evidence"]) >= {"flow", "valuation", "consensus"}
    block = out_val["evidence"]["valuation"]
    assert block["available"] is True
    assert block["pe_percentile_3y"] == pytest.approx(49.83)
    assert block["pe_ttm"] == pytest.approx(150.0)

    # A/B：recommendation 逐键一致；evidence 除 flow/valuation 外一致；action 不变
    assert out_val["recommendation"] == out_none["recommendation"]
    assert {k: v for k, v in out_val["evidence"].items()
            if k not in ("flow", "valuation")} == \
        {k: v for k, v in out_none["evidence"].items()
         if k not in ("flow", "valuation")}
    assert out_none["evidence"]["valuation"]["available"] is False
    assert any("估值上下文不可用" in w
               for w in out_none["diagnostics"]["warnings"])


def test_cli_valuation_respects_as_of(monkeypatch, tmp_path):
    days = _bdays(date(2026, 11, 12), 600)
    rows = []
    for i, day in enumerate(days):
        if i < 299:
            pe = float(i + 1)             # 1..299
        elif i == 299:
            pe = 150.0                    # as_of 当日
        else:
            pe = 9999.0                   # 未来行：若发生前视会改变分位
        rows.append(_row(day, pe=pe, pb=10.0, ps=float(i + 1)))
    _write_cache(tmp_path, "600519", rows)

    as_of = days[299].isoformat()
    out = _run_cli(monkeypatch, tmp_path, as_of=as_of)
    block = out["evidence"]["valuation"]
    assert block["as_of"] == as_of
    assert block["rows"] == 300
    assert block["pe_ttm"] == pytest.approx(150.0)
    assert block["pe_percentile_3y"] == pytest.approx(49.83)


def test_error_response_valuation_isomorphic():
    import cli

    doc = cli.create_error_response("data_source_error", "失败", "600519", "stock")
    block = doc["evidence"]["valuation"]
    assert set(block) == set(val_mod.VALUATION_KEYS)
    assert block["available"] is False
    assert block["reason"] == "data_source_error"
    assert block["pub_lag_days"] == 0
