# -*- coding: utf-8 -*-
"""decision 包：股票与基金共用的决策层。"""
from .schema import (
    SCHEMA_VERSION,
    Status,
    Action,
    TriggerState,
    JSONOutput,
)
from .instrument import classify_board, get_price_limits
from .scoring import risk_adjusted_score, bootstrap_ci, deflated_sharpe, calculate_dispersion
from .consensus import select_consensus


def advise(code, type_hint='auto', start=None, opt='grid', jobs=1, fast=False):
    """C6 契约：单一真源 API。

    返回 dict，键集合与 `python cli.py <code> --json` 的输出完全一致
    （CLI 只是本函数的薄包装）。延迟导入 cli 以避免循环导入。
    """
    from cli import advise_internal
    return advise_internal(
        code, type_hint=type_hint, start=start, opt=opt, jobs=jobs, fast=fast
    )


__all__ = [
    'SCHEMA_VERSION',
    'Status',
    'Action',
    'TriggerState',
    'JSONOutput',
    'classify_board',
    'get_price_limits',
    'risk_adjusted_score',
    'bootstrap_ci',
    'deflated_sharpe',
    'calculate_dispersion',
    'select_consensus',
    'advise',
]