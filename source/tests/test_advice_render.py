# -*- coding: utf-8 -*-
"""建议渲染层 v1 的离线测试（issue #1 / R11 T1–T5）。

覆盖：
* 6 个 fixture 的完整输出快照（正常买点 / 停牌 / 一字板 / ST+warning /
  quote_stale / 可选字段全缺）；
* T1 仓位/持有期/复审日的存在与 None 消失；
* T2 可执行性闸门、盘前不误判、warnings 先于建议、action/confidence 不改写；
* T3 regime 冲突（逆风/顺风/一致/指数缺失）；
* T4 估值分位、四舍五入、无数据不编结论、小样本标注；
* T5 资金流方向与两种口径标签、混源只给方向、stale 带日期；
* ``cli.py --json`` 结构 golden（渲染层不得改动既有 JSON 结构）；
* 渲染零联网、纯函数（不修改入参）；
* ``tools/render_advice.py`` 的文件/stdin 包装与错误退出码。
"""
from __future__ import annotations

import copy
import json
import socket
import subprocess
import sys
from pathlib import Path

import pandas as pd
import pytest

SRC = Path(__file__).resolve().parents[1]
ROOT = SRC.parent
FIXTURES = Path(__file__).parent / "fixtures" / "advice_render"
GOLDEN = FIXTURES / "cli_json_structure.golden.json"

if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from stock_analysis.advice_render import render_advice  # noqa: E402

FIXTURE_NAMES = (
    "normal_buy",
    "suspended",
    "one_word_up",
    "st_warning",
    "quote_stale",
    "minimal_missing",
)


def _payload(name: str) -> dict:
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


def _render(name: str) -> str:
    return render_advice(_payload(name))


def _advice_lines(text: str):
    """去掉数据告警行与空行后的建议行（第一条即闸门/价格行）。"""
    return [line for line in text.splitlines()
            if line and not line.startswith("⚠️ 数据:")]


# ---------------------------------------------------------------------------
# 快照 + 基本契约
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("name", FIXTURE_NAMES)
def test_render_snapshot(name):
    payload = _payload(name)
    original = copy.deepcopy(payload)
    output = render_advice(payload)
    expected = (FIXTURES / f"{name}.expected.txt").read_text(
        encoding="utf-8").rstrip("\n")
    assert output == expected
    assert "None" not in output, "可选字段缺失时不得把 None 泄漏到文案"
    assert "null" not in output
    assert payload == original, "render_advice 必须是纯函数（不得修改入参）"


def test_fixtures_are_six_distinct_cases():
    assert len(FIXTURE_NAMES) == 6
    for name in FIXTURE_NAMES:
        assert (FIXTURES / f"{name}.json").is_file()
        assert (FIXTURES / f"{name}.expected.txt").is_file()


# ---------------------------------------------------------------------------
# T1：仓位 / 持有期 / 复审日
# ---------------------------------------------------------------------------
def test_t1_position_horizon_review_present():
    output = _render("normal_buy")
    assert "建议仓位 15%" in output
    assert "持有 20 日，2026-12-11 复审" in output


def test_t1_none_segments_disappear():
    output = _render("minimal_missing")
    assert "建议仓位" not in output
    assert "持有" not in output
    assert "复审" not in output
    assert "None" not in output


def test_t1_partial_optional_fields():
    """仓位缺失但持有期存在：只渲染持有期；复审日缺失则不写「复审」。"""
    payload = _payload("normal_buy")
    payload["recommendation"]["position_fraction"] = None
    payload["recommendation"]["review_date"] = None
    output = render_advice(payload)
    assert "建议仓位" not in output
    assert "持有 20 日" in output
    assert "复审" not in output


def test_t1_zero_position_is_rendered():
    """0.0 是有效值，不能被当成 None 吃掉。"""
    assert "建议仓位 0%" in _render("suspended")


# ---------------------------------------------------------------------------
# T2：可执行性 + 新鲜度 + warnings 顺序
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("name,reason", [
    ("suspended", "停牌"),
    ("one_word_up", "一字涨停"),
    ("st_warning", "ST 风险警示"),
])
def test_t2_gate_is_first_advice_line(name, reason):
    lines = _advice_lines(_render(name))
    assert lines[0].startswith("【今日不建议执行】"), lines[0]
    assert reason in lines[0]


@pytest.mark.parametrize("name,action_line", [
    ("suspended", "动作 avoid｜置信 0%"),
    ("one_word_up", "动作 avoid｜置信 0%"),
    ("st_warning", "动作 hold｜置信 66%"),
])
def test_t2_action_and_confidence_are_not_rewritten(name, action_line):
    output = _render(name)
    assert action_line in output
    # 闸门只描述可执行性，不替换 recommendation.action。
    assert f"动作 {_payload(name)['recommendation']['action']}｜" in output


def test_t2_warnings_precede_advice():
    output = _render("st_warning")
    warnings = [line for line in output.splitlines()
                if line.startswith("⚠️ 数据:")]
    assert len(warnings) == 2
    assert output.index("⚠️ 数据:") < output.index("动作 hold")


