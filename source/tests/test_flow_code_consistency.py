# -*- coding: utf-8 -*-
"""P2（issue #40）：``evidence.flow.code`` 错误路径也必须归一化。

V11（issue #38）观察：成功路径的 ``flow.code`` 走 ``_normalize_code``
（``sh600519`` → ``600519``），但错误路径（``data_source_error`` / 缓存缺失）
走的是 ``empty_flow_block``，那里直接 ``str(code)`` —— 同一个字段在两条路径上
给出两种编码（``600519.SH`` / ``SH600519``），下游按 6 位码匹配会漏。

修复后两条路径对同一标的必须给出**逐字相同**的 ``code``。本文件同时锁住
``evidence.valuation.code``（同一处缺陷的孪生副本）。
"""
import re
import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1]
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from stock_analysis.data_loader import flow as flow_mod  # noqa: E402
from stock_analysis.data_loader import valuation as valuation_mod  # noqa: E402
import cli  # noqa: E402

CANONICAL = [
    ("600519", "600519"),
    ("sh600519", "600519"),
    ("SH600519", "600519"),
    ("600519.SH", "600519"),
    ("600519.XSHG", "600519"),
    ("sz000001", "000001"),
    ("000001.SZ", "000001"),
    ("bj920982", "920982"),
    ("510300", "510300"),
    ("510300.SH", "510300"),
]


# ---------------------------------------------------------------------------
# 1) empty_flow_block 自身
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("raw,expected", CANONICAL)
def test_empty_flow_block_normalizes_code(raw, expected):
    assert flow_mod.empty_flow_block(code=raw)["code"] == expected


@pytest.mark.parametrize("raw,expected", CANONICAL)
def test_empty_valuation_block_normalizes_code(raw, expected):
    assert valuation_mod.empty_valuation_block(code=raw)["code"] == expected


def test_unparseable_code_is_left_alone_not_crashed():
    """无法归一（如 'bad-code'）时保持原样，但绝不抛异常。"""
    assert flow_mod.empty_flow_block(code="bad-code")["code"] == "bad-code"
    assert flow_mod.empty_flow_block(code=None)["code"] is None


def test_empty_flow_block_keys_unchanged():
    """归一化只改 code 的值，键集合与成功路径仍然一致。"""
    block = flow_mod.empty_flow_block(code="600519.SH")
    assert set(block) == set(flow_mod.FLOW_KEYS)
    assert block["available"] is False


# ---------------------------------------------------------------------------
# 2) 两条路径逐字一致（issue 的核心断言）
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("raw,expected", CANONICAL)
def test_error_and_success_path_agree(raw, expected):
    """成功路径（summarize_flow 的 code）与错误路径必须一致。"""
    success_code = flow_mod._normalize_code(raw)
    error_code = flow_mod.empty_flow_block(code=raw)["code"]
    assert success_code == error_code == expected


@pytest.mark.parametrize("raw,expected", CANONICAL)
def test_create_error_response_normalizes_both_blocks(raw, expected):
    """真实的错误响应（schema.create_error_response）里两个块都是 6 位纯码。"""
    doc = cli.create_error_response("data_source_error", "取数失败", raw, "stock")
    assert doc["evidence"]["flow"]["code"] == expected
    assert doc["evidence"]["valuation"]["code"] == expected
    assert re.fullmatch(r"\d{6}", doc["evidence"]["flow"]["code"])
    assert re.fullmatch(r"\d{6}", doc["evidence"]["valuation"]["code"])


@pytest.mark.parametrize("status", ["data_source_error", "insufficient_data"])
def test_all_error_statuses_normalize(status):
    doc = cli.create_error_response(status, "失败", "600519.SH", "stock")
    assert doc["evidence"]["flow"]["code"] == "600519"
    assert doc["evidence"]["valuation"]["code"] == "600519"


def test_cache_missing_path_uses_normalized_code(tmp_path, monkeypatch):
    """缓存缺失（build_flow_block）走的是同一函数 —— 一并锁住。"""
    monkeypatch.setattr(flow_mod, "_expected_trade_date",
                        lambda now=None, as_of=None: (None, "estimate"))
    # _expected_trade_date 返回 None 时 build_flow_block 仍应产出归一化 code
    block = flow_mod.build_flow_block("sh600519", flow_dir=str(tmp_path))
    assert block["available"] is False
    assert block["code"] == "600519"


def test_fund_path_uses_normalized_code():
    """基金分支（fund_not_supported）也是错误路径的一种。"""
    block, notes = flow_mod.flow_evidence("510300.SH", data_type="fund")
    assert block["available"] is False
    assert block["code"] == "510300"
    assert notes == []


# ---------------------------------------------------------------------------
# 3) 已有样例与 README 描述
# ---------------------------------------------------------------------------
def test_committed_example_flow_code_is_canonical():
    import json
    example = json.loads(
        (SRC / "docs" / "example_output_600519.json").read_text(encoding="utf-8"))
    code = example["evidence"]["flow"].get("code")
    assert code is None or re.fullmatch(r"\d{6}", str(code)), code


def test_readme_documents_source_and_caliber_as_open_sets():
    """README 的 source/caliber 描述必须覆盖两种现用取值，并说明可扩展。"""
    readme = (SRC.parent / "README.md").read_text(encoding="utf-8")
    section = readme.split("### evidence.flow", 1)[1].split("###", 1)[0]
    for token in ("eastmoney_push2his", "sina_fund_flow_backup",
                  "eastmoney_main_net", "sina_total_net"):
        assert token in section, f"README 的 flow 段缺 {token}"
    assert "开放集合" in section, "README 未说明 source/caliber 是开放集合"
    assert "6 位纯数字代码" in section
    assert "成功与错误路径一致" in section
