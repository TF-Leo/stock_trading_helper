# -*- coding: utf-8 -*-
"""Hermes Decision Engine CLI - Unified trigger-based output"""
import argparse
import json
import sys
import time
from datetime import datetime

import pandas as pd
import numpy as np

from stock_analysis.data_loader import get_stock_daily, get_fund_daily, get_stock_name
from stock_analysis.data_loader.meta import last_fetch_info
from stock_analysis.backtest.analyzer import run_strategy_tournament, calc_buy_hold_baseline
from stock_analysis.backtest.engine import BacktestConfig
from stock_analysis.strategy.registry import (
    build_strategy_signals, family_map as stock_family_map,
)
from fund_analysis.fund_strategy.registry import (
    build_fund_strategy_signals, grid_size, family_map as fund_family_map,
)
from decision.instrument import classify_board
from decision.schema import EXIT_CODES, exit_code_for  # noqa: F401
from decision import sizing
from decision.scoring import (  # noqa: F401
    risk_adjusted_score,
    deflated_sharpe,
    deflated_sharpe_from_annual,
    bootstrap_ci,
    equity_to_returns,
    excess_return_ci,
    metric_with_ci,
    stat_annual_return,
    stat_sharpe,
    stat_max_drawdown,
    stat_calmar,
    stat_win_rate,
    stat_profit_factor,
)
from decision.consensus import select_consensus
from decision.triggers import evaluate_triggers, format_trigger_message

# ---------------------------------------------------------------------------
# tests/ 契约入口（source/tests/test_p2_engineering.py::test_cli_offline_import_and_fund_split）
# 基金回测执行层已拆至 fund_analysis/fund_backtest.py，此处保留同名 re-export。
# ---------------------------------------------------------------------------
from fund_analysis.fund_backtest import (  # noqa: F401
    FUND_FEE_RATE,
    _fund_backtest_signals,
    _fund_backtest_with_stats,
)


def _build_stock_strategies():
    """P1 契约：单一策略网格真源（tests/test_p1_oos.py::test_registry_single_source）。

    必须与 stock_analysis.backtest.analyzer._build_strategy_grid() 键集合一致且为 581 组。
    """
    return build_strategy_signals()


