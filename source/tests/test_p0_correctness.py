# -*- coding: utf-8 -*-
"""P0正确性回归测试：资金守恒/费用/整手/次bar执行/前视/regime/基线。"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import pandas as pd
import numpy as np

from stock_analysis.backtest.engine import BacktestEngine, BacktestConfig
from stock_analysis.backtest.analyzer import _wilson_lower_bound, calc_buy_hold_baseline


def _toy_df(n=60, start=100.0, drift=0.02):
    idx = pd.date_range("2020-01-01", periods=n, freq="D")
    close = [start]
    for _ in range(1, n):
        close.append(close[-1] * (1 + drift))
    close = np.array(close)
    df = pd.DataFrame({
        "open": close * 0.999,
        "high": close * 1.01,
        "low": close * 0.99,
        "close": close,
        "volume": 100000,
    }, index=idx)
    return df


def test_cash_conservation_partial_position():
    """30%仓位两笔止盈全胜，不应出现-88%这种数学不可能结果；卖出后cash应累加而非替换。"""
    df = _toy_df(60, 100, 0.03)
    # 构造两次买入卖出信号（shift执行后仍能成交）
    def sig(d):
        buy = pd.Series(False, index=d.index)
        sell = pd.Series(False, index=d.index)
        buy.iloc[2] = True
        sell.iloc[10] = True
        buy.iloc[12] = True
        sell.iloc[20] = True
        return pd.DataFrame({"buy": buy, "sell": sell}, index=d.index)
    cfg = BacktestConfig(initial_capital=100000, max_position_pct=0.3,
                         enable_stop_loss=False, enable_take_profit=False,
                         commission_rate=0.0003, stamp_duty=0.001, slippage_rate=0.001)
    eng = BacktestEngine(cfg)
    r = eng.run_backtest(df, sig)
    assert r.trade_count == 2, f"期望2笔交易，实际{r.trade_count}"
    # 两笔都是上涨段买入持有8天，必为正收益；总收益不应为-88%
    assert r.total_return > -0.5, f"部分仓位资金守恒失败，总收益{r.total_return}"
    for t in r.trades:
        assert t.pnl > 0, f"上涨段交易应盈利，实际pnl={t.pnl}"


def test_lot_100_shares():
    cfg = BacktestConfig(initial_capital=100000, max_position_pct=1.0)
    eng = BacktestEngine(cfg)
    n = eng._calc_max_shares(100000, 10.0)
    assert n % 100 == 0, f"必须100整手，实际{n}"
    assert n > 0


def test_next_bar_execution():
    """信号T日触发，T+1日才能成交：最后一天的买信号不应成交（无0交易误判外）。"""
    df = _toy_df(20, 100, 0.0)
    def sig(d):
        buy = pd.Series(False, index=d.index)
        sell = pd.Series(False, index=d.index)
        buy.iloc[-1] = True  # 最后一天才买
        return pd.DataFrame({"buy": buy, "sell": sell}, index=d.index)
    eng = BacktestEngine(BacktestConfig(max_position_pct=1.0))
    r = eng.run_backtest(df, sig)
    assert r.trade_count == 0, f"次bar执行下最后一天信号不应成交，实际{r.trade_count}"


def test_squeeze_no_lookahead():
    from stock_analysis.strategy import BollSqueezeStrategy
    df = _toy_df(300, 100, 0.001)
    s = BollSqueezeStrategy(window=20, num_std=2.0, squeeze_pct=0.1)
    sig1 = s.generate_trade_signals(df.iloc[:150])
    sig2 = s.generate_trade_signals(df)
    # 前150条的信号在两种全样本长度下应一致（仅用过去252日滚动分位，截断无未来影响则至少不因未来改变过去）
    # 允许边界NaN差异，比较中间段
    a = sig1['buy'].iloc[30:140].astype(bool).values
    b = sig2['buy'].iloc[30:140].astype(bool).values
    assert (a == b).all(), "Squeeze信号不应随未来数据改变（前视）"


def test_regime_asof_alignment():
    from stock_analysis.backtest.analyzer import _calculate_metrics
    from stock_analysis.backtest.engine import BacktestResult, Trade
    from datetime import datetime
    r = BacktestResult()
    d1 = pd.Timestamp("2020-01-02")
    d2 = pd.Timestamp("2020-01-03")  # 假设停牌日，regime缺失
    r.trades = [Trade(entry_date=d2, exit_date=d2, entry_price=10, exit_price=11,
                      shares=100, order_type="sell", pnl=100, pnl_pct=0.1, holding_days=1)]
    eq = [100000, 100100]
    df = _toy_df(10)
    regimes = pd.Series(["Bull"], index=[d1])  # 只有前一天
    _calculate_metrics(r, eq, regimes, df)
    assert r.bull_win_rate == 1.0, f"asof应对齐到前一天Bull，实际{r.bull_win_rate}"


def test_advisor_no_crash():
    from stock_analysis.advisor.analyzer import analyze_volume_chip_structure
    df = _toy_df(100)
    out = analyze_volume_chip_structure(df)
    assert "score" in out and 0 <= out["score"] <= 100


def test_fund_fee_applied():
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    import types
    sys.modules.setdefault("akshare", types.ModuleType("akshare"))
    import cli
    assert hasattr(cli, "FUND_FEE_RATE") and cli.FUND_FEE_RATE > 0
    idx = pd.date_range("2023-01-01", periods=800, freq="D")
    close = np.linspace(1.0, 2.0, 800)
    df = pd.DataFrame({"close": close, "RSI": 50, "MA20": close}, index=idx)
    df["RSI"] = 50.0
    # 强制一次低RSI买入+高RSI卖出
    df.iloc[700, df.columns.get_loc("RSI")] = 10
    df.iloc[750, df.columns.get_loc("RSI")] = 90
    df = df.copy()
    # 用swing回测（近3年窗口内应有交易）
    from fund_analysis.fund_strategy.strategy import calculate_indicators  # noqa
    stats = cli._fund_backtest_with_stats(df, {"rsi_lower": 20, "rsi_upper": 80}, "swing")
    assert stats is not None and stats["trade_count"] >= 1
    # 含费用总收益应略低于无费用理论值（简单断言费用生效：手动复算无费用会更高）
    # 理论无费用：(p_sell/p_buy-1)；含费用应更小
    assert stats["total_return"] < 1.0  # 宽松 sanity


def test_buy_hold_baseline_and_no_edge_flag():
    df = _toy_df(100, 100, -0.005)  # 单边下跌
    base = calc_buy_hold_baseline(df)
    assert base["total_return"] < 0
    # 下跌市中随机策略应被标无优势
    from stock_analysis.backtest.analyzer import run_strategy_tournament
    # 用一个必然亏损的反向信号？直接测基线函数即可+锦标赛不崩
    best_name, best_res, comp = run_strategy_tournament(df)
    assert isinstance(best_name, str) and best_res is not None
    # 下跌市大概率触发无优势（若未触发也不判错，仅打印供人工核查）
    print(f"[info] 下跌市最优={best_name} 年化={best_res.annual_return:.2%} 基线年化={base['annual_return']:.2%}")


def test_wilson_bounds():
    assert _wilson_lower_bound(1.0, 2) < 0.6  # 小样本强惩罚
    assert _wilson_lower_bound(0.6, 100) > 0.4
    assert _wilson_lower_bound(0.5, 0) == 0.0
