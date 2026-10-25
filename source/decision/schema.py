# -*- coding: utf-8 -*-
"""C3/C6: Schema definition and advise() API"""
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Any
from enum import Enum
import datetime


SCHEMA_VERSION = "1.0"


class Status(Enum):
    """退出状态枚举"""
    OK = "ok"
    NO_EDGE = "no_edge"
    INSUFFICIENT_DATA = "insufficient_data"
    DATA_SOURCE_ERROR = "data_source_error"
    UNTRADEABLE = "untradeable"


# C5 契约：status -> 退出码。五个码互不相同，必须由 CLI 显式 sys.exit。
EXIT_CODES = {
    "ok": 0,
    "no_edge": 10,
    "insufficient_data": 20,
    "data_source_error": 30,
    "untradeable": 40,
}


def exit_code_for(status: str) -> int:
    """status 字符串 -> 进程退出码（未知状态按数据源错误处理，绝不静默返回 0）。"""
    return EXIT_CODES.get(str(status), 30)


class Action(Enum):
    """交易动作枚举"""
    BUY = "buy"
    SELL = "sell"
    HOLD = "hold"
    AVOID = "avoid"
    INSUFFICIENT_DATA = "insufficient_data"


class TriggerState(Enum):
    """触发器状态枚举"""
    BUY_TRIGGERED = "buy_triggered"
    NEAR_BUY = "near_buy"
    NEUTRAL = "neutral"
    NEAR_SELL = "near_sell"
    SELL_TRIGGERED = "sell_triggered"


@dataclass
class Request:
    """请求参数"""
    code: str
    type: str
    type_hint: str = "auto"
    start: Optional[str] = None
    opt: str = "grid"
    jobs: int = 1
    fast: bool = False


@dataclass
class Instrument:
    """标的信息"""
    code: str
    name: str
    board: str  # sh_main/sh_star/sz_main/sz_gem/bj/fund/unknown
    price_limit_pct: float
    is_st: bool


@dataclass
class Data:
    """数据信息"""
    bars: int
    start: str
    end: str
    last_close: float
    source: str                # 实际取数渠道（'cache'/'sina'/...）；请求的渠道见 requested_source
    from_cache: bool
    # F06f/F20（2026-10-18）：数据新鲜度与降级可见性 ——
    # 让调用方能判断结论是否建立在陈旧数据上（旧行为：降级与新鲜命中都报 from_cache=True）。
    stale: bool = False                        # True = 触网失败后回退旧缓存；此时 diagnostics.warnings 必有说明
    cache_last_date: Optional[str] = None      # 缓存最后一根 K 线日期
    lag_days: Optional[int] = None             # 缓存末日落后期望交易日的自然日数
    expected_date: Optional[str] = None        # 期望的最新交易日（_expected_latest_trade_date）
    requested_source: Optional[str] = None     # 调用方请求的渠道，与 source（实际渠道）区分
    window_note: Optional[str] = None


@dataclass
class MarketRegime:
    """市场状态"""
    label: str
    confidence: float
    as_of: str
    definition: str


@dataclass
class Recommendation:
    """建议"""
    status: str
    action: str
    action_reason: List[str]
    confidence: float
    position_fraction: float
    horizon_days: List[int]
    entry_zone: Optional[List[float]]
    stop_loss: Optional[float]
    take_profit: Optional[float]
    distance_to_trigger: Dict[str, Optional[float]]
    invalidation: str
    no_edge: bool
    stop_method: str = "atr"


@dataclass
class Consensus:
    """共识"""
    family: Optional[str]
    agreement: float
    members: List[Dict]


@dataclass
class Stability:
    """稳定性"""
    windows_tested: int
    action_stable: bool
    family_stable: bool
    top_k_overlap_min: int


@dataclass
class MetricsItem:
    """指标项"""
    point: float
    ci_95: Optional[List[float]] = None


@dataclass
class Evidence:
    """证据"""
    consensus: Consensus
    dispersion: float
    stability: Stability
    n_trials: int
    n_survivors: int
    deflated_sharpe: float
    significant: bool
    metrics: Dict[str, MetricsItem]
    excess_vs_buy_hold: Dict[str, Any]
    buy_hold_baseline: Dict[str, float]
    regime_win_rates: Dict[str, float]
    triggers: List[Dict]


