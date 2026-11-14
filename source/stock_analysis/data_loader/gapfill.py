# -*- coding: utf-8 -*-
"""冷启动数据完整性：交易日缺口检测 / 跨源补齐（issue #11）。

背景
----
600519 的腾讯不复权日线**源端**缺 2007-09-04 这一根（窄区间单请求已复现，
见 PR 证据）。冷启动（无缓存）走腾讯全量 → 少一根；有缓存时旧历史被保留 →
多一根。Hermes 复现的因果链是「**报价精度是主因、缺根是次因**」：
  * 主因——腾讯 4 位小数 vs 新浪 0.01 元报价单位，会让锦标赛 top-1 从
    唐奇安(30/20) 翻到 CMF（0.4897），归一到 0.01 元后回到 0.3809；
  * 次因——同一 vintage（已归一）下**删掉任意一根**，top-1 仍会从 0.3810
    翻到 0.4133。缺根不是唯一触发点，但「缓存状态决定数据集」同样会改变
    结论，因此冷启动必须主动检测缺口、跨源补齐并显式标注来源。

本模块只做三件事：
  1. **检测**：`missing_weekdays()` 先找出序列里缺的工作日（零成本），
     再用**官方交易日历**确认「应为交易日」——周末与法定节假日不会误报；
  2. **补齐**：拿另一条通道的同一区间数据做参照。参照源与当前序列在缺口
     **邻近日**上必须价格差 ≤0.01 元、成交量差 ≤100 股，否则视为复权口径/
     单位不一致，**不得混入**，只输出 warning；
  3. **告警**：补到的日期与来源写进 `gap_filled_dates` / `gap_fill_sources`；
     补不到、口径不一致、日历不可用都写进 `gap_warnings`，绝不静默。

为什么优先用「参照源日期集」而不是「逐月扫日历」
------------------------------------------------
600519 全历史有 539 个缺失工作日，其中绝大多数是法定节假日，还有 2006 年
股权分置改革等长期停牌日（停牌日两个源都没有 K 线，不是数据缺口）。逐月
请求官方日历要 ~160 次 HTTP（实测 0.25–0.35s/月），而且停牌日会被误判成
「应补缺口」。先比日期集：参照源有、当前序列没有的日期通常只有 0–2 个，
再对这几个日期查官方日历确认，既准又省。参照源不可用时才退回逐月扫日历
（有月份上限，并显式告警未扫描区间）。
"""
from __future__ import annotations

import sys
from typing import Callable, Dict, List, Optional, Tuple

import pandas as pd

# 与 meta.OVERLAP_PRICE_TOL / OVERLAP_VOLUME_TOL 同口径（不复用常量，避免循环导入）
PRICE_TOL = 0.01          # 元
VOLUME_TOL = 100.0        # 股
PRICE_COLUMNS = ('open', 'high', 'low', 'close')
OHLCV_COLUMNS = PRICE_COLUMNS + ('volume',)
# 浮点表示误差：容差边界上的 0.010000000000000009 不得被判成超差（要求是 ≤0.01）
_TOL_EPS = 1e-9
LOCAL_BASIS_RADIUS = 10   # 缺口前后各取 10 个交易日核对复权口径
MAX_FILL_DAYS = 50        # 单次冷启动最多补 50 根（防御：源端整段缺失不是本模块职责）
MAX_FALLBACK_CALENDAR_MONTHS = 36  # 参照源不可用时，逐月扫日历的上限

ReferenceLoader = Callable[[], Tuple[Optional[pd.DataFrame], Optional[str]]]
# 从缺口日前若干天起的**近端重抓**（不是精确窄区间：渠道池只接受起始日，
# 请求实际一直取到最新交易日，代价≈一次全历史请求；详见 reconcile_gaps 文档）。
RefetchFetcher = Callable[[pd.Timestamp], Tuple[Optional[pd.DataFrame], Optional[str]]]
CalendarLookup = Callable[[int, int], Optional[Dict[str, bool]]]


