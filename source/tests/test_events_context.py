# -*- coding: utf-8 -*-
"""T7（issue #1 / R11 缺口表 #7–#10）：事件风险 —— 离线测试。

覆盖：
* 数据层：缓存缺失 / 代码缺失 / 缓存损坏 / 子项失败隔离 / 过期判定 /
  ``--as-of`` 回放不套用当前快照 / 基金显式不支持；
* 渲染层：四类事件命中 / 未命中 / 过期（**不得写成「无事件」**）/ 第 20 日
  与阈值边界 / 每类至多一行 / 不改写 action / confidence；
* 预计算：一票四端点、10 票 40 请求的串行耗时估算、失败隔离、保留旧子项；
* 端点：``code=9201`` 空结果、错误码 / 多页 / 非列表抛错、字段单位换算、
  北交所质押显式空结果；
* 盘前路径零请求（socket 与 ``_http_get`` 双保险）；
* CLI：``evidence.events`` additive、错误路径键同构（G35）。

所有网络入口都被打桩；测试使用公开代码 + 虚构事件数据，不出现任何真实持仓。
"""
from __future__ import annotations

import datetime as _dt
import json
import os
import socket
import sys
from pathlib import Path

import pandas as pd
import pytest

SRC = Path(__file__).resolve().parents[1]
for _path in (SRC, SRC.parent / "tools"):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from stock_analysis.advice_render import (  # noqa: E402
    EVENT_STALE_MARK,
    EVENT_THRESHOLDS,
    render_advice,
)
from stock_analysis.data_loader import events as ev  # noqa: E402

import precompute_events as pe  # noqa: E402

NOW = _dt.datetime(2026, 11, 16, 9, 15)  # 北京时间，周一盘前
REF_DAY = "2026-11-13"                   # 上一交易日（缓存快照日）


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------
def _section(items=None, available=True, stale=False, reason=None,
             error=None, as_of=REF_DAY, fetched_at=REF_DAY + "T18:35:00"):
    return {
        "available": available,
        "stale": stale,
        "as_of": as_of,
        "fetched_at": fetched_at,
        "reason": reason,
        "error": error,
        "items": list(items or []),
    }


def _lockup_item(day="2026-11-18", ratio=1.5, shares=1200.0):
    return {"date": day, "type": "首发原股东限售股份",
            "shares_10k": shares, "free_shares_10k": shares * 100,
            "ratio_pct": ratio}


def _holder_item(notice="2026-11-10", shares=-620.0, pct=-0.8,
                 direction="减持"):
    return {"notice_date": notice, "holder": "示例股东", "direction": direction,
            "change_shares_10k": shares, "change_pct_total": pct,
            "change_pct_float": pct, "avg_price": 10.0, "channel": "二级市场",
            "start_date": "2026-11-05", "end_date": "2026-11-09"}


def _forecast_item(notice="2026-11-10", ftype="预减",
                   lower=-1.2e8, upper=-0.8e8,
                   change_low=-50.0, change_high=-30.0,
                   indicator="归母净利润"):
    return {"notice_date": notice, "report_date": "2026-09-30",
            "indicator": indicator, "forecast_type": ftype,
            "amount_lower": lower, "amount_upper": upper,
            "change_pct_lower": change_low, "change_pct_upper": change_high}


def _pledge_item(day="2026-11-07", ratio=22.5):
    return {"date": day, "ratio_pct": ratio, "pledged_shares_10k": 1000.0,
            "pledged_mktcap_10k": 10000.0, "pledge_count": 3}


def _sections(**overrides):
    sections = {
        "lockup": _section([_lockup_item()]),
        "holder_trades": _section([_holder_item()]),
        "forecast": _section([_forecast_item()]),
        "pledge": _section([_pledge_item()]),
    }
    sections.update(overrides)
    return sections


def _cache_payload(code_sections, fetched_at=REF_DAY + "T18:35:00",
                   stale=False, schema=1):
    payload = {
        "schema": schema,
        "source": "eastmoney_datacenter",
        "upstream": ev.UPSTREAM,
        "fetched_at": fetched_at,
        "stale": stale,
    }
    payload.update(code_sections)
    return payload


def _write_cache(events_dir, payload):
    os.makedirs(events_dir, exist_ok=True)
    path = os.path.join(events_dir, "events.json")
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False)


