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
from stock_analysis.data_sources.tencent_throttle import (
    TencentBudgetExceeded,
    run_with_deadline,
)

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(PROJECT_ROOT, 'data')

if not os.path.exists(DATA_DIR):
    os.makedirs(DATA_DIR)

DATA_SOURCE_COOLDOWN_SECONDS = int(os.getenv("DATA_SOURCE_COOLDOWN_SECONDS", "120"))
DATA_SOURCE_MAX_CONSECUTIVE_FAILURES = int(os.getenv("DATA_SOURCE_MAX_CONSECUTIVE_FAILURES", "3"))
DATA_SOURCE_MAX_TOTAL_FAILURES = int(os.getenv("DATA_SOURCE_MAX_TOTAL_FAILURES", "8"))
DATA_SOURCE_FAILURE_WINDOW_SECONDS = int(os.getenv("DATA_SOURCE_FAILURE_WINDOW_SECONDS", "600"))
# issue #13 第 5 条：单票全通道失败的总耗时上限。R5 实测「三入口 × 19 分页窗口」
# 在限流时能把一只票拖到分钟级；20s 内必出结论，超时回退缓存并标 degraded。
DEFAULT_TICKER_BUDGET_SECONDS = 20.0
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
# issue #26：整条 warning 的上限。多通道同时失败时每条 warning 都要带来源名，
# 单条再被 200 字的 error_text 撑满，warnings 就会变得难以扫读。
# 截断保留**前缀**，因此「来源名 + error_class + 错误摘要开头」必然留存。
MAX_WARNING_TEXT = 300
# 任意 scheme 的绝对 URL（https / ftp / ws / wss / s3 / postgres …）。
# 旧规则只认 http(s)，`ftp://user:pw@host` 这类会原样进 diagnostics（issue #26 第 3 条）。
_URL_PATTERN = re.compile(r'[A-Za-z][A-Za-z0-9+.\-]*://\S+')
# requests 的异常常写成 `... with url: /api/qt/...?fields1=...`（相对路径，
# 不匹配上面的绝对 URL 规则），同样可能带长查询串 / 参数，一并脱敏。
_RELATIVE_URL_PATTERN = re.compile(r'(?i)\burl:\s*\S+')
# Authorization / Proxy-Authorization：必须把认证方案**后面的凭据本体**一起吃掉。
# 旧规则 `authorization\s*[=:]\s*[^\s,;&]+` 只匹配到 `Bearer`，token 原样留存
# （issue #26 第 1 条）。
_AUTH_PATTERN = re.compile(
    r'(?i)\b((?:proxy-)?authorization)\b\s*[:=]\s*'
    r'(?:(?:bearer|basic|digest|negotiate|token)\s+)?[^\s,;&}]+'
)
# key=value / JSON 引号形式（issue #26 第 2 条）：`"api_key": "abc"`、
# `"token":"abc"`、`token=abc`、`SECRET_KEY=xyz`。键与值两侧的引号都可选，
# 替换时丢弃引号（只保留键名，避免把引号形式原样带进 diagnostics）。
_SECRET_KEY = (r'token|api[_-]?key|access[_-]?key|secret(?:[_-]?key)?|'
               r'password|passwd|pwd|auth[_-]?key|cookie|session(?:[_-]?id)?')
