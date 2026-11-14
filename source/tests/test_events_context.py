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
    """缓存缺失必须显式「未生成」：既不是「无事件」，也不写成「过期（未知）」。"""
    block, notes = ev.events_evidence("600000", "stock",
                                      events_dir=str(tmp_path), now=NOW)
    assert block["available"] is False
    assert block["stale"] is True
    assert block["reason"] == "cache_missing"
    for key in ev.SECTION_KEYS:
        assert block[key]["available"] is False
        assert block[key]["stale"] is True
    assert any("不可用" in note for note in notes)
    output = render_advice(_payload(block))
    assert "⚠️ 事件数据未生成" in output
    assert "预计算未运行" in output
    assert "（未知）" not in output


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


def test_stale_by_fetch_date(monkeypatch, tmp_path):
    # 官方日历在 conftest 里被默认置空（避免本机缓存影响测试）；这里显式
    # 注入「上一交易日 = 2026-11-13」，只测日期过期判定本身。
    monkeypatch.setattr(ev, "_expected_trade_date",
                        lambda now=None: (_dt.date(2026, 11, 13), "calendar_cache"))
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
    for reason in ("fund_not_supported", "replay_not_supported"):
        block = _events_block(
            available=False, stale=False, reason=reason,
            lockup=_section([], available=False, reason=reason),
            holder_trades=_section([], available=False, reason=reason),
            forecast=_section([], available=False, reason=reason),
            pledge=_section([], available=False, reason=reason),
        )
        output = render_advice(_payload(block))
        assert EVENT_STALE_MARK not in output, reason


def test_render_not_supported_is_coverage_not_no_event():
    """P3-1：北交所质押必须显示「未覆盖」，不能静默成「无事件」。"""
    block = _events_block(
        available=True, stale=False,
        pledge=_section([], available=False, stale=False,
                        reason="not_supported"),
    )
    output = render_advice(_payload(block))
    assert "ℹ️ 股权质押：未覆盖" in output
    assert EVENT_STALE_MARK not in output
    assert "无事件" not in output


def test_render_interface_suspected_is_not_no_event():
    """P2-2：探针失败必须显式「接口疑似异常」，不得当成「无事件」。"""
    block = _events_block(
        available=False, stale=True, reason="interface_suspected",
        lockup=_section([], available=False, stale=True,
                        reason="interface_suspected"),
        holder_trades=_section([], available=False, stale=True,
                               reason="interface_suspected"),
        forecast=_section([], available=False, stale=True,
                          reason="interface_suspected"),
        pledge=_section([], available=False, stale=True,
                        reason="interface_suspected"),
    )
    output = render_advice(_payload(block))
    assert "接口疑似异常" in output
    assert "解禁" in output and "股权质押" in output


def test_render_empty_pool_suspected_is_not_no_event():
    block = _events_block(
        available=True, stale=True,
        lockup=_section([], stale=True, reason="empty_pool_suspected"),
    )
    output = render_advice(_payload(block))
    assert "接口疑似异常，本轮空结果不采信" in output
    assert "解禁" in output


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


def _fake_probes(empty=(), fail=()):
    """P2-2 对照探针桩：默认每个报表返回 1 行（接口正常）。"""
    def make(key):
        def probe(as_of):
            if key in fail:
                raise RuntimeError("probe boom")
            if key in empty:
                return []
            return [{"SECURITY_CODE": "999999", "NOTICE_DATE": as_of}]
        return probe

    return {key: make(key) for key in ("lockup", "holder_trades",
                                       "forecast", "pledge")}


def _empty_fetchers():
    return [(key, lambda code, as_of: []) for key in
            ("lockup", "holder_trades", "forecast", "pledge")]


def test_precompute_one_request_per_endpoint_per_code(tmp_path):
    calls = []
    stats = pe.precompute(["600000", "600001", "600002"], out_dir=str(tmp_path),
                          fetchers=_fake_fetchers(calls),
                          probes=_fake_probes(),
                          sleep_fn=lambda _s: None, now_fn=_Clock().now)
    assert stats["probe_requests"] == 4
    assert stats["requests"] == 12 + 4
    assert stats["ok_sections"] == 12
    assert stats["failed_sections"] == 0
    assert [key for key, _code, _day in calls] == [
        "lockup", "holder_trades", "forecast", "pledge"] * 3
    payload = json.loads(Path(str(tmp_path), "events.json").read_text(encoding="utf-8"))
    assert payload["schema"] == ev.EVENTS_SCHEMA
    for code in ("600000", "600001", "600002"):
        assert set(payload[code]) == set(ev.SECTION_KEYS)


