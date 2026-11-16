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

**issue #124：所有持久 state / 冷却 / 缓存目录都指向用例临时目录。**
真实机器上 ``source/stock_analysis/data/`` 可能带着东财冷却、监控池缓存、
ST 名单、资金流/估值快照、交易日历；任何一条被测试读到，测试结果就依赖
机器状态（2026-11-15：22 项 ST 测试因真实东财冷却文件整片变红）。
隔离覆盖：腾讯/东财冷却、监控池、事件、ST 名单、资金流、估值、前向台账、
交易日历，以及行情/指数缓存目录。

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
    # issue #124：每个用例一套隔离目录。state 与行情/上下文缓存分开，
    # 避免用例之间的「写进去再读出来」变成隐式前置条件。
    state_root = tmp_path / "state"
    data_root = tmp_path / "data"
    # issue #13：腾讯跨进程冷却状态默认落在 source/stock_analysis/data/state/。
    # 测试必须指向临时目录 —— 否则一个用例触发的冷却会写进开发者本机状态文件，
    # 并让后续用例「整域冷却中」而跳过所有请求（实测 10 个用例因此误红）。
    monkeypatch.setenv("TENCENT_COOLDOWN_PATH",
                       str(state_root / "tencent_cooldown.json"))
    # issue #108：东财反爬冷却（资金流 / ST 名单 / 监控池共用）同样必须落在
    # 临时目录，否则一个用例触发的熔断会让后续用例「冷却中」而跳过请求，
    # 甚至写进开发者本机状态文件。
    monkeypatch.setenv("EASTMONEY_COOLDOWN_PATH",
                       str(state_root / "eastmoney_cooldown.json"))
    # T6（issue #1）：监控池默认离线 + 独立状态目录。离线时 load_pool 不触网；
    # 状态目录指向临时目录，避免用例写进开发者本机的 market_watch.json 缓存。
    monkeypatch.setenv("ASTOCK_MARKET_WATCH_OFFLINE", "1")
    monkeypatch.setenv("MARKET_WATCH_STATE_DIR", str(state_root / "market_watch"))
    # T7（issue #1）：事件风险缓存默认指向临时目录，避免用例读/写开发者本机
    # 的 events.json，也避免本机缓存改变测试结果。
    monkeypatch.setenv("EVENTS_STATE_DIR", str(state_root / "events"))
    # issue #124：ST 名单缓存（st_list.json）与冷却（eastmoney_cooldown.json）
    # 共用 state 目录；旧实现只 patch 了 cache_path，_state_dir() 仍指向真实
    # 目录，direct caller 会读到开发者本机的名单。
    monkeypatch.setenv("ST_LIST_STATE_DIR", str(state_root))
    # issue #124：交易日历缓存目录此前是**硬编码常量**（无法注入），测试
    # 会把真实 data/calendar/szse_*.json 当作缓存命中，甚至写盘。
    monkeypatch.setenv("ASTOCK_CALENDAR_DIR", str(data_root / "calendar"))
    try:
        from stock_analysis.data_sources import astock, _astock_generated
        from stock_analysis.data_sources import market_watch
        from stock_analysis.data_loader import status as trading_status
        from stock_analysis.data_loader import flow as flow_mod
        from stock_analysis.data_loader import valuation as val_mod
        from stock_analysis.data_loader import meta as meta_mod
        from stock_analysis.data_loader import bars as bars_mod
        from stock_analysis import forward_ledger
        astock._CALENDAR_MEMO.clear()
        astock._CALENDAR_INFO.update(source=None, last_error=None, last_month=None)
        monkeypatch.setattr(astock, "_read_disk_calendar", lambda year, month: None)
        _astock_generated._tencent_host_down_until.clear()
        trading_status.reset_cache()
        market_watch.reset_cache()
        # issue #124：CLI 主路径按模块常量读上下文缓存（不传 dir 参数），
        # 因此必须把常量本身指向临时目录；显式传 dir 的用例会覆盖这里的值。
        monkeypatch.setattr(flow_mod, "FLOW_DIR", str(data_root / "flow"))
        monkeypatch.setattr(val_mod, "VALUATION_DIR", str(data_root / "valuation"))
        monkeypatch.setattr(forward_ledger, "LEDGER_DIR",
                            str(data_root / "forward"))
        # issue #124：行情 / 指数 / 代码名称缓存（data/*.csv、.name_cache.json）。
        # 冷克隆机器没有这些文件；开发者机器有。不隔离则同一提交结果不同。
        quote_dir = str(data_root / "quotes")
        monkeypatch.setattr(forward_ledger, "DATA_DIR", quote_dir)
        monkeypatch.setattr(meta_mod, "DATA_DIR", quote_dir)
        monkeypatch.setattr(bars_mod, "DATA_DIR", quote_dir)
        # fund 侧缓存目录同样只在模块已被导入时改写，避免为了打补丁而
        # 触发额外的 import 副作用。
        for name in ("fund_analysis.utils", "fund_analysis.fund_strategy.strategy"):
            module = sys.modules.get(name)
            if module is not None and hasattr(module, "DATA_DIR"):
                monkeypatch.setattr(module, "DATA_DIR", str(data_root / "fund"))
    except Exception:
        pass
    yield