_SECRET_PATTERN = re.compile(
    r'(?i)(?P<q>["\']?)\b(?P<key>' + _SECRET_KEY + r')(?P=q)\s*[=:]\s*'
    r'(?:"[^"]*"|\'[^\']*\'|[^\s,;&}]+)'
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
    # 认证头先于通用 key=value：否则 `Authorization: Bearer <tok>` 只会被
    # 通用规则吃掉 `Bearer` 四个字母。
    text = _AUTH_PATTERN.sub(r'\1=<redacted>', text)
    text = _SECRET_PATTERN.sub(r'\g<key>=<redacted>', text)
    text = ' '.join(text.split())
    if len(text) > MAX_ATTEMPT_ERROR_TEXT:
        text = text[:MAX_ATTEMPT_ERROR_TEXT - 3] + '...'
    return text


def _cap_warning(text) -> str:
    """整条 warning 的硬上限（issue #26）：超长时保留前缀 + 省略号。"""
    text = str(text)
    if len(text) > MAX_WARNING_TEXT:
        return text[:MAX_WARNING_TEXT - 3] + '...'
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
             cooldown_seconds=None, budget_exhausted=False):
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
    # issue #13 第 5 条：只有超预算时才有这个键（成功/普通失败的 record 键集合不变，
    # 避免既有调用方按固定键集合比对时被新增键打破）。
    if budget_exhausted:
        record['budget_exhausted'] = True
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
        lines.append(_cap_warning(msg))
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
        # issue #13 第 3/5 条：单票时间预算 + 「主源连败 → 本轮优先备胎」。
        #   * ticker_budget_seconds：单票所有通道的总耗时上限，超时立刻放弃该票
        #     （调用方回退缓存并标 degraded），不让一只票把整轮拖成乘法级耗时。
        #   * _primary_blocked_until：主源（腾讯）连续失败达到阈值后，本轮剩余票
        #     不再先试主源，直接把备胎排到最前——避免「每票都重试三入口」把
        #     一次限流放大三倍。新的一轮（调用 reset_primary_block）重新判定。
        self.ticker_budget_seconds = float(
            os.getenv("DATA_SOURCE_TICKER_BUDGET_SECONDS",
                      str(DEFAULT_TICKER_BUDGET_SECONDS)))
        self.primary_source_name = None
        self.primary_block_after = int(os.getenv(
            "DATA_SOURCE_PRIMARY_BLOCK_AFTER", "3"))
        self.primary_consecutive_failures = 0
        self.primary_blocked_reason = None
        self.budget_timeouts = 0

    def reset_primary_block(self):
        """开始新一轮抓取：清除「主源本轮停用」状态（连续计数也归零）。"""
        with self._lock:
            self.primary_consecutive_failures = 0
            self.primary_blocked_reason = None

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
            primary = self.primary_source_name
            blocked = bool(self.primary_blocked_reason)
        # 主源本轮已停用：把它排到最后，让备胎先试（不是剔除，主源仍可兜底）。
        if blocked and primary:
            ordered.sort(key=lambda s: 1 if s.name == primary else 0)
        if preferred:
            rank = {name: i for i, name in enumerate(preferred)}
            # 稳定排序：blocked 的优先级高于 preferred，否则 preferred 会
            # 把主源又提回第一位，等于没停用。
            if not blocked:
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
        # issue #13 第 5 条：单票总耗时预算。从本票开始计时，超预算立刻停止尝试
        # 剩余通道（不再为「注定失败」的通道各付一次超时），由调用方回退缓存。
        # issue #67（D2 验收退回）：预算此前只约束「通道之间」，**正在阻塞的单个
        # 通道**可以吃掉任意长的墙钟（3s 预算实测 12.01s）。现在每个通道调用都
        # 套 ``run_with_deadline``（剩余预算），单票墙钟不超过预算 + 线程调度误差。
        deadline = time.monotonic() + self.ticker_budget_seconds
        budget_hit = False
        for state in candidates:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                budget_hit = True
                attempts.append(_attempt(
                    state.name, False,
                    error=RuntimeError(
                        f"单票耗时超过 {self.ticker_budget_seconds:g}s 预算，"
                        f"跳过剩余通道"),
                    budget_exhausted=True))
                print(f"[WARN] {symbol} 单票耗时超过 {self.ticker_budget_seconds:g}s "
                      f"预算，停止尝试剩余通道（{state.name} 未调用）", file=sys.stderr)
                break
            try:
                df = run_with_deadline(
                    state.fetcher, remaining, symbol, start_date, adjust,
                    budget=remaining)
                if df is None or df.empty:
                    raise RuntimeError(f"{state.name} 返回空数据")
                self._mark_success(state)
                attempts.append(_attempt(state.name, True))
                self._publish_attempts(attempts)
                self._note_primary_result(state, ok=True)
                return df, state.name
            except TencentBudgetExceeded as err:
                # 单通道自己吃满了剩余预算（网络挂死 / read timeout 比预算还长）。
                # 不判通道故障（可能是前面通道已耗掉大半预算导致的「饿死」），
                # 但要让主源连败计数看见它——腾讯挂死时要尽快把备胎排到前面。
                budget_hit = True
                last_err = err
                attempts.append(_attempt(state.name, False, error=err))
                self._publish_attempts(attempts)
                self._note_primary_result(state, ok=False, error=err)
                print(f"[WARN] {symbol} 通道 {state.name} 单次取数超过剩余预算 "
                      f"{remaining:.2f}s，已放弃（剩余通道不再尝试）", file=sys.stderr)
                continue
            except Exception as err:
                last_err = err
                cooldown, removed = self._mark_failure(state, err)
                attempts.append(_attempt(
                    state.name, False, error=err,
                    cooldown=cooldown, removed=removed,
                    cooldown_seconds=self._cooldown_seconds if cooldown else None))
                self._note_primary_result(state, ok=False, error=err)
        if not budget_hit and last_err is not None and time.monotonic() > deadline:
            # 预算在**最后一个**通道上被用尽：没有「剩余通道」可跳过，但整票耗时
            # 确实超预算，同样要记一次（调用方据此回退缓存并标 degraded）。
            budget_hit = True
            print(f"[WARN] {symbol} 单票耗时 {self.ticker_budget_seconds:g}s "
                  f"预算已用尽（全部通道均已尝试）", file=sys.stderr)
        if budget_hit:
            self.budget_timeouts += 1
        self._publish_attempts(attempts)
        if last_err:
            print(f"[WARN] 渠道池全部失败 {symbol}: {last_err}", file=sys.stderr)
        return None, None

    def _note_primary_result(self, state, ok, error=None):
        """记录主源（腾讯）连败计数；达到阈值即「本轮剩余票优先备胎」。

        issue #13 第 2/3 条。只在 ``state.name == primary_source_name`` 时生效，
        备胎的成败不影响主源判定（备胎挂了不代表腾讯被限流）。
        """
        primary = self.primary_source_name
        if not primary or state.name != primary:
            return
        with self._lock:
            if ok:
                self.primary_consecutive_failures = 0
                return
            self.primary_consecutive_failures += 1
            if (self.primary_blocked_reason is None
                    and self.primary_consecutive_failures >= self.primary_block_after):
                self.primary_blocked_reason = (
                    f"主源 {primary} 连续 {self.primary_consecutive_failures} 只失败，"
                    f"本轮剩余票直接走备胎")
        if self.primary_blocked_reason and error is not None and \
                self.primary_consecutive_failures == self.primary_block_after:
            print(f"[WARN] {self.primary_blocked_reason}"
                  f"（最后一次：{_sanitize_error_text(error)}）", file=sys.stderr)


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


