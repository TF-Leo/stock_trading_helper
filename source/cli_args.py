# -*- coding: utf-8 -*-
"""CLI 纯工具：参数定义 / 日期校验 / JSON 归一化（从 ``cli.py`` 迁出）。

迁出动机与 ``decision/context.py`` 相同（但本模块不放 ``decision/``：
本次任务约束「不改 ``decision/*``」）：契约 D4 要求 ``cli.py ≤ 700`` 行。
这里全是**纯函数、零业务依赖**的一块，搬走不改任何行为。

``cli.py`` 仍以同名 re-export 暴露 ``build_parser`` / ``_parse_cli_date`` /
``_date_arg`` / ``_jsonable`` / ``parse_pct``，因此
``tests/test_cli_date_args.py``、``tests/test_p3_bayes.py`` 与 ``tools/probe.py``
的调用点（含 ``python cli.py --help`` 真跑）全部不受影响。
"""
from __future__ import annotations

import argparse
import re
from datetime import datetime

import numpy as np

__all__ = [
    "build_parser",
    "parse_pct",
    "_date_arg",
    "_jsonable",
    "_parse_cli_date",
]


def _jsonable(obj):
    """C1 契约：递归把 numpy 标量/数组转换为原生 Python 类型。

    修复前 ``python cli.py 600519 --json`` 会抛
    "Object of type bool is not JSON serializable" 并以退出码 30 结束。
    """
    if isinstance(obj, dict):
        return {k: _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_jsonable(v) for v in obj]
    if isinstance(obj, np.bool_):
        return bool(obj)
    if isinstance(obj, np.integer):
        return int(obj)
    if isinstance(obj, np.floating):
        return float(obj)
    if isinstance(obj, np.ndarray):
        return _jsonable(obj.tolist())
    return obj


def parse_pct(s):
    """解析百分比字符串"""
    if isinstance(s, (int, float)):
        return s
    try:
        return float(str(s).replace('%', '')) / 100
    except Exception:
        return 0.0


_DATE_FORMATS = ("YYYYMMDD", "YYYY-MM-DD")

_DATE_SHAPES = (re.compile(r'^\d{8}$'), re.compile(r'^\d{4}-\d{2}-\d{2}$'))


def _parse_cli_date(value, option):
    """把 ``--start`` / ``--as-of`` 的入参校验成日期。

    P2（issue #18）：旧实现把非法值**静默忽略** —— ``--start 2026-13-99`` 会被
    ``_apply_start`` 里的 ``except: return df`` 吞掉，按默认窗口继续跑，调用方
    无法区分「参数写错」和「正常输出」。现在非法值一律抛 ``ValueError``，
    由 argparse 转成退出码 2 + 可读原因（``parser.error`` 的语义）。

    接受 ``YYYYMMDD`` 与 ``YYYY-MM-DD`` 两种写法（工具链两者都在用）。
    先用正则卡**字面形状**，再交给 ``strptime`` 校验日历有效性：只靠
    ``strptime`` 会放过 ``2026113`` / ``2026-1-1`` 这类补零不全的写法
    （它会当合法日期接受），而 issue #18 的契约是这两个精确格式。
    格式对但日期不存在（如 ``20260231``）同样拒绝 —— 否则会被
    ``pd.to_datetime`` 悄悄解析成 3 月 3 日，窗口与调用方预期不符。

    返回原字符串（不做格式归一）：``--start`` 与 ``--as-of`` 的下游分别按
    ``pd.to_datetime`` / ``pd.Timestamp`` 解析，改写入参可能改变既有输出。

    抛 ``argparse.ArgumentTypeError``（而非 ``ValueError``）：前者会让 argparse
    原样打印这条可读原因，后者只会打印 ``invalid <type> value`` 这类无信息文本。
    两条路径都是 ``parser.error`` → stderr + 退出码 2。
    """
    text = str(value).strip()
    if any(shape.match(text) for shape in _DATE_SHAPES):
        for fmt in ("%Y%m%d", "%Y-%m-%d"):
            try:
                datetime.strptime(text, fmt)
            except ValueError:
                continue
            return text
    raise argparse.ArgumentTypeError(
        f"{option} 需要 {_DATE_FORMATS[0]} 或 {_DATE_FORMATS[1]} 格式的有效日期，"
        f"收到 {value!r}")


def _date_arg(option):
    """为 argparse 生成 ``type=`` 回调（抛 ArgumentTypeError → 退出码 2）。"""
    def _convert(value):
        return _parse_cli_date(value, option)
    _convert.__name__ = option
    return _convert


def build_parser():
    """D6: CLI 参数定义（probe 会直接复用本函数验证"旧参数不报 usage error"）。"""
    parser = argparse.ArgumentParser(description='Hermes Trading Decision Engine')
    parser.add_argument('code', help='股票代码或基金代码')
    parser.add_argument('--json', action='store_true', help='输出 JSON 格式（stdout 只输出单个 JSON 文档）')
    parser.add_argument('--fast', action='store_true', help='快速模式（缩减 bootstrap / 候选池 / walk-forward 折数）')
    parser.add_argument('--start', default=None, type=_date_arg('--start'),
                        help='开始日期，YYYYMMDD 或 YYYY-MM-DD（如 20200101）')
    parser.add_argument('--type', '-t', default='auto',
                        help='类型：auto/stock/fund（auto 按代码前缀推断，5 开头视为基金）')
    parser.add_argument('--jobs', type=int, default=1, help='并行任务数')
    parser.add_argument('--opt', default='grid', choices=['grid', 'bayes'],
                        help='优化方式：grid=全网格581组，bayes=贝叶斯寻优')
    # D6 向后兼容：以下三个参数曾被删除，旧调用方会直接 usage error(2)
    parser.add_argument('--detail', action='store_true',
                        help='文本模式输出详细内容（触发器 / 候选表 / 指标）')
    parser.add_argument('--no-regime', action='store_true',
                        help='跳过 market_regime 计算，输出 Unknown/disabled（不假装 Bull）')
    parser.add_argument('--opt-iters', type=int, default=30,
                        help='--opt bayes 的迭代次数（--fast 时上限 12）')
    parser.add_argument('--as-of', dest='as_of', default=None,
                        metavar='YYYY-MM-DD', type=_date_arg('--as-of'),
                        help='历史复盘：把行情截断到该日期（不含未来数据）；'
                             'YYYYMMDD 或 YYYY-MM-DD')
    return parser