def _events_block(**overrides):
    block = {
        "available": True,
        "stale": False,
        "as_of": REF_DAY,
        "fetched_at": REF_DAY + "T18:35:00",
        "reason": None,
        "code": "600000",
        "lockup": _section([]),
        "holder_trades": _section([]),
        "forecast": _section([]),
        "pledge": _section([]),
    }
    block.update(overrides)
    return block


PAYLOAD = {
    "schema_version": "1.0",
    "request": {"code": "600000", "type": "stock"},
    "instrument": {"code": "600000", "name": "示例股份", "board": "sh_main",
                   "is_st": False, "is_suspended": False, "quote_stale": False,
                   "status_source": "offline"},
    "data": {"last_close": 10.0, "end": REF_DAY, "start": "2020-01-01",
             "expected_date": REF_DAY, "lag_days": 0, "stale": False},
    "market_regime": {"label": "Bull", "confidence": 0.7},
    "recommendation": {
        "status": "ok", "action": "buy", "confidence": 0.72,
        "action_reason": ["触发买点"], "position_fraction": 0.15,
        "horizon_days": [20, 20], "review_date": "2026-12-11",
        "entry_zone": {"lower": 9.8, "upper": 10.2},
        "stop_loss": 9.0, "take_profit": 12.0,
    },
    "evidence": {"triggers": [], "flow": {"available": False},
                 "valuation": {"available": False}},
    "diagnostics": {"warnings": []},
    "disclaimer": "本结果不构成投资建议，市场有风险，投资需谨慎。",
}


def _payload(events=None):
    payload = json.loads(json.dumps(PAYLOAD))
    if events is not None:
        payload["evidence"]["events"] = events
    return payload


# ===========================================================================
# 1) 数据层：缺失 / 损坏 / 过期 / 失败隔离
# ===========================================================================
def test_missing_cache_is_stale_not_empty(tmp_path):
    """缓存缺失必须 ``stale=True``：渲染层据此报「过期」，不是「无事件」。"""
    block, notes = ev.events_evidence("600000", "stock",
                                      events_dir=str(tmp_path), now=NOW)
    assert block["available"] is False
    assert block["stale"] is True
    assert block["reason"] == "cache_missing"
    for key in ev.SECTION_KEYS:
        assert block[key]["available"] is False
        assert block[key]["stale"] is True
    assert any("不可用" in note for note in notes)
    assert EVENT_STALE_MARK in render_advice(_payload(block))


def test_fund_is_not_supported_and_silent(tmp_path):
    block, notes = ev.events_evidence("000001", "fund",
                                      events_dir=str(tmp_path), now=NOW)
    assert block["reason"] == "fund_not_supported"
    assert notes == []
    output = render_advice(_payload(block))
    assert EVENT_STALE_MARK not in output
    assert "解禁" not in output and "质押" not in output


def test_code_missing_in_existing_cache(tmp_path):
    _write_cache(str(tmp_path), _cache_payload({"600001": _sections()}))
    block, notes = ev.events_evidence("600000", "stock",
                                      events_dir=str(tmp_path), now=NOW)
    assert block["reason"] == "code_missing"
    assert block["stale"] is True
    assert any("不可用" in note for note in notes)


def test_corrupt_cache_is_stale_not_empty(tmp_path):
    Path(str(tmp_path), "events.json").write_text("{not json", encoding="utf-8")
    block, _notes = ev.events_evidence("600000", "stock",
                                       events_dir=str(tmp_path), now=NOW)
    assert block["reason"] == "cache_invalid"
    assert block["stale"] is True
    assert EVENT_STALE_MARK in render_advice(_payload(block))


def test_cache_roundtrip_shape(tmp_path):
    _write_cache(str(tmp_path), _cache_payload({"600000": _sections()}))
    block, notes = ev.events_evidence("600000", "stock",
                                      events_dir=str(tmp_path), now=NOW)
    assert set(block) >= set(ev.EVENTS_KEYS)
    for key in ev.SECTION_KEYS:
        assert set(block[key]) == set(ev.SECTION_BLOCK_KEYS)
        assert block[key]["available"] is True
        assert block[key]["stale"] is False
        assert block[key]["items"]
    assert notes == []