def _fetch_from_astock_tencent(symbol, start_date, adjust, budget=None):
    """a-stock-data 腾讯 K 线（固定快照；§1.2 原始价 + §1.6 比例前复权）。

    返回 DatetimeIndex + OHLCV；meta 的 `_normalize_daily_df` 会把索引还原成
    `date` 列后走与其它通道完全相同的规范化/缓存路径。

    ``budget``：渠道池剩余预算（秒）；腾讯侧据此把单次 HTTP 的 (connect, read)
    收敛到预算内（issue #67 D2）。
    """
    return _astock_source.fetch_daily(
        symbol, start_date=start_date, adjust=adjust, budget=budget)


def _fetch_from_eastmoney(symbol, start_date, adjust, budget=None):
    _ = budget  # akshare 不接受 per-call 超时；墙钟由渠道池 run_with_deadline 兜底
    if ak is None:
        raise RuntimeError("akshare未安装，无法在线拉取(eastmoney)")
    df = ak.stock_zh_a_hist(symbol=symbol, period="daily", start_date=start_date, adjust=adjust)
    if df is None or df.empty:
        raise RuntimeError("eastmoney 无数据")
    return _normalize_daily_df(df)


def _fetch_from_sina_stock(symbol, start_date, adjust, budget=None):
    _ = budget  # 同上：超时控制不在 akshare 内
    if ak is None:
        raise RuntimeError("akshare未安装，无法在线拉取(sina_stock)")
    sina_symbol = _to_exchange_symbol(symbol)
    df = ak.stock_zh_a_daily(symbol=sina_symbol, start_date=start_date, adjust=adjust)
    if df is None or df.empty:
        raise RuntimeError("sina_stock 无数据")
    return _normalize_daily_df(df)


def _fetch_from_sina_fund(symbol, start_date, adjust, budget=None):
    _ = budget
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
# issue #13：腾讯是主源。连续 3 只失败 → 本轮剩余票把备胎排到前面，不再每票
# 先撞三次腾讯入口（R5 的 32 只失败里绝大多数是这种放大后的重试）。
_STOCK_DATA_SOURCE_POOL.primary_source_name = "astock_tencent"
