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
    _STOCK_DATA_SOURCE_POOL,
)

# 调用方 `source=` 入参 → 渠道池优先顺序。
#   'akshare'（默认）/ 'astock' → 腾讯 K 线优先（固定快照，不封 IP）
#   'eastmoney'                → 显式要求东财时仍先试东财，失败再腾讯
#   'sina' / 'sina_stock'      → 显式要求新浪时先试新浪，失败再腾讯
# 未列出的（含 None）→ 不指定偏好，按渠道池自身的轮询顺序（腾讯仍排第一）
_SOURCE_PREFERENCE = {
    'akshare': ['astock_tencent', 'eastmoney', 'sina_stock', 'sina_fund'],
    'astock': ['astock_tencent', 'eastmoney', 'sina_stock', 'sina_fund'],
    'astock_tencent': ['astock_tencent', 'eastmoney', 'sina_stock', 'sina_fund'],
    'eastmoney': ['eastmoney', 'astock_tencent', 'sina_stock', 'sina_fund'],
    'sina': ['sina_stock', 'astock_tencent', 'eastmoney', 'sina_fund'],
    'sina_stock': ['sina_stock', 'astock_tencent', 'eastmoney', 'sina_fund'],
}

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
    # a-stock-data §12：期望交易日的来源。'astock_calendar' = 深交所官方日历；
    # 'estimate' = 原有「周末推算」回退。只进 _LAST_FETCH_INFO（CLI JSON 契约未改，
    # 因为 decision/schema.py 属本任务禁止修改的范围——见 PR 说明）。
    'expected_source': None,
}

# 最近一次 _expected_latest_trade_date() 的判定来源
_EXPECTED_SOURCE = {'value': None}


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
    # 期望交易日来源随每条取数记录一起落账（caller 无需逐处传参）
    _LAST_FETCH_INFO['expected_source'] = _EXPECTED_SOURCE.get('value')


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


# ---------------------------------------------------------------------------
# 旧缓存增量更新（2026-11-12，修复 PR #9 阻断回归）
#
# 事故：新主通道 astock_tencent 把「未指定 start」解释成 2020-01-01，冷/热跑都把
# 600519 的 6041 根缓存覆盖成 1662 根，回测结论从 no_edge/唐奇安 变成 ok/配对交易。
#
# 修复后的不变量：
#   1. 冷启动（无缓存）→ 从 FULL_HISTORY_START 拉全量；
#   2. 过期缓存 → 只拉「缓存末日 - CACHE_OVERLAP_DAYS」的近端窗口（通常 1 个
#      腾讯分页窗口），与旧缓存的重叠交易日比对后合并（新值覆盖旧值）；
#   3. 重叠不一致（除权/复权基准变化）或完全无重叠 → 从**缓存首日**全量重取并
#      整体替换（不是从 1990，避免无谓的 19 次分页）；
#   4. 任何情况下写盘长度不得小于原缓存；更短则保留原缓存并报 degraded。
# ---------------------------------------------------------------------------
CACHE_OVERLAP_DAYS = 40       # 近端回看天数（覆盖春节等长假，保证有重叠交易日）
OVERLAP_PRICE_TOL = 0.01      # 重叠段价格容差（元；4 位小数舍入）
OVERLAP_VOLUME_TOL = 100.0    # 重叠段成交量容差（股；腾讯按整手取整，上游 #57）
# 冷启动下限：1990-01-01 早于 A 股任一标的上市日，等价「全量」。
# 用 YYYYMMDD 是因为 eastmoney/sina 通道收这个格式（astock 侧会转成 1990-01-01）。
FULL_HISTORY_START = '19900101'


def _prepare_indexed_frame(raw):
    """渠道原始 DataFrame → DatetimeIndex + 数值列（写盘与合并共用）。"""
    full = raw
    if 'date' not in full.columns:
        full = _normalize_daily_df(full)
    full = full.copy()
    full['date'] = pd.to_datetime(full['date'])
    full = full.set_index('date').sort_index()
    for col in ('open', 'high', 'low', 'close', 'volume'):
        if col in full.columns:
            full[col] = pd.to_numeric(full[col], errors='coerce')
    return full[~full.index.duplicated(keep='last')]