def test_precompute_10_codes_serial_estimate(tmp_path):
    """10 只 × 4 端点 + 4 探针 = 44 请求；串行下限 (44-1)×1s = 43s。"""
    calls = []
    clock = _Clock()
    stats = pe.precompute([f"60000{i}" for i in range(10)],
                          out_dir=str(tmp_path),
                          fetchers=_fake_fetchers(calls),
                          probes=_fake_probes(),
                          sleep_fn=clock.sleep, now_fn=clock.now)
    assert stats["requests"] == 44
    assert stats["probe_requests"] == 4
    assert len(calls) == 40
    assert clock.seconds == pytest.approx(43.0)
    assert stats["wall_seconds"] == pytest.approx(43.0)

    estimate = pe.estimate(10)
    assert estimate["requests"] == 44
    assert estimate["probe_requests"] == 4
    assert estimate["min_serial_seconds"] == 43.0
    assert estimate["estimated_seconds"] == 44.0
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
                  probes=_fake_probes(),
                  sleep_fn=lambda _s: None, now_fn=_Clock().now)
    assert max_active[0] == 1


def test_precompute_failure_is_isolated(tmp_path):
    calls = []
    stats = pe.precompute(["600000", "600001"], out_dir=str(tmp_path),
                          retries=0,
                          fetchers=_fake_fetchers(calls, fail={"lockup"}),
                          probes=_fake_probes(),
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
                  probes=_fake_probes(),
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
                          fetchers=fetchers, probes=_fake_probes(),
                          sleep_fn=lambda _s: None, now_fn=_Clock().now)
    assert attempts["n"] == 2
    assert stats["failed_sections"] == 0
    assert stats["requests"] == 4 + 1 + 2 + 2


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
        "SECURITY_CODE": "600000",
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


def test_pledge_bj_is_not_supported_without_request(monkeypatch):
    """P3-1：北交所不是「无事件」，而是 ``not_supported``（渲染为未覆盖）。"""
    def _boom(*args, **kwargs):
        raise AssertionError("北交所质押不得发请求")

    monkeypatch.setattr(pe, "_datacenter", _boom)
    result = pe.fetch_pledge("920999", REF_DAY)
    assert list(result) == []
    assert result.reason == "not_supported"


def test_pledge_balance_mismatch_raises(monkeypatch):
    _patch_datacenter(monkeypatch, {"code": 0, "result": {"pages": 1, "data": [{
        "SECURITY_CODE": "600000",
        "TRADE_DATE": "2026-11-07", "PLEDGE_RATIO": 22.5,
        "REPURCHASE_BALANCE": 1000, "REPURCHASE_UNLIMITED_BALANCE": 400,
        "REPURCHASE_LIMITED_BALANCE": 400,
    }]}})
    with pytest.raises(pe.EventsFetchError) as exc:
        pe.fetch_pledge("600000", REF_DAY)
    assert "≠" in str(exc.value)


# ===========================================================================
# 4b) P2-1 行级归属校验：他票 / 窗口外 / 重复行 → 丢弃 + warning
# ===========================================================================
def _lockup_row(code, day, shares=1200.0, ratio=0.015):
    return {"SECURITY_CODE": code, "FREE_DATE": day + " 00:00:00",
            "FREE_SHARES_TYPE": "首发原股东限售股份",
            "FREE_SHARES": shares * 100, "ABLE_FREE_SHARES": shares,
            "FREE_RATIO": ratio}


def test_lockup_drops_foreign_out_of_window_and_duplicate_rows(monkeypatch):
    _patch_datacenter(monkeypatch, {"code": 0, "result": {"pages": 1, "data": [
        _lockup_row("600000", "2026-12-01"),
        _lockup_row("000001", "2026-12-01"),       # 他票
        _lockup_row("600000", "2020-01-01"),       # 窗口外（早于 -365 天）
        _lockup_row("600000", "2026-12-01"),       # 与第 1 行完全重复
    ]}})
    result = pe.fetch_lockup("600000", REF_DAY)
    assert len(result) == 1
    assert result[0]["date"] == "2026-12-01"
    text = " ".join(result.warnings)
    assert "丢弃 3 行" in text
    assert "他票行 1" in text
    assert "窗口外行 1" in text
    assert "重复行 1" in text


