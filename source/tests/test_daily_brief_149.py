# -*- coding: utf-8 -*-
"""盘前简报 v2（issue #149 / #150 复验）的离线测试。

覆盖：
* 五形态快照：买入 / 继续持有 / 暂不买 / 卖出 / 不参与；
* 三种数据状态快照：正常 / 部分标的取数失败 / 全部失败（含空输入）；
* 结论行计数：0 条 →「今天不用动」，N 条 →「今天有 N 件事要做」；
* 全部失败不得出现「不用动」类结论；CLI 退出码 4；
* 低把握只有一种口径（`模型把握低（x%）`），不再写「没出现买点」；
* 超期持仓：条目补全「建议持有 N 个交易日，已超 M 日」，结论带「有 N 只
  已超建议持有期」；
* 买入条带「入场区 / 止损」，卖出条保持止损 / 止盈；
* 术语黑名单扫描 0 命中（名称属于白名单），并覆盖新增的
  `信号族 / 显著 / 胜率 / 相关性`；
* 空仓/持仓提示互斥：空仓无卖出提示、持仓无买入提示；矛盾 payload 被忽略
  并在数据行说明；
* 价格 2 位小数、百分比最多 1 位；
* 纯函数（不修改入参）、零 None、休市日不逐只列；
* ``tools/render_daily_brief.py`` 的文件/stdin 包装、错误退出码与术语自检。

测试标的只用 600519 / 600000（不使用任何真实持仓代码）。
"""
from __future__ import annotations

import copy
import json
import re
import subprocess
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parents[1]
ROOT = SRC.parent
TOOL = ROOT / "tools" / "render_daily_brief.py"

if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from stock_analysis.daily_brief import (  # noqa: E402
    CONFIDENCE_EXPLAIN,
    DATA_STATUS_NO_DATA,
    DATA_STATUS_OK,
    DATA_STATUS_PARTIAL,
    data_status,
    find_banned_terms,
    render_daily_brief,
)

DATE = "2026-11-18"
HEADER = "📈 盘前 11/18"

NAMES = {"600519": "示例白酒", "600000": "示例银行"}


def _payload(code: str, name: str, *, action: str, position: str,
             confidence=0.72, price=12.34, status="ok",
             suspension=False, **rec_fields) -> dict:
    rec = {
        "status": status,
        "action": action,
        "signal": {"position_state": position, "as_of": "2026-11-18"},
        "confidence": confidence,
        "position_fraction": 0.15,
        "entry_zone": [11.8, 12.2],
        "stop_loss": 11.0,
        "take_profit": 13.5,
        "horizon_days": [20, 20],
        "holding_days": None,
        "action_reason": [],
    }
    rec.update(rec_fields)
    return {
        "schema_version": "1.0",
        "request": {"code": code, "type": "stock"},
        "instrument": {
            "code": code,
            "name": name,
            "is_suspended": suspension,
            "is_st": False,
            "one_word_limit": None,
            "market_closed": False,
        },
        "data": {
            "last_close": price,
            "end": "2026-11-17",
            "expected_date": "2026-11-17",
            "stale": False,
            "lag_days": 0,
        },
        "recommendation": rec,
        "diagnostics": {"warnings": []},
    }


def _buy() -> dict:
    return _payload("600519", NAMES["600519"], action="buy", position="flat",
                    confidence=0.82, price=1820.5)


def _holding() -> dict:
    return _payload("600519", NAMES["600519"], action="hold",
                    position="holding", confidence=0.63, price=1701.2,
                    holding_days=22)


def _flat() -> dict:
    return _payload("600000", NAMES["600000"], action="hold", position="flat",
                    confidence=0.55, price=12.3)


def _flat_low_confidence() -> dict:
    return _payload("600000", NAMES["600000"], action="hold", position="flat",
                    confidence=0.021, price=12.3)


def _sell() -> dict:
    return _payload(
        "600519", NAMES["600519"], action="sell", position="holding",
        confidence=0.21, price=33.1, stop_loss=33.17,
        action_reason=["今日（2026-11-18）触发止损，目标仓位 0%"])


def _avoid() -> dict:
    return _payload("600000", NAMES["600000"], action="avoid", position="flat",
                    confidence=None, price=8.8, status="untradeable",
                    suspension=True)