def test_stale_by_fetch_date(tmp_path):
    sections = _sections()
    for section in sections.values():
        section["fetched_at"] = "2026-11-12T18:35:00"
    payload = _cache_payload({"600000": sections},
                             fetched_at="2026-11-12T18:35:00")
    _write_cache(str(tmp_path), payload)
    block, notes = ev.events_evidence("600000", "stock",
                                      events_dir=str(tmp_path), now=NOW)
    assert block["stale"] is True
    assert block["lockup"]["stale"] is True
    assert any("过期" in note for note in notes)
    assert EVENT_STALE_MARK in render_advice(_payload(block))


def test_section_failure_is_isolated_and_carries_prior(tmp_path):
    sections = _sections(
        holder_trades=_section([_holder_item()], error="ConnectionError: rst"),
    )
    # 预计算失败路径会把 stale 置 True；这里模拟缓存已落盘后的读取。
    sections["holder_trades"]["stale"] = True
    _write_cache(str(tmp_path), _cache_payload({"600000": sections}))
    block, notes = ev.events_evidence("600000", "stock",
                                      events_dir=str(tmp_path), now=NOW)
    assert block["holder_trades"]["available"] is True
    assert block["holder_trades"]["stale"] is True
    assert block["holder_trades"]["items"]      # 旧数据必须保留
    assert block["lockup"]["stale"] is False    # 其他三类不受影响
    assert block["forecast"]["stale"] is False
    assert block["pledge"]["stale"] is False
    assert any("取数失败" in note for note in notes)


def test_replay_suppresses_current_snapshot(tmp_path):
    _write_cache(str(tmp_path), _cache_payload({"600000": _sections()}))
    block, notes = ev.events_evidence("600000", "stock", as_of="2026-06-01",
                                      events_dir=str(tmp_path), now=NOW)
    assert block["reason"] == "replay_not_supported"
    assert notes and "回放" in notes[0]
    output = render_advice(_payload(block))
    assert EVENT_STALE_MARK not in output
    assert "解禁" not in output


def test_premarket_read_path_makes_zero_requests(monkeypatch, tmp_path):
    """CLI 主路径只读缓存：socket / 预计算 HTTP 入口都不得被调用。"""
    _write_cache(str(tmp_path), _cache_payload({"600000": _sections()}))

    def _boom(*args, **kwargs):
        raise AssertionError("盘前路径不得触网")

    monkeypatch.setattr(socket, "socket", _boom)
    monkeypatch.setattr(socket, "create_connection", _boom)
    monkeypatch.setattr(pe, "_http_get", _boom)
    block, _notes = ev.events_evidence("600000", "stock",
                                       events_dir=str(tmp_path), now=NOW)
    assert block["available"] is True
    assert render_advice(_payload(block))


# ===========================================================================
# 2) 渲染层：命中 / 未命中 / 边界 / 阈值
# ===========================================================================
def _render(**sections):
    return render_advice(_payload(_events_block(**sections)))


def test_render_all_four_event_lines():
    output = _render(**{
        "lockup": _section([_lockup_item()]),
        "holder_trades": _section([_holder_item()]),
        "forecast": _section([_forecast_item()]),
        "pledge": _section([_pledge_item()]),
    })
    assert "⚠️ 解禁：2026-11-18 解禁 1200 万股（占总股本 1.5%）" in output
    assert "⚠️ 股东减持：近 20 日 1 笔，合计 620 万股（占总股本 0.8%）" in output
    assert ("⚠️ 业绩预告：预减（归母净利润 -1.2亿~-0.8亿，"
            "2026-11-10 公告）") in output
    assert "⚠️ 股权质押：质押比例 22.5%（2026-11-07 中国结算）" in output
    assert "None" not in output


def test_render_no_event_lines_disappear():
    output = _render()
    for needle in ("⚠️ 解禁", "⚠️ 股东减持", "⚠️ 业绩预告", "⚠️ 股权质押"):
        assert needle not in output
    assert EVENT_STALE_MARK not in output


def test_render_one_line_per_event_class():
    output = _render(**{
        "lockup": _section([_lockup_item(day="2026-11-18"),
                            _lockup_item(day="2026-11-19", ratio=3.0)]),
        "holder_trades": _section([_holder_item(), _holder_item(notice="2026-11-11")]),
        "forecast": _section([_forecast_item(), _forecast_item(notice="2026-11-11")]),
    })
    assert output.count("⚠️ 解禁：") == 1
    assert output.count("⚠️ 股东减持：") == 1
    assert output.count("⚠️ 业绩预告：") == 1


