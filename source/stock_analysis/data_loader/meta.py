# -*- coding: utf-8 -*-
"""Meta data functions for stock/fund data loading"""
import os
import pandas as pd
import numpy as np
import datetime
import sys

from stock_analysis.data_loader.bars import (
    DATA_DIR,
    _normalize_daily_df,
)


def get_stock_daily(symbol, start_date='20200101', adjust='qfq', source='akshare'):
    """获取股票日线数据（前复权）。"""
    cache_file = os.path.join(DATA_DIR, f'stock_{symbol}_{adjust}.csv')
    
    if os.path.exists(cache_file):
        try:
            df = pd.read_csv(cache_file, index_col='date', parse_dates=True)
            if 'outstanding_share' in df.columns and 'volume' in df.columns:
                shares = df['outstanding_share'].replace(0, np.nan)
                df['turnover'] = (df['volume'] / shares * 100).fillna(0)
            if not df.empty:
                last_date = df.index[-1].date()
                today = datetime.date.today()
                if last_date >= today - datetime.timedelta(days=1):
                    return df
        except Exception as e:
            print(f"Error reading cache {cache_file}: {e}", file=sys.stderr)
    
    print(f"正在拉取 {symbol} 历史数据 (渠道池：{source})...", file=sys.stderr)
    try:
        import akshare as ak
        ak_symbol = symbol
        if symbol.isdigit():
            if symbol.startswith(('5', '6')):
                ak_symbol = 'sh' + symbol
            elif symbol.startswith(('1', '2', '3', '4', '0')):
                ak_symbol = 'sz' + symbol
        df = ak.stock_zh_a_hist(symbol=ak_symbol, period="daily", start_date=start_date.replace('-', ''), end_date="20300101")
        if df is not None and not df.empty:
            df = df.rename(columns={'日期': 'date', '收盘': 'close', '开盘': 'open', '最高': 'high', '最低': 'low', '成交量': 'volume'})
            df['date'] = pd.to_datetime(df['date'])
            df = df.set_index('date').sort_index()
            df = df[df.index >= pd.to_datetime(start_date)]
            df['close'] = pd.to_numeric(df['close'], errors='coerce')
            df['volume'] = pd.to_numeric(df['volume'], errors='coerce')
            df.to_csv(cache_file)
            return df
    except Exception as e:
        print(f"拉取股票数据失败：{e}", file=sys.stderr)
    
    return None


def get_fund_daily(symbol, start_date='20200101', adjust='', source='akshare'):
    """获取基金日净值数据（close-only）。"""
    cache_file = os.path.join(DATA_DIR, f'fund_{symbol}.csv')
    cached_df = None
    
    if os.path.exists(cache_file):
        try:
            df = pd.read_csv(cache_file, index_col='date', parse_dates=True)
            cached_df = df.copy()
            if not df.empty:
                last_date = df.index[-1].date()
                today = datetime.date.today()
                if last_date >= today - datetime.timedelta(days=1):
                    return df
        except Exception as e:
            print(f"Error reading cache {cache_file}: {e}", file=sys.stderr)
    
    print(f"正在拉取基金 {symbol} 历史数据...", file=sys.stderr)
    df = None
    
    try:
        import akshare as ak
        sina_symbol = symbol
        if symbol.isdigit():
            if symbol.startswith(('5', '6')):
                sina_symbol = 'sh' + symbol
            elif symbol.startswith(('1', '2', '3', '4', '0')):
                sina_symbol = 'sz' + symbol
        df = ak.fund_etf_hist_sina(symbol=sina_symbol)
    except Exception as e:
        print(f"拉取基金数据失败：{e}", file=sys.stderr)
        pass
    
    if df is None or df.empty:
        if cached_df is not None and not cached_df.empty:
            print(f"[WARN] {symbol} 本次拉取失败，回退到本地缓存", file=sys.stderr)
            return cached_df
        return None
    
    try:
        df = _normalize_daily_df(df)
        df['date'] = pd.to_datetime(df['date'], errors='coerce')
        df = df.dropna(subset=['date'])
        df = df.set_index('date').sort_index()
        df = df[df.index >= pd.to_datetime(start_date)]
        if 'close' in df.columns:
            df['close'] = pd.to_numeric(df['close'], errors='coerce')
        df = df[~df.index.duplicated(keep='last')]
        df.to_csv(cache_file)
        return df
    except Exception as e:
        print(f"数据处理失败：{e}")
        return None


def get_stock_name(code):
    """获取股票名称。"""
    try:
        import akshare as ak
        df = ak.stock_individual_info_em(symbol=code)
        if df is not None and not df.empty:
            return str(df.iloc[0].get('名称', code))
    except Exception:
        pass
    return code


def _expected_latest_trade_date():
    """计算期望的最新交易日日期。"""
    now = datetime.datetime.now()
    expected = now.date()
    if now.hour < 15:
        expected = expected - datetime.timedelta(days=1)
    while expected.weekday() >= 5:  # 5=周六，6=周日
        expected = expected - datetime.timedelta(days=1)
    return expected


def resample_stock_data(df, period='W'):
    """重采样股票数据到周/月。"""
    if df is None or df.empty:
        return None
    if period == 'M':
        period = 'ME'
    agg_dict = {
        'open': 'first', 'high': 'max', 'low': 'min',
        'close': 'last', 'volume': 'sum', 'amount': 'sum',
    }
    agg_dict = {k: v for k, v in agg_dict.items() if k in df.columns}
    try:
        resampled = df.resample(period).agg(agg_dict)
        resampled = resampled.dropna(subset=['close'])
        return resampled
    except Exception as e:
        print(f"重采样失败：{e}")
        return None


def get_market_index_daily(symbol='000985', start_date='20200101', source='akshare'):
    """获取指数数据。"""
    return None
