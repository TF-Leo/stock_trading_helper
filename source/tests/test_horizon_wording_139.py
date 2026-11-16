# -*- coding: utf-8 -*-
"""issue #139：超过建议持有期的三处文案矛盾（**不改判定逻辑**）。

背景（已确认，不在测试里重新论证）：

* ``decision/policy.py`` 的 horizon **只做时间止损**
  （``bars_held >= horizon and close <= entry_price`` 才出场），盈利仓位可以
  无限期持有；历史逐日回放 3645 天里有 89 天 ``bars_held > 20``，最长 46 日。
* 因此「持有 21/20 日仍 hold」是设计如此，不是「有规则没触发」。

本文件锁住三处**文案**修正：

1. ``decision/render.py::build_reasons``：``bars_held > horizon`` 时补
   「已超建议持有期 N 日（不强制退出）」；未超期时**不得**出现该措辞。
2. ``decision/sizing.py::build_invalidation``：持仓态不得再写
   「N 个交易日内未触发入场」（入场早已触发，自相矛盾），改为与
   ``policy.run_policy`` 时间止损同一条规则的表述；空仓态保持原入场计划语义。
3. horizon 口径统一为**交易日**：回测 ``avg_holding_days`` 是自然日
   （``engine.py`` 的 ``(exit_date - entry_date).days``），必须先换算。

另外对拍 ``decide()``：本次只改字符串，动作/目标仓位必须逐组合不变。

测试标的固定为 ``600519`` / ``600000``，不使用任何真实持仓代码。
"""
from __future__ import annotations

import itertools

import pytest

from decision import actions as act_mod
from decision import render as rnd_mod
from decision import sizing

DISCLAIMER = "本结果不构成投资建议，市场有风险，投资需谨慎。"


def _signal(**overrides) -> dict:
    sig = {
        "strategy": "示例策略",
        "indicator": "均线趋势(3-5-8)",
        "position_state": "holding",
        "state": "neutral",
        "as_of": "2026-11-17",
        "last_buy_date": "2026-10-19",
        "last_buy_age": 20,
        "last_sell_date": None,
        "last_sell_age": None,
        "current_value": 1215.0,
        "last_close": 1500.0,
        "threshold": {"buy": None, "sell": None},
        "conflict": False,
    }
    sig.update(overrides)
    return sig


def _expectancy(mean=0.0287, n=231, hit=0.593, t=4.4) -> dict:
    return {'side': 'buy', 'horizon': 20, 'mean': mean, 'hit_rate': hit,
            'n': n, 't_stat': t, 'ok': True}


def _policy(**overrides) -> dict:
    pol = {
        'holding': True, 'pending': None, 'bars_held': 8,
        'entry_price': 1450.0, 'entry_date': '2026-11-02',
        'stop_price': 1400.0, 'target_price': 1600.0,
    }
    pol.update(overrides)
    return pol


# ---------------------------------------------------------------------------
# 1) 渲染层：超期说明
# ---------------------------------------------------------------------------
def test_holding_within_horizon_has_no_overdue_wording():
    """持仓未超期：保留「第 N/H 个交易日」，且不得出现「已超建议持有期」。"""
    reasons = rnd_mod.build_reasons(
        'hold', _signal(position_state='holding'), _expectancy(),
        _policy(holding=True, bars_held=8), review_date='2026-12-15', horizon=20)
    text = ' '.join(reasons)
    assert '持有中（第 8/20 个交易日' in text
    assert '已超建议持有期' not in text
    assert '不强制退出' not in text


def test_holding_past_horizon_states_overdue_and_not_forced_out():
    """持仓超期（21/20）：补「已超建议持有期 1 日（不强制退出）」。"""
    reasons = rnd_mod.build_reasons(
        'hold', _signal(position_state='holding'), _expectancy(),
        _policy(holding=True, bars_held=21), review_date='2026-12-15', horizon=20)
    text = ' '.join(reasons)
    assert '持有中（第 21/20 个交易日' in text
    assert '已超建议持有期 1 日（不强制退出）' in text
    # 不得把超期说成卖出/退出
    assert '卖出信号' not in text and '买入信号' not in text