def test_render_stale_line_is_not_no_event():
    block = _events_block(
        stale=True,
        lockup=_section([], stale=True, as_of="2026-11-12"),
        holder_trades=_section([], stale=True, as_of="2026-11-12"),
        forecast=_section([], stale=True, as_of="2026-11-12"),
        pledge=_section([], stale=True, as_of="2026-11-12"),
    )
    output = render_advice(_payload(block))
    assert EVENT_STALE_MARK in output
    assert "（2026-11-12）" in output
    # 过期行必须同时点名受影响的子项，不能让人以为「四类都没有事件」。
    assert "解禁" in output and "股权质押" in output


def test_render_stale_and_events_can_coexist():
    block = _events_block(
        stale=True,
        lockup=_section([_lockup_item()], stale=True, as_of="2026-11-12"),
    )
    output = render_advice(_payload(block))
    assert EVENT_STALE_MARK in output
    assert "⚠️ 解禁：" in output  # 旧缓存里的重大事件仍然要报


def test_render_hidden_reasons_are_silent():
    for reason in ("fund_not_supported", "replay_not_supported", "not_supported"):
        block = _events_block(
            available=False, stale=False, reason=reason,
            lockup=_section([], available=False, reason=reason),
            holder_trades=_section([], available=False, reason=reason),
            forecast=_section([], available=False, reason=reason),
            pledge=_section([], available=False, reason=reason),
        )
        output = render_advice(_payload(block))
        assert EVENT_STALE_MARK not in output, reason


def test_window_day_20_included_and_day_21_excluded():
    # 快照日 2026-11-13；+20 天 = 2026-12-03，+21 天 = 2026-12-04。
    assert "⚠️ 解禁" in _render(lockup=_section([_lockup_item(day="2026-12-03")]))
    assert "⚠️ 解禁" not in _render(lockup=_section([_lockup_item(day="2026-12-04")]))
    # 回看窗口同理：-20 天 = 2026-10-24，-21 天 = 2026-10-23。
    assert "⚠️ 股东减持" in _render(
        holder_trades=_section([_holder_item(notice="2026-10-24")]))
    assert "⚠️ 股东减持" not in _render(
        holder_trades=_section([_holder_item(notice="2026-10-23")]))
    assert "⚠️ 业绩预告" in _render(
        forecast=_section([_forecast_item(notice="2026-10-24")]))
    assert "⚠️ 业绩预告" not in _render(
        forecast=_section([_forecast_item(notice="2026-10-23")]))


def test_lockup_threshold_boundaries():
    """占总股本 1.0% 刚好命中；0.99% 不命中；500 万股刚好命中。"""
    assert "⚠️ 解禁" in _render(
        lockup=_section([_lockup_item(ratio=1.0, shares=100.0)]))
    assert "⚠️ 解禁" not in _render(
        lockup=_section([_lockup_item(ratio=0.99, shares=100.0)]))
    assert "⚠️ 解禁" in _render(
        lockup=_section([_lockup_item(ratio=None, shares=500.0)]))
    assert "⚠️ 解禁" not in _render(
        lockup=_section([_lockup_item(ratio=None, shares=499.0)]))


def test_holder_threshold_boundaries():
    """合计 0.5% 刚好命中；0.49% 不命中；500 万股刚好命中。"""
    assert "⚠️ 股东减持" in _render(
        holder_trades=_section([_holder_item(pct=-0.5, shares=-1.0)]))
    assert "⚠️ 股东减持" not in _render(
        holder_trades=_section([_holder_item(pct=-0.49, shares=-1.0)]))
    assert "⚠️ 股东减持" in _render(
        holder_trades=_section([_holder_item(pct=None, shares=-500.0)]))
    assert "⚠️ 股东减持" not in _render(
        holder_trades=_section([_holder_item(pct=None, shares=-499.0)]))


