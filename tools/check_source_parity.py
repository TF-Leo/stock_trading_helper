# -*- coding: utf-8 -*-
"""渠道一致性核查：确认不同行情源给出的 qfq 历史数值是否一致。

为什么需要这个工具（2026-10-18）：
  数据层是**多通道**的（eastmoney → sina_stock → sina_fund），
  一次运行里具体用哪个通道取决于网络可达性。于是出现一个问题：
  「上一版用东财拉的基线数字，这一版用新浪拉的数据，还能直接比吗？」

  答案不应该靠感觉。本工具把缓存与**指定通道**重新拉取的同一段历史逐日逐列比对，
  给出最大绝对差 / 相对差 / 超差点数。全部为 0 才说明跨通道可比。

用法（在仓库根目录）：
    python tools/check_source_parity.py 600519
    python tools/check_source_parity.py 600519 --source sina
    python tools/check_source_parity.py 600519 --source sina --tol 1e-6

退出码：
    0 = 一致（可跨通道对比）
    1 = 存在超差（跨版本对比需谨慎，本工具会打印最大差异样本）
    2 = 前置条件不满足（缓存缺失 / 通道不可用 / 无重叠交易日）
"""
from __future__ import annotations

import argparse
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "source")
sys.path.insert(0, SRC)
os.chdir(SRC)

COLS = ("open", "high", "low", "close", "volume")


def load_cache(symbol, adjust="qfq"):
    import pandas as pd

    path = os.path.join(SRC, "stock_analysis", "data", f"stock_{symbol}_{adjust}.csv")
    if not os.path.exists(path):
        return None, path
    df = pd.read_csv(path, index_col="date", parse_dates=True)
    return df.sort_index(), path


def _to_exchange_symbol(symbol):
    if not symbol.isdigit():
        return symbol
    if symbol.startswith(("5", "6")):
        return "sh" + symbol
    return "sz" + symbol


def fetch_via(symbol, source, start_date="20200101", adjust="qfq"):
    """按指定通道拉取；返回 (df|None, err)。df 统一成 DatetimeIndex。"""
    import pandas as pd

    try:
        import akshare as ak
    except ImportError as e:
        return None, f"akshare 未安装: {e}"

    sem = _to_exchange_symbol(symbol)
    try:
        if source == "sina":
            df = ak.stock_zh_a_daily(symbol=sem, start_date=start_date, adjust=adjust)
        elif source == "eastmoney":
            df = ak.stock_zh_a_hist(symbol=sem, period="daily",
                                    start_date=start_date, adjust=adjust)
            df = df.rename(columns={"日期": "date", "收盘": "close", "开盘": "open",
                                    "最高": "high", "最低": "low", "成交量": "volume"})
        else:
            return None, f"未知 source: {source}"
    except Exception as e:
        return None, f"{type(e).__name__}: {str(e)[:160]}"

    if df is None or len(df) == 0:
        return None, "通道返回空数据"

    # 新浪把 date 放在列里、index 是位置索引；东财同理
    if "date" in df.columns:
        df = df.copy()
        df["date"] = pd.to_datetime(df["date"])
        df = df.set_index("date").sort_index()
    elif not isinstance(df.index, pd.DatetimeIndex):
        df = df.copy()
        df.index = pd.to_datetime(df.index)
        df = df.sort_index()
    return df, None


def main(argv=None):
    ap = argparse.ArgumentParser(description="跨行情通道的 qfq 数值一致性核查")
    ap.add_argument("symbol", nargs="?", default="600519")
    ap.add_argument("--source", default="sina", choices=["sina", "eastmoney"],
                    help="要与缓存比对的外部通道（默认 sina）")
    ap.add_argument("--tol", type=float, default=1e-6, help="绝对差容差，默认 1e-6")
    a = ap.parse_args(argv)

    cache, path = load_cache(a.symbol)
    if cache is None:
        print(f"ERROR 缓存不存在: {path}", file=sys.stderr)
        print("请先运行: python tools/warm_cache.py", file=sys.stderr)
        return 2
    print(f"缓存: {len(cache)} 行  {cache.index[0].date()} ~ {cache.index[-1].date()}")
    print(f"       {path}")

    fresh, err = fetch_via(a.symbol, a.source)
    if fresh is None:
        print(f"ERROR 通道 {a.source} 不可用: {err}", file=sys.stderr)
        print("（这不影响程序正确性；换 --source 或换网络再试）", file=sys.stderr)
        return 2
    print(f"{a.source}: {len(fresh)} 行  {fresh.index[0].date()} ~ {fresh.index[-1].date()}")

    common = cache.index.intersection(fresh.index)
    print(f"\n重叠交易日: {len(common)}")
    if len(common) == 0:
        print("ERROR 无重叠交易日，无法比对（检查 --start 或数据源覆盖区间）", file=sys.stderr)
        return 2

    all_ok = True
    worst_samples = []
    for col in COLS:
        if col not in fresh.columns:
            print(f"  {col:8}: 外部通道缺该列，跳过")
            continue
        x = cache.loc[common, col].astype(float)
        y = fresh.loc[common, col].astype(float)
        diff = (x - y).abs()
        n_bad = int((diff > a.tol).sum())
        if n_bad:
            all_ok = False
            for dt in diff.nlargest(3).index:
                worst_samples.append((col, dt.date(), float(x[dt]), float(y[dt])))
        print(f"  {col:8}: 最大绝对差={diff.max():.6f}  超差点数={n_bad}/{len(common)}  "
              f"{'一致' if n_bad == 0 else '<<< 不一致'}")

    if all_ok:
        print(f"\n一致（容差 {a.tol:g}）→ 跨通道/跨版本数字可以对比")
        return 0

    print(f"\n存在超差（容差 {a.tol:g}）→ 跨版本对比需谨慎。最大差异样本：")
    for col, dt, xv, yv in worst_samples:
        print(f"  {dt}  {col:6}  缓存={xv:.4f}  外部={yv:.4f}  差{xv - yv:+.4f}")
    print("\n建议：在同一通道下重跑基线，或把本差异写入验证快照后再对比。")
    return 1


if __name__ == "__main__":
    sys.exit(main())
