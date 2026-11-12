# -*- coding: utf-8 -*-
"""Global pytest setup for the offline test suite.

The a-stock-data adapter must never touch the network during tests.  The
calendar helper is the only code path that can lazily fetch during a data call
(``_expected_latest_trade_date``), so every test runs with:

* ``ASTOCK_CALENDAR_OFFLINE=1`` — no calendar HTTP;
* the on-disk calendar cache stubbed out — otherwise a developer machine that
  ran the CLI once would feed different data into the suite;
* process-level memo / host cooldown reset — no cross-test leakage.

Tests that exercise the calendar explicitly monkeypatch the provider.
"""
import os
import sys

import pytest

_SRC = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)


@pytest.fixture(autouse=True)
def _astock_offline(monkeypatch, tmp_path):
    monkeypatch.setenv("ASTOCK_CALENDAR_OFFLINE", "1")
    # P0-3：默认禁止交易状态层触网（ST 名单 + 腾讯报价）。需要联网语义的
    # 测试在用例内显式覆盖该环境变量，并注入 quote / st_snapshot。
    monkeypatch.setenv("ASTOCK_STATUS_OFFLINE", "1")
    try:
        from stock_analysis.data_sources import astock, _astock_generated
        from stock_analysis.data_loader import status as trading_status
        astock._CALENDAR_MEMO.clear()
        astock._CALENDAR_INFO.update(source=None, last_error=None, last_month=None)
        monkeypatch.setattr(astock, "_read_disk_calendar", lambda year, month: None)
        _astock_generated._tencent_host_down_until.clear()
        trading_status.reset_cache()
        # 每个用例独立缓存文件：不读开发者本机缓存，也不污染它。
        monkeypatch.setattr(trading_status, "cache_path",
                            lambda: str(tmp_path / "st_list.json"))
    except Exception:
        pass
    yield