def test_holder_trades_drops_foreign_out_of_window_and_duplicate_rows(monkeypatch):
    def row(code, notice, holder="示例股东"):
        return {"SECURITY_CODE": code, "NOTICE_DATE": notice,
                "HOLDER_NAME": holder, "DIRECTION": "减持",
                "CHANGE_NUM_SYMBOL": -100.0, "AFTER_CHANGE_RATE": 0.2,
                "CHANGE_FREE_RATIO": 0.3, "TRADE_AVERAGE_PRICE": 10.0,
                "MARKET": "二级市场", "START_DATE": "2026-11-05",
                "END_DATE": "2026-11-09"}

    _patch_datacenter(monkeypatch, {"code": 0, "result": {"pages": 1, "data": [
        row("600519", "2026-11-10"),
        row("000001", "2026-11-10"),               # 他票
        row("600519", "2026-01-01"),               # 窗口外（早于 -60 天）
        row("600519", "2026-11-10"),               # 重复
    ]}})
    result = pe.fetch_holder_trades("600519", REF_DAY)
    assert len(result) == 1
    text = " ".join(result.warnings)
    assert "丢弃 3 行" in text and "他票行 1" in text
    assert "窗口外行 1" in text and "重复行 1" in text


def _holder_row(code="600519", notice="2026-11-10", holder="示例股东",
                direction="减持", shares=-100.0, start="2026-11-05",
                end="2026-11-09"):
    return {"SECURITY_CODE": code, "NOTICE_DATE": notice,
            "HOLDER_NAME": holder, "DIRECTION": direction,
            "CHANGE_NUM_SYMBOL": shares, "AFTER_CHANGE_RATE": 0.2,
            "CHANGE_FREE_RATIO": 0.3, "TRADE_AVERAGE_PRICE": 10.0,
            "MARKET": "二级市场", "START_DATE": start, "END_DATE": end}


@pytest.mark.parametrize("start,end,needle", [
    (None, "2026-11-09", "START_DATE 1"),
    ("2026-11-05", None, "END_DATE 1"),
    (None, None, "START_DATE 1、END_DATE 1"),
])
def test_holder_trades_keeps_rows_with_missing_optional_dates(
        monkeypatch, start, end, needle):
    """P2：``START_DATE=None`` 是合法行，不得让整只票的增减持子项失败。"""
    _patch_datacenter(monkeypatch, {"code": 0, "result": {"pages": 1, "data": [
        _holder_row(holder="空日期股东", start=start, end=end),
        _holder_row(holder="完整日期股东"),
    ]}})
    result = pe.fetch_holder_trades("600519", REF_DAY)
    assert {item["holder"] for item in result} == {"空日期股东", "完整日期股东"}
    null_row = next(item for item in result if item["holder"] == "空日期股东")
    if start is None:
        assert null_row["start_date"] is None
    if end is None:
        assert null_row["end_date"] is None
    text = " ".join(result.warnings)
    assert "可选日期缺失" in text and needle in text
    assert "保留该行并置 null" in text


def test_holder_trades_missing_notice_date_drops_only_that_row(monkeypatch):
    """窗口判定所必需的关键日期缺失 → 只丢该行，好行照常入库。"""
    _patch_datacenter(monkeypatch, {"code": 0, "result": {"pages": 1, "data": [
        _holder_row(notice=None),
        _holder_row(notice="2026-11-11", holder="好行股东"),
    ]}})
    result = pe.fetch_holder_trades("600519", REF_DAY)
    assert [item["holder"] for item in result] == ["好行股东"]
    text = " ".join(result.warnings)
    assert "丢弃 1 行" in text and "日期不可解析 1" in text


