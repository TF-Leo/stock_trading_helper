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

# F20: 缓存写盘最小行数 —— 低于该值视为"失败拉取的空壳"，绝不落盘。
MIN_CACHE_ROWS = 20

# F06f: 最近一次取数的真实来源（供 CLI 输出 data.source / data.from_cache，
# 此前这两个字段在 cli.py 里被硬编码成 'sina' / True）。
_LAST_FETCH_INFO = {'source': None, 'from_cache': None}


def last_fetch_info():
    """返回最近一次 get_*_daily 的真实取数来源。"""
    return dict(_LAST_FETCH_INFO)


def _record_fetch(source, from_cache):
    _LAST_FETCH_INFO['source'] = source
    _LAST_FETCH_INFO['from_cache'] = bool(from_cache)


def _drop_shell_cache(cache_file, df, label):
    """空壳缓存（只有表头/极少行）读取后视为无缓存，并删除该文件避免反复命中。"""
    try:
        if df is None or len(df) < 5:
            if os.path.exists(cache_file):
                os.remove(cache_file)
                print(f"[WARN] {label} 缓存 {cache_file} 只有 {0 if df is None else len(df)} 行，"
                      f"判定为空壳并已删除，将重新拉取", file=sys.stderr)
            return True
    except Exception as e:
        print(f"[WARN] 清理空壳缓存失败：{e}", file=sys.stderr)
    return False


def _write_cache(df, cache_file, label):
    """F20: 只有拿到足够行数的真实数据才写盘；失败时绝不创建/覆盖缓存。"""
    try:
        if df is None or df.empty or len(df) < MIN_CACHE_ROWS:
            print(f"[WARN] {label} 拉取结果仅 {0 if df is None else len(df)} 行，"
                  f"不写缓存（避免留下空壳文件）", file=sys.stderr)
            return False
        df.to_csv(cache_file)
        return True
    except Exception as e:
        print(f"[WARN] 写缓存失败 {cache_file}: {e}", file=sys.stderr)
        return False


def get_stock_daily(symbol, start_date='20200101', adjust='qfq', source='akshare'):
    """获取股票日线数据（前复权）。"""
    cache_file = os.path.join(DATA_DIR, f'stock_{symbol}_{adjust}.csv')
    _stale_cache = None
    
    if os.path.exists(cache_file):
        try:
            df = pd.read_csv(cache_file, index_col='date', parse_dates=True)
            if 'outstanding_share' in df.columns and 'volume' in df.columns:
                shares = df['outstanding_share'].replace(0, np.nan)
                df['turnover'] = (df['volume'] / shares * 100).fillna(0)
            if _drop_shell_cache(cache_file, df, f'stock_{symbol}'):
                df = None
            elif not df.empty:
                last_date = df.index[-1].date()
                today = datetime.date.today()
                if last_date >= today - datetime.timedelta(days=1):
                    _record_fetch('cache', True)
                    return df
                # 缓存过旧 → 需要触网；若触网失败仍要能回退到这份可用缓存
                _stale_cache = df
            else:
                _stale_cache = None
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
            _write_cache(df, cache_file, f'stock_{symbol}')
            _record_fetch(source, False)
            return df
    except Exception as e:
        print(f"拉取股票数据失败：{e}", file=sys.stderr)

    try:
        if _stale_cache is not None and not _stale_cache.empty:
            print(f"[WARN] {symbol} 本次拉取失败，回退到本地旧缓存", file=sys.stderr)
            _record_fetch('cache', True)
            return _stale_cache
    except Exception:
        pass
    _record_fetch(None, None)
    return None


def get_fund_daily(symbol, start_date='20200101', adjust='', source='akshare'):
    """获取基金日净值数据（close-only）。"""
    cache_file = os.path.join(DATA_DIR, f'fund_{symbol}.csv')
    cached_df = None
    
    if os.path.exists(cache_file):
        try:
            df = pd.read_csv(cache_file, index_col='date', parse_dates=True)
            if _drop_shell_cache(cache_file, df, f'fund_{symbol}'):
                df = None
            else:
                cached_df = df.copy()
            if df is not None and not df.empty:
                last_date = df.index[-1].date()
                today = datetime.date.today()
                if last_date >= today - datetime.timedelta(days=1):
                    _record_fetch('cache', True)
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
            _record_fetch('cache', True)
            return cached_df
        _record_fetch(None, None)
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
        if df is None or df.empty:
            # 拉到了但标准化/过滤后为空 → 视为失败，回退可用缓存，且绝不写空壳
            print(f"[WARN] {symbol} 标准化后无有效数据，回退本地缓存", file=sys.stderr)
            if cached_df is not None and not cached_df.empty:
                _record_fetch('cache', True)
                return cached_df
            _record_fetch(None, None)
            return None
        # F20: 空结果不得写盘（历史事故：留下 56 字节、零数据行的空壳缓存）
        if not _write_cache(df, cache_file, f'fund_{symbol}'):
            if df is None or df.empty:
                _record_fetch(None, None)
                return None
        _record_fetch(source, False)
        return df
    except Exception as e:
        print(f"数据处理失败：{e}", file=sys.stderr)
        _record_fetch(None, None)
        return None


def _name_cache_path():
    return os.path.join(DATA_DIR, '.name_cache.json')


def _load_name_cache():
    try:
        import json
        with open(_name_cache_path(), 'r', encoding='utf-8') as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def get_stock_name(code):
    """获取股票名称。

    离线时会走网络超时（约 10s），每次 CLI 调用都白等一次会把 --fast 墙钟推到 60s 边缘，
    因此加一个本地名称缓存（data 目录已被 .gitignore 排除，不入库）。
    """
    cache = _load_name_cache()
    if code in cache:
        return str(cache[code])
    try:
        import akshare as ak
        df = ak.stock_individual_info_em(symbol=code)
        if df is not None and not df.empty:
            name = str(df.iloc[0].get('名称', code))
            try:
                import json
                cache[code] = name
                with open(_name_cache_path(), 'w', encoding='utf-8') as f:
                    json.dump(cache, f, ensure_ascii=False, indent=1)
            except Exception:
                pass
            return name
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
