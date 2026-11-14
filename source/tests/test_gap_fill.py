# -*- coding: utf-8 -*-
"""issue #11：冷启动缺口检测 / 跨源补齐 / 显式告警的离线测试。

全部用合成缺口 fixture，不触网：日历与参照源都以可调用对象注入。
"""
import calendar as _cal
import datetime as _dt

import pandas as pd
import pytest

from stock_analysis.data_loader import gapfill, meta


def _frame(dates, base=10.0):
    index = pd.DatetimeIndex(dates, name='date')
    close = [base + i * 0.01 for i in range(len(index))]
    return pd.DataFrame({
        'open': close, 'high': [c + 0.05 for c in close],
        'low': [c - 0.05 for c in close], 'close': close,
        'volume': [10000.0 + i for i in range(len(index))],
    }, index=index)


def _month_calendar(year, month):
    """整月日历：工作日开市、周末休市（合成 fixture 用）。"""
    last = _cal.monthrange(year, month)[1]
    return {
        _dt.date(year, month, day).isoformat():
            _dt.date(year, month, day).weekday() < 5
        for day in range(1, last + 1)
    }


def _calendar_with_closed(closed_dates):
    closed = {pd.Timestamp(d).date().isoformat() for d in closed_dates}

    def lookup(year, month):
        days = _month_calendar(year, month)
        for key in closed:
            if key in days:
                days[key] = False
        return days
    return lookup


# ---------------------------------------------------------------------------
# 1) 检测到缺口
# ---------------------------------------------------------------------------
def test_missing_weekdays_detects_gap_only_for_weekdays():
    dates = pd.bdate_range('2024-01-01', periods=40)
    frame = _frame(dates.difference(pd.DatetimeIndex([dates[20]])))
    missing = gapfill.missing_weekdays(frame.index)
    assert missing == [dates[20]]


# ---------------------------------------------------------------------------
# 2) 补齐成功
# ---------------------------------------------------------------------------
def test_gap_filled_from_reference_when_basis_matches():
    dates = pd.bdate_range('2024-01-01', periods=60)
    gap_day = dates[30]
    full = _frame(dates)
    primary = full.drop(index=[gap_day])
    reference = full

    out, info = gapfill.reconcile_gaps(
        primary,
        symbol='TEST',
        reference_loader=lambda: (reference, 'sina_stock'),
        calendar_lookup=_month_calendar,
    )

    assert len(out) == len(dates)
    assert gap_day in out.index
    assert out.index.is_monotonic_increasing
    assert info['gap_filled_dates'] == [gap_day.date().isoformat()]
    assert info['gap_fill_sources'] == {gap_day.date().isoformat(): 'sina_stock'}
    assert any('已由 sina_stock 补齐' in line for line in info['gap_warnings'])
    # 补入行与原序列同口径（逐值一致）
    assert out.loc[gap_day, 'close'] == pytest.approx(reference.loc[gap_day, 'close'])


def test_holiday_is_not_treated_as_gap():
    """参照源也缺的节假日/停牌日不在候选集里，不应产生任何告警。"""
    dates = pd.bdate_range('2024-01-01', periods=40)
    gap_day = dates[20]
    primary = _frame(dates.difference(pd.DatetimeIndex([gap_day])))
    reference = _frame(dates.difference(pd.DatetimeIndex([gap_day])))  # 两边都没有

    out, info = gapfill.reconcile_gaps(
        primary, symbol='TEST',
        reference_loader=lambda: (reference, 'sina_stock'),
        calendar_lookup=_month_calendar)

    assert len(out) == len(primary)
    assert info['gap_filled_dates'] == []
    assert info['gap_warnings'] == []