def _error_payload(code: str, name: str = "") -> dict:
    """与 ``create_error_response(status='data_source_error')`` 同构。"""
    message = "行情拉取失败（渠道池全失败且无可用缓存）"
    payload = _payload(code, name, action="insufficient_data",
                       position="flat", confidence=0.0, price=None,
                       status="data_source_error")
    payload["data"].update({"bars": 0, "last_close": None})
    payload["recommendation"]["action_reason"] = [message]
    payload["diagnostics"]["warnings"] = [message]
    return payload


def _insufficient_payload(code: str, name: str = "") -> dict:
    """取到了数据但根数不够（``status='insufficient_data'``）。"""
    message = "数据不足（仅 50 根 < 100）"
    payload = _payload(code, name, action="insufficient_data",
                       position="flat", confidence=None, price=12.3,
                       status="insufficient_data")
    payload["data"].update({"bars": 50})
    payload["recommendation"]["action_reason"] = [message]
    payload["diagnostics"]["warnings"] = [message]
    return payload


def _render(*payloads) -> str:
    return render_daily_brief(list(payloads), date=DATE)


# ---------------------------------------------------------------------------
# 五形态快照
# ---------------------------------------------------------------------------
def test_snapshot_buy():
    expected = (
        HEADER + "\n"
        "\n"
        "结论：今天有 1 件事要做。\n"
        + CONFIDENCE_EXPLAIN + "\n"
        "- 示例白酒 1820.50：买入，今天出现了买点，入场区 11.80–12.20，"
        "止损 11.00，仓位约 15%（把握 高，82%）。"
    )
    assert _render(_buy()) == expected


def test_snapshot_holding():
    expected = (
        HEADER + "\n"
        "\n"
        "结论：今天不用动；有 1 只已超建议持有期。\n"
        "- 示例白酒 1701.20：继续持有，第 22 个交易日"
        "（建议持有 20 个交易日，已超 2 日"
        "（未跌破止损，不强制卖））。"
    )
    assert _render(_holding()) == expected


def test_snapshot_flat():
    expected = (
        HEADER + "\n"
        "\n"
        "结论：今天不用动。\n"
        "- 示例银行 12.30：不买，今天没出现买点。"
    )
    assert _render(_flat()) == expected


def test_flat_low_grade_shows_confidence_not_certainty():
    # 43.6% 是「低」：必须让读者看见不确定，不能写成确定的「今天没出现买点」。
    payload = _payload("600000", NAMES["600000"], action="hold",
                       position="flat", confidence=0.436, price=21.95)
    text = _render(payload)
    assert "- 示例银行 21.95：不买，模型把握低（43.6%）。" in text
    assert "没出现买点" not in text


def test_low_grade_has_a_single_phrasing():
    # 2.1% 与 43.6% 同属「低」：同一动作只允许一种口径。
    for confidence in (0.021, 0.347, 0.436):
        text = _render(_payload("600000", NAMES["600000"], action="hold",
                                position="flat", confidence=confidence,
                                price=12.3))
        assert "不买，模型把握低（" in text
        assert "没出现买点" not in text


def test_flat_medium_grade_keeps_certain_phrasing():
    text = _render(_flat())  # 0.55 = 中
    assert "不买，今天没出现买点。" in text


def test_flat_without_confidence_never_claims_no_setup():
    payload = _payload("600000", NAMES["600000"], action="hold",
                       position="flat", confidence=None, price=12.3)
    text = _render(payload)
    assert "不买，模型没给把握。" in text
    assert "没出现买点" not in text


def test_snapshot_flat_low_confidence():
    text = _render(_flat_low_confidence())
    assert "- 示例银行 12.30：不买，模型把握低（2.1%）。" in text
    assert "模型也没把握" not in text


def test_snapshot_sell():
    expected = (
        HEADER + "\n"
        "\n"
        "结论：今天有 1 件事要做。\n"
        "- 示例白酒 33.10：卖出，跌破了止损价 33.17。"
    )
    assert _render(_sell()) == expected


def test_snapshot_avoid():
    expected = (
        HEADER + "\n"
        "\n"
        "结论：今天不用动。\n"
        "- 示例银行 8.80：不参与，停牌中，今天下不了单。"
    )
    assert _render(_avoid()) == expected


