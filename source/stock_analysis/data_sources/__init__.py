# -*- coding: utf-8 -*-
"""数据源适配层。

``astock`` 封装 a-stock-data 固定快照（腾讯日线 / 实时快照 / 官方交易日历）。
``_astock_generated`` 由 ``tools/astock_extract.py`` 生成，勿手改。
``eastmoney_guard`` 是东财统一取数闸门（444 / 零字节 FIN 熔断 + 冷却 + 预算，
issue #108）：资金流、ST 名单与监控池共用同一套判定。

导入本包不会发起任何网络请求。
"""
from . import astock  # noqa: F401  (导入无副作用：仅创建 requests.Session)
from . import eastmoney_guard  # noqa: F401  (纯函数 / 状态文件，不触网)

__all__ = ["astock", "eastmoney_guard"]
