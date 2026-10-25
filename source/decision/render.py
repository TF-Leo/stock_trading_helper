# -*- coding: utf-8 -*-
"""F23：`action_reason` 的唯一出口（契约 v2.0 §6.2）。

历史问题
--------
`cli.py:389` 在 `action` 决定（441–446 行）**之前**就生成了文案：

    trigger_msg = format_trigger_message(triggers, 'hold')

而 `format_trigger_message(triggers, action)` 的 `action` 形参在函数体内
**从未被使用**。于是文案永远不知道动作是什么，只能按触发器状态自行表述，
出现了 `action=hold` + `"卖出信号 (32.42)"` 这种自相矛盾。

本模块只做一件事：给定已经算好的 `action` 与 `signal`，生成**可核对**的理由。
五条硬规则（契约 §6.2）：
    1. 第一条必含当前策略仓位 + 依据日期；
    2. `action=hold` 时禁止出现"卖出信号"/"买入信号"字样，历史信号只写"卖点/买点 + 日期"；
    3. `action=sell` 必须写明"今日（YYYY-MM-DD）触发"，并说明由持有转为空仓；
    4. `action=buy` 必须写明今日触发与证据等级；
    5. 文案中的每个数字都能回溯到 signal / edge 的字段。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional


def _position_phrase(signal: Dict[str, Any]) -> str:
    """当前策略仓位 + 依据日期（规则 1）。"""
    ps = signal.get('position_state')
    if ps == 'holding':
        date, age = signal.get('last_buy_date'), signal.get('last_buy_age')
        if date:
            return f"策略当前持有（{date} 买点入场，距今 {age} 个交易日）"
        return "策略当前持有（样本内未见买点记录）"
    if ps == 'flat':
        date, age = signal.get('last_sell_date'), signal.get('last_sell_age')
        if date:
            return f"策略当前空仓（{date} 卖点离场，距今 {age} 个交易日）"
        return "策略当前空仓（样本内无买点记录）"
    return "策略仓位未知（信号不可用）"


def _near_phrase(state: Optional[str]) -> Optional[str]:
    if state == 'near_buy':
        return "指标已接近买点阈值，但尚未触发"
    if state == 'near_sell':
        return "指标已接近卖点阈值，但尚未触发"
    return None


def build_reasons(action: str, signal: Dict[str, Any],
                  expectancy: Optional[Dict[str, Any]] = None,
                  policy: Optional[Dict[str, Any]] = None,
                  status: Optional[str] = None,
                  review_date: Optional[str] = None,
                  horizon: int = 20) -> List[str]:
    """生成 `recommendation.action_reason`（1–3 条）。

    v3.0 规则：
      1. 每条都带日期与有效期，读者能判断"什么时候该重新看"；
      2. `action=hold` 时禁止出现"卖出信号"/"买入信号"，历史信号只写"卖点/买点 + 日期"；
      3. `action=sell` 必须说明是哪种风控（止损/止盈/时间止损），并声明"不代表趋势判断"；
      4. `action=buy` 必须给出该侧条件期望的数值（均值/样本/命中/t 值）。
    """
    from .actions import EXIT_LABELS

    signal = signal or {}
    expectancy = expectancy or {}
    policy = policy or {}
    as_of = signal.get('as_of') or '未知日期'
    reasons: List[str] = []

    mean = expectancy.get('mean')
    n = expectancy.get('n')
    hit = expectancy.get('hit_rate')
    t = expectancy.get('t_stat')
    exp_txt = (f"{horizon} 日期望 {mean:+.2%}（n={n}，命中 {hit:.1%}，t={t:+.1f}）"
               if None not in (mean, n, hit, t) else f"{horizon} 日期望样本不足")

    if action == 'sell':
        kind = EXIT_LABELS.get(policy.get('pending') or '', '风控')
        reasons.append(f"今日（{as_of}）触发{kind}，目标仓位 0%")
        reasons.append("这是风控离场（止损/止盈/到期），不代表趋势判断")
        if review_date:
            reasons.append(f"{review_date} 后可重新评估入场")

    elif action == 'buy':
        reasons.append(f"今日（{as_of}）触发买点；{exp_txt}")
        tail = "目标仓位 100%"
        if policy.get('stop_price') is not None and policy.get('target_price') is not None:
            tail += f"，止损 {policy['stop_price']}，止盈 {policy['target_price']}"
        reasons.append(tail + (f"；{review_date} 复审" if review_date else ""))

    elif action == 'avoid':
        reasons.append("不可交易或结果不可信，建议不参与")

    elif action == 'insufficient_data':
        reasons.append("数据不足，无法给出方向")

    else:  # hold
        if policy.get('holding'):
            reasons.append(
                f"持有中（第 {policy.get('bars_held', 0)}/{horizon} 个交易日，"
                f"入场 {policy.get('entry_date')} @ {policy.get('entry_price')}）；未触发风控")
            tail = (f"止损 {policy.get('stop_price')}、止盈 {policy.get('target_price')}"
                    if policy.get('stop_price') is not None else "止损/止盈不可用")
            reasons.append(tail + (f"；{review_date} 复审" if review_date else ""))
        else:
            reasons.append(f"策略当前空仓；今日无合格买点（{exp_txt}）")

    return reasons[:3]


def print_detail(response) -> None:
    """`--detail` 的文本输出（F29 期间从 cli 迁出，保持 cli 在 700 行以内）。"""
    rec = response['recommendation']
    ev = response['evidence']
    print(f"\n--- 详细 ---")
    print(f"止损/止盈方式：{rec.get('stop_method')}")
    print(f"入场区：{rec.get('entry_zone')}  距触发：{rec.get('distance_to_trigger')}")
    print(f"horizon：{rec.get('horizon_days')} 天  失效条件：{rec.get('invalidation')}")
    print(f"市场环境：{response.get('market_regime', {}).get('label')}"
          f"（{response.get('market_regime', {}).get('definition')}）")
    print(f"n_trials={ev.get('n_trials')}  DSR={ev.get('deflated_sharpe')}  "
          f"significant={ev.get('significant')}")
    exc = ev.get('excess_vs_buy_hold') or {}
    print(f"超额收益：point={exc.get('point')} ci_95={exc.get('ci_95')} "
          f"p_positive={exc.get('p_positive')} n_obs={exc.get('n_obs')}")
    print("候选（族 / 评分 / 年化 / 回撤 / 交易数）：")
    for c in response.get('candidates') or []:
        print(f"  - {c['name']:<28} {c['family']:<10} {c['score']:>8.4f} "
              f"{c['annual_return']:>8.2%} {c['max_drawdown']:>8.2%} {c['trade_count']:>5d}")
    print("触发器：")
    for t in ev.get('triggers') or []:
        print(f"  - {t['indicator']}: state={t['state']} current={t['current']} "
              f"buy_price={t['buy_price']} sell_price={t['sell_price']}")
    warns = (response.get('diagnostics') or {}).get('warnings') or []
    if warns:
        print("告警：")
        for w in warns:
            print(f"  [WARN] {w}")