def test_gap_requires_calendar_trading_day():
    """参照源有该行、但官方日历说休市 → 不得补入。"""
    dates = pd.bdate_range('2024-01-01', periods=40)
    gap_day = dates[20]
    primary = _frame(dates.difference(pd.DatetimeIndex([gap_day])))
    reference = _frame(dates)

    out, info = gapfill.reconcile_gaps(
        primary, symbol='TEST',
        reference_loader=lambda: (reference, 'sina_stock'),
        calendar_lookup=_calendar_with_closed([gap_day]))

    assert gap_day not in out.index
    assert info['gap_filled_dates'] == []
    assert any('均非官方交易日' in line for line in info['gap_warnings'])


def test_gap_rejected_when_neighbor_basis_differs():
    """邻近日价格差 >0.01 元 → 复权口径不一致，禁止混入并告警。"""
    dates = pd.bdate_range('2024-01-01', periods=60)
    gap_day = dates[30]
    full = _frame(dates)
    primary = full.drop(index=[gap_day])
    reference = full
    neighbour = gap_day - pd.offsets.BDay(1)
    reference.loc[neighbour, ['open', 'high', 'low', 'close']] += 0.02

    out, info = gapfill.reconcile_gaps(
        primary, symbol='TEST',
        reference_loader=lambda: (reference, 'sina_stock'),
        calendar_lookup=_month_calendar)

    assert gap_day not in out.index
    assert info['gap_filled_dates'] == []
    assert any('不得混入' in line for line in info['gap_warnings'])


def test_gap_rejected_when_volume_basis_differs():
    dates = pd.bdate_range('2024-01-01', periods=60)
    gap_day = dates[30]
    full = _frame(dates)
    primary = full.drop(index=[gap_day])
    reference = full
    neighbour = gap_day + pd.offsets.BDay(1)
    reference.loc[neighbour, 'volume'] += 101

    out, info = gapfill.reconcile_gaps(
        primary, symbol='TEST',
        reference_loader=lambda: (reference, 'sina_stock'),
        calendar_lookup=_month_calendar)

    assert gap_day not in out.index
    assert any('成交量差' in line and '不得混入' in line
               for line in info['gap_warnings'])


# ---------------------------------------------------------------------------
# 3) 补不到时输出 warning（禁止静默）
# ---------------------------------------------------------------------------
def test_unfillable_gap_emits_explicit_warning():
    dates = pd.bdate_range('2024-01-01', periods=40)
    gap_day = dates[20]
    primary = _frame(dates.difference(pd.DatetimeIndex([gap_day])))

    out, info = gapfill.reconcile_gaps(
        primary, symbol='TEST',
        reference_loader=None,                 # 参照源不可用
        calendar_lookup=_month_calendar,       # 日历确认它是交易日
        refetch_fetcher=lambda day: (None, None),  # 近端重抓也补不到
        calendar_fallback=True,
    )

    assert gap_day not in out.index
    assert info['gap_filled_dates'] == []
    assert any('无法补齐' in line for line in info['gap_warnings'])


def test_no_reference_warns_without_scanning_calendar():
    """默认（不开启日历扫描）只在无参照源时给一条显式告警，不逐月打日历。"""
    dates = pd.bdate_range('2024-01-01', periods=40)
    primary = _frame(dates.difference(pd.DatetimeIndex([dates[20]])))
    calls = []

    out, info = gapfill.reconcile_gaps(
        primary, symbol='TEST',
        reference_loader=lambda: (None, None),
        calendar_lookup=lambda y, m: calls.append((y, m)) or _month_calendar(y, m),
    )

    assert len(out) == len(primary)
    assert calls == []
    assert any('未做缺口核验' in line for line in info['gap_warnings'])


def test_calendar_unavailable_emits_warning():
    dates = pd.bdate_range('2024-01-01', periods=40)
    gap_day = dates[20]
    primary = _frame(dates.difference(pd.DatetimeIndex([gap_day])))

    out, info = gapfill.reconcile_gaps(
        primary, symbol='TEST',
        reference_loader=None,
        calendar_lookup=lambda y, m: None,
        calendar_fallback=True,
    )

    assert info['gap_filled_dates'] == []
    assert any('缺口未检测' in line for line in info['gap_warnings'])


