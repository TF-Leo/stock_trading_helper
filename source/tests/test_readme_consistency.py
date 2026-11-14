# -*- coding: utf-8 -*-
"""P2（issue #35）：README 的 P1-3 示例必须与真实输出一致。

V10（issue #34）观察到的三处不一致：

1. README 的 JSON 示例里 ``market_regime.definition`` 仍写
   ``simple_moving_average``，而真实输出（``decision/regime.py`` 的
   ``stock_regime``）是 ``ma20_ma60_return_volatility``；
2. ``source/docs/example_output_600519.json`` 被 README 称作「唯一完整真实样例」，
   但它是 P1-1/P1-2/P1-3 之前的快照 —— 缺 ``market_regime.index``、
   ``evidence.flow``、``evidence.valuation``、``diagnostics.source_attempts``；
3. WAF 措辞把「2026-11-12 当时 1/3 入口 501」写成了永久事实。

这里用**离线**断言锁住 1 与 2：不跑 CLI（跑一次要 20+ 秒且依赖行情缓存），
而是直接检查文档文本与已提交的真实样例文件。示例与代码的同步由
``example_output_600519.json`` 由命令现跑生成这一约定保证 ——
本测试负责发现「文件过期了」这件事。
"""
import json
import re
import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1]
README = SRC.parent / "README.md"
EXAMPLE = SRC / "docs" / "example_output_600519.json"

INDEX_KEYS = {"label", "confidence", "as_of", "definition", "source", "bars"}


