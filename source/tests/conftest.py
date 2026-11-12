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
def _astock_offline(monkeypatch):
    monkeypatch.setenv("ASTOCK_CALENDAR_OFFLINE", "1")
    try:
        from stock_analysis.data_sources import astock, _astock_generated
        astock._CALENDAR_MEMO.clear()
        astock._CALENDAR_INFO.update(source=None, last_error=None, last_month=None)
        monkeypatch.setattr(astock, "_read_disk_calendar", lambda year, month: None)
        _astock_generated._tencent_host_down_until.clear()
    except Exception:
        pass
    yield
