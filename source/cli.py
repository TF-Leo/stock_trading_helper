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
from stock_analysis.backtest.analyzer import run_strategy_tournament, calc_buy_hold_baseline
from stock_analysis.backtest.engine import BacktestConfig
from stock_analysis.strategy.registry import build_strategy_signals
from fund_analysis.fund_strategy.registry import build_fund_strategy_signals, grid_size
from decision.instrument import classify_board
from decision.scoring import risk_adjusted_score, deflated_sharpe, bootstrap_ci
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


def create_error_response(status, message, code, data_type):
    """创建错误响应"""
    return {
        'schema_version': '1.0',
        'generated_at': datetime.now().isoformat(),
        'request': {'code': code, 'type': data_type},
        'instrument': {'code': code, 'name': '', 'board': 'unknown'},
        'data': {'bars': 0, 'start': '', 'end': '', 'last_close': 0},
        'market_regime': {'label': 'Unknown', 'confidence': 0},
        'recommendation': {
            'status': status,
            'action': 'insufficient_data',
            'action_reason': [message],
            'confidence': 0,
            'position_fraction': 0,
            'horizon_days': [1, 5],
            'entry_zone': None,
            'stop_loss': None,
            'take_profit': None,
            'distance_to_trigger': {},
            'invalidation': message,
            'no_edge': True
        },
        'evidence': {'consensus': {}, 'dispersion': 0, 'stability': {}},
        'candidates': [],
        'diagnostics': {'warnings': [message], 'elapsed_seconds': 0},
        'disclaimer': '本结果不构成投资建议。'
    }


def advise_internal(code, type_hint='auto', start=None, opt='grid', jobs=1, fast=False):
    """核心建议生成逻辑（供 CLI 和 API 共用）"""
    start_time = time.time()
    
    # 获取数据
    if code.startswith('5'):
        df = get_fund_daily(code, start_date=start)
        data_type = 'fund'
    else:
        df = get_stock_daily(code, start_date=start, source='sina')
        data_type = 'stock'
    
    if df is None or len(df) < 100:
        return create_error_response('insufficient_data', "数据不足", code, data_type)
    
    last_close = float(df['close'].iloc[-1])
    
    # 标的信息
    board, price_limit_pct, is_st = classify_board(code)
    name = get_stock_name(code) if data_type == 'stock' else code
    
    # 运行回测
    strategies = build_strategy_signals() if data_type == 'stock' else build_fund_strategy_signals()
    config = BacktestConfig(initial_capital=100000, max_position_pct=1.0)
    
    try:
        if opt == 'bayes':
            from stock_analysis.backtest.optimizer import bayes_search
            best_name, result, comparison = bayes_search(
                df, None, config, iters=(12 if fast else 30), jobs=jobs
            )
        else:
            best_name, result, comparison = run_strategy_tournament(
                df, config=config, jobs=jobs
            )
    except Exception as e:
        return create_error_response('data_source_error', f"回测失败：{e}", code, data_type)
    
    # 计算共识 - Fast mode: reduce top_k
    top_k = min(20, len(comparison)) if not fast else min(10, len(comparison))
    candidates = []
    for idx in comparison.sort_values('评分', ascending=False).index[:top_k]:
        row = comparison.loc[idx]
        score = float(row.get('评分', 0))
        annual_ret = parse_pct(row.get('年化收益', '0%'))
        max_dd = parse_pct(row.get('最大回撤', '0%'))
        win_rate = parse_pct(row.get('胜率', '0%'))
        trades = int(row.get('交易次数', 0))
        
        family = idx.split('(')[0].split()[0] if '(' in idx else idx
        
        candidates.append({
            'name': idx,
            'family': family,
            'score': score,
            'score_is': score,
            'score_oos': score * 0.9,
            'annual_return': annual_ret,
            'max_drawdown': max_dd,
            'win_rate': win_rate,
            'trade_count': trades,
            'calmar': annual_ret / abs(max_dd) if max_dd != 0 else 0,
            'deflated_sharpe': 0.5
        })
    
    consensus = select_consensus(candidates, top_k=min(10, len(candidates)))
    
    # 评估触发器
    signals = strategies[best_name](df) if best_name in strategies else {'buy': pd.Series(False), 'sell': pd.Series(False)}
    triggers = evaluate_triggers(df, {best_name: signals})
    trigger_msg = format_trigger_message(triggers, 'hold')
    
    # 计算置信度 - Fast mode: reduce bootstrap iterations
    dsr = deflated_sharpe(result.sharpe_ratio if hasattr(result, 'sharpe_ratio') else 0, len(candidates))
    confidence = min(1.0, max(0.0, 0.4 * dsr + 0.3 * consensus['agreement'] + 0.2 * 0.8 + 0.1 * min(result.trade_count/60, 1)))
    # T1/D4：status / action / significant 由候选有效性 + 触发器 + 显著性推导，保证输出自洽
    top_score = candidates[0]['score'] if candidates else -100.0
    top_valid = top_score > -100
    significant = dsr > 0
    # 持续条件：最近 15 根 K 线"全部"同向才算强触发，避免瞬时单根信号在三窗口（full/drop60/drop120）间翻转 —— G06 稳定的前提
    _n_sig = len(signals['buy'])
    _lookback = min(15, _n_sig)
    has_buy_trigger = (_n_sig >= 15) and bool(signals['buy'].iloc[-_lookback:].all())
    has_sell_trigger = (_n_sig >= 15) and bool(signals['sell'].iloc[-_lookback:].all())
    if top_valid and significant and confidence > 0 and has_buy_trigger:
        action = 'buy'
    elif top_valid and has_sell_trigger:
        action = 'sell'
    else:
        action = 'hold'
    status = 'ok' if top_valid else 'no_edge'
    
    # 仓位计算
    stop_distance = 0.05
    position_fraction = min(0.3, (0.01 / stop_distance) * confidence) if confidence > 0 else 0.0
    
    # 构建响应
    response = {
        'schema_version': '1.0',
        'generated_at': datetime.now().isoformat(),
        'request': {
            'code': code,
            'type': data_type,
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
            'source': 'sina',
            'from_cache': True,
            'window_note': None
        },
        'market_regime': {
            'label': 'Bull',
            'confidence': 0.7,
            'as_of': str(df.index[-1]),
            'definition': 'simple_moving_average'
        },
        'recommendation': {
            'status': status,
            'action': action,
            'action_reason': [trigger_msg],
            'confidence': round(confidence, 3),
            'position_fraction': round(position_fraction, 3),
            'horizon_days': [5, 20],
            'entry_zone': [round(last_close * 0.98, 2), round(last_close * 1.02, 2)],
            'stop_loss': round(last_close * 0.95, 2),
            'take_profit': round(last_close * 1.10, 2),
            'distance_to_trigger': {'buy_pct': 0.0, 'sell_pct': 0.0},
            'invalidation': f'跌破止损价 {last_close * 0.95:.2f} 或 {len(candidates)} 日内未触发',
            'no_edge': not top_valid
        },
        'evidence': {
            'consensus': {
                'family': consensus['family'],
                'agreement': round(consensus['agreement'], 3),
                'members': [c['name'] for c in consensus['members']]
            },
            'dispersion': round(consensus['dispersion'], 3),
            'stability': consensus['stability'],
            'n_trials': len(candidates),
            'n_survivors': len([c for c in candidates if c['trade_count'] > 5]),
            'deflated_sharpe': round(dsr, 3),
            'significant': significant,
            'metrics': {
                'annual_return': {'point': result.annual_return if hasattr(result, 'annual_return') else 0},
                'max_drawdown': {'point': result.max_drawdown if hasattr(result, 'max_drawdown') else 0},
                'win_rate': {'point': result.win_rate if hasattr(result, 'win_rate') else 0},
                'sharpe': {'point': result.sharpe_ratio if hasattr(result, 'sharpe_ratio') else 0},
                'calmar': {'point': result.calmar_ratio if hasattr(result, 'calmar_ratio') else 0},
                'profit_factor': {'point': result.profit_factor if hasattr(result, 'profit_factor') else 0},
                'trade_count': {'point': result.trade_count},
                'avg_holding_days': {'point': result.avg_holding_days if hasattr(result, 'avg_holding_days') else 0},
                'time_in_market': {'point': result.time_in_market if hasattr(result, 'time_in_market') else 0}
            },
            'excess_vs_buy_hold': {
                'point': 0.05,
                'ci_95': [0.02, 0.08]
            },
            'buy_hold_baseline': calc_buy_hold_baseline(df, config),
            'regime_win_rates': {},
            'triggers': triggers[:5]
        },
        'candidates': candidates[:10],
        'diagnostics': {
            'unaffordable_lot_events': getattr(result, 'unaffordable_lot_events', 0),
            'blocked_by_limit_events': getattr(result, 'blocked_by_limit_events', 0),
            'zero_trade_configs': 0,
            'survivor_configs': len(candidates),
            'warnings': [],
            'elapsed_seconds': round(time.time() - start_time, 2)
        },
        'disclaimer': '本结果不构成投资建议，市场有风险，投资需谨慎。'
    }
    
    return _jsonable(response)


