import pandas as pd
import numpy as np
from datetime import datetime
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple, Callable
from enum import Enum


class OrderType(Enum):
    BUY = "buy"
    SELL = "sell"


@dataclass
class Trade:
    entry_date: datetime
    exit_date: datetime
    entry_price: float
    exit_price: float
    shares: int
    order_type: OrderType
    pnl: float = 0
    pnl_pct: float = 0
    holding_days: int = 0
    entry_reason: str = ""
    exit_reason: str = ""


@dataclass
class BacktestConfig:
    initial_capital: float = 100000.0
    commission_rate: float = 0.0003
    stamp_duty: float = 0.001
    slippage_rate: float = 0.001
    min_commission: float = 5.0
    max_position_pct: float = 0.3
    stop_loss_pct: float = 0.08
    take_profit_pct: float = 0.15
    enable_stop_loss: bool = True
    enable_take_profit: bool = True


@dataclass
class BacktestResult:
    trades: List[Trade] = field(default_factory=list)
    total_return: float = 0
    annual_return: float = 0
    max_drawdown: float = 0
    sharpe_ratio: float = 0
    win_rate: float = 0
    profit_factor: float = 0
    avg_win: float = 0
    avg_loss: float = 0
    max_consecutive_wins: int = 0
    max_consecutive_losses: int = 0
    avg_holding_days: float = 0
    trade_count: int = 0
    bull_win_rate: float = 0
    bear_win_rate: float = 0
    shock_win_rate: float = 0
    calmar_ratio: float = 0
    sortino_ratio: float = 0
    var_95: float = 0

    def to_dict(self):
        return {
            "总收益率": f"{self.total_return:.2%}",
            "年化收益": f"{self.annual_return:.2%}",
            "最大回撤": f"{self.max_drawdown:.2%}",
            "夏普比率": f"{self.sharpe_ratio:.2f}",
            "胜率": f"{self.win_rate:.2%}",
            "盈亏比": f"{self.profit_factor:.2f}",
            "交易次数": self.trade_count,
            "平均盈利": f"{self.avg_win:.2%}",
            "平均亏损": f"{self.avg_loss:.2%}",
            "最大连赢": self.max_consecutive_wins,
            "最大连亏": self.max_consecutive_losses,
            "平均持仓天数": f"{self.avg_holding_days:.1f}",
            "牛市胜率": f"{self.bull_win_rate:.2%}",
            "熊市胜率": f"{self.bear_win_rate:.2%}",
            "震荡胜率": f"{self.shock_win_rate:.2%}",
        }