def test_forecast_keeps_row_with_missing_report_date(monkeypatch):
    """其他可选日期（REPORT_DATE）同规则：置 null、保留行、计数告警。"""
    _patch_datacenter(monkeypatch, {"code": 0, "result": {"pages": 1, "data": [{
        "SECURITY_CODE": "600519", "NOTICE_DATE": "2026-11-10",
        "REPORT_DATE": None, "PREDICT_FINANCE": "归属于上市公司股东的净利润",
        "PREDICT_TYPE": "预减", "PREDICT_AMT_LOWER": -1e8,
        "PREDICT_AMT_UPPER": -0.8e8, "ADD_AMP_LOWER": -50, "ADD_AMP_UPPER": -30,
    }]}})
    result = pe.fetch_forecast("600519", REF_DAY)
    assert len(result) == 1 and result[0]["report_date"] is None
    assert "可选日期缺失 1 处（REPORT_DATE 1）" in " ".join(result.warnings)


def test_forecast_drops_foreign_rows_and_warns_unknown_type(monkeypatch):
    def row(code, notice, ptype):
        return {"SECURITY_CODE": code, "NOTICE_DATE": notice,
                "REPORT_DATE": "2026-09-30",
                "PREDICT_FINANCE": "归属于上市公司股东的净利润",
                "PREDICT_TYPE": ptype, "PREDICT_AMT_LOWER": -1e8,
                "PREDICT_AMT_UPPER": -0.8e8, "ADD_AMP_LOWER": -50,
                "ADD_AMP_UPPER": -30}

    _patch_datacenter(monkeypatch, {"code": 0, "result": {"pages": 1, "data": [
        row("600519", "2026-11-10", "预减"),
        row("000001", "2026-11-10", "预减"),       # 他票
        row("600519", "2026-11-11", "预亏"),       # P3-4 未知类型
    ]}})
    result = pe.fetch_forecast("600519", REF_DAY)
    assert {item["forecast_type"] for item in result} == {"预减", "预亏"}
    text = " ".join(result.warnings)
    assert "他票行 1" in text
    assert "未知 PREDICT_TYPE：预亏" in text


def test_pledge_drops_foreign_row(monkeypatch):
    _patch_datacenter(monkeypatch, {"code": 0, "result": {"pages": 3, "data": [{
        "SECURITY_CODE": "000001", "TRADE_DATE": "2026-11-13 00:00:00",
        "PLEDGE_RATIO": 22.5, "REPURCHASE_BALANCE": 1000,
        "REPURCHASE_UNLIMITED_BALANCE": 400,
        "REPURCHASE_LIMITED_BALANCE": 600,
    }]}})
    result = pe.fetch_pledge("600000", REF_DAY)
    assert list(result) == []
    assert "他票行 1" in " ".join(result.warnings)


def test_precompute_keeps_bad_rows_out_of_cache_and_warns(monkeypatch, tmp_path):
    """端到端：坏行不入库，warning 落盘并可被 CLI 读到。"""
    def fake_datacenter(report, filter_str="", **kwargs):
        if report == "RPT_LIFT_STAGE":
            return [_lockup_row("600519", "2026-12-01"),
                    _lockup_row("000001", "2026-12-01"),
                    _lockup_row("600519", "2020-01-01"),
                    _lockup_row("600519", "2026-12-01")]
        return []

    monkeypatch.setattr(pe, "_datacenter", fake_datacenter)
    stats = pe.precompute(["600519"], out_dir=str(tmp_path),
                          fetchers=pe.FETCHERS, probes=_fake_probes(),
                          sleep_fn=lambda _s: None, now_fn=_Clock().now)
    assert stats["failed_sections"] == 0
    payload = json.loads(Path(str(tmp_path), "events.json").read_text(encoding="utf-8"))
    lockup = payload["600519"]["lockup"]
    assert len(lockup["items"]) == 1
    assert any("丢弃 3 行" in warning for warning in lockup["warnings"])
    block, notes = ev.events_evidence("600519", "stock",
                                      events_dir=str(tmp_path), now=NOW)
    assert any("丢弃 3 行" in note for note in notes)
    assert len(block["lockup"]["items"]) == 1


