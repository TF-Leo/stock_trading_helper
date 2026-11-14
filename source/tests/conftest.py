# -*- coding: utf-8 -*-
"""Global pytest setup for the offline test suite.

The a-stock-data adapter must never touch the network during tests.  The
calendar helper is the only code path that can lazily fetch during a data call
(``_expected_latest_trade_date``), so every test runs with:

* ``ASTOCK_CALENDAR_OFFLINE=1`` — no calendar HTTP;
* ``ASTOCK_MARKET_WATCH_OFFLINE=1`` — no 重点监控池 HTTP（T6）；状态目录指向临时目录；
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
    # issue #13：腾讯跨进程冷却状态默认落在 source/stock_analysis/data/state/。
    # 测试必须指向临时目录 —— 否则一个用例触发的冷却会写进开发者本机状态文件，
    # 并让后续用例「整域冷却中」而跳过所有请求（实测 10 个用例因此误红）。
    monkeypatch.setenv("TENCENT_COOLDOWN_PATH",
                       str(tmp_path / "tencent_cooldown.json"))
    # issue #108：东财反爬冷却（资金流 / ST 名单 / 监控池共用）同样必须落在
    # 临时目录，否则一个用例触发的熔断会让后续用例「冷却中」而跳过请求，
    # 甚至写进开发者本机状态文件。
    monkeypatch.setenv("EASTMONEY_COOLDOWN_PATH",
                       str(tmp_path / "eastmoney_cooldown.json"))
    # T6（issue #1）：监控池默认离线 + 独立状态目录。离线时 load_pool 不触网；
    # 状态目录指向临时目录，避免用例写进开发者本机的 market_watch.json 缓存。
    monkeypatch.setenv("ASTOCK_MARKET_WATCH_OFFLINE", "1")
    monkeypatch.setenv("MARKET_WATCH_STATE_DIR", str(tmp_path / "market_watch_state"))
    # T7（issue #1）：事件风险缓存默认指向临时目录，避免用例读/写开发者本机
    # 的 events.json，也避免本机缓存改变测试结果。
    monkeypatch.setenv("EVENTS_STATE_DIR", str(tmp_path / "events_state"))
    try:
        from stock_analysis.data_sources import astock, _astock_generated
        from stock_analysis.data_sources import market_watch
        from stock_analysis.data_loader import status as trading_status
        astock._CALENDAR_MEMO.clear()
        astock._CALENDAR_INFO.update(source=None, last_error=None, last_month=None)
        monkeypatch.setattr(astock, "_read_disk_calendar", lambda year, month: None)
        _astock_generated._tencent_host_down_until.clear()
        trading_status.reset_cache()
        market_watch.reset_cache()
        # 每个用例独立缓存文件：不读开发者本机缓存，也不污染它。
        monkeypatch.setattr(trading_status, "cache_path",
                            lambda: str(tmp_path / "st_list.json"))
    except Exception:
        pass
    yield
