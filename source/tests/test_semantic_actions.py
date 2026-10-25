# -*- coding: utf-8 -*-
"""v3.0 决策层回归：条件期望、策略引擎、动作与文案。

对应 2026-10-25 的架构复审（`ARCHITECTURE_REVIEW.md`）：
  * 买点后 20 日 +1.72%（13/14 只为正）→ 买入看条件期望；
  * 卖点后 20 日 +1.04%（仅 5/14 只为负）→ 趋势卖出不成立，卖出只做风控；
  * 全样本 alpha 显著性不再门控单笔动作（那是"策略值不值得信"的检验）。
"""
import numpy as np
import pandas as pd
import pytest

from decision import actions as act_mod
from decision import expectancy as exp_mod
from decision import policy as pol_mod
from decision import render as rnd_mod
from decision import signals as sig_mod
from stock_analysis.strategy.base import TrendStrategy


def _df(n=120, start=10.0, step=0.0, vol=0.0):
    idx = pd.date_range('2024-01-01', periods=n, freq='B')
    close = start + np.arange(n, dtype=float) * step
    return pd.DataFrame({
        'open': close, 'high': close * (1 + vol), 'low': close * (1 - vol),
        'close': close, 'volume': np.full(n, 1e6),
    }, index=idx)


def _flat(index):
    return pd.Series(False, index=index)


# ---------------------------------------------------------------------------
# 决策表：买入看期望，卖出看风控
# ---------------------------------------------------------------------------
def test_buy_requires_expectancy_not_significance():
    assert act_mod.decide('ok', 'entry', True, False) == 'buy'
    # no_edge 不再阻断交易：它只描述"策略全样本 alpha 不显著"
    assert act_mod.decide('no_edge', 'entry', True, False) == 'buy'
    # 期望不达标 → 不买
    assert act_mod.decide('ok', 'entry', False, False) == 'hold'
    # 已持仓时不再重复买
    assert act_mod.decide('ok', 'entry', True, True) == 'hold'


@pytest.mark.parametrize('pending', ['stop', 'target', 'time'])
def test_sell_only_from_risk_events(pending):
    assert act_mod.decide('ok', pending, True, True) == 'sell'


def test_trend_sell_does_not_produce_sell():
    """趋势卖点（state=sell_triggered）不再产生 sell 动作。"""
    assert act_mod.decide('ok', None, True, True) == 'hold'


def test_status_overrides():
    assert act_mod.decide('untradeable', 'entry', True, False) == 'avoid'
    assert act_mod.decide('insufficient_data', 'entry', True, False) == 'insufficient_data'


def test_target_position():
    assert act_mod.target_position_for('buy', False) == 1.0
    assert act_mod.target_position_for('sell', True) == 0.0
    assert act_mod.target_position_for('hold', True) == 1.0
    assert act_mod.target_position_for('hold', False) == 0.0
    assert act_mod.target_position_for('avoid', False) == 0.0


# ---------------------------------------------------------------------------
# 条件期望
# ---------------------------------------------------------------------------
def test_expectancy_detects_positive_edge():
    # 带噪声的上涨序列：有漂移也有方差，避免"零方差导致 t 值无定义"的合成假象
    rng = np.random.default_rng(7)
    rets = rng.normal(0.004, 0.012, 400)
    close = 10.0 * np.exp(np.cumsum(rets))
    idx = pd.date_range('2024-01-01', periods=400, freq='B')
    df = pd.DataFrame({'open': close, 'high': close, 'low': close,
                       'close': close, 'volume': 1e6}, index=idx)
    buy = _flat(df.index)
    for i in range(10, 340, 10):
        buy.iloc[i] = True
    e = exp_mod.side_expectancy(df['close'], buy, horizon=20, side='buy')
    assert e['n'] >= 10
    assert e['mean'] > 0 and e['ok'] is True


def test_expectancy_rejects_short_sample():
    df = _df(80)
    buy = _flat(df.index)
    buy.iloc[10] = True
    e = exp_mod.side_expectancy(df['close'], buy, horizon=20, side='buy')
    assert e['n'] < exp_mod.MIN_SAMPLES
    assert e['ok'] is False


def test_trailing_expectancy_has_no_lookahead():
    """第 t 根只能用 j+horizon<=t 的已实现收益，预热期内必须为 False。"""
    df = _df(400, step=0.02)
    buy = _flat(df.index)
    buy.iloc[::5] = True
    ok = exp_mod.trailing_expectancy_ok(df['close'], buy, horizon=20, warmup=250)
    assert len(ok) == len(df)
    assert not ok[:250].any(), "预热期内不得声称期望可用"


