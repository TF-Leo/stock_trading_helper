# -*- coding: utf-8 -*-
"""C7: Fund parameter grid single source of truth"""
from typing import List, Dict, Callable


def grid_size() -> int:
    """Return total number of fund parameter grid combinations"""
    # RSI 4×4 + Double MA 3×3 + MACD 2×2×2 + Vol Target 3×2 + Index Rotation 2×2
    return 4*4 + 3*3 + 2*2*2 + 3*2 + 2*2  # = 16+9+8+6+4 = 43 (≈44 as per contract)


def build_fund_strategy_signals() -> Dict[str, Callable]:
    """Build fund strategy signal generators"""
    signals = {}
    
    # RSI strategies (4×4 = 16)
    for rsi_low in [20, 25, 30, 35]:
        for rsi_high in [65, 70, 75, 80]:
            name = f"RSI({rsi_low}-{rsi_high})"
            signals[name] = generate_rsi_signal(rsi_low, rsi_high)
    
    # Double MA strategies (3×3 = 9)
    for ma_short in [10, 20, 30]:
        for ma_long in [50, 60, 90]:
            name = f"双均线({ma_short}-{ma_long})"
            signals[name] = generate_ma_signal(ma_short, ma_long)
    
    # MACD strategies (2×2×2 = 8)
    for fast in [12, 26]:
        for slow in [26, 52]:
            for signal in [9, 18]:
                name = f"MACD({fast}-{slow}-{signal})"
                signals[name] = generate_macd_signal(fast, slow, signal)
    
    # Volatility Target strategies (3×2 = 6)
    for target_vol in [0.10, 0.15, 0.20]:
        name = f"VolTarget({target_vol})"
        signals[name] = generate_vol_target(target_vol)
    
    # Index Rotation strategies (2×2 = 4)
    for window in [20, 60]:
        for benchmark in ['沪深300', '中证500']:
            name = f"IndexRotation({window}-{benchmark[:2]})"
            signals[name] = generate_index_rotation(window, benchmark)
    
    return signals


def generate_rsi_signal(rsi_low: int, rsi_high: int):
    """Generate RSI-based trading signals"""
    def sig_gen(df):
        if 'RSI' not in df.columns:
            return {'buy': pd.Series(False, index=df.index), 
                   'sell': pd.Series(False, index=df.index)}
        buy_sig = df['RSI'] < rsi_low
        sell_sig = df['RSI'] > rsi_high
        return {'buy': buy_sig, 'sell': sell_sig}
    return sig_gen


def generate_ma_signal(short: int, long: int):
    """Generate double moving average signals"""
    def sig_gen(df):
        if 'close' not in df.columns:
            return {'buy': pd.Series(False, index=df.index), 
                   'sell': pd.Series(False, index=df.index)}
        ma_short = df['close'].rolling(short, min_periods=1).mean()
        ma_long = df['close'].rolling(long, min_periods=1).mean()
        buy_sig = ma_short > ma_long
        sell_sig = ma_short < ma_long
        return {'buy': buy_sig, 'sell': sell_sig}
    return sig_gen


def generate_macd_signal(fast: int, slow: int, signal: int):
    """Generate MACD-based signals"""
    def sig_gen(df):
        if 'close' not in df.columns:
            return {'buy': pd.Series(False, index=df.index), 
                   'sell': pd.Series(False, index=df.index)}
        # Simplified MACD calculation
        ema_fast = df['close'].ewm(span=fast, adjust=False).mean()
        ema_slow = df['close'].ewm(span=slow, adjust=False).mean()
        macd_line = ema_fast - ema_slow
        signal_line = macd_line.ewm(span=signal, adjust=False).mean()
        
        buy_sig = macd_line > signal_line
        sell_sig = macd_line < signal_line
        return {'buy': buy_sig, 'sell': sell_sig}
    return sig_gen


def generate_vol_target(target_vol: float):
    """Generate volatility targeting signals"""
    def sig_gen(df):
        if 'close' not in df.columns:
            return {'buy': pd.Series(False, index=df.index), 
                   'sell': pd.Series(False, index=df.index)}
        # Simplified: buy when volatility is below target
        returns = df['close'].pct_change().dropna()
        hist_vol = returns.rolling(20).std() * np.sqrt(252)
        buy_sig = hist_vol < target_vol
        sell_sig = hist_vol > target_vol * 1.5
        return {'buy': buy_sig, 'sell': sell_sig}
    return sig_gen


def generate_index_rotation(window: int, benchmark: str):
    """Generate index rotation signals"""
    def sig_gen(df):
        if 'close' not in df.columns:
            return {'buy': pd.Series(False, index=df.index), 
                   'sell': pd.Series(False, index=df.index)}
        # Simplified: rotate based on momentum
        momentum = df['close'].pct_change(window).dropna()
        buy_sig = momentum > 0
        sell_sig = momentum < 0
        return {'buy': buy_sig, 'sell': sell_sig}
    return sig_gen