def test_snapshot_insufficient_data_as_partial_state():
    # 一只有结论 + 一只数据不够 → 部分失败：结论写明数量，条目只写状态。
    text = _render(_flat(), _insufficient_payload("600519", NAMES["600519"]))
    assert "结论：今天不用动；另有 1 只数据不够。" in text
    assert "- 示例白酒：数据不够，今天不给判断。" in text
    line = next(item for item in _item_lines(text) if "数据不够" in item)
    for word in ("买", "卖", "参与", "观望"):
        assert word not in line


# ---------------------------------------------------------------------------
# 数据三态（issue #150 复验 P1）
# ---------------------------------------------------------------------------
def test_data_status_three_states():
    assert data_status([]) == DATA_STATUS_NO_DATA
    assert data_status([_flat()]) == DATA_STATUS_OK
    assert data_status([_flat(), _error_payload("600519", NAMES["600519"])]) \
        == DATA_STATUS_PARTIAL
    assert data_status([_error_payload("600000")]) == DATA_STATUS_NO_DATA
    assert data_status([_insufficient_payload("600000")]) == DATA_STATUS_NO_DATA


def test_snapshot_all_data_failed_never_says_no_action():
    text = _render(_error_payload("600519", NAMES["600519"]),
                   _error_payload("600000", NAMES["600000"]))
    assert text == (
        HEADER + "\n\n"
        "结论：今天数据没拿到，无法给出建议。\n"
        "原因：2 只标的都没取到行情数据。")
    assert "不用动" not in text
    assert "数据不够" not in text


def test_all_insufficient_is_no_data():
    text = _render(_insufficient_payload("600519", NAMES["600519"]),
                   _insufficient_payload("600000", NAMES["600000"]))
    assert "结论：今天数据没拿到，无法给出建议。" in text
    assert "原因：2 只标的的行情数据都不够。" in text
    assert "不用动" not in text


def test_mixed_all_failure_reason_mentions_no_usable_data():
    text = _render(_error_payload("600519", NAMES["600519"]),
                   _insufficient_payload("600000", NAMES["600000"]))
    assert "原因：2 只标的都没有可用的行情数据。" in text
    assert "不用动" not in text


def test_partial_data_failure_conclusion_and_item():
    text = _render(_flat(), _error_payload("600519", NAMES["600519"]))
    assert "结论：今天不用动；另有 1 只没取到数据。" in text
    assert "- 示例白酒：今天数据没拿到，没有给出建议。" in text
    line = next(item for item in _item_lines(text) if "数据没拿到" in item)
    for word in ("买", "卖", "参与", "观望"):
        assert word not in line


def test_partial_failure_with_action_still_counts_action():
    text = _render(_buy(), _error_payload("600000", NAMES["600000"]))
    assert "结论：今天有 1 件事要做；另有 1 只没取到数据。" in text


def test_empty_payload_object_is_failure_not_neutral():
    text = _render({})
    assert "结论：今天数据没拿到，无法给出建议。" in text
    assert "不用动" not in text


# ---------------------------------------------------------------------------
# 结论行计数
# ---------------------------------------------------------------------------
def test_conclusion_zero_actions():
    other_flat = _payload("600519", NAMES["600519"], action="hold",
                          position="flat", confidence=0.55, price=12.3)
    text = _render(_flat(), other_flat)
    assert "结论：今天不用动。" in text
    assert "件事要做" not in text


def test_conclusion_two_actions():
    text = render_daily_brief([_buy(), _sell(), _flat()], date=DATE)
    assert "结论：今天有 2 件事要做。" in text


def test_actions_listed_before_informational_items():
    text = render_daily_brief([_flat(), _buy()], date=DATE)
    assert text.index("示例白酒") < text.index("示例银行")


# ---------------------------------------------------------------------------
# 超期持仓 / 买入价位（issue #150 复验 P2-2 / P2-3）
# ---------------------------------------------------------------------------
def test_overdue_holding_wording_and_conclusion():
    text = _render(_holding())
    assert "- 示例白酒 1701.20：继续持有，第 22 个交易日" in text
    assert "建议持有 20 个交易日，已超 2 日（未跌破止损，不强制卖）" in text
    assert "建议 20 日" not in text
    assert "结论：今天不用动；有 1 只已超建议持有期。" in text