class BacktestEngine:

    def __init__(self, config: BacktestConfig = None):
        self.config = config or BacktestConfig()

    def run_backtest(
        self,
        df: pd.DataFrame,
        signal_generator: Callable,
        market_regimes: pd.Series = None,
    ) -> BacktestResult:
        df = df.copy()
        signals = signal_generator(df)
        # P0: 消除同收盘偏差 — 信号T日收盘算出，T+1日才能执行
        # shift(1)保证不用当日close同时算信号又成交
        raw_buy = signals['buy'].reindex(df.index).fillna(False).astype(bool)
        raw_sell = signals['sell'].reindex(df.index).fillna(False).astype(bool)
        df['buy_signal'] = raw_buy.shift(1).fillna(False).astype(bool)
        df['sell_signal'] = raw_sell.shift(1).fillna(False).astype(bool)

        cash = self.config.initial_capital
        position = 0
        entry_price = 0
        entry_date = None
        entry_reason = ""
        entry_cost_total = 0.0  # 买入总成本（含买入佣金），用于准确计算pnl

        result = BacktestResult()
        equity_curve = []

        prev_close = float(df['close'].iloc[0]) if len(df) else 0.0
        for i in range(len(df)):
            row = df.iloc[i]
            date = df.index[i]
            high = row['high']
            low = row['low']
            close = row['close']
            open_px = row['open'] if 'open' in row else close
            # A股涨跌停：涨停买不进、跌停卖不出（默认10%）
            is_limit_up = prev_close > 0 and close >= prev_close * 1.099
            is_limit_down = prev_close > 0 and close <= prev_close * 0.901

            if position > 0 and not is_limit_down:
                if self.config.enable_stop_loss:
                    if low <= entry_price * (1 - self.config.stop_loss_pct):
                        exit_price = entry_price * (1 - self.config.stop_loss_pct)
                        exit_price = self._apply_slippage(exit_price, OrderType.SELL)
                        trade = self._execute_sell(
                            date, exit_price, position, cash,
                            entry_date, entry_price, entry_cost_total,
                            entry_reason, "止损",
                        )
                        result.trades.append(trade)
                        cash += self._apply_commission(exit_price * position, OrderType.SELL)
                        position = 0
                        entry_cost_total = 0.0
                        equity_curve.append(cash)
                        prev_close = float(close)
                        continue

                if self.config.enable_take_profit:
                    if high >= entry_price * (1 + self.config.take_profit_pct):
                        exit_price = entry_price * (1 + self.config.take_profit_pct)
                        exit_price = self._apply_slippage(exit_price, OrderType.SELL)
                        trade = self._execute_sell(
                            date, exit_price, position, cash,
                            entry_date, entry_price, entry_cost_total,
                            entry_reason, "止盈",
                        )
                        result.trades.append(trade)
                        cash += self._apply_commission(exit_price * position, OrderType.SELL)
                        position = 0
                        entry_cost_total = 0.0
                        equity_curve.append(cash)
                        prev_close = float(close)
                        continue

            if position > 0 and row['sell_signal'] and not is_limit_down:
                exit_price = self._apply_slippage(close, OrderType.SELL)
                trade = self._execute_sell(
                    date, exit_price, position, cash,
                    entry_date, entry_price, entry_cost_total,
                    entry_reason, "信号卖出",
                )
                result.trades.append(trade)
                cash += self._apply_commission(exit_price * position, OrderType.SELL)
                position = 0
                entry_cost_total = 0.0

            elif position == 0 and row['buy_signal'] and not is_limit_up:
                max_shares = self._calc_max_shares(cash, close)
                if max_shares > 0:
                    entry_price = self._apply_slippage(close, OrderType.BUY)
                    cost = entry_price * max_shares
                    commission = self._calc_commission(cost, OrderType.BUY)
                    if cost + commission <= cash:
                        position = max_shares
                        cash -= cost + commission
                        entry_cost_total = cost + commission
                        entry_date = date
                        entry_reason = "信号买入"

            equity_curve.append(cash + position * close)
            prev_close = float(close)

        if position > 0:
            final_price = self._apply_slippage(df['close'].iloc[-1], OrderType.SELL)
            trade = self._execute_sell(
                df.index[-1], final_price, position, cash,
                entry_date, entry_price, entry_cost_total,
                entry_reason, "强制平仓",
            )
            result.trades.append(trade)
            cash += self._apply_commission(final_price * position, OrderType.SELL)
            position = 0
            equity_curve.append(cash)

        from stock_analysis.backtest.analyzer import _calculate_metrics
        _calculate_metrics(result, equity_curve, market_regimes, df)

        return result

    def _apply_slippage(self, price: float, order_type: OrderType) -> float:
        if order_type == OrderType.BUY:
            return price * (1 + self.config.slippage_rate)
        else:
            return price * (1 - self.config.slippage_rate)

    def _calc_commission(self, amount: float, order_type: OrderType) -> float:
        commission = max(amount * self.config.commission_rate, self.config.min_commission)
        if order_type == OrderType.SELL:
            commission += amount * self.config.stamp_duty
        return commission

    def _apply_commission(self, amount: float, order_type: OrderType) -> float:
        return amount - self._calc_commission(amount, order_type)

    def _calc_max_shares(self, cash: float, price: float) -> int:
        # 预留滑点和手续费空间，避免低价股全仓时 cost+commission 超出现金
        # P0: A股100股整手取整
        effective_price = price * (1 + self.config.slippage_rate)
        max_position = cash * self.config.max_position_pct
        reserve = max(max_position * self.config.commission_rate, self.config.min_commission)
        affordable = max_position - reserve
        if affordable <= 0 or effective_price <= 0:
            return 0
        raw = int(affordable / effective_price)
        lots = (raw // 100) * 100  # 整手
        return lots

    def _execute_sell(
        self, date, price, shares, cash,
        entry_date, entry_price, entry_cost_total=None,
        entry_reason="", exit_reason="",
    ) -> Trade:
        # 兼容旧8参调用：_execute_sell(date,price,shares,cash,entry_date,entry_price,entry_reason,exit_reason)
        if isinstance(entry_cost_total, str):
            exit_reason = entry_reason
            entry_reason = entry_cost_total
            entry_cost_total = entry_price * shares
        sell_amount = price * shares
        net_amount = self._apply_commission(sell_amount, OrderType.SELL)
        # P0: pnl需扣除买入佣金
        cost_base = float(entry_cost_total) if entry_cost_total else entry_price * shares
        if cost_base <= 0:
            cost_base = entry_price * shares
        pnl = net_amount - cost_base
        pnl_pct = pnl / cost_base if cost_base else 0.0
        holding_days = int((date - entry_date).days) if entry_date is not None else 0
        return Trade(
            entry_date=entry_date,
            exit_date=date,
            entry_price=entry_price,
            exit_price=price,
            shares=shares,
            order_type=OrderType.SELL,
            pnl=pnl,
            pnl_pct=pnl_pct,
            holding_days=holding_days,
            entry_reason=entry_reason,
            exit_reason=exit_reason,
        )
