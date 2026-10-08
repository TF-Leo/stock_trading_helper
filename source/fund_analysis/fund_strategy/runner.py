import pandas as pd
import numpy as np
import datetime

from fund_analysis.fund_strategy.strategy import (
    BENCHMARKS,
    load_config,
    save_config,
    fetch_fund_data,
    fetch_index_data,
    calculate_indicators,
    match_benchmark,
)


def backtest_strategy(df, params, strategy_type='swing'):
    df = df.copy()
    initial_capital = 100000.0
    cash = initial_capital
    position = 0.0
    start_date = df.index[-1] - pd.DateOffset(years=3)
    test_df = df[df.index >= start_date].copy()
    if test_df.empty:
        return 0, 0
    if 'RSI' in test_df:
        test_df['RSI_Signal'] = test_df['RSI'].shift(1)
    else:
        test_df['RSI_Signal'] = 50
    if 'MA20' in test_df:
        test_df['MA20_Signal'] = test_df['MA20'].shift(1)
    else:
        test_df['MA20_Signal'] = test_df['close'].shift(1)
    values = []
    for i in range(len(test_df)):
        price = test_df['close'].iloc[i]
        rsi = test_df['RSI_Signal'].iloc[i]
        ma_s = test_df['MA20_Signal'].iloc[i]
        if pd.isna(rsi) or pd.isna(ma_s):
            values.append(cash + position * price)
            continue
        buy_signal = False
        sell_signal = False
        if strategy_type == 'swing':
            if rsi < params.get('rsi_lower', 30):
                buy_signal = True
            elif rsi > params.get('rsi_upper', 70):
                sell_signal = True
        else:
            prev_close = test_df['close'].iloc[i - 1] if i > 0 else price
            if prev_close > ma_s:
                buy_signal = True
        if buy_signal and cash > 0:
            position = cash / price
            cash = 0
        elif sell_signal and position > 0:
            cash = position * price
            position = 0
        curr_val = cash + position * price
        values.append(curr_val)
    if not values:
        return 0, 0
    final_val = values[-1]
    total_ret = (final_val - initial_capital) / initial_capital
    val_series = pd.Series(values)
    cummax = val_series.cummax()
    drawdown = (val_series - cummax) / cummax
    max_dd = drawdown.min()
    return total_ret, max_dd


def optimize_parameters(df, base_params):
    best_score = -100
    best_params = base_params.copy()
    for lower in [20, 25, 30, 35]:
        for upper in [65, 70, 75, 80]:
            params = base_params.copy()
            params['rsi_lower'] = lower
            params['rsi_upper'] = upper
            ret, dd = backtest_strategy(df, params, strategy_type='swing')
            if dd == 0:
                dd = -0.01
            score = ret / abs(dd)
            if score > best_score:
                best_score = score
                best_params = params
    return best_params


def analyze_portfolio_correlation(funds, fund_dfs):
    print("\n[Step 3] 基金组合相关性分析...")
    valid_funds = [f for f in funds if f in fund_dfs and fund_dfs[f] is not None]
    if len(valid_funds) < 2:
        print("有效基金数量不足，无法分析相关性。")
        return
    combined_ret = pd.DataFrame()
    for code in valid_funds:
        df = fund_dfs[code]
        recent_df = df[df.index >= (df.index[-1] - pd.DateOffset(years=1))]
        ret = recent_df['close'].pct_change()
        combined_ret[code] = ret
    combined_ret = combined_ret.dropna()
    if combined_ret.empty:
        print("无重叠交易日，无法分析。")
        return
    corr_matrix = combined_ret.corr()
    print("\n>>> 近1年相关性矩阵:")
    print(corr_matrix.round(2))
    high_corr_pairs = []
    cols = corr_matrix.columns
    for i in range(len(cols)):
        for j in range(i + 1, len(cols)):
            c1, c2 = cols[i], cols[j]
            val = corr_matrix.loc[c1, c2]
            if val > 0.85:
                high_corr_pairs.append((c1, c2, val))
    if high_corr_pairs:
        print("\n[!] 警告: 以下基金相关性过高，可能缺乏分散效果:")
        for c1, c2, val in high_corr_pairs:
            print(f"   - {c1} 与 {c2}: {val:.2f}")
    else:
        print("\n[OK] 组合分散性良好 (无 >0.85 的相关对)")


def main():
    print("=== 基金策略大师 (Fund Strategy Master) 启动 ===")
    print(f"运行时间: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    config = load_config()
    funds = list(config.keys())
    if not funds:
        print("未找到基金配置。请检查 fund_config.json")
        return
    print("\n[Step 1] 更新指数数据...")
    benchmark_dfs = {}
    for code, name in BENCHMARKS.items():
        benchmark_dfs[code] = fetch_index_data(code)
    print("\n[Step 2] 分析各只基金策略...")
    fund_dfs = {}
    for code in funds:
        fund_name = config[code].get('name', '未知')
        print(f"\n>>> 正在分析: {fund_name} ({code})")
        df = fetch_fund_data(code)
        if df is None:
            continue
        fund_dfs[code] = df
        df = calculate_indicators(df)
        bench_code, corr = match_benchmark(df, benchmark_dfs)
        bench_name = BENCHMARKS.get(bench_code, "未知")
        print(f"    - 最佳对标指数: {bench_name} (相关系数: {corr:.2f})")
        default_params = {
            "rsi_lower": 30, "rsi_upper": 70,
            "ma_window_short": 20, "ma_window_long": 60,
            "boll_window": 20, "boll_std": 2,
        }
        ret_hold, dd_hold = backtest_strategy(df, default_params, strategy_type='hold')
        best_params = optimize_parameters(df, default_params)
        ret_swing, dd_swing = backtest_strategy(df, best_params, strategy_type='swing')
        print(f"    - 策略回测对比 (近3年):")
        print(f"      [稳健持有] 收益: {ret_hold * 100:6.1f}%, 最大回撤: {dd_hold * 100:6.1f}%")
        print(f"      [波段操作] 收益: {ret_swing * 100:6.1f}%, 最大回撤: {dd_swing * 100:6.1f}%")
        print(f"      [最优参数] RSI买入<{best_params['rsi_lower']}, 卖出>{best_params['rsi_upper']}")
        is_swing_better = (ret_swing > ret_hold * 1.15) and (ret_swing > 0)
        if is_swing_better:
            strategy_type = 'swing'
            rec_strategy = "波段操作 (Swing)"
            buy_desc = f"RSI < {best_params['rsi_lower']} (超卖)"
            sell_desc = f"RSI > {best_params['rsi_upper']} (超买)"
        else:
            strategy_type = 'hold'
            rec_strategy = "稳健持有 (Hold)"
            buy_desc = f"RSI < {best_params['rsi_lower']} 或 跌破MA60"
            sell_desc = "仅在极度高估时减仓 (长持为主)"
        print(f"    - 最终建议: {rec_strategy}")
        config[code]['strategy_type'] = strategy_type
        config[code]['benchmark'] = bench_code
        config[code]['params'] = best_params
        config[code]['buy_signal_desc'] = buy_desc
        config[code]['sell_signal_desc'] = sell_desc
        config[code]['last_analysis_date'] = datetime.date.today().strftime("%Y-%m-%d")
    save_config(config)
    print("\n[配置更新] 所有策略参数已保存至 fund_config.json")
    analyze_portfolio_correlation(funds, fund_dfs)
    print("\n=== 全部完成 ===")


if __name__ == "__main__":
    main()