def test_calendar_unavailable_is_fail_closed_and_fills_nothing():
    """Hermes review 第 2 条：日历取不到 → 一个日期都不补。

    旧实现会退回「参照源有这天就补」，把参照源多带的节假日/周末行（实测 7 根）
    写进 K 线。现在必须是 fail-closed：只输出「未做缺口核验（日历不可用）」。
    """
    dates = pd.bdate_range('2024-01-01', periods=40)
    gap_day = dates[20]
    # 参照源多出：1 个真缺口（工作日）+ 1 个周六 + 1 个周日
    weekend = pd.DatetimeIndex([pd.Timestamp('2024-01-20'),   # 周六
                                pd.Timestamp('2024-01-21')])  # 周日
    assert weekend[0].weekday() == 5 and weekend[1].weekday() == 6
    full = _frame(dates)
    primary = full.drop(index=[gap_day])
    reference = pd.concat([full, _frame(weekend, base=20.0)])

    out, info = gapfill.reconcile_gaps(
        primary, symbol='TEST',
        reference_loader=lambda: (reference, 'sina_stock'),
        calendar_lookup=lambda y, m: None,  # 日历整月不可用
    )

    assert len(out) == len(primary)
    for day in [gap_day, weekend[0], weekend[1]]:
        assert day not in out.index
    assert info['gap_filled_dates'] == []
    assert info['gap_fill_sources'] == {}
    assert any('未做缺口核验（日历不可用）' in line
               for line in info['gap_warnings'])
    # 不能出现「按参照源日期补齐（未经日历确认）」这种 fail-open 措辞
    assert not any('未经日历确认' in line for line in info['gap_warnings'])


# ---------------------------------------------------------------------------
# 无缺口时不触网：参照源/日历一次都不应被调用
# ---------------------------------------------------------------------------
def test_no_candidates_makes_no_calls():
    dates = pd.bdate_range('2024-01-01', periods=40)
    frame = _frame(dates)
    calls = {'ref': 0, 'cal': 0}

    def ref():
        calls['ref'] += 1
        return None, None

    def cal(y, m):
        calls['cal'] += 1
        return _month_calendar(y, m)

    out, info = gapfill.reconcile_gaps(
        frame, symbol='TEST', reference_loader=ref, calendar_lookup=cal)

    pd.testing.assert_frame_equal(out, frame)
    assert info == gapfill.empty_info()
    assert calls == {'ref': 0, 'cal': 0}


# ---------------------------------------------------------------------------
# meta 集成：冷启动补根 + 报价精度归一 + _LAST_FETCH_INFO 新字段
# ---------------------------------------------------------------------------
class _TwoSourcePool:
    """主源返回缺一根的 4 位小数帧；参照源（新浪）返回含该根的 2 位小数帧。"""

    last_attempts = []

    def __init__(self, primary, reference):
        self.primary = primary
        self.reference = reference

    def fetch(self, symbol, start_date, adjust, preferred=None):
        if preferred and preferred[0] == 'sina_stock':
            return self.reference.copy(), 'sina_stock'
        return self.primary.copy(), 'astock_tencent'


