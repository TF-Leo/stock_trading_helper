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
    _attempt,
    EmptyDataError,
)
from stock_analysis.data_loader import gapfill as _gapfill
from stock_analysis.data_sources import astock as _astock_source

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
    # 本次刷新是否与旧缓存取并集，以及并集里有多少行只存在于旧缓存。
    # 仅用于可观测性（stderr + last_fetch_info），不进 CLI JSON。
    'merged_from_cache': False,
    'cache_rows_kept': 0,
    # P0-4（issue #7）：本轮渠道池的逐通道尝试明细（additive）。
    # 每条 {source, ok, error_class, error_text, cooldown, removed}；无触网时为空。
    'source_attempts': [],
    # issue #11：冷启动缺口检测 / 跨源补齐的可观测字段（additive，schema_version 不变）。
    #   gap_filled_dates : 本次补入的日期（ISO 升序）
    #   gap_fill_sources : {日期: 实际补入来源}，与 gap_filled_dates 一一对应
    #   gap_warnings     : 补齐成功/失败/口径不一致/日历不可用等显式告警
    'gap_filled_dates': [],
    'gap_fill_sources': {},
    'gap_warnings': [],
}

# 最近一次 _expected_latest_trade_date() 的判定来源
_EXPECTED_SOURCE = {'value': None}


def last_fetch_info():
    """返回最近一次 get_*_daily 的真实取数来源与降级事实。"""
    info = dict(_LAST_FETCH_INFO)
    info['source_attempts'] = [dict(item) for item in
                               (_LAST_FETCH_INFO.get('source_attempts') or [])]
    info['gap_filled_dates'] = list(_LAST_FETCH_INFO.get('gap_filled_dates') or [])
    info['gap_fill_sources'] = dict(_LAST_FETCH_INFO.get('gap_fill_sources') or {})
    info['gap_warnings'] = list(_LAST_FETCH_INFO.get('gap_warnings') or [])
    return info


def _reset_fetch_info(requested_source=None):
    for k in _LAST_FETCH_INFO:
        _LAST_FETCH_INFO[k] = None
    _LAST_FETCH_INFO['degraded'] = False
    _LAST_FETCH_INFO['requested_source'] = requested_source
    _LAST_FETCH_INFO['merged_from_cache'] = False
    _LAST_FETCH_INFO['cache_rows_kept'] = 0
    _LAST_FETCH_INFO['source_attempts'] = []
    _LAST_FETCH_INFO['gap_filled_dates'] = []
    _LAST_FETCH_INFO['gap_fill_sources'] = {}
    _LAST_FETCH_INFO['gap_warnings'] = []


def _capture_source_attempts():
    """把渠道池最近一次 fetch 的逐通道明细追加到 _LAST_FETCH_INFO。

    一次 get_stock_daily 可能调用池两次（近端增量 + 全量重取），故用追加而非覆盖。
    兼容既有测试里的 fake pool：没有 `last_attempts` 属性时静默跳过。
    """
    attempts = getattr(_STOCK_DATA_SOURCE_POOL, 'last_attempts', None)
    if not attempts:
        return
    existing = list(_LAST_FETCH_INFO.get('source_attempts') or [])
    existing.extend(dict(item) for item in attempts)
    _LAST_FETCH_INFO['source_attempts'] = existing


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
#      腾讯分页窗口），按 _classify_overlap 分类后合并；
#   3. 分类为 basis_changed / no_overlap → 从**缓存首日**全量重取（不是从 1990，
#      避免无谓的 19 次分页）；
#   4. 全量重取后按**覆盖范围**而非行数决定是否接受：
#      * 起始日 <= 缓存起始日（覆盖起点）→ 接受。
#        - consistent / source_noise → 与旧缓存取并集，**重叠日保留旧缓存值**，
#          只追加缓存末日之后的新行（保留源端缺失的历史根，如腾讯缺 2007-09-04；
#          源间噪声不污染旧基准）；
#        - basis_changed（系统性平移）→ 以新基准整体替换并 warning；
#        - no_overlap（但覆盖起点）→ 直接用新结果。
#      * 起始日 >  缓存起始日（真正的历史截断）→ 保留原缓存并报 degraded。
#      行数变少本身不再是拒绝理由：少 1 根可能只是源端缺点，缓存必须能前进。
# ---------------------------------------------------------------------------
CACHE_OVERLAP_DAYS = 40       # 近端回看天数（覆盖春节等长假，保证有重叠交易日）
OVERLAP_PRICE_TOL = 0.01      # 重叠段价格容差（元；4 位小数舍入）
OVERLAP_VOLUME_TOL = 100.0    # 重叠段成交量容差（股；腾讯按整手取整，上游 #57）
# 源间噪声 vs 复权基准变化（2026-11-12，PR #9 最终判据修正）
#
# 判据不能是「任一重叠日超差 = 基准已变」，也不能是「最大相对差 >0.5% = 基准已变」：
# 后者仍会被**单个异常日**绑架。实盘 600519 缓存历史来自新浪，腾讯全量重取在
# 6040 个重叠交易日里只有 12 天（0.199%）价格差 0.1–0.72 元；其中 2011-12-13
# 相对差 0.6155%、2011-12-15 0.5527% —— 两天就足以把整段判成「系统性平移」，
# 于是整体替换成腾讯 6040 根，丢掉新浪独有的 2007-09-04（策略由唐奇安变 CMF）。
#
# 真正的系统性复权基准变化（除权后 qfq 整体重算）会让**大多数**日期同时超差，
# 而不是 6040 天里的 12 天。因此价格基准改用**分布判据**：
#
#   frac_price = OHLC 逐项超差占比（超差项数 / 4×重叠日数；价格超差 = 绝对差 > 0.01）
#   p95_rel    = 逐日相对价格差 |Δ|/|缓存价| 的 95 分位
#                （只对价格超差日取值，其余日按 0 计入；重叠 <20 日时退回 max，
#                 小样本下 95 分位几乎恒为 0，用 max 才不会漏判）
#
#   consistent   : 价格、成交量全部在容差内
#   source_noise : frac_price <= 1% 且 p95_rel <= 0.5%
#                  —— 少数日子的源间差异，旧历史仍属同一基准，取并集并保留旧值
#   basis_changed: frac_price > 1% 或 p95_rel > 0.5%（系统性平移，如除权后 ×0.98）
#   no_overlap   : 无共同交易日
#
# 成交量与价格基准**分开判断**（成交量没有复权基准，只有单位/口径问题）：
#   frac_vol = 成交量超差日数 / 重叠日数   （超差 = 绝对差 > 100 股）
#     frac_vol > 10%      → 成交量单位/尺度变化，新 tail 不能与旧序列混用，
#                           同样判 basis_changed（走整体替换）
#     0 < frac_vol <= 10% → 只 warning（数据质量提示），不改变价格基准分类
#   成交量超差日不并入 frac_price。
OVERLAP_NOISE_MAX_PRICE_FRAC = 0.01  # OHLC 逐项超差占比上限（1%）
OVERLAP_NOISE_MAX_REL = 0.005        # 相对价格差 95 分位上限（0.5%）
OVERLAP_VOLUME_SCALE_FRAC = 0.10     # 成交量超差日占比超过 10% → 尺度变化
OVERLAP_MIN_SAMPLE = 20              # 重叠日少于此值：p95 退回 max
# 冷启动下限：1990-01-01 早于 A 股任一标的上市日，等价「全量」。
# 用 YYYYMMDD 是因为 eastmoney/sina 通道收这个格式（astock 侧会转成 1990-01-01）。
FULL_HISTORY_START = '19900101'