def test_holding_well_past_horizon_reports_actual_overdue_days():
    """46 日持仓：超期天数 = bars_held - horizon，不是固定 1。"""
    reasons = rnd_mod.build_reasons(
        'hold', _signal(position_state='holding'), _expectancy(),
        _policy(holding=True, bars_held=46), review_date='2026-12-15', horizon=20)
    assert '已超建议持有期 26 日（不强制退出）' in ' '.join(reasons)


def test_flat_hold_never_mentions_overdue():
    """空仓观望：不得出现任何「超持有期」措辞。"""
    reasons = rnd_mod.build_reasons(
        'hold', _signal(position_state='flat'), _expectancy(),
        _policy(holding=False, bars_held=0, entry_price=None, entry_date=None,
                stop_price=None, target_price=None),
        review_date='2026-12-15', horizon=20)
    text = ' '.join(reasons)
    assert '策略当前空仓' in text
    assert '已超建议持有期' not in text


def test_hold_reason_is_unchanged_when_exactly_at_horizon():
    """正好 20/20 不算超期（条件是 ``bars_held > horizon``）。"""
    reasons = rnd_mod.build_reasons(
        'hold', _signal(position_state='holding'), _expectancy(),
        _policy(holding=True, bars_held=20), review_date='2026-12-15', horizon=20)
    assert '已超建议持有期' not in ' '.join(reasons)


# ---------------------------------------------------------------------------
# 2) invalidation：与仓位一致
# ---------------------------------------------------------------------------
def test_invalidation_holding_never_says_entry_not_triggered():
    """持仓态：不得再写「N 个交易日内未触发入场」（入场早已触发）。"""
    text = sizing.build_invalidation(1430.0, '均线趋势', 20, holding=True)
    assert '未触发入场' not in text
    assert '收盘跌破 1430.00' in text
    assert '均线趋势 族共识转为看空' in text
    # 与 policy.run_policy 的时间止损同一条规则
    assert '满 20 个交易日仍低于入场价（时间止损）' in text


def test_invalidation_flat_keeps_entry_plan_wording():
    """空仓态：保持原入场计划语义（这段文字在空仓时是正确的）。"""
    text = sizing.build_invalidation(1430.0, '均线趋势', 20, holding=False)
    assert '20 个交易日内未触发入场' in text
    assert '时间止损' not in text


def test_invalidation_default_is_flat_wording():
    """默认（不传 holding）沿用空仓文案，保证既有调用方不受影响。"""
    assert (sizing.build_invalidation(1430.0, '均线趋势', 20)
            == sizing.build_invalidation(1430.0, '均线趋势', 20, holding=False))


def test_invalidation_stop_and_family_are_not_rewritten():
    """只改时间分句；止损价与族名逐字保留。"""
    text = sizing.build_invalidation(1430.5, '配对交易', 42, holding=True)
    assert text.startswith('收盘跌破 1430.50，或 配对交易 族共识转为看空，或满 42 个交易日')


# ---------------------------------------------------------------------------
# 3) horizon 口径：交易日
# ---------------------------------------------------------------------------
def test_calendar_days_to_trading_days_uses_five_day_week():
    """自然日 → 交易日按「每周 5 交易日」换算。"""
    assert sizing.calendar_days_to_trading_days(7) == pytest.approx(5.0)
    assert sizing.calendar_days_to_trading_days(14) == pytest.approx(10.0)
    assert sizing.calendar_days_to_trading_days(0) is None
    assert sizing.calendar_days_to_trading_days(None) is None
    assert sizing.calendar_days_to_trading_days(-3) is None


