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
#
# G33（本轮新增）：只报 source/from_cache 不够 —— "触网失败→回退旧缓存"这种**降级**
# 在旧实现里对调用方完全不可见（JSON 里 from_cache=True，但看不出是"新鲜命中"还是
# "拉取失败后的兜底"）。因此这里把降级事实、缓存末日与滞后交易日数一并结构化记录，
# 由 cli.py 写进 diagnostics.warnings + data.stale。
_LAST_FETCH_INFO = {
    'source': None,          # 实际生效的数据来源（cache / akshare / None）
    'from_cache': None,
    'requested_source': None,  # 调用方传入的渠道名（可能与实际渠道不同）
    'degraded': False,       # True = 本次发生"取数失败/不新鲜→回退旧缓存"
    'degraded_reason': None,
    'cache_last_date': None,  # 缓存末根 K 线日期（ISO）
    'lag_days': None,        # 缓存末日 → 期望最新交易日 之间的交易日数
    'expected_date': None,   # 期望的最新交易日（跳过周末/未收盘）
}


def last_fetch_info():
    """返回最近一次 get_*_daily 的真实取数来源与降级事实。"""
    return dict(_LAST_FETCH_INFO)


def _reset_fetch_info(requested_source=None):
    for k in _LAST_FETCH_INFO:
        _LAST_FETCH_INFO[k] = None
    _LAST_FETCH_INFO['degraded'] = False
    _LAST_FETCH_INFO['requested_source'] = requested_source


def _record_fetch(source, from_cache, **extra):
    _LAST_FETCH_INFO['source'] = source
    _LAST_FETCH_INFO['from_cache'] = bool(from_cache)
    for k, v in extra.items():
        if k in _LAST_FETCH_INFO:
            _LAST_FETCH_INFO[k] = v


def _mark_degraded(reason, cached_df):
    """记录"回退旧缓存"这一降级事实（G33 的判定依据）。"""
    last_date = None
    lag = None
    expected = _expected_latest_trade_date()
    try:
        if cached_df is not None and len(cached_df) > 0:
            last_date = pd.Timestamp(cached_df.index[-1]).date()
            lag = _trading_days_between(last_date, expected)
    except Exception:
        last_date = None
    _record_fetch(
        'cache', True,
        degraded=True,
        degraded_reason=reason,
        cache_last_date=last_date.isoformat() if last_date else None,
        lag_days=lag,
        expected_date=expected.isoformat(),
    )
    print(f"[WARN] {reason}（缓存末日 {last_date}，滞后 {lag} 个交易日）", file=sys.stderr)


def _trading_days_between(d1, d2):
    """d1→d2 之间的交易日数（仅排除周末；不含法定节假日，故为下界估计）。"""
    try:
        if d1 is None or d2 is None or d2 <= d1:
            return 0
        n = 0
        cur = d1
        while cur < d2:
            cur = cur + datetime.timedelta(days=1)
            if cur.weekday() < 5:
                n += 1
        return n
    except Exception:
        return None


