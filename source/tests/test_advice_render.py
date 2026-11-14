# -*- coding: utf-8 -*-
"""建议渲染层 v1 的离线测试（issue #1 / R11 T1–T5 + issue #92）。

覆盖：
* 9 个 fixture 的完整输出快照（正常买点 / 停牌 / 一字板 / 显式一字板字段 /
  ST+warning / quote_stale / 可选字段全缺 / no_edge 白话 / 数据滞后）；
* T1 仓位/持有期/复审日与触发事实行（triggers + distance_to_trigger）；
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
import re
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

from stock_analysis.advice_render import (  # noqa: E402
    COMPACT_GLOSSARY,
    render_advice,
)

FIXTURE_NAMES = (
    "normal_buy",
    "suspended",
    "one_word_up",
    "one_word_explicit",
    "st_warning",
    "quote_stale",
    "minimal_missing",
    "data_stale_lag4",
    "no_edge",
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


def test_fixtures_cover_required_cases():
    assert len(FIXTURE_NAMES) == 9
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
# T1b：触发事实行（issue #92）
# ---------------------------------------------------------------------------
def test_trigger_line_matches_json_values():
    """触发价与距触发百分比逐值取自 JSON，渲染层不得重算。"""
    payload = _payload("normal_buy")
    trigger = payload["evidence"]["triggers"][0]
    distance = payload["recommendation"]["distance_to_trigger"]["buy_pct"]
    assert trigger["buy_price"] == 12.0 and distance == 0.0
    assert "触发：均线趋势(3-5-8) 买信号已触发（触发价 12，现价距触发 0%）" \
        in render_advice(payload)


def test_trigger_line_sits_between_advice_and_reason():
    lines = _advice_lines(_render("normal_buy"))
    advice_at = next(i for i, line in enumerate(lines) if line.startswith("建议仓位"))
    trigger_at = next(i for i, line in enumerate(lines) if line.startswith("触发："))
    reason_at = next(i for i, line in enumerate(lines) if line.startswith("依据："))
    assert advice_at < trigger_at < reason_at


@pytest.mark.parametrize("mutate", [
    lambda p: p["evidence"].__setitem__("triggers", []),
    lambda p: p["evidence"].pop("triggers"),
    lambda p: p["evidence"].__setitem__("triggers", None),
    lambda p: p["evidence"].__setitem__("triggers", {}),
])
def test_trigger_line_disappears_without_triggers(mutate):
    payload = _payload("normal_buy")
    mutate(payload)
    assert "触发：" not in render_advice(payload)


@pytest.mark.parametrize("mutate", [
    lambda p: p["evidence"]["triggers"][0].__setitem__("buy_price", None),
    lambda p: p["evidence"]["triggers"][0].__setitem__("indicator", ""),
    lambda p: p["evidence"]["triggers"][0].__setitem__("state", "near_buy"),
    lambda p: p["recommendation"].pop("distance_to_trigger"),
    lambda p: p["recommendation"]["distance_to_trigger"].__setitem__("buy_pct", None),
])
def test_trigger_line_disappears_when_fields_missing(mutate):
    payload = _payload("normal_buy")
    mutate(payload)
    assert "触发：" not in render_advice(payload)


def test_trigger_line_uses_sell_side_fields():
    payload = _payload("normal_buy")
    payload["evidence"]["triggers"] = [{
        "indicator": "示例指标", "state": "sell_triggered",
        "buy_price": 13.0, "sell_price": 10.5,
    }]
    payload["recommendation"]["distance_to_trigger"] = {
        "buy_pct": 8.3, "sell_pct": -12.5,
    }
    line = next(line for line in render_advice(payload).splitlines()
                if line.startswith("触发："))
    assert line == "触发：示例指标 卖信号已触发（触发价 10.5，现价距触发 -12.5%）"


def test_trigger_line_lists_multiple_triggered_signals():
    payload = _payload("normal_buy")
    payload["evidence"]["triggers"] = [
        {"indicator": "甲", "state": "buy_triggered",
         "buy_price": 11.5, "sell_price": None},
        {"indicator": "乙", "state": "buy_triggered",
         "buy_price": 11.8, "sell_price": None},
    ]
    payload["recommendation"]["distance_to_trigger"] = {
        "buy_pct": -4.17, "sell_pct": None,
    }
    assert ("触发：甲 买信号已触发（触发价 11.5，现价距触发 -4.17%）；"
            "乙 买信号已触发（触发价 11.8，现价距触发 -4.17%）") in render_advice(payload)


def test_trigger_line_avoids_current_price_wording_when_stale():
    """行情非当日时不得写「现价」；距离值仍照抄 JSON。"""
    payload = _payload("data_stale_lag4")
    payload["evidence"]["triggers"] = [{
        "indicator": "甲", "state": "buy_triggered",
        "buy_price": 18.7, "sell_price": None,
    }]
    payload["recommendation"]["distance_to_trigger"] = {
        "buy_pct": 0.0, "sell_pct": None,
    }
    line = next(line for line in render_advice(payload).splitlines()
                if line.startswith("触发："))
    assert "距触发 0%" in line
    assert "现价距触发" not in line


# ---------------------------------------------------------------------------
# no_edge：一句不带术语的白话解释（issue #92）
# ---------------------------------------------------------------------------
def test_no_edge_has_plain_language_explanation():
    reason = next(line for line in _render("no_edge").splitlines()
                  if line.startswith("依据："))
    assert "历史样本不足以支持买入判断，建议观望" in reason
    assert "p_positive" not in reason
    assert "CI" not in reason


def test_no_edge_plain_sentence_is_not_a_forecast():
    reason = next(line for line in _render("no_edge").splitlines()
                  if line.startswith("依据："))
    for word in ("上涨", "下跌", "必涨", "保证", "预计", "预测"):
        assert word not in reason


def test_no_edge_sell_is_not_told_to_wait():
    payload = _payload("no_edge")
    payload["recommendation"].update({
        "action": "sell",
        "action_reason": ["今日（2026-11-13）触发止损，目标仓位 0%"],
    })
    reason = next(line for line in render_advice(payload).splitlines()
                  if line.startswith("依据："))
    assert "风控离场" in reason
    assert "建议观望" not in reason


def test_non_no_edge_status_has_no_plain_sentence():
    assert "历史样本不足以支持买入判断" not in _render("normal_buy")
    assert "历史样本不足以支持买入判断" not in _render("st_warning")


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


# ---------------------------------------------------------------------------
# 一字板：显式字段优先，warning 文案兜底（issue #92）
# ---------------------------------------------------------------------------
def test_one_word_gate_reads_explicit_field_without_warning():
    payload = _payload("one_word_explicit")
    assert payload["diagnostics"]["warnings"] == []
    gate = _advice_lines(render_advice(payload))[0]
    assert gate.startswith("【今日不建议执行】")
    assert "一字涨停" in gate


def test_one_word_gate_falls_back_to_warning_text():
    payload = _payload("one_word_explicit")
    payload["instrument"].pop("one_word_limit")
    payload["diagnostics"]["warnings"] = [
        "一字涨停（最高=最低=涨停价），买单无法成交，判定不可交易（P0-3）",
    ]
    gate = _advice_lines(render_advice(payload))[0]
    assert "一字涨停" in gate


def test_one_word_explicit_field_wins_over_warning_text():
    payload = _payload("one_word_explicit")
    payload["instrument"]["one_word_limit"] = "down"
    payload["diagnostics"]["warnings"] = [
        "一字涨停（最高=最低=涨停价），买单无法成交，判定不可交易（P0-3）",
    ]
    gate = _advice_lines(render_advice(payload))[0]
    assert "一字跌停" in gate
    assert "一字涨停" not in gate


def test_one_word_null_field_without_warning_does_not_gate():
    payload = _payload("one_word_explicit")
    payload["instrument"]["one_word_limit"] = None
    assert "不建议执行" not in render_advice(payload)


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


def test_t2_any_non_today_signal_replaces_current_price():
    """quote_stale=false 但 data.stale=true + lag_days=4 也必须走「收盘为」。"""
    output = _render("data_stale_lag4")
    assert "收盘为 2026-11-09" in output
    assert "滞后 4 日" in output
    assert "现价" not in output


def test_t2_expected_date_ahead_is_enough():
    payload = _payload("normal_buy")
    payload["data"].update({
        "stale": False, "lag_days": 0, "expected_date": "2026-11-16",
    })
    output = render_advice(payload)
    assert "收盘为 2026-11-13" in output
    assert "非最新交易日" in output
    assert "现价" not in output


def test_t2_fresh_data_still_says_current_price():
    """四项新鲜度信号都为「当日」时保持原行为。"""
    output = _render("normal_buy")
    assert "现价 12" in output
    assert "收盘为" not in output


# ---------------------------------------------------------------------------
# 名称：场外基金 name == code 时不得打印「代码 代码」
# ---------------------------------------------------------------------------
def _fund_payload(**instrument_overrides):
    payload = {
        "request": {"code": "123456", "type": "fund"},
        "instrument": {
            "code": "123456",
            "name": "123456",
            "board": "fund",
            "is_st": None,
            "is_suspended": None,
            "quote_stale": None,
            "status_source": "fund_static",
        },
        "data": {
            "last_close": 1.2345, "end": "2026-11-13",
            "expected_date": "2026-11-13", "stale": False, "lag_days": 0,
        },
        "recommendation": {"action": "hold", "confidence": 0.5},
        "market_regime": {"label": "Bull", "confidence": 0.7,
                          "index": {"label": "Shock", "confidence": 0.5}},
    }
    payload["instrument"].update(instrument_overrides)
    return payload


def test_name_equals_code_prints_code_once():
    output = render_advice(_fund_payload())
    assert output.splitlines()[0] == "123456 现价 1.2345"
    assert "123456 123456" not in output


def test_instrument_display_name_wins():
    output = render_advice(_fund_payload(display_name="示例基金"))
    assert output.splitlines()[0] == "123456 示例基金 现价 1.2345"


def test_top_level_display_name_is_supported():
    payload = _fund_payload()
    payload["display_name"] = "示例基金二"
    output = render_advice(payload)
    assert output.splitlines()[0] == "123456 示例基金二 现价 1.2345"


def test_empty_name_prints_code_only():
    output = render_advice(_fund_payload(name=""))
    assert output.splitlines()[0] == "123456 现价 1.2345"


def test_fund_regime_uses_object_wording():
    output = render_advice(_fund_payload())
    assert "vs 标的 Bull(0.7)" in output
    assert "个股" not in output


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
# --compact：只压缩 warnings，且只做整段删除
# ---------------------------------------------------------------------------
REAL_WARNINGS_FIXTURE = FIXTURES / "compact_real_warnings.json"
REAL_WARNINGS = json.loads(
    REAL_WARNINGS_FIXTURE.read_text(encoding="utf-8"))["diagnostics"]["warnings"]


def _compact_warning_lines(text: str):
    prefix = "⚠️ 数据: "
    return [line[len(prefix):] for line in text.splitlines()
            if line.startswith(prefix)]


def _compact_single(warning: str) -> str:
    """把单条 warning 放进最小 payload 渲染；整行消失时返回空串。"""
    payload = _payload("minimal_missing")
    payload["diagnostics"] = {"warnings": [warning]}
    lines = _compact_warning_lines(render_advice(payload, compact=True))
    return lines[0] if lines else ""


#: ``--compact`` 的词表是**封闭集合**：条目的增删必须同时改这条测试与 README
#: 的契约说明（issue #92 裁定）。这里刻意不复制实现的正则，只锁定集合内容。
CLOSED_GLOSSARY = {
    "p_positive": "买侧期望胜率",
    "trigger.current": "触发参考价",
    "is_st": "ST 标记",
    "ProxyError": "代理异常",
    "no_edge": "无统计优势",
    "95% CI": "置信区间",
}


def _is_deletion_subsequence(shorter: str, longer: str) -> bool:
    """``shorter`` 的字符是否按序出现在 ``longer`` 中（允许中间有删除）。"""
    iterator = iter(longer)
    return all(char in iterator for char in shorter)


def _parens_balanced(text: str) -> bool:
    depth = 0
    for char in text:
        if char in "（(":
            depth += 1
        elif char in "）)":
            depth -= 1
            if depth < 0:
                return False
    return depth == 0


#: 不含词表 token 的告警：compact 结果必须是原文的删除子序列。
DELETION_ONLY_WARNINGS = (
    *REAL_WARNINGS,
    "接口失败 https://example.com/a?b=1（RuntimeError: timeout）；已回退旧缓存（P0-3）",
    "状态未知：停牌/退市/代码无效无法区分",
    # token 落在整对括号里，括号被整段删除 → 结果仍只是删除。
    "统计证据不足（p_positive=0.42，n=12）",
    "两条独立事实。第二句不得被截断。",
    "括号不配对（issue #1 决策",
    "括号不配对 issue #1 决策）",
    "During handling of the above exception, another exception occurred: boom",
    'File "/tmp/x.py", line 12, in f',
)

#: 含词表 token 的告警：(原文, 期望 compact 输出)。
#: 期望值显式写出，**不**用裸 ``str.replace`` 复算——上一轮变异测试没抓住
#: 边界缺陷，正是因为测试 helper 与实现用了不同的替换语义。
GLOSSARY_CASES = (
    ("统计证据不足 p_positive=0.42", "统计证据不足买侧期望胜率=0.42"),
    ("统计证据不足 trigger.current=null", "统计证据不足触发参考价=null"),
    ("状态 is_st 只能依据名称判断", "状态 ST 标记只能依据名称判断"),
    ("上游抛 ProxyError", "上游抛代理异常"),
    # 两个词表 token 相邻：接缝里的空格也不得留下「中文 空格 中文」。
    ("is_st p_positive", "ST 标记买侧期望胜率"),
    # P4：no_edge 告警不得把 95% CI / no_edge 漏给用户。
    ("超额收益 95% CI 上界 ≤ 0：统计上无优势，判 no_edge（B8）",
     "超额收益置信区间上界 ≤ 0：统计上无优势，判无统计优势"),
)

#: token 前后粘连字母/数字/下划线/中文/dot 时不得替换（词边界）。
GLOSSARY_BOUNDARY_WARNINGS = (
    "字段 is_status 未知",
    "字段 my_p_positive 未知",
    "字段 p_positive_2 未知",
    "字段 trigger.current_x 未知",
    "字段 ProxyErrors 未知",
    "字段 xProxyError 未知",
    "仅is_st推断",
    "is_st仅按名称",
    "x.is_st 推断",
)


def test_compact_glossary_is_closed_set():
    assert COMPACT_GLOSSARY == CLOSED_GLOSSARY


@pytest.mark.parametrize("warning", DELETION_ONLY_WARNINGS)
def test_compact_deletion_only_warning_is_deletion_subsequence(warning):
    """不含词表 token：compact 结果必须仍是原文的删除子序列。"""
    assert _is_deletion_subsequence(_compact_single(warning), warning)


@pytest.mark.parametrize("warning,expected", GLOSSARY_CASES)
def test_compact_glossary_substitution_is_exact(warning, expected):
    """含词表 token：经公开 ``render_advice`` 断言逐字结果（含空格吸收）。"""
    assert _compact_single(warning) == expected


@pytest.mark.parametrize("warning", GLOSSARY_BOUNDARY_WARNINGS)
def test_compact_glossary_respects_word_boundaries(warning):
    """``is_st`` 不得命中 ``is_status`` / ``仅is_st推断`` 等粘连形式。

    这条测试就是边界 lookaround 的变异哨兵：去掉 ``(?<![\\w.])`` 或
    ``(?![\\w.])`` 后，上面 9 个用例至少 6 个立刻变红。
    """
    assert _compact_single(warning) == warning


def test_compact_glossary_does_not_insert_cjk_spaces():
    """P3：替换不得把中文与中文顶出空格（``ST 标记 只能`` → ``ST 标记只能``）。"""
    compacted = _compact_single("状态 is_st 只能依据名称判断")
    assert compacted == "状态 ST 标记只能依据名称判断"
    assert "ST 标记 只能" not in compacted
    assert "  " not in compacted
    assert not re.search(r"[\u4e00-\u9fff] [\u4e00-\u9fff]", compacted)


@pytest.mark.parametrize(
    "warning", DELETION_ONLY_WARNINGS + tuple(case[0] for case in GLOSSARY_CASES))
def test_compact_output_has_no_double_or_cjk_spaces(warning):
    compacted = _compact_single(warning)
    assert "  " not in compacted, compacted
    assert not re.search(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff] "
                         r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]", compacted), compacted


def test_compact_no_edge_warning_has_no_jargon_and_keeps_semantics():
    """P4：no_edge 告警在 compact 下不得再出现 ``95% CI`` / ``no_edge``。

    语义红线：只能说「统计上无优势」，不得写成「没有机会」或「样本不足」的
    反转（样本不足是另一类判定）。
    """
    payload = _payload("no_edge")
    payload["diagnostics"]["warnings"] = [
        "超额收益 95% CI 上界 ≤ 0：统计上无优势，判 no_edge（B8）",
    ]
    compact = render_advice(payload, compact=True)
    warning_line = next(line for line in compact.splitlines()
                        if line.startswith("⚠️ 数据:"))
    assert "95% CI" not in warning_line
    assert "CI" not in warning_line
    assert "no_edge" not in warning_line
    assert "置信区间" in warning_line
    assert "统计上无优势" in warning_line
    assert "没有机会" not in compact


def test_compact_warnings_strip_urls_stacks_and_codes():
    payload = _payload("one_word_up")
    payload["diagnostics"]["warnings"] = [
        "接口失败 https://example.com/a?b=1（RuntimeError: timeout）；已回退旧缓存（P0-3）",
        "Traceback (most recent call last): File \"/tmp/x.py\", line 1 ValueError: boom",
        "ST 名单为空。后续还有一句必须保留",
    ]
    normal = render_advice(payload)
    compact = render_advice(payload, compact=True)

    assert "https://" in normal
    assert "https://" not in compact
    for token in ("RuntimeError", "P0-3", "Traceback", "ValueError", "/tmp/x.py"):
        assert token not in compact
    assert "接口失败" in compact
    assert "已回退旧缓存" in compact
    # 只删整段：不得再按句号截断，第二句原样保留。
    assert "ST 名单为空。" in compact
    assert "后续还有一句必须保留" in compact

    # 非 warning 行逐行不变。
    normal_lines = [line for line in normal.splitlines()
                    if not line.startswith("⚠️ 数据:")]
    compact_lines = [line for line in compact.splitlines()
                     if not line.startswith("⚠️ 数据:")]
    assert normal_lines == compact_lines
    # 纯堆栈那条整段删除后为空，整行不出现；其余两条保留。
    assert len(_compact_warning_lines(compact)) == 2


def test_compact_real_warning_keeps_unknown_status():
    """复验缺陷 (a)：不得把「无法判断是否停牌」压成事实「停牌」。"""
    payload = json.loads(REAL_WARNINGS_FIXTURE.read_text(encoding="utf-8"))
    normal = render_advice(payload)
    compact = render_advice(copy.deepcopy(payload), compact=True)

    for warning in REAL_WARNINGS:
        assert f"⚠️ 数据: {warning}" in normal
    assert normal == render_advice(payload, compact=False)

    lines = _compact_warning_lines(compact)
    status_line = next(line for line in lines if "无法获取实时交易状态" in line)
    assert "停牌/退市/代码无效无法区分" in status_line
    assert "无法区分" in status_line
    assert not status_line.endswith("：停牌")

    # 复验缺陷 (b)：嵌套括号整对删除，不得残留内部引用或孤立括号。
    st_line = next(line for line in lines if "ST 名单接口失败" in line)
    assert "issue #1" not in st_line
    assert "RuntimeError" not in st_line
    assert "且无本地缓存" in st_line
    assert _parens_balanced(st_line)


@pytest.mark.parametrize(
    "warning", DELETION_ONLY_WARNINGS + tuple(case[0] for case in GLOSSARY_CASES))
def test_compact_never_breaks_parentheses(warning):
    compacted = _compact_single(warning)
    if _parens_balanced(warning):
        assert _parens_balanced(compacted)
    else:
        # 原文本身括号不配对：不得单侧删除，必须原样返回。
        assert compacted == warning


def test_compact_does_not_truncate_or_rewrite():
    warning = "第一条事实。第二条事实（issue #1 决策）。"
    assert _compact_single(warning) == "第一条事实。第二条事实。"


def test_compact_leaves_unbalanced_parentheses_untouched():
    warning = "状态无法判断（issue #1 决策"
    assert _compact_single(warning) == warning


def test_compact_internal_parenthetical_is_deleted_whole():
    assert _compact_single("统计证据不足（p_positive=0.42，n=12）") == "统计证据不足"


def test_compact_replaces_bare_internal_field_names():
    """词表内的裸字段名换成用户可读表述（issue #92）。"""
    warning = ("统计证据不足 p_positive=0.42；trigger.current 缺失；"
               "is_st 仅按名称判断；上游抛 ProxyError")
    compacted = _compact_single(warning)
    for token in ("p_positive", "trigger.current", "is_st", "ProxyError"):
        assert token not in compacted
    assert "买侧期望胜率=0.42" in compacted
    assert "触发参考价缺失" in compacted
    assert "ST 标记仅按名称判断" in compacted
    assert "上游抛代理异常" in compacted
    # P3：替换不得在中文之间留下空格。
    assert "触发参考价 缺失" not in compacted
    assert "ST 标记 仅按" not in compacted
    assert "上游抛 代理异常" not in compacted


