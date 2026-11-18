# -*- coding: utf-8 -*-
"""盘前简报 v2（issue #149）的离线测试。

覆盖：
* 五形态快照：买入 / 继续持有 / 暂不买 / 卖出 / 不参与；
* 结论行计数：0 条 →「今天不用动」，N 条 →「今天有 N 件事要做」；
* 术语黑名单扫描 0 命中（名称属于白名单）；
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
        "- 示例白酒 1820.50：买入，今天出现了买点，仓位约 15%（把握 高，82%）。"
    )
    assert _render(_buy()) == expected


def test_snapshot_holding():
    expected = (
        HEADER + "\n"
        "\n"
        "结论：今天不用动。\n"
        "- 示例白酒 1701.20：继续持有，第 22 个交易日"
        "（建议 20 日，没跌破止损就不强制卖）。"
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


def test_flat_low_grade_without_near_zero_omits_confidence():
    # 43.6% 是「低」，但模型并非毫无方向：照 issue #149 示例只说不买，
    # 不把置信百分比塞进每一行。
    payload = _payload("600000", NAMES["600000"], action="hold",
                       position="flat", confidence=0.436, price=21.95)
    text = _render(payload)
    assert "- 示例银行 21.95：不买，今天没出现买点。" in text
    assert "把握" not in text


def test_snapshot_flat_low_confidence():
    text = _render(_flat_low_confidence())
    assert "- 示例银行 12.30：不买，模型也没把握（把握 低，2.1%）。" in text


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


def test_snapshot_insufficient_data():
    payload = _payload("600000", NAMES["600000"], action="insufficient_data",
                       position="flat", confidence=None, price=12.3,
                       status="insufficient_data")
    text = _render(payload)
    assert "- 示例银行 12.30：不参与，数据不够，今天不给判断。" in text
    assert "结论：今天不用动。" in text


# ---------------------------------------------------------------------------
# 结论行计数
# ---------------------------------------------------------------------------
def test_conclusion_zero_actions():
    text = _render(_flat(), _holding())
    assert "结论：今天不用动。" in text
    assert "件事要做" not in text


def test_conclusion_two_actions():
    text = render_daily_brief([_buy(), _sell(), _flat()], date=DATE)
    assert "结论：今天有 2 件事要做。" in text


def test_actions_listed_before_informational_items():
    text = render_daily_brief([_flat(), _buy()], date=DATE)
    assert text.index("示例白酒") < text.index("示例银行")


# ---------------------------------------------------------------------------
# 术语黑名单
# ---------------------------------------------------------------------------
def test_banned_terms_zero_hits_on_all_shapes():
    outputs = [
        _render(_buy()), _render(_holding()), _render(_flat()),
        _render(_flat_low_confidence()), _render(_sell()), _render(_avoid()),
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
    assert render_daily_brief([], date=DATE) == (
        HEADER + "\n\n结论：今天不用动。")


def test_no_none_in_output():
    text = _render(_buy(), _holding(), _flat(), _sell(), _avoid())
    assert "None" not in text


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