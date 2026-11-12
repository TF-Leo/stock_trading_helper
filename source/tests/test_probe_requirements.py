# -*- coding: utf-8 -*-
"""P2（issue #31）：probe 的 requirements 检查不得把 marker 分支当硬 pin。

历史现象（V9 冒烟）：``tools/probe.py`` 用
``^([\\w-]+)==([\\w.]+)`` 抽取 ``==`` 行塞进 dict，于是

    numpy==2.2.6; python_version < "3.14"
    numpy>=2.3.3,<3; python_version >= "3.14"

被压成同一个键（后写覆盖前写），只剩 ``numpy==2.2.6``；3.14 机器实装
2.3.5/2.5.4 → 从 d4264a4 起长期假 FAIL，掩盖真实依赖问题。

本文件覆盖 issue 明确要求的三类场景：
  * 3.14 + numpy 2.3.5 / 2.5.4 → PASS；
  * 3.11 + numpy 2.2.6 → PASS；
  * 故意装错版本 → FAIL。

另外用 ``packaging``（若可用）交叉验证版本区间与 marker 求值，防止自己
手写的 PEP 440 子集在边界上悄悄偏掉。
"""
import importlib.util
import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1]
TOOLS = SRC.parent / "tools"
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

import requirements_check as rc  # noqa: E402

REQUIREMENTS_TXT = (SRC / "requirements.txt").read_text(encoding="utf-8")


