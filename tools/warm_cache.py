# -*- coding: utf-8 -*-
"""离线预热：把所需标的的行情缓存落盘，之后可完全离线运行。

用法（在 source/ 下执行，或任意目录执行均可）：

    python tools/warm_cache.py                # 默认标的集
    python tools/warm_cache.py 600519 510300  # 指定标的

设计要点（2026-10-18）：
  * **逐票断言**：`get_stock_daily` 取数失败时返回 None、只往 stderr 打一行，**不抛异常**。
    若用 `[get_stock_daily(c) for c in ...]` 这种写法，失败会被静默吞掉，
    结果是"命令成功退出但缓存是空的"，下一步才以 FileNotFoundError /
    insufficient_data 爆炸，误导排查方向。故此处逐票检查行数并汇总失败。
  * 退出码：0 = 全部就绪；1 = 有标的取数失败（会逐条列出）。
  * 不写任何"空壳"缓存：落盘由 data_loader 负责，失败时它不创建文件。
"""
from __future__ import annotations

import os
import sys


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))   # 仓库根
SRC = os.path.join(ROOT, "source")                                   # Python 代码在 source/
sys.path.insert(0, SRC)
DEFAULT_STOCKS = ["600519", "688981", "002371", "300750"]
DEFAULT_FUNDS = ["510300"]
MIN_BARS = 100          # 与 cli.py 的 insufficient_data 阈值一致


def main(argv):
    stocks = [a for a in argv if not a.startswith("5")] or DEFAULT_STOCKS
    funds = [a for a in argv if a.startswith("5")] or DEFAULT_FUNDS
    if argv:
        # 显式指定时不再套默认集
        stocks = [a for a in argv if not a.startswith("5")]
        funds = [a for a in argv if a.startswith("5")]

    from stock_analysis.data_loader import get_stock_daily, get_fund_daily

    fails = []
    for code in stocks:
        try:
            df = get_stock_daily(code)
        except Exception as e:                      # 取数渠道抛错也要记为失败
            print(f"{code}: 异常 {type(e).__name__}: {e}", file=sys.stderr)
            fails.append((code, "exception"))
            continue
        n = 0 if df is None else len(df)
        print(f"{code}: {n} 根")
        if n < MIN_BARS:
            fails.append((code, f"{n} 根 < {MIN_BARS}"))

    for code in funds:
        try:
            df = get_fund_daily(code)
        except Exception as e:
            print(f"{code}: 异常 {type(e).__name__}: {e}", file=sys.stderr)
            fails.append((code, "exception"))
            continue
        n = 0 if df is None else len(df)
        print(f"{code}: {n} 根")
        if n < MIN_BARS:
            fails.append((code, f"{n} 根 < {MIN_BARS}"))

    if fails:
        print("", file=sys.stderr)
        print("ERROR 预热失败：", file=sys.stderr)
        for code, why in fails:
            print(f"  - {code}: {why}", file=sys.stderr)
        print("", file=sys.stderr)
        print("常见原因：出口 IP 被该行情源封锁 / 当前网络不可达 / 代码不存在。", file=sys.stderr)
        print("请把以上完整输出（含 stderr）反馈，不要继续后续步骤。", file=sys.stderr)
        return 1

    print("OK 预热完成，可离线运行")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
