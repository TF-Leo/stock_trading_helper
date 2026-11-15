# -*- coding: utf-8 -*-
"""issue #119：``tools/collect_evidence.py::step_asof`` 必须把 exit 50 认成休市。

休市日 ``cli.py --as-of <过去交易日>`` 仍返回 ``status=market_closed`` / 退出码
50（``--as-of`` 只截断行情，不改「今天是不是交易日」）。旧实现把非 0/10 一律
判失败，导致 as-of 取证整步变红。这里离线假 run/write，锁住：
  * 50 → 跳过整步、``ok=True``、写明 ``skipped=market_closed``；
  * 30 → 仍判失败（其余退出码语义不变）。

测试标的用公开基准代码 600519，不触碰任何真实持仓代码。
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parents[1]
ROOT = SRC.parent
TOOLS = ROOT / "tools"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


def _load_tool():
    spec = importlib.util.spec_from_file_location(
        "collect_evidence_tool", TOOLS / "collect_evidence.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _patch(module, monkeypatch, results, code="600519"):
    calls, writes = [], {}

    def fake_run(cmd, cwd=None, timeout=1800):
        calls.append(cmd)
        return results[cmd[-1]]

    monkeypatch.setattr(module, "ASOF_CODE", code)
    monkeypatch.setattr(module, "run", fake_run)
    monkeypatch.setattr(module, "write",
                        lambda name, text: writes.__setitem__(name, text))
    return calls, writes


def test_step_asof_exit_50_is_skipped_not_failed(monkeypatch):
    module = _load_tool()
    payload = json.dumps({"recommendation": {"status": "market_closed"}})
    calls, writes = _patch(module, monkeypatch, {
        "2026-10-22": (50, payload, "", 0.1),
        "2026-10-23": (50, payload, "", 0.1),
    })

    result = module.step_asof()

    assert result["ok"] is True
    assert result["skipped"] == "market_closed"
    assert "休市" in result["note"]
    assert len(calls) == 1, "首日确认休市后应跳过整步，不再跑次日"
    assert calls[0][2] == "600519"
    summary = json.loads(writes["asof_summary.json"])
    assert summary == {"2026-10-22": {"exit": 50, "skipped": "market_closed",
                                      "status": "market_closed"}}


def test_step_asof_other_exit_codes_still_fail(monkeypatch):
    module = _load_tool()
    calls, writes = _patch(module, monkeypatch, {
        "2026-10-22": (30, "", "boom", 0.1),
        "2026-10-23": (30, "", "boom", 0.1),
    })

    result = module.step_asof()

    assert result["ok"] is False
    assert "skipped" not in result
    assert len(calls) == 2, "非 50 失败不跳过次日"
    summary = json.loads(writes["asof_summary.json"])
    assert all(row["exit"] == 30 and row["error"] for row in summary.values())


# ---------------------------------------------------------------------------
# issue #119 P3：休市日的 idempotent / timing 也必须认 exit 50、跳过且不计失败
# ---------------------------------------------------------------------------
def _patch_all_runs(module, monkeypatch, exit_code=50,
                    payload='{"recommendation": {"status": "market_closed"}}'):
    writes = {}
    monkeypatch.setattr(module, "run", lambda cmd, cwd=None, timeout=1800:
                        (exit_code, payload, "", 0.1))
    monkeypatch.setattr(module, "write",
                        lambda name, text: writes.__setitem__(name, text))
    return writes


def test_step_idempotent_exit_50_is_skipped(monkeypatch):
    module = _load_tool()
    writes = _patch_all_runs(module, monkeypatch)

    result = module.step_idempotent()

    assert result["ok"] is True
    assert result["skipped"] == "market_closed"
    assert "休市" in result["note"]
    saved = json.loads(writes["idem_result.json"])
    assert saved["exit"] == [50, 50] and saved["skipped"] == "market_closed"


def test_step_idempotent_other_exit_codes_still_fail(monkeypatch):
    module = _load_tool()
    _patch_all_runs(module, monkeypatch, exit_code=30, payload="boom")

    result = module.step_idempotent()

    assert result["ok"] is False
    assert "skipped" not in result


def test_step_timing_exit_50_is_skipped(monkeypatch):
    module = _load_tool()
    _patch_all_runs(module, monkeypatch)

    result = module.step_timing()

    assert result["ok"] is True
    assert result["skipped"] == "market_closed"
    assert "休市" in result["note"]
    assert set(result["observed"]) == {"full", "fast"}


def test_step_timing_mixed_50_and_ok_still_fails(monkeypatch):
    """只有两条路径都 50（今日休市）才跳过；混合退出码仍是失败。"""
    module = _load_tool()
    codes = iter([50, 0])

    monkeypatch.setattr(module, "run", lambda cmd, cwd=None, timeout=1800:
                        (next(codes), "", "", 0.1))
    monkeypatch.setattr(module, "write", lambda name, text: None)

    result = module.step_timing()

    assert result["ok"] is False
    assert "skipped" not in result