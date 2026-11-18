#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""盘前简报 v2 的命令行入口（issue #149）：把多个 ``cli.py --json`` payload
渲染成**最终投递文本**，Hermes 侧原样发送 stdout，不改写任何内容。

用法：
    source/.venv/bin/python tools/render_daily_brief.py --json-dir /path/to/payloads \
        --date 2026-11-18 --name 002415=海康威视 --name 023299=汇添富A500C

    source/.venv/bin/python tools/render_daily_brief.py --json-file a.json --json-file b.json

    cat payloads.json | source/.venv/bin/python tools/render_daily_brief.py

输入：
* ``--json-file PATH``（可重复）：每份文件可以是单个 payload 对象，也可以是
  payload 列表；``-`` 表示 stdin。
* ``--json-dir DIR``：读取目录下所有 ``*.json``（按文件名排序）。
* 都不给时读 stdin。

可选：
* ``--date``：投递日期（``YYYY-MM-DD`` / ``M/D``），默认今天；只影响标题。
* ``--name CODE=简称``（可重复）：注入 ``display_name``（场外基金的
  ``instrument.name`` 常等于代码）。
* ``--check-terms``：渲染后扫描术语黑名单，命中时把术语写 stderr 并退出 3
  （文本仍照常打印，便于定位）。

设计边界：只做 I/O 包装；渲染逻辑全部在
``stock_analysis.daily_brief.render_daily_brief``；不触网、不重算任何决策值。
非法 JSON / 非对象 payload 以退出码 2 结束。
"""
from __future__ import annotations

import argparse
import datetime as _dt
import glob
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "source")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from stock_analysis.daily_brief import (  # noqa: E402
    find_banned_terms,
    render_daily_brief,
)


def _read_text(path: str) -> str:
    if path == "-":
        return sys.stdin.read()
    with open(path, "r", encoding="utf-8") as handle:
        return handle.read()


def _payloads_from_path(path: str) -> list:
    text = _read_text(path)
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise SystemExit(f"错误：{path} JSON 解析失败：{exc}") from exc
    if isinstance(data, dict):
        return [data]
    if isinstance(data, list) and all(isinstance(item, dict) for item in data):
        return list(data)
    raise SystemExit(f"错误：{path} 必须是 payload 对象或 payload 对象列表")


def _collect(args) -> list:
    paths = list(args.json_file or [])
    if args.json_dir:
        paths.extend(sorted(glob.glob(os.path.join(args.json_dir, "*.json"))))
    if not paths:
        paths = ["-"]
    payloads = []
    for path in paths:
        payloads.extend(_payloads_from_path(path))
    return payloads


def _name_map(raw_names) -> dict:
    mapping = {}
    for item in raw_names or []:
        if "=" not in item:
            raise SystemExit(f"错误：--name 需要 CODE=简称，收到 {item!r}")
        code, name = item.split("=", 1)
        code, name = code.strip(), name.strip()
        if not code or not name:
            raise SystemExit(f"错误：--name 需要非空 CODE 和简称，收到 {item!r}")
        mapping[code] = name
    return mapping


def _code(payload: dict) -> str:
    inst = payload.get("instrument") if isinstance(payload, dict) else None
    request = payload.get("request") if isinstance(payload, dict) else None
    for block in (inst, request):
        if isinstance(block, dict):
            value = block.get("code")
            if isinstance(value, str) and value.strip():
                return value.strip()
    return ""


def _display_names(payloads) -> list:
    """payload 里已带的名称（术语扫描的白名单，含未通过 --name 覆盖的）。"""
    names = []
    for payload in payloads:
        if not isinstance(payload, dict):
            continue
        inst = payload.get("instrument")
        inst = inst if isinstance(inst, dict) else {}
        for candidate in (payload.get("display_name"),
                          inst.get("display_name"), inst.get("name")):
            if isinstance(candidate, str) and candidate.strip():
                names.append(candidate.strip())
    return names


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="把多个 cli.py --json payload 渲染成可直接投递的盘前简报")
    parser.add_argument("--json-file", action="append", default=[],
                        metavar="PATH",
                        help="payload 文件（对象或数组）；可重复；'-' 为 stdin")
    parser.add_argument("--json-dir", default=None, metavar="DIR",
                        help="读取目录下所有 *.json（按文件名排序）")
    parser.add_argument("--date", default=None, metavar="DATE",
                        help="投递日期（YYYY-MM-DD 或 M/D），默认今天")
    parser.add_argument("--name", action="append", default=[],
                        metavar="CODE=简称",
                        help="注入标的简称；可重复")
    parser.add_argument("--check-terms", action="store_true",
                        help="扫描术语黑名单；命中退出码 3")
    args = parser.parse_args(argv)

    try:
        payloads = _collect(args)
        name_map = _name_map(args.name)
    except SystemExit as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except OSError as exc:
        print(f"错误：读取 payload 失败：{exc}", file=sys.stderr)
        return 2
    # 不修改调用方数据：浅拷贝后注入 display_name（渲染层仍是纯函数）。
    prepared = []
    for payload in payloads:
        copy = dict(payload)
        code = _code(payload)
        if code in name_map:
            copy["display_name"] = name_map[code]
        prepared.append(copy)

    date = args.date
    if date is None:
        date = _dt.date.today()
    text = render_daily_brief(prepared, date=date)
    print(text)

    if args.check_terms:
        names = list(name_map.values()) + _display_names(prepared)
        hits = find_banned_terms(text, names=names)
        if hits:
            print("术语黑名单命中：" + "、".join(hits), file=sys.stderr)
            return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())