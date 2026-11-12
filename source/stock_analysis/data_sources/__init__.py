# -*- coding: utf-8 -*-
"""数据源适配层。

``astock`` 封装 a-stock-data 固定快照（腾讯日线 / 实时快照 / 官方交易日历）。
``_astock_generated`` 由 ``tools/astock_extract.py`` 生成，勿手改。

导入本包不会发起任何网络请求。
"""
from . import astock  # noqa: F401  (导入无副作用：仅创建 requests.Session)

__all__ = ["astock"]
