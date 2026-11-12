try:
    import akshare as ak
except ImportError:  # P2: 离线/最小依赖时允许仅用本地缓存回退
    ak = None
import pandas as pd
import numpy as np
import datetime
import os
import re
import sys
import requests
import time
from dataclasses import dataclass, field
from threading import Lock
from typing import Callable, Optional

from stock_analysis.data_sources import astock as _astock_source

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(PROJECT_ROOT, 'data')

if not os.path.exists(DATA_DIR):
    os.makedirs(DATA_DIR)

DATA_SOURCE_COOLDOWN_SECONDS = int(os.getenv("DATA_SOURCE_COOLDOWN_SECONDS", "120"))
DATA_SOURCE_MAX_CONSECUTIVE_FAILURES = int(os.getenv("DATA_SOURCE_MAX_CONSECUTIVE_FAILURES", "3"))
DATA_SOURCE_MAX_TOTAL_FAILURES = int(os.getenv("DATA_SOURCE_MAX_TOTAL_FAILURES", "8"))
DATA_SOURCE_FAILURE_WINDOW_SECONDS = int(os.getenv("DATA_SOURCE_FAILURE_WINDOW_SECONDS", "600"))
RATE_LIMIT_KEYWORDS = (
    "429", "too many", "frequency", "limit", "反爬", "频率", "限制", "forbidden",
)
TIMEOUT_KEYWORDS = ("timeout", "timed out", "超时", "read timed out", "connect timeout")


# ---------------------------------------------------------------------------
# P0-4（issue #7）：逐通道取数尝试记录
#
# 旧实现只保留 `last_err`，前几个通道的错误在日志里一闪而过，调用方（Hermes）
# 只看得到"全部失败"或"回退缓存"，无法执行「先报问题再报建议」。
# 这里把每次尝试结构化：{source, ok, error_class, error_text, cooldown, removed}。
#   * ok=True  → 本次成功
#   * ok=False → 本次真实调用失败（error_class/error_text 必有）
#   * ok=None  → 未调用：通道在冷却中或已被剔除（由 cooldown/removed 说明）
# 错误文本经**脱敏 + 截断**，避免把长 URL / token / key 带进 diagnostics。
# ---------------------------------------------------------------------------
MAX_ATTEMPT_ERROR_TEXT = 200
_URL_PATTERN = re.compile(r'https?://\S+', re.IGNORECASE)
# requests 的异常常写成 `... with url: /api/qt/...?fields1=...`（相对路径，
# 不匹配上面的绝对 URL 规则），同样可能带长查询串 / 参数，一并脱敏。
_RELATIVE_URL_PATTERN = re.compile(r'(?i)\burl:\s*\S+')
_SECRET_PATTERN = re.compile(
    r'(?i)\b(token|api[_-]?key|access[_-]?key|secret|authorization|password)'
    r'\b\s*[=:]\s*[^\s,;&]+'
)
_SAFE_SOURCE_PATTERN = re.compile(r'^[A-Za-z0-9_.-]{1,32}$')


def _safe_source_name(name) -> str:
    """来源白名单：只允许池内登记的简单标识符，异常输入一律显示 unknown。"""
    text = str(name or '')
    return text if _SAFE_SOURCE_PATTERN.match(text) else 'unknown'


def _sanitize_error_text(err) -> str:
    """错误文本脱敏：去 URL、去 token/key、压缩空白、截断 ≤200 字。"""
    text = '' if err is None else str(err)
    text = _URL_PATTERN.sub('<url>', text)
    text = _RELATIVE_URL_PATTERN.sub('url: <url>', text)
    text = _SECRET_PATTERN.sub(r'\1=<redacted>', text)
    text = ' '.join(text.split())
    if len(text) > MAX_ATTEMPT_ERROR_TEXT:
        text = text[:MAX_ATTEMPT_ERROR_TEXT - 3] + '...'
    return text