def _overlap_matches(cached, fresh):
    """重叠交易日上的价格/成交量是否一致。

    返回 True / False / None（无重叠，无法判定）。None 与 False 在调用方都按
    「不能安全拼接」处理：前复权因子会在除权/分红后整体重算，把两段不同基准的
    序列直接拼接会让历史收益静默失真。
    """
    common = cached.index.intersection(fresh.index)
    if len(common) == 0:
        return None
    for col in ('open', 'high', 'low', 'close'):
        if col not in cached.columns or col not in fresh.columns:
            continue
        a = pd.to_numeric(cached.loc[common, col], errors='coerce')
        b = pd.to_numeric(fresh.loc[common, col], errors='coerce')
        if a.isna().any() or b.isna().any():
            return False
        if float((a - b).abs().max()) > OVERLAP_PRICE_TOL:
            return False
    if 'volume' in cached.columns and 'volume' in fresh.columns:
        a = pd.to_numeric(cached.loc[common, 'volume'], errors='coerce')
        b = pd.to_numeric(fresh.loc[common, 'volume'], errors='coerce')
        if a.isna().any() or b.isna().any():
            return False
        if float((a - b).abs().max()) > OVERLAP_VOLUME_TOL:
            return False
    return True


def _merge_incremental(cached, fresh):
    """旧缓存 + 近端增量：按日期去重保留新值，升序；旧历史一根不丢。"""
    merged = pd.concat([cached, fresh])
    merged = merged[~merged.index.duplicated(keep='last')]
    return merged.sort_index()