@dataclass
class Candidate:
    """候选策略"""
    name: str
    family: str
    score: float
    score_is: float
    score_oos: float
    annual_return: float
    max_drawdown: float
    win_rate: float
    trade_count: int
    calmar: float
    deflated_sharpe: float


@dataclass
class Diagnostics:
    """诊断信息"""
    unaffordable_lot_events: int
    blocked_by_limit_events: int
    zero_trade_configs: int
    survivor_configs: int
    warnings: List[str]
    elapsed_seconds: float


@dataclass
class JSONOutput:
    """完整输出结构"""
    schema_version: str
    generated_at: str
    request: Request
    instrument: Instrument
    data: Data
    market_regime: MarketRegime
    recommendation: Recommendation
    evidence: Evidence
    candidates: List[Candidate]
    diagnostics: Diagnostics
    disclaimer: str


# ---------------------------------------------------------------------------
# C3：响应构造（2026-10-18 从 cli.py 迁入）
# 契约 D4 要求 cli.py ≤ 700 行，且模块布局把"结构定义"归到本文件。
# 迁入的动因：错误响应与成功响应必须**同构**（不适用处显式 null），
# 这属于 schema 职责，不该散落在 CLI 里。
# ---------------------------------------------------------------------------
METRIC_KEYS = (
    'annual_return', 'max_drawdown', 'win_rate', 'sharpe', 'calmar',
    'profit_factor', 'trade_count', 'avg_holding_days', 'time_in_market',
)


def empty_metrics():
    """C3: 指标块不适用时逐项显式 null（键集合必须与 cli._build_metrics 完全一致）。"""
    return {k: {'point': None, 'ci_95': None} for k in METRIC_KEYS}


def window_note(start=None, as_of=None, bars=0):
    """F31：如实说明本次用了哪个窗口（成功/错误路径共用，避免两边写法漂移）。"""
    parts = []
    if start:
        parts.append(f"--start {start}")
    if as_of:
        parts.append(f"--as-of {as_of}")
    return f"{' + '.join(parts)} 截取后 {int(bars or 0)} 根" if parts else None


def execution_block(config=None) -> dict:
    """F29：成交假设（与 BacktestConfig 同源，禁止在别处再写一份）。

    下游 Agent 必须知道回测数字对应哪种执行口径：`next_open` = 信号次日开盘成交
    （可执行），`next_close` = 次日收盘成交（旧行为，晚一天）。
    """
    if config is None:
        from stock_analysis.backtest.engine import BacktestConfig
        config = BacktestConfig()
    return {
        'signal_bar': 'close',
        'fill': str(getattr(config, 'exec_price', 'next_open')),
        'slippage_bps': int(round(float(config.slippage_rate) * 10000)),
        'commission_bps': int(round(float(config.commission_rate) * 10000)),
    }