def test_forecast_threshold_boundaries():
    """同比上限 -20% 刚好命中；-19.9% 不命中；负面类型即使无量也命中。"""
    assert "⚠️ 业绩预告" in _render(
        forecast=_section([_forecast_item(ftype="不确定", change_high=-20.0)]))
    assert "⚠️ 业绩预告" not in _render(
        forecast=_section([_forecast_item(ftype="不确定", change_high=-19.9)]))
    assert "⚠️ 业绩预告" in _render(
        forecast=_section([_forecast_item(ftype="首亏", lower=None, upper=None,
                                          change_low=None, change_high=None)]))


def test_pledge_threshold_boundary():
    assert "⚠️ 股权质押" in _render(
        pledge=_section([_pledge_item(ratio=20.0)]))
    assert "⚠️ 股权质押" not in _render(
        pledge=_section([_pledge_item(ratio=19.9)]))


def test_holder_ignores_increases():
    assert "⚠️ 股东减持" not in _render(
        holder_trades=_section([_holder_item(direction="增持", shares=900.0,
                                             pct=1.0)]))


def test_render_does_not_rewrite_action_or_confidence():
    with_events = _render(lockup=_section([_lockup_item()]),
                          pledge=_section([_pledge_item()]))
    without = _render()
    assert "动作 buy｜置信 72%" in with_events
    assert "动作 buy｜置信 72%" in without
    stripped = [line for line in with_events.splitlines()
                if not line.startswith("⚠️ 解禁")
                and not line.startswith("⚠️ 股权质押")]
    assert stripped == without.splitlines()


def test_render_missing_events_block_is_noop():
    assert render_advice(_payload()) == render_advice(_payload())


def test_threshold_table_is_explicit():
    """阈值必须写死在代码常量里（PR 说明引用同一张表）。"""
    assert EVENT_THRESHOLDS == {
        "window_days": 20,
        "lockup_ratio_pct_min": 1.0,
        "lockup_shares_10k_min": 500.0,
        "holder_reduce_pct_min": 0.5,
        "holder_reduce_shares_10k_min": 500.0,
        "forecast_change_pct_max": -20.0,
        "pledge_ratio_pct_min": 20.0,
    }


# ===========================================================================
# 3) 预计算：一票四端点 / 串行 / 失败隔离 / 保留旧子项
# ===========================================================================
class _Clock:
    def __init__(self, start=_dt.datetime(2026, 11, 13, 18, 35)):
        self.base = start
        self.seconds = 0.0

    def now(self):
        return self.base + _dt.timedelta(seconds=self.seconds)

    def sleep(self, seconds):
        self.seconds += float(seconds)


def _fake_fetchers(calls=None, fail=()):
    calls = calls if calls is not None else []

    def make(key):
        def fetch(code, as_of):
            calls.append((key, code, as_of))
            if key in fail:
                raise RuntimeError("boom")
            return [_lockup_item()] if key == "lockup" else []
        return fetch

    return [(key, make(key)) for key in ("lockup", "holder_trades",
                                         "forecast", "pledge")]


def test_precompute_one_request_per_endpoint_per_code(tmp_path):
    calls = []
    stats = pe.precompute(["600000", "600001", "600002"], out_dir=str(tmp_path),
                          fetchers=_fake_fetchers(calls),
                          sleep_fn=lambda _s: None, now_fn=_Clock().now)
    assert stats["requests"] == 12
    assert stats["ok_sections"] == 12
    assert stats["failed_sections"] == 0
    assert [key for key, _code, _day in calls] == [
        "lockup", "holder_trades", "forecast", "pledge"] * 3
    payload = json.loads(Path(str(tmp_path), "events.json").read_text(encoding="utf-8"))
    assert payload["schema"] == ev.EVENTS_SCHEMA
    for code in ("600000", "600001", "600002"):
        assert set(payload[code]) == set(ev.SECTION_KEYS)


def test_precompute_10_codes_serial_estimate(tmp_path):
    """10 只 × 4 端点 = 40 请求；串行下限 (40-1)×1s = 39s。"""
    calls = []
    clock = _Clock()
    stats = pe.precompute([f"60000{i}" for i in range(10)],
                          out_dir=str(tmp_path),
                          fetchers=_fake_fetchers(calls),
                          sleep_fn=clock.sleep, now_fn=clock.now)
    assert stats["requests"] == 40
    assert len(calls) == 40
    assert clock.seconds == pytest.approx(39.0)
    assert stats["wall_seconds"] == pytest.approx(39.0)

    estimate = pe.estimate(10)
    assert estimate["requests"] == 40
    assert estimate["min_serial_seconds"] == 39.0
    assert estimate["estimated_seconds"] == 40.0
    with pytest.raises(ValueError):
        pe.estimate(10, interval=0.9)
    with pytest.raises(ValueError):
        pe.precompute([], interval=0.9)