def _prepare_indexed_frame(raw):
    """渠道原始 DataFrame → DatetimeIndex + 数值列（写盘与合并共用）。

    issue #11（Hermes review 第 3 条）：**所有进入数据集的帧都在这里归一到
    0.01 元报价精度**，不再只对冷启动写盘归一。这样冷启动、近端增量、全量
    重取、参照源四条路径共用同一入口函数，避免「冷启动写 2 位、增量又写回
    4 位」的精度反复。**缓存命中快路径**不经过本函数（帧已带 DatetimeIndex，
    无需重建索引），但会直接调用同一个 `_canonicalize_price_tick`，口径一致。
    适配器层（astock.fetch_daily）仍保留 4 位小数，归一化只发生在数据集组装层。
    """
    full = raw
    if 'date' not in full.columns:
        full = _normalize_daily_df(full)
    full = full.copy()
    full['date'] = pd.to_datetime(full['date'])
    full = full.set_index('date').sort_index()
    for col in ('open', 'high', 'low', 'close', 'volume'):
        if col in full.columns:
            full[col] = pd.to_numeric(full[col], errors='coerce')
    out = full[~full.index.duplicated(keep='last')]
    # `_canonicalize_price_tick` 在本模块稍后定义；函数体在调用时才解析名称，
    # 因此这里可以直接引用（单一入口，杜绝各路径各写一份取整逻辑）。
    return _canonicalize_price_tick(out)


# ---------------------------------------------------------------------------
# issue #11：报价精度归一（**所有数据集入口**共享，非冷启动专用）
#
# 事故（PR #9 之后的复验）：同一天、同一只票，冷启动选中 CMF(30,0.1/-0.1)，
# 有新浪缓存时选中 唐奇安(30/20)。逐项隔离（详见 PR 证据表）证明：
#   * 只补回腾讯缺的 2007-09-04       → 仍是 CMF；
#   * 补根 + 12 个价格异常日全换成新浪 → 仍是 CMF；
#   * 成交量全部换成新浪                → 仍是 CMF；
#   * **只把腾讯复权价四舍五入到 0.01 元** → 变回 唐奇安(30/20)。
# 根因（主因）：腾讯通道返回「原始价 × 新浪比例因子」的**未取整**结果
# （4 位小数），而新浪/东财通道返回按 0.01 元报价单位取整后的价格。同一份
# 底层数据在两条通道里精度不同，CMF 的 money-flow 乘数在临界日被亚分差改变，
# 锦标赛 top-1 随之翻转（CMF 分 0.4897 vs 0.2988；唐奇安 0.3809 基本不变）。
#
# 次因（Hermes 复现）：即使数据已归一到 0.01 元，同一 vintage 下**删掉任意
# 一根**，top-1 仍会从 0.3810 翻到 0.4133。所以缺根不是唯一触发点，但也不是
# 「无关」——它属于同一类「数据集状态改变结论」的问题，由 gapfill 处理。
#
# Hermes review 第 3 条：归一化必须**一致**。旧实现只在冷启动写盘时归一，
# 增量合并又把 4 位小数写回，同一路径反复切换精度。现在统一在
# `_prepare_indexed_frame`（数据集唯一入口）归一，并在 `_merge_with_cache`
# 的合并结果上再归一一次（旧缓存可能来自更早的 4 位版本）。
#
# 适配器 `astock.fetch_daily` 仍保留 4 位小数（其契约测试断言 956.4076），
# 归一化只发生在数据集组装层。
# ---------------------------------------------------------------------------
PRICE_TICK = 0.01


