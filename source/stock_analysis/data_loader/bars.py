try:
    import akshare as ak
except ImportError:  # P2: 离线/最小依赖时允许仅用本地缓存回退
    ak = None
import pandas as pd
import numpy as np
import datetime
import os
import requests
import time
from dataclasses import dataclass, field
from threading import Lock
from typing import Callable, Optional

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
        print(f"获取名称失败 {symbol}: {e}")
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
            print(f"[WARN] 数据源 {state.name} 触发冷却 {self._cooldown_seconds}s: {err}")
        if should_disable:
            print(f"[WARN] 数据源 {state.name} 失败过多，已从渠道池移除")

    def fetch(self, symbol, start_date, adjust, preferred=None):
        candidates = self._ordered_candidates(preferred)
        if not candidates:
            print("[WARN] 渠道池无可用数据源（均在冷却或已剔除）")
            return None, None
        last_err = None
        for state in candidates:
            try:
                df = state.fetcher(symbol, start_date, adjust)
                if df is None or df.empty:
                    raise RuntimeError(f"{state.name} 返回空数据")
                self._mark_success(state)
                return df, state.name
            except Exception as err:
                last_err = err
                self._mark_failure(state, err)
        if last_err:
            print(f"[WARN] 渠道池全部失败 {symbol}: {last_err}")
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
        ("eastmoney", _fetch_from_eastmoney),
        ("sina_stock", _fetch_from_sina_stock),
        ("sina_fund", _fetch_from_sina_fund),
    ],
    cooldown_seconds=DATA_SOURCE_COOLDOWN_SECONDS,
    max_consecutive_failures=DATA_SOURCE_MAX_CONSECUTIVE_FAILURES,
    max_total_failures=DATA_SOURCE_MAX_TOTAL_FAILURES,
    failure_window_seconds=DATA_SOURCE_FAILURE_WINDOW_SECONDS,
)
