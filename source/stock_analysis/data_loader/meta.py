try:
    import akshare as ak
except ImportError:  # P2: 与bars.py一致，离线时仅用缓存
    ak = None
import pandas as pd
import numpy as np
import datetime
import os

from stock_analysis.data_loader.bars import (
    DATA_DIR,
    _STOCK_DATA_SOURCE_POOL,
    _normalize_daily_df,
    get_stock_name,
)


def _expected_latest_trade_date():
    """计算期望的最新交易日日期。

    规则：
    - 若当前时间 >= 15:00，期望今日收盘数据（回退到最近工作日）
    - 若当前时间 < 15:00，期望昨日数据（回退到最近工作日）
    - 跳过周末（不处理节假日，节假日会触发重拉但无新数据，可接受）
    """
    now = datetime.datetime.now()
    expected = now.date()
    if now.hour < 15:
        expected = expected - datetime.timedelta(days=1)
    while expected.weekday() >= 5:  # 5=周六, 6=周日
        expected = expected - datetime.timedelta(days=1)
    return expected


def get_stock_daily(symbol, start_date='20200101', adjust='qfq', source='akshare'):
    cache_file = os.path.join(DATA_DIR, f'stock_{symbol}_{adjust}.csv')
    cached_df = None

    if os.path.exists(cache_file):
        try:
            df = pd.read_csv(cache_file, index_col='date', parse_dates=True)
            cached_df = df.copy()
            if 'outstanding_share' in df.columns and 'volume' in df.columns:
                shares = df['outstanding_share'].replace(0, np.nan)
                df['turnover'] = (df['volume'] / shares * 100).fillna(0)
            if not df.empty:
                last_date = df.index[-1].date()
                if last_date >= _expected_latest_trade_date():
                    return df
        except Exception as e:
            print(f"Error reading cache {cache_file}: {e}")

    print(f"正在拉取 {symbol} 历史数据 (渠道池: {source})...")
    preferred_sources = []
    if source == 'sina':
        preferred_sources = ["sina_stock", "sina_fund"]
    elif source in ('eastmoney', 'em'):
        preferred_sources = ["eastmoney"]

    df, used_source = _STOCK_DATA_SOURCE_POOL.fetch(
        symbol=symbol,
        start_date=start_date,
        adjust=adjust,
        preferred=preferred_sources,
    )

    if df is None or df.empty:
        if cached_df is not None and not cached_df.empty:
            print(f"[WARN] {symbol} 本次拉取失败，回退到本地缓存 (最后日期: {cached_df.index[-1].date()})")
            return cached_df
        if ak is None:
            print(f"[WARN] {symbol} 在线拉取失败原因: akshare未安装")
        return None

    try:
        if used_source:
            print(f"[INFO] {symbol} 使用数据源: {used_source}")
        df = _normalize_daily_df(df)
        df['date'] = pd.to_datetime(df['date'], errors='coerce')
        df = df.dropna(subset=['date'])
        df = df.set_index('date').sort_index()
        cols = ['open', 'close', 'high', 'low', 'volume', 'amount', 'turnover']
        for col in cols:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors='coerce')
        df = df[~df.index.duplicated(keep='last')]
        df.to_csv(cache_file)
        return df
    except Exception as e:
        print(f"数据处理失败: {e}")
        return None


def get_market_index_daily(symbol="000985", start_date='20200101', source='akshare'):
    if symbol == "000985":
        csi_file = os.path.join(DATA_DIR, 'csi_all_share.csv')
        if os.path.exists(csi_file):
            try:
                df = pd.read_csv(csi_file, index_col='date', parse_dates=True)
                if not df.empty:
                    if 'volume' not in df.columns:
                        df['volume'] = 0
                    last_date = df.index[-1].date()
                    today = datetime.date.today()
                    if last_date >= today - datetime.timedelta(days=1):
                        return df
            except Exception as e:
                print(f"读取 csi_all_share.csv 失败: {e}")

    cache_file = os.path.join(DATA_DIR, f'index_{symbol}.csv')
    cached_df = None

    if os.path.exists(cache_file):
        try:
            cached_df = pd.read_csv(cache_file, index_col='date', parse_dates=True)
            if not cached_df.empty:
                last_date = cached_df.index[-1].date()
                today = datetime.date.today()
                if last_date >= today - datetime.timedelta(days=1):
                    return cached_df
        except Exception as e:
            print(f"读取缓存失败: {e}")
            cached_df = None

    print(f"正在拉取指数 {symbol} 数据...")
    df = None

    try:
        ak_symbol = symbol
        if symbol.isdigit():
            if symbol.startswith('000'):
                ak_symbol = f"sh{symbol}"
            elif symbol.startswith('399'):
                ak_symbol = f"sz{symbol}"
        if source == 'sina':
            df = ak.stock_zh_index_daily_sina(symbol=ak_symbol)
        else:
            df = ak.stock_zh_index_daily(symbol=ak_symbol)
    except Exception:
        pass

    if df is None or df.empty:
        try:
            em_symbol = symbol
            if symbol.startswith('sh') or symbol.startswith('sz'):
                em_symbol = symbol[2:]
            df = ak.stock_zh_index_daily_em(symbol=em_symbol, start_date=start_date.replace('-', ''))
            if df is not None and not df.empty:
                df = df.rename(columns={'date': 'date', 'close': 'close', 'volume': 'volume'})
        except Exception:
            pass

    if df is None or df.empty:
        try:
            em_symbol = symbol
            if symbol.startswith('sh') or symbol.startswith('sz'):
                em_symbol = symbol[2:]
            df = ak.index_zh_a_hist(symbol=em_symbol, period="daily", start_date=start_date.replace('-', ''))
            if df is not None and not df.empty:
                df = df.rename(columns={'日期': 'date', '收盘': 'close', '成交量': 'volume'})
        except Exception:
            pass

    if df is None or df.empty:
        try:
            if symbol == "000985":
                end_date_str = datetime.datetime.now().strftime("%Y%m%d")
                df = ak.stock_zh_index_hist_csindex(symbol="000985", start_date=start_date.replace('-', ''), end_date=end_date_str)
                if df is not None and not df.empty:
                    df = df.rename(columns={'日期': 'date', '收盘': 'close', '成交金额': 'volume'})
        except Exception:
            pass

    if df is not None and not df.empty:
        try:
            df['date'] = pd.to_datetime(df['date'])
            df = df.set_index('date').sort_index()
            df = df[df.index >= pd.to_datetime(start_date)]
            df['volume'] = pd.to_numeric(df['volume'], errors='coerce')
            df['close'] = pd.to_numeric(df['close'], errors='coerce')
            df.to_csv(cache_file)
            return df
        except Exception as e:
            print(f"Index data processing failed: {e}")

    if cached_df is not None:
        print(f"警告: 无法更新 {symbol} 数据，使用本地缓存 (最后日期: {cached_df.index[-1].date()})")
        return cached_df

    return None


def resample_stock_data(df, period='W'):
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
        print(f"重采样失败: {e}")
        return None
