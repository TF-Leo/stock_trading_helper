# -*- coding: utf-8 -*-
"""D4: Triggers module - Unified trigger evaluation and rendering"""
from typing import Dict, List, Optional, Any
import pandas as pd
import numpy as np


def evaluate_triggers(df: pd.DataFrame, strategy_signals: Dict[str, pd.Series]) -> List[Dict]:
    """从传入的信号序列本身推导触发状态（D4：不依赖硬编码名字表）。

    Args:
        df: 行情数据
        strategy_signals: {strategy_name: {'buy': Series, 'sell': Series}}
            值可为 DataFrame（含 buy/sell 列）或 dict（含 buy/sell 键）。

    Returns:
        List of trigger dicts with current, buy_trigger, sell_trigger, state
    """
    triggers = []
    n = len(df)
    if n == 0:
        return triggers
    last_close = float(df['close'].iloc[-1])
    lookback = max(1, min(10, n))

    for name, sig in strategy_signals.items():
        buy = sig['buy'].astype(bool)
        sell = sig['sell'].astype(bool)
        buy_now = bool(buy.iloc[-1])
        sell_now = bool(sell.iloc[-1])
        buy_recent = int(buy.iloc[-lookback:].sum())
        sell_recent = int(sell.iloc[-lookback:].sum())

        if buy_now:
            state = 'buy_triggered'
        elif sell_now:
            state = 'sell_triggered'
        elif buy_recent > 0:
            state = 'near_buy'
        elif sell_recent > 0:
            state = 'near_sell'
        else:
            state = 'neutral'

        # F06b: buy_price/sell_price 必须是**真实历史价位**（最近一次该向信号所在 bar 的收盘价），
        # 此前恒等于 last_close → distance_to_trigger 恒 0（占位常量）。
        buy_price = None
        sell_price = None
        try:
            bidx = buy[buy].index
            sidx = sell[sell].index
            if len(bidx):
                buy_price = float(df.loc[bidx[-1], 'close'])
            if len(sidx):
                sell_price = float(df.loc[sidx[-1], 'close'])
        except Exception:
            buy_price = sell_price = None

        triggers.append({
            'indicator': name,
            'current': last_close,
            'buy_trigger': (f"买入信号触发价 {buy_price:.2f}（最近一次买入信号收盘价）"
                            if buy_price is not None else "当前无买入信号"),
            'sell_trigger': (f"卖出信号触发价 {sell_price:.2f}（最近一次卖出信号收盘价）"
                             if sell_price is not None else "当前无卖出信号"),
            'state': state,
            'buy_price': buy_price,
            'sell_price': sell_price
        })

    return triggers


def _get_ma_state(short_ma: float, long_ma: float) -> str:
    """判断均线状态"""
    diff_pct = (short_ma - long_ma) / long_ma * 100
    if diff_pct >= 2:
        return "buy_triggered"
    elif diff_pct >= -2:
        return "neutral"
    else:
        return "sell_triggered"


def _get_macd_state(macd: float, signal: float) -> str:
    """判断 MACD 状态"""
    if macd > signal * 1.1:
        return "buy_triggered"
    elif abs(macd - signal) < signal * 0.1:
        return "neutral"
    else:
        return "sell_triggered"


def _get_rsi_state(rsi: float, low: int, high: int) -> str:
    """判断 RSI 状态"""
    if rsi <= low:
        return "buy_triggered"
    elif rsi >= high:
        return "sell_triggered"
    else:
        return "neutral"


def _get_donchian_state(close: float, upper: float, lower: float) -> str:
    """判断唐奇安状态"""
    if close >= upper * 0.99:
        return "buy_triggered"
    elif close <= lower * 1.01:
        return "sell_triggered"
    else:
        return "neutral"


def format_trigger_message(triggers: List[Dict], action: str) -> str:
    """格式化触发器消息"""
    messages = []
    
    for t in triggers:
        if t['state'] in ['buy_triggered', 'near_buy']:
            messages.append(f"{t['indicator']}: 买入信号 ({t['current']:.2f})")
        elif t['state'] in ['sell_triggered', 'near_sell']:
            messages.append(f"{t['indicator']}: 卖出信号 ({t['current']:.2f})")
    
    if not messages:
        return "暂无明确交易信号，建议观望"
    
    return "; ".join(messages[:5])  # 最多显示 5 个信号