def test_not_overdue_holding_has_no_overdue_clause():
    payload = _payload("600519", NAMES["600519"], action="hold",
                       position="holding", confidence=0.63, price=1701.2,
                       holding_days=5)
    text = _render(payload)
    assert "（建议持有 20 个交易日）。" in text
    assert "已超" not in text
    assert "结论：今天不用动。" in text


def test_overdue_count_in_conclusion_with_other_actions():
    overdue = _payload("600519", NAMES["600519"], action="hold",
                       position="holding", confidence=0.63, price=1701.2,
                       holding_days=25)
    text = _render(_buy(), overdue)
    assert "结论：今天有 1 件事要做；另有 1 只已超建议持有期。" in text


def test_multiple_overdue_counted():
    first = _payload("600519", NAMES["600519"], action="hold",
                     position="holding", confidence=0.63, price=1701.2,
                     holding_days=22)
    second = _payload("600000", NAMES["600000"], action="hold",
                      position="holding", confidence=0.63, price=12.3,
                      holding_days=30)
    text = _render(first, second)
    assert "结论：今天不用动；有 2 只已超建议持有期。" in text


def test_buy_line_has_entry_zone_and_stop():
    line = _item_lines(_render(_buy()))[0]
    assert "入场区 11.80–12.20" in line
    assert "止损 11.00" in line


def test_buy_line_shows_only_available_levels():
    payload = _buy()
    payload["recommendation"]["entry_zone"] = None
    line = _item_lines(_render(payload))[0]
    assert "入场区" not in line
    assert "止损 11.00" in line

    payload = _buy()
    payload["recommendation"]["stop_loss"] = None
    line = _item_lines(_render(payload))[0]
    assert "入场区 11.80–12.20" in line
    assert "止损" not in line


def test_sell_line_keeps_stop():
    line = _item_lines(_render(_sell()))[0]
    assert "卖出，跌破了止损价 33.17" in line


# ---------------------------------------------------------------------------
# 术语黑名单
# ---------------------------------------------------------------------------
def test_banned_terms_zero_hits_on_all_shapes():
    outputs = [
        _render(_buy()), _render(_holding()), _render(_flat()),
        _render(_flat_low_confidence()), _render(_sell()), _render(_avoid()),
        _render(_flat(), _error_payload("600519", NAMES["600519"])),
        _render(_error_payload("600519", NAMES["600519"]),
                _error_payload("600000", NAMES["600000"])),
        render_daily_brief([], date=DATE),
    ]
    for text in outputs:
        assert find_banned_terms(text, names=NAMES.values()) == [], text


def test_st_payload_renders_plainly_and_scans_clean():
    payload = _payload("600000", NAMES["600000"], action="avoid",
                       position="flat", confidence=None, price=8.8,
                       status="untradeable")
    payload["instrument"]["is_st"] = True
    text = _render(payload)
    assert "风险警示股，风险太高" in text
    assert find_banned_terms(text, names=NAMES.values()) == []


def test_banned_terms_catches_internal_jargon():
    hits = find_banned_terms("策略族'CMF'触发参考价缺失（n=53,t=0.1）")
    assert "策略族" in hits
    assert "CMF" in hits
    assert "触发参考价" in hits
    assert "n=" in hits
    assert "t=" in hits


def test_banned_terms_cover_review_vocabulary():
    hits = find_banned_terms("信号族 显著 胜率 相关性")
    for term in ("信号族", "显著", "胜率", "相关性"):
        assert term in hits


def test_banned_term_scan_whitelists_names():
    assert find_banned_terms("新华策略C：不买", names=["新华策略C"]) == []


def test_no_statistical_jargon_in_reasons():
    text = _render(_sell(), _flat_low_confidence(), _holding())
    for term in ("期望", "超额", "分位", "置信", "样本", "回测", "指标"):
        assert term not in text


# ---------------------------------------------------------------------------
# 空仓 / 持仓提示互斥
# ---------------------------------------------------------------------------
def _item_lines(text: str):
    return [line for line in text.splitlines() if line.startswith("- ")]