def _canonicalize_price_tick(df):
    """把 OHLC 四舍五入到 0.01 元（幂等；已按报价单位取整的源无变化）。"""
    out = df.copy()
    for col in ('open', 'high', 'low', 'close'):
        if col in out.columns:
            out[col] = pd.to_numeric(out[col], errors='coerce').round(2)
    return out


def _gap_reference_loader(symbol, start_date, adjust, primary_source):
    """冷启动缺口参照源：从**非主源**通道再取一次同区间数据。

    返回 ``(DataFrame|None, source|None)``。优先新浪（一次请求即全历史），
    其次东财，最后腾讯；主源本身不参与（它刚才是缺根的一方）。渠道池会按
    ``preferred`` 排序，冷却/剔除的通道自动跳过。
    """
    order = [name for name in ('sina_stock', 'eastmoney', 'astock_tencent')
             if name != primary_source]
    if not order:
        return None, None
    try:
        raw, used = _STOCK_DATA_SOURCE_POOL.fetch(
            symbol, start_date, adjust, preferred=order)
    except Exception as exc:
        print(f"[WARN] {symbol} 缺口参照源取数异常：{exc}", file=sys.stderr)
        raw, used = None, None
    _capture_source_attempts()
    if raw is None or raw.empty or not used or used == primary_source:
        return None, None
    return _prepare_indexed_frame(raw), used


def _reconcile_cold_start_gaps(full, symbol, fetch_start, adjust, used_source):
    """冷启动数据集缺口检测/补齐；返回 ``(full, gap_info)``。

    仅冷启动调用：有缓存时的旧历史是既成基准，保留旧值/取并集由
    `_merge_with_cache` 负责（见 issue #11 的范围说明）。
    """
    # `_prepare_indexed_frame` 已归一（这里再调一次是幂等的，显式表达冷启动
    # 路径与增量路径共用同一精度口径）。
    frame = _canonicalize_price_tick(full)

    def _reference():
        return _gap_reference_loader(symbol, fetch_start, adjust, used_source)

    def _refetch_from(day):
        # **近端重抓，不是精确窄区间**：渠道池 `fetch()` 只有 start_date，
        # 没有 end_date，实际请求是「缺口日前 5 天 → 最新交易日」。对 2007 年
        # 的缺口这就是一次全历史请求（腾讯分页约 19 次 HTTP）。PR 与 gapfill
        # 文档都如实记录了这一代价，未伪装成窄区间。
        start = (day - pd.Timedelta(days=5)).strftime('%Y%m%d')
        order = [name for name in ('sina_stock', 'eastmoney', 'astock_tencent')
                 if name != used_source]
        raw, used = _STOCK_DATA_SOURCE_POOL.fetch(
            symbol, start, adjust, preferred=order)
        _capture_source_attempts()
        if raw is None or raw.empty or not used or used == used_source:
            return None, None
        return _prepare_indexed_frame(raw), used

    try:
        frame, gap_info = _gapfill.reconcile_gaps(
            frame,
            symbol=symbol,
            reference_loader=_reference,
            calendar_lookup=_astock_source.get_month_calendar,
            refetch_fetcher=_refetch_from,
            calendar_fallback=True,
        )
    except Exception as exc:
        # 缺口核验是**增强**而非主数据通路：任何异常都不得让本来已经拿到的行情失败。
        gap_info = _gapfill.empty_info()
        gap_info['gap_warnings'].append(f"{symbol} 缺口核验异常，已跳过：{exc}")
    for line in gap_info.get('gap_warnings') or []:
        print(f"[WARN] {line}", file=sys.stderr)
    return frame, gap_info


