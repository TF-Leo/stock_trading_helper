# -*- coding: utf-8 -*-
"""P2（issue #18）：``--start`` / ``--as-of`` 非法值必须显式失败（退出码 2）。

历史现象（V3 实盘冒烟，2026-11-12）：非法日期被**静默忽略** ——
``--start 2026-13-99`` 在 ``_apply_start`` 里撞到 ``except: return df``，
按默认窗口继续跑并正常输出，调用方无法区分「参数写错」和「正常输出」。

本文件分两层：
  1. ``_parse_cli_date`` 的纯函数行为（合法 / 非法 / 边界）；
  2. 真实子进程端到端（argparse 的 ``parser.error`` 语义 = stderr + 退出码 2）。

第 2 层只跑**非法**输入：argparse 在 ``parse_args`` 阶段就退出，不会触网；
合法输入的端到端行为由 ``test_legal_dates_are_accepted_by_parser`` 在进程内
用 ``parse_args`` 覆盖（真跑合法窗口需要行情缓存，属于 probe G29 的职责）。
"""
import subprocess
import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1]
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


def _parse(value, option="--start"):
    import cli
    return cli._parse_cli_date(value, option)


# ---------------------------------------------------------------------------
# 1) 纯函数：合法格式
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("value", [
    "20200101", "2026-11-13", "19900101", "2024-02-29",   # 闰日
    "20260101", "20991231",
])
def test_legal_dates_pass_and_are_returned_unchanged(value):
    """合法值原样返回（不改写格式：下游解析依赖调用方原本的写法）。"""
    assert _parse(value) == value


def test_legal_dates_surrounding_whitespace_is_stripped():
    """两侧空白不算非法（shell 引用容易带入），但返回值不带空白。"""
    assert _parse("  20260101 ") == "20260101"
    assert _parse("\t2026-01-01\n") == "2026-01-01"


# ---------------------------------------------------------------------------
# 2) 纯函数：非法格式与边界
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("value", [
    "abc", "", "   ", "2026/01/01", "20260101x", "x20260101",
    "202601", "2026-11", "202601010",                 # 长度不对
    "2026113", "2026-1-1", "202611-3",                # 补零不全（strptime 会放过）
    "20261301", "20260132", "20260231", "2026-02-30",  # 月/日越界
    "2026-13-01", "00000101",                          # 月份 13 / 公元 0 年
])
def test_illegal_dates_raise_readable_error(value):
    """非法值抛 ArgumentTypeError，且原因里带原始入参与期望格式。"""
    import argparse
    with pytest.raises(argparse.ArgumentTypeError) as exc:
        _parse(value)
    msg = str(exc.value)
    assert repr(value) in msg, f"错误信息未回显原始入参: {msg}"
    assert "YYYYMMDD" in msg and "YYYY-MM-DD" in msg, msg


def test_option_name_appears_in_error():
    """--start 与 --as-of 的错误要能区分（同一个非法值，不同选项名）。"""
    import argparse
    for option in ("--start", "--as-of"):
        with pytest.raises(argparse.ArgumentTypeError) as exc:
            _parse("nope", option)
        assert option in str(exc.value), str(exc.value)


# ---------------------------------------------------------------------------
# 3) 进程内 parse_args：合法值被接受且落到对应属性
# ---------------------------------------------------------------------------
def test_legal_dates_are_accepted_by_parser():
    import cli
    parser = cli.build_parser()
    args = parser.parse_args(["600519", "--start", "20200101",
                              "--as-of", "2026-11-13"])
    assert args.start == "20200101"
    assert args.as_of == "2026-11-13"

    args = parser.parse_args(["600519", "--start", "2020-01-01"])
    assert args.start == "2020-01-01"
    assert args.as_of is None, "未传 --as-of 时必须保持 None，不得被 type 回调改写"


def test_parser_error_raises_systemexit_2():
    """``parser.error`` 的进程内语义：SystemExit(2)。"""
    import cli
    parser = cli.build_parser()
    with pytest.raises(SystemExit) as exc:
        parser.parse_args(["600519", "--start", "2026-13-99"])
    assert exc.value.code == 2


# ---------------------------------------------------------------------------
# 4) 真实子进程：stderr 有可读原因 + 退出码 2
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("args", [
    ["--start", "20261399"],
    ["--start", "2026-13-99"],
    ["--start", "abc"],
    ["--start", "2026113"],
    ["--as-of", "2026-1-1"],
    ["--as-of", "20260231"],
    ["--as-of", "not-a-date"],
])
def test_cli_subprocess_exits_2_with_reason(args):
    """真跑 CLI：argparse 在取数之前退出，stderr 说明哪个参数、收到什么。"""
    proc = subprocess.run(
        [sys.executable, "cli.py", "600519", *args],
        cwd=str(SRC), capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=120,
    )
    assert proc.returncode == 2, (
        f"{args} 退出码 {proc.returncode}（应为 2）\n"
        f"stdout={proc.stdout[:300]}\nstderr={proc.stderr[:500]}")
    assert proc.stdout.strip() == "", "参数错误时不得向 stdout 输出正常结果"
    err = proc.stderr
    option = args[0]
    bad = args[1]
    assert option in err, f"stderr 未指出出错参数 {option}: {err[:400]}"
    assert bad in err, f"stderr 未回显非法值 {bad!r}: {err[:400]}"
    assert "YYYYMMDD" in err and "YYYY-MM-DD" in err, err[:400]