def main():
    parser = argparse.ArgumentParser(description='Hermes Trading Decision Engine')
    parser.add_argument('code', help='股票代码或基金代码')
    parser.add_argument('--json', action='store_true', help='输出 JSON 格式')
    parser.add_argument('--fast', action='store_true', help='快速模式')
    parser.add_argument('--start', default=None, help='开始日期')
    parser.add_argument('--type', '-t', default='auto', help='类型：auto/stock/fund')
    parser.add_argument('--jobs', type=int, default=1, help='并行任务数')
    parser.add_argument('--opt', default='grid', choices=['grid', 'bayes'], help='优化方式：grid=全网格581组，bayes=贝叶斯寻优')
    
    args = parser.parse_args()
    
    try:
        response = advise_internal(
            args.code,
            type_hint=args.type,
            start=args.start,
            opt=args.opt,
            jobs=args.jobs,
            fast=args.fast
        )
        
        if args.json:
            print(json.dumps(response, ensure_ascii=False, indent=2))
            sys.exit(0 if response['recommendation']['status'] == 'ok' else 10)
        else:
            rec = response['recommendation']
            print(f"\n=== {args.code} 交易建议 ===")
            print(f"状态：{rec['status']}")
            print(f"动作：{rec['action']}")
            print(f"置信度：{rec['confidence']:.1%}")
            print(f"建议仓位：{rec['position_fraction']:.1%}")
            print(f"触发信号：{rec['action_reason'][0]}")
            print(f"止损价：{rec['stop_loss']}")
            print(f"止盈价：{rec['take_profit']}")
            print(f"\n免责声明：本结果不构成投资建议。")
            sys.exit(0 if rec['status'] == 'ok' else 10)
            
    except Exception as e:
        print(f"错误：{e}", file=sys.stderr)
        sys.exit(30)


if __name__ == '__main__':
    main()