def empty_info() -> dict:
    """`_LAST_FETCH_INFO` 的缺口字段初值（additive，schema_version 不变）。"""
    return {
        'gap_filled_dates': [],
        'gap_fill_sources': {},
        'gap_warnings': [],
    }


def missing_weekdays(index) -> List[pd.Timestamp]:
    """序列区间内缺失的**工作日**（周一至周五）。零网络成本。

    这里只做粗筛：工作日缺根既可能是节假日，也可能是停牌/源端缺根，
    必须再经 :func:`reconcile_gaps` 的日历确认。
    """
    if index is None or len(index) < 2:
        return []
    idx = pd.DatetimeIndex(pd.to_datetime(index)).normalize().unique().sort_values()
    expected = pd.bdate_range(idx[0], idx[-1])
    return list(expected.difference(idx))


def _month_calendar(calendar_lookup, year: int, month: int,
                    cache: Dict[Tuple[int, int], Optional[Dict[str, bool]]]):
    key = (int(year), int(month))
    if key not in cache:
        try:
            cache[key] = calendar_lookup(int(year), int(month))
        except Exception as exc:  # 日历是辅助信号，失败不应中断取数
            print(f"[WARN] 交易日历读取失败 {year}-{month:02d}: {exc}", file=sys.stderr)
            cache[key] = None
    return cache[key]


def _normalize_reference(frame: pd.DataFrame) -> pd.DataFrame:
    """参照源 → DatetimeIndex + OHLCV 数值列（与 meta._prepare_indexed_frame 同形）。"""
    out = frame.copy()
    if 'date' in out.columns:
        out['date'] = pd.to_datetime(out['date'], errors='coerce')
        out = out.set_index('date')
    out.index = pd.DatetimeIndex(pd.to_datetime(out.index))
    out = out.sort_index()
    out = out[~out.index.duplicated(keep='last')]
    for col in OHLCV_COLUMNS:
        if col in out.columns:
            out[col] = pd.to_numeric(out[col], errors='coerce')
    return out


def _row_is_usable(frame: pd.DataFrame, day) -> bool:
    if day not in frame.index:
        return False
    row = frame.loc[day]
    if isinstance(row, pd.DataFrame):  # 极端情况下重复索引
        row = row.iloc[-1]
    for col in OHLCV_COLUMNS:
        if col not in row.index:
            return False
        try:
            value = float(row[col])
        except (TypeError, ValueError):
            return False
        if value != value or value <= 0:  # NaN / 非正价格与成交量都不可用
            return False
    return True


def _local_basis_ok(primary: pd.DataFrame, reference: pd.DataFrame,
                    day, radius: int = LOCAL_BASIS_RADIUS) -> Tuple[bool, str]:
    """用缺口前后各 ``radius`` 个交易日核对参照源与当前序列的口径。

    返回 ``(ok, reason)``。要求邻近日**全部**满足价格差 ≤0.01 元、成交量差
    ≤100 股；任何一天超差都判定「口径不一致」，该缺口不得混入。
    """
    pos = int(primary.index.searchsorted(day))
    lo = max(0, pos - radius)
    hi = min(len(primary), pos + radius)
    window = primary.index[lo:hi]
    common = window.intersection(reference.index)
    if len(common) == 0:
        return False, '邻近日与参照源无重叠，无法核对复权口径'
    for when in common:
        for col in PRICE_COLUMNS:
            a = float(primary.at[when, col])
            b = float(reference.at[when, col])
            if abs(a - b) > PRICE_TOL + _TOL_EPS:
                return False, (f'邻近日 {when.date()} {col} 差 {abs(a - b):.4f} 元'
                               f'（容差 {PRICE_TOL}）')
        if 'volume' in primary.columns and 'volume' in reference.columns:
            va = float(primary.at[when, 'volume'])
            vb = float(reference.at[when, 'volume'])
            if abs(va - vb) > VOLUME_TOL + _TOL_EPS:
                return False, (f'邻近日 {when.date()} 成交量差 {abs(va - vb):.0f} 股'
                               f'（容差 {VOLUME_TOL:.0f}）')
    return True, f'邻近日 {len(common)} 日口径一致'


