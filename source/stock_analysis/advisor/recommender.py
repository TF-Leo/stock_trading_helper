import pandas as pd
import numpy as np
from stock_analysis.indicators import calc_rsi, calc_cyc_cost, calc_winner_pct, add_all_indicators
from stock_analysis.advisor.analyzer import (
    analyze_trend_strength,
    analyze_market_level,
    get_current_market_regime,
    analyze_volume_chip_structure,
)


def analyze_stock_row(row, df=None, market_regime="Shock"):
    signals = {"buy": [], "sell": [], "info": []}
    if df is not None and len(df) >= 60:
        df = add_all_indicators(df)
        trend_score, trend_desc = analyze_trend_strength(df)
        level, level_desc = analyze_market_level(df)
        chip = analyze_volume_chip_structure(df)
        signals["info"].append(f"趋势:{trend_desc}({trend_score}/8)")
        signals["info"].append(f"水位:{level_desc}({level:.0f}%)")
        signals["info"].append(f"量价筹码:{chip['desc']}({chip['score']}/100)")
        if trend_score >= 7 and chip['score'] >= 70:
            signals["buy"].append("趋势+筹码共振(强力买入)")
        elif trend_score >= 4 and level < 40:
            signals["buy"].append("趋势偏多+低位(逢低吸纳)")
        if trend_score <= 1:
            signals["sell"].append("趋势弱势(考虑减仓)")
        if level > 80 and chip['score'] < 30:
            signals["sell"].append("高位+筹码偏弱(风险较大)")
        curr = df.iloc[-1]
        if 'RSI' in df.columns:
            rsi = curr['RSI']
            if rsi < 30:
                signals["buy"].append(f"RSI超卖({rsi:.0f})")
            elif rsi > 70:
                signals["sell"].append(f"RSI超买({rsi:.0f})")
        if 'MACD' in df.columns and 'DIF' in df.columns and 'DEA' in df.columns:
            dif = curr['DIF']
            dea = curr['DEA']
            if dif > dea and df['DIF'].iloc[-2] <= df['DEA'].iloc[-2]:
                signals["buy"].append("MACD金叉")
            elif dif < dea and df['DIF'].iloc[-2] >= df['DEA'].iloc[-2]:
                signals["sell"].append("MACD死叉")
    else:
        signals["info"].append("数据不足，仅基础分析")
    close = row.get('close', 0)
    pct_chg = row.get('pct_chg', 0)
    if close > 0:
        if pct_chg and pct_chg > 5:
            signals["info"].append(f"今日大涨{pct_chg:.1f}%")
        elif pct_chg and pct_chg < -5:
            signals["info"].append(f"今日大跌{pct_chg:.1f}%")
    return signals


def load_config():
    import json
    import os
    config_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "stock_config.json",
    )
    if os.path.exists(config_path):
        try:
            with open(config_path, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def main():
    from stock_analysis.data_loader import get_stock_daily, get_market_index_daily
    print("=== 股票投资顾问 ===")
    config = load_config()
    watchlist = config.get("watchlist", ["600519", "000858", "601318"])
    mkt_df = get_market_index_daily("000985", source="sina")
    if mkt_df is not None and len(mkt_df) >= 60:
        regime, confidence = get_current_market_regime(mkt_df)
        print(f"\n当前市场环境: {regime} (置信度: {confidence:.0%})")
    else:
        regime = "Shock"
        print("\n当前市场环境: Shock (默认)")
    for code in watchlist:
        df = get_stock_daily(code, source="sina")
        if df is None or len(df) < 60:
            print(f"\n{code}: 数据不足")
            continue
        df = add_all_indicators(df)
        last = df.iloc[-1]
        signals = analyze_stock_row(last, df, regime)
        print(f"\n{'='*10} {code} {'='*10}")
        print(f"收盘: {last['close']:.2f}")
        if signals['buy']:
            print(f"买入信号: {', '.join(signals['buy'])}")
        if signals['sell']:
            print(f"卖出信号: {', '.join(signals['sell'])}")
        if signals['info']:
            print(f"分析: {', '.join(signals['info'])}")


if __name__ == "__main__":
    main()