def test_cold_start_fills_gap_and_normalizes_price_tick(tmp_path, monkeypatch):
    from stock_analysis.data_sources import astock

    dates = pd.bdate_range('2024-01-01', periods=120)
    gap_day = dates[60]
    full = _frame(dates, base=100.0)
    primary = full.drop(index=[gap_day]).copy()
    # 主源价格带 4 位小数（腾讯比例复权的未取整结果）
    for col in ('open', 'high', 'low', 'close'):
        primary[col] = primary[col] + 0.004

    monkeypatch.setattr(meta, 'DATA_DIR', str(tmp_path))
    monkeypatch.setattr(meta, '_STOCK_DATA_SOURCE_POOL',
                        _TwoSourcePool(primary, full))
    monkeypatch.setattr(meta, '_expected_latest_trade_date',
                        lambda now=None: dates[-1].date())
    monkeypatch.setattr(astock, 'get_month_calendar', _month_calendar)

    out = meta.get_stock_daily('600519', start_date=None, source='akshare')
    info = meta.last_fetch_info()

    assert gap_day in out.index                       # 缺口补回
    assert len(out) == len(dates)
    assert info['gap_filled_dates'] == [gap_day.date().isoformat()]
    assert info['gap_fill_sources'] == {gap_day.date().isoformat(): 'sina_stock'}
    assert any('已由 sina_stock 补齐' in line for line in info['gap_warnings'])
    # 冷启动写盘前归一到 0.01 元报价精度
    rounded = out[['open', 'high', 'low', 'close']].to_numpy()
    assert ((rounded * 100) - (rounded * 100).round()).__abs__().max() < 1e-6
    written = pd.read_csv(tmp_path / 'stock_600519_qfq.csv',
                          index_col='date', parse_dates=True)
    assert gap_day in written.index


class _StaticPool:
    """所有通道返回同一份帧（模拟同源同数据，隔离精度问题）。"""

    last_attempts = []

    def __init__(self, frame):
        self.frame = frame

    def fetch(self, symbol, start_date, adjust, preferred=None):
        return self.frame.copy(), 'astock_tencent'


def test_cold_start_and_incremental_sequences_are_value_equal(tmp_path, monkeypatch):
    """Hermes review 第 3 条（硬要求）：同标的、同一天，两条路径逐值相等。

    构造：源端给 4 位小数；冷启动从零拉全量；增量路径的旧缓存故意留 4 位
    小数（模拟更早版本写下的缓存）。修复后两者都必须收敛到 0.01 元，
    且整段序列逐值相等 —— 否则「冷启动归一、增量写回 4 位」会反复复发。
    """
    from stock_analysis.data_sources import astock

    dates = pd.bdate_range('2024-01-01', periods=120)
    full = _frame(dates, base=100.0)
    for col in ('open', 'high', 'low', 'close'):
        full[col] = full[col] + 0.0043          # 未取整的 4 位小数
    assert not (full['close'] * 100).round().equals(full['close'] * 100)

    monkeypatch.setattr(meta, '_STOCK_DATA_SOURCE_POOL', _StaticPool(full))
    monkeypatch.setattr(meta, '_expected_latest_trade_date',
                        lambda now=None: dates[-1].date())
    monkeypatch.setattr(astock, 'get_month_calendar', _month_calendar)

    # --- 冷启动路径（无缓存） ---
    cold_dir = tmp_path / 'cold'
    cold_dir.mkdir()
    monkeypatch.setattr(meta, 'DATA_DIR', str(cold_dir))
    cold = meta.get_stock_daily('600519', start_date=None, source='akshare')
    assert cold is not None and len(cold) == len(dates)

    # --- 增量路径（旧缓存末日在 100 根处，且是 4 位小数） ---
    incr_dir = tmp_path / 'incr'
    incr_dir.mkdir()
    seed = full.iloc[:100].copy()
    seed.to_csv(incr_dir / 'stock_600519_qfq.csv')
    monkeypatch.setattr(meta, 'DATA_DIR', str(incr_dir))
    incr = meta.get_stock_daily('600519', start_date=None, source='akshare')
    assert incr is not None and len(incr) == len(dates)

    # 两条路径对同一天逐值相等（含增量写回的新 tail）
    pd.testing.assert_frame_equal(cold.sort_index(), incr.sort_index())
    written = pd.read_csv(incr_dir / 'stock_600519_qfq.csv',
                          index_col='date', parse_dates=True)
    pd.testing.assert_frame_equal(cold.sort_index(), written.sort_index())
    # 增量写回的磁盘数据同样是 0.01 元精度
    prices = written[['open', 'high', 'low', 'close']].to_numpy()
    assert (abs(prices * 100 - (prices * 100).round()) < 1e-6).all()


