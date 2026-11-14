#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""建议渲染层 v1 的命令行包装：读 ``cli.py --json`` 的完整 payload，打印建议文本。

用法：
    source/.venv/bin/python tools/render_advice.py --json-file payload.json
    source/.venv/bin/python cli.py 600000 --json | source/.venv/bin/python tools/render_advice.py

可选参数：
* ``--name "<简称>"``：覆盖 payload 里的名称（场外基金的 ``instrument.name``
  常等于代码，Hermes 可用它喂入基金短名）。
* ``--compact``：压缩 ``diagnostics.warnings``（整段删除 URL / 堆栈回溯段 /
  配对完整的内部引用括号，再把少量裸内部字段名换成固定中文表述），盘前投递
  建议开启；不传时输出与默认渲染逐字节一致。

设计边界：
* 只做 I/O 包装，渲染逻辑全部在 ``stock_analysis.advice_render.render_advice``；
* **不触网**、不读行情缓存、不重算任何决策数值；
* 非法 JSON / 非对象 payload 以退出码 2 结束，并把原因写到 stderr；渲染成功退出码 0。
"""
from __future__ import annotations

import argparse
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "source")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from stock_analysis.advice_render import render_advice  # noqa: E402


def _read_payload(path: str) -> dict:
    if path == "-":
        text = sys.stdin.read()
    else:
        with open(path, "r", encoding="utf-8") as handle:
            text = handle.read()
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise SystemExit(f"错误：JSON 解析失败：{exc}") from exc
    if not isinstance(payload, dict):
        raise SystemExit("错误：payload 必须是 JSON 对象（cli.py --json 的完整输出）")
    return payload


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="把 cli.py --json 的输出渲染成一段可直接投递的每日建议")
    parser.add_argument("--json-file", default="-", metavar="PATH",
                        help="payload JSON 文件；默认 '-' 表示从 stdin 读取")
    parser.add_argument("--name", default=None, metavar="简称",
                        help="覆盖 payload 名称（写入顶层 display_name）")
    parser.add_argument("--compact", action="store_true",
                        help="压缩 diagnostics.warnings（整段删除 URL/堆栈/内部引用括号，"
                             "并把少量裸内部字段名换成固定中文表述）")
    args = parser.parse_args(argv)

    try:
        payload = _read_payload(args.json_file)
    except SystemExit as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except OSError as exc:
        print(f"错误：读取 {args.json_file} 失败：{exc}", file=sys.stderr)
        return 2

    if args.name:
        # 不修改原 payload：浅拷贝后注入 display_name，render_advice 仍是纯函数。
        payload = dict(payload)
        payload["display_name"] = args.name

    print(render_advice(payload, compact=args.compact))
    return 0


if __name__ == "__main__":
    sys.exit(main())
