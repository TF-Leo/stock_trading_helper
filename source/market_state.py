# -*- coding: utf-8 -*-
"""issue #117：非交易日（休市）状态与退出码的唯一出口。

背景
----
休市日腾讯报价对所有标的定格，旧逻辑据此判停牌 → ``untradeable`` / 退出码
40，运维与 Hermes 会把「大面积休市」误读成「大面积停牌」。

两层职责，刻意分开
------------------
* **决策层**：``decision/actions.py::decide`` —— 只认识数据/可交易性状态，
  **不感知** ``market_closed``（本次约束不允许改 ``decision/*``）。
* **闸门层**（本模块）:func:`apply_market_state_gate` —— 回答「今天这条结论
  可不可执行」。休市日把 ``status`` 覆盖为 ``market_closed``（退出码 50）、
  ``action`` 固定 ``avoid``。与 P0-3 的 ``TradingStatus.execution_block``
  属同一层：不推翻决策表，只在 CLI 契约上做可执行性裁决。

「今天是不是交易日」的事实由
``stock_analysis.data_loader.status.market_day_state`` 产出（只认官方交易日历），
本模块不自行推算日期。个股用它判停牌，**基金路径同样用它**（场外基金净值
在休市日不更新，不能继续输出可执行建议）。
"""
from __future__ import annotations

import datetime as _dt

from decision.schema import EXIT_CODES, exit_code_for

MARKET_CLOSED_STATUS = "market_closed"
MARKET_CLOSED_EXIT_CODE = 50

#: 休市日允许被 market_closed 覆盖的状态（数据问题 20/30 优先保留）。
OVERRIDABLE_STATUSES = ("ok", "no_edge", "untradeable")

MARKET_CLOSED_REASON = "今日休市（非交易日）：本结论仅基于历史数据，不产生可执行建议"


def exit_code_for_status(status) -> int:
    """status -> 进程退出码。

    ``market_closed`` 走独立码 50；其余状态仍由
    ``decision.schema.exit_code_for`` 唯一决定。
    """
    if str(status) == MARKET_CLOSED_STATUS:
        return MARKET_CLOSED_EXIT_CODE
    return exit_code_for(status)


def market_closed_warning() -> str:
    """与状态覆盖配套的 warning（写进 ``diagnostics.warnings``）。"""
    return ("今日休市（官方交易日历：非交易日）：不产生可执行建议；"
            f"退出码 {MARKET_CLOSED_EXIT_CODE}"
            f"（与真实停牌 {EXIT_CODES['untradeable']} 区分）")


def market_instrument_fields(trading=None, now=None) -> dict:
    """``instrument`` 层的 ``market_closed`` / ``market_state_source`` 两键。

    * 个股：沿用 ``resolve_trading_status`` 同一次日历判定的结果；
    * 基金（``trading is None``）：**本次新增**——基金路径同样按官方交易日历
      判定今天是否休市（issue #117 P2-2）。场外基金在休市日不更新净值，
      旧行为会在法定节假日继续输出 ``ok`` 并退出 0。源名与个股一致
      （``astock_calendar`` / ``calendar_unavailable``），不再是 ``fund_static``。
    """
    if trading is not None:
        return {"market_closed": trading.market_closed,
                "market_state_source": trading.market_state_source}
    from stock_analysis.data_loader import status as _status
    state = _status.market_day_state(now or _dt.datetime.now())
    return {"market_closed": state.closed,
            "market_state_source": state.source}


def apply_market_state_gate(payload: dict) -> dict:
    """**闸门层**：休市日把可执行状态统一改成 ``market_closed``（退出码 50）。

    ``cli.py`` 只在响应构造完成后调用本函数一次；``decide()`` 不动，所以
    ``decide('market_closed', 'entry')`` 仍返回 ``buy`` —— 覆盖发生在闸门层，
    不是决策层，这是刻意的分层（见模块 docstring）。

    优先级：``insufficient_data`` / ``data_source_error``（数据本身的问题）
    优先保留；其余（``ok`` / ``no_edge`` / ``untradeable``）在官方日历确认
    非交易日时改为 ``market_closed``。
    """
    rec = payload.get("recommendation")
    inst = payload.get("instrument")
    if not isinstance(rec, dict) or not isinstance(inst, dict):
        return payload

    diagnostics = payload.setdefault("diagnostics", {})
    warnings = diagnostics.setdefault("warnings", [])
    if (inst.get("market_state_source") == "calendar_unavailable"
            and not any("交易日历不可用" in str(w) for w in warnings)):
        # 个股路径由 status 层告警；基金路径没有 status 层，闸门层补齐，不留静默。
        warnings.append("交易日历不可用：无法判定今日是否为交易日；"
                        "不把「工作日」当成交易日，也不据此判定停牌")

    if inst.get("market_closed") is not True:
        return payload
    if rec.get("status") not in OVERRIDABLE_STATUSES:
        # 数据问题（20/30）优先保留；已是 market_closed 则幂等返回。
        return payload

    rec["status"] = MARKET_CLOSED_STATUS
    rec["action"] = "avoid"
    rec["action_reason"] = [MARKET_CLOSED_REASON]
    # 动作被闸门改成 avoid 后，由 action 派生的字段必须一起收敛，
    # 否则会留下「avoid + target_position=1.0」的自相矛盾。
    rec["target_position"] = 0.0
    rec["position_fraction"] = 0.0
    rec["no_edge"] = False
    if not any("今日休市" in str(w) for w in warnings):
        warnings.append(market_closed_warning())
    return payload


def with_market_state_error_keys(builder):
    """错误响应的 ``instrument`` 也带 ``market_closed`` 两键（G35）。

    错误路径根本没有查交易状态：``market_closed=None`` +
    ``market_state_source='unavailable'`` 才是如实表达（不是 False）。
    """
    def wrapper(*args, **kwargs):
        doc = builder(*args, **kwargs)
        instrument = doc.get("instrument")
        if isinstance(instrument, dict):
            instrument.setdefault("market_closed", None)
            instrument.setdefault("market_state_source", "unavailable")
        return doc

    return wrapper