def _overlap_anomalies(primary: pd.DataFrame, reference: pd.DataFrame) -> dict:
    """全重叠段的源间异常统计（只用于告警，不参与补齐判定）。"""
    common = primary.index.intersection(reference.index)
    if len(common) == 0:
        return {'common': 0, 'price_days': 0, 'vol_days': 0,
                'max_price_diff': 0.0, 'max_vol_diff': 0.0}
    price_days = 0
    max_price_diff = 0.0
    for col in PRICE_COLUMNS:
        if col not in primary.columns or col not in reference.columns:
            continue
        diff = (pd.to_numeric(primary.loc[common, col], errors='coerce')
                - pd.to_numeric(reference.loc[common, col], errors='coerce')).abs()
        price_days += int((diff > PRICE_TOL).sum())
        if len(diff):
            max_price_diff = max(max_price_diff, float(diff.max()))
    vol_days = 0
    max_vol_diff = 0.0
    if 'volume' in primary.columns and 'volume' in reference.columns:
        vdiff = (pd.to_numeric(primary.loc[common, 'volume'], errors='coerce')
                 - pd.to_numeric(reference.loc[common, 'volume'], errors='coerce')).abs()
        vol_days = int((vdiff > VOLUME_TOL).sum())
        if len(vdiff):
            max_vol_diff = float(vdiff.max())
    # price_days 按「列次」累计，同一日期可能多列超差；换成日期集合更直观
    return {
        'common': int(len(common)),
        'price_days': price_days,
        'vol_days': vol_days,
        'max_price_diff': max_price_diff,
        'max_vol_diff': max_vol_diff,
    }


def _append_warning(info: dict, text: str) -> None:
    info['gap_warnings'].append(text)


def _fill_day(primary: pd.DataFrame, reference: pd.DataFrame, day,
              source: Optional[str], info: dict, label: str) -> bool:
    """校验邻近日口径后把单根补进 primary；返回是否成功。"""
    if not _row_is_usable(reference, day):
        _append_warning(info, f"{label} {day.date()} 参照源 {source} 该行不可用，未补齐")
        return False
    ok, reason = _local_basis_ok(primary, reference, day)
    if not ok:
        _append_warning(
            info, f"{label} {day.date()} 未补齐：参照源 {source} {reason}；"
                  f"不满足比例复权口径的行走不得混入")
        return False
    values = {col: float(reference.at[day, col]) for col in OHLCV_COLUMNS
              if col in reference.columns}
    # 补入行同样归一到 A 股 0.01 元报价单位（参照源若也是未取整的复权价，不能
    # 把 4 位小数带进已归一的序列）。
    for col in PRICE_COLUMNS:
        if col in values:
            values[col] = round(values[col], 2)
    # 先占位再逐列写入：primary 可能带有参照源没有的附加列（turnover 等），
    # 这些列在补入行上保持 NaN，而不是拿位置对齐错填。
    primary.loc[day] = float('nan')
    for col, value in values.items():
        if col in primary.columns:
            primary.loc[day, col] = value
    info['gap_filled_dates'].append(day.date().isoformat())
    info['gap_fill_sources'][day.date().isoformat()] = source or 'unknown'
    _append_warning(
        info, f"{label} 已由 {source or 'unknown'} 补齐 {day.date()}（{reason}）")
    return True


