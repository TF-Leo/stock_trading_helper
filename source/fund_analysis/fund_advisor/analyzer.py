import pandas as pd
import numpy as np

try:
    from fund_analysis.utils import (
        load_or_fetch, _fetch_fund, _fetch_index,
        _fetch_csi_all_share_full, _fetch_csi_all_share_incremental,
        FUND_CONFIG, save_config,
    )
    from stock_analysis.data_loader import get_stock_name
except ImportError:
    import sys
    import os
    sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from utils import (
        load_or_fetch, _fetch_fund, _fetch_index,
        _fetch_csi_all_share_full, _fetch_csi_all_share_incremental,
        FUND_CONFIG, save_config,
    )
    from stock_analysis.data_loader import get_stock_name

pd.set_option('display.max_rows', None)
pd.set_option('display.max_columns', None)
pd.set_option('display.width', 1000)


def calc_rsi(series, period=14):
    delta = series.diff()
    up = delta.clip(lower=0)
    down = -1 * delta.clip(upper=0)
    ema_up = up.ewm(com=period - 1, adjust=False).mean()
    ema_down = down.ewm(com=period - 1, adjust=False).mean()
    rs = ema_up / ema_down
    rsi = 100 - (100 / (1 + rs))
    return rsi


def analyze_fund_status(code):
    print(f"正在获取基金 {code} 数据...")
    df = load_or_fetch(f'fund_{code}.csv', lambda: _fetch_fund(code))
    if df is None:
        print(f"无法获取基金 {code} 数据")
        return
    params = FUND_CONFIG.get(code, {}).get('params', {})
    ma_short = params.get('ma_window_short', 20)
    ma_long = params.get('ma_window_long', 60)
    rsi_period = params.get('rsi_period', 14)
    rsi_low = params.get('rsi_lower', 30)
    df['MA_Short'] = df['close'].rolling(ma_short).mean()
    df['MA_Long'] = df['close'].rolling(ma_long).mean()
    df['RSI'] = calc_rsi(df['close'], rsi_period)
    boll_w = params.get('boll_window', 20)
    boll_std = params.get('boll_std', 2)
    df['BOLL_Mid'] = df['close'].rolling(boll_w).mean()
    df['BOLL_Std'] = df['close'].rolling(boll_w).std()
    df['BOLL_Lower'] = df['BOLL_Mid'] - boll_std * df['BOLL_Std']
    df['BOLL_Width'] = (df['BOLL_Mid'] + boll_std * df['BOLL_Std'] - df['BOLL_Lower']) / df['BOLL_Mid']
    last = df.iloc[-1]
    last_date = last.name.date()
    info = FUND_CONFIG.get(code, {})
    name = info.get('name', code)
    if name == "待定名称" or name == code:
        print(f"尝试获取基金 {code} 的真实名称...")
        real_name = get_stock_name(code)
        if real_name:
            print(f"获取成功: {real_name}")
            FUND_CONFIG[code]['name'] = real_name
            name = real_name
            save_config(FUND_CONFIG)
    strategy = info.get('strategy', '')
    strategy_type = info.get('strategy_type', 'hold')
    print(f"\n{'=' * 10} {code} {name} {'=' * 10}")
    print(f"日期: {last_date}")
    print(f"净值: {last['close']:.4f} ({last['pct_chg'] * 100:+.2f}%)")
    print(f"MA{ma_short}: {last['MA_Short']:.4f} | MA{ma_long}: {last['MA_Long']:.4f}")
    print(f"RSI({rsi_period}): {last['RSI']:.2f}")
    print(f"BOLL带宽: {last['BOLL_Width']:.3f} (波动率)")
    buy_signals = []
    sell_signals = []
    if last['close'] > last['MA_Short']:
        status = f"[OK] 趋势向上 (站上MA{ma_short})"
    else:
        status = f"[!] 趋势调整 (跌破MA{ma_short})"
    print(f"状态: {status}")
    buy_advice = "⚪ 暂无加仓信号"
    if last['close'] < last['MA_Long']:
        dist = (last['close'] / last['MA_Long'] - 1) * 100
        buy_advice = f"[*] **黄金坑机会** (低于MA{ma_long} {abs(dist):.1f}%)"
        buy_signals.append(f"MA{ma_long}低吸")
    if last['RSI'] < rsi_low:
        buy_advice = f"[!!] **严重超卖** (RSI<{rsi_low})"
        buy_signals.append("RSI超卖")
    if last['close'] <= last['BOLL_Lower'] * 1.01:
        buy_signals.append("触及布林下轨")
        if "黄金坑" not in buy_advice and "超卖" not in buy_advice:
            buy_advice = "[*] **布林下轨支撑** (短线反弹机会)"
    print(f"买入: {buy_advice}")
    sell_advice = ""
    if last['RSI'] > 75:
        sell_signals.append("RSI超买")
        if strategy_type == 'swing':
            sell_advice = f"[!!] **严重超买** (RSI>75，建议止盈)"
        else:
            sell_advice = f"[!] **技术面超买** (RSI>75，长持策略可忽略)"
    if last['close'] >= last['BOLL_Mid'] + 2 * last['BOLL_Std']:
        sell_signals.append("触及布林上轨")
        if not sell_advice:
            if strategy_type == 'swing':
                sell_advice = f"[!!] **触及上轨** (短期过热，建议减仓)"
            else:
                sell_advice = f"[!] **触及上轨** (短期过热，长持策略可忽略)"
    if sell_signals:
        print(f"卖出: {sell_advice} -> 触发 {', '.join(sell_signals)}")
    print(f"定位: {strategy} [{'长持型' if strategy_type == 'hold' else '波段型'}]")
    if buy_signals:
        print(f"建议: 触发买入信号 [{', '.join(buy_signals)}]，建议执行定投倍投或一次性加仓！")
    elif sell_signals:
        if strategy_type == 'swing':
            print(f"建议: 触发止盈信号 [{', '.join(sell_signals)}]，建议坚决执行波段减仓！")
        else:
            print(f"建议: 触发技术面高点 [{', '.join(sell_signals)}]，但策略偏向长持，建议持有或仅做T。")
    else:
        print(f"建议: 无明显信号，继续持有/定投，等待更好机会。")