class EmptyDataError(RuntimeError):
    """通道正常响应但**没有数据**（与「调用失败」区分开）。

    P0（issue #24）：场外基金走场内 ETF 通道（``ak.fund_etf_hist_sina``）时，
    正常结果就是 0 行 —— 这不是故障，而是「该代码不是场内 ETF」的正常答案。
    用独立异常类把它和网络/解析失败分开：

      * 仍按 ``{source, ok=False, error_class='EmptyDataError', ...}`` 结构化留痕，
        满足「逐通道可见」；
      * ``format_attempt_warnings`` 在**同轮有其它通道成功**时不把它当噪声输出，
        避免 023299/001917 这类场外基金的正常路径被无意义的失败条目刷屏。

    股票渠道池的空结果仍用 ``RuntimeError('... 返回空数据')``，语义不变。
    """


def _attempt(source, ok, error=None, cooldown=False, removed=False,
             cooldown_seconds=None):
    record = {
        'source': _safe_source_name(source),
        'ok': ok,
        'error_class': None if error is None else type(error).__name__,
        'error_text': None if error is None else _sanitize_error_text(error),
        'cooldown': bool(cooldown),
        'removed': bool(removed),
    }
    if cooldown and cooldown_seconds:
        record['cooldown_seconds'] = int(cooldown_seconds)
    return record


def format_attempt_warnings(attempts):
    """把 source_attempts 转成可读中文 warnings。

    只在失败 / 冷却 / 剔除时产出条目；全通道首试成功时返回空列表
    （P0-4 要求：成功路径不新增噪声）。

    特例（issue #24）：``EmptyDataError`` 表示「通道正常响应但无数据」，典型场景
    是场外基金走场内 ETF 通道（023299/001917 等常态 0 行）。当同轮**已有通道成功**
    时它是预期内的「不适用」，不再输出噪声；只有整轮全部失败时才作为诊断信息呈现。
    """
    items = list(attempts or [])
    any_ok = any(item.get('ok') is True for item in items)
    lines = []
    for item in items:
        source = _safe_source_name(item.get('source'))
        if item.get('ok') is True:
            continue
        if any_ok and item.get('error_class') == 'EmptyDataError':
            continue
        if item.get('ok') is None:
            reason = '已从渠道池剔除' if item.get('removed') else '冷却中'
            lines.append(f"{source} {reason}，本轮跳过")
            continue
        detail = ' '.join(
            str(x) for x in (item.get('error_class'), item.get('error_text')) if x)
        msg = f"{source} 失败：{detail or '未知错误'}"
        flags = []
        if item.get('cooldown'):
            seconds = item.get('cooldown_seconds')
            flags.append(f"本轮已冷却 {seconds}s" if seconds else "本轮已冷却")
        if item.get('removed'):
            flags.append("已从渠道池剔除")
        if flags:
            msg += f"（{'，'.join(flags)}）"
        lines.append(msg)
    return lines


def _to_exchange_symbol(symbol: str) -> str:
    if symbol.startswith(('6', '5', '9')):
        return 'sh' + symbol
    if symbol.startswith(('0', '3', '1', '2')):
        return 'sz' + symbol
    if symbol.startswith(('4', '8')):
        return 'bj' + symbol
    return symbol


def get_stock_name(symbol):
    try:
        url = f"https://hq.sinajs.cn/list={_to_exchange_symbol(symbol)}"
        headers = {'Referer': 'http://finance.sina.com.cn', 'User-Agent': 'Mozilla/5.0'}
        response = requests.get(url, headers=headers, timeout=5)
        if response.status_code == 200:
            text = response.text
            if '="' in text:
                content = text.split('="')[1]
                if content:
                    name = content.split(',')[0]
                    return name.strip() or None
    except Exception as e:
        print(f"获取名称失败 {symbol}: {e}", file=sys.stderr)
    return None


@dataclass
class _SourceState:
    name: str
    fetcher: Callable[[str, str, str], pd.DataFrame]
    cooldown_until: float = 0.0
    consecutive_failures: int = 0
    total_failures: int = 0
    total_successes: int = 0
    disabled: bool = False
    failure_timestamps: list[float] = field(default_factory=list)


