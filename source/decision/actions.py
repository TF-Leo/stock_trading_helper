# -*- coding: utf-8 -*-
"""决策表：买入看条件期望，卖出只看风控（契约 v3.0 §7）。

与 v2.0 的两处根本差别
----------------------
1. **`significant` 不再门控买入。** v2.0 要求"策略全样本 alpha 显著"才允许买入，
   那是"策略值不值得信"的检验，几乎不可能通过，于是 `action=buy` 实际不可达。
   现在买入只要求：**当根买点 + 该侧 20 日条件期望为正且样本充足**。
2. **卖出不再由趋势反转触发。** 实测卖点后 20 日平均 +1.04%（仅 5/14 只为负），
   "跌破均线即清仓"是负期望。卖出只由风控触发：止损 / 止盈 / 时间止损。

动作语义不变：`buy`/`sell` 是**相对当前仓位的增量动作**，当前仓位见
`recommendation.signal.position_state`；`target_position` 给出动作后的目标仓位。
"""
from __future__ import annotations

from typing import Optional

ACTION_MEANING = {
    'buy': '相对当前空仓建仓',
    'sell': '相对当前持仓离场（风控触发）',
    'hold': '不动作（维持当前仓位）',
    'avoid': '不参与（不可交易）',
    'insufficient_data': '数据不足，无法给出方向',
}

VALID_ACTIONS = tuple(ACTION_MEANING)

# 风控出场类型 → 可读文案（render 与门禁共用，避免各写一份）
EXIT_LABELS = {
    'stop': '止损',
    'target': '止盈',
    'time': '时间止损',
}


def decide(status: Optional[str], pending: Optional[str],
           expectancy_ok: bool = False, holding: bool = False) -> str:
    """产出动作。

    Args:
        status: 数据/可交易性状态。**`no_edge` 不再阻断交易** —— 它只描述
                "该策略全样本 alpha 不显著"，是信任度信息，不是交易禁令。
        pending: 策略引擎在最后一根收盘给出的待执行动作（`entry`/`stop`/`target`/`time`/None）
        expectancy_ok: 该侧条件期望是否可用（样本量 + 方向 + t 值）
        holding: 当前是否持仓

    Returns:
        `buy` / `sell` / `hold` / `avoid` / `insufficient_data`
    """
    if status in ('insufficient_data', 'data_source_error'):
        return 'insufficient_data'
    if status == 'untradeable':
        return 'avoid'

    if pending in EXIT_LABELS:
        return 'sell'
    if pending == 'entry' and expectancy_ok and not holding:
        return 'buy'
    return 'hold'


def target_position_for(action: str, holding: bool) -> float:
    """动作之后的目标仓位（0 / 0.5 / 1.0）。"""
    if action == 'buy':
        return 1.0
    if action == 'sell':
        return 0.0
    if action == 'hold':
        return 1.0 if holding else 0.0
    return 0.0
