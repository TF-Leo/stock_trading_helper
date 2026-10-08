import argparse

try:
    from fund_analysis.utils import (
        load_or_fetch, _fetch_fund, _fetch_index,
        _fetch_csi_all_share_full, _fetch_csi_all_share_incremental,
        FUND_CONFIG, save_config,
    )
except ImportError:
    import sys
    import os
    sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from utils import (
        load_or_fetch, _fetch_fund, _fetch_index,
        _fetch_csi_all_share_full, _fetch_csi_all_share_incremental,
        FUND_CONFIG, save_config,
    )

from fund_analysis.fund_advisor.analyzer import (
    analyze_fund_status,
    analyze_market_sentiment,
    analyze_entry_opportunity,
)


def analyze_fund_entry(code, market_score):
    df = load_or_fetch(f'fund_{code}.csv', lambda: _fetch_fund(code))
    if df is None:
        return
    last = df.iloc[-1]
    roll_max = df['close'].cummax()
    dd = (df['close'] / roll_max - 1) * 100
    current_dd = dd.iloc[-1]
    info = FUND_CONFIG.get(code, {})
    name = info.get('name', code)
    rec = "观察"
    if current_dd < -20:
        rec = "积极买入 (深跌)"
    elif current_dd < -10:
        rec = "分批买入 (回调)"
    elif current_dd < -5:
        rec = "小额定投"
    else:
        rec = "暂停/观望 (高位)"
    if market_score >= 1.5 and current_dd < -5:
        rec = "[!!] 强力建仓 (共振)"
    print(f"基金 {code} ({name}): 当前回撤 {current_dd:.2f}% -> 建议: {rec}")


def main():
    parser = argparse.ArgumentParser(description='基金投资顾问')
    parser.add_argument('--report', type=str, help='生成指定基金的深度复盘报告 (例如: 001564)')
    args = parser.parse_args()

    if args.report:
        try:
            from fund_analysis.fund_detailed_analyzer import generate_detailed_report
        except ImportError:
            from fund_detailed_analyzer import generate_detailed_report
        generate_detailed_report(args.report)
        return

    print("正在启动基金投资顾问...")
    analyze_market_sentiment()
    market_score = analyze_entry_opportunity()
    target_funds = list(FUND_CONFIG.keys())
    print(f"\n{'=' * 10} 个基建仓指引 {'=' * 10}")
    for code in target_funds:
        analyze_fund_entry(code, market_score)
    print(f"\n{'=' * 10} 持仓监控 {'=' * 10}")
    for code in target_funds:
        analyze_fund_status(code)
    print(f"\n{'=' * 40}")
    print("注: 数据仅供参考，不构成投资建议。")


if __name__ == "__main__":
    main()
