import pandas as pd
from decision.instrument import classify_board, get_price_limits
import numpy as np
from datetime import datetime
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple, Callable
from enum import Enum


class OrderType(Enum):
    BUY = "buy"
    SELL = "sell"


# B2: ATR 内容指纹缓存（{指纹: pd.Series}）。只在本进程内有效，不参与序列化。
_ATR_CACHE = {}


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
    # B2: 波动率自适应止损止盈。默认 "atr"（契约要求默认启用），"fixed" 保留旧逻辑做回归保护。
    stop_mode: str = "atr"
    atr_period: int = 14
    atr_stop_mult: float = 2.5
    atr_profit_mult: float = 4.0
    # F29: 成交时点。信号在 T 日收盘算出，最早只能 T+1 日开盘成交（可执行时点）；
    # "next_close" 是旧行为（T+1 收盘成交，比真实可执行时点晚一天），保留用于回归对照。
    exec_price: str = "next_open"


@dataclass
class BacktestResult:
    trades: List[Trade] = field(default_factory=list)
    total_return: float = 0
    annual_return: float = 0
    max_drawdown: float = 0
    sharpe_ratio: float = 0
    equity_curve: List[float] = field(default_factory=list)  # B7: 权益曲线，供 bootstrap CI 使用
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
    unaffordable_lot_events: int = 0
    blocked_by_limit_events: int = 0
    # A4: 持仓 bar 数与占比（此前 holding_bar_count 从未被赋值，time_in_market 恒 0）
    holding_bar_count: int = 0
    time_in_market: float = 0.0

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

    def _atr_series(self, df: pd.DataFrame):
        """B2: 逐 bar ATR（带缓存，避免 581 组网格重复计算）。

        同一份 df 会被 581 个策略共用（compare_strategies 直接传同一个对象），
        因此把结果按"内容指纹"缓存即可把 581 次 calc_atr 降为 1 次。
        注意：不能把 Series 塞进 `df.attrs` —— pandas 的 `__finalize__` 会做
        `attrs == attrs` 比较，dict 里含 Series 会抛
        "The truth value of a Series is ambiguous"（实测踩过）。
        取不到 OHLC 或计算出错时返回 None（调用方回落固定百分比止损）。
        """
        if str(getattr(self.config, 'stop_mode', 'atr')).lower() != 'atr':
            return None
        if df is None or len(df) == 0:
            return None
        if not {'high', 'low', 'close'}.issubset(set(df.columns)):
            return None
        period = int(getattr(self.config, 'atr_period', 14) or 14)
        try:
            key = (len(df), float(df['close'].iloc[0]), float(df['close'].iloc[-1]),
                   round(float(df['high'].sum()), 4), round(float(df['low'].sum()), 4), period)
        except Exception:
            key = None
        if key is not None and key in _ATR_CACHE:
            return _ATR_CACHE[key]
        try:
            from stock_analysis.indicators.volatility import calc_atr
            series = calc_atr(df['high'], df['low'], df['close'], period=period)
        except Exception:
            series = None
        if key is not None:
            if len(_ATR_CACHE) > 64:
                _ATR_CACHE.clear()
            _ATR_CACHE[key] = series
        return series

    def run_backtest(
        self,
        df: pd.DataFrame,
        signal_generator: Callable,
        market_regimes: pd.Series = None,
    ) -> BacktestResult:
        # B2: 在 copy 之前取 ATR（同 df 对象共享缓存，581 组只算一次）
        atr_cached = self._atr_series(df)
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
        unaffordable_count = 0
        limit_blocked_count = 0
        equity_curve = []

        # 性能：逐 bar 取值从 Series 改为 numpy 数组。
        # 原实现每根 K 线都执行 `df.iloc[i]` 生成一个 Series，再做 6 次 Series 索引；
        # cProfile 显示 581 组网格下 pandas __getitem__ 被调用 100 万次、占总耗时 90%+。
        # 数组化后取值语义完全等价（同样是 float/bool），只是去掉逐行装箱。
        n_bars = len(df)
        dates = list(df.index)
        close_arr = df['close'].to_numpy(dtype=float)
        high_arr = df['high'].to_numpy(dtype=float)
        low_arr = df['low'].to_numpy(dtype=float)
        open_arr = (df['open'].to_numpy(dtype=float) if 'open' in df.columns
                    else close_arr)
        # F29: 成交价口径（见 BacktestConfig.exec_price）
        fill_open = (str(getattr(self.config, 'exec_price', 'next_open')) == 'next_open')
        buy_arr = df['buy_signal'].to_numpy(dtype=bool)
        sell_arr = df['sell_signal'].to_numpy(dtype=bool)
        atr_arr = None
        if atr_cached is not None:
            try:
                _a = atr_cached.to_numpy(dtype=float)
                if len(_a) == n_bars:
                    atr_arr = _a
            except Exception:
                atr_arr = None

        prev_close = float(close_arr[0]) if n_bars else 0.0
        # B2: 逐 bar 滚动历史 ATR（含当根 TR；本引擎为"收盘后决策、次日成交"，
        # 入场当根收盘时该值已知，不构成前视）。取不到 ATR 时该笔回落固定百分比。
        entry_stop_dist = None      # 入场时锁定的止损距离（比例）
        entry_profit_dist = None    # 入场时锁定的止盈距离（比例）
        for i in range(n_bars):
            date = dates[i]
            high = high_arr[i]
            low = low_arr[i]
            close = close_arr[i]
            open_px = open_arr[i]
            # F29: 成交价 —— 信号 T 日收盘算出，T+1 日**开盘**才能成交。
            # 旧实现用 T+1 收盘成交，比 Hermes 拿到建议后可执行的时点晚一整天。
            fill_px = (open_px if (fill_open and open_px == open_px and open_px > 0)
                       else close)
            # A股涨跌停：涨停买不进、跌停卖不出（按成交价判定）
            is_limit_up = prev_close > 0 and fill_px >= prev_close * 1.099
            is_limit_down = prev_close > 0 and fill_px <= prev_close * 0.901

            if position > 0 and not is_limit_down:
                if self.config.enable_stop_loss:
                    # A11: 止损成交价考虑跳空；B2: 止损距离优先用入场时已知的 ATR
                    if entry_stop_dist is None:
                        stop_dist = float(self.config.stop_loss_pct)
                    else:
                        stop_dist = float(entry_stop_dist)
                    stop_threshold = entry_price * (1 - stop_dist)
                    if low <= stop_threshold:
                        exit_price = min(stop_threshold, open_px)
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
                        entry_stop_dist = None
                        entry_profit_dist = None
                        equity_curve.append(cash)
                        prev_close = float(close)
                        continue

                if self.config.enable_take_profit:
                    # A11: 止盈成交价考虑跳空；B2: 止盈距离优先用入场时已知的 ATR
                    if entry_profit_dist is None:
                        profit_dist = float(self.config.take_profit_pct)
                    else:
                        profit_dist = float(entry_profit_dist)
                    profit_threshold = entry_price * (1 + profit_dist)
                    if high >= profit_threshold:
                        exit_price = max(profit_threshold, open_px)
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
                        entry_stop_dist = None
                        entry_profit_dist = None
                        equity_curve.append(cash)
                        prev_close = float(close)
                        continue

            if position > 0 and sell_arr[i] and not is_limit_down:
                exit_price = self._apply_slippage(fill_px, OrderType.SELL)
                trade = self._execute_sell(
                    date, exit_price, position, cash,
                    entry_date, entry_price, entry_cost_total,
                    entry_reason, "信号卖出",
                )
                result.trades.append(trade)
                cash += self._apply_commission(exit_price * position, OrderType.SELL)
                position = 0
                entry_cost_total = 0.0
                entry_stop_dist = None
                entry_profit_dist = None

            elif position == 0 and buy_arr[i] and not is_limit_up:
                max_shares = self._calc_max_shares(cash, fill_px)
                if max_shares <= 0:
                    unaffordable_count += 1
                if max_shares > 0:
                    entry_price = self._apply_slippage(fill_px, OrderType.BUY)
                    cost = entry_price * max_shares
                    commission = self._calc_commission(cost, OrderType.BUY)
                    if cost + commission <= cash:
                        position = max_shares
                        cash -= cost + commission
                        entry_cost_total = cost + commission
                        entry_date = date
                        entry_reason = "信号买入"
                        # B2: 用**入场当根收盘时已知**的 ATR 锁定止损/止盈距离，
                        # 持仓期间不再回溯改写（否则等于用未来波动调整当前止损）。
                        entry_stop_dist = None
                        entry_profit_dist = None
                        if atr_arr is not None:
                            try:
                                _atr = float(atr_arr[i])
                            except Exception:
                                _atr = float('nan')
                            if _atr == _atr and _atr > 0 and entry_price > 0:
                                entry_stop_dist = (self.config.atr_stop_mult * _atr) / entry_price
                                entry_profit_dist = (self.config.atr_profit_mult * _atr) / entry_price

            if position > 0:
                result.holding_bar_count += 1
            equity_curve.append(cash + position * close)
            prev_close = float(close)

        if position > 0:
            final_price = self._apply_slippage(float(close_arr[-1]), OrderType.SELL)
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
        # A4: 持仓时长 = 持仓 bar 数 / 总 bar 数（holding_bar_count 已在循环内累加）
        if len(df) > 0:
            result.time_in_market = float(result.holding_bar_count) / float(len(df))
        _calculate_metrics(result, equity_curve, market_regimes, df)

        result.unaffordable_lot_events = unaffordable_count
        result.blocked_by_limit_events = limit_blocked_count
        # B7: 暴露权益曲线，供 bootstrap CI / 超额收益显著性检验使用
        try:
            result.equity_curve = [float(v) for v in equity_curve]
        except Exception:
            result.equity_curve = []
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
        """返回可买整手股数（100 股整数倍）；返回 0 表示一手也买不起。

        A3: 调用方以 `max_shares == 0` 计入 unaffordable_lot_events。
        契约：本函数必须返回 int（tests/test_p0_correctness.py::test_lot_100_shares）。
        """
        # 预留滑点和手续费空间，避免低价股全仓时 cost+commission 超出现金
        # P0: A股100股整手取整
        effective_price = price * (1 + self.config.slippage_rate)
        max_position = cash * self.config.max_position_pct
        reserve = max(max_position * self.config.commission_rate, self.config.min_commission)
        affordable = max_position - reserve
        if affordable <= 0 or effective_price <= 0:
            return 0  # A3: unaffordable（一手也买不起）
        raw = int(affordable / effective_price)
        return (raw // 100) * 100  # 整手

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