# ---------------------------------------------------------------------------
# 策略引擎
# ---------------------------------------------------------------------------
def _policy(df, buy, ok_all=True, **kw):
    ok = np.full(len(df), ok_all, dtype=bool)
    return pol_mod.run_policy(df, buy, ok, horizon=20, **kw)


def test_policy_enters_on_buy_and_exits_on_stop():
    df = _df(120, start=10.0)
    # 入场后砸盘触发止损
    df.loc[df.index[40]:, 'low'] = 8.0
    df.loc[df.index[40]:, 'close'] = 9.0
    buy = _flat(df.index)
    buy.iloc[30] = True
    p = _policy(df, buy)
    assert p['trade_count'] >= 1
    assert p['exit_reasons'].get('stop', 0) >= 1


def test_policy_no_entry_without_expectancy():
    df = _df(120)
    buy = _flat(df.index)
    buy.iloc[30] = True
    p = _policy(df, buy, ok_all=False)
    assert p['trade_count'] == 0
    assert p['holding'] is False


def test_policy_time_stop():
    df = _df(160, start=10.0, step=-0.01)   # 缓慢下跌
    buy = _flat(df.index)
    buy.iloc[60] = True
    p = _policy(df, buy)
    assert p['trade_count'] >= 1
    assert sum(p['exit_reasons'].values()) >= 1


def test_policy_public_view_has_only_json_keys():
    df = _df(120)
    p = _policy(df, _flat(df.index))
    view = pol_mod.public_view(p)
    assert set(view) == set(pol_mod.POLICY_KEYS)
    assert 'position_series' not in view


# ---------------------------------------------------------------------------
# 信号块与文案
# ---------------------------------------------------------------------------
def test_signal_keys_are_stable():
    df = _df()
    buy, sell = _flat(df.index), _flat(df.index)
    block = sig_mod.build(df, buy, sell, strategy='X')
    assert set(block) == set(sig_mod.SIGNAL_KEYS)
    assert set(sig_mod.null_signal()) == set(sig_mod.SIGNAL_KEYS)


def test_flat_never_reports_near_sell():
    df = _df()
    buy, sell = _flat(df.index), _flat(df.index)
    sell.iloc[100] = True
    ind = {'current_value': 1.0, 'buy_threshold': 1.0,
           'sell_threshold': 1.0, 'scale': 1.0}
    block = sig_mod.build(df, buy, sell, strategy='X', indicator=ind)
    assert block['position_state'] == 'flat'
    assert block['state'] not in ('near_sell', 'sell_triggered')


def _exp(mean=0.0287, n=231, hit=0.593, t=4.4):
    return {'side': 'buy', 'horizon': 20, 'mean': mean, 'hit_rate': hit,
            'n': n, 't_stat': t, 'ok': True}


def test_hold_reason_has_no_signal_words():
    df = _df()
    block = sig_mod.build(df, _flat(df.index), _flat(df.index), strategy='X')
    text = ' '.join(rnd_mod.build_reasons('hold', block, _exp(), {'holding': False},
                                          review_date='2026-11-20', horizon=20))
    assert '卖出信号' not in text and '买入信号' not in text
    assert '空仓' in text


def test_sell_reason_names_risk_type():
    df = _df()
    block = sig_mod.build(df, _flat(df.index), _flat(df.index), strategy='X')
    pol = {'holding': True, 'pending': 'stop', 'bars_held': 3,
           'entry_price': 10.0, 'entry_date': '2026-10-01',
           'stop_price': 9.5, 'target_price': 11.0}
    text = ' '.join(rnd_mod.build_reasons('sell', block, _exp(), pol,
                                          review_date='2026-11-20', horizon=20))
    assert '止损' in text
    assert '不代表趋势判断' in text


def test_buy_reason_carries_expectancy_numbers():
    df = _df()
    block = sig_mod.build(df, _flat(df.index), _flat(df.index), strategy='X')
    pol = {'holding': False, 'pending': 'entry', 'stop_price': 9.5, 'target_price': 11.0}
    text = ' '.join(rnd_mod.build_reasons('buy', block, _exp(), pol,
                                          review_date='2026-11-20', horizon=20))
    assert '+2.87%' in text and 'n=231' in text and '2026-11-20' in text


def test_indicator_value_is_not_close_price():
    df = _df(step=0.05)
    ind = sig_mod.describe_indicator(TrendStrategy(3, 30, 60), '均线趋势', df)
    assert ind['current_value'] is not None
    assert abs(ind['current_value'] - float(df['close'].iloc[-1])) > 1e-6