def test_t2_quote_stale_replaces_current_price():
    output = _render("quote_stale")
    assert "收盘为 2026-11-12" in output
    assert "现价" not in output


def test_t2_preopen_unknown_is_not_suspension():
    """盘前 is_suspended=None：必须写「盘前无法判断」，不得标不可执行。"""
    payload = {
        "instrument": {
            "code": "600000", "name": "示例银行",
            "is_st": False, "is_suspended": None, "quote_stale": True,
            "status_source": "tencent_quote",
        },
        "recommendation": {"action": "hold", "confidence": 0.6},
    }
    output = render_advice(payload)
    assert "盘前无法判断是否停牌" in output
    assert "未确认停牌" in output
    assert "不建议执行" not in output
    assert "停牌，今日不可执行" not in output


def test_t2_normal_stock_not_marked_unexecutable():
    output = _render("normal_buy")
    assert "不建议执行" not in output
    assert "交易状态" not in output


# ---------------------------------------------------------------------------
# T3：regime 冲突
# ---------------------------------------------------------------------------
def test_t3_conflict_outputs_headwind():
    output = _render("quote_stale")  # 个股 Bull 0.7 / 指数 Shock 0.5
    assert "⚠️ 大盘逆风：指数 Shock(0.5) vs 个股 Bull(0.7)" in output
    assert "不改变建议" in output
    assert "不要买" not in output


def test_t3_tailwind_when_index_is_stronger():
    payload = _payload("st_warning")  # 个股 Bear 0.6
    payload["market_regime"]["index"] = {
        "label": "Bull", "confidence": 0.8, "as_of": "2026-11-13",
        "definition": "ma20_ma60_return_volatility",
        "source": "tencent_index:sh000300", "bars": 500,
    }
    output = render_advice(payload)
    assert "⚠️ 大盘顺风" in output


def test_t3_same_label_no_line():
    output = _render("normal_buy")  # 两边都是 Bull
    assert "大盘逆风" not in output
    assert "大盘顺风" not in output


def test_t3_missing_index_no_line_no_error():
    output = _render("suspended")  # market_regime 无 index 键
    assert "大盘逆风" not in output
    assert "大盘顺风" not in output
    assert "建议仓位 0%" in output  # 其余建议仍然正常渲染


# ---------------------------------------------------------------------------
# T4：估值分位
# ---------------------------------------------------------------------------
def test_t4_percentile_rounding_half_up():
    output = _render("normal_buy")
    assert "PE 分位 12%（3 年" in output   # 12.46 → 12
    assert "PB 分位 30%（3 年" in output   # 30.10 → 30
    assert "TTM" in output                 # PE 必须带口径标签


def test_t4_unavailable_means_no_sentence():
    output = _render("st_warning")  # available=false / cache_missing
    assert "估值：" not in output
    assert "估值适中" not in output
    assert "不贵" not in output


def test_t4_small_sample_is_labelled():
    output = _render("quote_stale")  # pe 样本 600 < 756
    assert "PE 分位 88%" in output
    assert "（样本 600 日）" in output


# ---------------------------------------------------------------------------
# T5：资金流方向 + 口径
# ---------------------------------------------------------------------------
def test_t5_eastmoney_direction_and_caliber():
    output = _render("normal_buy")
    assert "主力 5 日净流入 1.23 亿" in output
    assert "东财主力口径" in output


def test_t5_sina_caliber_is_distinguishable():
    payload = _payload("normal_buy")
    flow = payload["evidence"]["flow"]
    flow.update({
        "source": "sina_fund_flow_backup",
        "caliber": "sina_total_net",
        "main_net_5d": None,
        "main_net_20d": None,
        "net_5d": 56000000.0,
        "net_20d": 90000000.0,
        "direction": "inflow",
        "direction_basis": "net_5d_sign",
    })
    output = render_advice(payload)
    assert "总 5 日净流入 0.56 亿" in output
    assert "新浪总净额口径" in output
    assert "东财" not in output


def test_t5_mixed_source_has_direction_but_no_amount():
    payload = _payload("normal_buy")
    payload["evidence"]["flow"]["net_5d"] = 10000000.0  # 两族同时有值
    output = render_advice(payload)
    assert "混源口径" in output
    assert "净流入方向" in output
    assert "亿" not in output
    assert "1.23" not in output


def test_t5_stale_has_date_and_no_buy_wording():
    output = _render("quote_stale")
    flow_line = next(line for line in output.splitlines()
                     if line.startswith("资金流："))
    assert "2026-11-10 数据" in flow_line
    assert "买入" not in flow_line
    assert "建议" not in flow_line


# ---------------------------------------------------------------------------
# --json 结构 golden：渲染层不得改动既有输出结构
# ---------------------------------------------------------------------------
def _toy(n=150, seed=7, drift=0.001, vol=0.02, start=100.0):
    import numpy as np

    rng = np.random.default_rng(seed)
    close = start * np.cumprod(1 + rng.normal(drift, vol, n))
    idx = pd.date_range("2020-01-01", periods=n, freq="B")
    return pd.DataFrame({
        "open": close * 0.999, "high": close * (1 + vol),
        "low": close * (1 - vol), "close": close, "volume": 1e6,
    }, index=idx)