def test_compact_field_vocabulary_does_not_touch_other_internal_names():
    """词表之外的内部字段名仍只许删除，不做替换。"""
    warning = "统计证据不足 confidence=0.7"
    assert _compact_single(warning) == warning


SAFETY_QUALIFIER_WARNINGS = (
    "状态未知：停牌/退市/代码无效无法区分（issue #92 说明）",
    "无法判断是否停牌（p_positive=0.2）",
    "ST 标的：退市与流动性风险（is_st 由报价名称判断）",
    "一字涨停（最高=最低=涨停价），买单无法成交（P0-3）",
    # 词表替换路径同样不得吃掉任何限定词。
    "状态未知：停牌/退市无法区分 is_st",
    "无法判断是否停牌 ProxyError",
    "一字跌停（最高=最低=跌停价），卖单无法成交 is_st",
)


@pytest.mark.parametrize("warning", SAFETY_QUALIFIER_WARNINGS)
def test_compact_preserves_safety_qualifiers(warning):
    """含停牌/退市/ST/一字的告警，压缩前后的限定词不得丢失。"""
    compacted = _compact_single(warning)
    assert compacted, warning
    for token in ("停牌", "退市", "ST", "一字"):
        if token in warning:
            assert token in compacted, (token, compacted)
    for token in ("无法区分", "无法判断", "无法成交"):
        if token in warning:
            assert token in compacted, (token, compacted)
    # 不得把「无法判断」压成事实性结论。
    assert not compacted.endswith("：停牌")
    # P3：词表替换后也不得在中文之间留下空格。
    assert not re.search(r"[\u4e00-\u9fff] [\u4e00-\u9fff]", compacted), compacted
    assert "  " not in compacted, compacted


def test_compact_does_not_change_default_output():
    for name in FIXTURE_NAMES:
        payload = _payload(name)
        assert render_advice(payload) == render_advice(payload, compact=False)
    payload = json.loads(REAL_WARNINGS_FIXTURE.read_text(encoding="utf-8"))
    assert render_advice(payload) == render_advice(payload, compact=False)


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


def test_tool_wrapper_name_override():
    result = _run_tool("--json-file", str(FIXTURES / "normal_buy.json"),
                       "--name", "覆盖简称")
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines()[0] == "600000 覆盖简称 现价 12"


def test_tool_wrapper_compact_flag():
    fixture = str(FIXTURES / "one_word_up.json")
    normal = _run_tool("--json-file", fixture)
    compact = _run_tool("--json-file", fixture, "--compact")
    assert normal.returncode == 0 and compact.returncode == 0
    assert "P0-3" in normal.stdout
    assert "P0-3" not in compact.stdout


def test_tool_wrapper_rejects_invalid_json():
    result = _run_tool(stdin="{not json")
    assert result.returncode == 2
    assert "JSON" in result.stderr
