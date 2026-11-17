# -*- coding: utf-8 -*-
"""issue #142：建议主行「按动作裁剪、说人话」。

主行只回答「现在做什么」，不再把模型内部字段并列倒给用户：

| 动作 | 主行 |
|---|---|
| buy | ``可买，仓位约 {x}%｜…｜入场区 …｜止损 …｜止盈 …`` |
| hold + holding | ``继续持有（第 N 个交易日）｜…｜止损 …｜止盈 …`` |
| hold + flat | ``暂不买（无合格买点）`` |
| sell | ``卖出（{风控类型}）｜…｜止损 …｜止盈 …``（不显示入场区） |
| avoid | ``不参与（{原因}）`` |

硬规则（本文件逐条断言）：

* 「仓位」只在 buy 出现；
* 「持有 N 日」「复审」只在 hold-holding 出现；
* sell / avoid / hold-flat 的主行不得出现「建议仓位」「目标仓位」「持有」
  「复审」；
* sell 不显示入场区。

实现只改渲染层：``--json`` 结构与既有键不变，``decision/*`` 未改动。
测试标的固定为 ``600519`` / ``600000``，不使用任何真实持仓代码。
"""
from __future__ import annotations

import copy

from stock_analysis.advice_render import render_advice

DISCLAIMER = "本结果不构成投资建议，市场有风险，投资需谨慎。"
_MAIN_ROW_PREFIXES = ("可买", "继续持有", "暂不买", "卖出", "不参与")


def _signal(*, position_state="flat", last_buy_age=None, as_of="2026-11-17"):
    return {
        "strategy": "示例策略",
        "indicator": "均线趋势(3-5-8)",
        "position_state": position_state,
        "state": "neutral",
        "as_of": as_of,
        "last_buy_date": None,
        "last_buy_age": last_buy_age,
        "last_sell_date": None,
        "last_sell_age": None,
        "current_value": 1500.0,
        "last_close": 1500.0,
        "threshold": {"buy": 1.0, "sell": 0.0},
        "conflict": False,
    }


def _payload(code, *, action, status="ok", confidence=0.6,
             position_fraction=0.2, horizon=(20, 20), signal=None,
             reason=None, stop_loss=1430.0, take_profit=1600.0,
             entry_zone=(1480.0, 1520.0)):
    rec = {
        "status": status,
        "action": action,
        "confidence": confidence,
        "action_reason": reason or ["示例理由"],
        "signal": signal or _signal(),
        "target_position": 1.0 if action in ("buy", "hold") else 0.0,
        "review_date": "2026-12-15",
        "position_fraction": position_fraction,
        "horizon_days": list(horizon) if horizon else None,
        "entry_zone": list(entry_zone) if entry_zone else None,
        "stop_loss": stop_loss,
        "take_profit": take_profit,
    }
    return {
        "request": {"code": code, "type": "stock"},
        "instrument": {
            "code": code, "name": "示例标的", "board": "sh_main",
            "is_st": False, "is_suspended": False, "quote_stale": False,
            "status_source": "tencent_quote",
        },
        "data": {
            "last_close": 1500.0, "end": "2026-11-17",
            "expected_date": "2026-11-17", "stale": False, "lag_days": 0,
        },
        "recommendation": rec,
        "evidence": {"triggers": []},
        "diagnostics": {"warnings": []},
        "disclaimer": DISCLAIMER,
    }


def _main_row(text: str) -> str:
    return next(line for line in text.splitlines()
                if line.startswith(_MAIN_ROW_PREFIXES))


# ---------------------------------------------------------------------------
# 五形态快照
# ---------------------------------------------------------------------------
def test_buy_main_row():
    payload = _payload(
        "600519", action="buy", confidence=0.7, position_fraction=0.2,
        signal=_signal(position_state="flat"),
        reason=["今日（2026-11-17）触发买点"],
    )
    assert _main_row(render_advice(payload)) == (
        "可买，仓位约 20%｜置信 70%（中）"
        "｜入场区 1480-1520｜止损 1430｜止盈 1600")


def test_hold_holding_main_row():
    payload = _payload(
        "600519", action="hold", position_fraction=0.2,
        signal=_signal(position_state="holding", last_buy_age=7),
        reason=["持有中（第 7/20 个交易日，入场 2026-11-06 @ 1450）；未触发风控"],
    )
    assert _main_row(render_advice(payload)) == (
        "继续持有（第 7 个交易日）｜置信 60%（中）｜止损 1430｜止盈 1600")


def test_hold_holding_overdue_appends_plain_note():
    payload = _payload(
        "600519", action="hold",
        signal=_signal(position_state="holding", last_buy_age=25),
        reason=["持有中（第 25/20 个交易日，入场 2026-10-10 @ 1450）；未触发风控"],
    )
    assert _main_row(render_advice(payload)) == (
        "继续持有（第 25 个交易日）｜已超建议持有期 5 日（不强制退出）"
        "｜置信 60%（中）｜止损 1430｜止盈 1600")


