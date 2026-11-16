# -*- coding: utf-8 -*-
"""issue #124：测试隔离真实 state 目录的守卫测试。

背景
----
2026-11-15 生产升级复验：22 项 ST 相关测试在真实机器上整片变红 —— 它们读到了
真实的 ``source/stock_analysis/data/state/eastmoney_cooldown.json``（当时东财
正在冷却）。同一提交在不同机器/不同时刻结果不同，冷却期的红灯还会掩盖真实
回归。本文件把「测试绝不依赖真实 state」变成可执行的断言：

1. :func:`test_state_accessor_resolves_under_test_root` —— 所有可能落盘的
   state / 冷却 / 缓存路径在本进程内都解析到用例临时目录，且不在仓库真实
   ``stock_analysis/data`` / ``fund_analysis/data`` 之下；
2. :func:`test_default_state_paths_unchanged_without_override` —— 去掉测试
   注入后，默认路径仍是原来的生产路径（隔离不改变生产行为）；
3. :func:`test_full_suite_is_immune_to_poisoned_real_state` —— 把仓库复制到
   临时目录，在副本的真实 data 目录里写入**会改变结论的毒数据**（活跃冷却、
   把 600519 标成 ST 的名单、资金流/估值/事件/监控池/指数/行情缓存），再跑
   全量 pytest：必须全绿，且毒文件逐字节不变（证明没有任何用例读/写真实
   state）。

守卫测试自身在子进程里用 ``STATE_ISOLATION_GUARD_CHILD`` 哨兵跳过，避免
无限递归。所有标的只用 600519 / 600000。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1]
ROOT = SRC.parent
REAL_DATA_DIRS = (SRC / "stock_analysis" / "data", SRC / "fund_analysis" / "data")
GUARD_CHILD_ENV = "STATE_ISOLATION_GUARD_CHILD"

#: 子进程不得继承父进程的隔离变量 —— 否则守卫会变成「验证父进程的 monkeypatch」，
#: 而不是「验证子进程自己的 conftest 隔离」。
_ISOLATION_ENV_KEYS = (
    "TENCENT_COOLDOWN_PATH",
    "EASTMONEY_COOLDOWN_PATH",
    "ASTOCK_MARKET_WATCH_OFFLINE",
    "MARKET_WATCH_STATE_DIR",
    "EVENTS_STATE_DIR",
    "ST_LIST_STATE_DIR",
    "ST_LIST_COOLDOWN_PATH",
    "ASTOCK_CALENDAR_DIR",
)


def _inside(path, root) -> bool:
    try:
        Path(path).resolve().relative_to(Path(root).resolve())
        return True
    except (ValueError, OSError):
        return False


def _state_accessors():
    """(名字, 取路径的函数)；全部只读，不触网、不写盘。"""
    from stock_analysis import forward_ledger
    from stock_analysis.data_loader import events, flow, meta, status, valuation
    from stock_analysis.data_sources import (
        astock, eastmoney_guard, market_watch, tencent_throttle)

    return {
        "tencent_cooldown": tencent_throttle.state_path,
        "eastmoney_cooldown": eastmoney_guard.state_path,
        "market_watch_cache": market_watch.cache_path,
        "events_cache": events.cache_path,
        "st_list_cache": status.cache_path,
        "st_list_cooldown": status.cooldown_path,
        "flow_cache": lambda: flow._cache_path("600519"),
        "valuation_cache": lambda: valuation._cache_path("600000"),
        "forward_ledger_day": lambda: forward_ledger.day_path("2026-11-16"),
        "calendar_cache": lambda: astock._calendar_file(2026, 11),
        "quote_cache": lambda: os.path.join(
            meta.DATA_DIR, "stock_600519_qfq.csv"),
        "index_cache": lambda: meta._index_cache_path("sh000300"),
    }


@pytest.mark.parametrize("name", sorted(_state_accessors()))
def test_state_accessor_resolves_under_test_root(name, tmp_path):
    """每条 state 路径都必须落在本用例的临时目录，而不是仓库真实 data/。"""
    path = _state_accessors()[name]()
    assert _inside(path, tmp_path), f"{name} 未隔离：{path}（tmp={tmp_path}）"
    for real in REAL_DATA_DIRS:
        assert not _inside(path, real), f"{name} 仍指向真实目录：{path}"


def test_default_state_paths_unchanged_without_override(monkeypatch):
    """去掉测试注入后必须回到原来的生产默认路径（隔离不改变生产行为）。

    只检查运行期读取环境变量的访问器；模块常量（FLOW_DIR 等）由用例内的
    monkeypatch 注入，不会改变导入期默认值。
    """
    from stock_analysis.data_loader import events, flow, status, valuation
    from stock_analysis.data_sources import (
        astock, eastmoney_guard, market_watch, tencent_throttle)

    for key in _ISOLATION_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)

    assert eastmoney_guard.state_path().endswith(
        os.path.join("data", "state", "eastmoney_cooldown.json"))
    assert tencent_throttle.state_path().endswith(
        os.path.join("data", "state", "tencent_cooldown.json"))
    assert market_watch.cache_path().endswith(
        os.path.join("data", "state", "market_watch.json"))
    assert events.cache_path().endswith(
        os.path.join("data", "state", "events", "events.json"))
    assert status.cache_path().endswith(
        os.path.join("data", "state", "st_list.json"))
    assert astock._calendar_dir().endswith(os.path.join("data", "calendar"))
    # 上下文缓存的模块常量默认值不变
    assert flow.FLOW_DIR.endswith(os.path.join("data", "flow"))
    assert valuation.VALUATION_DIR.endswith(os.path.join("data", "valuation"))


# ---------------------------------------------------------------------------
# 全量守卫：真实 data 目录带毒 → 全量 pytest 仍须全绿，且毒文件不被改动
# ---------------------------------------------------------------------------
def _copy_repo(dest: Path) -> None:
    ignore = shutil.ignore_patterns(
        ".git", ".venv", "venv", "data", "__pycache__", ".pytest_cache",
        ".ruff_cache", ".mypy_cache", "evidence", ".pi", "*.pyc",
    )
    shutil.copytree(ROOT, dest, ignore=ignore, symlinks=True)


def _poison_real_state(data_dir: Path) -> None:
    """写入「若被读到就会改变结论」的毒数据（只为守卫，不参与生产）。"""
    state = data_dir / "state"
    state.mkdir(parents=True, exist_ok=True)
    (state / "eastmoney_cooldown.json").write_text(json.dumps({
        "schema": 1, "schema_version": 1, "reason": "http_444",
        "detail": "issue#124 guard poison",
        "blocked_at": "2026-11-16T00:00:00",
        "blocked_until": "2099-01-01T00:00:00",
        "seconds": 1800.0, "updated_at": "2026-11-16T00:00:00", "pid": 1,
    }), encoding="utf-8")
    (state / "tencent_cooldown.json").write_text(json.dumps({
        "schema_version": 1, "updated_at": "2026-11-16T00:00:00+00:00",
        "hosts": {host: 4070908800 for host in (
            "qt.gtimg.cn", "web.ifzq.gtimg.cn", "proxy.finance.qq.com",
            "sqt.gtimg.cn")},
        "last_reason": "guard poison", "pid": 1,
        "waf": {"hosts": {host: 4070908800 for host in (
            "qt.gtimg.cn", "web.ifzq.gtimg.cn")},
            "last_reason": "guard poison", "updated_at": "2026-11-16T00:00:00+00:00"},
    }), encoding="utf-8")
    # 监控池：把 600519 写成命中，读到就会多出「重点监控」块与告警。
    from stock_analysis.data_sources import market_watch
    market_watch.write_cache(
        [{"code": "600519", "name": "毒数据", "market": "SH",
          "start": "2026-11-16", "end": "2026-11-27", "reason": "monitor"}],
        fetched_at="2026-11-16T18:30:00", state_dir=str(state))
    # 事件：600519 解禁命中。
    section = {"available": True, "stale": False, "as_of": "2026-11-13",
               "fetched_at": "2026-11-13T18:35:00", "reason": None,
               "error": None, "items": [{"date": "2026-11-20",
                                         "detail": "毒数据"}], "warnings": []}
    (state / "events").mkdir(parents=True, exist_ok=True)
    (state / "events" / "events.json").write_text(json.dumps({
        "schema": 1, "as_of": "2026-11-13",
        "fetched_at": "2026-11-13T18:35:00",
        "600519": {key: dict(section) for key in (
            "lockup", "holder_trades", "forecast", "pledge")},
    }), encoding="utf-8")
    # ST 名单：把 600519 标成 ST。读到就会把 is_st 判成 True。
    (state / "st_list.json").write_text(json.dumps({
        "schema": 2, "source": "eastmoney", "coverage": "沪深京",
        "fetched_at": "2026-11-16T18:30:00", "session_date": "2026-11-16",
        "count": 1, "codes": ["600519"],
        "sha256": hashlib.sha256(b"600519").hexdigest(),
        "stocks": {"600519": {"name": "*ST毒数据", "market": "sh",
                              "st_type": "*ST", "price": 1.0,
                              "pct_change": -5.0}},
        "state": "ok", "merged_from": [],
    }), encoding="utf-8")
    # 资金流 / 估值：600519 有「好看」的缓存，读到就会让 evidence 可用。
    flow = data_dir / "flow"
    flow.mkdir(parents=True, exist_ok=True)
    rows = [{"date": f"2026-11-{day:02d}", "main_net": 1e8}
            for day in range(1, 21)]
    (flow / "600519.json").write_text(json.dumps({
        "schema_version": "1.0", "code": "600519",
        "source": "eastmoney_push2his", "caliber": "eastmoney_main_net",
        "upstream": "a-stock-data v3.10.4 §4.5", "fetched_at":
        "2026-11-16T16:30:00", "as_of": "2026-11-20", "rows": rows,
    }), encoding="utf-8")
    valuation = data_dir / "valuation"
    valuation.mkdir(parents=True, exist_ok=True)
    val_rows = [{"date": f"2026-11-{day:02d}", "close": 100.0,
                 "pe_ttm": 10.0, "pb": 1.0, "ps": 2.0, "pcf": 5.0,
                 "turnover": 1.0, "trade_status": 1, "is_st": 1}
                for day in range(1, 21)]
    (valuation / "600519.json").write_text(json.dumps({
        "schema_version": "1.0", "code": "600519",
        "source": "baostock_valuation", "upstream": "毒数据",
        "fetched_at": "2026-11-16T16:30:00", "as_of": "2026-11-20",
        "pub_lag_days": 0, "rows": val_rows,
    }), encoding="utf-8")
    # 交易日历：把 11 月全部标成非交易日；读到就会改变「期望交易日」。
    calendar = data_dir / "calendar"
    calendar.mkdir(parents=True, exist_ok=True)
    (calendar / "szse_2026-11.json").write_text(json.dumps({
        "source": "astock_calendar", "provider": "szse", "year": 2026,
        "month": 11,
        "days": {f"2026-11-{day:02d}": False for day in range(1, 31)},
    }), encoding="utf-8")
    # 行情 / 指数：毒价格（100 → 1），读到就会改变回测与 regime。
    (data_dir / "stock_600519_qfq.csv").write_text(
        "date,open,high,low,close,volume\n"
        "2026-11-16,1.0,1.0,1.0,1.0,1000\n", encoding="utf-8")
    (data_dir / "index_sh000300.csv").write_text(
        "date,open,high,low,close,volume\n"
        "2026-11-16,1.0,1.0,1.0,1.0,1000\n", encoding="utf-8")


def _tree_hashes(root: Path):
    hashes = {}
    if not root.exists():
        return hashes
    for path in sorted(root.rglob("*")):
        if path.is_file():
            hashes[str(path.relative_to(root))] = hashlib.sha256(
                path.read_bytes()).hexdigest()
    return hashes


def test_full_suite_is_immune_to_poisoned_real_state(tmp_path):
    """真实 data/ 里带毒 → 全量 pytest 仍全绿，且毒文件逐字节不变。

    这是 issue #124 的验收守卫：任何新增用例若直接读 ``stock_analysis/data``
    下的真实 state，毒数据就会改变断言结果（或写入毒目录被哈希发现）。
    """
    if os.environ.get(GUARD_CHILD_ENV) == "1":
        pytest.skip("守卫子进程：避免递归复制与递归跑全量")

    copy_root = tmp_path / "repo"
    _copy_repo(copy_root)
    copy_src = copy_root / "source"
    _poison_real_state(copy_src / "stock_analysis" / "data")
    # fund_analysis/data 同样建出来（真实机器上可能存在），防止写入落到副本外。
    (copy_src / "fund_analysis" / "data").mkdir(parents=True, exist_ok=True)

    # 毒数据路径按副本重算，避免把父仓库路径带进断言。
    copy_real = (copy_src / "stock_analysis" / "data",
                 copy_src / "fund_analysis" / "data")
    before = {str(root): _tree_hashes(root) for root in copy_real}

    env = dict(os.environ)
    for key in _ISOLATION_ENV_KEYS:
        env.pop(key, None)
    env.pop("PYTHONPATH", None)
    env[GUARD_CHILD_ENV] = "1"
    env["PYTHONDONTWRITEBYTECODE"] = "1"

    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "tests/", "-q", "--tb=line",
         "-p", "no:cacheprovider"],
        cwd=str(copy_src), env=env, capture_output=True, text=True, timeout=900,
    )
    tail = (proc.stdout or "")[-4000:] + (proc.stderr or "")[-2000:]
    assert proc.returncode == 0, f"毒数据下全量测试失败：\n{tail}"
    match = re.search(r"(\d+) passed", proc.stdout or "")
    assert match and int(match.group(1)) > 0, f"未取得通过数：\n{tail}"
    assert " failed" not in (proc.stdout or ""), tail

    after = {str(root): _tree_hashes(root) for root in copy_real}
    for root, hashes in before.items():
        assert after.get(root) == hashes, (
            f"测试读取/写入了真实 data 目录：{root}\n"
            f"新增：{sorted(set(after.get(root, {})) - set(hashes))}\n"
            f"变更：{sorted(k for k in hashes if hashes[k] != after.get(root, {}).get(k))}"
        )
