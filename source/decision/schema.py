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
    source: str
    from_cache: bool
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