def _overlap_stats(cached, fresh):
    """重叠交易日的超差统计（向量化）。

    价格与成交量**分别**统计（成交量没有复权基准，不能并入价格超差占比）：

      * ``common``       —— 重叠交易日数；
      * ``price_exceed`` —— 价格超差日数（任一 OHLC 绝对差 > 0.01，或任一侧 NaN）；
      * ``vol_exceed``   —— 成交量超差日数（绝对差 > 100 股，或任一侧 NaN）；
      * ``frac_price``   —— OHLC 逐项超差占比（超差项数 / 4×重叠日数）。按列而非
                            按日统计，才能区分「4% 的日期只有一列差 1%」（1% 项，
                            源噪声）与「5% 的日期各列都差 1%」（>1% 项，系统性）；
      * ``p95_rel``      —— 逐日相对价格差 |Δ|/|缓存价| 的 95 分位；
      * ``max_rel``      —— 价格超差日的最大相对差（无超差为 0）；
      * ``frac_vol``     —— vol_exceed / common；
      * ``max_vol_abs`` / ``max_vol_rel`` —— 成交量超差日的最大绝对/相对差；
      * ``exceed``       —— 兼容旧调用方的别名，等于 price_exceed。

    ``p95_rel`` 的口径：在**全部重叠日**上取 95 分位，非超差日按 0 计入 ——
    这样 6040 天里 12 天（0.2%）的源间噪声落在右侧 5% 尾部之外，p95 仍为 0，
    不会被单个异常日绑架。重叠样本 <20 日时 95 分位几乎恒等于最大值，改为
    直接取 max，避免小样本下 p95 退化成 0 而漏判基准变化。

    相对差只在超过**绝对**容差的日子上计算：未超容差的日子不参与，避免低价股
    因为 0.005 元的正常舍入就被判成「相对差 1%」。
    任一侧 NaN 视为超差且相对差 = inf（无法验证的基准不能并入旧序列）。
    """
    empty = {
        'common': 0, 'price_exceed': 0, 'vol_exceed': 0, 'exceed': 0,
        'price_cells': 0, 'price_cells_bad': 0,
        'frac_price': 0.0, 'p95_rel': 0.0, 'max_rel': 0.0,
        'frac_vol': 0.0, 'max_vol_abs': 0.0, 'max_vol_rel': 0.0,
        'price_nan': 0,
    }
    common = cached.index.intersection(fresh.index)
    if len(common) == 0:
        return empty

    a = cached.loc[common]
    b = fresh.loc[common]
    price_bad = pd.Series(False, index=common)
    price_nan = pd.Series(False, index=common)
    rel = pd.Series(0.0, index=common)
    price_cells = 0          # OHLC 逐项比较的样本数（= 4 × 重叠日）
    price_cells_bad = 0

    for col in ('open', 'high', 'low', 'close'):
        if col not in cached.columns or col not in fresh.columns:
            continue
        ca = pd.to_numeric(a[col], errors='coerce').astype(float)
        cb = pd.to_numeric(b[col], errors='coerce').astype(float)
        diff = (ca - cb).abs()
        nan = ca.isna() | cb.isna()
        over = diff > OVERLAP_PRICE_TOL
        price_bad = price_bad | nan | over
        price_nan = price_nan | nan
        price_cells += int(len(common))
        price_cells_bad += int((nan | over).sum())
        with np.errstate(divide='ignore', invalid='ignore'):
            day_rel = diff / ca.abs()
        day_rel = day_rel.where(over & ~nan, 0.0)
        day_rel = day_rel.mask(nan | ((ca.abs() == 0) & over), np.inf)
        rel = np.maximum(rel, day_rel.fillna(0.0))

    vol_bad = pd.Series(False, index=common)
    vol_rel = pd.Series(0.0, index=common)
    vol_abs = pd.Series(0.0, index=common)
    if 'volume' in cached.columns and 'volume' in fresh.columns:
        va = pd.to_numeric(a['volume'], errors='coerce').astype(float)
        vb = pd.to_numeric(b['volume'], errors='coerce').astype(float)
        vol_abs = (va - vb).abs()
        vnan = va.isna() | vb.isna()
        vol_bad = vnan | (vol_abs > OVERLAP_VOLUME_TOL)
        with np.errstate(divide='ignore', invalid='ignore'):
            day_vrel = vol_abs / va.abs()
        day_vrel = day_vrel.where(vol_bad & ~vnan, 0.0)
        day_vrel = day_vrel.mask(vnan | ((va.abs() == 0) & vol_bad), np.inf)
        vol_rel = day_vrel.fillna(0.0)

    n = int(len(common))
    price_exceed = int(price_bad.sum())
    vol_exceed = int(vol_bad.sum())
    rel_values = rel.to_numpy(dtype=float)

    if price_exceed == 0:
        max_rel = p95_rel = 0.0
    else:
        max_rel = float(np.max(rel_values))
        if n < OVERLAP_MIN_SAMPLE:
            p95_rel = max_rel
        else:
            # 'higher'：取不低于 95% 位置的那个实际观测值，边界（恰好 5% 的日期
            # 超差）不会被线性插值稀释。
            p95_rel = float(np.quantile(rel_values, 0.95, method='higher'))

    if vol_exceed == 0:
        max_vol_abs = max_vol_rel = 0.0
    else:
        max_vol_abs = float(vol_abs.to_numpy()[vol_bad.to_numpy()].max())
        max_vol_rel = float(vol_rel.to_numpy()[vol_bad.to_numpy()].max())

    return {
        'common': n,
        'price_exceed': price_exceed,
        'vol_exceed': vol_exceed,
        'exceed': price_exceed,          # 兼容旧调用方
        'price_cells': price_cells,
        'price_cells_bad': price_cells_bad,
        'frac_price': (price_cells_bad / price_cells) if price_cells else 0.0,
        'p95_rel': p95_rel,
        'max_rel': max_rel,
        'frac_vol': vol_exceed / n,
        'max_vol_abs': max_vol_abs,
        'max_vol_rel': max_vol_rel,
        'price_nan': int(price_nan.sum()),
    }