class _DataSourcePool:
    def __init__(self, sources, cooldown_seconds, max_consecutive_failures, max_total_failures, failure_window_seconds):
        self._states = [_SourceState(name=name, fetcher=fetcher) for name, fetcher in sources]
        self._cooldown_seconds = cooldown_seconds
        self._max_consecutive_failures = max_consecutive_failures
        self._max_total_failures = max_total_failures
        self._failure_window_seconds = failure_window_seconds
        self._cursor = 0
        self._lock = Lock()
        # P0-4：最近一次 fetch 的逐通道尝试明细（含成功 / 失败 / 冷却 / 剔除）。
        # 用属性而不是改返回值，保持既有 `df, used = pool.fetch(...)` 调用不变。
        self.last_attempts = []

    def _is_transient_failure(self, err):
        msg = str(err).lower()
        return any(k in msg for k in RATE_LIMIT_KEYWORDS) or any(k in msg for k in TIMEOUT_KEYWORDS)

    def _ordered_candidates(self, preferred=None):
        now = time.time()
        with self._lock:
            available = [s for s in self._states if not s.disabled and s.cooldown_until <= now]
            if not available:
                return []
            if self._cursor >= len(available):
                self._cursor = 0
            ordered = available[self._cursor:] + available[:self._cursor]
            self._cursor = (self._cursor + 1) % len(available)
        if preferred:
            rank = {name: i for i, name in enumerate(preferred)}
            ordered.sort(key=lambda s: rank.get(s.name, len(rank)))
        return ordered

    def _mark_success(self, state):
        with self._lock:
            state.total_successes += 1
            state.consecutive_failures = 0

    def _mark_failure(self, state, err):
        """记录失败；返回 (是否进入冷却, 是否被剔除) 供 attempts 使用。"""
        now = time.time()
        transient = self._is_transient_failure(err)
        with self._lock:
            state.total_failures += 1
            state.consecutive_failures += 1
            state.failure_timestamps.append(now)
            cutoff = now - self._failure_window_seconds
            state.failure_timestamps = [ts for ts in state.failure_timestamps if ts >= cutoff]
            if transient:
                state.cooldown_until = now + self._cooldown_seconds
            frequent_failures = len(state.failure_timestamps) >= self._max_total_failures
            should_disable = state.consecutive_failures >= self._max_consecutive_failures or frequent_failures
            if should_disable:
                state.disabled = True
        if transient:
            print(f"[WARN] 数据源 {state.name} 触发冷却 {self._cooldown_seconds}s: {err}",
                  file=sys.stderr)
        if should_disable:
            print(f"[WARN] 数据源 {state.name} 失败过多，已从渠道池移除", file=sys.stderr)
        return transient, should_disable

    def _publish_attempts(self, attempts):
        with self._lock:
            self.last_attempts = [dict(item) for item in attempts]

    def fetch(self, symbol, start_date, adjust, preferred=None):
        attempts = []
        # 先清空，避免上一轮明细在异常提前退出时被误当成本轮结果。
        self._publish_attempts(attempts)
        now = time.time()
        with self._lock:
            all_states = list(self._states)
        candidates = self._ordered_candidates(preferred)
        candidate_names = {state.name for state in candidates}
        # 没被调用的通道也要留痕：冷却中 / 已剔除对调用方同样是"为什么没拿到数"。
        for state in all_states:
            if state.name in candidate_names:
                continue
            cooling = (not state.disabled) and state.cooldown_until > now
            if state.disabled:
                attempts.append(_attempt(
                    state.name, None,
                    error=RuntimeError('已从渠道池剔除'), removed=True))
            elif cooling:
                remaining = max(0, int(round(state.cooldown_until - now)))
                attempts.append(_attempt(
                    state.name, None,
                    error=RuntimeError(f'冷却中（剩余 {remaining}s）'),
                    cooldown=True, cooldown_seconds=remaining))
        if not candidates:
            self._publish_attempts(attempts)
            print("[WARN] 渠道池无可用数据源（均在冷却或已剔除）", file=sys.stderr)
            return None, None
        last_err = None
        for state in candidates:
            try:
                df = state.fetcher(symbol, start_date, adjust)
                if df is None or df.empty:
                    raise RuntimeError(f"{state.name} 返回空数据")
                self._mark_success(state)
                attempts.append(_attempt(state.name, True))
                self._publish_attempts(attempts)
                return df, state.name
            except Exception as err:
                last_err = err
                cooldown, removed = self._mark_failure(state, err)
                attempts.append(_attempt(
                    state.name, False, error=err,
                    cooldown=cooldown, removed=removed,
                    cooldown_seconds=self._cooldown_seconds if cooldown else None))
        self._publish_attempts(attempts)
        if last_err:
            print(f"[WARN] 渠道池全部失败 {symbol}: {last_err}", file=sys.stderr)
        return None, None


