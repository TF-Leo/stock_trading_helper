# -*- coding: utf-8 -*-
"""issue #132：已触发态的参考价语义 + 仓位/入场区渲染。

复验后的口径（PR #133）：

* 已触发态的参考价**必须带日期**，并说明它属于哪一次触发：
  - 与当日信号同 bar → 「本次触发价」；
  - 当日动作是风控离场（止损/止盈/时间止损）而历史卖点更早 →
    「本次触发为风控止损；最近一次卖点参考价 P（D 收盘），非本次触发价」；
  - 无法判断来源 → 只写日期 + 「历史参考价」，不得暗示与本次触发相关；
  - 样本内无历史卖点 → 写「样本内无历史卖点」，不编造价格。
* issue #142 起主行按动作裁剪：仓位只随 ``buy`` 以「仓位约」出现；持有
  天数只随 ``hold`` + ``position_state=holding`` 出现；``sell`` / ``avoid`` /
  空仓 ``hold`` 不再并列「建议仓位」「目标仓位」「持有 N 日」「复审日」。
* ``buy`` 的 ``entry_zone`` 为 null 时保留「入场区 —」占位；``sell`` 不显示
  入场区（issue #132 / #142）；
* 渲染层不修改入参（``--json`` 既有字段与含义不变，本次未新增 JSON 字段）。

测试标的固定为 ``600519`` / ``600000``，不使用任何真实持仓代码。
"""
from __future__ import annotations

import copy

import pytest

from stock_analysis.advice_render import render_advice

DISCLAIMER = "本结果不构成投资建议，市场有风险，投资需谨慎。"


def _signal(**overrides) -> dict:
    """``recommendation.signal`` 的完整块（CLI 成功路径同构）。"""
    sig = {
        "strategy": "示例策略",
        "indicator": "均线趋势(3-5-8)",
        "position_state": "flat",
        "state": "neutral",
        "as_of": "2026-11-17",
        "last_buy_date": None,
        "last_buy_age": None,
        "last_sell_date": None,
        "last_sell_age": None,
        "current_value": 1215.0,
        "last_close": 1500.0,
        "threshold": {"buy": None, "sell": None},
        "conflict": False,
    }
    sig.update(overrides)
    return sig


def _stock_payload(code: str, rec: dict, *, triggers=None,
                   last_close: float = 1500.0) -> dict:
    payload = {
        "request": {"code": code, "type": "stock"},
        "instrument": {
            "code": code, "name": "示例标的", "board": "sh_main",
            "is_st": False, "is_suspended": False, "quote_stale": False,
            "status_source": "tencent_quote",
        },
        "data": {
            "last_close": last_close, "end": "2026-11-17",
            "expected_date": "2026-11-17", "stale": False, "lag_days": 0,
        },
        "recommendation": rec,
        "evidence": {"triggers": triggers or []},
        "diagnostics": {"warnings": []},
        "disclaimer": DISCLAIMER,
    }
    return payload


def _recommendation(**overrides) -> dict:
    rec = {
        "status": "ok",
        "action": "hold",
        "confidence": 0.6,
        "action_reason": ["策略当前空仓；今日无合格买点"],
        "target_position": 1.0,
        "position_fraction": 0.5,
        "horizon_days": [20, 20],
        "review_date": "2026-12-15",
        "entry_zone": [1480.0, 1520.0],
        "stop_loss": 1430.0,
        "take_profit": 1600.0,
        "distance_to_trigger": {"buy_pct": -0.67, "sell_pct": None},
    }
    rec.update(overrides)
    return rec


# ---------------------------------------------------------------------------
# 五种形态快照
# ---------------------------------------------------------------------------
def test_snapshot_triggered_buy_uses_today_reference_price():
    """已触发（买）：历史 bar 与当日同源 → 参考价带日期且标「本次触发价」。"""
    payload = _stock_payload(
        "600519",
        _recommendation(action="buy", confidence=0.7,
                        position_fraction=0.2,
                        review_date="2026-12-15",
                        signal=_signal(state="buy_triggered",
                                       position_state="flat",
                                       last_buy_date="2026-11-17",
                                       last_buy_age=0),
                        action_reason=["今日（2026-11-17）触发买点"]),
        triggers=[{
            "indicator": "均线趋势(3-5-8)", "state": "buy_triggered",
            "buy_price": 1490.0, "sell_price": None,
        }],
    )
    assert render_advice(payload) == "\n".join([
        "600519 示例标的 现价 1500",
        "可买，仓位约 20%｜置信 70%（中）｜入场区 1480-1520｜止损 1430｜止盈 1600",
        "触发：均线趋势(3-5-8) 买信号已触发"
        "（参考价 1490（2026-11-17 收盘），本次触发价）",
        "依据：今日（2026-11-17）触发买点",
        f"免责声明：{DISCLAIMER}",
    ])
    assert "距触发" not in render_advice(payload)


