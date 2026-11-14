# -*- coding: utf-8 -*-
"""issue #117：非交易日（休市）状态与退出码的唯一出口。

背景
----
休市日腾讯报价对所有标的定格，旧逻辑据此判停牌 → ``untradeable`` / 退出码
40，运维与 Hermes 会把「大面积休市」误读成「大面积停牌」。

本模块只做三件与 **CLI 契约**有关的事（``decision/*`` 本次不改，故不放进该
目录）：

1. :data:`MARKET_CLOSED_STATUS` / :data:`MARKET_CLOSED_EXIT_CODE`（50）；
2. :func:`exit_code_for_status`：``market_closed`` 走 50，其余仍由
   ``decision.schema.exit_code_for`` 唯一决定；
3. :func:`with_market_state_error_keys`：错误响应也带
   ``instrument.market_closed`` / ``instrument.market_state_source``，
   保持 G35「错误/成功路径键同构」。

「今天是不是交易日」的事实由 ``stock_analysis.data_loader.status.market_day_state``
产出（只认官方交易日历）；本模块不自行推算日期。
"""
from __future__ import annotations

from decision.schema import EXIT_CODES, exit_code_for

MARKET_CLOSED_STATUS = "market_closed"
MARKET_CLOSED_EXIT_CODE = 50

#: 休市日允许被 market_closed 覆盖的状态（数据问题 20/30 优先保留）。
OVERRIDABLE_STATUSES = ("ok", "no_edge", "untradeable")


def exit_code_for_status(status) -> int:
    """status -> 进程退出码。

    ``market_closed`` 走独立码 50；其余状态仍由
    ``decision.schema.exit_code_for`` 唯一决定。
    """
    if str(status) == MARKET_CLOSED_STATUS:
        return MARKET_CLOSED_EXIT_CODE
    return exit_code_for(status)


def market_closed_override(trading, status: str) -> str:
    """休市日把可执行状态统一改成 ``market_closed``；返回新状态。

    优先级：``insufficient_data`` / ``data_source_error``（数据本身的问题）
    优先保留；其余（``ok`` / ``no_edge`` / ``untradeable``）在官方日历确认
    非交易日时改为 ``market_closed``。
    """
    if (trading is not None and getattr(trading, "market_closed", None) is True
            and status in OVERRIDABLE_STATUSES):
        return MARKET_CLOSED_STATUS
    return status


def market_closed_warning() -> str:
    """与状态覆盖配套的 warning（写进 ``diagnostics.warnings``）。"""
    return ("今日休市（官方交易日历：非交易日）：不产生可执行建议；"
            f"退出码 {MARKET_CLOSED_EXIT_CODE}"
            f"（与真实停牌 {EXIT_CODES['untradeable']} 区分）")


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