def test_precompute_is_serial_not_concurrent(tmp_path):
    active, max_active = [0], [0]

    def make(key):
        def fetch(code, as_of):
            active[0] += 1
            max_active[0] = max(max_active[0], active[0])
            active[0] -= 1
            return []
        return fetch

    fetchers = [(key, make(key)) for key in
                ("lockup", "holder_trades", "forecast", "pledge")]
    pe.precompute(["600000", "600001"], out_dir=str(tmp_path), fetchers=fetchers,
                  sleep_fn=lambda _s: None, now_fn=_Clock().now)
    assert max_active[0] == 1


def test_precompute_failure_is_isolated(tmp_path):
    calls = []
    stats = pe.precompute(["600000", "600001"], out_dir=str(tmp_path),
                          retries=0,
                          fetchers=_fake_fetchers(calls, fail={"lockup"}),
                          sleep_fn=lambda _s: None, now_fn=_Clock().now)
    assert stats["failed_sections"] == 2
    assert stats["ok_sections"] == 6
    assert len(calls) == 8          # 失败后仍继续做其余三个子项
    payload = json.loads(Path(str(tmp_path), "events.json").read_text(encoding="utf-8"))
    for code in ("600000", "600001"):
        assert payload[code]["lockup"]["available"] is False
        assert payload[code]["lockup"]["stale"] is True
        assert "boom" in payload[code]["lockup"]["error"]
        assert payload[code]["holder_trades"]["available"] is True
        assert payload[code]["holder_trades"]["stale"] is False


def test_precompute_preserves_prior_section_on_failure(tmp_path):
    _write_cache(str(tmp_path), _cache_payload({"600000": _sections()}))
    pe.precompute(["600000"], out_dir=str(tmp_path),
                  fetchers=_fake_fetchers(fail={"holder_trades"}),
                  sleep_fn=lambda _s: None, now_fn=_Clock().now)
    payload = json.loads(Path(str(tmp_path), "events.json").read_text(encoding="utf-8"))
    section = payload["600000"]["holder_trades"]
    assert section["available"] is True
    assert section["stale"] is True
    assert section["items"]                       # 旧数据保留
    assert "boom" in section["error"]


def test_precompute_retries_same_section(tmp_path):
    attempts = {"n": 0}

    def flaky(code, as_of):
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise RuntimeError("transient")
        return []

    fetchers = [("lockup", flaky), ("holder_trades", lambda c, d: []),
                ("forecast", lambda c, d: []), ("pledge", lambda c, d: [])]
    stats = pe.precompute(["600000"], out_dir=str(tmp_path), retries=1,
                          fetchers=fetchers, sleep_fn=lambda _s: None,
                          now_fn=_Clock().now)
    assert attempts["n"] == 2
    assert stats["failed_sections"] == 0
    assert stats["requests"] == 5


# ===========================================================================
# 4) 端点契约（上游字段 / 空结果 / 错误）
# ===========================================================================
class _FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


def _patch_datacenter(monkeypatch, payload):
    monkeypatch.setattr(pe, "_http_get",
                        lambda *a, **k: _FakeResponse(payload))


def test_datacenter_9201_is_legal_empty(monkeypatch):
    _patch_datacenter(monkeypatch, {"code": 9201})
    assert pe._datacenter("RPT_LIFT_STAGE", '(SECURITY_CODE="600000")') == []


@pytest.mark.parametrize("payload,needle", [
    ({"code": 9501, "message": "sort mismatch"}, "9501"),
    ({"code": 0, "result": None}, "result"),
    ({"code": 0, "result": {"pages": 2, "data": []}}, "2 页"),
    ({"code": 0, "result": {"pages": 1, "data": "nope"}}, "对象列表"),
    ({"code": 0, "result": {"pages": 1, "data": None, "count": 5}}, "data"),
    ("not-json", "对象"),
])
def test_datacenter_broken_structure_raises(monkeypatch, payload, needle):
    _patch_datacenter(monkeypatch, payload)
    with pytest.raises(pe.EventsFetchError) as exc:
        pe._datacenter("RPT_LIFT_STAGE")
    assert needle in str(exc.value)


