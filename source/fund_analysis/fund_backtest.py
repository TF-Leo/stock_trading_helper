# -*- coding: utf-8 -*-
"""P2: 基金回测执行层（从cli.py拆出，原~130行基金信号模拟）。

cli.py保留同名re-export以兼容旧测试/调用。
"""
from __future__ import annotations
import pandas as pd

FUND_FEE_RATE = 0.0015  # P0: 基金申购+赎回近似费率，原0费用高估收益


def _fund_backtest_with_stats(
    df: pd.DataFrame, params: dict, strategy_type: str = "swing"
) -> dict | None:
    """基金单策略回测，统计胜率/总收益/最大回撤/交易次数。

    复用 fund_strategy.runner.backtest_strategy 的信号逻辑，额外记录每笔交易盈亏。
    P0: 已补双边申赎费用FUND_FEE_RATE。
    """
    test = df.copy()
    initial = 100000.0
    cash = initial
    position = 0.0
    start = test.index[-1] - pd.DateOffset(years=3)
    test = test[test.index >= start].copy()
    if test.empty:
        return None
    test["RSI_S"] = test["RSI"].shift(1) if "RSI" in test else 50
    test["MA20_S"] = test["MA20"].shift(1) if "MA20" in test else test["close"].shift(1)

    trades: list[float] = []
    values: list[float] = []
    buy_price = 0.0
    holding = False
    for i in range(len(test)):
        price = float(test["close"].iloc[i])
        rsi = test["RSI_S"].iloc[i]
        ma = test["MA20_S"].iloc[i]
        if pd.isna(rsi) or pd.isna(ma):
            values.append(cash + position * price)
            continue
        buy_signal = sell_signal = False
        if strategy_type == "swing":
            if rsi < params.get("rsi_lower", 30):
                buy_signal = True
            elif rsi > params.get("rsi_upper", 70):
                sell_signal = True
        else:  # hold
            prev_close = test["close"].iloc[i - 1] if i > 0 else price
            if prev_close > ma:
                buy_signal = True
        if buy_signal and not holding and cash > 0:
            position = cash * (1 - FUND_FEE_RATE) / price
            buy_price = price
            buy_cost = cash  # 含申购费的总成本
            cash = 0.0
            holding = True
        elif sell_signal and holding:
            cash = position * price * (1 - FUND_FEE_RATE)
            trades.append((cash - buy_cost) / buy_cost if 'buy_cost' in locals() and buy_cost else (price - buy_price) / buy_price)
            position = 0.0
            holding = False
        values.append(cash + position * price)
    if holding and values:
        last_price = float(test["close"].iloc[-1])
        _net = position * last_price * (1 - FUND_FEE_RATE)
        trades.append((_net - buy_cost) / buy_cost if 'buy_cost' in locals() and buy_cost else (last_price - buy_price) / buy_price)
    if not values:
        return None
    total_ret = (values[-1] - initial) / initial
    vs = pd.Series(values)
    max_dd = float(((vs - vs.cummax()) / vs.cummax()).min())
    wins = [t for t in trades if t > 0]
    win_rate = len(wins) / len(trades) if trades else 0.0
    return {
        "total_return": total_ret,
        "max_drawdown": max_dd,
        "win_rate": win_rate,
        "trade_count": len(trades),
    }


def _fund_backtest_signals(df: pd.DataFrame, signals: pd.DataFrame) -> dict | None:
    """基金信号驱动回测：根据 buy/sell 信号列模拟交易，统计胜率/收益/回撤。

    适配 P2 基金策略（双均线/MACD/波动率目标）的信号输出。
    P0: 已补双边申赎费用FUND_FEE_RATE。
    """
    test = df.copy()
    initial = 100000.0
    cash = initial
    position = 0.0
    start = test.index[-1] - pd.DateOffset(years=3)
    test = test[test.index >= start].copy()
    if test.empty:
        return None
    buy_sig = signals['buy'].reindex(test.index).fillna(False)
    sell_sig = signals['sell'].reindex(test.index).fillna(False)

    trades: list[float] = []
    values: list[float] = []
    buy_price = 0.0
    holding = False
    for i in range(len(test)):
        price = float(test['close'].iloc[i])
        if buy_sig.iloc[i] and not holding and cash > 0:
            position = cash * (1 - FUND_FEE_RATE) / price
            buy_price = price
            buy_cost = cash
            cash = 0.0
            holding = True
        elif sell_sig.iloc[i] and holding:
            cash = position * price * (1 - FUND_FEE_RATE)
            trades.append((cash - buy_cost) / buy_cost if 'buy_cost' in locals() and buy_cost else (price - buy_price) / buy_price)
            position = 0.0
            holding = False
        values.append(cash + position * price)
    if holding and values:
        last_price = float(test['close'].iloc[-1])
        _net = position * last_price * (1 - FUND_FEE_RATE)
        trades.append((_net - buy_cost) / buy_cost if 'buy_cost' in locals() and buy_cost else (last_price - buy_price) / buy_price)
    if not values:
        return None
    total_ret = (values[-1] - initial) / initial
    vs = pd.Series(values)
    max_dd = float(((vs - vs.cummax()) / vs.cummax()).min())
    wins = [t for t in trades if t > 0]
    win_rate = len(wins) / len(trades) if trades else 0.0
    return {
        "total_return": total_ret,
        "max_drawdown": max_dd,
        "win_rate": win_rate,
        "trade_count": len(trades),
    }