def test_precompute_keeps_row_with_missing_optional_date(monkeypatch, tmp_path):
    """端到端：``START_DATE=None`` 的行保留，holder_trades 子项整体不失败。"""
    def fake_datacenter(report, filter_str="", **kwargs):
        if report == "RPT_SHARE_HOLDER_INCREASE":
            return [_holder_row(holder="空日期股东", start=None),
                    _holder_row(holder="完整日期股东")]
        return []

    monkeypatch.setattr(pe, "_datacenter", fake_datacenter)
    stats = pe.precompute(["600519"], out_dir=str(tmp_path),
                          fetchers=pe.FETCHERS, probes=_fake_probes(),
                          sleep_fn=lambda _s: None, now_fn=_Clock().now)
    assert stats["failed_sections"] == 0
    payload = json.loads(Path(str(tmp_path), "events.json").read_text(encoding="utf-8"))
    section = payload["600519"]["holder_trades"]
    assert section["available"] is True
    assert {item["holder"] for item in section["items"]} == {
        "空日期股东", "完整日期股东"}
    assert any("可选日期缺失" in warning for warning in section["warnings"])


# ===========================================================================
# 4c) P2-2 对照探针 / 零成本兜底
# ===========================================================================
def test_probe_zero_rows_marks_all_codes_interface_suspected(tmp_path):
    calls = []
    stats = pe.precompute(["600000", "600001"], out_dir=str(tmp_path),
                          retries=0,
                          fetchers=_fake_fetchers(calls),
                          probes=_fake_probes(empty={"lockup"}),
                          sleep_fn=lambda _s: None, now_fn=_Clock().now)
    assert stats["interface_suspected"] == ["lockup"]
    assert stats["probes"]["lockup"]["ok"] is False
    assert stats["probes"]["lockup"]["rows"] == 0
    assert all(key != "lockup" for key, _code, _day in calls)  # 跳过逐票请求
    payload = json.loads(Path(str(tmp_path), "events.json").read_text(encoding="utf-8"))
    for code in ("600000", "600001"):
        lockup = payload[code]["lockup"]
        assert lockup["available"] is False
        assert lockup["stale"] is True
        assert lockup["reason"] == "interface_suspected"
        assert lockup["items"] == []
        assert payload[code]["holder_trades"]["available"] is True
    block, notes = ev.events_evidence("600000", "stock",
                                      events_dir=str(tmp_path), now=NOW)
    assert block["lockup"]["reason"] == "interface_suspected"
    assert any("接口疑似异常" in note for note in notes)
    assert "接口疑似异常" in render_advice(_payload(block))


def test_probe_error_marks_interface_suspected(tmp_path):
    stats = pe.precompute(["600000"], out_dir=str(tmp_path), retries=0,
                          fetchers=_fake_fetchers(),
                          probes=_fake_probes(fail={"forecast"}),
                          sleep_fn=lambda _s: None, now_fn=_Clock().now)
    assert stats["interface_suspected"] == ["forecast"]
    payload = json.loads(Path(str(tmp_path), "events.json").read_text(encoding="utf-8"))
    forecast = payload["600000"]["forecast"]
    assert forecast["reason"] == "interface_suspected"
    assert "probe boom" in (forecast["error"] or "")


def test_probe_ok_and_empty_pool_is_genuine_empty(tmp_path):
    stats = pe.precompute(["600000", "600001"], out_dir=str(tmp_path),
                          fetchers=_empty_fetchers(), probes=_fake_probes(),
                          sleep_fn=lambda _s: None, now_fn=_Clock().now)
    assert stats["interface_suspected"] == []
    assert stats["failed_sections"] == 0
    payload = json.loads(Path(str(tmp_path), "events.json").read_text(encoding="utf-8"))
    for code in ("600000", "600001"):
        for key in ev.SECTION_KEYS:
            section = payload[code][key]
            assert section["available"] is True
            assert section["stale"] is False
            assert section["reason"] is None
            assert section["items"] == []


def test_all_pool_empty_after_prior_data_is_suspected(tmp_path):
    _write_cache(str(tmp_path), _cache_payload({"600000": _sections()}))
    stats = pe.precompute(["600000"], out_dir=str(tmp_path),
                          fetchers=_empty_fetchers(), probes=_fake_probes(),
                          sleep_fn=lambda _s: None, now_fn=_Clock().now)
    payload = json.loads(Path(str(tmp_path), "events.json").read_text(encoding="utf-8"))
    for key in ev.SECTION_KEYS:
        section = payload["600000"][key]
        assert section["reason"] == "empty_pool_suspected"
        assert section["stale"] is True
        assert section["items"] == []
        assert any("上一轮有数据" in warning for warning in section["warnings"])
    assert any("empty_pool_suspected" in warning for warning in stats["warnings"])