@pytest.fixture(scope="module")
def readme_text():
    return README.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def example():
    return json.loads(EXAMPLE.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# 1) definition 取值
# ---------------------------------------------------------------------------
def test_readme_has_no_stale_simple_moving_average(readme_text):
    """``simple_moving_average`` 已不是任何路径的取值，README 不得再出现。"""
    assert "simple_moving_average" not in readme_text


def test_readme_market_regime_definition_matches_code(readme_text):
    """README 示例里的 definition 必须等于 ``stock_regime`` 的真实返回值。

    直接读 ``decision/regime.py`` 的源码常量（该文件是 definition 的唯一真源），
    避免 README 与实现各写一份、互相对不上。
    """
    from decision import regime
    source = Path(regime.__file__).read_text(encoding="utf-8")
    found = set(re.findall(r"'definition': '([a-z0-9_]+)'", source))
    assert "ma20_ma60_return_volatility" in found, found
    assert '"definition": "ma20_ma60_return_volatility"' in readme_text


def test_example_definition_matches_code(example):
    assert example["market_regime"]["definition"] == "ma20_ma60_return_volatility"


# ---------------------------------------------------------------------------
# 2) market_regime.index 六键
# ---------------------------------------------------------------------------
def test_example_market_regime_has_all_six_index_keys(example):
    index = example["market_regime"].get("index")
    assert isinstance(index, dict), "真实样例缺 market_regime.index"
    assert set(index) == INDEX_KEYS, set(index) ^ INDEX_KEYS


def test_readme_documents_all_six_index_keys(readme_text):
    """README 的 index 表格必须逐键覆盖六个键。"""
    section = readme_text.split("### market_regime", 1)[1].split("###", 1)[0]
    for key in INDEX_KEYS:
        assert f"`{key}`" in section, f"README 的 index 表格缺 {key}"


def test_readme_index_example_block_is_valid_json(readme_text):
    """README 里那段 market_regime 示例必须能解析成 JSON（不是伪 JSON）。"""
    match = re.search(r'"market_regime": \{.*?\n  \}', readme_text, re.S)
    assert match, "未找到 market_regime 示例块"
    block = json.loads("{" + match.group(0) + "}")
    assert block["market_regime"]["index"].keys() == INDEX_KEYS


# ---------------------------------------------------------------------------
# 3) 「完整真实样例」的完整性（防止再次提交过期快照）
# ---------------------------------------------------------------------------
def test_example_is_a_full_response(example):
    for key in ("schema_version", "generated_at", "request", "instrument",
                "data", "market_regime", "recommendation", "evidence",
                "candidates", "diagnostics", "disclaimer"):
        assert key in example, f"真实样例缺顶层键 {key}"


def test_example_evidence_includes_p1_blocks(example):
    """P1-1 / P1-2 / P1-3 的展示块必须都在真实样例里。"""
    evidence = example["evidence"]
    for key in ("flow", "valuation", "edge", "signal_stats", "decision_replay"):
        assert key in evidence, f"真实样例缺 evidence.{key}"
    assert set(evidence["flow"]) >= {
        "available", "code", "source", "caliber", "as_of", "reason"}
    assert len(evidence["valuation"]) >= 20


def test_example_diagnostics_has_source_attempts(example):
    assert "source_attempts" in example["diagnostics"]


def test_example_flow_code_is_six_digits(example):
    """issue #40 的一致性要求：样例里的 flow.code 也必须是 6 位纯码。"""
    code = example["evidence"]["flow"].get("code")
    assert code is None or re.fullmatch(r"\d{6}", str(code)), code


# ---------------------------------------------------------------------------
# 4) WAF 措辞标注观测时点
# ---------------------------------------------------------------------------
def test_readme_waf_wording_has_observation_date(readme_text):
    """WAF/入口描述必须带观测时点，不能写成永久结论。"""
    section = readme_text.split("### market_regime", 1)[1].split("###", 1)[0]
    assert "2026-11-12" in section, "README 的 WAF 措辞未标注观测时点"
    assert "1/3" in section, "README 未说明「三入口中仅 1/3 不可用」"
    assert "并非永久" in section or "不是永久" in section, \
        "README 未说明该观测不是永久结论"


def test_evidence_plan_waf_rows_carry_date():
    plan = (SRC.parent / "docs" / "P1_EVIDENCE_PLAN.md").read_text(encoding="utf-8")
    assert "**2026-11-12 观测**" in plan
    # A3 行与 §2.2.3 标题都要带时点
    a3 = [ln for ln in plan.splitlines() if ln.startswith("| A3 |")]
    assert a3 and "2026-11-12" in a3[0], a3
    assert "2026-11-12 观测" in plan.split("#### 2.2.3", 1)[1][:200]


# ---------------------------------------------------------------------------
# 5) 前向台账记录键集合（issue #99：新增 basis_uncertain 必须同步文档）
# ---------------------------------------------------------------------------
LEDGER_RUN_KEYS = {"cli_exit", "elapsed_s", "recorded_at", "missing",
                   "instrument_type", "price_source", "price_caliber",
                   "instrument_basis", "basis_uncertain"}


def test_readme_ledger_run_row_matches_build_record_keys(readme_text):
    """README 的「运行」行必须逐键等于 ``build_record`` 的口径/运行字段。

    历史问题：README 与实现各写一份键列表，加了 additive 字段只改一边。
    这里直接从实现取键集合，与 README 表格逐键比对。
    """
    from stock_analysis import forward_ledger as fl

    rec = fl.build_record("600519", None, 30, 1.0, "2026-11-12")
    assert LEDGER_RUN_KEYS <= set(rec), LEDGER_RUN_KEYS - set(rec)
    row = next(ln for ln in readme_text.splitlines()
               if ln.startswith("| 运行 |"))
    documented = set(re.findall(r"`([a-z_]+)`", row))
    assert documented == LEDGER_RUN_KEYS, documented ^ LEDGER_RUN_KEYS


def test_readme_documents_basis_uncertain_semantics(readme_text):
    """「口径记录时确定、结算时不再更改」与一次重判必须写进 README。"""
    assert "口径记录时确定、结算时不再更改" in readme_text
    assert "basis_uncertain=true" in readme_text
    assert "只允许" in readme_text and "uncertain → 确认" in readme_text