def _probe_module():
    spec = importlib.util.spec_from_file_location("probe_req", TOOLS / "probe.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _env(py_version, **extra):
    """构造 marker 环境（其余变量取当前机器，避免平台相关抖动）。"""
    env = dict(rc._default_env())
    env["python_version"] = py_version
    env["python_full_version"] = py_version if py_version.count(".") >= 2 \
        else py_version + ".0"
    env.update(extra)
    return env


def _run(versions, env):
    """用给定「已安装版本」求值真实的 requirements.txt。"""
    reqs = rc.parse_requirements(REQUIREMENTS_TXT)
    return rc.check_environment(reqs, versions, env=env)


BASE = {"akshare": "1.18.29", "requests": "2.32.5", "pandas": "3.0.1",
        "pytest": "8.3.5"}


# ---------------------------------------------------------------------------
# 1) issue 要求的三类场景
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("numpy_version", ["2.3.5", "2.5.4", "2.3.3", "2.9.9"])
def test_py314_with_interval_numpy_passes(numpy_version):
    problems, observed = _run({**BASE, "numpy": numpy_version},
                              _env("3.14"))
    assert problems == [], problems
    assert observed["numpy"]["active"] is True
    # 3.14 分支生效、<3.14 那行被 marker 跳过
    assert observed["numpy"]["skipped_markers"] == ['python_version < "3.14"']


def test_py311_with_pinned_numpy_passes():
    problems, observed = _run({**BASE, "numpy": "2.2.6"}, _env("3.11"))
    assert problems == [], problems
    assert observed["numpy"]["skipped_markers"] == ['python_version >= "3.14"']


@pytest.mark.parametrize("env_version,numpy_version", [
    ("3.14", "2.2.6"),   # 3.14 上装了旧分支的 pin
    ("3.14", "3.0.0"),   # 越过 <3 上界
    ("3.14", "2.3.2"),   # 低于 >=2.3.3 下界
    ("3.11", "2.3.5"),   # 3.11 上装了新分支的版本（不是 ==2.2.6）
    ("3.12", "2.2.7"),
])
def test_wrong_version_fails(env_version, numpy_version):
    problems, _ = _run({**BASE, "numpy": numpy_version}, _env(env_version))
    assert problems, "版本不满足区间时必须报 FAIL"
    assert any("numpy" in p for p in problems), problems


def test_missing_package_fails():
    versions = dict(BASE)  # 不放 numpy
    problems, _ = _run(versions, _env("3.14"))
    assert any("numpy" in p and "未安装" in p for p in problems), problems


def test_every_other_pin_still_checked():
    """numpy 修好不代表可以放过其它包：akshare 装错仍要 FAIL。"""
    problems, _ = _run({**BASE, "numpy": "2.5.4", "akshare": "1.18.30"},
                       _env("3.14"))
    assert any("akshare" in p for p in problems), problems


# ---------------------------------------------------------------------------
# 2) 真实环境 + probe 入口
# ---------------------------------------------------------------------------
def test_current_environment_passes_via_probe():
    """本机（3.14 + numpy 2.5.4）真跑 probe 的 G30：必须是 PASS。"""
    result = _probe_module().check_requirements()
    assert result["status"] == "PASS", result["reason"]
    assert result["observed"]["numpy"]["active"] is True


def test_probe_result_shape_is_stable():
    result = _probe_module().check_requirements()
    assert set(result) == {"status", "reason", "observed"}
    for pkg in ("akshare", "numpy", "pandas", "pytest", "requests"):
        assert pkg in result["observed"], f"observed 缺 {pkg}"
        entry = result["observed"][pkg]
        assert set(entry) == {"requirements", "active", "actual",
                              "skipped_markers"}


def test_probe_reports_failure_when_requirements_lie(tmp_path, monkeypatch):
    """改坏 requirements.txt 后 probe 必须 FAIL（证明它真的在读文件判定）。"""
    probe = _probe_module()
    broken = tmp_path / "requirements.txt"
    broken.write_text("numpy==2.2.6; python_version < \"3.14\"\n"
                      "numpy>=99,<100; python_version >= \"3.14\"\n",
                      encoding="utf-8")
    monkeypatch.setattr(probe, "SRC", str(tmp_path))
    result = probe.check_requirements()
    assert result["status"] == "FAIL", result
    assert "numpy" in result["reason"]


# ---------------------------------------------------------------------------
# 3) requirements.txt 解析本身
# ---------------------------------------------------------------------------
def test_marker_lines_are_kept_separate():
    """两条同名但 marker 不同的行不得被压成一条（事故根因）。"""
    reqs = rc.parse_requirements(REQUIREMENTS_TXT)
    numpy = [r for r in reqs if r["name"] == "numpy"]
    assert len(numpy) == 2, numpy
    assert {r["marker"] for r in numpy} == {
        'python_version < "3.14"', 'python_version >= "3.14"'}
    # 区间行必须被识别成区间，而不是「==某个版本」
    interval = [r for r in numpy if r["marker"] and ">=" in r["marker"]][0]
    assert (">=", "2.3.3") in interval["specifiers"]
    assert ("<", "3") in interval["specifiers"]


def test_comments_and_blanks_are_ignored():
    reqs = rc.parse_requirements("# 注释\n\n   \nrequests==2.32.5  # 行尾注释\n")
    assert [r["name"] for r in reqs] == ["requests"]
    assert reqs[0]["specifiers"] == [("==", "2.32.5")]


def test_parse_error_is_not_silently_skipped():
    """看不懂的行必须报错，不能当通过（否则门禁形同虚设）。"""
    with pytest.raises(rc.RequirementsParseError):
        rc.parse_requirements("numpy @@@ 2.3.3\n")


def test_unknown_marker_variable_is_rejected():
    with pytest.raises(rc.RequirementsParseError):
        rc.evaluate_marker('nonsense_var == "1"', _env("3.14"))


# ---------------------------------------------------------------------------
# 4) 与 packaging 交叉验证（防止手写 PEP 440 子集悄悄偏掉）
# ---------------------------------------------------------------------------
packaging = pytest.importorskip("packaging")


@pytest.mark.parametrize("specifier", [
    "==2.2.6", ">=2.3.3,<3", "~=2.3.3", ">2.0", "<=2.5.4", "!=2.3.5",
    "==2.3.*", "!=2.3.*", ">2.3.5rc1", "<3.0.0", ">=2.3.5", "~=2.3",
])
@pytest.mark.parametrize("version", [
    "2.2.6", "2.3.3", "2.3.5", "2.5.4", "3.0.0", "1.26.0", "2.3.2",
    "2.4.0rc1", "2.3.5.dev0", "2.3.5.post1", "2.3.5rc2", "10.0.1",
])
def test_version_interval_matches_packaging(specifier, version):
    from packaging.specifiers import SpecifierSet
    from packaging.version import Version

    mine = rc.version_satisfies(version, rc.parse_specifiers(specifier))
    assert mine == (Version(version) in SpecifierSet(specifier)), \
        f"{version} {specifier}"


@pytest.mark.parametrize("marker", [
    'python_version < "3.14"',
    'python_version >= "3.14"',
    'python_version < "3.14" or python_version >= "3.14"',
    'python_version > "3.0" and sys_platform == "linux"',
    'sys_platform != "win32"',
    '"linux" in sys_platform',
    'platform_machine in "x86_64 AMD64"',
])
def test_marker_matches_packaging(marker):
    from packaging.markers import Marker

    env = rc._default_env()
    assert rc.evaluate_marker(marker, env) == Marker(marker).evaluate(env), marker