def test_datacenter_partial_page_allows_latest_pledge_row(monkeypatch):
    """质押表按个股累积多年历史：只要第 1 页最新一行时允许 pages>1。"""
    _patch_datacenter(monkeypatch, {"code": 0, "result": {
        "pages": 633, "count": 633,
        "data": [{"SECURITY_CODE": "600000", "TRADE_DATE": "2026-11-13 00:00:00",
                  "PLEDGE_RATIO": 22.5, "REPURCHASE_BALANCE": 1000,
                  "REPURCHASE_UNLIMITED_BALANCE": 400,
                  "REPURCHASE_LIMITED_BALANCE": 600}],
    }})
    rows = pe._datacenter("RPT_CSDC_LIST", page_size=1,
                          allow_partial_page=True)
    assert len(rows) == 1
    assert rows[0]["TRADE_DATE"] == "2026-11-13 00:00:00"


def test_datacenter_partial_page_never_accepts_empty_first_page(monkeypatch):
    _patch_datacenter(monkeypatch, {"code": 0, "result": {
        "pages": 633, "count": 633, "data": []}})
    with pytest.raises(pe.EventsFetchError):
        pe._datacenter("RPT_CSDC_LIST", page_size=1, allow_partial_page=True)


def test_datacenter_non_2xx_and_non_json_raise(monkeypatch):
    monkeypatch.setattr(pe, "_http_get",
                        lambda *a, **k: _FakeResponse({}, status_code=500))
    with pytest.raises(pe.EventsFetchError):
        pe._datacenter("RPT_LIFT_STAGE")

    monkeypatch.setattr(pe, "_http_get",
                        lambda *a, **k: _FakeResponse(ValueError("no json")))
    with pytest.raises(pe.EventsFetchError):
        pe._datacenter("RPT_LIFT_STAGE")


def test_lockup_ratio_decimal_is_multiplied_by_100(monkeypatch):
    """FREE_RATIO 是小数（×100 得 %）；股数必须取 ABLE_FREE_SHARES。"""
    _patch_datacenter(monkeypatch, {"code": 0, "result": {"pages": 1, "data": [{
        "FREE_DATE": "2026-11-18 00:00:00", "FREE_SHARES_TYPE": "首发原股东限售股份",
        "FREE_SHARES": 999999, "ABLE_FREE_SHARES": 1200, "FREE_RATIO": 0.015,
    }]}})
    items = pe.fetch_lockup("600000", REF_DAY)
    assert items == [{
        "date": "2026-11-18", "type": "首发原股东限售股份",
        "shares_10k": 1200.0, "free_shares_10k": 999999.0, "ratio_pct": 1.5,
    }]


def test_holder_sign_direction_mismatch_raises(monkeypatch):
    _patch_datacenter(monkeypatch, {"code": 0, "result": {"pages": 1, "data": [{
        "NOTICE_DATE": "2026-11-10", "SECURITY_CODE": "600000",
        "HOLDER_NAME": "示例股东", "DIRECTION": "减持",
        "CHANGE_NUM_SYMBOL": 100, "START_DATE": "2026-11-05",
        "END_DATE": "2026-11-09",
    }]}})
    with pytest.raises(pe.EventsFetchError) as exc:
        pe.fetch_holder_trades("600000", REF_DAY)
    assert "符号不一致" in str(exc.value)


def test_pledge_bj_is_empty_without_request(monkeypatch):
    def _boom(*args, **kwargs):
        raise AssertionError("北交所质押不得发请求")

    monkeypatch.setattr(pe, "_datacenter", _boom)
    assert pe.fetch_pledge("920999", REF_DAY) == []


def test_pledge_balance_mismatch_raises(monkeypatch):
    _patch_datacenter(monkeypatch, {"code": 0, "result": {"pages": 1, "data": [{
        "TRADE_DATE": "2026-11-07", "PLEDGE_RATIO": 22.5,
        "REPURCHASE_BALANCE": 1000, "REPURCHASE_UNLIMITED_BALANCE": 400,
        "REPURCHASE_LIMITED_BALANCE": 400,
    }]}})
    with pytest.raises(pe.EventsFetchError) as exc:
        pe.fetch_pledge("600000", REF_DAY)
    assert "≠" in str(exc.value)