def test_flat_has_no_sell_hint():
    lines = _item_lines(_render(_flat()))
    assert lines
    for line in lines:
        assert "卖" not in line
        assert "止损" not in line


def test_holding_has_no_buy_hint():
    lines = _item_lines(_render(_holding()))
    assert lines
    for line in lines:
        assert "买" not in line


def test_flat_sell_contradiction_suppressed():
    payload = _payload("600000", NAMES["600000"], action="sell",
                       position="flat", confidence=0.4, price=12.3,
                       action_reason=["今日（2026-11-18）触发止损，目标仓位 0%"])
    text = _render(payload)
    assert "卖出，" not in text
    assert "数据：" in text
    assert "是空仓，卖出提示已忽略" in text


def test_suspended_holding_never_shows_sell():
    # 持有中触发止损，但今天停牌卖不出：只写「不参与」，不写「卖出」。
    payload = _payload("600519", NAMES["600519"], action="sell",
                       position="holding", confidence=0.4, price=33.1,
                       suspension=True,
                       action_reason=["今日（2026-11-18）触发止损，目标仓位 0%"])
    text = _render(payload)
    assert "卖出，" not in text
    assert "停牌中，今天下不了单" in text


def test_holding_buy_contradiction_suppressed():
    payload = _payload("600519", NAMES["600519"], action="buy",
                       position="holding", confidence=0.8, price=1701.2)
    text = _render(payload)
    assert "买入，" not in text
    assert "已持仓，买入提示已忽略" in text


# ---------------------------------------------------------------------------
# 数值精度 / Data line
# ---------------------------------------------------------------------------
def test_price_two_decimals_percent_at_most_one():
    text = _render(_buy(), _flat_low_confidence())
    assert "1820.50" in text
    assert re.search(r"\d+\.\d{2,}%", text) is None


def test_data_line_only_when_it_affects_conclusion():
    # 资金流口径 / 估值缺失 / 事件过期都不改变动作 → 不出现数据行。
    payload = _flat()
    payload["evidence"] = {
        "flow": {"available": True, "caliber": "sina_total_net"},
        "valuation": {"available": False},
    }
    payload["diagnostics"]["warnings"] = [
        "策略族 'CMF' 未提供可复算的指标值",
        "超额收益点估计为负（p_positive=6.8%）",
    ]
    text = _render(payload)
    assert "数据：" not in text


def test_stale_price_is_reported_once():
    stale = _flat()
    stale["data"].update({"stale": True, "end": "2026-11-10"})
    text = _render(stale)
    data_lines = [line for line in text.splitlines() if line.startswith("数据：")]
    assert len(data_lines) == 1
    assert "示例银行的价格是旧数据（到 2026-11-10）" in data_lines[0]


def test_buy_without_entry_zone_is_reported():
    payload = _buy()
    payload["recommendation"]["entry_zone"] = None
    text = _render(payload)
    assert "没有给出买入价位，只给方向" in text


# ---------------------------------------------------------------------------
# 边界
# ---------------------------------------------------------------------------
def test_market_closed_prints_one_conclusion_no_items():
    payloads = []
    for code in ("600519", "600000"):
        payload = _payload(code, NAMES[code], action="hold", position="flat",
                           confidence=None, price=12.3,
                           status="market_closed")
        payload["instrument"]["market_closed"] = True
        payloads.append(payload)
    text = render_daily_brief(payloads, date=DATE)
    assert text == HEADER + "\n\n结论：今天休市，不用动。"


def test_empty_payload_list():
    text = render_daily_brief([], date=DATE)
    assert text == (
        HEADER + "\n\n"
        "结论：今天数据没拿到，无法给出建议。\n"
        "原因：这次没有收到任何标的的数据。")
    assert "不用动" not in text


def test_no_none_in_output():
    text = _render(_buy(), _holding(), _flat(), _sell(), _avoid())
    assert "None" not in text
    failure_text = _render(_flat(), _error_payload("600519", NAMES["600519"]))
    assert "None" not in failure_text


def test_pure_function_does_not_mutate_payloads():
    payloads = [_buy(), _sell(), _flat(), _holding()]
    snapshot = copy.deepcopy(payloads)
    render_daily_brief(payloads, date=DATE)
    assert payloads == snapshot


