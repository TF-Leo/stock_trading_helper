# -*- coding: utf-8 -*-
"""issue #132：已触发态的「距触发」语义 + 仓位/入场区渲染矛盾。

五个快照（已触发 / 未触发 / sell / avoid / 基金）锁定渲染层的新口径：

* 已触发态只写「参考价 {P}，最近一次买点/卖点收盘」，**不得**出现「距触发」；
* ``target_position == 0`` 或 ``action`` 为 ``sell`` / ``avoid`` 时只写
  「目标仓位 0%」，不再并列「建议仓位」；
* ``entry_zone`` 为 null 时保留「入场区 —」占位；
* 渲染层不修改入参（``--json`` 既有字段与含义不变，本次未新增 JSON 字段）。

测试标的固定为 ``600519`` / ``600000``，不使用任何真实持仓代码。
"""
from __future__ import annotations

import copy

import pytest

from stock_analysis.advice_render import render_advice

DISCLAIMER = "本结果不构成投资建议，市场有风险，投资需谨慎。"


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
# 五个快照
# ---------------------------------------------------------------------------
def test_snapshot_triggered_buy_uses_reference_price():
    """已触发（买）：参考价来自 JSON，且不再出现「距触发」。"""
    payload = _stock_payload(
        "600519",
        _recommendation(action="buy", confidence=0.7,
                        position_fraction=0.2,
                        review_date="2026-12-15",
                        action_reason=["今日（2026-11-17）触发买点"]),
        triggers=[{
            "indicator": "均线趋势(3-5-8)", "state": "buy_triggered",
            "buy_price": 1490.0, "sell_price": None,
        }],
    )
    assert render_advice(payload) == "\n".join([
        "600519 示例标的 现价 1500",
        "动作 buy｜置信 70%（中）",
        "建议仓位 20%｜持有 20 日，2026-12-15 复审",
        "触发：均线趋势(3-5-8) 买信号已触发（参考价 1490，最近一次买点收盘）",
        "价位：入场区 1480-1520｜止损 1430｜止盈 1600",
        "依据：今日（2026-11-17）触发买点",
        f"免责声明：{DISCLAIMER}",
    ])
    assert "距触发" not in render_advice(payload)


def test_snapshot_untriggered_has_no_trigger_line():
    """未触发：没有触发事实行，「距触发」语义不变（本来就不出现）。"""
    payload = _stock_payload(
        "600519",
        _recommendation(),
        triggers=[{
            "indicator": "均线趋势(3-5-8)", "state": "neutral",
            "buy_price": 1490.0, "sell_price": None,
        }],
    )
    assert render_advice(payload) == "\n".join([
        "600519 示例标的 现价 1500",
        "动作 hold｜置信 60%（中）",
        "建议仓位 50%｜持有 20 日，2026-12-15 复审",
        "价位：入场区 1480-1520｜止损 1430｜止盈 1600",
        "依据：策略当前空仓；今日无合格买点",
        f"免责声明：{DISCLAIMER}",
    ])
    assert "触发：" not in render_advice(payload)
    assert "距触发" not in render_advice(payload)


def test_snapshot_sell_zero_target_and_entry_zone_placeholder():
    """sell：目标仓位 0% 取代建议仓位；入场区 null 保留占位。"""
    payload = _stock_payload(
        "600000",
        _recommendation(action="sell", confidence=0.65,
                        target_position=0.0, position_fraction=0.002,
                        entry_zone=None, stop_loss=11.5, take_profit=13.5,
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
        "动作 sell｜置信 65%（中）",
        "目标仓位 0%｜持有 20 日，2026-12-15 复审",
        "触发：均线趋势(3-5-8) 卖信号已触发（参考价 18，最近一次卖点收盘）",
        "价位：入场区 —｜止损 11.5｜止盈 13.5",
        "依据：今日（2026-11-17）触发止损，目标仓位 0%",
        f"免责声明：{DISCLAIMER}",
    ])
    assert "建议仓位" not in output
    assert "距触发" not in output


def test_snapshot_avoid_zero_target_and_entry_zone_placeholder():
    """avoid：只写目标仓位 0%；没有任何价位时仍保留入场区占位。"""
    payload = _stock_payload(
        "600000",
        _recommendation(action="avoid", confidence=0.0,
                        target_position=0.0, position_fraction=0.0,
                        horizon_days=None, review_date=None,
                        entry_zone=None, stop_loss=None, take_profit=None,
                        action_reason=["不可交易或结果不可信，建议不参与"]),
        last_close=12.0,
    )
    output = render_advice(payload)
    assert output == "\n".join([
        "600000 示例标的 现价 12",
        "动作 avoid｜置信 0%（低）",
        "目标仓位 0%",
        "价位：入场区 —",
        "依据：不可交易或结果不可信，建议不参与",
        f"免责声明：{DISCLAIMER}",
    ])
    assert "建议仓位" not in output


def test_snapshot_fund_trigger_uses_reference_price():
    """场外基金：名称等于代码只打印一次；历史参考价不写「距触发」。"""
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
        "动作 sell｜置信 60%（中）",
        "目标仓位 0%｜持有 20 日，2026-12-15 复审",
        "触发：场外基金净值 卖信号已触发（参考价 4.0527，最近一次卖点收盘）",
        "价位：入场区 —｜止损 2.5｜止盈 2.8",
        "依据：今日（2026-11-17）触发止盈，目标仓位 0%",
        f"免责声明：{DISCLAIMER}",
    ])
    assert "距触发" not in output
    assert "建议仓位" not in output


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