def test_holding_days_come_from_policy_fact_not_signal_age():
    """持仓期间出现新买点信号时 last_buy_age 会重置，不能拿它当持有天数。

    真实 600519 payload：``signal.last_buy_age=2``，但策略引擎已持有 21 个
    交易日；主行必须与 ``action_reason`` 的「第 21/20 个交易日」一致。
    """
    payload = _payload(
        "600519", action="hold",
        signal=_signal(position_state="holding", last_buy_age=2),
        reason=["持有中（第 21/20 个交易日，入场 2026-10-19 @ 1450）"
                "；已超建议持有期 1 日（不强制退出）；未触发风控"],
    )
    assert _main_row(render_advice(payload)) == (
        "继续持有（第 21 个交易日）｜已超建议持有期 1 日（不强制退出）"
        "｜置信 60%（中）｜止损 1430｜止盈 1600")


def test_hold_flat_main_row():
    payload = _payload(
        "600519", action="hold", position_fraction=0.5,
        signal=_signal(position_state="flat"),
        reason=["策略当前空仓；今日无合格买点"],
    )
    assert _main_row(render_advice(payload)) == "暂不买（无合格买点）｜置信 60%（中）"


def test_sell_main_row_uses_existing_risk_kind():
    payload = _payload(
        "600000", action="sell", status="ok", confidence=0.65,
        position_fraction=0.002, entry_zone=None,
        signal=_signal(position_state="holding"),
        reason=["今日（2026-11-17）触发止损，目标仓位 0%"],
    )
    assert _main_row(render_advice(payload)) == (
        "卖出（风控止损）｜置信 65%（中）｜止损 1430｜止盈 1600")


def test_sell_without_derivable_kind_writes_only_sell():
    payload = _payload(
        "600000", action="sell", entry_zone=None,
        signal=_signal(position_state="holding"),
        reason=["策略给出卖出动作"],
    )
    assert _main_row(render_advice(payload)) == (
        "卖出｜置信 60%（中）｜止损 1430｜止盈 1600")


def test_avoid_main_row_uses_existing_reason():
    payload = _payload(
        "600000", action="avoid", status="untradeable", confidence=0.0,
        position_fraction=0.0, signal=_signal(position_state="flat"),
        reason=["不可交易或结果不可信，建议不参与"],
    )
    assert _main_row(render_advice(payload)) == (
        "不参与（不可交易或结果不可信）｜置信 0%（低）")


# ---------------------------------------------------------------------------
# 硬规则：字段只跟着该动作出现
# ---------------------------------------------------------------------------
def _five_rows():
    return {
        "buy": _main_row(render_advice(_payload(
            "600519", action="buy", reason=["今日（2026-11-17）触发买点"]))),
        "hold_holding": _main_row(render_advice(_payload(
            "600519", action="hold",
            signal=_signal(position_state="holding", last_buy_age=7),
            reason=["持有中（第 7/20 个交易日，入场 2026-11-06 @ 1450）"]))),
        "hold_flat": _main_row(render_advice(_payload(
            "600519", action="hold", reason=["策略当前空仓；今日无合格买点"]))),
        "sell": _main_row(render_advice(_payload(
            "600000", action="sell", entry_zone=None,
            signal=_signal(position_state="holding"),
            reason=["今日（2026-11-17）触发止损，目标仓位 0%"]))),
        "avoid": _main_row(render_advice(_payload(
            "600000", action="avoid", status="untradeable",
            position_fraction=0.0,
            reason=["不可交易或结果不可信，建议不参与"]))),
    }


def test_position_wording_only_for_buy():
    rows = _five_rows()
    assert "仓位" in rows["buy"]
    for action in ("hold_holding", "hold_flat", "sell", "avoid"):
        assert "仓位" not in rows[action], (action, rows[action])


def test_holding_days_and_review_only_for_hold_holding():
    rows = _five_rows()
    assert "持有（第 7 个交易日）" in rows["hold_holding"]
    for action in ("buy", "hold_flat", "sell", "avoid"):
        assert "持有" not in rows[action], (action, rows[action])
    # 「复审」在任何动作的主行都不再出现（只作为 JSON 字段保留）。
    for action, row in rows.items():
        assert "复审" not in row, (action, row)


def test_sell_and_flat_rows_have_no_sizing_or_horizon():
    rows = _five_rows()
    for action in ("sell", "hold_flat", "avoid"):
        for word in ("建议仓位", "目标仓位", "持有", "复审"):
            assert word not in rows[action], (action, word, rows[action])


def test_entry_zone_only_on_buy():
    rows = _five_rows()
    assert "入场区 1480-1520" in rows["buy"]
    for action in ("hold_holding", "hold_flat", "sell", "avoid"):
        assert "入场区" not in rows[action], (action, rows[action])


def test_sell_and_holding_keep_stop_and_take_profit():
    rows = _five_rows()
    for action in ("hold_holding", "sell"):
        assert "止损 1430" in rows[action]
        assert "止盈 1600" in rows[action]


# ---------------------------------------------------------------------------
# 渲染层契约：纯函数、JSON 结构不变、不触网
# ---------------------------------------------------------------------------
def test_render_is_pure_and_json_keys_unchanged():
    payload = _payload(
        "600519", action="buy", reason=["今日（2026-11-17）触发买点"])
    original = copy.deepcopy(payload)
    render_advice(payload)
    assert payload == original


def test_json_keys_still_present_after_render():
    payload = _payload("600000", action="sell", entry_zone=None,
                       signal=_signal(position_state="holding"),
                       reason=["今日（2026-11-17）触发止损，目标仓位 0%"])
    render_advice(payload)
    rec = payload["recommendation"]
    for key in ("target_position", "position_fraction", "horizon_days",
                "review_date", "entry_zone", "stop_loss", "take_profit"):
        assert key in rec
