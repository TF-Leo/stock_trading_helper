# -*- coding: utf-8 -*-
"""D4: Triggers module - Unified trigger evaluation and rendering"""
from typing import Dict, List, Optional, Any
import pandas as pd
import numpy as np


def evaluate_triggers(df: pd.DataFrame, strategy_signals: Dict[str, pd.Series],
                      signal_block: Dict[str, Any] = None) -> List[Dict]:
    """从传入的信号序列本身推导触发状态（D4：不依赖硬编码名字表）。

    F24/F25 语义变更（2026-10-25）：
      * `state` 优先取 `signal_block['state']`（由 `decision.signals` 的状态机给出）。
        旧实现在这里用"近 10 根出现过卖点"判定 `near_sell`，把**历史事件**
        当成了**当前状态**，导致一次死叉在其后 10 个交易日天天被报"卖出信号"。
      * `current` 改为**指标当前值**（`signal_block['current_value']`），
        不再等于收盘价；收盘价另设 `last_close`。旧实现让文案里的数字
        随价格逐日变化（32.42 → 33.78），看起来像指标读数，实际是价格。

    Args:
        df: 行情数据
        strategy_signals: {strategy_name: {'buy': Series, 'sell': Series}}
        signal_block: `decision.signals.build()` 的输出；为 None 时退化为旧口径
                      （仅为兼容旧调用，`current` 返回 None 而不是价格）

    Returns:
        List of trigger dicts with current, last_close, buy_trigger, sell_trigger, state
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

        if signal_block and signal_block.get('state'):
            state = signal_block['state']
        elif buy_now:
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
            # F25: 指标当前值；不可得时为 None（绝不拿收盘价顶替）
            'current': (signal_block or {}).get('current_value'),
            'last_close': last_close,
            'buy_trigger': (f"买点参考价 {buy_price:.2f}（最近一次买点所在 bar 收盘价）"
                            if buy_price is not None else "样本内无买点"),
            'sell_trigger': (f"卖点参考价 {sell_price:.2f}（最近一次卖点所在 bar 收盘价）"
                             if sell_price is not None else "样本内无卖点"),
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


def format_trigger_message(triggers: List[Dict]) -> str:
    """【已弃用】旧文案生成器，仅供历史调用。

    F23：`recommendation.action_reason` 现由 `decision.render.build_reasons()` 生成——
    文案必须在 `action` 确定之后产生，且不得把 `near_*`（接近阈值）说成"信号"。
    原签名里的 `action` 形参从未被使用，已删除。
    """
    messages = []

    for t in triggers:
        state = t.get('state')
        if state == 'buy_triggered':
            messages.append(f"{t['indicator']}: 买点触发")
        elif state == 'sell_triggered':
            messages.append(f"{t['indicator']}: 卖点触发")
        elif state == 'near_buy':
            messages.append(f"{t['indicator']}: 接近买点")
        elif state == 'near_sell':
            messages.append(f"{t['indicator']}: 接近卖点")

    if not messages:
        return "暂无明确交易信号，建议观望"

    return "; ".join(messages[:5])  # 最多显示 5 个信号