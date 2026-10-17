# -*- coding: utf-8 -*-
"""A10: 板块分类与涨跌停阈值"""
from typing import Tuple

def classify_board(code: str) -> Tuple[str, float, bool]:
    """
    根据股票代码分类板块并返回涨跌停阈值。
    
    Args:
        code: 6 位股票代码
        
    Returns:
        (board, price_limit_pct, is_st)
        - board: sh_main/sh_star/sz_main/sz_gem/bj/fund/unknown
        - price_limit_pct: 涨跌停幅度 (如 0.10 表示 10%)
        - is_st: 是否 ST 股
    """
    if len(code) != 6:
        return ('unknown', 0.10, False)
    
    prefix = code[:2]
    
    if prefix in ['68', '69']:
        return ('sh_star', 0.20, False)
    elif prefix == '60':
        return ('sh_main', 0.10, False)
    elif prefix in ['30', '31']:
        return ('sz_gem', 0.20, False)
    elif prefix in ['00', '01', '02', '03']:
        return ('sz_main', 0.10, False)
    elif prefix in ['4', '8']:
        return ('bj', 0.30, False)
    elif code.startswith('5'):
        return ('fund', 0.10, False)
    
    is_st = False
    return ('unknown', 0.10, is_st)


def get_price_limits(price_limit_pct: float) -> Tuple[float, float]:
    """
    根据涨跌停幅度计算实际交易阈值。
    
    规则：threshold = 1 +/- (limit - 0.001)
    """
    upper = 1.0 + (price_limit_pct - 0.001)
    lower = 1.0 - (price_limit_pct - 0.001)
    return (upper, lower)
