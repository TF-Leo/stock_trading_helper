import pandas as pd
import numpy as np
import os
import sys

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from stock_analysis.data_loader import get_stock_daily, get_stock_name
from stock_analysis.strategy import (
    TrendStrategy, VolumeStrategy, ChipStrategy,
    BollStrategy, MacdStrategy, ComprehensiveStrategy,
)
from stock_analysis.strategy_master.config import (
    load_config, save_config, get_market_regime_series,
)


def run_detailed_backtest(strategy, df, market_regimes):
    df = df.copy()
    signals = strategy.generate_trade_signals(df)
    df['buy'] = signals['buy']
    df['sell'] = signals['sell']
    if market_regimes is not None:
        df['regime'] = market_regimes.reindex(df.index).fillna('Shock')
    else:
        df['regime'] = 'Shock'
    trades = []
    holding = False
    buy_price = 0.0
    buy_regime = 'Shock'
    for i in range(len(df)):
        if df['buy'].iloc[i] and not holding:
            buy_price = df['close'].iloc[i]
            buy_regime = df['regime'].iloc[i]
            holding = True
        elif df['sell'].iloc[i] and holding:
            sell_price = df['close'].iloc[i]
            pct = (sell_price - buy_price) / buy_price
            trades.append((pct, buy_regime))
            holding = False
    regime_stats = {}
    for r in ['Bull', 'Bear', 'Shock']:
        r_trades = [t[0] for t in trades if t[1] == r]
        if r_trades:
            wins = sum(1 for t in r_trades if t > 0)
            regime_stats[r] = wins / len(r_trades)
        else:
            regime_stats[r] = 0.0
    all_rets = [t[0] for t in trades]
    if all_rets:
        overall_win_rate = sum(1 for r in all_rets if r > 0) / len(all_rets)
        avg_return = np.mean(all_rets)
    else:
        overall_win_rate = 0.0
        avg_return = 0.0
    return {
        "win_rates": regime_stats,
        "overall_win_rate": overall_win_rate,
        "avg_return": avg_return,
        "trade_count": len(trades),
    }


def optimize_stock_strategy(df, market_regimes):
    """P2: 旧9策略锦标赛已废弃，委托到新统一锦标赛（保留API兼容）。CLI未调用此函数。"""
    import warnings
    warnings.warn("optimize_stock_strategy已废弃，请用backtest.analyzer.run_strategy_tournament", DeprecationWarning, stacklevel=2)
    try:
        from stock_analysis.backtest.analyzer import run_strategy_tournament
        from stock_analysis.backtest.engine import BacktestConfig
        best_name, best_result, _comp = run_strategy_tournament(df, market_regimes, BacktestConfig())
        # 尝试按名找回实例
        from stock_analysis.strategy.registry import build_strategy_instances
        insts = build_strategy_instances()
        key = best_name.replace("（无优势，建议观望）", "")
        best_strat = insts.get(key)
        details = {
            "overall_win_rate": best_result.win_rate,
            "avg_return": best_result.total_return / max(best_result.trade_count, 1),
            "trade_count": best_result.trade_count,
            "win_rates": {"Bull": best_result.bull_win_rate, "Bear": best_result.bear_win_rate, "Shock": best_result.shock_win_rate},
            "best_name": best_name,
        }
        return best_strat, details
    except Exception:
        pass
    strategies = [
        TrendStrategy(5, 13, 34),
        TrendStrategy(5, 20, 60),
        TrendStrategy(13, 34, 55),
        TrendStrategy(20, 60, 120),
        VolumeStrategy(),
        ChipStrategy(),
        ComprehensiveStrategy(),
        BollStrategy(),
        MacdStrategy(),
    ]
    best_score = -9999
    best_strat = None
    best_details = None
    for strat in strategies:
        try:
            details = run_detailed_backtest(strat, df, market_regimes)
            if details['trade_count'] < 3:
                score = -100
            else:
                score = details['overall_win_rate'] * 100 + details['avg_return'] * 100
            if details['overall_win_rate'] > 0.6:
                score += 20
            if score > best_score:
                best_score = score
                best_strat = strat
                best_details = details
        except Exception as e:
            print(f"Strategy {strat.name} failed: {e}")
            continue
    return best_strat, best_details


def main():
    print("正在启动策略优化锦标赛...")
    config = load_config()
    regimes = get_market_regime_series()
    if regimes is None:
        print("错误: 无法获取市场指数数据。")
        return
    stocks = config.keys()
    if not stocks:
        stocks = ["600519", "000858", "601318", "002594", "300059", "600036", "000001", "601689"]
    for code in stocks:
        name = config.get(code, {}).get('name', code)
        if name == "待定名称" or name == code:
            print(f"尝试获取 {code} 的真实名称...")
            real_name = get_stock_name(code)
            if real_name:
                print(f"获取成功: {real_name}")
                config[code]['name'] = real_name
                name = real_name
                save_config(config)
        print(f"正在优化 {name} ({code})...")
        df = get_stock_daily(code, start_date='20200101', source='sina')
        if df is None or len(df) < 100:
            print(f"跳过 {code} (数据不足)")
            continue
        best_strat, stats = optimize_stock_strategy(df, regimes)
        strat_map = {
            "Trend_MA": "均线趋势",
            "Volume_Breakout": "量价突破",
            "Chip_Distribution": "筹码分布",
            "Comprehensive_Trend_Vol_Chip": "三维共振(量价筹)",
            "Boll_MeanReversion": "布林回归",
            "MACD_Turnaround": "MACD反转",
        }
        if best_strat:
            s_name = strat_map.get(best_strat.name, best_strat.name)
            print(f"  最佳策略: {s_name}")
            print(f"  胜率: {stats['overall_win_rate']:.2%}, 平均收益: {stats['avg_return']:.2%}")
            entry = config.get(code, {})
            entry['strategy_type'] = best_strat.name
            entry['win_rates'] = stats['win_rates']
            if isinstance(best_strat, TrendStrategy):
                entry['params'] = {'s': best_strat.s, 'm': best_strat.m, 'l': best_strat.l}
            else:
                entry['params'] = {}
            config[code] = entry
        else:
            print(f"  未找到有效策略。")
    save_config(config)
    print("优化完成。配置已更新。")


if __name__ == "__main__":
    main()