def _jsonable(obj):
    """C1 契约：递归把 numpy 标量/数组转换为原生 Python 类型。

    修复前 `python cli.py 600519 --json` 会抛
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
    except:
        return 0.0


# C3：响应构造已迁至 decision/schema.py（契约 D4 要求 cli.py ≤ 700 行）。
# 错误响应与成功响应必须**同构**（不适用处显式 null），属 schema 职责。
# 保留同名绑定，避免本文件其余调用点与自检逻辑改签名。
from decision.schema import (  # noqa: E402
    METRIC_KEYS,
    create_error_response,
)
from decision.schema import empty_metrics as _empty_metrics  # noqa: E402


def _ci_block(values, stat_fn, point, n_boot):
    """F10: 组装 {'point', 'ci_95'}；CI 覆盖点估计，样本不足时 ci_95 为 None。"""
    try:
        point = float(point)
    except (TypeError, ValueError):
        point = 0.0
    if not np.isfinite(point):
        # 非有限值（如零亏损时的 profit_factor=inf）不得进 JSON：显式 null
        return {'point': None, 'ci_95': None}
    out = metric_with_ci(values, stat_fn, n_boot=n_boot)
    ci = out.get('ci_95')
    if ci:
        ci = [round(min(ci[0], point), 6), round(max(ci[1], point), 6)]
    return {'point': round(point, 6), 'ci_95': ci}


def _stability_score(consensus):
    """F06h: confidence 的 0.2 项必须来自数据，不能是常数 0.8。

    用候选评分的相对离散度衡量"共识有多稳"：离散度越小越稳。
    """
    members = consensus.get('members') or []
    if not members:
        return 0.0
    disp = float(consensus.get('dispersion') or 0.0)
    mean_abs = sum(abs(float(m.get('score', 0) or 0)) for m in members) / len(members)
    if mean_abs <= 1e-12:
        return 0.0
    return round(max(0.0, min(1.0, 1.0 - disp / mean_abs)), 4)


def _excess_vs_benchmark(result, df, config, fast=False):
    """B8: 策略相对"买入持有"的超额收益 bootstrap 95% CI。

    历史问题：这里曾是硬编码 {'point': 0.05, 'ci_95': [0.02, 0.08]}，
    与真实数据无关，no_edge 判定因此没有统计依据。

    做法：
      1. 策略权益曲线取 result.equity_curve（B7 已在引擎侧暴露）；
      2. 基准权益曲线按"首日以收盘价全仓买入并持有"重建，含单边成本近似；
      3. 对逐期超额收益做配对 bootstrap，给出点估计、2.5%/97.5% 分位与 p_positive。
    """
    empty = {'point': 0.0, 'ci_95': [0.0, 0.0], 'p_positive': 0.0, 'n_obs': 0}
    try:
        eq = list(getattr(result, 'equity_curve', None) or [])
        if len(eq) < 3 or df is None or len(df) < 3:
            return empty
        close = df['close'].astype(float).to_numpy()
        # 与策略同长度对齐：策略权益从初始资金起步
        n = min(len(eq), len(close))
        if n < 3:
            return empty
        eq = eq[:n]
        close = close[:n]
        initial = float(eq[0]) if eq[0] > 0 else float(config.initial_capital)
        bh = close / close[0] * initial
        ci = excess_return_ci(eq, list(bh), n_boot=(400 if fast else 2000))
        return {
            'point': ci['point'],
            'ci_95': ci['ci_95'],
            'p_positive': ci['p_positive'],
            'n_obs': ci['n_obs'],
            'benchmark': 'buy_and_hold_close',
        }
    except Exception:
        return empty

def _market_regime(df):
    """把"当前市场环境"从占位常量改为基于真实行情的判定。

    历史问题：这里曾硬编码 {'label': 'Bull', 'confidence': 0.7}，
    无论行情如何都报牛市，与 C4（输出不得自相矛盾）冲突。

    直接复用 advisor.analyzer.get_current_market_regime（A8 单一 regime 真源），
    避免再造第二套标签。失败时显式返回 Unknown 而不是假装 Bull。
    """
    try:
        from stock_analysis.advisor.analyzer import get_current_market_regime
        label, conf = get_current_market_regime(df)
        return {
            'label': str(label),
            'confidence': round(float(conf), 3),
            'as_of': str(df.index[-1]),
            'definition': 'ma20_ma60_return_volatility',
        }
    except Exception:
        return {
            'label': 'Unknown',
            'confidence': 0.0,
            'as_of': str(df.index[-1]) if df is not None and len(df) else '',
            'definition': 'unavailable',
        }

def _build_metrics(result, fast=False):
    """F10/B7: evidence.metrics 逐项填 point + bootstrap 95% CI。

    数据源：result.equity_curve（收益类）与 result.trades（逐笔类）。
    计数型指标（trade_count）与状态型指标（time_in_market）不适用 bootstrap，ci_95 显式 None。
    """
    rets = equity_to_returns(list(getattr(result, 'equity_curve', None) or []))
    trades = list(getattr(result, 'trades', None) or [])
    pnl = [float(t.pnl_pct) for t in trades]
    hold = [float(t.holding_days) for t in trades]
    nb = 400 if fast else 2000
    block = {
        'annual_return': _ci_block(rets, stat_annual_return, getattr(result, 'annual_return', 0), nb),
        'max_drawdown': _ci_block(rets, stat_max_drawdown, getattr(result, 'max_drawdown', 0), nb),
        'win_rate': _ci_block(pnl, stat_win_rate, getattr(result, 'win_rate', 0), nb),
        'sharpe': _ci_block(rets, stat_sharpe, getattr(result, 'sharpe_ratio', 0), nb),
        'calmar': _ci_block(rets, stat_calmar, getattr(result, 'calmar_ratio', 0), nb),
        'profit_factor': _ci_block(pnl, stat_profit_factor, getattr(result, 'profit_factor', 0), nb),
        'trade_count': {'point': int(getattr(result, 'trade_count', 0) or 0), 'ci_95': None},
        'avg_holding_days': _ci_block(hold, lambda v: float(np.mean(v)),
                                      getattr(result, 'avg_holding_days', 0), nb),
        'time_in_market': {'point': round(float(getattr(result, 'time_in_market', 0.0) or 0.0), 6),
                           'ci_95': None},
    }
    # C3/G35: 与 _empty_metrics() 的键集合必须完全一致，否则成功路径与错误路径再次不同构。
    # 只在开发者改了其中一侧时才会触发，属自检而非业务校验。
    if set(block) != set(METRIC_KEYS):
        raise RuntimeError(
            'evidence.metrics 键集合漂移（C3 同构被破坏）：'
            f'仅在一侧存在 {sorted(set(block) ^ set(METRIC_KEYS))}'
        )
    return block


def advise_internal(code, type_hint='auto', start=None, opt='grid', jobs=1,
                    fast=False, detail=False, no_regime=False, opt_iters=30):
    """核心建议生成逻辑（供 CLI 和 API 共用）"""
    start_time = time.time()
    warnings = []

    # 类型判定与取数统一委托数据层 resolve_and_fetch()：
    #   旧逻辑 `data_type = 'fund' if code.startswith('5') else 'stock'` 把场外基金
    #   （023298/006195/001917/001564）判成股票 → data_source_error。
    #   但 0/1 段不能一律当基金（002270/002415 这些真股票也在基金名录里），
    #   故改为双向探测，细节见 data_loader.meta.resolve_and_fetch 的 docstring。
    from stock_analysis.data_loader.meta import resolve_and_fetch
    df, data_type, type_err = resolve_and_fetch(code, type_hint=type_hint, start_date=start)
    if df is None and type_err:
        return create_error_response(
            'data_source_error', type_err, code, data_type, type_hint,
            fetch_info=last_fetch_info(), elapsed=time.time() - start_time,
            requested_start=start)

    # F05: 拉取失败(30) 与 数据太短(20) 是两种状态，不得都压成 10
    if df is None:
        return create_error_response(
            'data_source_error', "行情拉取失败（渠道池全失败且无可用缓存）",
            code, data_type, type_hint,
            fetch_info=last_fetch_info(), elapsed=time.time() - start_time,
            requested_start=start)
    if len(df) < 100:
        # C3: 数据太短时 bars/start/end/last_close 都是已知真实值，必须如实填
        return create_error_response(
            'insufficient_data', f"数据不足（仅 {len(df)} 根 < 100）",
            code, data_type, type_hint,
            bars=len(df), start=df.index[0], end=df.index[-1],
            last_close=float(df['close'].iloc[-1]),
            fetch_info=last_fetch_info(), elapsed=time.time() - start_time,
            requested_start=start)

    fetch_info = last_fetch_info()
    last_close = float(df['close'].iloc[-1])

    # G33: 数据降级（触网失败→回退旧缓存）必须对调用方可见。
    # 旧实现里 JSON 只有 from_cache=True，Hermes 无法区分"新鲜命中"与"兜底旧数据"。
    if fetch_info.get('degraded'):
        warnings.append(
            f"数据降级：{fetch_info.get('degraded_reason')}；"
            f"缓存末日 {fetch_info.get('cache_last_date')}，"
            f"滞后 {fetch_info.get('lag_days')} 个交易日"
            f"（期望最新交易日 {fetch_info.get('expected_date')}），结论基于陈旧数据")

    # 标的信息
    board, price_limit_pct, is_st = classify_board(code)
    name = get_stock_name(code) if data_type == 'stock' else code

    # 运行回测
    strategies = build_strategy_signals() if data_type == 'stock' else build_fund_strategy_signals()
    fam_map = stock_family_map() if data_type == 'stock' else fund_family_map()
    config = BacktestConfig(initial_capital=100000, max_position_pct=1.0)
    iters = min(int(opt_iters), 12) if fast else int(opt_iters)

    # A8/F06g: 分市况胜率需要真实 regime 序列（与 market_regime 共用同一判定规则）
    market_regimes = None
    if not no_regime:
        try:
            from stock_analysis.advisor.analyzer import market_regime_series
            market_regimes = market_regime_series(df)
        except Exception:
            market_regimes = None

    try:
        if opt == 'bayes':
            from stock_analysis.backtest.optimizer import bayes_search
            best_name, result, comparison = bayes_search(
                df, market_regimes, config, iters=iters, jobs=jobs, strategies=strategies,
            )
            # F03: bayes 的试验次数 = 实际评估次数（候选表行数），不是全网格 581
            n_trials = max(len(comparison), 1)
        else:
            best_name, result, comparison = run_strategy_tournament(
                df, market_regimes, config=config, jobs=jobs, strategies=strategies,
                n_splits=2 if fast else 3,
            )
            # F03: n_trials = 本次实际评分过的配置数（多重检验校正的分母）
            n_trials = max(len(strategies), len(comparison), 1)
    except Exception as e:
        return create_error_response(
            'data_source_error', f"回测失败：{e}", code, data_type, type_hint,
            bars=len(df), start=df.index[0], end=df.index[-1],
            last_close=last_close, fetch_info=fetch_info,
            elapsed=time.time() - start_time, requested_start=start)

    # 计算共识 - Fast mode: reduce top_k
    n_bars = len(df)
    # F13: 候选池必须**大于**对外 Top-K，否则 B5 的"任一族 ≤50%"无解 ——
    # 池子只有 10 个、其中 9 个同族时，怎么选都会超 50%。先取池（fast 20 / 全量 40），
    # 再由 select_consensus 按族上限挑出对外 10 个。
    pool_k = min(20, len(comparison)) if fast else min(40, len(comparison))
    candidates = []
    for idx in comparison.sort_values('评分', ascending=False).index[:pool_k]:
        row = comparison.loc[idx]
        score = float(row.get('评分', 0))
        annual_ret = parse_pct(row.get('年化收益', '0%'))
        max_dd = parse_pct(row.get('最大回撤', '0%'))
        win_rate = parse_pct(row.get('胜率', '0%'))
        trades = int(row.get('交易次数', 0))

        # F14: 族必须取自注册表 prefix（禁止按 '(' 切分推断）
        family = fam_map.get(idx, 'unknown')

        # F06a: 真实样本外评分（此前是 score * 0.9 的伪造值）
        raw_oos = row.get('评分OOS', None)
        try:
            score_oos = None if raw_oos is None or pd.isna(raw_oos) else round(float(raw_oos), 4)
        except (TypeError, ValueError):
            score_oos = None
        if score_oos is None:
            warnings.append(f"候选 {idx} 缺样本外评分（评分OOS），score_oos 置 null")

        candidates.append({
            'name': idx,
            'family': family,
            'score': score,
            'score_is': float(row.get('评分IS', score)),
            'score_oos': score_oos,
            'annual_return': annual_ret,
            'max_drawdown': max_dd,
            'win_rate': win_rate,
            'trade_count': trades,
            'calmar': annual_ret / abs(max_dd) if max_dd != 0 else 0,
            # B4: 逐候选 DSR（用它自己的样本量 + 真实试验次数）
            'deflated_sharpe': round(
                deflated_sharpe_from_annual(
                    (annual_ret / max(abs(max_dd), 1e-9)) if max_dd != 0 else 0.0,
                    n_trials,
                    n_obs=max(int(n_bars), 3),
                ), 4,
            )
        })

    # B6: 主导族必须对窗口扰动稳健 —— 用"族内**全部配置**评分的中位数"（基于完整 comparison 表），
    # 而不是候选计数或族内 Top-3 均值（后者会被一两个幸运参数拉高并在窗口间翻转）。
    _fam_buckets = {}
    for _name, _row in comparison.iterrows():
        _fam = fam_map.get(_name, 'unknown')
        _fam_buckets.setdefault(_fam, []).append(float(_row.get('评分', -100)))
    family_scores = {f: float(np.median(v)) for f, v in _fam_buckets.items() if v}

    consensus = select_consensus(candidates, top_k=min(10, len(candidates)),
                                 family_scores=family_scores)
    # F13: 对外 candidates 与 consensus.members 同源，B5 的"任一族 ≤ 50%"才真正生效
    final_candidates = list(consensus.get('members') or []) or candidates[:10]

    # 评估触发器
    if best_name in strategies:
        signals = strategies[best_name](df)
    else:
        # F01: 回查失败必须显式告警，不得静默降级为全 False 信号
        signals = {'buy': pd.Series(False, index=df.index),
                   'sell': pd.Series(False, index=df.index)}
        warnings.append(f"最优策略 '{best_name}' 不在策略表中，信号已降级为空（触发器不可信）")
    triggers = evaluate_triggers(df, {best_name: signals})
    trigger_msg = format_trigger_message(triggers, 'hold')

    # 计算置信度 - Fast mode: reduce bootstrap iterations
    # B4: 真实 Deflated Sharpe（同频口径）；F03: 分母用真实试验次数
    _n_obs = len(result.equity_curve) if getattr(result, 'equity_curve', None) else 0
    if _n_obs >= 3:
        dsr = deflated_sharpe_from_annual(
            result.sharpe_ratio if hasattr(result, 'sharpe_ratio') else 0,
            n_trials,
            n_obs=_n_obs,
        )
    else:
        dsr = 0.0
    stability_score = _stability_score(consensus)
    confidence = min(1.0, max(0.0, 0.4 * max(0.0, min(1.0, dsr))
                              + 0.3 * float(consensus.get('agreement') or 0.0)
                              + 0.2 * stability_score
                              + 0.1 * min(result.trade_count / 60, 1)))

    # F04/B8: 超额收益 CI 只算一次（避免 bootstrap 跑两遍拖慢 G12/G14）
    exc = _excess_vs_benchmark(result, df, config, fast)
    exc_ci_hi = float((exc.get('ci_95') or [0.0, 0.0])[1])
    exc_n = int(exc.get('n_obs') or 0)

    # T1/D4：status / action / significant 由候选有效性 + 触发器 + 显著性推导，保证输出自洽
    top_score = final_candidates[0]['score'] if final_candidates else -100.0
    # 双重判定：候选自身有效 + 共识层没有"全员无效"标记（B5 过滤后的结果）
    top_valid = (top_score > -100) and not bool(consensus.get('all_invalid'))
    # F04: significant 必须同时满足 DSR>0 与超额收益 CI 上界>0
    significant = bool(dsr > 0 and exc_ci_hi > 0)
    # 持续条件：最近 15 根 K 线"全部"同向才算强触发，避免瞬时单根信号在三窗口（full/drop60/drop120）间翻转 —— G06 稳定的前提
    _n_sig = len(signals['buy'])
    _lookback = min(15, _n_sig)
    has_buy_trigger = (_n_sig >= 15) and bool(signals['buy'].iloc[-_lookback:].all())
    has_sell_trigger = (_n_sig >= 15) and bool(signals['sell'].iloc[-_lookback:].all())

    # F05/A3: 出现"一手也买不起"事件 → untradeable（退出码 40 才可达）
    if getattr(result, 'unaffordable_lot_events', 0) > 0:
        status = 'untradeable'
        warnings.append(
            f"存在 {int(result.unaffordable_lot_events)} 次'一手也买不起'事件，回测结果不可信（A3）")
    elif not top_valid:
        status = 'no_edge'
    elif exc_n >= 3 and exc_ci_hi <= 0:
        # F04/B8: 超额收益 CI 上界 ≤ 0 → 统计上无优势
        status = 'no_edge'
        warnings.append("超额收益 95% CI 上界 ≤ 0：统计上无优势，判 no_edge（B8）")
    else:
        status = 'ok'

    if status in ('no_edge', 'untradeable'):
        action = 'avoid'          # 契约 status→action 映射：no_edge/untradeable → avoid
    elif top_valid and significant and confidence > 0 and has_buy_trigger:
        action = 'buy'
    elif top_valid and has_sell_trigger:
        action = 'sell'
    else:
        action = 'hold'

    # F12: 止损/止盈/入场区/仓位/horizon 全部由 decision.sizing 推导（cli 不再写字面量）
    atr_val = None
    try:
        if {'high', 'low', 'close'}.issubset(set(df.columns)):
            from stock_analysis.indicators.volatility import calc_atr
            _atr_series = calc_atr(df['high'], df['low'], df['close'], period=14)
            _atr_last = float(_atr_series.iloc[-1])
            if _atr_last == _atr_last and _atr_last > 0:
                atr_val = _atr_last
    except Exception:
        atr_val = None
    if atr_val is None:
        warnings.append("无可用 ATR（缺少 OHLC 数据），止损/止盈回落固定百分比（method=fixed）")

    stops = sizing.derive_stops(last_close, atr_val, config)
    stop_distance = float(stops['stop_distance'])
    trig0 = triggers[0] if triggers else {}
    buy_px = trig0.get('buy_price')
    sell_px = trig0.get('sell_price')
    entry_zone = None if action == 'sell' else sizing.derive_entry_zone(last_close, atr_val, buy_px)
    horizon = sizing.derive_horizon(getattr(result, 'avg_holding_days', 0))
    if action in ('avoid', 'insufficient_data'):
        position_fraction = 0.0
    else:
        position_fraction = sizing.derive_position_fraction(confidence, stop_distance)
    dist = sizing.distance_to_trigger(last_close, buy_px, sell_px)
    invalidation = sizing.build_invalidation(
        stops['stop_loss'], consensus.get('family'), horizon[1])

    # F06e: 告警必须由真实事实触发
    if getattr(result, 'blocked_by_limit_events', 0) > 0:
        warnings.append(f"存在 {int(result.blocked_by_limit_events)} 次涨跌停拦截事件")
    if not triggers:
        warnings.append("触发器为空：策略元信息不可用，action 仅由评分/共识推导")
    if exc_n < 3:
        warnings.append(f"超额收益样本不足（n_obs={exc_n}），CI 退化不可用于显著性判断")
    if n_trials <= 1:
        warnings.append("n_trials<=1，Deflated Sharpe 未做多重检验校正")

    # 构建响应
    response = {
        'schema_version': '1.0',
        'generated_at': datetime.now().isoformat(),
        'request': {
            'code': code,
            'type': data_type,
            'type_hint': type_hint,
            'start': start,
            'opt': opt,
            'jobs': jobs,
            'fast': fast
        },
        'instrument': {
            'code': code,
            'name': name,
            'board': board,
            'price_limit_pct': price_limit_pct,
            'is_st': is_st
        },
        'data': {
            'bars': len(df),
            'start': str(df.index[0]),
            'end': str(df.index[-1]),
            'last_close': last_close,
            # F06f: 真实取数来源（此前硬编码 'sina' / True）
            'source': fetch_info.get('source'),
            'from_cache': fetch_info.get('from_cache'),
            # G33: 降级可见性 —— stale=True 表示本次结论基于"拉取失败后回退的旧缓存"
            'stale': bool(fetch_info.get('degraded')),
            'cache_last_date': fetch_info.get('cache_last_date'),
            'lag_days': fetch_info.get('lag_days'),
            'expected_date': fetch_info.get('expected_date'),
            'requested_source': fetch_info.get('requested_source'),
            'window_note': (f"--start {start} 截取后 {len(df)} 根" if start else None)
        },
        'market_regime': ({'label': 'Unknown', 'confidence': 0.0,
                           'as_of': str(df.index[-1]), 'definition': 'disabled'}
                          if no_regime else _market_regime(df)),
        'recommendation': {
            'status': status,
            'action': action,
            'action_reason': [trigger_msg],
            'confidence': round(confidence, 3),
            'position_fraction': round(position_fraction, 3),
            'horizon_days': horizon,
            'entry_zone': entry_zone,
            'stop_loss': stops['stop_loss'],
            'take_profit': stops['take_profit'],
            'stop_method': stops['method'],
            'distance_to_trigger': dist,
            'invalidation': invalidation,
            'no_edge': status == 'no_edge'
        },
        'evidence': {
            'consensus': {
                'family': consensus['family'],
                'agreement': round(consensus['agreement'], 3),
                'members': [c['name'] for c in final_candidates]
            },
            'dispersion': round(consensus['dispersion'], 3),
            'stability': consensus['stability'],
            'n_trials': int(n_trials),
            'n_survivors': int((comparison['交易次数'].astype(int) > 5).sum()),
            'deflated_sharpe': round(dsr, 3),
            'significant': significant,
            'metrics': _build_metrics(result, fast),
            'excess_vs_buy_hold': exc,
            'buy_hold_baseline': calc_buy_hold_baseline(df, config),
            # F06g: 分市况胜率（此前是空占位 {}）
            'regime_win_rates': {
                'Bull': round(float(getattr(result, 'bull_win_rate', 0) or 0), 4),
                'Bear': round(float(getattr(result, 'bear_win_rate', 0) or 0), 4),
                'Shock': round(float(getattr(result, 'shock_win_rate', 0) or 0), 4),
            },
            'triggers': triggers[:5]
        },
        'candidates': final_candidates,
        'diagnostics': {
            'unaffordable_lot_events': getattr(result, 'unaffordable_lot_events', 0),
            'blocked_by_limit_events': getattr(result, 'blocked_by_limit_events', 0),
            # F06d: 真实零交易组数（此前硬编码 0）
            'zero_trade_configs': int((comparison['交易次数'].astype(int) == 0).sum()),
            'survivor_configs': int((comparison['评分'] > -100).sum()),
            'warnings': warnings,
            'elapsed_seconds': round(time.time() - start_time, 2)
        },
        'disclaimer': '本结果不构成投资建议，市场有风险，投资需谨慎。'
    }
    
    return _jsonable(response)


def build_parser():
    """D6: CLI 参数定义（probe 会直接复用本函数验证"旧参数不报 usage error"）。"""
    parser = argparse.ArgumentParser(description='Hermes Trading Decision Engine')
    parser.add_argument('code', help='股票代码或基金代码')
    parser.add_argument('--json', action='store_true', help='输出 JSON 格式（stdout 只输出单个 JSON 文档）')
    parser.add_argument('--fast', action='store_true', help='快速模式（缩减 bootstrap / 候选池 / walk-forward 折数）')
    parser.add_argument('--start', default=None, help='开始日期，如 20200101')
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
    return parser


def _print_detail(response):
    rec = response['recommendation']
    ev = response['evidence']
    print(f"\n--- 详细 ---")
    print(f"止损/止盈方式：{rec.get('stop_method')}")
    print(f"入场区：{rec.get('entry_zone')}  距触发：{rec.get('distance_to_trigger')}")
    print(f"horizon：{rec.get('horizon_days')} 天  失效条件：{rec.get('invalidation')}")
    print(f"市场环境：{response.get('market_regime', {}).get('label')}"
          f"（{response.get('market_regime', {}).get('definition')}）")
    print(f"n_trials={ev.get('n_trials')}  DSR={ev.get('deflated_sharpe')}  "
          f"significant={ev.get('significant')}")
    exc = ev.get('excess_vs_buy_hold') or {}
    print(f"超额收益：point={exc.get('point')} ci_95={exc.get('ci_95')} "
          f"p_positive={exc.get('p_positive')} n_obs={exc.get('n_obs')}")
    print("候选（族 / 评分 / 年化 / 回撤 / 交易数）：")
    for c in response.get('candidates') or []:
        print(f"  - {c['name']:<28} {c['family']:<10} {c['score']:>8.4f} "
              f"{c['annual_return']:>8.2%} {c['max_drawdown']:>8.2%} {c['trade_count']:>5d}")
    print("触发器：")
    for t in ev.get('triggers') or []:
        print(f"  - {t['indicator']}: state={t['state']} current={t['current']} "
              f"buy_price={t['buy_price']} sell_price={t['sell_price']}")
    warns = (response.get('diagnostics') or {}).get('warnings') or []
    if warns:
        print("告警：")
        for w in warns:
            print(f"  [WARN] {w}")


def main():
    parser = build_parser()
    args = parser.parse_args()

    try:
        response = advise_internal(
            args.code,
            type_hint=args.type,
            start=args.start,
            opt=args.opt,
            jobs=args.jobs,
            fast=args.fast,
            detail=args.detail,
            no_regime=args.no_regime,
            opt_iters=args.opt_iters,
        )
        rec = response['recommendation']

        if args.json:
            print(json.dumps(response, ensure_ascii=False, indent=2))
        else:
            print(f"\n=== {args.code} 交易建议 ===")
            print(f"状态：{rec['status']}")
            print(f"动作：{rec['action']}")
            print(f"置信度：{rec['confidence']:.1%}")
            print(f"建议仓位：{rec['position_fraction']:.1%}")
            print(f"触发信号：{rec['action_reason'][0]}")
            print(f"止损价：{rec['stop_loss']}   止盈价：{rec['take_profit']}")
            if args.detail:
                _print_detail(response)
            print(f"\n免责声明：本结果不构成投资建议。")

        # F05/C5: status -> 退出码一一对应（0/10/20/30/40）
        sys.exit(exit_code_for(rec['status']))

    except SystemExit:
        raise
    except Exception as e:
        print(f"错误：{e}", file=sys.stderr)
        sys.exit(30)


if __name__ == '__main__':
    main()
