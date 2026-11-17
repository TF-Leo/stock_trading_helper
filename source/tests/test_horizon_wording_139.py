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
3. horizon 口径统一：``invalidation`` 的 N 与 ``horizon_days`` / ``policy`` 的
   时间止损阈值同源（``cli.HORIZON``），不再用回测平均持仓派生的窗口。换算工具
   ``calendar_days_to_trading_days`` / ``derive_horizon_from_calendar`` 保留，但
   仅作自然日→交易日的展示换算，**不参与**时间止损阈值（issue #139 P1）。

另外对拍 ``decide()``：本次只改字符串，动作/目标仓位必须逐组合不变。

测试标的固定为 ``600519`` / ``600000``，不使用任何真实持仓代码。
"""
from __future__ import annotations

import hashlib
import itertools
import re

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
# 2b) P1：持仓态 invalidation 的 N 必须与 policy 时间止损同源（cli.HORIZON）
# ---------------------------------------------------------------------------
def _toy_df(n=150, seed=7, drift=0.001, vol=0.02, start=100.0):
    import numpy as np
    import pandas as pd

    rng = np.random.default_rng(seed)
    close = start * np.cumprod(1 + rng.normal(drift, vol, n))
    idx = pd.date_range("2020-01-01", periods=n, freq="B")
    return pd.DataFrame({
        "open": close * 0.999, "high": close * (1 + vol),
        "low": close * (1 - vol), "close": close, "volume": 1e6,
    }, index=idx)


def _advise_offline(monkeypatch, avg_holding_days, holding, bars_held=8):
    """离线驱动 ``cli.advise_internal``，返回 (响应, policy 收到的 horizon)。"""
    import pandas as pd

    import cli
    import stock_analysis.data_loader.meta as meta
    from stock_analysis.backtest.engine import BacktestResult
    from stock_analysis.data_loader.flow import empty_flow_block
    from stock_analysis.data_loader.status import TradingStatus
    from stock_analysis.data_loader.valuation import empty_valuation_block

    df = _toy_df()
    monkeypatch.setattr(meta, "resolve_and_fetch", lambda *a, **k: (df, "stock", None))
    monkeypatch.setattr(cli, "last_fetch_info", lambda: {
        "source": "cache", "from_cache": True, "degraded": False,
        "cache_last_date": "2026-11-13", "lag_days": 0,
        "expected_date": "2026-11-13", "requested_source": "cache",
        "source_attempts": [],
    })
    monkeypatch.setattr(cli, "get_stock_name", lambda code: "示例银行")
    monkeypatch.setattr(cli, "resolve_trading_status", lambda *a, **k: TradingStatus(
        is_st=False, is_suspended=False, limit_up=13.2, limit_down=10.8,
        quote_stale=False, status_source="eastmoney_st_list+tencent_quote"))
    monkeypatch.setattr(cli, "evidence_context", lambda *a, **k: (
        empty_flow_block(code="600519"),
        empty_valuation_block(code="600519", reason="cache_missing"),
        [],
    ))

    names = list(cli.build_strategy_signals().keys())[:3]
    comparison = pd.DataFrame(
        [{"评分": 1.0 - i * 0.1, "评分IS": 1.0 - i * 0.1,
          "评分OOS": 0.9 - i * 0.1, "年化收益": "10.00%",
          "最大回撤": "5.00%", "胜率": "60.00%", "交易次数": 20}
         for i, _ in enumerate(names)],
        index=names,
    )
    result = BacktestResult(trade_count=20, win_rate=0.6, annual_return=0.1,
                            max_drawdown=0.05, unaffordable_lot_events=0)
    result.equity_curve = [100000.0] * 50
    result.avg_holding_days = avg_holding_days
    monkeypatch.setattr(cli, "run_strategy_tournament",
                        lambda *a, **k: (names[0], result, comparison))

    # 记录 policy 实际收到的 horizon，并把仓位状态固定成用例要的形态。
    real_run_policy = cli.pol_mod.run_policy
    seen = {}

    def _spy(df_, buy, exp_ok, horizon=20, **kwargs):
        seen["horizon"] = horizon
        out = real_run_policy(df_, buy, exp_ok, horizon=horizon, **kwargs)
        out["holding"] = bool(holding)
        out["pending"] = None
        if holding:
            out.update(bars_held=bars_held, entry_price=1450.0,
                       entry_date="2026-11-02", stop_price=1400.0,
                       target_price=1600.0)
        return out

    monkeypatch.setattr(cli.pol_mod, "run_policy", _spy)
    doc = cli.advise_internal("600519", fast=True, no_regime=True)
    return doc, seen


def _invalidation_n(text: str) -> int:
    match = re.search(r"(\d+) 个交易日", text)
    assert match, f"invalidation 没有「N 个交易日」: {text!r}"
    return int(match.group(1))


@pytest.mark.parametrize("holding", [False, True])
@pytest.mark.parametrize("avg_holding_days", [0.0, 15.694215, 27.646617, 140.0])
def test_invalidation_n_matches_horizon_days(monkeypatch, holding, avg_holding_days):
    """两态、多组 ``avg_holding_days``：N 与 ``horizon_days`` / policy 阈值一致。

    15.69 / 27.65 / 140 自然日分别会派生出 17 / 30 / 60 个交易日；修复后
    ``invalidation`` 与 ``horizon_days``、``run_policy(horizon=...)`` 三者同源，
    一律是 ``cli.HORIZON``。空仓/持仓只影响文案分句，不影响 N。
    """
    import cli

    doc, seen = _advise_offline(monkeypatch, avg_holding_days, holding=holding)
    rec = doc["recommendation"]
    n = _invalidation_n(rec["invalidation"])
    assert rec["horizon_days"] == [cli.HORIZON, cli.HORIZON]
    assert n == rec["horizon_days"][0] == cli.HORIZON
    assert seen["horizon"] == cli.HORIZON, (
        "invalidation 的 N 必须与 policy.run_policy 的时间止损阈值同源")
    if holding:
        assert "时间止损" in rec["invalidation"]
        assert "未触发入场" not in rec["invalidation"]
    else:
        assert "未触发入场" in rec["invalidation"]
        assert "时间止损" not in rec["invalidation"]


@pytest.mark.parametrize("holding", [False, True])
def test_invalidation_takes_real_threshold_not_derived(monkeypatch, holding):
    """派生值与真实阈值不同的场景：取的是后者。

    600519 ``--as-of 2019-11-20`` 实测 ``avg_holding_days=15.694215``（自然日），
    ``derive_horizon_from_calendar`` 派生出 17，而时间止损阈值是 20。修复前
    ``invalidation`` 写「满 17 个交易日」，与 ``horizon_days=[20,20]`` 同文矛盾。
    """
    import cli

    derived = sizing.derive_horizon_from_calendar(15.694215)
    assert derived[1] == 17 and derived[1] != cli.HORIZON

    doc, _ = _advise_offline(monkeypatch, 15.694215, holding=holding)
    invalidation = doc["recommendation"]["invalidation"]
    assert f"{cli.HORIZON} 个交易日" in invalidation
    assert f"{derived[1]} 个交易日" not in invalidation
    # 函数本身收显式阈值；调用方传的就是 20，而不是派生值。
    assert "20 个交易日" in sizing.build_invalidation(1430.0, "均线趋势", cli.HORIZON, holding)


# ---------------------------------------------------------------------------
# 4) decide() 对拍：只改字符串，动作/目标仓位逐组合不变
# ---------------------------------------------------------------------------
_STATUSES = (None, 'ok', 'no_edge', 'untradeable', 'insufficient_data',
             'data_source_error', 'market_closed')
_PENDINGS = (None, 'entry', 'stop', 'target', 'time', 'bogus')

#: 父提交 e4cd804 上算出的 168 组合摘要（actions.py 两版逐字节相同）。
_DECIDE_MATRIX_SHA256 = "979b04f7b1951fea8293037a428890a10e50b759b85de4c1c2a0d05669fa6b00"


def _decide_matrix():
    expected = {}
    for status, pending, exp_ok, holding in itertools.product(
            _STATUSES, _PENDINGS, (False, True), (False, True)):
        action = act_mod.decide(status, pending, exp_ok, holding)
        expected[(status, pending, exp_ok, holding)] = (
            action, act_mod.target_position_for(action, holding))
    return expected


def _decide_matrix_digest(expected) -> str:
    lines = []
    for key in itertools.product(_STATUSES, _PENDINGS, (False, True), (False, True)):
        action, target = expected[key]
        status, pending, exp_ok, holding = key
        lines.append(f"{status}|{pending}|{exp_ok}|{holding}|{action}|{target}")
    return hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()


def test_decide_matrix_is_unchanged_by_issue_139():
    """168 个入参组合的 (action, target_position) 与**改动前**逐字节相同。

    168 = 7 status × 6 pending × 2 expectancy_ok × 2 holding。下面的 SHA-256
    是在父提交 ``e4cd804`` 上算出来的真实冻结值（该提交与本分支的
    ``decision/actions.py`` 逐字节相同），不是现场自比的“冻结表”。
    """
    expected = _decide_matrix()
    assert len(expected) == 168
    assert _decide_matrix_digest(expected) == _DECIDE_MATRIX_SHA256

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
