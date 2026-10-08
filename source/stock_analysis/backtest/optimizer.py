# -*- coding: utf-8 -*-
"""P3: 贝叶斯优化（轻量）替代全网格。

无重依赖实现：
- 全网格575组评估约40s；bayes模式默认30次评估，~3s，适合快速验证/参数微调。
- 策略：首轮均匀随机探索 iters//2；次轮围绕当前最优同family局部开采+剩余随机探索，
  用OOS评分（与锦标赛一致）选优。本质为随机搜索+爬山，可视为无高斯过程的轻量贝叶斯。
"""
from __future__ import annotations
import random
from typing import Dict, Tuple
import pandas as pd

from stock_analysis.backtest.engine import BacktestConfig, BacktestEngine, BacktestResult
from stock_analysis.backtest.analyzer import (
    split_train_test, split_regimes, score_oos_result, adaptive_oos_config,
    calc_buy_hold_baseline,
)


def _family_of(name: str) -> str:
    for sep in ["(", "（"]:
        if sep in name:
            return name.split(sep)[0]
    return name


def bayes_search(
    df: pd.DataFrame,
    market_regimes: pd.Series = None,
    config: BacktestConfig = None,
    iters: int = 30,
    seed: int = 7,
    jobs: int = 1,
) -> Tuple[str, BacktestResult, pd.DataFrame]:
    from stock_analysis.strategy.registry import build_strategy_signals
    rng = random.Random(seed)
    grid = build_strategy_signals()
    names = list(grid.keys())
    fam_map: Dict[str, list] = {}
    for n in names:
        fam_map.setdefault(_family_of(n), []).append(n)

    n_explore = max(5, iters // 2)
    evaluated: Dict[str, float] = {}
    eval_trades: Dict[str, int] = {}
    oos_cfg = adaptive_oos_config(df, config)
    eng_is = BacktestEngine(config)
    eng_oos = BacktestEngine(oos_cfg)
    train_df, test_df = split_train_test(df, 0.7)
    _tr_reg, test_reg = split_regimes(market_regimes, train_df, test_df)

    def _eval_oos(name: str) -> tuple:
        sig = grid[name]
        r = eng_oos.run_backtest(test_df, sig, test_reg)
        return score_oos_result(r), r

    # 轮1：按family分层探索（各family至少覆盖1个，避免30次随机全落在冷门family）
    fams = list(fam_map.keys())
    rng.shuffle(fams)
    cand: list = []
    i = 0
    while len(cand) < min(n_explore, len(names)) and fams:
        fam = fams[i % len(fams)]
        members = [x for x in fam_map[fam] if x not in cand]
        if members:
            cand.append(rng.choice(members))
        i += 1
        if i > len(names) * 2:
            break
    for n in cand:
        s, r_ = _eval_oos(n)
        evaluated[n] = s
        eval_trades[n] = int(r_.trade_count)
    # 轮2：开采最优family + 探索
    for _ in range(max(0, iters - n_explore)):
        if evaluated:
            best_so = max(evaluated, key=evaluated.get)
            fam = _family_of(best_so)
            pool = [x for x in fam_map.get(fam, []) if x not in evaluated]
            if pool and rng.random() < 0.7:
                nxt = rng.choice(pool)
            else:
                rest = [x for x in names if x not in evaluated]
                nxt = rng.choice(rest) if rest else rng.choice(names)
        else:
            nxt = rng.choice(names)
        s, r_ = _eval_oos(nxt)
        evaluated[nxt] = s
        eval_trades[nxt] = int(r_.trade_count)

    # 回退：若OOS全-100（小样本全灭），按全量IS在已评估集合内重排，避免任意返回首个
    if evaluated and max(evaluated.values()) <= -100:
        from stock_analysis.backtest.analyzer import score_backtest_result
        is_scores: Dict[str, float] = {}
        for n in evaluated:
            try:
                r_is = eng_is.run_backtest(df, grid[n], market_regimes)
                is_scores[n] = score_backtest_result(r_is)
            except Exception:
                is_scores[n] = -100.0
        best_name = max(is_scores, key=is_scores.get)
        evaluated = is_scores
    else:
        best_name = max(evaluated, key=evaluated.get)
    best_result = eng_is.run_backtest(df, grid[best_name], market_regimes)
    rows = [{"策略": n, "评分OOS": s, "交易数_OOS": int(eval_trades.get(n, 0))}
            for n, s in sorted(evaluated.items(), key=lambda kv: kv[1], reverse=True)]
    comp = pd.DataFrame(rows).set_index("策略")
    base = calc_buy_hold_baseline(df, config)
    if evaluated[best_name] <= -100 or (best_result.annual_return <= 0 and best_result.annual_return <= base["annual_return"]):
        best_name = best_name + "（无优势，建议观望）"
    return best_name, best_result, comp


def run_bayes_tournament(code: str, start_date: str, use_regime: bool, detail: bool, iters: int = 30, jobs: int = 1) -> None:
    from stock_analysis.data_loader import get_stock_daily, get_stock_name
    from stock_analysis.strategy_master.config import get_market_regime_series
    import cli as _cli
    name = get_stock_name(code) or code
    print(f"\n=== 股票(贝叶斯优化): {name} ({code}) ===")
    df = get_stock_daily(code, start_date=start_date, source="sina")
    if df is None or len(df) < 100:
        print(f"数据不足（{len(df) if df is not None else 0} 条），至少需要 100 条")
        return
    print(f"数据区间: {df.index[0].date()} ~ {df.index[-1].date()}（{len(df)} 条）")
    regimes = None
    if use_regime:
        regimes = get_market_regime_series(start_date)
    print(f"贝叶斯寻优 iters={iters}（替代575组全网格）...\n")
    from stock_analysis.backtest.engine import BacktestConfig
    config = BacktestConfig(max_position_pct=1.0)
    best_name, best_result, comp = bayes_search(df, regimes, config, iters=iters, jobs=jobs)
    r = best_result
    print(f">>> 最优策略: {best_name}")
    print(f"    年化收益  : {r.annual_return:.2%}  最大回撤: {r.max_drawdown:.2%}  交易次数: {r.trade_count}")
    lookup = best_name.replace("（无优势，建议观望）", "")
    try:
        from stock_analysis.strategy.registry import build_strategy_instances
        insts = build_strategy_instances()
        if lookup in insts:
            _cli._print_detailed_stock_advice(insts[lookup], df, lookup)
    except Exception as e:
        print(f"建议生成失败: {e}")
    if detail:
        print("\n=== 贝叶斯评估Top ===")
        print(comp.head(20).to_string())