def _classify_overlap(cached, fresh, stats=None):
    """重叠段分类：'consistent' / 'source_noise' / 'basis_changed' / 'no_overlap'。

    旧实现按「任一超差日」判基准变化，后来按「超差占比 + 最大相对差」，两者都
    会被**单个异常日**绑架（12/6040 的源间噪声里有两天相对差 0.55–0.62%）。
    现按**分布判据**：大多数日期同时超差才是系统性复权基准变化。

    价格基准：
      * frac_price > 1%  或
      * p95_rel > 0.5%   或
      * 存在无法验证的价格 NaN（相对差 inf）
      → basis_changed；否则 source_noise。

    成交量尺度（与价格基准分开）：
      * frac_vol > 10% → 成交量单位/口径变化，tail 不能与旧序列混用 → basis_changed；
      * 0 < frac_vol <= 10% → 只 warning，不改变价格基准分类。

    ``stats``（可选 dict）会被填入 ``_overlap_stats`` 的统计，供调用方打日志。
    """
    s = _overlap_stats(cached, fresh)
    if stats is not None:
        stats.update(s)
    if s['common'] == 0:
        return 'no_overlap'
    if s['price_exceed'] == 0 and s['vol_exceed'] == 0:
        return 'consistent'

    # 用整数比较，避开 1/100、10/100 这类浮点边界误差：
    #   frac_price > 1%  <=>  price_cells_bad * 100 > price_cells
    #   frac_vol   > 10% <=>  vol_exceed * 10      > common
    price_frac_changed = (s['price_cells_bad'] * 100 > s['price_cells']) \
        if s['price_cells'] else False
    price_rel_changed = (not np.isfinite(s['p95_rel'])
                         or s['p95_rel'] > OVERLAP_NOISE_MAX_REL)
    vol_scale_changed = s['vol_exceed'] * 10 > s['common']
    if (price_frac_changed or price_rel_changed or vol_scale_changed
            or s['price_nan'] > 0):
        return 'basis_changed'
    return 'source_noise'


def _log_overlap_verdict(label, cls, s, stage=''):
    """把完整判据打到 stderr（可审计：分类不靠猜）。"""
    def _pct(v):
        return 'inf' if not np.isfinite(v) else f'{v:.3%}'

    print(f"[INFO] {label} {stage}重叠判据：重叠 {s['common']} 日；"
          f"价格超差 {s['price_exceed']} 日（frac_price={_pct(s['frac_price'])}），"
          f"p95_rel={_pct(s['p95_rel'])}，max_rel={_pct(s['max_rel'])}；"
          f"成交量超差 {s['vol_exceed']} 日（frac_vol={_pct(s['frac_vol'])}）；"
          f"分类={cls}", file=sys.stderr)
    if 0 < s['vol_exceed'] and s['frac_vol'] <= OVERLAP_VOLUME_SCALE_FRAC:
        print(f"[WARN] {label} 成交量异常 {s['vol_exceed']} 日"
              f"（最大绝对差 {s['max_vol_abs']:.0f} 股、最大相对差 "
              f"{_pct(s['max_vol_rel'])}），仅数据质量提示，不改变价格基准分类",
              file=sys.stderr)


def _merge_with_cache(cached, fresh, label, cls='consistent'):
    """旧缓存与本次结果取并集，返回 (merged, 只存在于旧缓存的行数)。

    并集语义（2026-11-12 修正）：**重叠日保留旧缓存值**，只追加缓存末日之后
    的新行；旧缓存独有的历史根保留（腾讯不复权日线缺 2007-09-04 这类单点缺口
    不因刷新增量而丢失）。不用新值覆盖重叠日，是因为本次来源可能与旧缓存历史
    来自不同渠道（新浪历史 + 腾讯刷新），两套基准存在 0.01–0.72 元的源间噪声；
    保留旧值可让既有序列维持单一基准，也满足「源噪声不污染旧历史」的要求。
    """
    new_tail = fresh[fresh.index > cached.index[-1]]
    merged = pd.concat([cached, new_tail])
    merged = merged[~merged.index.duplicated(keep='first')].sort_index()
    # 旧缓存可能由更早版本写入（4 位小数）。合并结果在写盘/返回前再归一一
    # 次，保证「冷启动路径」与「增量路径」对同一天产出逐值相等的序列
    # （Hermes review 第 3 条验收口径）。
    merged = _canonicalize_price_tick(merged)
    kept = int(len(cached.index.difference(fresh.index)))
    print(f"[WARN] {label} 增量合并：保留旧缓存 {kept} 行"
          f"（本次源端 {len(fresh)} 行，合并后 {len(merged)} 行；重叠分类={cls}）",
          file=sys.stderr)
    return merged, kept