def get_stock_daily(symbol, start_date='20200101', adjust='qfq', source='akshare'):
    """获取股票日线数据（前复权）。

    缓存策略（本轮修正）：
    * 新鲜度按**期望最新交易日**判断（跳过周末、收盘前不算当天），
      非交易日不再无谓触网（原实现用 today-1，周日在周六→必然触网失败再回退）；
    * 缓存命中时**仍按 start_date 截取**（原实现完全忽略该参数）；
    * 写盘一律写**未截断的全量**（原实现把 start 过滤后的子集写回，
      一次 `--start 2024` 就会把 2020–2023 的缓存永久削掉）；
    * 未指定 start_date 时按**全量历史**取数（FULL_HISTORY_START），不再被
      适配器默认的 2020-01-01 截断；过期缓存走近端增量 + 合并，绝不写回更短的
      缓存（详见上面 CACHE_OVERLAP_DAYS 一节的不变量）。
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

    # 2026-10-18：此前这里**只用东财单通道**（ak.stock_zh_a_hist），
    # 但 bars.py 里早已实现并注册了 `_STOCK_DATA_SOURCE_POOL`（eastmoney →
    # sina_stock → sina_fund 三通道 + 冷却/剔除策略），却从来没被 meta 调用。
    # 后果：东财节点一封 IP，整条取数链就死掉，尽管新浪通道完全可用
    # （实测 Hermes 环境：ak.stock_zh_a_daily 返回正常，东财 TCP 层被拒）。
    # 现改为走渠道池，任一通道成功即返回，全部失败才回退旧缓存。
    # 取数窗口：冷启动拉全量；过期缓存只拉近端窗口，拿回后再与旧缓存比对/合并。
    incremental = _stale_cache is not None and not _stale_cache.empty
    if incremental:
        last_cached = pd.Timestamp(_stale_cache.index[-1])
        fetch_start = (last_cached - pd.Timedelta(days=CACHE_OVERLAP_DAYS)).strftime('%Y%m%d')
    else:
        fetch_start = FULL_HISTORY_START

    print(f"正在拉取 {symbol} 历史数据 (渠道池，起 {fetch_start}) ...", file=sys.stderr)
    raw = None
    used_source = None
    try:
        raw, used_source = _STOCK_DATA_SOURCE_POOL.fetch(
            symbol, fetch_start, adjust, preferred=_SOURCE_PREFERENCE.get(source)
        )
    except Exception as e:
        print(f"渠道池调用异常：{e}", file=sys.stderr)

    if raw is not None and not raw.empty:
        try:
            full = _prepare_indexed_frame(raw)
            if incremental:
                consistent = _overlap_matches(_stale_cache, full)
                if consistent is True:
                    full = _merge_incremental(_stale_cache, full)
                else:
                    # 无法确认与旧缓存同一复权基准 → 从缓存首日全量重取、整体替换。
                    reason = '重叠段不一致' if consistent is False else '与旧缓存无重叠'
                    print(f"[WARN] {symbol} 增量{reason}，改为从缓存首日 "
                          f"{pd.Timestamp(_stale_cache.index[0]).date()} 全量重取", file=sys.stderr)
                    raw2, used2 = None, None
                    try:
                        raw2, used2 = _STOCK_DATA_SOURCE_POOL.fetch(
                            symbol, pd.Timestamp(_stale_cache.index[0]).strftime('%Y%m%d'),
                            adjust, preferred=_SOURCE_PREFERENCE.get(source))
                    except Exception as e:
                        print(f"渠道池全量重取异常：{e}", file=sys.stderr)
                    if raw2 is not None and not raw2.empty:
                        full = _prepare_indexed_frame(raw2)
                        used_source = used2 or used_source
                    else:
                        print(f"全量重取失败，回退到本地旧缓存", file=sys.stderr)
                        _mark_degraded(f"{symbol} 增量{reason}且全量重取失败，回退到本地旧缓存",
                                       _stale_cache)
                        return _apply_start(_stale_cache, start_date)

            # 硬不变量：写盘不得比原缓存短（历史丢失不可逆，宁可保留旧缓存）
            if incremental and _stale_cache is not None and len(full) < len(_stale_cache):
                _mark_degraded(f"{symbol} 拉取结果比本地缓存短"
                               f"（{len(full)} < {len(_stale_cache)} 行），保留原缓存",
                               _stale_cache)
                return _apply_start(_stale_cache, start_date)

            # 写盘用全量，返回用截断窗口
            _write_cache(full, cache_file, f'stock_{symbol}')
            _record_fetch(used_source or 'unknown', False,
                          cache_last_date=(pd.Timestamp(full.index[-1]).date().isoformat()
                                           if len(full) else None),
                          lag_days=0,
                          expected_date=expected.isoformat(),
                          requested_source=source)
            return _apply_start(full, start_date)
        except Exception as e:
            print(f"渠道池数据规范化失败（source={used_source}）：{e}", file=sys.stderr)
    else:
        print(f"拉取股票数据失败：渠道池全部通道未返回数据", file=sys.stderr)

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

    # 基金取数通道（2026-10-18 新增 OTC）：
    #   1) 场内 ETF：ak.fund_etf_hist_sina —— 只覆盖 510300 这类场内 ETF；
    #   2) 场外 OTC：ak.fund_open_fund_info_em（单位净值走势）—— 覆盖 023298/001917 等场外基金。
    # 历史缺口（Hermes 实测暴露）：此前**只有 ETF 通道**，4 只场外基金
    # （023298/006195/001917/001564）全部 0 行 → data_source_error，
    # 而同一批基金在 OTC 通道上有 366~2689 行、数据到 2026-10-16。
    print(f"正在拉取基金 {symbol} 历史数据（ETF → OTC）...", file=sys.stderr)
    df = None
    fund_channel = None

    # --- 通道 1：场内 ETF（新浪）
    try:
        import akshare as ak
        sina_symbol = symbol
        if symbol.isdigit():
            if symbol.startswith(('5', '6')):
                sina_symbol = 'sh' + symbol
            elif symbol.startswith(('1', '2', '3', '4', '0')):
                sina_symbol = 'sz' + symbol
        etf = ak.fund_etf_hist_sina(symbol=sina_symbol)
        if etf is not None and not etf.empty:
            df, fund_channel = etf, 'sina_etf'
        else:
            print(f"ETF 通道无数据（{symbol}），尝试 OTC 净值通道", file=sys.stderr)
    except Exception as e:
        print(f"ETF 通道失败：{e}；尝试 OTC 净值通道", file=sys.stderr)

    # --- 通道 2：场外 OTC 单位净值（东财）
    if df is None or df.empty:
        try:
            import akshare as ak  # 已在上面 import，此处防御 ak 未定义情形
            otc = ak.fund_open_fund_info_em(symbol=symbol, indicator="单位净值走势")
            if otc is not None and not otc.empty:
                df, fund_channel = otc, 'eastmoney_otc'
        except Exception as e:
            print(f"OTC 净值通道失败：{e}", file=sys.stderr)

    if df is None or df.empty:
        if cached_df is not None and not cached_df.empty:
            _mark_degraded(f"{symbol} 两个通道均失败，回退到本地缓存", cached_df)
            return _apply_start(cached_df, start_date)
        _record_fetch(None, None)
        return None

    try:
        # OTC 场外基金只提供单位净值，没有 OHLCV。用净值补齐单价格序列：
        #   open/high/low 全部 = 单位净值（净值无日内波动），volume = 0（场外无成交量）。
        # 这是场外基金唯一可用的表示；回测里基于 close 的信号/收益不受影响。
        if fund_channel == 'eastmoney_otc':
            raw = df.copy()
            raw = raw.rename(columns={'净值日期': 'date', '单位净值': 'close'})
            if 'close' not in raw.columns:
                raise ValueError(f"OTC 净值列缺失，实际列={list(raw.columns)}")
            raw['close'] = pd.to_numeric(raw['close'], errors='coerce')
            raw = raw.dropna(subset=['close'])
            for c in ('open', 'high', 'low'):
                raw[c] = raw['close']
            raw['volume'] = 0
            full = raw[['date', 'open', 'high', 'low', 'close', 'volume']]
        else:
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
        _record_fetch(fund_channel or 'sina', False,
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


def _expected_latest_trade_date(now=None):
    """计算期望的最新交易日日期。

    a-stock-data §12 接入（2026-11-12）：优先使用深交所官方整月日历，
    能正确跨过法定节假日（旧实现只跳周末，节假日会误判"缓存过期"→
    无谓触网 50s 再回退）。日历不可用（离线 / 未发布 / 接口失败）时，
    **完全回退**到原来的周末推算法，行为与接入前一致。
    """
    now = now or datetime.datetime.now()

    try:
        from stock_analysis.data_sources import astock
        official = astock.latest_trading_day_at(now)
    except Exception as e:  # 日历是辅助信号，任何异常都不得阻断取数
        print(f"[WARN] 官方交易日历不可用，回退周末推算：{e}", file=sys.stderr)
        official = None

    if official is not None:
        _EXPECTED_SOURCE['value'] = 'astock_calendar'
        return official

    _EXPECTED_SOURCE['value'] = 'estimate'
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
        print(f"重采样失败：{e}", file=sys.stderr)
        return None


def get_market_index_daily(symbol='000985', start_date='20200101', source='akshare'):
    """获取指数数据。"""
    return None


def resolve_and_fetch(code, type_hint='auto', start_date=None):
    """按 `type_hint` 取数，auto 时做**双向探测**。

    返回 `(df, data_type, error_msg)`；`df is None` 且 error_msg 非空表示彻底失败。

    为什么需要这个函数（2026-10-18，Hermes 实测暴露）：
      旧逻辑在 cli.py 里写 `data_type = 'fund' if code.startswith('5') else 'stock'`，
      导致场外基金 023298/006195/001917/001564 被判成**股票** → data_source_error。

      但**不能简单把 0/1 段判成基金**：实测 `ak.fund_name_em()` 名录里
      002270(华明装备)、002415(海康威视) 这些**真股票**也出现在基金名录中
      （场外基金代码段与深市股票代码段重叠），纯代码规则无法可靠区分。

      故 auto 改为按通道探测：先按可靠前缀试，取不到再试另一侧；
      两侧都失败则返回明确错误、提示用 --type，**绝不猜一个当成事实**。
    """
    def _fetch(dtype):
        if dtype == 'fund':
            return get_fund_daily(code, start_date=start_date)
        # 2026-11-12：主流程改用默认渠道优先级（腾讯优先），不再硬绑新浪。
        # 显式 source='sina' 仍可用（见 _SOURCE_PREFERENCE）。
        return get_stock_daily(code, start_date=start_date, source='akshare')

    if type_hint == 'fund':
        return _fetch('fund'), 'fund', None
    if type_hint == 'stock':
        return _fetch('stock'), 'stock', None

    # auto：5/15/16/18 开头是场内基金的可靠段；其余默认按股票试
    first = 'fund' if code.startswith(('5', '15', '16', '18')) else 'stock'
    df = _fetch(first)
    if df is not None and len(df) >= 100:
        return df, first, None

    alt = 'stock' if first == 'fund' else 'fund'
    alt_df = _fetch(alt)
    if alt_df is not None and len(alt_df) >= 100:
        print(f"[INFO] {code} 以 {first} 取数失败，已自动按 {alt} 取数成功"
              f"（如需固定，请用 --type {alt}）", file=sys.stderr)
        return alt_df, alt, None

    # 两侧都没拿到足量数据：若首选有数据（只是不足 100 根）就返回它，让上层判 insufficient_data
    if df is not None:
        return df, first, None
    return None, first, (
        f"自动判定失败：按 stock 与 fund 两种类型均无法取到 {code} 的数据。"
        f"请用 --type stock 或 --type fund 显式指定。")