def analyze_market_sentiment():
    print(f"\n{'=' * 10} 市场风向标 (中证全指) {'=' * 10}")
    df = load_or_fetch('csi_all_share.csv', _fetch_csi_all_share_full, _fetch_csi_all_share_incremental)
    if df is None:
        print("无法获取市场数据")
        return
    df['MA20_Vol'] = df['amount_yi'].rolling(20).mean()
    df['Std_Vol'] = df['amount_yi'].rolling(20).std()
    last = df.iloc[-1]
    last_date = last.name.date()
    vol = last['amount_yi']
    ma20_vol = last['MA20_Vol']
    high_threshold = ma20_vol + 2.0 * last['Std_Vol']
    low_threshold = ma20_vol * 0.7
    print(f"日期: {last_date}")
    print(f"成交额: {vol:.0f} 亿")
    print(f"基准线: {ma20_vol:.0f} 亿 (MA20)")
    sentiment = "😐 情绪正常"
    action = "按兵不动"
    if vol > high_threshold:
        sentiment = "🥵 **情绪过热**"
        action = "🛑 停止买入，考虑止盈/减仓"
    elif vol < low_threshold:
        sentiment = "🥶 **情绪冰点**"
        action = "💰 大胆贪婪，加倍定投"
    print(f"评价: {sentiment}")
    print(f"指引: {action}")
    return last


def analyze_entry_opportunity():
    print(f"\n{'=' * 10} 建仓机会评估 (市场水位) {'=' * 10}")
    indices = {'HS300': 'sh000300', 'SH_Index': 'sh000001'}
    scores = []
    csi = load_or_fetch('csi_all_share.csv', _fetch_csi_all_share_full, _fetch_csi_all_share_incremental)
    if csi is not None:
        indices_data = {'CSI_All': csi}
    else:
        indices_data = {}
    for name, code in indices.items():
        df = load_or_fetch(f'{name}.csv', lambda: _fetch_index(code))
        if df is not None:
            indices_data[name] = df
    for name, df in indices_data.items():
        recent_df = df[df.index >= pd.Timestamp.now() - pd.Timedelta(days=365 * 5)]
        if recent_df.empty:
            continue
        last_price = recent_df['close'].iloc[-1]
        max_p = recent_df['close'].max()
        min_p = recent_df['close'].min()
        percentile = (last_price - min_p) / (max_p - min_p) * 100
        ma250 = recent_df['close'].rolling(250).mean().iloc[-1]
        bias_250 = (last_price / ma250 - 1) * 100
        score = 0
        status = ""
        if percentile < 20:
            status = "🟢 低估区域 (安全)"
            score = 2
        elif percentile < 50:
            status = "🟡 合理偏低 (可配)"
            score = 1
        elif percentile < 80:
            status = "🟠 合理偏高 (谨慎)"
            score = 0
        else:
            status = "🔴 高估区域 (风险)"
            score = -1
        print(f"{name:<10} | 水位: {percentile:>5.1f}% | 年线偏离: {bias_250:>5.1f}% | {status}")
        scores.append(score)
    print("-" * 40)
    avg_score = sum(scores) / len(scores) if scores else 0
    if avg_score >= 1.5:
        print("💡 市场整体处于【底部区域】，建议加大建仓力度 (如: 60%-80%仓位)。")
    elif avg_score >= 0.5:
        print("💡 市场整体处于【合理区域】，建议按计划分批建仓 (如: 每月定投)。")
    else:
        print("💡 市场整体处于【相对高位】，建议谨慎，保留现金或小额定投。")
    return avg_score