def test_snapshot_untriggered_has_no_trigger_line():
    """未触发：没有触发事实行，「距触发」语义不变（本来就不出现）。"""
    payload = _stock_payload(
        "600519",
        _recommendation(signal=_signal(state="neutral")),
        triggers=[{
            "indicator": "均线趋势(3-5-8)", "state": "neutral",
            "buy_price": 1490.0, "sell_price": None,
        }],
    )
    assert render_advice(payload) == "\n".join([
        "600519 示例标的 现价 1500",
        "暂不买（无合格买点）｜置信 60%（中）",
        "依据：策略当前空仓；今日无合格买点",
        f"免责声明：{DISCLAIMER}",
    ])
    assert "触发：" not in render_advice(payload)
    assert "距触发" not in render_advice(payload)


def test_snapshot_sell_risk_exit_hides_position_and_horizon():
    """sell（风控止损）：主行只写卖出原因 + 止损/止盈，不展示仓位/持有期。"""
    payload = _stock_payload(
        "600000",
        _recommendation(action="sell", confidence=0.65,
                        target_position=0.0, position_fraction=0.002,
                        entry_zone=None, stop_loss=11.5, take_profit=13.5,
                        signal=_signal(state="sell_triggered",
                                       position_state="holding",
                                       last_sell_date="2026-09-24",
                                       last_sell_age=31),
                        action_reason=["今日（2026-11-17）触发止损，目标仓位 0%"]),
        triggers=[{
            "indicator": "均线趋势(3-5-8)", "state": "sell_triggered",
            "buy_price": 10.0, "sell_price": 18.0,
        }],
        last_close=12.0,
    )
    output = render_advice(payload)
    assert output == "\n".join([
        "600000 示例标的 现价 12",
        "卖出（风控止损）｜置信 65%（中）｜止损 11.5｜止盈 13.5",
        "触发：均线趋势(3-5-8) 卖信号已触发"
        "（本次触发为风控止损；最近一次卖点参考价 18"
        "（2026-09-24 收盘），非本次触发价）",
        "依据：今日（2026-11-17）触发止损，目标仓位 0%",
        f"免责声明：{DISCLAIMER}",
    ])
    row = output.splitlines()[1]
    for word in ("建议仓位", "目标仓位", "持有", "复审", "入场区"):
        assert word not in row
    assert "距触发" not in output


def test_snapshot_avoid_has_only_plain_reason():
    """avoid：只写「不参与（原因）」；仓位/持有期/价位一律不出现。"""
    payload = _stock_payload(
        "600000",
        _recommendation(action="avoid", confidence=0.0,
                        target_position=0.0, position_fraction=0.0,
                        horizon_days=None, review_date=None,
                        entry_zone=None, stop_loss=None, take_profit=None,
                        signal=_signal(state="neutral", position_state="flat"),
                        action_reason=["不可交易或结果不可信，建议不参与"]),
        last_close=12.0,
    )
    output = render_advice(payload)
    assert output == "\n".join([
        "600000 示例标的 现价 12",
        "不参与（不可交易或结果不可信）｜置信 0%（低）",
        "依据：不可交易或结果不可信，建议不参与",
        f"免责声明：{DISCLAIMER}",
    ])
    row = output.splitlines()[1]
    for word in ("建议仓位", "目标仓位", "持有", "复审", "入场区"):
        assert word not in row


def test_snapshot_fund_risk_exit_distinguishes_historical_sell_point():
    """场外基金：名称等于代码只打印一次；4.0527 属于 31 日前的历史卖点。"""
    payload = {
        "request": {"code": "600000", "type": "fund"},
        "instrument": {
            "code": "600000", "name": "600000", "board": "fund",
            "is_st": None, "is_suspended": None, "quote_stale": False,
            "status_source": "fund_static",
        },
        "data": {
            "last_close": 2.6, "end": "2026-11-17",
            "expected_date": "2026-11-17", "stale": False, "lag_days": 0,
        },
        "recommendation": _recommendation(
            action="sell", confidence=0.6, target_position=0.0,
            position_fraction=0.002, entry_zone=None,
            stop_loss=2.5, take_profit=2.8,
            signal=_signal(state="sell_triggered", position_state="holding",
                           last_sell_date="2026-09-24", last_sell_age=31),
            action_reason=["今日（2026-11-17）触发止盈，目标仓位 0%"],
        ),
        "evidence": {"triggers": [{
            "indicator": "场外基金净值", "state": "sell_triggered",
            "buy_price": 2.1, "sell_price": 4.0527,
        }]},
        "diagnostics": {"warnings": []},
        "disclaimer": DISCLAIMER,
    }
    output = render_advice(payload)
    assert output == "\n".join([
        "600000 现价 2.6",
        "卖出（风控止盈）｜置信 60%（中）｜止损 2.5｜止盈 2.8",
        "触发：场外基金净值 卖信号已触发"
        "（本次触发为风控止盈；最近一次卖点参考价 4.0527"
        "（2026-09-24 收盘），非本次触发价）",
        "依据：今日（2026-11-17）触发止盈，目标仓位 0%",
        f"免责声明：{DISCLAIMER}",
    ])
    assert "距触发" not in output
    row = output.splitlines()[1]
    for word in ("建议仓位", "目标仓位", "持有", "复审", "入场区"):
        assert word not in row


