# -*- coding: utf-8 -*-
"""F23/F24/F25/F26 语义回归：动作、状态、文案、指标值必须同源。

这些用例对应 2026-10-25 的实盘事故：
    002415 在 2026-10-22 输出 action=hold，文案却是"卖出信号 (32.42)"，
    而 32.42 是收盘价、死叉发生在 10-20、10-23 大涨当天仍在报卖出。
"""
import numpy as np
import pandas as pd
import pytest

from decision import actions as act_mod
from decision import render as rnd_mod
from decision import signals as sig_mod
from stock_analysis.strategy.base import TrendStrategy


def _df(n=120, start=10.0, step=0.05):
    idx = pd.date_range('2024-01-01', periods=n, freq='B')
    close = start + np.arange(n, dtype=float) * step
    return pd.DataFrame({
        'open': close, 'high': close * 1.01, 'low': close * 0.99,
        'close': close, 'volume': np.full(n, 1e6),
    }, index=idx)


def _flat_signals(index):
    return (pd.Series(False, index=index), pd.Series(False, index=index))


# ---------------------------------------------------------------------------
# 结构：信号块键集合必须稳定（G35 同构的前提）
# ---------------------------------------------------------------------------
def test_signal_keys_are_stable():
    df = _df()
    buy, sell = _flat_signals(df.index)
    block = sig_mod.build(df, buy, sell, strategy='X')
    assert set(block) == set(sig_mod.SIGNAL_KEYS)
    assert set(sig_mod.null_signal()) == set(sig_mod.SIGNAL_KEYS)


# ---------------------------------------------------------------------------
# F24：仓位状态机
# ---------------------------------------------------------------------------
def test_position_state_machine():
    buy = np.array([0, 1, 0, 0, 0, 0], dtype=bool)
    sell = np.array([0, 0, 0, 1, 0, 0], dtype=bool)
    pos = sig_mod.position_states(buy, sell)
    assert list(pos) == [0, 1, 1, 0, 0, 0]


def test_sell_wins_when_both_true_on_same_bar():
    buy = np.array([0, 1, 1], dtype=bool)
    sell = np.array([0, 0, 1], dtype=bool)
    pos = sig_mod.position_states(buy, sell)
    assert list(pos) == [0, 1, 0], "同根同时为真时必须按 sell 优先（风控优先）"


def test_flat_never_reports_near_sell():
    """核心回归：空仓时不得出现 near_sell。

    旧实现用"近 10 根内出现过卖点"判定 near_sell，导致 10-20 的死叉
    在之后 10 个交易日天天被报"卖出信号"。这里让指标**恰好等于卖出阈值**，
    如果仍按旧口径就会命中 near_sell。
    """
    df = _df()
    buy, sell = _flat_signals(df.index)
    sell.iloc[100] = True                     # 19 根之前的卖点
    ind = {'current_value': 1.0, 'buy_threshold': 1.0,
           'sell_threshold': 1.0, 'scale': 1.0}
    block = sig_mod.build(df, buy, sell, strategy='X', indicator=ind)
    assert block['position_state'] == 'flat'
    assert block['last_sell_date'] == str(df.index[100])[:10]
    assert block['last_sell_age'] == len(df) - 1 - 100
    assert block['state'] not in ('near_sell', 'sell_triggered')


# ---------------------------------------------------------------------------
# F26：决策表
# ---------------------------------------------------------------------------
def test_single_bar_buy_event_triggers_buy():
    """单根金叉必须能产生 buy —— 旧实现要求"连续 15 根全为真"，永不触发。"""
    df = _df()
    buy, sell = _flat_signals(df.index)
    buy.iloc[-1] = True
    block = sig_mod.build(df, buy, sell, strategy='X')
    assert block['state'] == 'buy_triggered'
    assert act_mod.decide('ok', block['state'], 'positive_significant', 0.5, True) == 'buy'


def test_single_bar_sell_event_triggers_sell():
    df = _df()
    buy, sell = _flat_signals(df.index)
    buy.iloc[-3] = True
    sell.iloc[-1] = True
    block = sig_mod.build(df, buy, sell, strategy='X')
    assert block['state'] == 'sell_triggered'
    assert act_mod.decide('ok', block['state'], 'inconclusive', 0.2, False) == 'sell'


def test_buy_requires_significance_and_positive_verdict():
    assert act_mod.decide('ok', 'buy_triggered', 'positive_significant', 0.5, False) == 'hold'
    assert act_mod.decide('ok', 'buy_triggered', 'inconclusive', 0.9, True) == 'hold'
    assert act_mod.decide('ok', 'buy_triggered', 'positive_significant', 0.1, True) == 'hold'


def test_status_overrides_state():
    assert act_mod.decide('no_edge', 'sell_triggered', 'inconclusive', 0.9, True) == 'avoid'
    assert act_mod.decide('untradeable', 'buy_triggered', 'positive_significant', 0.9, True) == 'avoid'
    assert act_mod.decide('insufficient_data', 'neutral', None, 0.0, False) == 'insufficient_data'


# ---------------------------------------------------------------------------
# F23：文案
# ---------------------------------------------------------------------------
def _block(**kw):
    base = sig_mod.null_signal()
    base.update({'strategy': 'X', 'as_of': '2026-10-22', 'position_state': 'flat',
                 'state': 'neutral', 'last_sell_date': '2026-10-20', 'last_sell_age': 2})
    base.update(kw)
    return base


def test_hold_reason_never_says_signal_words():
    reason = rnd_mod.build_reasons('hold', _block(), {'verdict': 'inconclusive'})
    text = ' '.join(reason)
    assert '卖出信号' not in text and '买入信号' not in text
    assert '2026-10-20' in text and '2 个交易日' in text


def test_sell_reason_mentions_today_and_position_change():
    text = ' '.join(rnd_mod.build_reasons('sell', _block(), {'verdict': 'inconclusive'}))
    assert '2026-10-22' in text and '由持有转为空仓' in text


def test_buy_reason_carries_verdict():
    text = ' '.join(rnd_mod.build_reasons('buy', _block(), {'verdict': 'positive_significant'}))
    assert '2026-10-22' in text and 'positive_significant' in text


# ---------------------------------------------------------------------------
# F25：指标值不得是收盘价
# ---------------------------------------------------------------------------
def test_indicator_value_is_not_close_price():
    df = _df()
    ind = sig_mod.describe_indicator(TrendStrategy(3, 30, 60), '均线趋势', df)
    assert ind['current_value'] is not None
    assert abs(ind['current_value'] - float(df['close'].iloc[-1])) > 1e-6
    assert ind['buy_threshold'] == 1.0 and ind['sell_threshold'] == 1.0
    assert 'MA' in ind['indicator']


def test_unknown_family_returns_null_not_price():
    df = _df()
    ind = sig_mod.describe_indicator(object(), '不存在的族', df)
    assert ind['current_value'] is None
    block = sig_mod.build(df, *_flat_signals(df.index), strategy='X', indicator=ind)
    assert block['current_value'] is None
    assert block['last_close'] is not None
