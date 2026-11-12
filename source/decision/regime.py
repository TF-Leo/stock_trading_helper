# -*- coding: utf-8 -*-
"""市场环境（regime）输出块组装。

从 ``cli.py`` 迁出（契约 D4：``cli.py`` ≤ 700 行）。这里只做**组装**，
判定逻辑仍只有一份真源：``advisor.analyzer.get_current_market_regime``
（个股）与 ``data_loader.get_index_regime``（指数，内部复用同一个
``analyzer._regime_from_row``）。

P1-3（issue #32）：``market_regime`` 新增 additive 的 ``index`` 子块，
**只展示、不改 action**。原有四字段 ``label/confidence/as_of/definition``
的计算与取值一个字都不改。
"""


def market_regime_block(df, warnings):
    """``market_regime`` = 原有四字段（逐字不变）+ P1-3 additive 的 ``index``。

    指数 regime 不参与 action / 仓位 / ``regime_win_rates``（后者仍用个股
    序列，见 ``analyzer.market_regime_series``）。
    """
    block = stock_regime(df)
    block['index'] = index_regime(warnings)
    return block


def disabled_block(last_date):
    """``--no-regime`` 时的显式禁用结构（两个子块都不假装有标签）。"""
    return {'label': 'Unknown', 'confidence': 0.0,
            'as_of': str(last_date), 'definition': 'disabled',
            'index': {'label': None, 'confidence': None, 'as_of': None,
                      'definition': 'disabled', 'source': None, 'bars': None}}


def empty_index_block():
    """指数 regime 不可用时的显式 null 结构（键集合固定，G35 同构要求）。"""
    return {'label': None, 'confidence': None, 'as_of': None,
            'definition': None, 'source': None, 'bars': None}


def stock_regime(df):
    """个股自身日线的市场环境判定（历史行为，保持不变）。

    历史问题：这里曾硬编码 ``{'label': 'Bull', 'confidence': 0.7}``，无论行情
    如何都报牛市，与 C4（输出不得自相矛盾）冲突。现直接复用
    ``advisor.analyzer.get_current_market_regime``（A8 单一 regime 真源），
    避免再造第二套标签。失败时显式返回 Unknown 而不是假装 Bull。
    """
    try:
        from stock_analysis.advisor.analyzer import get_current_market_regime
        label, conf = get_current_market_regime(df)
        return {
            'label': str(label),
            'confidence': round(float(conf), 3),
            'as_of': str(df.index[-1]),
            'definition': 'ma20_ma60_return_volatility',
        }
    except Exception:
        return {
            'label': 'Unknown',
            'confidence': 0.0,
            'as_of': str(df.index[-1]) if df is not None and len(df) else '',
            'definition': 'unavailable',
        }


def index_regime(warnings):
    """P1-3：沪深 300 指数 regime —— 只展示，不改 action。

    取不到时返回显式 null 结构并追加一条 warning，绝不阻断主流程、绝不伪装成
    某个标签。导入放在函数内：便于调用方（及测试）替换
    ``stock_analysis.data_loader.get_index_regime``，也避免循环导入。
    """
    try:
        from stock_analysis.data_loader import get_index_regime
        block = get_index_regime()
    except Exception as e:
        warnings.append(f"指数 regime 不可用：{e}（仅影响展示，不影响 action）")
        return empty_index_block()
    if not block:
        warnings.append(
            "指数 regime 不可用（沪深 300 日线取数失败或不足 60 根）；"
            "仅影响展示，market_regime 与 action 不受影响")
        return empty_index_block()
    return block