def get_stock_daily(symbol, start_date='20200101', adjust='qfq', source='akshare'):
    """获取股票日线数据（前复权）。

    缓存策略（本轮修正）：
    * 新鲜度按**期望最新交易日**判断（跳过周末、收盘前不算当天），
      非交易日不再无谓触网（原实现用 today-1，周日在周六→必然触网失败再回退）；
    * 缓存命中时**仍按 start_date 截取**（原实现完全忽略该参数）；
    * 写盘一律写**未截断的全量**（原实现把 start 过滤后的子集写回，
      一次 `--start 2024` 就会把 2020–2023 的缓存永久削掉）；
    * 未指定 start_date 时按**全量历史**取数（FULL_HISTORY_START），不再被
      适配器默认的 2020-01-01 截断；过期缓存走近端增量 + 合并，写盘以**覆盖
      范围**为准（见上面 CACHE_OVERLAP_DAYS 一节的不变量），不因源端少一根而
      拒绝推进缓存。
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
                    # issue #11（Hermes 复验遗留）：缓存命中是**快路径**，但同样
                    # 必须过报价精度归一。旧实现直接返回 CSV 原样，导致更早版本
                    # 写下的 4 位小数缓存一直保持 4 位（实测 480/480 单元），
                    # 与「所有进入数据集的帧都在 _prepare_indexed_frame 归一」的
                    # 契约不符，同一份数据在缓存命中/冷启动两条路径上精度不一致。
                    # 归一幂等且只动 OHLC 列，DatetimeIndex、volume、turnover
                    # 与 `_apply_start` 的 start_date 截取语义均不受影响。
                    return _apply_start(_canonicalize_price_tick(df), start_date)
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
    _capture_source_attempts()

    if raw is not None and not raw.empty:
        try:
            full = _prepare_indexed_frame(raw)
            merged_from_cache = False
            cache_rows_kept = 0
            # issue #11：报价精度归一只在 `_prepare_indexed_frame` 这一入口做，
            # 冷启动 / 增量 / 全量重取 / 参照源四条路径共用（见该函数注释）。
            # 缺口检测只在冷启动做：有缓存时旧历史是既成基准，不重新核验
            # （避免把源间噪声混进旧序列，见 _merge_with_cache）。
            gap_info = _gapfill.empty_info()
            if not incremental:
                full, gap_info = _reconcile_cold_start_gaps(
                    full, symbol, fetch_start, adjust, used_source)
            if incremental:
                cls_stats = {}
                cls = _classify_overlap(_stale_cache, full, cls_stats)
                _log_overlap_verdict(f'stock_{symbol}', cls, cls_stats, '增量段')
                if cls in ('consistent', 'source_noise'):
                    full, cache_rows_kept = _merge_with_cache(
                        _stale_cache, full, f'stock_{symbol}', cls)
                    merged_from_cache = True
                else:
                    # 近端窗口无法与旧缓存直接拼接 → 从缓存首日全量重取。
                    # 是否接受这份全量结果，取决于它**是否覆盖缓存起始日**
                    # （覆盖范围不变量），而不是行数多少：行数更少可能只是
                    # 源端少一根（如腾讯缺 2007-09-04），不代表历史被截断。
                    reason = ('重叠段复权基准已变' if cls == 'basis_changed'
                              else '与旧缓存无重叠')
                    print(f"[WARN] {symbol} 增量{reason}（分类={cls}，"
                          f"重叠 {cls_stats['common']} 日、价格超差 "
                          f"{cls_stats['price_exceed']} 日、成交量超差 "
                          f"{cls_stats['vol_exceed']} 日），"
                          f"改为从缓存首日 "
                          f"{pd.Timestamp(_stale_cache.index[0]).date()} 全量重取", file=sys.stderr)
                    raw2, used2 = None, None
                    try:
                        raw2, used2 = _STOCK_DATA_SOURCE_POOL.fetch(
                            symbol, pd.Timestamp(_stale_cache.index[0]).strftime('%Y%m%d'),
                            adjust, preferred=_SOURCE_PREFERENCE.get(source))
                    except Exception as e:
                        print(f"渠道池全量重取异常：{e}", file=sys.stderr)
                    _capture_source_attempts()
                    if raw2 is not None and not raw2.empty:
                        fresh_full = _prepare_indexed_frame(raw2)
                        used_source = used2 or used_source
                        if fresh_full.index[0] > _stale_cache.index[0]:
                            # 真正的历史截断：新结果起始日晚于缓存首日，覆盖不了
                            # 旧历史 → 历史一旦丢失不可逆，保留原缓存并降级。
                            _mark_degraded(
                                f"{symbol} 全量重取起始日 "
                                f"{pd.Timestamp(fresh_full.index[0]).date()} 晚于缓存首日 "
                                f"{pd.Timestamp(_stale_cache.index[0]).date()}"
                                f"（比本地缓存短或缺根），保留原缓存",
                                _stale_cache)
                            return _apply_start(_stale_cache, start_date)
                        recon_stats = {}
                        recon = _classify_overlap(_stale_cache, fresh_full, recon_stats)
                        _log_overlap_verdict(f'stock_{symbol}', recon, recon_stats,
                                             '全量重取段')
                        if recon == 'basis_changed':
                            # 系统性平移（除权/分红后 qfq 整体重算）：新基准才是正确
                            # 基准，整体采用新结果，绝不把旧基准混进序列。
                            print(f"[WARN] {symbol} 全量重取与旧缓存重叠段不一致"
                                  f"（复权基准已变，分类={recon}，重叠 "
                                  f"{recon_stats['common']} 日、价格超差 "
                                  f"{recon_stats['price_exceed']} 日、成交量超差 "
                                  f"{recon_stats['vol_exceed']} 日、"
                                  f"frac_price={recon_stats['frac_price']:.4%}、"
                                  f"p95_rel={recon_stats['p95_rel']:.4%}、"
                                  f"frac_vol={recon_stats['frac_vol']:.4%}），"
                                  f"采用新基准并整体替换旧缓存",
                                  file=sys.stderr)
                            full = fresh_full
                        elif recon in ('consistent', 'source_noise'):
                            # 覆盖缓存起始日，且差异只是源间噪声（或完全一致）
                            # → 取并集，保留源端缺失的历史根与旧值。
                            full, cache_rows_kept = _merge_with_cache(
                                _stale_cache, fresh_full, f'stock_{symbol}', recon)
                            merged_from_cache = True
                        else:
                            # 无重叠但已确认覆盖缓存起始日（理论边界）→ 用新结果
                            print(f"[INFO] {symbol} 全量重取与旧缓存无重叠"
                                  f"（分类={recon}），采用新结果", file=sys.stderr)
                            full = fresh_full
                    else:
                        print(f"全量重取失败，回退到本地旧缓存", file=sys.stderr)
                        _mark_degraded(f"{symbol} 增量{reason}且全量重取失败，回退到本地旧缓存",
                                       _stale_cache)
                        return _apply_start(_stale_cache, start_date)

            # 写盘用全量，返回用截断窗口
            _write_cache(full, cache_file, f'stock_{symbol}')
            _record_fetch(used_source or 'unknown', False,
                          cache_last_date=(pd.Timestamp(full.index[-1]).date().isoformat()
                                           if len(full) else None),
                          lag_days=0,
                          expected_date=expected.isoformat(),
                          requested_source=source,
                          merged_from_cache=merged_from_cache,
                          cache_rows_kept=cache_rows_kept,
                          **gap_info)
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
    # P0（issue #24）：基金 ETF / OTC 通道的逐次尝试，结构与股票渠道池一致
    # （{source, ok, error_class, error_text, cooldown, removed}）。基金没有渠道池，
    # 因此 cooldown/removed 恒为 False —— 没有的状态不虚构。
    fund_attempts = []

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
            fund_attempts.append(_attempt('fund_etf_sina', True))
            df, fund_channel = etf, 'fund_etf_sina'
        else:
            # 场外基金（023299/001917…）走场内 ETF 通道常态就是 0 行：这是
            # 「不适用」而非故障。仍结构化留痕，但同轮 OTC 成功时不会变成噪声。
            fund_attempts.append(_attempt(
                'fund_etf_sina', False,
                error=EmptyDataError('fund_etf_sina 无数据（非场内 ETF）')))
            print(f"ETF 通道无数据（{symbol}），尝试 OTC 净值通道", file=sys.stderr)
    except Exception as e:
        fund_attempts.append(_attempt('fund_etf_sina', False, error=e))
        print(f"ETF 通道失败：{e}；尝试 OTC 净值通道", file=sys.stderr)

    # --- 通道 2：场外 OTC 单位净值（东财）
    if df is None or df.empty:
        try:
            import akshare as ak  # 已在上面 import，此处防御 ak 未定义情形
            otc = ak.fund_open_fund_info_em(symbol=symbol, indicator="单位净值走势")
            if otc is not None and not otc.empty:
                fund_attempts.append(_attempt('eastmoney_otc', True))
                df, fund_channel = otc, 'eastmoney_otc'
            else:
                fund_attempts.append(_attempt(
                    'eastmoney_otc', False,
                    error=EmptyDataError('eastmoney_otc 无数据')))
        except Exception as e:
            fund_attempts.append(_attempt('eastmoney_otc', False, error=e))
            print(f"OTC 净值通道失败：{e}", file=sys.stderr)

    _LAST_FETCH_INFO['source_attempts'] = [dict(item) for item in fund_attempts]

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


INDEX_CACHE_PREFIX = 'index_'
# 指数缓存「新鲜」判据：末根日期 >= 期望最新交易日。与个股共用
# _expected_latest_trade_date（官方日历优先、离线回退周末推算）。
# 指数日线在收盘后才落定，故与个股同一口径，不额外放宽。
INDEX_CACHE_OVERLAP_DAYS = 40   # 增量回看天数，与个股 CACHE_OVERLAP_DAYS 一致


def _index_cache_path(symbol):
    """指数缓存文件：``data/index_<sh000300>.csv``（data/ 已在 .gitignore）。"""
    safe = str(symbol).strip().lower().replace('/', '_').replace('\\', '_')
    return os.path.join(DATA_DIR, f'{INDEX_CACHE_PREFIX}{safe}.csv')


def get_market_index_daily(symbol='000985', start_date='20200101', source='akshare'):
    """获取指数日线（P1-3，issue #32）。

    数据源：腾讯指数 K 线（``astock.fetch_index_daily``，§1.2），默认沪深 300。
    缓存：``source/stock_analysis/data/index_<symbol>.csv``（gitignore）。
    失败：**返回 None**（不抛），并在 stderr 打 ``[WARN]``；调用方据此降级，
    绝不阻断主流程 —— 指数 regime 只展示、不参与决策。

    返回 ``DataFrame``：``DatetimeIndex`` + ``open/high/low/close/volume``，
    按 ``start_date`` 截取。``None`` 表示取不到（离线/网络/格式/空数据）。

    缓存策略比个股简单（个股那套增量合并是为复权基准变化服务的，指数不复权，
    没有基准问题）：新鲜则直接用；过期则拉「末根 - 40 天」近端窗口，与旧缓存
    按日期并集（重叠日以新值为准）。取数失败时回退旧缓存并 warning，不推进缓存。
    """
    from stock_analysis.data_sources import astock

    cache_file = _index_cache_path(symbol)
    cached = None
    if os.path.exists(cache_file):
        try:
            # 用位置索引而不是 index_col='date'：缓存若由外部工具写入、首列表头为空，
            # 按名字取列会抛 "list.index(x): x not in list"，这里只依赖列顺序。
            cached = pd.read_csv(cache_file, index_col=0, parse_dates=True)
            if cached.empty:
                cached = None
            elif len(cached) < MIN_CACHE_ROWS:
                print(f"[WARN] 指数缓存 {cache_file} 仅 {len(cached)} 行，判定为空壳并删除",
                      file=sys.stderr)
                os.remove(cache_file)
                cached = None
        except Exception as e:
            print(f"[WARN] 读取指数缓存 {cache_file} 失败：{e}", file=sys.stderr)
            cached = None

    if cached is not None:
        try:
            last_date = pd.Timestamp(cached.index[-1]).date()
            if last_date >= _expected_latest_trade_date():
                return _apply_start(cached, start_date)
        except Exception:
            pass

    # 过期缓存只拉近端窗口；冷启动拉 2020 起（regime 只要 60 根，不需要全历史）
    fetch_start = (pd.Timestamp(cached.index[-1]) - pd.Timedelta(days=INDEX_CACHE_OVERLAP_DAYS)
                   ).strftime('%Y%m%d') if cached is not None else str(start_date or '20200101')
    try:
        fresh = astock.fetch_index_daily(symbol, start_date=fetch_start)
    except Exception as e:
        fresh = None
        reason = f"指数取数失败（{symbol}）：{e}"
        if cached is not None:
            reason += f"；回退本地缓存（末日 {pd.Timestamp(cached.index[-1]).date()}）"
            print(f"[WARN] {reason}", file=sys.stderr)
            return _apply_start(cached, start_date)
        print(f"[WARN] {reason}", file=sys.stderr)
        return None

    if fresh is None or fresh.empty:
        if cached is not None:
            print(f"[WARN] 指数取数为空（{symbol}）；回退本地缓存", file=sys.stderr)
            return _apply_start(cached, start_date)
        print(f"[WARN] 指数取数为空（{symbol}）", file=sys.stderr)
        return None

    # 与旧缓存并集：重叠日以新值为准（指数不复权，不存在基准平移）
    merged = fresh
    if cached is not None:
        try:
            keep = cached.loc[~cached.index.isin(fresh.index)]
            merged = pd.concat([keep, fresh]).sort_index()
            merged = merged[~merged.index.duplicated(keep='last')]
        except Exception as e:
            print(f"[WARN] 指数缓存合并失败，改用新结果：{e}", file=sys.stderr)
            merged = fresh

    _write_cache(merged, cache_file, f'index_{symbol}')
    return _apply_start(merged, start_date)


def get_index_regime(symbol='sh000300', start_date=None):
    """指数 regime（P1-3）：**复用** ``analyzer._regime_from_row`` 的同一套规则。

    返回 ``{'label', 'confidence', 'as_of', 'definition', 'source', 'bars'}``；
    取不到指数日线或行数不足 60 根时返回 ``None``（调用方写 null + warning），
    绝不编造标签。``definition`` 与个股 ``market_regime`` 用同一个字符串，
    便于调用方确认二者是同一判定口径、不同输入。

    这里**不复制**判定逻辑：MA20/MA60/ret20/ret60/年化波动全部按
    ``analyzer.get_current_market_regime`` 的算法算，然后调用同一个
    ``_regime_from_row``。区别只在输入是**指数**日线而不是个股日线。
    """
    from stock_analysis.advisor.analyzer import get_current_market_regime

    df = get_market_index_daily(symbol, start_date=start_date)
    if df is None or len(df) < 60:
        return None
    try:
        label, conf = get_current_market_regime(df)
    except Exception as e:
        print(f"[WARN] 指数 regime 计算失败（{symbol}）：{e}", file=sys.stderr)
        return None
    if str(label) == 'Unknown':
        return None
    return {
        'label': str(label),
        'confidence': round(float(conf), 3),
        'as_of': str(df.index[-1]),
        # 与个股 market_regime.definition 同一字符串：同一判定规则、不同输入
        'definition': 'ma20_ma60_return_volatility',
        'source': f'tencent_index:{symbol}',
        'bars': int(len(df)),
    }


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
    # P0-4：第二次探测（通常是基金）会 _reset_fetch_info，把第一次的渠道明细冲掉。
    # 先快照，待两侧都失败时合并，否则"断源演练"里股票池的 4 条 attempts 全部丢失。
    first_attempts = [dict(item) for item in
                      (_LAST_FETCH_INFO.get('source_attempts') or [])]
    if df is not None and len(df) >= 100:
        return df, first, None

    alt = 'stock' if first == 'fund' else 'fund'
    alt_df = _fetch(alt)
    if alt_df is not None and len(alt_df) >= 100:
        print(f"[INFO] {code} 以 {first} 取数失败，已自动按 {alt} 取数成功"
              f"（如需固定，请用 --type {alt}）", file=sys.stderr)
        return alt_df, alt, None

    alt_attempts = [dict(item) for item in
                    (_LAST_FETCH_INFO.get('source_attempts') or [])]
    merged_attempts = first_attempts + [
        item for item in alt_attempts if item not in first_attempts]
    # 两侧都没拿到足量数据：若首选有数据（只是不足 100 根）就返回它，让上层判 insufficient_data
    if df is not None:
        _LAST_FETCH_INFO['source_attempts'] = merged_attempts
        return df, first, None
    _LAST_FETCH_INFO['source_attempts'] = merged_attempts
    return None, first, (
        f"自动判定失败：按 stock 与 fund 两种类型均无法取到 {code} 的数据。"
        f"请用 --type stock 或 --type fund 显式指定。")