def _normalize_daily_df(df):
    normalized = df.copy()
    normalized = normalized.rename(columns={
        '日期': 'date', '开盘': 'open', '收盘': 'close',
        '最高': 'high', '最低': 'low', '成交量': 'volume',
        '成交额': 'amount', '换手率': 'turnover',
    })
    if 'date' not in normalized.columns:
        normalized = normalized.reset_index()
        if 'date' not in normalized.columns and 'index' in normalized.columns:
            normalized = normalized.rename(columns={'index': 'date'})
        elif 'date' not in normalized.columns:
            first_col = normalized.columns[0]
            normalized = normalized.rename(columns={first_col: 'date'})
    required_cols = {'date', 'open', 'close', 'high', 'low'}
    missing = [col for col in required_cols if col not in normalized.columns]
    if missing:
        raise ValueError(f"数据缺少关键列: {missing}")
    return normalized


def _fetch_from_astock_tencent(symbol, start_date, adjust):
    """a-stock-data 腾讯 K 线（固定快照；§1.2 原始价 + §1.6 比例前复权）。

    返回 DatetimeIndex + OHLCV；meta 的 `_normalize_daily_df` 会把索引还原成
    `date` 列后走与其它通道完全相同的规范化/缓存路径。
    """
    return _astock_source.fetch_daily(symbol, start_date=start_date, adjust=adjust)


def _fetch_from_eastmoney(symbol, start_date, adjust):
    if ak is None:
        raise RuntimeError("akshare未安装，无法在线拉取(eastmoney)")
    df = ak.stock_zh_a_hist(symbol=symbol, period="daily", start_date=start_date, adjust=adjust)
    if df is None or df.empty:
        raise RuntimeError("eastmoney 无数据")
    return _normalize_daily_df(df)


def _fetch_from_sina_stock(symbol, start_date, adjust):
    if ak is None:
        raise RuntimeError("akshare未安装，无法在线拉取(sina_stock)")
    sina_symbol = _to_exchange_symbol(symbol)
    df = ak.stock_zh_a_daily(symbol=sina_symbol, start_date=start_date, adjust=adjust)
    if df is None or df.empty:
        raise RuntimeError("sina_stock 无数据")
    return _normalize_daily_df(df)


def _fetch_from_sina_fund(symbol, start_date, adjust):
    if ak is None:
        raise RuntimeError("akshare未安装，无法在线拉取(sina_fund)")
    _ = adjust
    sina_symbol = _to_exchange_symbol(symbol)
    df = ak.fund_etf_hist_sina(symbol=sina_symbol)
    if df is None or df.empty:
        raise RuntimeError("sina_fund 无数据")
    normalized = _normalize_daily_df(df)
    normalized['date'] = pd.to_datetime(normalized['date'], errors='coerce')
    normalized = normalized[normalized['date'] >= pd.to_datetime(start_date)]
    if normalized.empty:
        raise RuntimeError("sina_fund 过滤后无数据")
    return normalized


_STOCK_DATA_SOURCE_POOL = _DataSourcePool(
    sources=[
        # 首选腾讯：实测不封 IP，只做单入口限流（自带三入口轮换 + 120s 冷却）。
        # 东财被 IP 级封锁时仍能取数——这正是本任务要解决的健壮性问题。
        ("astock_tencent", _fetch_from_astock_tencent),
        ("eastmoney", _fetch_from_eastmoney),
        ("sina_stock", _fetch_from_sina_stock),
        ("sina_fund", _fetch_from_sina_fund),
    ],
    cooldown_seconds=DATA_SOURCE_COOLDOWN_SECONDS,
    max_consecutive_failures=DATA_SOURCE_MAX_CONSECUTIVE_FAILURES,
    max_total_failures=DATA_SOURCE_MAX_TOTAL_FAILURES,
    failure_window_seconds=DATA_SOURCE_FAILURE_WINDOW_SECONDS,
)