@pytest.fixture
def cli_response(monkeypatch):
    """离线、确定性的 CLI 成功响应（只用于锁定 JSON 键结构）。"""
    import cli
    import stock_analysis.data_loader.meta as meta
    from stock_analysis.backtest.engine import BacktestResult
    from stock_analysis.data_loader.flow import empty_flow_block
    from stock_analysis.data_loader.status import TradingStatus
    from stock_analysis.data_loader.valuation import empty_valuation_block

    df = _toy()
    monkeypatch.setattr(meta, "resolve_and_fetch",
                        lambda *a, **k: (df, "stock", None))
    monkeypatch.setattr(cli, "last_fetch_info", lambda: {
        "source": "cache", "from_cache": True, "degraded": False,
        "cache_last_date": "2026-11-13", "lag_days": 0,
        "expected_date": "2026-11-13", "requested_source": "cache",
        "source_attempts": [],
    })
    monkeypatch.setattr(cli, "get_stock_name", lambda code: "示例银行")
    monkeypatch.setattr(cli, "resolve_trading_status", lambda *a, **k: TradingStatus(
        is_st=False, is_suspended=False, limit_up=13.2, limit_down=10.8,
        quote_stale=False, status_source="eastmoney_st_list+tencent_quote"))
    monkeypatch.setattr(cli, "evidence_context", lambda *a, **k: (
        empty_flow_block(code="600000"),
        empty_valuation_block(code="600000", reason="cache_missing"),
        [],
    ))

    names = list(cli.build_strategy_signals().keys())[:3]
    comparison = pd.DataFrame(
        [{"评分": 1.0 - i * 0.1, "评分IS": 1.0 - i * 0.1,
          "评分OOS": 0.9 - i * 0.1, "年化收益": "10.00%",
          "最大回撤": "5.00%", "胜率": "60.00%", "交易次数": 20}
         for i, _ in enumerate(names)],
        index=names,
    )
    result = BacktestResult(trade_count=20, win_rate=0.6, annual_return=0.1,
                            max_drawdown=0.05, unaffordable_lot_events=0)
    result.equity_curve = [100000.0] * 50
    monkeypatch.setattr(cli, "run_strategy_tournament",
                        lambda *a, **k: (names[0], result, comparison))
    return cli.advise_internal("600000", fast=True, no_regime=True)


def test_cli_json_structure_golden(cli_response):
    golden = json.loads(GOLDEN.read_text(encoding="utf-8"))
    actual = {
        "top": sorted(cli_response),
        "instrument": sorted(cli_response["instrument"]),
        "data": sorted(cli_response["data"]),
        "market_regime": sorted(cli_response["market_regime"]),
        "market_regime.index": sorted(cli_response["market_regime"]["index"]),
        "recommendation": sorted(cli_response["recommendation"]),
        "evidence": sorted(cli_response["evidence"]),
        "evidence.flow": sorted(cli_response["evidence"]["flow"]),
        "evidence.valuation": sorted(cli_response["evidence"]["valuation"]),
        "diagnostics": sorted(cli_response["diagnostics"]),
    }
    assert actual == golden, "cli.py --json 的键结构被改动；渲染层不得改变既有 JSON"


def test_render_of_real_cli_response_never_leaks_none(cli_response):
    output = render_advice(copy.deepcopy(cli_response))
    assert output
    assert "None" not in output
    assert "免责声明：" in output


# ---------------------------------------------------------------------------
# 纯函数：零联网
# ---------------------------------------------------------------------------
def test_render_does_not_touch_network(monkeypatch):
    def _boom(*args, **kwargs):
        raise AssertionError("渲染阶段不得发起任何网络请求")

    monkeypatch.setattr(socket, "socket", _boom)
    monkeypatch.setattr(socket, "create_connection", _boom)
    output = render_advice(_payload("normal_buy"))
    assert "建议仓位 15%" in output


# ---------------------------------------------------------------------------
# tools/render_advice.py 包装
# ---------------------------------------------------------------------------
def _run_tool(*args, stdin: str | None = None):
    tool = ROOT / "tools" / "render_advice.py"
    return subprocess.run(
        [sys.executable, str(tool), *args],
        input=stdin, capture_output=True, text=True, timeout=60,
    )


def test_tool_wrapper_reads_file_and_stdin():
    fixture = FIXTURES / "normal_buy.json"
    expected = (FIXTURES / "normal_buy.expected.txt").read_text(
        encoding="utf-8").rstrip("\n") + "\n"

    by_file = _run_tool("--json-file", str(fixture))
    assert by_file.returncode == 0, by_file.stderr
    assert by_file.stdout == expected

    by_stdin = _run_tool(stdin=fixture.read_text(encoding="utf-8"))
    assert by_stdin.returncode == 0, by_stdin.stderr
    assert by_stdin.stdout == expected


def test_tool_wrapper_rejects_invalid_json():
    result = _run_tool(stdin="{not json")
    assert result.returncode == 2
    assert "JSON" in result.stderr