class _NoNetworkPool:
    """缓存命中快路径不应触网：一旦被调用就判定测试失败。"""

    last_attempts = []

    def fetch(self, *args, **kwargs):
        raise AssertionError('新鲜缓存命中路径不应调用数据源池')


def test_fresh_cache_hit_normalizes_price_tick(tmp_path, monkeypatch):
    """Hermes 复验遗留：新鲜缓存命中快路径也必须归一到 0.01 元报价精度。

    构造一份「末日 = 期望交易日」的 4 位小数 CSV 缓存（命中快路径、不触网），
    断言返回帧 OHLC 已归一到 2 位，且与冷启动路径逐值相等；同时确认
    `--start`（start_date）截取语义未被归一化改动破坏。
    """
    from stock_analysis.data_sources import astock

    dates = pd.bdate_range('2024-01-01', periods=120)
    full = _frame(dates, base=100.0)
    for col in ('open', 'high', 'low', 'close'):
        full[col] = full[col] + 0.0043          # 未取整的 4 位小数
    assert not (full['close'] * 100).round().equals(full['close'] * 100)

    monkeypatch.setattr(meta, '_expected_latest_trade_date',
                        lambda now=None: dates[-1].date())
    monkeypatch.setattr(astock, 'get_month_calendar', _month_calendar)

    # --- 冷启动基准（无缓存，走全量拉取） ---
    cold_dir = tmp_path / 'cold'
    cold_dir.mkdir()
    monkeypatch.setattr(meta, 'DATA_DIR', str(cold_dir))
    monkeypatch.setattr(meta, '_STOCK_DATA_SOURCE_POOL', _StaticPool(full))
    cold = meta.get_stock_daily('600519', start_date=None, source='akshare')
    assert cold is not None and len(cold) == len(dates)

    # --- 新鲜 4 位小数缓存：命中快路径，池子一旦被调用即失败（证明零触网） ---
    hit_dir = tmp_path / 'hit'
    hit_dir.mkdir()
    full.to_csv(hit_dir / 'stock_600519_qfq.csv')
    monkeypatch.setattr(meta, 'DATA_DIR', str(hit_dir))
    monkeypatch.setattr(meta, '_STOCK_DATA_SOURCE_POOL', _NoNetworkPool())
    hit = meta.get_stock_daily('600519', start_date=None, source='akshare')

    assert hit is not None and len(hit) == len(dates)
    assert meta.last_fetch_info()['from_cache'] is True
    prices = hit[['open', 'high', 'low', 'close']].to_numpy()
    assert (abs(prices * 100 - (prices * 100).round()) < 1e-6).all()
    pd.testing.assert_frame_equal(cold.sort_index(), hit.sort_index())

    # start_date 截取语义不变：仍从指定日起、且值等于全量帧的对应子集
    cutoff = pd.Timestamp('2024-03-01')
    truncated = meta.get_stock_daily('600519', start_date='20240301',
                                     source='akshare')
    assert len(truncated) == len(hit[hit.index >= cutoff])
    pd.testing.assert_frame_equal(truncated.sort_index(),
                                  hit[hit.index >= cutoff].sort_index())


def test_last_fetch_info_exposes_gap_fields_and_copies(tmp_path, monkeypatch):
    monkeypatch.setattr(meta, 'DATA_DIR', str(tmp_path))
    meta._reset_fetch_info()
    info = meta.last_fetch_info()
    assert info['gap_filled_dates'] == []
    assert info['gap_fill_sources'] == {}
    assert info['gap_warnings'] == []
    # 返回的是副本：调用方修改不应污染内部账本
    info['gap_filled_dates'].append('x')
    info['gap_fill_sources']['x'] = 'y'
    assert meta.last_fetch_info()['gap_filled_dates'] == []
    assert meta.last_fetch_info()['gap_fill_sources'] == {}