# ---------------------------------------------------------------------------
# 三种触发形态（复验新增）
# ---------------------------------------------------------------------------
def _trigger_lines(output: str):
    return [line for line in output.splitlines() if line.startswith("触发：")]


def test_trigger_reference_risk_stop_is_not_todays_trigger_price():
    """形态 1：当日触发来自风控止损，历史卖点 31 个交易日前。"""
    payload = _stock_payload(
        "600000",
        _recommendation(action="sell", target_position=0.0,
                        position_fraction=0.0, entry_zone=None,
                        signal=_signal(state="sell_triggered",
                                       position_state="holding",
                                       last_sell_date="2026-09-24",
                                       last_sell_age=31),
                        action_reason=["今日（2026-11-17）触发止损，目标仓位 0%"]),
        triggers=[{"indicator": "均线趋势(3-5-8)", "state": "sell_triggered",
                   "buy_price": 12.5, "sell_price": 4.0527}],
        last_close=4.3,
    )
    line = _trigger_lines(render_advice(payload))[0]
    assert "本次触发为风控止损" in line
    assert "2026-09-24" in line
    assert "非本次触发价" in line
    assert "参考价 4.0527" in line
    # 不得把 4.0527 说成本次触发价，也不得把历史价位与现价做百分比。
    assert "本次触发价" not in line.replace("非本次触发价", "")
    assert "距触发" not in line


def test_trigger_reference_indicator_signal_is_todays_trigger_price():
    """形态 2：当日触发来自指标信号（历史 bar 就是当日）。"""
    payload = _stock_payload(
        "600519",
        _recommendation(action="buy",
                        signal=_signal(state="buy_triggered",
                                       position_state="flat",
                                       last_buy_date="2026-11-17",
                                       last_buy_age=0),
                        action_reason=["今日（2026-11-17）触发买点"]),
        triggers=[{"indicator": "均线趋势(3-5-8)", "state": "buy_triggered",
                   "buy_price": 1490.0, "sell_price": None}],
    )
    line = _trigger_lines(render_advice(payload))[0]
    assert line == ("触发：均线趋势(3-5-8) 买信号已触发"
                    "（参考价 1490（2026-11-17 收盘），本次触发价）")


def test_trigger_reference_without_historical_sell_point():
    """形态 3：样本内无历史卖点，仍能说明本次触发来源，不编造价格。"""
    payload = _stock_payload(
        "600000",
        _recommendation(action="sell", target_position=0.0,
                        position_fraction=0.0, entry_zone=None,
                        signal=_signal(state="sell_triggered",
                                       position_state="holding"),
                        action_reason=["今日（2026-11-17）触发止损，目标仓位 0%"]),
        triggers=[{"indicator": "均线趋势(3-5-8)", "state": "sell_triggered",
                   "buy_price": None, "sell_price": None}],
        last_close=12.0,
    )
    line = _trigger_lines(render_advice(payload))[0]
    assert line == ("触发：均线趋势(3-5-8) 卖信号已触发"
                    "（本次触发为风控止损；样本内无历史卖点）")


def test_trigger_reference_unknown_source_does_not_imply_todays_trigger():
    """来源不可判断：只写日期 +「历史参考价」，不暗示与本次触发相关。"""
    payload = _stock_payload(
        "600000",
        _recommendation(action="hold",
                        signal=_signal(state="sell_triggered",
                                       position_state="holding",
                                       last_sell_date="2026-09-24"),
                        action_reason=["持有中"]),
        triggers=[{"indicator": "示例指标", "state": "sell_triggered",
                   "buy_price": None, "sell_price": 18.0}],
    )
    line = _trigger_lines(render_advice(payload))[0]
    assert line == ("触发：示例指标 卖信号已触发"
                    "（参考价 18（2026-09-24 收盘），历史参考价）")
    assert "本次触发" not in line
    assert "非本次触发价" not in line


