# -*- coding: utf-8 -*-
"""F26：决策表 —— `action` 的唯一真源（契约 v2.0 §7）。

历史问题
--------
`cli.py` 曾用「最近 15 根 K 线**全部**为真」来确认强触发：

    has_sell_trigger = (_n_sig >= 15) and bool(signals['sell'].iloc[-15:].all())

均线死叉是**单根事件**，不可能连续 15 根为真，于是事件型策略的 `action`
被机制性地钉死为 `hold`。002415 在 2026-10-22 的 `hold` 不是判断结果，
是这个过滤器算出来的。该过滤器当初是为了让三窗口 `action` 一致（旧 B6），
属于"用语义错误换稳定"，已由契约 §8 的新不变式替换。

设计原则（不要"优化"掉）
------------------------
1. **卖出靠纪律**：风控动作不等统计显著性。
2. **买入靠证据**：没有可证明的超额优势就不建仓。
3. **动作是增量**：`buy`/`sell` 表示"相对策略当前仓位要做什么"，不是对行情的判断；
   当前仓位由 `recommendation.signal.position_state` 单独给出。
"""
from __future__ import annotations

from typing import Optional

# 动作语义（写进 README 与文案，避免下游误读）
ACTION_MEANING = {
    'buy': '相对当前空仓状态建仓',
    'sell': '相对当前持仓状态清仓',
    'hold': '不动作（维持当前仓位）',
    'avoid': '不参与（无统计优势或不可交易）',
    'insufficient_data': '数据不足，无法给出方向',
}

VALID_ACTIONS = tuple(ACTION_MEANING)


def decide(status: Optional[str], state: Optional[str],
           edge_verdict: Optional[str], confidence: float = 0.0,
           significant: bool = False) -> str:
    """按契约 §7 产出动作。

    注意：**不需要传 position_state**。`state` 的定义已经包含了"事件前"的仓位
    （`buy_triggered` ⇔ 此前空仓，`sell_triggered` ⇔ 此前持仓），再传事件**后**的
    仓位只会自相矛盾——首次实现就因为拿事后仓位判断事前前提，导致 buy 永不触发。

    Args:
        status: 数据/可交易性状态
        state: 当根信号状态（五档）
        edge_verdict: 超额收益证据等级（positive_significant/inconclusive/negative_significant）
        confidence: 置信度 0–1
        significant: 是否统计显著（当前实现 = DSR>0 且 CI 下界>0）

    Returns:
        `buy` / `sell` / `hold` / `avoid` / `insufficient_data`
    """
    if status in ('insufficient_data', 'data_source_error'):
        return 'insufficient_data'
    if status in ('untradeable', 'no_edge'):
        return 'avoid'

    # 卖出：当根真的发生了"持仓→空仓"事件（state 的定义已保证此前是 holding）。
    # 风控动作不等统计显著性。
    if state == 'sell_triggered':
        return 'sell'

    # 买入：必须同时满足"当根买点（此前空仓）+ 统计显著 + 置信度下限"。
    # 比旧实现更严（旧实现只要求 CI 上界>0，即"不能排除正收益"）。
    if (state == 'buy_triggered'
            and edge_verdict == 'positive_significant' and significant
            and float(confidence or 0.0) >= 0.30):
        return 'buy'

    return 'hold'