# ===========================================================================
# 5) CLI 接线
# ===========================================================================
def test_error_response_has_events_block():
    import cli
    response = cli.create_error_response(
        "data_source_error", "取数失败", "600000", "stock")
    block = response["evidence"]["events"]
    assert block["available"] is False
    assert block["stale"] is True
    for key in ev.SECTION_KEYS:
        assert set(block[key]) == set(ev.SECTION_BLOCK_KEYS)


def _patch_cli_deps(monkeypatch, events_dir):
    import cli
    import stock_analysis.data_loader.meta as meta
    from stock_analysis.backtest.engine import BacktestResult
    from stock_analysis.data_loader.status import TradingStatus

    df = pd.DataFrame(
        {"open": [1.0] * 300, "high": [1.0] * 300, "low": [1.0] * 300,
         "close": [10.0 + i * 0.01 for i in range(300)], "volume": [1e6] * 300},
        index=pd.bdate_range("2020-01-01", periods=300),
    )
    monkeypatch.setattr(meta, "resolve_and_fetch", lambda *a, **k: (df, "stock", None))
    monkeypatch.setattr(cli, "last_fetch_info", lambda: {
        "source": "cache", "from_cache": True, "degraded": False,
        "cache_last_date": REF_DAY, "lag_days": 0,
        "expected_date": REF_DAY, "requested_source": "cache",
        "source_attempts": [],
    })
    monkeypatch.setattr(cli, "get_stock_name", lambda c: "示例股份")
    monkeypatch.setattr(cli, "resolve_trading_status", lambda *a, **k: TradingStatus(
        is_st=False, is_suspended=False, status_source="offline", warnings=[]))

    names = list(cli.build_strategy_signals().keys())[:3]
    comparison = pd.DataFrame(
        [{"评分": 1.0 - i * 0.1, "评分IS": 1.0 - i * 0.1, "评分OOS": 0.9 - i * 0.1,
          "年化收益": "10.00%", "最大回撤": "5.00%", "胜率": "60.00%", "交易次数": 20}
         for i, _ in enumerate(names)],
        index=names,
    )
    result = BacktestResult(trade_count=20, win_rate=0.6, annual_return=0.1,
                            max_drawdown=0.05, unaffordable_lot_events=0)
    result.equity_curve = [100000.0] * 50
    monkeypatch.setattr(cli, "run_strategy_tournament",
                        lambda *a, **k: (names[0], result, comparison))
    # 事件快照日钉在 REF_DAY，避免测试机日期漂移改变过期判定。
    monkeypatch.setattr(ev, "_expected_trade_date",
                        lambda now=None: (_dt.date(2026, 11, 13), "estimate"))
    return cli


def test_cli_success_adds_events_block(monkeypatch, tmp_path):
    _write_cache(str(tmp_path), _cache_payload({"600000": _sections()}))
    monkeypatch.setenv("EVENTS_STATE_DIR", str(tmp_path))
    cli = _patch_cli_deps(monkeypatch, str(tmp_path))
    out = cli.advise_internal("600000", fast=True, no_regime=True)

    block = out["evidence"]["events"]
    assert set(block) >= set(ev.EVENTS_KEYS)
    assert block["available"] is True
    assert block["lockup"]["items"][0]["date"] == "2026-11-18"
    # additive：既有键一个都不能少
    for key in ("flow", "valuation", "market_watch", "triggers"):
        assert key in out["evidence"]
    # 渲染层能直接看到事件行
    assert "⚠️ 解禁：2026-11-18" in render_advice(out)


def test_cli_missing_events_cache_is_stale_not_empty(monkeypatch, tmp_path):
    monkeypatch.setenv("EVENTS_STATE_DIR", str(tmp_path))
    cli = _patch_cli_deps(monkeypatch, str(tmp_path))
    out = cli.advise_internal("600000", fast=True, no_regime=True)
    block = out["evidence"]["events"]
    assert block["available"] is False
    assert block["stale"] is True
    assert EVENT_STALE_MARK in render_advice(out)
    assert any("事件风险上下文不可用" in warning
               for warning in out["diagnostics"]["warnings"])