def reconcile_gaps(frame: pd.DataFrame, *, symbol: str,
                   reference_loader: Optional[ReferenceLoader] = None,
                   calendar_lookup: Optional[CalendarLookup] = None,
                   refetch_fetcher: Optional[RefetchFetcher] = None,
                   max_fill_days: int = MAX_FILL_DAYS,
                   fallback_max_months: int = MAX_FALLBACK_CALENDAR_MONTHS,
                   calendar_fallback: bool = False,
                   ) -> Tuple[pd.DataFrame, dict]:
    """冷启动缺口检测 + 跨源补齐。返回 ``(frame, info)``。

    ``info`` 含 ``gap_filled_dates`` / ``gap_fill_sources`` / ``gap_warnings``，
    由调用方写进 ``_LAST_FETCH_INFO``。

    策略：
      1. 先算缺失工作日；没有则立即返回（不触网、不查日历）；
      2. 有 ``reference_loader`` → 取另一通道同区间数据，只把「参照源有、
         当前序列没有」的日期当候选（天然排除节假日与停牌日），再查官方
         日历确认；
      3. 参照源不可用 → 默认只告警「未做缺口检测」（逐月扫日历会产生大量
         停牌日误报，且冷启动要多付上百次日历请求）；``calendar_fallback=True``
         时才逐个确认缺失工作日并做**近端重抓**；
      4. 每一根补入行都必须通过邻近日口径校验。

    **fail-closed**：缺失工作日只有被官方日历**明确判为交易日**才可能补入。
    日历取不到（``calendar_lookup=None`` 或整月接口失败）时一根都不补，只输出
    「未做缺口核验（日历不可用）」——绝不能退回「参照源有这天就补」，那会把
    参照源多带的节假日/周末行写进 K 线。

    **近端重抓的真实语义**：``refetch_fetcher`` 的入参只是一个日期，渠道池
    ``fetch()`` 只接受起始日、没有结束日参数，所以实际请求是「缺口日前 5 天 →
    最新交易日」而非缺口前后的小窗口。对 2007 年的缺口这等于**多一次全历史
    请求**（腾讯分页约 19 次 HTTP）。这是当前渠道池 API 的既有限制；本 PR 选择
    如实记录代价，不伪装成「窄区间」。
    """
    info = empty_info()
    if frame is None or len(frame) < 2:
        return frame, info

    candidates = missing_weekdays(frame.index)
    if not candidates:
        return frame, info

    cal_cache: Dict[Tuple[int, int], Optional[Dict[str, bool]]] = {}

    def _calendar(day) -> Optional[bool]:
        if calendar_lookup is None:
            return None
        cal = _month_calendar(calendar_lookup, day.year, day.month, cal_cache)
        if not cal:
            return None
        return cal.get(day.date().isoformat())

    reference = None
    reference_source = None
    if reference_loader is not None:
        try:
            raw_ref, used = reference_loader()
        except Exception as exc:  # 参照源失败不阻断主数据
            raw_ref, used = None, None
            _append_warning(info, f"{symbol} 缺口参照源调用失败：{exc}")
        if raw_ref is not None and len(raw_ref) > 0 and used:
            reference = _normalize_reference(raw_ref)
            reference_source = used

    if reference is not None:
        start, end = frame.index[0], frame.index[-1]
        ref_dates = reference.index[(reference.index >= start) & (reference.index <= end)]
        gap_days = list(ref_dates.difference(frame.index))

        anomalies = _overlap_anomalies(frame, reference)
        if anomalies['price_days'] or anomalies['vol_days']:
            _append_warning(
                info,
                f"{symbol} 跨源校验（{reference_source}）：重叠 {anomalies['common']} 日，"
                f"价格超差 {anomalies['price_days']} 列次（最大 "
                f"{anomalies['max_price_diff']:.4f} 元）、成交量超差 "
                f"{anomalies['vol_days']} 日（最大 {anomalies['max_vol_diff']:.0f} 股）；"
                f"这些行未混入，仅告警")

        if not gap_days:
            return frame, info

        # fail-closed：只有官方日历**明确判为交易日**（True）的候选日才可补。
        # 日历取不到（None）时绝不按参照源日期集补 —— 参照源可能多带节假日/
        # 周末行，旧实现会把这些非交易日写进 K 线（Hermes 实测 7 根）。
        verdicts = {day: _calendar(day) for day in gap_days}
        unavailable = [day for day, verdict in verdicts.items() if verdict is None]
        fillable = [day for day, verdict in verdicts.items() if verdict is True]

        if unavailable and not fillable:
            _append_warning(
                info, f"{symbol} 未做缺口核验（日历不可用）："
                      f"{len(gap_days)} 个候选缺口日全部未校验")
            return frame, info
        if unavailable:
            _append_warning(
                info, f"{symbol} {len(unavailable)} 个候选缺口日的官方交易日历不可用，"
                      f"按 fail-closed 未补齐（日历不可用，不影响已确认的交易日）")

        if not fillable:
            _append_warning(
                info, f"{symbol} 参照源 {reference_source} 多出 "
                      f"{len(gap_days)} 个日期，但均非官方交易日，未补齐")
            return frame, info

        for day in fillable[:max_fill_days]:
            _fill_day(frame, reference, day, reference_source, info, symbol)
        if len(fillable) > max_fill_days:
            _append_warning(
                info, f"{symbol} 另有 {len(fillable) - max_fill_days} 个缺口超过单次上限 "
                      f"{max_fill_days}，本次未处理")
        return frame.sort_index(), info

    # ---- 参照源不可用 -------------------------------------------------------
    if not calendar_fallback:
        _append_warning(
            info, f"{symbol} 检测到 {len(candidates)} 个缺失工作日，但无可用参照源，"
                  f"未做缺口核验（数据可能不完整）")
        return frame, info
    if calendar_lookup is None:
        _append_warning(
            info, f"{symbol} 检测到 {len(candidates)} 个缺失工作日，但无参照源且"
                  f"交易日历不可用，缺口未检测（数据可能不完整）")
        return frame, info

    months = sorted({(d.year, d.month) for d in candidates}, reverse=True)
    scanned = months[:max(1, int(fallback_max_months))]
    if len(months) > len(scanned):
        _append_warning(
            info, f"{symbol} 无可用参照源，仅扫描最近 {len(scanned)}/{len(months)} 个月"
                  f"的官方日历；更早的缺口本次未检测")
    available = [m for m in scanned
                 if _month_calendar(calendar_lookup, m[0], m[1], cal_cache)]
    if not available:
        _append_warning(
            info, f"{symbol} 检测到 {len(candidates)} 个缺失工作日，但官方交易日历"
                  f"不可用且无参照源，缺口未检测（数据可能不完整）")
        return frame, info
    if len(available) < len(scanned):
        _append_warning(
            info, f"{symbol} 官方日历仅覆盖 {len(available)}/{len(scanned)} 个月，"
                  f"其余月份的缺口本次未检测")
    confirmed = []
    for day in candidates:
        if (day.year, day.month) not in scanned:
            continue
        if _calendar(day) is True:
            confirmed.append(day)
    if not confirmed:
        return frame, info

    unfilled = []
    for day in confirmed[:max_fill_days]:
        if refetch_fetcher is None:
            unfilled.append(day)
            continue
        try:
            raw_near, used_near = refetch_fetcher(day)
        except Exception as exc:
            raw_near, used_near = None, None
            _append_warning(info, f"{symbol} {day.date()} 近端重抓失败：{exc}")
        if raw_near is None or len(raw_near) == 0 or not used_near:
            unfilled.append(day)
            continue
        near = _normalize_reference(raw_near)
        if not _fill_day(frame, near, day, used_near, info, symbol):
            unfilled.append(day)
    if unfilled:
        shown = ', '.join(d.date().isoformat() for d in unfilled[:10])
        more = f" 等 {len(unfilled)} 日" if len(unfilled) > 10 else ''
        _append_warning(
            info, f"{symbol} {len(unfilled)} 个应为交易日的日期无法补齐（{shown}{more}），"
                  f"数据可能不完整，结论需谨慎")
    if len(confirmed) > max_fill_days:
        _append_warning(
            info, f"{symbol} 另有 {len(confirmed) - max_fill_days} 个确认缺口超过单次上限 "
                  f"{max_fill_days}，本次未处理")
    return frame.sort_index(), info