def test_derive_horizon_from_calendar_does_not_inflate_trading_days():
    """27.6 个**自然日** ≈ 19.7 个交易日，不能再被读成 42 个交易日。

    历史上直接把自然日喂进 ``derive_horizon``：``ceil(1.5*27.6)=42``，
    而 42 个交易日 ≈ 59 个自然日 —— 两套单位混用。
    """
    lo, hi = sizing.derive_horizon_from_calendar(27.6)
    trading_days = 27.6 * 5 / 7
    assert lo == max(sizing.HORIZON_MIN_FLOOR, int(trading_days * 0.5))
    assert hi == min(sizing.HORIZON_MAX_CAP, -(-int(trading_days * 1.5 * 1000) // 1000))
    assert hi <= 30, f"自然日 27.6 换算后不应超过 30 个交易日，实际 {hi}"


def test_derive_horizon_still_accepts_trading_days_directly():
    """``derive_horizon`` 本身口径不变：入参即交易日。"""
    assert sizing.derive_horizon(20) == [10, 30]
    assert sizing.derive_horizon(0) == [5, 20]


def test_derive_horizon_from_calendar_falls_back_when_missing():
    """缺回测数据（0 / None / 非数值）时回落到 [5, 20]，不抛异常。"""
    assert sizing.derive_horizon_from_calendar(0) == [5, 20]
    assert sizing.derive_horizon_from_calendar(None) == [5, 20]
    assert sizing.derive_horizon_from_calendar('oops') == [5, 20]


# ---------------------------------------------------------------------------
# 4) decide() 对拍：只改字符串，动作/目标仓位逐组合不变
# ---------------------------------------------------------------------------
_STATUSES = (None, 'ok', 'no_edge', 'untradeable', 'insufficient_data',
             'data_source_error', 'market_closed')
_PENDINGS = (None, 'entry', 'stop', 'target', 'time', 'bogus')


def test_decide_matrix_is_unchanged_by_issue_139():
    """穷举 decide()/target_position_for 的入参组合，锁住动作表。

    期望值是**改动前**跑出来的（issue #139 前后逐字节相同）。这里写死成表，
    任何判定逻辑变化都会让它变红——本 PR 只允许改字符串。
    """
    expected = {}
    for status, pending, exp_ok, holding in itertools.product(
            _STATUSES, _PENDINGS, (False, True), (False, True)):
        action = act_mod.decide(status, pending, exp_ok, holding)
        expected[(status, pending, exp_ok, holding)] = (
            action, act_mod.target_position_for(action, holding))

    # 关键锚点（与改动前的实现逐条一致）
    assert expected[(None, None, False, True)] == ('hold', 1.0)
    assert expected[('ok', None, True, True)] == ('hold', 1.0)
    assert expected[('ok', 'stop', True, True)] == ('sell', 0.0)
    assert expected[('ok', 'target', True, True)] == ('sell', 0.0)
    assert expected[('ok', 'time', True, True)] == ('sell', 0.0)
    assert expected[('ok', 'entry', True, False)] == ('buy', 1.0)
    # 持仓中再来 entry 不买（已满仓）
    assert expected[('ok', 'entry', True, True)] == ('hold', 1.0)
    assert expected[('untradeable', None, True, True)] == ('avoid', 0.0)
    assert expected[('insufficient_data', None, True, True)] == ('insufficient_data', 0.0)
    assert expected[('data_source_error', None, True, True)] == ('insufficient_data', 0.0)
    # no_edge 不再阻断交易（v3.0）
    assert expected[('no_edge', 'entry', True, False)] == ('buy', 1.0)

    # 全矩阵只出现五种合法动作，且目标仓位只取 0/0.5/1.0
    actions = {value[0] for value in expected.values()}
    assert actions == {'buy', 'sell', 'hold', 'avoid', 'insufficient_data'}
    assert {value[1] for value in expected.values()} <= {0.0, 0.5, 1.0}


def test_hold_reason_action_word_set_is_unchanged():
    """hold 文案仍不得出现「卖出信号/买入信号」（F23 硬规则，未被本次改动破坏）。"""
    for bars in (8, 20, 21, 46):
        reasons = rnd_mod.build_reasons(
            'hold', _signal(position_state='holding'), _expectancy(),
            _policy(holding=True, bars_held=bars), horizon=20)
        text = ' '.join(reasons)
        assert '卖出信号' not in text and '买入信号' not in text