def test_not_supported_bj_pledge_reaches_renderer(monkeypatch, tmp_path):
    monkeypatch.setattr(pe, "_datacenter", lambda *a, **k: [])
    stats = pe.precompute(["920999"], out_dir=str(tmp_path),
                          fetchers=pe.FETCHERS, probes=_fake_probes(),
                          sleep_fn=lambda _s: None, now_fn=_Clock().now)
    assert stats["not_supported_sections"] == 1
    assert stats["failed_sections"] == 0
    payload = json.loads(Path(str(tmp_path), "events.json").read_text(encoding="utf-8"))
    pledge = payload["920999"]["pledge"]
    assert pledge["reason"] == "not_supported"
    assert pledge["available"] is False and pledge["stale"] is False
    block, _notes = ev.events_evidence("920999", "stock",
                                       events_dir=str(tmp_path), now=NOW)
    output = render_advice(_payload(block))
    assert "ℹ️ 股权质押：未覆盖" in output
    assert EVENT_STALE_MARK not in output


# ===========================================================================
# 4d) P3-3 长假后首日：过期判定用官方日历
# ===========================================================================
def _september_calendar():
    return {"2026-09-30": True}


def test_long_holiday_first_day_not_false_stale(monkeypatch, tmp_path):
    """10-08 盘前：10 月日历缺失时，官方 9 月日历说明上一交易日是 9-30。"""
    from stock_analysis.data_sources import astock

    def fake(year, month):
        return _september_calendar() if (year, month) == (2026, 9) else None

    monkeypatch.setattr(astock, "calendar_days_from_disk", fake)
    sections = _sections()
    for section in sections.values():
        section["as_of"] = "2026-09-30"
        section["fetched_at"] = "2026-09-30T18:35:00"
    _write_cache(str(tmp_path), _cache_payload(
        {"600000": sections}, fetched_at="2026-09-30T18:35:00"))
    now = _dt.datetime(2026, 10, 8, 9, 0)
    block, _notes = ev.events_evidence("600000", "stock",
                                       events_dir=str(tmp_path), now=now)
    assert block["stale"] is False
    assert EVENT_STALE_MARK not in render_advice(_payload(block))


def test_official_calendar_still_flags_real_stale_after_holiday(monkeypatch, tmp_path):
    """10-09 盘前（10-08 已收盘）：缓存仍是 9-30 → 确实过期。"""
    from stock_analysis.data_sources import astock

    def fake(year, month):
        if (year, month) == (2026, 9):
            return _september_calendar()
        if (year, month) == (2026, 10):
            return {"2026-10-08": True, "2026-10-07": False}
        return None

    monkeypatch.setattr(astock, "calendar_days_from_disk", fake)
    sections = _sections()
    for section in sections.values():
        section["as_of"] = "2026-09-30"
        section["fetched_at"] = "2026-09-30T18:35:00"
    _write_cache(str(tmp_path), _cache_payload(
        {"600000": sections}, fetched_at="2026-09-30T18:35:00"))
    now = _dt.datetime(2026, 10, 9, 9, 0)
    block, notes = ev.events_evidence("600000", "stock",
                                      events_dir=str(tmp_path), now=now)
    assert block["stale"] is True
    assert any("过期" in note for note in notes)


def test_calendar_unavailable_skips_date_staleness(monkeypatch, tmp_path):
    from stock_analysis.data_sources import astock

    monkeypatch.setattr(astock, "calendar_days_from_disk", lambda y, m: None)
    sections = _sections()
    for section in sections.values():
        section["as_of"] = "2020-01-02"
        section["fetched_at"] = "2020-01-02T18:35:00"
    _write_cache(str(tmp_path), _cache_payload(
        {"600000": sections}, fetched_at="2020-01-02T18:35:00"))
    block, _notes = ev.events_evidence("600000", "stock",
                                       events_dir=str(tmp_path), now=NOW)
    # 官方日历不可用 → 不做日期判定；不误报过期（显式 stale/error 仍生效）。
    assert block["stale"] is False


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
    assert "⚠️ 事件数据未生成" in render_advice(out)
    assert any("事件风险上下文不可用" in warning
               for warning in out["diagnostics"]["warnings"])