def create_error_response(status, message, code, data_type, type_hint='auto',
                          bars=0, start=None, end=None, last_close=None,
                          fetch_info=None, elapsed=0.0, warnings=None,
                          requested_start=None, requested_as_of=None):
    """创建错误响应。

    C3: 键集合必须与正常响应**同构**；不适用处显式 null，并在
    action_reason / diagnostics.warnings 说明原因。

    历史问题：错误响应只输出 46 个键、成功路径 134 个 —— 缺 88 条路径
    （evidence.metrics / n_trials / significant / triggers / data 8 字段 / stop_method …），
    且 data.bars 恒 0 与同文档 invalidation="仅 72 根 < 100" 自相矛盾。
    """
    from .instrument import classify_board
    from .signals import null_signal as _null_signal
    from .evaluate import null_stats

    fi = dict(fetch_info or {})
    try:
        board, price_limit_pct, is_st = classify_board(code)
    except Exception:
        board, price_limit_pct, is_st = 'unknown', None, None
    notes = list(warnings or [])
    if message not in notes:
        notes.insert(0, message)
    return {
        'schema_version': SCHEMA_VERSION,
        'generated_at': datetime.datetime.now().isoformat(),
        'request': {'code': code, 'type': data_type, 'type_hint': type_hint,
                    'start': requested_start, 'opt': 'grid', 'jobs': 1, 'fast': False,
                    # F31: 回显 --as-of，便于 Agent 自证"这条结论基于哪一天"
                    'as_of': requested_as_of},
        'instrument': {'code': code, 'name': '', 'board': board,
                       'price_limit_pct': price_limit_pct, 'is_st': is_st},
        'data': {
            # bars/start/end/last_close 在"数据太短"时是**已知的真实值**，
            # 必须如实填（此前恒 0/''/0，与 invalidation 文本互相打脸）
            'bars': int(bars or 0),
            'start': '' if start is None else str(start),
            'end': '' if end is None else str(end),
            'last_close': None if last_close is None else round(float(last_close), 4),
            'source': fi.get('source'),
            'from_cache': fi.get('from_cache'),
            'stale': (None if 'degraded' not in fi else bool(fi.get('degraded'))),
            'cache_last_date': fi.get('cache_last_date'),
            'lag_days': fi.get('lag_days'),
            'expected_date': fi.get('expected_date'),
            'requested_source': fi.get('requested_source'),
            # 与成功路径同语义：带 --start 时必须如实说明窗口，不能恒为 None。
            # （G35 只比对"键是否存在"，值被硬编码成 None 时它查不出来 —— 这里补齐值。）
            'window_note': window_note(requested_start, requested_as_of, bars),
            # F29: 成交假设（错误路径也必须给出，G35 递归比对键集合）
            'execution': execution_block(),
        },
        'market_regime': {'label': 'Unknown', 'confidence': 0.0,
                          'as_of': None, 'definition': None},
        'recommendation': {
            'status': status,
            'action': 'insufficient_data' if status in ('insufficient_data', 'data_source_error') else 'avoid',
            'action_reason': [message],
            # F24: 与成功路径同构（G35 递归比对键集合）；无信号时全部显式 null
            'signal': _null_signal(),
            'confidence': 0.0,
            'position_fraction': 0.0,
            # 未知持仓时长时给 [null, null] 而不是编造 [1, 5]
            'horizon_days': [None, None],
            'entry_zone': None,
            'stop_loss': None,
            'take_profit': None,
            'stop_method': None,
            'distance_to_trigger': {'buy_pct': None, 'sell_pct': None},
            'invalidation': message,
            'no_edge': True,
        },
        'evidence': {
            'consensus': {'family': None, 'agreement': None, 'members': []},
            'dispersion': None,
            'stability': {'action_stable': None, 'family_stable': None,
                          'windows_tested': 0, 'top_k_overlap_min': None},
            'n_trials': 0,
            'n_survivors': 0,
            'deflated_sharpe': None,
            'significant': False,
            # F27: 证据等级三档；无数据时全部显式 null（G35 同构）
            'edge': {'verdict': None, 'point': None, 'ci_95': None,
                     'p_positive': None, 'n_obs': 0, 'dsr': None, 'dsr_ok': None},
            # F28: 信号级统计；无信号时保留键、值为 null
            'signal_stats': null_stats('无可用信号，无法统计'),
            'metrics': empty_metrics(),
            'excess_vs_buy_hold': {'point': None, 'ci_95': None,
                                   'p_positive': None, 'n_obs': 0,
                                   'benchmark': 'buy_and_hold_close'},
            'buy_hold_baseline': {'total_return': None, 'annual_return': None},
            'regime_win_rates': {'Bull': None, 'Bear': None, 'Shock': None},
            'triggers': [],
        },
        'candidates': [],
        'diagnostics': {
            'unaffordable_lot_events': 0,
            'blocked_by_limit_events': 0,
            'zero_trade_configs': 0,
            'survivor_configs': 0,
            'warnings': notes,
            'elapsed_seconds': round(float(elapsed or 0.0), 2),
        },
        'disclaimer': '本结果不构成投资建议，市场有风险，投资需谨慎。',
    }
