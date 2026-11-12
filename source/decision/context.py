# -*- coding: utf-8 -*-
"""P1 只读上下文块的组装（从 ``cli.py`` 迁出，守住其 ≤700 行契约）。

这里只调用两个**只读缓存**的 data_loader，不取数、不触网：

* ``evidence.flow``（P1-2）：日级资金流；
* ``evidence.valuation``（P1-1）：估值历史摘要。

两者都只影响展示，**不参与** ``action`` / ``status`` / 仓位计算。
"""
from __future__ import annotations


def evidence_context(code, data_type, as_of):
    """返回 ``(flow_block, valuation_block, warnings)``。"""
    from stock_analysis.data_loader.flow import flow_evidence
    from stock_analysis.data_loader.valuation import valuation_evidence

    flow_block, flow_notes = flow_evidence(code, data_type, as_of)
    valuation_block, valuation_notes = valuation_evidence(code, data_type, as_of)
    return flow_block, valuation_block, flow_notes + valuation_notes
