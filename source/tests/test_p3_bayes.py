# -*- coding: utf-8 -*-
"""P3贝叶斯优化回归测试：快速+确定性+有建议输出。"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import pandas as pd
import numpy as np


def _toy(n=300, seed=9):
    rng = np.random.default_rng(seed)
    c = 100 * np.cumprod(1 + rng.normal(0.001, 0.02, n))
    idx = pd.date_range("2020-01-01", periods=n, freq="D")
    return pd.DataFrame({"open": c*0.999, "high": c*1.01, "low": c*0.99,
                         "close": c, "volume": 100000}, index=idx)


def test_bayes_fast_and_deterministic():
    import time
    from stock_analysis.backtest.optimizer import bayes_search
    from stock_analysis.backtest.engine import BacktestConfig
    df = _toy(300)
    t0 = time.time()
    b1, r1, c1 = bayes_search(df, None, BacktestConfig(), iters=20, seed=7)
    b2, r2, c2 = bayes_search(df, None, BacktestConfig(), iters=20, seed=7)
    dt = time.time() - t0
    assert b1 == b2, "同seed必须确定性"
    assert len(c1) <= 20 and len(c1) >= 10
    assert r1 is not None and r1.trade_count >= 0
    assert dt < 60, f"20次评估应远快于全网格581组，实际{dt:.1f}s"


def test_bayes_cli_entry_exists():
    """D6: `--opt bayes` 入口必须真实可用。

    说明（F16 同步）：原断言 `inspect.getsource(cli.main)` 里出现 "--opt"/"bayes" 字样，
    这是**源码文本耦合**而不是行为验证 —— argparse 定义已抽到 `cli.build_parser()`，
    该断言会因"代码搬家"而红，而入口其实还在。

    现分两层，都是行为验证：
      1. 真跑 `python cli.py --help`（subprocess），断言退出码 0 且帮助里列出全部旧参数
         —— 证明这些参数在**真实进程**里被 argparse 接受，而不只是"源码里有字符串"；
      2. 进程内检查 `build_parser()` 的 option 集合，覆盖 --help 可能折叠显示的短选项。
    """
    import subprocess
    import types
    sys.modules.setdefault("akshare", types.ModuleType("akshare"))
    import cli

    src_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    help_txt = subprocess.run(
        [sys.executable, "cli.py", "--help"],
        cwd=src_dir, capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=120,
    )
    assert help_txt.returncode == 0, f"--help 退出码 {help_txt.returncode}: {help_txt.stderr[:300]}"
    for flag in ("--opt", "--detail", "--no-regime", "--opt-iters",
                 "--start", "--jobs", "-t", "--json", "--fast"):
        assert flag in help_txt.stdout, f"--help 未列出 {flag}"

    assert callable(cli.build_parser)
    parser = cli.build_parser()
    opts = {a for act in parser._actions for a in act.option_strings}
    for flag in ("--opt", "--detail", "--no-regime", "--opt-iters",
                 "--start", "--jobs", "-t", "--json", "--fast"):
        assert flag in opts, f"缺参数 {flag}"
    from stock_analysis.backtest.optimizer import run_bayes_tournament
    assert callable(run_bayes_tournament)