def _apply_start(df, start_date):
    """按 start_date 截取返回窗口。

    历史缺陷：缓存命中路径**完全不看 start_date**，直接返回全量缓存 ——
    `--start 20260701` 与不带 `--start` 得到逐字节相同的结果，参数形同虚设。
    """
    if df is None or len(df) == 0 or not start_date:
        return df
    try:
        sd = pd.to_datetime(str(start_date))
    except Exception:
        return df
    try:
        out = df[df.index >= sd]
        return out
    except Exception:
        return df


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
    """获取股票日线数据（前复权）。

    缓存策略（本轮修正）：
    * 新鲜度按**期望最新交易日**判断（跳过周末、收盘前不算当天），
      非交易日不再无谓触网（原实现用 today-1，周日在周六→必然触网失败再回退）；
    * 缓存命中时**仍按 start_date 截取**（原实现完全忽略该参数）；
    * 写盘一律写**未截断的全量**（原实现把 start 过滤后的子集写回，
      一次 `--start 2024` 就会把 2020–2023 的缓存永久削掉）。
    """
    cache_file = os.path.join(DATA_DIR, f'stock_{symbol}_{adjust}.csv')
    _reset_fetch_info(source)
    _stale_cache = None
    expected = _expected_latest_trade_date()

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
                if last_date >= expected:
                    _record_fetch('cache', True,
                                  cache_last_date=last_date.isoformat(),
                                  lag_days=0,
                                  expected_date=expected.isoformat())
                    return _apply_start(df, start_date)
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
        raw = ak.stock_zh_a_hist(symbol=ak_symbol, period="daily", start_date=start_date.replace('-', ''), end_date="20300101")
        if raw is not None and not raw.empty:
            full = raw.rename(columns={'日期': 'date', '收盘': 'close', '开盘': 'open', '最高': 'high', '最低': 'low', '成交量': 'volume'})
            full['date'] = pd.to_datetime(full['date'])
            full = full.set_index('date').sort_index()
            full['close'] = pd.to_numeric(full['close'], errors='coerce')
            full['volume'] = pd.to_numeric(full['volume'], errors='coerce')
            # 写盘用全量，返回用截断窗口
            _write_cache(full, cache_file, f'stock_{symbol}')
            _record_fetch('akshare', False,
                          cache_last_date=(pd.Timestamp(full.index[-1]).date().isoformat()
                                           if len(full) else None),
                          lag_days=0,
                          expected_date=expected.isoformat())
            return _apply_start(full, start_date)
    except Exception as e:
        print(f"拉取股票数据失败：{e}", file=sys.stderr)

    try:
        if _stale_cache is not None and not _stale_cache.empty:
            _mark_degraded(f"{symbol} 本次拉取失败，回退到本地旧缓存", _stale_cache)
            return _apply_start(_stale_cache, start_date)
    except Exception:
        pass
    _record_fetch(None, None)
    return None


def get_fund_daily(symbol, start_date='20200101', adjust='', source='akshare'):
    """获取基金日净值数据（close-only）。缓存策略同 get_stock_daily。"""
    cache_file = os.path.join(DATA_DIR, f'fund_{symbol}.csv')
    _reset_fetch_info(source)
    cached_df = None
    expected = _expected_latest_trade_date()

    if os.path.exists(cache_file):
        try:
            df = pd.read_csv(cache_file, index_col='date', parse_dates=True)
            if _drop_shell_cache(cache_file, df, f'fund_{symbol}'):
                df = None
            else:
                cached_df = df.copy()
            if df is not None and not df.empty:
                last_date = df.index[-1].date()
                if last_date >= expected:
                    _record_fetch('cache', True,
                                  cache_last_date=last_date.isoformat(),
                                  lag_days=0,
                                  expected_date=expected.isoformat())
                    return _apply_start(df, start_date)
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
            _mark_degraded(f"{symbol} 本次拉取失败，回退到本地缓存", cached_df)
            return _apply_start(cached_df, start_date)
        _record_fetch(None, None)
        return None

    try:
        full = _normalize_daily_df(df)
        full['date'] = pd.to_datetime(full['date'], errors='coerce')
        full = full.dropna(subset=['date'])
        full = full.set_index('date').sort_index()
        if 'close' in full.columns:
            full['close'] = pd.to_numeric(full['close'], errors='coerce')
        full = full[~full.index.duplicated(keep='last')]
        window = _apply_start(full, start_date)
        if window is None or window.empty:
            # 拉到了但窗口内为空 → 视为失败，回退可用缓存，且绝不写空壳
            if cached_df is not None and not cached_df.empty:
                _mark_degraded(f"{symbol} 窗口内无有效数据，回退本地缓存", cached_df)
                return _apply_start(cached_df, start_date)
            _record_fetch(None, None)
            return None
        # F20: 空结果不得写盘（历史事故：留下 56 字节、零数据行的空壳缓存）
        # 写盘用全量，避免被 start_date 截断
        _write_cache(full, cache_file, f'fund_{symbol}')
        _record_fetch('sina', False,
                      cache_last_date=(pd.Timestamp(full.index[-1]).date().isoformat()
                                       if len(full) else None),
                      lag_days=0,
                      expected_date=expected.isoformat())
        return window
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