def test_trigger_reference_without_signal_block_is_date_unknown():
    """无 signal 块：不编造日期，明确写「日期未知」的历史参考价。"""
    payload = _stock_payload(
        "600000",
        _recommendation(action="hold",
                        action_reason=["持有中"]),
        triggers=[{"indicator": "示例指标", "state": "sell_triggered",
                   "buy_price": None, "sell_price": 18.0}],
    )
    line = _trigger_lines(render_advice(payload))[0]
    assert line == ("触发：示例指标 卖信号已触发"
                    "（参考价 18，历史参考价，日期未知）")


# ---------------------------------------------------------------------------
# 主行按动作裁剪（issue #142 起取代「建议仓位 / 目标仓位」并列）
# ---------------------------------------------------------------------------
_FORBIDDEN_ON_FLAT_OR_SELL = ("建议仓位", "目标仓位", "持有", "复审")


def test_hold_flat_hides_sizing_and_horizon():
    """空仓 hold：即使 position_fraction=0.5 也只在 buy 显示「仓位约」。"""
    payload = _stock_payload(
        "600519",
        _recommendation(action="hold", target_position=0.0,
                        position_fraction=0.5, entry_zone=None,
                        signal=_signal(state="neutral"),
                        action_reason=["策略当前空仓；今日无合格买点"]),
    )
    row = render_advice(payload).splitlines()[1]
    assert row == "暂不买（无合格买点）｜置信 60%（中）"
    for word in _FORBIDDEN_ON_FLAT_OR_SELL:
        assert word not in row


def test_avoid_never_shows_position_or_levels():
    """avoid：不显示仓位/持有期/价位，只写原因。"""
    payload = _stock_payload(
        "600000",
        _recommendation(action="avoid", target_position=0.0,
                        position_fraction=0.0, entry_zone=None,
                        signal=_signal(state="neutral"),
                        action_reason=["不可交易或结果不可信，建议不参与"]),
    )
    row = render_advice(payload).splitlines()[1]
    assert row == "不参与（不可交易或结果不可信）｜置信 60%（中）"
    for word in _FORBIDDEN_ON_FLAT_OR_SELL + ("入场区", "止损", "止盈"):
        assert word not in row


def test_sell_never_shows_position_horizon_or_entry_zone():
    """sell：风控类型进主行，仓位/持有期/入场区不出现。"""
    payload = _stock_payload(
        "600000",
        _recommendation(action="sell", target_position=0.0,
                        position_fraction=0.0004, entry_zone=None,
                        signal=_signal(state="sell_triggered",
                                       position_state="holding",
                                       last_sell_date="2026-09-24"),
                        action_reason=["今日（2026-11-17）触发止损，目标仓位 0%"]),
        triggers=[{"indicator": "均线趋势(3-5-8)", "state": "sell_triggered",
                   "buy_price": None, "sell_price": 18.0}],
    )
    row = render_advice(payload).splitlines()[1]
    assert row == "卖出（风控止损）｜置信 60%（中）｜止损 1430｜止盈 1600"
    for word in _FORBIDDEN_ON_FLAT_OR_SELL + ("入场区",):
        assert word not in row


def test_buy_keeps_position_even_when_target_position_is_one():
    """buy：建议仓位以「仓位约」出现；目标仓位不再并列。"""
    payload = _stock_payload(
        "600519",
        _recommendation(action="buy", target_position=1.0,
                        position_fraction=0.15),
    )
    row = render_advice(payload).splitlines()[1]
    assert row.startswith("可买，仓位约 15%｜置信 60%（中）｜入场区 1480-1520")
    assert "建议仓位" not in row
    assert "目标仓位" not in row


# ---------------------------------------------------------------------------
# JSON 不变（additive-only 约束）与纯函数
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("rec_overrides,triggers", [
    ({"action": "buy", "target_position": 1.0},
     [{"indicator": "甲", "state": "buy_triggered",
       "buy_price": 1490.0, "sell_price": None}]),
    ({"action": "sell", "target_position": 0.0}, []),
])
def test_render_keeps_existing_json_fields(rec_overrides, triggers):
    payload = _stock_payload("600519", _recommendation(**rec_overrides),
                             triggers=triggers)
    original = copy.deepcopy(payload)
    render_advice(payload)
    assert payload == original, "渲染层不得修改入参（JSON 既有字段与含义不变）"
    assert "distance_to_trigger" in payload["recommendation"]