# ---------------------------------------------------------------------------
# CLI 包装
# ---------------------------------------------------------------------------
def _run_tool(args, stdin_text=None):
    return subprocess.run(
        [sys.executable, str(TOOL), *args],
        input=stdin_text, capture_output=True, text=True, timeout=60)


def test_tool_reads_json_array_and_applies_names(tmp_path):
    path = tmp_path / "payloads.json"
    payload = _flat()
    path.write_text(json.dumps([payload], ensure_ascii=False), encoding="utf-8")
    result = _run_tool([
        "--json-file", str(path), "--date", DATE, "--name", "600000=示例银行",
        "--check-terms",
    ])
    assert result.returncode == 0, result.stderr
    assert "结论：今天不用动。" in result.stdout
    assert "示例银行 12.30：不买，今天没出现买点。" in result.stdout


def test_tool_reads_single_object_from_stdin():
    result = _run_tool(["--date", DATE], stdin_text=json.dumps(_holding(),
                                                             ensure_ascii=False))
    assert result.returncode == 0, result.stderr
    assert "继续持有，第 22 个交易日" in result.stdout


def test_tool_reads_directory(tmp_path):
    (tmp_path / "a.json").write_text(json.dumps(_flat(), ensure_ascii=False),
                                     encoding="utf-8")
    (tmp_path / "b.json").write_text(json.dumps(_holding(), ensure_ascii=False),
                                     encoding="utf-8")
    result = _run_tool(["--json-dir", str(tmp_path), "--date", DATE])
    assert result.returncode == 0, result.stderr
    assert result.stdout.count("- ") == 2


def test_tool_scan_whitelists_payload_names(tmp_path):
    # 未传 --name 时，payload 自带的名称也必须算白名单（简称可能含「策略」）。
    payload = _flat()
    payload["instrument"]["name"] = "示例策略C"
    path = tmp_path / "named.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    result = _run_tool(["--json-file", str(path), "--date", DATE,
                        "--check-terms"])
    assert result.returncode == 0, result.stderr
    assert "示例策略C 12.30" in result.stdout


def test_tool_rejects_bad_json(tmp_path):
    path = tmp_path / "bad.json"
    path.write_text("{not json", encoding="utf-8")
    result = _run_tool(["--json-file", str(path)])
    assert result.returncode == 2
    assert "JSON 解析失败" in result.stderr


def test_tool_check_terms_clean_exit_zero(tmp_path):
    # 用一个名称不在白名单里的脏 payload 无法经由渲染产生术语（渲染层本身
    # 干净），这里直接验证扫描器与工具契约：工具对干净输出退出 0。
    path = tmp_path / "clean.json"
    path.write_text(json.dumps(_buy(), ensure_ascii=False), encoding="utf-8")
    result = _run_tool(["--json-file", str(path), "--date", DATE,
                        "--check-terms"])
    assert result.returncode == 0, result.stderr
    assert "术语黑名单命中" not in result.stderr


def test_tool_all_data_failure_exits_4(tmp_path):
    path = tmp_path / "errors.json"
    payloads = [_error_payload("600519", NAMES["600519"]),
                _error_payload("600000", NAMES["600000"])]
    path.write_text(json.dumps(payloads, ensure_ascii=False), encoding="utf-8")
    result = _run_tool(["--json-file", str(path), "--date", DATE,
                        "--check-terms"])
    assert result.returncode == 4, result.stderr
    assert "结论：今天数据没拿到，无法给出建议。" in result.stdout
    assert "不用动" not in result.stdout


def test_tool_empty_array_exits_4():
    result = _run_tool(["--date", DATE], stdin_text="[]")
    assert result.returncode == 4, result.stderr
    assert "今天数据没拿到" in result.stdout
    assert "不用动" not in result.stdout


def test_tool_partial_failure_exits_zero_and_discloses(tmp_path):
    path = tmp_path / "partial.json"
    payloads = [_flat(), _error_payload("600519", NAMES["600519"])]
    path.write_text(json.dumps(payloads, ensure_ascii=False), encoding="utf-8")
    result = _run_tool(["--json-file", str(path), "--date", DATE])
    assert result.returncode == 0, result.stderr
    assert "另有 1 只没取到数据" in result.stdout