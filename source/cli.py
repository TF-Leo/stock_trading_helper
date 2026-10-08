# -*- coding: utf-8 -*-
"""
股票/基金多策略回测 CLI

用法:
    python cli.py <代码>                      # 自动识别类型，跑多策略锦标赛
    python cli.py <代码> --type fund          # 强制按基金处理
    python cli.py <代码> --detail             # 显示全策略对比表
    python cli.py <代码> --no-regime          # 跳过市场环境加载（加速）
    python cli.py <代码> --start 20200101     # 指定起始日期
"""
from __future__ import annotations

import argparse
import os
import sys

# 确保 source/ 在搜索路径中，便于直接 python cli.py 运行
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import pandas as pd  # noqa: E402

from stock_analysis.data_loader import get_stock_daily, get_stock_name  # noqa: E402
from stock_analysis.backtest.analyzer import run_strategy_tournament  # noqa: E402
from stock_analysis.backtest.engine import BacktestConfig  # noqa: E402
from stock_analysis.strategy_master.config import get_market_regime_series  # noqa: E402
from fund_analysis.fund_strategy.strategy import (  # noqa: E402
    fetch_fund_data,
    calculate_indicators,
)


def detect_type(code: str) -> str:
    """根据代码前缀推断股票/基金。

    ETF/LOF 前缀: 51/15/16/50/56/159 开头视为基金，其余按股票处理。
    """
    code = str(code).strip()
    if code.startswith(("51", "15", "16", "50", "56", "159")):
        return "fund"
    return "stock"


# P2: 基金回测执行层已拆至 fund_analysis/fund_backtest.py，此处re-export保持兼容
from fund_analysis.fund_backtest import (  # noqa: E402
    FUND_FEE_RATE,
    _fund_backtest_with_stats,
    _fund_backtest_signals,
)


def _build_stock_strategies() -> dict:
    """构造 {策略名: 策略实例} 字典，P1委托统一注册表，与 analyzer._build_strategy_grid 同源。"""
    from stock_analysis.strategy.registry import build_strategy_instances
    return build_strategy_instances()


def _print_detailed_stock_advice(strategy, df: pd.DataFrame, best_name: str) -> None:
    """基于最优策略，输出详细的当前操作建议、关键指标观察、明日操作预案。"""
    from stock_analysis.indicators import (
        calc_ma, calc_macd, calc_boll, calc_rsi, calc_cyc_cost, calc_winner_pct,
        calc_obv, calc_kdj, calc_vr, calc_atr, calc_adx,
    )

    df = df.copy()
    close = df['close']
    last = df.iloc[-1]
    last_date = df.index[-1]
    last_close = float(last['close'])

    signals = strategy.generate_trade_signals(df.copy())
    today_buy = bool(signals['buy'].astype(bool).iloc[-1])
    today_sell = bool(signals['sell'].astype(bool).iloc[-1])

    # 识别策略类型
    is_trend = best_name.startswith("均线趋势")
    is_boll = best_name.startswith("布林")
    is_volume = best_name == "量价突破"
    is_chip = best_name.startswith("筹码")
    is_macd = best_name.startswith("MACD")
    is_comp = best_name == "三维共振"
    is_kdj = best_name.startswith("KDJ")
    is_obv = best_name.startswith("OBV")
    is_vr = best_name.startswith("VR")
    is_kelly = best_name.startswith("Kelly")
    is_donchian = best_name.startswith("唐奇安")
    is_adx = best_name.startswith("ADX")
    is_atr = best_name.startswith("ATR通道")
    # P1 策略
    is_ema = best_name.startswith("EMA交叉")
    is_kama = best_name.startswith("KAMA")
    is_hma = best_name.startswith("HMA交叉")
    is_vwap = best_name.startswith("VWAP")
    is_squeeze = best_name.startswith("布林Squeeze")
    is_atr_filter = best_name.startswith("ATR过滤")
    is_zscore = best_name.startswith("Z-Score")
    is_wr = best_name.startswith("WilliamsR")
    is_cci = best_name.startswith("CCI")
    is_stochrsi = best_name.startswith("StochRSI")
    is_sar = best_name.startswith("SAR")
    is_di = best_name.startswith("DI交叉")
    # P2 策略
    is_mfi = best_name.startswith("MFI")
    is_cmf = best_name.startswith("CMF")
    is_gap = best_name.startswith("缺口跳空")
    is_engulfing = best_name == "吞没形态"
    is_hammer = best_name.startswith("锤子线")
    is_doji = best_name.startswith("十字星")
    is_grid_arith = best_name.startswith("等差网格")
    is_grid_geom = best_name.startswith("等比网格")
    is_grid_atr = best_name.startswith("ATR网格")
    is_vol_target = best_name.startswith("波动率目标")
    is_pairs = best_name.startswith("配对交易")
    is_spread_z = best_name.startswith("价差ZScore")
    is_voting = best_name.startswith("策略组合")
    is_etf_rot = best_name.startswith("指数轮动")
    is_risk_par = best_name.startswith("风险平价")

    # ========== 第一段：今日信号 ==========
    print("\n=== 当前操作建议 ===")
    print(f"    最新价      : {last_close:.2f} ({last_date.date()})")
    if today_buy:
        print("    今日信号    : 买入")
    elif today_sell:
        print("    今日信号    : 卖出")
    else:
        print("    今日信号    : 无（观望）")

    # ========== 第二段：关键指标观察 ==========
    print("\n=== 关键指标观察 ===")
    ind_lines: list[str] = []

    # 趋势类指标（trend / comprehensive 需要）
    need_trend = is_trend or is_comp
    if need_trend:
        s, m, l = 5, 13, 34
        if is_trend:
            try:
                nums = best_name.split("(")[1].split(")")[0].split("-")
                s, m, l = int(nums[0]), int(nums[1]), int(nums[2])
            except Exception:
                pass
        ma_s = float(calc_ma(close, s).iloc[-1])
        ma_m = float(calc_ma(close, m).iloc[-1])
        ma_l = float(calc_ma(close, l).iloc[-1])
        vol_ma5 = float(df['volume'].rolling(5).mean().iloc[-1])
        last_vol = float(last['volume'])
        aligned = ma_s > ma_m > ma_l
        cross_gap = ma_s - ma_m
        ind_lines.append(f"  趋势 ({s}/{m}/{l}):")
        ind_lines.append(f"    MA{s:<3}= {ma_s:.2f}   MA{m:<3}= {ma_m:.2f}   MA{l:<3}= {ma_l:.2f}")
        ind_lines.append(f"    多头排列    : {'是' if aligned else '否'}")
        ind_lines.append(f"    MA{s}/MA{m}差值: {cross_gap:+.2f}  ({'即将金叉' if -0.3 < cross_gap < 0 else '即将死叉' if 0 < cross_gap < 0.3 else '稳定'})")
        ind_lines.append(f"    量比(vs MA5): {last_vol / vol_ma5:.2f}  ({'活跃' if last_vol > vol_ma5 else '缩量'})")
        ind_lines.append(f"    close vs MA{l}: {'之上(多头)' if last_close > ma_l else '之下(空头)'}")

    # 量价类指标
    need_volume = is_volume or is_comp
    if need_volume:
        ma20 = float(calc_ma(close, 20).iloc[-1])
        vol_ma20 = float(df['volume'].rolling(20).mean().iloc[-1])
        obv = calc_obv(close, df['volume'])
        obv_ma20 = float(obv.rolling(20).mean().iloc[-1])
        last_obv = float(obv.iloc[-1])
        rsi = float(calc_rsi(close).iloc[-1])
        vol_ratio = float(last['volume']) / vol_ma20
        ind_lines.append(f"  量价:")
        ind_lines.append(f"    MA20        = {ma20:.2f}  close {'>' if last_close > ma20 else '<'} MA20")
        ind_lines.append(f"    量比(vs MA20): {vol_ratio:.2f}  ({'放量' if vol_ratio > 1.5 else '正常' if vol_ratio > 0.8 else '缩量'})")
        ind_lines.append(f"    OBV/MA20    : {last_obv:.0f} / {obv_ma20:.0f}  ({'向上' if last_obv > obv_ma20 else '向下'})")
        ind_lines.append(f"    RSI(14)     : {rsi:.1f}  ({'超买' if rsi > 70 else '超卖' if rsi < 30 else '中性'})")

    # 布林指标（从策略名解析参数，如 "布林(20,2.5)"）
    if is_boll:
        try:
            parts = best_name.split("(")[1].split(")")[0].split(",")
            boll_w, boll_std = int(parts[0]), float(parts[1])
        except Exception:
            boll_w, boll_std = 20, 2.0
        upper, mid, lower = calc_boll(close, window=boll_w, num_std=boll_std)
        u, md, lo = float(upper.iloc[-1]), float(mid.iloc[-1]), float(lower.iloc[-1])
        width = (u - lo) / md * 100
        ind_lines.append(f"  布林带({boll_w},{boll_std}):")
        ind_lines.append(f"    上轨 = {u:.2f}   中轨 = {md:.2f}   下轨 = {lo:.2f}")
        ind_lines.append(f"    带宽        : {width:.1f}%  ({'收窄(变盘在即)' if width < 5 else '正常'})")
        ind_lines.append(f"    close 位置  : {'破上轨(超买)' if last_close > u else '破下轨(超卖)' if last_close < lo else '中轨上方' if last_close > md else '中轨下方'}")

    # MACD 指标
    if is_macd:
        dif_s, dea_s, hist_s = calc_macd(close)
        dif, dea, hist = float(dif_s.iloc[-1]), float(dea_s.iloc[-1]), float(hist_s.iloc[-1])
        prev_hist = float(hist_s.iloc[-2]) if len(hist_s) > 1 else 0
        ind_lines.append(f"  MACD(12,26,9):")
        ind_lines.append(f"    DIF = {dif:.4f}   DEA = {dea:.4f}   Hist = {hist:.4f}")
        ind_lines.append(f"    DIF 位置   : {'零轴上' if dif > 0 else '零轴下'}")
        ind_lines.append(f"    DIF/DEA    : {'金叉' if dif > dea else '死叉'}  (差值 {dif - dea:+.4f})")
        hist_dir = "红柱伸长" if hist > prev_hist > 0 else "红柱缩短" if hist > 0 < prev_hist else "绿柱伸长" if hist < 0 else "绿柱缩短"
        ind_lines.append(f"    柱状图     : {hist_dir}")

    # 筹码指标
    need_chip = is_chip or is_comp
    if need_chip:
        if 'turnover' in df.columns:
            to_rate = df['turnover'] / 100
        else:
            to_rate = pd.Series(0.01, index=df.index)
        cyc_cost = calc_cyc_cost(close, to_rate)
        winner = calc_winner_pct(close, cyc_cost)
        cc, wp = float(cyc_cost.iloc[-1]), float(winner.iloc[-1])
        # 从策略名解析筹码阈值，如 "筹码(30-85)"
        try:
            parts = best_name.split("(")[1].split(")")[0].split("-")
            wl, wu = int(parts[0]), int(parts[1])
        except Exception:
            wl, wu = 30, 85
        ind_lines.append(f"  筹码({wl}-{wu}):")
        ind_lines.append(f"    筹码成本    = {cc:.2f}")
        ind_lines.append(f"    获利盘比例  = {wp:.1f}%  ({f'高位(>{wu}%触发卖出)' if wp > wu else f'低位(<{wl}%触发买入)' if wp < wl else '中性'})")
        ind_lines.append(f"    close vs 成本: {'成本上方' if last_close > cc else '成本下方'}  (偏离 {(last_close-cc)/cc*100:+.1f}%)")

    # ===== P0 策略指标 =====

    # KDJ 指标
    if is_kdj:
        try:
            parts = best_name.split("(")[1].split(")")[0].replace(",", "/").split("/")
            n, buy_k, sell_k = int(parts[0]), int(parts[1]), int(parts[2])
        except Exception:
            n, buy_k, sell_k = 9, 20, 80
        k_s, d_s, j_s = calc_kdj(df['high'], df['low'], close, n=n)
        k, d, j = float(k_s.iloc[-1]), float(d_s.iloc[-1]), float(j_s.iloc[-1])
        ind_lines.append(f"  KDJ({n},{buy_k}/{sell_k}):")
        ind_lines.append(f"    K = {k:.1f}   D = {d:.1f}   J = {j:.1f}")
        ind_lines.append(f"    K vs D      : {'金叉' if k > d else '死叉'}  (差 {k-d:+.1f})")
        ind_lines.append(f"    K vs 阈值   : {'超卖(<' + str(buy_k) + '触发买入)' if k < buy_k else '超买(>' + str(sell_k) + '触发卖出)' if k > sell_k else '中性'}")

    # OBV 指标
    if is_obv:
        try:
            obv_ma = int(best_name.split("(")[1].split(")")[0])
        except Exception:
            obv_ma = 20
        obv = calc_obv(close, df['volume'])
        obv_ma_v = float(obv.rolling(obv_ma).mean().iloc[-1])
        last_obv = float(obv.iloc[-1])
        ind_lines.append(f"  OBV(MA{obv_ma}):")
        ind_lines.append(f"    OBV = {last_obv:.0f}   MA{obv_ma} = {obv_ma_v:.0f}")
        ind_lines.append(f"    趋势        : {'向上(多头)' if last_obv > obv_ma_v else '向下(空头)'}  (差 {last_obv-obv_ma_v:+.0f})")

    # VR 指标
    if is_vr:
        try:
            parts = best_name.split("(")[1].split(")")[0].replace(",", "/").split("/")
            window, buy_vr, sell_vr = int(parts[0]), int(parts[1]), int(parts[2])
        except Exception:
            window, buy_vr, sell_vr = 26, 70, 150
        vr = calc_vr(close, df['volume'], window=window)
        last_vr = float(vr.iloc[-1])
        ind_lines.append(f"  VR({window},{buy_vr}/{sell_vr}):")
        ind_lines.append(f"    VR = {last_vr:.1f}")
        ind_lines.append(f"    状态        : {'超卖(<' + str(buy_vr) + '触发买入)' if last_vr < buy_vr else '超买(>' + str(sell_vr) + '触发卖出)' if last_vr > sell_vr else '中性'}")

    # Kelly 指标
    if is_kelly:
        try:
            parts = best_name.split("(")[1].split(")")[0].replace("-", ",").split(",")
            ma_s, ma_l, lookback = int(parts[0]), int(parts[1]), int(parts[2])
        except Exception:
            ma_s, ma_l, lookback = 5, 20, 60
        ma_short_v = float(calc_ma(close, ma_s).iloc[-1])
        ma_long_v = float(calc_ma(close, ma_l).iloc[-1])
        # 简化 Kelly 估算
        rets = close.pct_change()
        win_rate = float((rets > 0).rolling(lookback).mean().iloc[-1])
        avg_win = float(rets.clip(lower=0).rolling(lookback).mean().iloc[-1])
        avg_loss = float((-rets).clip(lower=0).rolling(lookback).mean().iloc[-1])
        kelly = (win_rate * (avg_win / avg_loss) - (1 - win_rate)) / (avg_win / avg_loss) if avg_loss > 0 else 0
        ind_lines.append(f"  Kelly({ma_s}-{ma_l},{lookback}):")
        ind_lines.append(f"    MA{ma_s} = {ma_short_v:.2f}   MA{ma_l} = {ma_long_v:.2f}  ({'金叉' if ma_short_v > ma_long_v else '死叉'})")
        ind_lines.append(f"    滚动胜率    : {win_rate:.1%}  (窗口 {lookback} 日)")
        ind_lines.append(f"    Kelly 分数  : {kelly:.3f}  ({'正(有优势,允许买入)' if kelly > 0 else '负(无优势,空仓)'})")

    # 唐奇安通道指标
    if is_donchian:
        try:
            parts = best_name.split("(")[1].split(")")[0].split("/")
            entry_n, exit_n = int(parts[0]), int(parts[1])
        except Exception:
            entry_n, exit_n = 20, 10
        upper = float(df['high'].rolling(entry_n).max().shift(1).iloc[-1])
        lower = float(df['low'].rolling(exit_n).min().shift(1).iloc[-1])
        ind_lines.append(f"  唐奇安({entry_n}/{exit_n}):")
        ind_lines.append(f"    突破上轨    : {upper:.2f}  (close {'已突破' if last_close > upper else '未突破'})")
        ind_lines.append(f"    跌破下轨    : {lower:.2f}  (close {'已跌破' if last_close < lower else '未跌破'})")

    # ADX 指标
    if is_adx:
        try:
            parts = best_name.split("(")[1].split(")")[0].split(",")
            period, threshold = int(parts[0]), int(parts[1])
        except Exception:
            period, threshold = 14, 25
        adx_s, plus_di_s, minus_di_s = calc_adx(df['high'], df['low'], close, period=period)
        adx_v, pdi, mdi = float(adx_s.iloc[-1]), float(plus_di_s.iloc[-1]), float(minus_di_s.iloc[-1])
        ind_lines.append(f"  ADX({period},{threshold}):")
        ind_lines.append(f"    ADX = {adx_v:.1f}  ({'强趋势(>' + str(threshold) + ')' if adx_v > threshold else '弱趋势(<' + str(threshold) + ')'})")
        ind_lines.append(f"    DI+ = {pdi:.1f}   DI- = {mdi:.1f}  ({'多头' if pdi > mdi else '空头'})")

    # ATR 通道指标
    if is_atr:
        try:
            parts = best_name.split("(")[1].split(")")[0].split(",")
            atr_p, ma_p, mult = int(parts[0]), int(parts[1]), float(parts[2])
        except Exception:
            atr_p, ma_p, mult = 14, 20, 3.0
        atr = calc_atr(df['high'], df['low'], close, period=atr_p)
        ma = close.rolling(ma_p).mean()
        last_atr = float(atr.iloc[-1])
        last_ma = float(ma.iloc[-1])
        upper = last_ma + mult * last_atr
        lower = last_ma - mult * last_atr
        ind_lines.append(f"  ATR通道({atr_p},{ma_p},{mult}):")
        ind_lines.append(f"    MA = {last_ma:.2f}   ATR = {last_atr:.2f}")
        ind_lines.append(f"    上轨 = {upper:.2f}  (close {'已突破' if last_close > upper else '未突破'})")
        ind_lines.append(f"    下轨 = {lower:.2f}  (close {'已跌破' if last_close < lower else '未跌破'})")

    # ===== P1 策略指标 =====
    from stock_analysis.indicators import (
        calc_ema, calc_kama, calc_hma, calc_vwap,
        calc_williams_r, calc_cci, calc_stoch_rsi, calc_sar,
    )

    # EMA 交叉
    if is_ema:
        try:
            parts = best_name.split("(")[1].split(")")[0].split("-")
            fast, slow = int(parts[0]), int(parts[1])
        except Exception:
            fast, slow = 5, 20
        ema_f = float(calc_ema(close, fast).iloc[-1])
        ema_s = float(calc_ema(close, slow).iloc[-1])
        ind_lines.append(f"  EMA交叉({fast}-{slow}):")
        ind_lines.append(f"    EMA{fast} = {ema_f:.2f}   EMA{slow} = {ema_s:.2f}  ({'金叉' if ema_f > ema_s else '死叉'})")
        ind_lines.append(f"    差值        : {ema_f-ema_s:+.2f}  ({'接近交叉' if abs(ema_f-ema_s) < 0.2 else '稳定'})")

    # KAMA
    if is_kama:
        try:
            period = int(best_name.split("(")[1].split(")")[0])
        except Exception:
            period = 10
        kama_v = float(calc_kama(close, period=period).iloc[-1])
        ind_lines.append(f"  KAMA({period}):")
        ind_lines.append(f"    KAMA = {kama_v:.2f}   close = {last_close:.2f}  ({'多头' if last_close > kama_v else '空头'})")
        ind_lines.append(f"    差值        : {last_close-kama_v:+.2f}")

    # HMA 交叉
    if is_hma:
        try:
            parts = best_name.split("(")[1].split(")")[0].split("-")
            fast, slow = int(parts[0]), int(parts[1])
        except Exception:
            fast, slow = 10, 30
        hma_f = float(calc_hma(close, fast).iloc[-1])
        hma_s = float(calc_hma(close, slow).iloc[-1])
        ind_lines.append(f"  HMA交叉({fast}-{slow}):")
        ind_lines.append(f"    HMA{fast} = {hma_f:.2f}   HMA{slow} = {hma_s:.2f}  ({'金叉' if hma_f > hma_s else '死叉'})")
        ind_lines.append(f"    差值        : {hma_f-hma_s:+.2f}")

    # VWAP
    if is_vwap:
        vwap_v = float(calc_vwap(df['high'], df['low'], close, df['volume']).iloc[-1])
        ind_lines.append(f"  VWAP:")
        ind_lines.append(f"    VWAP = {vwap_v:.2f}   close = {last_close:.2f}  ({'上方' if last_close > vwap_v else '下方'})")

    # 布林 Squeeze
    if is_squeeze:
        try:
            parts = best_name.split("(")[1].split(")")[0].split(",")
            w, std, sq = int(parts[0]), float(parts[1]), float(parts[2])
        except Exception:
            w, std, sq = 20, 2.0, 0.1
        upper, mid, lower = calc_boll(close, window=w, num_std=std)
        u, m, l_v = float(upper.iloc[-1]), float(mid.iloc[-1]), float(lower.iloc[-1])
        width = (u - l_v) / m if m > 0 else 0
        width_rank = float(close.rolling(w).apply(lambda x: (x.iloc[-1] - x.mean()) / x.std() if x.std() > 0 else 0).iloc[-1])
        ind_lines.append(f"  布林Squeeze({w},{std},{sq}):")
        ind_lines.append(f"    上轨 = {u:.2f}   中轨 = {m:.2f}   下轨 = {l_v:.2f}")
        ind_lines.append(f"    带宽 = {width:.4f}  (close {'已突破上轨' if last_close > u else '已跌破下轨' if last_close < l_v else '区间内'})")

    # ATR 过滤
    if is_atr_filter:
        try:
            parts = best_name.split("(")[1].split(")")[0].split(",")
            atr_p, ma_p, vh = int(parts[0]), int(parts[1]), float(parts[2])
        except Exception:
            atr_p, ma_p, vh = 14, 20, 0.05
        atr_v = float(calc_atr(df['high'], df['low'], close, period=atr_p).iloc[-1])
        atr_pct = atr_v / last_close
        ma_v = float(close.rolling(ma_p).mean().iloc[-1])
        ind_lines.append(f"  ATR过滤({atr_p},{ma_p},{vh}):")
        ind_lines.append(f"    ATR = {atr_v:.2f}   ATR% = {atr_pct:.4f}  ({'高波动(空仓)' if atr_pct > vh else '波动适中(可交易)'})")
        ind_lines.append(f"    MA{ma_p} = {ma_v:.2f}   close = {last_close:.2f}  ({'多头' if last_close > ma_v else '空头'})")

    # Z-Score
    if is_zscore:
        try:
            parts = best_name.split("(")[1].split(")")[0].split(",")
            w, ez, xz = int(parts[0]), float(parts[1]), float(parts[2])
        except Exception:
            w, ez, xz = 20, -2.0, 1.0
        ma_v = float(close.rolling(w).mean().iloc[-1])
        std_v = float(close.rolling(w).std().iloc[-1])
        z = (last_close - ma_v) / std_v if std_v > 0 else 0
        ind_lines.append(f"  Z-Score({w},{ez},{xz}):")
        ind_lines.append(f"    Z = {z:.2f}  ({'超卖(<' + str(ez) + '触发买入)' if z < ez else '超买(>' + str(xz) + '触发卖出)' if z > xz else '中性'})")
        ind_lines.append(f"    MA{w} = {ma_v:.2f}   STD = {std_v:.2f}")

    # Williams %R
    if is_wr:
        try:
            parts = best_name.split("(")[1].split(")")[0].replace(",", "/").split("/")
            p, bw, sw = int(parts[0]), int(parts[1]), int(parts[2])
        except Exception:
            p, bw, sw = 14, -80, -20
        wr_v = float(calc_williams_r(df['high'], df['low'], close, period=p).iloc[-1])
        ind_lines.append(f"  WilliamsR({p},{bw}/{sw}):")
        ind_lines.append(f"    WR = {wr_v:.2f}  ({'超卖(<' + str(bw) + '触发买入)' if wr_v < bw else '超买(>' + str(sw) + '触发卖出)' if wr_v > sw else '中性'})")

    # CCI
    if is_cci:
        try:
            parts = best_name.split("(")[1].split(")")[0].replace(",", "/").split("/")
            p, lo, up = int(parts[0]), int(parts[1]), int(parts[2])
        except Exception:
            p, lo, up = 14, -100, 100
        cci_v = float(calc_cci(df['high'], df['low'], close, period=p).iloc[-1])
        ind_lines.append(f"  CCI({p},{lo}/{up}):")
        ind_lines.append(f"    CCI = {cci_v:.2f}  ({'超卖(<' + str(lo) + '触发买入)' if cci_v < lo else '超买(>' + str(up) + '触发卖出)' if cci_v > up else '中性'})")

    # StochRSI
    if is_stochrsi:
        try:
            parts = best_name.split("(")[1].split(")")[0].replace(",", "/").split("/")
            p, lo, up = int(parts[0]), int(parts[1]), int(parts[2])
        except Exception:
            p, lo, up = 14, 20, 80
        stoch_v = float(calc_stoch_rsi(close, rsi_period=p, stoch_period=p).iloc[-1])
        ind_lines.append(f"  StochRSI({p},{lo}/{up}):")
        ind_lines.append(f"    StochRSI = {stoch_v:.2f}  ({'超卖(<' + str(lo) + '触发买入)' if stoch_v < lo else '超买(>' + str(up) + '触发卖出)' if stoch_v > up else '中性'})")

    # SAR
    if is_sar:
        try:
            parts = best_name.split("(")[1].split(")")[0].split("/")
            afs, afm = float(parts[0]), float(parts[1])
        except Exception:
            afs, afm = 0.02, 0.2
        sar_v = float(calc_sar(df['high'], df['low'], af_start=afs, af_max=afm).iloc[-1])
        ind_lines.append(f"  SAR({afs}/{afm}):")
        ind_lines.append(f"    SAR = {sar_v:.2f}   close = {last_close:.2f}  ({'多头(上方)' if last_close > sar_v else '空头(下方)'})")

    # DI 交叉
    if is_di:
        try:
            parts = best_name.split("(")[1].split(")")[0].split(",")
            p, th = int(parts[0]), int(parts[1])
        except Exception:
            p, th = 14, 20
        adx_v, pdi, mdi = calc_adx(df['high'], df['low'], close, period=p)
        adx_v, pdi, mdi = float(adx_v.iloc[-1]), float(pdi.iloc[-1]), float(mdi.iloc[-1])
        ind_lines.append(f"  DI交叉({p},{th}):")
        ind_lines.append(f"    ADX = {adx_v:.1f}  ({'强趋势' if adx_v > th else '弱趋势'})")
        ind_lines.append(f"    DI+ = {pdi:.1f}   DI- = {mdi:.1f}  ({'多头' if pdi > mdi else '空头'})")

    # ===== P2 策略指标 =====
    from stock_analysis.indicators import (
        calc_mfi, calc_cmf, detect_gap, detect_engulfing, detect_hammer, detect_doji,
    )

    # MFI 资金流量指数
    if is_mfi:
        try:
            parts = best_name.split("(")[1].split(")")[0].replace(",", "/").split("/")
            p, bm, sm = int(parts[0]), int(parts[1]), int(parts[2])
        except Exception:
            p, bm, sm = 14, 20, 80
        mfi_v = float(calc_mfi(df['high'], df['low'], close, df['volume'], period=p).iloc[-1])
        ind_lines.append(f"  MFI({p},{bm}/{sm}):")
        ind_lines.append(f"    MFI = {mfi_v:.1f}  ({'超卖(<' + str(bm) + '触发买入)' if mfi_v < bm else '超买(>' + str(sm) + '触发卖出)' if mfi_v > sm else '中性'})")

    # CMF 蔡金资金流
    if is_cmf:
        try:
            parts = best_name.split("(")[1].split(")")[0].replace(",", "/").split("/")
            p, bc, sc = int(parts[0]), float(parts[1]), float(parts[2])
        except Exception:
            p, bc, sc = 20, 0.1, -0.1
        cmf_v = float(calc_cmf(df['high'], df['low'], close, df['volume'], period=p).iloc[-1])
        ind_lines.append(f"  CMF({p},{bc}/{sc}):")
        ind_lines.append(f"    CMF = {cmf_v:.4f}  ({'资金流入(>' + str(bc) + '触发买入)' if cmf_v > bc else '资金流出(<' + str(sc) + '触发卖出)' if cmf_v < sc else '中性'})")

    # 缺口跳空
    if is_gap:
        try:
            gt = float(best_name.split("(")[1].split(")")[0])
        except Exception:
            gt = 0.01
        gap_up, gap_down = detect_gap(df, gap_threshold=gt)
        last_gu = bool(gap_up.iloc[-1])
        last_gd = bool(gap_down.iloc[-1])
        prev_high = float(df['high'].iloc[-2])
        prev_low = float(df['low'].iloc[-2])
        ind_lines.append(f"  缺口跳空({gt}):")
        ind_lines.append(f"    昨高 = {prev_high:.2f}   昨低 = {prev_low:.2f}")
        ind_lines.append(f"    今日        : {'跳空高开(买入)' if last_gu else '跳空低开(卖出)' if last_gd else '无缺口'}")

    # 吞没形态
    if is_engulfing:
        bull, bear = detect_engulfing(df)
        last_bull = bool(bull.iloc[-1])
        last_bear = bool(bear.iloc[-1])
        ind_lines.append(f"  吞没形态:")
        ind_lines.append(f"    今日        : {'看涨吞没(买入)' if last_bull else '看跌吞没(卖出)' if last_bear else '无吞没'}")

    # 锤子线
    if is_hammer:
        try:
            parts = best_name.split("(")[1].split(")")[0].split(",")
            br, sr = float(parts[0]), float(parts[1])
        except Exception:
            br, sr = 0.3, 2.0
        bull, bear = detect_hammer(df, body_ratio=br, shadow_ratio=sr)
        last_bull = bool(bull.iloc[-1])
        last_bear = bool(bear.iloc[-1])
        ind_lines.append(f"  锤子线({br},{sr}):")
        ind_lines.append(f"    今日        : {'锤子线(看涨买入)' if last_bull else '上吊线(看跌卖出)' if last_bear else '无形态'}")

    # 十字星
    if is_doji:
        try:
            parts = best_name.split("(")[1].split(")")[0].split(",")
            br, tw = float(parts[0]), int(parts[1])
        except Exception:
            br, tw = 0.1, 5
        doji = detect_doji(df, body_ratio=br)
        last_doji = bool(doji.iloc[-1])
        uptrend = bool(close.iloc[-1] > close.iloc[-1 - tw])
        ind_lines.append(f"  十字星({br},{tw}):")
        ind_lines.append(f"    今日        : {'十字星(反转信号)' if last_doji else '无形态'}  趋势: {'上涨(高位看跌)' if uptrend else '下跌(低位看涨)'}")

    # 等差网格
    if is_grid_arith:
        try:
            parts = best_name.split("(")[1].split(")")[0].split(",")
            mp, gs, gp = int(parts[0]), int(parts[1]), float(parts[2])
        except Exception:
            mp, gs, gp = 20, 5, 0.03
        last_ma = float(close.rolling(mp).mean().iloc[-1])
        grid_lower = last_ma - gs * gp * last_ma
        grid_upper = last_ma + gs * gp * last_ma
        ind_lines.append(f"  等差网格({mp},{gs},{gp}):")
        ind_lines.append(f"    中心 MA{mp}  = {last_ma:.2f}")
        ind_lines.append(f"    下网格      = {grid_lower:.2f}  (close {'已跌破(买入)' if last_close < grid_lower else '未跌破'})")
        ind_lines.append(f"    上网格      = {grid_upper:.2f}  (close {'已突破(卖出)' if last_close > grid_upper else '未突破'})")

    # 等比网格
    if is_grid_geom:
        try:
            parts = best_name.split("(")[1].split(")")[0].split(",")
            mp, gs, gp = int(parts[0]), int(parts[1]), float(parts[2])
        except Exception:
            mp, gs, gp = 20, 5, 0.03
        last_ma = float(close.rolling(mp).mean().iloc[-1])
        grid_lower = last_ma * ((1 - gp) ** gs)
        grid_upper = last_ma * ((1 + gp) ** gs)
        ind_lines.append(f"  等比网格({mp},{gs},{gp}):")
        ind_lines.append(f"    中心 MA{mp}  = {last_ma:.2f}")
        ind_lines.append(f"    下网格      = {grid_lower:.2f}  (close {'已跌破(买入)' if last_close < grid_lower else '未跌破'})")
        ind_lines.append(f"    上网格      = {grid_upper:.2f}  (close {'已突破(卖出)' if last_close > grid_upper else '未突破'})")

    # ATR 网格
    if is_grid_atr:
        try:
            parts = best_name.split("(")[1].split(")")[0].split(",")
            ap, mp, gm = int(parts[0]), int(parts[1]), float(parts[2])
        except Exception:
            ap, mp, gm = 14, 20, 2.0
        atr = calc_atr(df['high'], df['low'], close, period=ap)
        last_ma = float(close.rolling(mp).mean().iloc[-1])
        last_atr = float(atr.iloc[-1])
        grid_lower = last_ma - gm * last_atr
        grid_upper = last_ma + gm * last_atr
        ind_lines.append(f"  ATR网格({ap},{mp},{gm}):")
        ind_lines.append(f"    中心 MA{mp}  = {last_ma:.2f}   ATR = {last_atr:.2f}")
        ind_lines.append(f"    下网格      = {grid_lower:.2f}  (close {'已跌破(买入)' if last_close < grid_lower else '未跌破'})")
        ind_lines.append(f"    上网格      = {grid_upper:.2f}  (close {'已突破(卖出)' if last_close > grid_upper else '未突破'})")

    # 波动率目标
    if is_vol_target:
        try:
            parts = best_name.split("(")[1].split(")")[0].split(",")
            tv = float(parts[0])
            ms_ml = parts[1].split("-")
            ms, ml = int(ms_ml[0]), int(ms_ml[1])
        except Exception:
            tv, ms, ml = 0.15, 5, 20
        last_vol = float(close.pct_change().rolling(20).std().iloc[-1] * (252 ** 0.5))
        ma_s = float(close.rolling(ms).mean().iloc[-1])
        ma_l = float(close.rolling(ml).mean().iloc[-1])
        ind_lines.append(f"  波动率目标({tv},{ms}-{ml}):")
        ind_lines.append(f"    年化波动率  = {last_vol:.4f}  ({'低波动(可交易)' if last_vol < tv else '高波动(空仓)'})")
        ind_lines.append(f"    MA{ms} = {ma_s:.2f}   MA{ml} = {ma_l:.2f}  ({'金叉' if ma_s > ma_l else '死叉'})")

    # 配对交易
    if is_pairs:
        try:
            parts = best_name.split("(")[1].split(")")[0].split(",")
            w, ez, xz = int(parts[0]), float(parts[1]), float(parts[2])
        except Exception:
            w, ez, xz = 20, -1.5, 0.5
        spread = close / close.rolling(w).mean() - 1
        spread_ma = spread.rolling(w).mean()
        spread_std = spread.rolling(w).std().replace(0, float('nan'))
        z = (spread - spread_ma) / spread_std
        last_z = float(z.iloc[-1])
        ind_lines.append(f"  配对交易({w},{ez},{xz}):")
        ind_lines.append(f"    价差 Z      = {last_z:.2f}  ({'超卖(<' + str(ez) + '触发买入)' if last_z < ez else '超买(>' + str(xz) + '触发卖出)' if last_z > xz else '中性'})")

    # 价差 Z-Score
    if is_spread_z:
        try:
            parts = best_name.split("(")[1].split(")")[0].split(",")
            w, ez, xz = int(parts[0]), float(parts[1]), float(parts[2])
        except Exception:
            w, ez, xz = 20, -2.0, 0.0
        spread = close / close.rolling(w).mean()
        spread_ma = spread.rolling(w).mean()
        spread_std = spread.rolling(w).std().replace(0, float('nan'))
        z = (spread - spread_ma) / spread_std
        last_z = float(z.iloc[-1])
        ind_lines.append(f"  价差ZScore({w},{ez},{xz}):")
        ind_lines.append(f"    Z           = {last_z:.2f}  ({'超卖(<' + str(ez) + '触发买入)' if last_z < ez else '超买(>' + str(xz) + '触发卖出)' if last_z > xz else '中性'})")

    # 策略组合（多策略投票）
    if is_voting:
        try:
            vt = int(best_name.split("(")[1].split("票")[0])
        except Exception:
            vt = 2
        rsi_s = calc_rsi(close)
        macd_s, signal_s, _ = calc_macd(close)
        upper_s, _, lower_s = calc_boll(close)
        last_rsi = float(rsi_s.iloc[-1])
        last_macd = float(macd_s.iloc[-1])
        last_sig = float(signal_s.iloc[-1])
        last_lower = float(lower_s.iloc[-1])
        last_upper = float(upper_s.iloc[-1])
        prev_macd = float(macd_s.iloc[-2]) if len(macd_s) > 1 else last_macd
        prev_sig = float(signal_s.iloc[-2]) if len(signal_s) > 1 else last_sig
        votes_buy = int(last_rsi < 30) + int(last_macd > last_sig and prev_macd <= prev_sig) + int(last_close < last_lower)
        votes_sell = int(last_rsi > 70) + int(last_macd < last_sig and prev_macd >= prev_sig) + int(last_close > last_upper)
        ind_lines.append(f"  策略组合({vt}票):")
        ind_lines.append(f"    RSI = {last_rsi:.1f}  MACD金叉 = {'是' if last_macd > last_sig else '否'}  close vs 下轨 = {'之下' if last_close < last_lower else '之上'}")
        ind_lines.append(f"    买入票数    : {votes_buy}/{vt}  ({'触发买入' if votes_buy >= vt else '未触发'})")
        ind_lines.append(f"    卖出票数    : {votes_sell}/{vt}  ({'触发卖出' if votes_sell >= vt else '未触发'})")

    # 指数轮动门控
    if is_etf_rot:
        try:
            parts = best_name.split("(")[1].split(")")[0].split("-")
            ms, ml = int(parts[0]), int(parts[1])
        except Exception:
            ms, ml = 5, 20
        ma_s = float(close.rolling(ms).mean().iloc[-1])
        ma_l = float(close.rolling(ml).mean().iloc[-1])
        mom = float(close.iloc[-1] / close.iloc[-21] - 1) if len(close) > 21 else 0.0
        ind_lines.append(f"  指数轮动({ms}-{ml}):")
        ind_lines.append(f"    MA{ms} = {ma_s:.2f}   MA{ml} = {ma_l:.2f}  ({'金叉' if ma_s > ma_l else '死叉'})")
        ind_lines.append(f"    基准动量(20日): {mom:+.2%}  ({'门控开(允许做多)' if mom > 0 else '门控关(空仓)'})")

    # 风险平价组合
    if is_risk_par:
        try:
            w = int(best_name.split("(")[1].split(")")[0])
        except Exception:
            w = 20
        last_vol = float(close.pct_change().rolling(w).std().iloc[-1] * (252 ** 0.5))
        ind_lines.append(f"  风险平价({w}):")
        ind_lines.append(f"    年化波动率  = {last_vol:.4f}（逆波动加权 RSI/MACD/布林三子信号）")
        ind_lines.append(f"    综合得分    : {'买入区' if today_buy else '卖出区' if today_sell else '中性区'}")

    for line in ind_lines:
        print(line)

    # ========== 第三段：明日操作预案 ==========
    print("\n=== 明日操作预案 ===")
    plan_lines: list[str] = _build_tomorrow_plan(
        best_name, today_buy, today_sell, df, close
    )
    for line in plan_lines:
        print(line)


def _build_tomorrow_plan(
    best_name: str, today_buy: bool, today_sell: bool,
    df: pd.DataFrame, close: pd.Series
) -> list[str]:
    """根据策略类型和今日信号，构建明日操作预案。"""
    from stock_analysis.indicators import (
        calc_ma, calc_macd, calc_boll, calc_rsi, calc_cyc_cost, calc_winner_pct,
        calc_obv, calc_kdj, calc_vr, calc_atr, calc_adx,
        calc_ema, calc_kama, calc_hma, calc_vwap,
        calc_williams_r, calc_cci, calc_stoch_rsi, calc_sar,
        calc_mfi, calc_cmf, detect_gap, detect_engulfing, detect_hammer, detect_doji,
    )
    lines: list[str] = []
    last_close = float(close.iloc[-1])

    # 通用：确定当前持仓状态
    if today_buy:
        lines.append("  若今日已买入  : 持有，观察止损条件")
        lines.append("  若今日未操作  : 明日继续关注买入条件是否成立")
    elif today_sell:
        lines.append("  若今日已卖出  : 空仓观望，等待下次买入信号")
        lines.append("  若今日未操作  : 明日关注卖出/止损条件")
    else:
        lines.append("  今日无信号    : 维持当前持仓，观察信号触发条件")

    # 按策略类型补充具体观察价位和指标
    if best_name.startswith("均线趋势"):
        try:
            nums = best_name.split("(")[1].split(")")[0].split("-")
            s, m, l = int(nums[0]), int(nums[1]), int(nums[2])
        except Exception:
            s, m, l = 5, 13, 34
        ma_s = float(calc_ma(close, s).iloc[-1])
        ma_m = float(calc_ma(close, m).iloc[-1])
        ma_l = float(calc_ma(close, l).iloc[-1])
        lines.append(f"  明日观察价位  :")
        lines.append(f"    买入触发    : MA{s} 金叉 MA{m}（当前差 {ma_s - ma_m:+.2f}）且 close > MA{l}({ma_l:.2f})")
        lines.append(f"    卖出触发    : MA{s} 死叉 MA{m}（当前差 {ma_s - ma_m:+.2f}）")
        lines.append(f"    止损参考    : close 跌破 MA{l}({ma_l:.2f})")
        lines.append(f"  明日观察指标  : 量比是否放大（>1.0 为活跃），MA{s}/MA{m} 差值变化趋势")

    elif best_name.startswith("布林"):
        try:
            parts = best_name.split("(")[1].split(")")[0].split(",")
            boll_w, boll_std = int(parts[0]), float(parts[1])
        except Exception:
            boll_w, boll_std = 20, 2.0
        upper, mid, lower = calc_boll(close, window=boll_w, num_std=boll_std)
        u, lo = float(upper.iloc[-1]), float(lower.iloc[-1])
        lines.append(f"  明日观察价位  :")
        lines.append(f"    买入触发    : close 跌破下轨 {lo:.2f}")
        lines.append(f"    卖出触发    : close 突破上轨 {u:.2f}")
        lines.append(f"    止损参考    : close 跌破下轨 5% 以下")

    elif best_name == "量价突破":
        ma20 = float(calc_ma(close, 20).iloc[-1])
        rsi = float(calc_rsi(close).iloc[-1])
        lines.append(f"  明日观察价位  :")
        lines.append(f"    买入触发    : close > MA20({ma20:.2f}) 且放量(>1.5倍MA20) 且 RSI 50-80")
        lines.append(f"    卖出触发    : close < MA20({ma20:.2f})")
        lines.append(f"  明日观察指标  : RSI(当前 {rsi:.1f})、OBV 方向、量比")

    elif best_name.startswith("MACD"):
        try:
            parts = best_name.split("(")[1].split(")")[0].split("-")
            fast, slow, signal = int(parts[0]), int(parts[1]), int(parts[2])
        except Exception:
            fast, slow, signal = 12, 26, 9
        dif_s, dea_s, hist_s = calc_macd(close, fast=fast, slow=slow, signal=signal)
        dif, dea = float(dif_s.iloc[-1]), float(dea_s.iloc[-1])
        lines.append(f"  明日观察指标  :")
        lines.append(f"    买入触发    : DIF({dif:.4f}) 金叉 DEA({dea:.4f}) 且 DIF < 0（零轴下）")
        lines.append(f"    卖出触发    : DIF({dif:.4f}) 死叉 DEA({dea:.4f}) 且 DIF > 0（零轴上）")
        lines.append(f"    当前 DIF/DEA 差 : {dif-dea:+.4f}（{'接近交叉' if abs(dif-dea) < 0.02 else '稳定'})")

    elif best_name.startswith("筹码"):
        if 'turnover' in df.columns:
            to_rate = df['turnover'] / 100
        else:
            to_rate = pd.Series(0.01, index=df.index)
        cyc_cost = calc_cyc_cost(close, to_rate)
        winner = calc_winner_pct(close, cyc_cost)
        cc, wp = float(cyc_cost.iloc[-1]), float(winner.iloc[-1])
        # 从策略名解析阈值
        try:
            parts = best_name.split("(")[1].split(")")[0].split("-")
            wl, wu = int(parts[0]), int(parts[1])
        except Exception:
            wl, wu = 30, 85
        lines.append(f"  明日观察价位  :")
        lines.append(f"    买入触发    : close < 筹码成本*1.02({cc*1.02:.2f}) 且 获利盘 < {wl}%（当前 {wp:.1f}%）")
        lines.append(f"    卖出触发    : 获利盘 > {wu}%（当前 {wp:.1f}%）")
        lines.append(f"    关键价位    : 筹码成本 {cc:.2f}")

    elif best_name == "三维共振":
        # 综合：趋势 + 量价 + 筹码
        ma5 = float(calc_ma(close, 5).iloc[-1])
        ma13 = float(calc_ma(close, 13).iloc[-1])
        ma20 = float(calc_ma(close, 20).iloc[-1])
        rsi = float(calc_rsi(close).iloc[-1])
        if 'turnover' in df.columns:
            to_rate = df['turnover'] / 100
        else:
            to_rate = pd.Series(0.01, index=df.index)
        cyc_cost = calc_cyc_cost(close, to_rate)
        winner = calc_winner_pct(close, cyc_cost)
        cc, wp = float(cyc_cost.iloc[-1]), float(winner.iloc[-1])
        lines.append(f"  明日观察价位  :")
        lines.append(f"    买入触发    : MA5 金叉 MA13（差 {ma5-ma13:+.2f}）且 放量 且 获利盘 < 30%")
        lines.append(f"    卖出触发    : MA5 死叉 MA13 或 获利盘 > 85%")
        lines.append(f"    止损参考    : close 跌破 MA20({ma20:.2f}) 或 筹码成本 {cc:.2f}")
        lines.append(f"  明日观察指标  : RSI(当前 {rsi:.1f})、获利盘(当前 {wp:.1f}%)、量比")

    # ===== P0 策略明日预案 =====

    elif best_name.startswith("KDJ"):
        try:
            parts = best_name.split("(")[1].split(")")[0].replace(",", "/").split("/")
            n, buy_k, sell_k = int(parts[0]), int(parts[1]), int(parts[2])
        except Exception:
            n, buy_k, sell_k = 9, 20, 80
        k_s, d_s, _ = calc_kdj(df['high'], df['low'], close, n=n)
        k, d = float(k_s.iloc[-1]), float(d_s.iloc[-1])
        lines.append(f"  明日观察指标  :")
        lines.append(f"    买入触发    : K 金叉 D 且 K < {buy_k}（当前 K={k:.1f} D={d:.1f}）")
        lines.append(f"    卖出触发    : K 死叉 D 且 K > {sell_k}（当前 K={k:.1f}）")
        lines.append(f"    K/D 差值    : {k-d:+.1f}  ({'接近金叉' if -2 < k-d < 0 else '接近死叉' if 0 < k-d < 2 else '稳定'})")

    elif best_name.startswith("OBV"):
        try:
            obv_ma = int(best_name.split("(")[1].split(")")[0])
        except Exception:
            obv_ma = 20
        obv = calc_obv(close, df['volume'])
        obv_ma_v = float(obv.rolling(obv_ma).mean().iloc[-1])
        last_obv = float(obv.iloc[-1])
        lines.append(f"  明日观察指标  :")
        lines.append(f"    买入触发    : OBV 上穿 MA{obv_ma}（当前 OBV={last_obv:.0f} MA{obv_ma}={obv_ma_v:.0f}）")
        lines.append(f"    卖出触发    : OBV 下穿 MA{obv_ma}")
        lines.append(f"    OBV/MA 差值 : {last_obv-obv_ma_v:+.0f}")

    elif best_name.startswith("VR"):
        try:
            parts = best_name.split("(")[1].split(")")[0].replace(",", "/").split("/")
            window, buy_vr, sell_vr = int(parts[0]), int(parts[1]), int(parts[2])
        except Exception:
            window, buy_vr, sell_vr = 26, 70, 150
        vr = calc_vr(close, df['volume'], window=window)
        last_vr = float(vr.iloc[-1])
        lines.append(f"  明日观察指标  :")
        lines.append(f"    买入触发    : VR < {buy_vr}（当前 {last_vr:.1f}）")
        lines.append(f"    卖出触发    : VR > {sell_vr}（当前 {last_vr:.1f}）")

    elif best_name.startswith("Kelly"):
        try:
            parts = best_name.split("(")[1].split(")")[0].replace("-", ",").split(",")
            ma_s, ma_l, lookback = int(parts[0]), int(parts[1]), int(parts[2])
        except Exception:
            ma_s, ma_l, lookback = 5, 20, 60
        ma_short_v = float(calc_ma(close, ma_s).iloc[-1])
        ma_long_v = float(calc_ma(close, ma_l).iloc[-1])
        lines.append(f"  明日观察价位  :")
        lines.append(f"    买入触发    : MA{ma_s}({ma_short_v:.2f}) 金叉 MA{ma_l}({ma_long_v:.2f}) 且 Kelly 分数 > 0")
        lines.append(f"    卖出触发    : MA{ma_s} 死叉 MA{ma_l} 或 Kelly 分数转负")
        lines.append(f"    止损参考    : close 跌破 MA{ma_l}({ma_long_v:.2f})")

    elif best_name.startswith("唐奇安"):
        try:
            parts = best_name.split("(")[1].split(")")[0].split("/")
            entry_n, exit_n = int(parts[0]), int(parts[1])
        except Exception:
            entry_n, exit_n = 20, 10
        upper = float(df['high'].rolling(entry_n).max().shift(1).iloc[-1])
        lower = float(df['low'].rolling(exit_n).min().shift(1).iloc[-1])
        lines.append(f"  明日观察价位  :")
        lines.append(f"    买入触发    : close 突破 {entry_n} 日最高 {upper:.2f}")
        lines.append(f"    卖出触发    : close 跌破 {exit_n} 日最低 {lower:.2f}")
        lines.append(f"    止损参考    : close 跌破 {exit_n} 日最低 {lower:.2f}")

    elif best_name.startswith("ADX"):
        try:
            parts = best_name.split("(")[1].split(")")[0].split(",")
            period, threshold = int(parts[0]), int(parts[1])
        except Exception:
            period, threshold = 14, 25
        adx_s, plus_di_s, minus_di_s = calc_adx(df['high'], df['low'], close, period=period)
        adx_v, pdi, mdi = float(adx_s.iloc[-1]), float(plus_di_s.iloc[-1]), float(minus_di_s.iloc[-1])
        lines.append(f"  明日观察指标  :")
        lines.append(f"    买入触发    : ADX > {threshold}（当前 {adx_v:.1f}）且 DI+ > DI-（当前 {pdi:.1f}/{mdi:.1f}）")
        lines.append(f"    卖出触发    : ADX > {threshold} 且 DI- > DI+")
        lines.append(f"    注意        : ADX < {threshold} 时无趋势，不操作")

    elif best_name.startswith("ATR通道"):
        try:
            parts = best_name.split("(")[1].split(")")[0].split(",")
            atr_p, ma_p, mult = int(parts[0]), int(parts[1]), float(parts[2])
        except Exception:
            atr_p, ma_p, mult = 14, 20, 3.0
        atr = calc_atr(df['high'], df['low'], close, period=atr_p)
        ma = close.rolling(ma_p).mean()
        last_atr = float(atr.iloc[-1])
        last_ma = float(ma.iloc[-1])
        upper = last_ma + mult * last_atr
        lower = last_ma - mult * last_atr
        lines.append(f"  明日观察价位  :")
        lines.append(f"    买入触发    : close 突破上轨 {upper:.2f}（MA{ma_p}+{mult}×ATR）")
        lines.append(f"    卖出触发    : close 跌破下轨 {lower:.2f}")
        lines.append(f"    止损参考    : close 跌破下轨 {lower:.2f}")

    # ===== P1 策略明日预案 =====
    elif best_name.startswith("EMA交叉"):
        try:
            parts = best_name.split("(")[1].split(")")[0].split("-")
            fast, slow = int(parts[0]), int(parts[1])
        except Exception:
            fast, slow = 5, 20
        ema_f = float(calc_ema(close, fast).iloc[-1])
        ema_s = float(calc_ema(close, slow).iloc[-1])
        lines.append(f"  明日观察指标  :")
        lines.append(f"    买入触发    : EMA{fast}({ema_f:.2f}) 金叉 EMA{slow}({ema_s:.2f})")
        lines.append(f"    卖出触发    : EMA{fast} 死叉 EMA{slow}")
        lines.append(f"    差值        : {ema_f-ema_s:+.2f}")

    elif best_name.startswith("KAMA"):
        try:
            period = int(best_name.split("(")[1].split(")")[0])
        except Exception:
            period = 10
        kama_v = float(calc_kama(close, period=period).iloc[-1])
        lines.append(f"  明日观察价位  :")
        lines.append(f"    买入触发    : close 上穿 KAMA {kama_v:.2f}")
        lines.append(f"    卖出触发    : close 下穿 KAMA {kama_v:.2f}")

    elif best_name.startswith("HMA交叉"):
        try:
            parts = best_name.split("(")[1].split(")")[0].split("-")
            fast, slow = int(parts[0]), int(parts[1])
        except Exception:
            fast, slow = 10, 30
        hma_f = float(calc_hma(close, fast).iloc[-1])
        hma_s = float(calc_hma(close, slow).iloc[-1])
        lines.append(f"  明日观察指标  :")
        lines.append(f"    买入触发    : HMA{fast}({hma_f:.2f}) 金叉 HMA{slow}({hma_s:.2f})")
        lines.append(f"    卖出触发    : HMA{fast} 死叉 HMA{slow}")
        lines.append(f"    差值        : {hma_f-hma_s:+.2f}")

    elif best_name.startswith("VWAP"):
        vwap_v = float(calc_vwap(df['high'], df['low'], close, df['volume']).iloc[-1])
        lines.append(f"  明日观察价位  :")
        lines.append(f"    买入触发    : close 上穿 VWAP {vwap_v:.2f}")
        lines.append(f"    卖出触发    : close 下穿 VWAP {vwap_v:.2f}")

    elif best_name.startswith("布林Squeeze"):
        try:
            parts = best_name.split("(")[1].split(")")[0].split(",")
            w, std, sq = int(parts[0]), float(parts[1]), float(parts[2])
        except Exception:
            w, std, sq = 20, 2.0, 0.1
        upper, mid, lower = calc_boll(close, window=w, num_std=std)
        u, m, l_v = float(upper.iloc[-1]), float(mid.iloc[-1]), float(lower.iloc[-1])
        lines.append(f"  明日观察价位  :")
        lines.append(f"    买入触发    : close 突破上轨 {u:.2f}（带宽收缩后）")
        lines.append(f"    卖出触发    : close 跌破下轨 {l_v:.2f}")
        lines.append(f"    中轨参考    : {m:.2f}")

    elif best_name.startswith("ATR过滤"):
        try:
            parts = best_name.split("(")[1].split(")")[0].split(",")
            atr_p, ma_p, vh = int(parts[0]), int(parts[1]), float(parts[2])
        except Exception:
            atr_p, ma_p, vh = 14, 20, 0.05
        atr_v = float(calc_atr(df['high'], df['low'], close, period=atr_p).iloc[-1])
        ma_v = float(close.rolling(ma_p).mean().iloc[-1])
        atr_pct = atr_v / float(close.iloc[-1])
        lines.append(f"  明日观察指标  :")
        lines.append(f"    买入触发    : close 上穿 MA{ma_p}({ma_v:.2f}) 且 ATR% < {vh}（当前 {atr_pct:.4f}）")
        lines.append(f"    卖出触发    : close 跌破 MA{ma_p} 或 ATR% > {vh}（高波动空仓）")

    elif best_name.startswith("Z-Score"):
        try:
            parts = best_name.split("(")[1].split(")")[0].split(",")
            w, ez, xz = int(parts[0]), float(parts[1]), float(parts[2])
        except Exception:
            w, ez, xz = 20, -2.0, 1.0
        ma_v = float(close.rolling(w).mean().iloc[-1])
        std_v = float(close.rolling(w).std().iloc[-1])
        z = (float(close.iloc[-1]) - ma_v) / std_v if std_v > 0 else 0
        entry_price = ma_v + ez * std_v
        exit_price = ma_v + xz * std_v
        lines.append(f"  明日观察价位  :")
        lines.append(f"    买入触发    : Z < {ez}（当前 {z:.2f}，对应价格 < {entry_price:.2f}）")
        lines.append(f"    卖出触发    : Z > {xz}（对应价格 > {exit_price:.2f}）")
        lines.append(f"    均值回归    : MA{w} = {ma_v:.2f}")

    elif best_name.startswith("WilliamsR"):
        try:
            parts = best_name.split("(")[1].split(")")[0].replace(",", "/").split("/")
            p, bw, sw = int(parts[0]), int(parts[1]), int(parts[2])
        except Exception:
            p, bw, sw = 14, -80, -20
        wr_v = float(calc_williams_r(df['high'], df['low'], close, period=p).iloc[-1])
        lines.append(f"  明日观察指标  :")
        lines.append(f"    买入触发    : WR 上穿 {bw}（当前 {wr_v:.2f}）")
        lines.append(f"    卖出触发    : WR 下穿 {sw}（当前 {wr_v:.2f}）")

    elif best_name.startswith("CCI"):
        try:
            parts = best_name.split("(")[1].split(")")[0].replace(",", "/").split("/")
            p, lo, up = int(parts[0]), int(parts[1]), int(parts[2])
        except Exception:
            p, lo, up = 14, -100, 100
        cci_v = float(calc_cci(df['high'], df['low'], close, period=p).iloc[-1])
        lines.append(f"  明日观察指标  :")
        lines.append(f"    买入触发    : CCI 上穿 {lo}（当前 {cci_v:.2f}）")
        lines.append(f"    卖出触发    : CCI 下穿 {up}（当前 {cci_v:.2f}）")

    elif best_name.startswith("StochRSI"):
        try:
            parts = best_name.split("(")[1].split(")")[0].replace(",", "/").split("/")
            p, lo, up = int(parts[0]), int(parts[1]), int(parts[2])
        except Exception:
            p, lo, up = 14, 20, 80
        stoch_v = float(calc_stoch_rsi(close, rsi_period=p, stoch_period=p).iloc[-1])
        lines.append(f"  明日观察指标  :")
        lines.append(f"    买入触发    : StochRSI 上穿 {lo}（当前 {stoch_v:.2f}）")
        lines.append(f"    卖出触发    : StochRSI 下穿 {up}（当前 {stoch_v:.2f}）")

    elif best_name.startswith("SAR"):
        try:
            parts = best_name.split("(")[1].split(")")[0].split("/")
            afs, afm = float(parts[0]), float(parts[1])
        except Exception:
            afs, afm = 0.02, 0.2
        sar_v = float(calc_sar(df['high'], df['low'], af_start=afs, af_max=afm).iloc[-1])
        lines.append(f"  明日观察价位  :")
        lines.append(f"    买入触发    : close 上穿 SAR {sar_v:.2f}")
        lines.append(f"    卖出触发    : close 下穿 SAR {sar_v:.2f}（SAR 跟踪止损）")

    elif best_name.startswith("DI交叉"):
        try:
            parts = best_name.split("(")[1].split(")")[0].split(",")
            p, th = int(parts[0]), int(parts[1])
        except Exception:
            p, th = 14, 20
        adx_v, pdi, mdi = calc_adx(df['high'], df['low'], close, period=p)
        adx_v, pdi, mdi = float(adx_v.iloc[-1]), float(pdi.iloc[-1]), float(mdi.iloc[-1])
        lines.append(f"  明日观察指标  :")
        lines.append(f"    买入触发    : DI+ 上穿 DI- 且 ADX > {th}（当前 DI+={pdi:.1f} DI-={mdi:.1f} ADX={adx_v:.1f}）")
        lines.append(f"    卖出触发    : DI+ 下穿 DI-")
        lines.append(f"    注意        : ADX < {th} 时无趋势，不操作")

    # ===== P2 策略明日预案 =====
    elif best_name.startswith("MFI"):
        try:
            parts = best_name.split("(")[1].split(")")[0].replace(",", "/").split("/")
            p, bm, sm = int(parts[0]), int(parts[1]), int(parts[2])
        except Exception:
            p, bm, sm = 14, 20, 80
        mfi_v = float(calc_mfi(df['high'], df['low'], close, df['volume'], period=p).iloc[-1])
        lines.append(f"  明日观察指标  :")
        lines.append(f"    买入触发    : MFI 上穿 {bm}（当前 {mfi_v:.1f}）")
        lines.append(f"    卖出触发    : MFI 下穿 {sm}（当前 {mfi_v:.1f}）")

    elif best_name.startswith("CMF"):
        try:
            parts = best_name.split("(")[1].split(")")[0].replace(",", "/").split("/")
            p, bc, sc = int(parts[0]), float(parts[1]), float(parts[2])
        except Exception:
            p, bc, sc = 20, 0.1, -0.1
        cmf_v = float(calc_cmf(df['high'], df['low'], close, df['volume'], period=p).iloc[-1])
        lines.append(f"  明日观察指标  :")
        lines.append(f"    买入触发    : CMF 上穿 {bc}（当前 {cmf_v:.4f}，资金流入）")
        lines.append(f"    卖出触发    : CMF 下穿 {sc}（当前 {cmf_v:.4f}，资金流出）")

    elif best_name.startswith("缺口跳空"):
        try:
            gt = float(best_name.split("(")[1].split(")")[0])
        except Exception:
            gt = 0.01
        prev_high = float(df['high'].iloc[-2])
        prev_low = float(df['low'].iloc[-2])
        lines.append(f"  明日观察价位  :")
        lines.append(f"    买入触发    : 开盘价 > 昨高 {prev_high:.2f} × (1+{gt}) = {prev_high*(1+gt):.2f}（跳空高开）")
        lines.append(f"    卖出触发    : 开盘价 < 昨低 {prev_low:.2f} × (1-{gt}) = {prev_low*(1-gt):.2f}（跳空低开）")

    elif best_name == "吞没形态":
        bull, bear = detect_engulfing(df)
        last_bull = bool(bull.iloc[-1])
        last_bear = bool(bear.iloc[-1])
        lines.append(f"  明日观察形态  :")
        lines.append(f"    买入触发    : 出现看涨吞没（今日实体包住昨日阴线实体）")
        lines.append(f"    卖出触发    : 出现看跌吞没（今日实体包住昨日阳线实体）")
        if last_bull:
            lines.append(f"    今日已触发  : 看涨吞没，明日持有")
        elif last_bear:
            lines.append(f"    今日已触发  : 看跌吞没，明日关注是否确认")

    elif best_name.startswith("锤子线"):
        try:
            parts = best_name.split("(")[1].split(")")[0].split(",")
            br, sr = float(parts[0]), float(parts[1])
        except Exception:
            br, sr = 0.3, 2.0
        bull, bear = detect_hammer(df, body_ratio=br, shadow_ratio=sr)
        last_bull = bool(bull.iloc[-1])
        last_bear = bool(bear.iloc[-1])
        lines.append(f"  明日观察形态  :")
        lines.append(f"    买入触发    : 下跌趋势中出现锤子线（小实体 + 长下影）")
        lines.append(f"    卖出触发    : 上涨趋势中出现上吊线（小实体 + 长下影）")
        if last_bull:
            lines.append(f"    今日已触发  : 锤子线，明日关注是否反转上行")

    elif best_name.startswith("十字星"):
        try:
            parts = best_name.split("(")[1].split(")")[0].split(",")
            br, tw = float(parts[0]), int(parts[1])
        except Exception:
            br, tw = 0.1, 5
        doji = detect_doji(df, body_ratio=br)
        last_doji = bool(doji.iloc[-1])
        uptrend = bool(close.iloc[-1] > close.iloc[-1 - tw])
        lines.append(f"  明日观察形态  :")
        lines.append(f"    买入触发    : 下跌趋势中出现十字星（反转上行）")
        lines.append(f"    卖出触发    : 上涨趋势中出现十字星（反转向下）")
        if last_doji:
            lines.append(f"    今日已触发  : 十字星，{'高位注意见顶' if uptrend else '低位关注反弹'}")

    elif best_name.startswith("等差网格"):
        try:
            parts = best_name.split("(")[1].split(")")[0].split(",")
            mp, gs, gp = int(parts[0]), int(parts[1]), float(parts[2])
        except Exception:
            mp, gs, gp = 20, 5, 0.03
        last_ma = float(close.rolling(mp).mean().iloc[-1])
        grid_lower = last_ma - gs * gp * last_ma
        grid_upper = last_ma + gs * gp * last_ma
        lines.append(f"  明日观察价位  :")
        lines.append(f"    买入触发    : close 跌破下网格 {grid_lower:.2f}（MA{mp} - {gs}×{gp}）")
        lines.append(f"    卖出触发    : close 突破上网格 {grid_upper:.2f}（MA{mp} + {gs}×{gp}）")
        lines.append(f"    中心参考    : MA{mp} = {last_ma:.2f}")

    elif best_name.startswith("等比网格"):
        try:
            parts = best_name.split("(")[1].split(")")[0].split(",")
            mp, gs, gp = int(parts[0]), int(parts[1]), float(parts[2])
        except Exception:
            mp, gs, gp = 20, 5, 0.03
        last_ma = float(close.rolling(mp).mean().iloc[-1])
        grid_lower = last_ma * ((1 - gp) ** gs)
        grid_upper = last_ma * ((1 + gp) ** gs)
        lines.append(f"  明日观察价位  :")
        lines.append(f"    买入触发    : close 跌破下网格 {grid_lower:.2f}（MA{mp} × (1-{gp})^{gs}）")
        lines.append(f"    卖出触发    : close 突破上网格 {grid_upper:.2f}（MA{mp} × (1+{gp})^{gs}）")
        lines.append(f"    中心参考    : MA{mp} = {last_ma:.2f}")

    elif best_name.startswith("ATR网格"):
        try:
            parts = best_name.split("(")[1].split(")")[0].split(",")
            ap, mp, gm = int(parts[0]), int(parts[1]), float(parts[2])
        except Exception:
            ap, mp, gm = 14, 20, 2.0
        atr = calc_atr(df['high'], df['low'], close, period=ap)
        last_ma = float(close.rolling(mp).mean().iloc[-1])
        last_atr = float(atr.iloc[-1])
        grid_lower = last_ma - gm * last_atr
        grid_upper = last_ma + gm * last_atr
        lines.append(f"  明日观察价位  :")
        lines.append(f"    买入触发    : close 跌破下网格 {grid_lower:.2f}（MA{mp} - {gm}×ATR）")
        lines.append(f"    卖出触发    : close 突破上网格 {grid_upper:.2f}（MA{mp} + {gm}×ATR）")
        lines.append(f"    中心参考    : MA{mp} = {last_ma:.2f}   ATR = {last_atr:.2f}")

    elif best_name.startswith("波动率目标"):
        try:
            parts = best_name.split("(")[1].split(")")[0].split(",")
            tv = float(parts[0])
            ms_ml = parts[1].split("-")
            ms, ml = int(ms_ml[0]), int(ms_ml[1])
        except Exception:
            tv, ms, ml = 0.15, 5, 20
        last_vol = float(close.pct_change().rolling(20).std().iloc[-1] * (252 ** 0.5))
        ma_s = float(close.rolling(ms).mean().iloc[-1])
        ma_l = float(close.rolling(ml).mean().iloc[-1])
        lines.append(f"  明日观察指标  :")
        lines.append(f"    买入触发    : 波动率 < {tv}（当前 {last_vol:.4f}）且 MA{ms}({ma_s:.2f}) 金叉 MA{ml}({ma_l:.2f})")
        lines.append(f"    卖出触发    : 波动率 > {tv} 或 MA{ms} 死叉 MA{ml}")
        lines.append(f"    注意        : 高波动时空仓回避")

    elif best_name.startswith("配对交易"):
        try:
            parts = best_name.split("(")[1].split(")")[0].split(",")
            w, ez, xz = int(parts[0]), float(parts[1]), float(parts[2])
        except Exception:
            w, ez, xz = 20, -1.5, 0.5
        spread = close / close.rolling(w).mean() - 1
        spread_ma = spread.rolling(w).mean()
        spread_std = spread.rolling(w).std().replace(0, float('nan'))
        z = (spread - spread_ma) / spread_std
        last_z = float(z.iloc[-1])
        ma_v = float(close.rolling(w).mean().iloc[-1])
        lines.append(f"  明日观察指标  :")
        lines.append(f"    买入触发    : 价差 Z < {ez}（当前 {last_z:.2f}，超卖）")
        lines.append(f"    卖出触发    : 价差 Z > {xz}（当前 {last_z:.2f}，回归均值）")
        lines.append(f"    均值参考    : MA{w} = {ma_v:.2f}  close = {last_close:.2f}  偏离 {(last_close-ma_v)/ma_v*100:+.1f}%")

    elif best_name.startswith("价差ZScore"):
        try:
            parts = best_name.split("(")[1].split(")")[0].split(",")
            w, ez, xz = int(parts[0]), float(parts[1]), float(parts[2])
        except Exception:
            w, ez, xz = 20, -2.0, 0.0
        spread = close / close.rolling(w).mean()
        spread_ma = spread.rolling(w).mean()
        spread_std = spread.rolling(w).std().replace(0, float('nan'))
        z = (spread - spread_ma) / spread_std
        last_z = float(z.iloc[-1])
        ma_v = float(close.rolling(w).mean().iloc[-1])
        lines.append(f"  明日观察指标  :")
        lines.append(f"    买入触发    : Z < {ez}（当前 {last_z:.2f}，超卖）")
        lines.append(f"    卖出触发    : Z > {xz}（当前 {last_z:.2f}，回归均值）")
        lines.append(f"    均值参考    : MA{w} = {ma_v:.2f}")

    elif best_name.startswith("策略组合"):
        try:
            vt = int(best_name.split("(")[1].split("票")[0])
        except Exception:
            vt = 2
        rsi_s = calc_rsi(close)
        macd_s, signal_s, _ = calc_macd(close)
        upper_s, _, lower_s = calc_boll(close)
        last_rsi = float(rsi_s.iloc[-1])
        last_macd = float(macd_s.iloc[-1])
        last_sig = float(signal_s.iloc[-1])
        last_lower = float(lower_s.iloc[-1])
        last_upper = float(upper_s.iloc[-1])
        lines.append(f"  明日观察指标  :")
        lines.append(f"    买入触发    : RSI < 30 + MACD 金叉 + close < 布林下轨，≥ {vt} 票")
        lines.append(f"    卖出触发    : RSI > 70 + MACD 死叉 + close > 布林上轨，≥ {vt} 票")
        lines.append(f"    当前状态    : RSI={last_rsi:.1f}  MACD{'金叉' if last_macd > last_sig else '死叉'}  close{'<下轨' if last_close < last_lower else '>上轨' if last_close > last_upper else '区间内'}")

    elif best_name.startswith("指数轮动"):
        try:
            parts = best_name.split("(")[1].split(")")[0].split("-")
            ms, ml = int(parts[0]), int(parts[1])
        except Exception:
            ms, ml = 5, 20
        ma_s = float(close.rolling(ms).mean().iloc[-1])
        ma_l = float(close.rolling(ml).mean().iloc[-1])
        lines.append(f"  明日观察价位  :")
        lines.append(f"    买入触发    : MA{ms}({ma_s:.2f}) 金叉 MA{ml}({ma_l:.2f}) 且基准动量转正")
        lines.append(f"    卖出触发    : MA{ms} 死叉 MA{ml} 或基准动量转负（门控关）")

    elif best_name.startswith("风险平价"):
        lines.append(f"  明日观察指标  :")
        lines.append(f"    买入触发    : 逆波动综合得分上穿阈值（RSI/MACD/布林等权风险）")
        lines.append(f"    卖出触发    : 综合得分下穿阈值")

    return lines


def run_stock_tournament(
    code: str, start_date: str, use_regime: bool, detail: bool, jobs: int = 1,
) -> None:
    name = get_stock_name(code) or code
    print(f"\n=== 股票: {name} ({code}) ===")
    df = get_stock_daily(code, start_date=start_date, source="sina")
    if df is None or len(df) < 100:
        print(f"数据不足（{len(df) if df is not None else 0} 条），至少需要 100 条")
        return
    print(f"数据区间: {df.index[0].date()} ~ {df.index[-1].date()}（{len(df)} 条）")

    regimes = None
    if use_regime:
        print("加载市场环境...")
        regimes = get_market_regime_series(start_date)
        if regimes is None:
            print("市场环境加载失败，将不分牛熊震荡统计")
        else:
            print("市场环境加载完成")

    print(f"运行多策略锦标赛... (jobs={jobs})\n")
    # 单标的回测允许全仓进出
    config = BacktestConfig(max_position_pct=1.0)
    best_name, best_result, comparison = run_strategy_tournament(df, regimes, config, jobs=jobs)

    r = best_result
    print(f">>> 最优策略: {best_name}")
    if "无优势" in best_name:
        print("    提示        : 未跑赢买入持有/空仓基线，建议观望，勿强行交易")
    print(f"    胜率      : {r.win_rate:.2%}")
    print(f"    盈亏比    : {r.profit_factor:.2f}")
    print(f"    年化收益  : {r.annual_return:.2%}")
    print(f"    最大回撤  : {r.max_drawdown:.2%}")
    print(f"    夏普比率  : {r.sharpe_ratio:.2f}")
    print(f"    交易次数  : {r.trade_count}")
    print(f"    平均持仓  : {r.avg_holding_days:.1f} 天")
    if r.bull_win_rate or r.bear_win_rate or r.shock_win_rate:
        print(
            f"    分市场胜率: 牛{r.bull_win_rate:.0%} / 熊{r.bear_win_rate:.0%} / "
            f"震荡{r.shock_win_rate:.0%}"
        )

    # 当前操作建议（兼容“（无优势，建议观望）”后缀）
    strategies = _build_stock_strategies()
    _lookup = best_name.replace("（无优势，建议观望）", "")
    if _lookup in strategies:
        _print_detailed_stock_advice(strategies[_lookup], df, _lookup)

    if detail:
        print("\n=== 全策略对比（按样本外Final降序，P1默认看样本外）===")
        cols = ["胜率", "年化收益", "最大回撤", "盈亏比", "交易次数", "评分IS", "评分OOS", "Calmar_OOS", "交易数_OOS", "评分"]
        cols = [c for c in cols if c in comparison.columns]
        print(comparison[cols].sort_values("评分", ascending=False).head(30).to_string(float_format=lambda x: f"{x:.4f}"))


def _print_fund_current_advice(df: pd.DataFrame, best_name: str) -> None:
    """基金当前操作建议 + 明日预案：支持 RSI 波段 / MA20 持有 / 双均线 / MACD / 波动率目标 / 指数轮动。"""
    last_date = df.index[-1]
    last_close = float(df['close'].iloc[-1])
    last_rsi = float(df['RSI'].iloc[-1]) if 'RSI' in df else 0.0
    last_ma20 = float(df['MA20'].iloc[-1]) if 'MA20' in df else last_close
    last_ma60 = float(df['MA60'].iloc[-1]) if 'MA60' in df else last_close

    print("\n=== 当前操作建议 ===")
    print(f"    最新价      : {last_close:.4f} ({last_date.date()})")
    print(f"    最新 RSI    : {last_rsi:.1f}")
    print(f"    MA20        : {last_ma20:.4f}")
    print(f"    MA60        : {last_ma60:.4f}")

    # ===== P2 基金策略：双均线 / MACD / 波动率目标 =====
    if best_name.startswith("双均线"):
        try:
            parts = best_name.split("(")[1].split(")")[0].split("-")
            short, long = int(parts[0]), int(parts[1])
        except Exception:
            short, long = 20, 60
        ma_s = float(df['close'].rolling(short).mean().iloc[-1])
        ma_l = float(df['close'].rolling(long).mean().iloc[-1])
        gold = ma_s > ma_l
        prev_ms = float(df['close'].rolling(short).mean().iloc[-2]) if len(df) > 1 else ma_s
        prev_ml = float(df['close'].rolling(long).mean().iloc[-2]) if len(df) > 1 else ma_l
        just_gold = (ma_s > ma_l) and (prev_ms <= prev_ml)
        just_dead = (ma_s < ma_l) and (prev_ms >= prev_ml)
        print(f"    MA{short}        : {ma_s:.4f}")
        print(f"    MA{long:<8}: {ma_l:.4f}  ({'金叉' if gold else '死叉'})")
        if just_gold:
            print(f"    今日信号    : 买入（MA{short} 刚金叉 MA{long}）")
            print("    建议操作    : 按最新价买入/申购")
        elif just_dead:
            print(f"    今日信号    : 卖出（MA{short} 刚死叉 MA{long}）")
            print("    建议操作    : 按最新价卖出/赎回")
        elif gold:
            print(f"    今日信号    : 持有（MA{short} 在 MA{long} 之上）")
            print("    建议操作    : 持有不动，等待死叉再赎回")
        else:
            print(f"    今日信号    : 观望（MA{short} 在 MA{long} 之下）")
            print("    建议操作    : 空仓，等待金叉再买入")
        print("\n=== 明日操作预案 ===")
        if gold:
            print("  若今日已持有  : 继续持有，观察 MA{short} 是否死叉 MA{long}".format(short=short, long=long))
            print(f"  若今日未操作  : 明日 MA{short} 仍在 MA{long} 之上则可买入")
            print(f"  止损参考      : close 跌破 MA{long}({ma_l:.4f}) 或 MA{short} 死叉 MA{long}")
        else:
            print(f"  若今日未操作  : 明日 MA{short} 上穿 MA{long}({ma_l:.4f}) 则买入")
            print(f"  回补参考      : MA{short} > MA{long} 且 close > MA{short}({ma_s:.4f})")
        print(f"  趋势参考      : close vs MA60({last_ma60:.4f})  {'之上(多头)' if last_close > last_ma60 else '之下(空头)'}")
        return

    if best_name.startswith("MACD"):
        try:
            parts = best_name.split("(")[1].split(")")[0].split("-")
            fast, slow, signal = int(parts[0]), int(parts[1]), int(parts[2])
        except Exception:
            fast, slow, signal = 12, 26, 9
        close = df['close']
        exp1 = close.ewm(span=fast, adjust=False).mean()
        exp2 = close.ewm(span=slow, adjust=False).mean()
        macd = exp1 - exp2
        signal_line = macd.ewm(span=signal, adjust=False).mean()
        last_macd = float(macd.iloc[-1])
        last_sig = float(signal_line.iloc[-1])
        prev_macd = float(macd.iloc[-2]) if len(macd) > 1 else last_macd
        prev_sig = float(signal_line.iloc[-2]) if len(signal_line) > 1 else last_sig
        gold = last_macd > last_sig
        just_gold = gold and (prev_macd <= prev_sig)
        just_dead = (not gold) and (prev_macd >= prev_sig)
        print(f"    MACD({fast},{slow},{signal}):")
        print(f"    DIF         : {last_macd:.6f}")
        print(f"    DEA         : {last_sig:.6f}  ({'金叉' if gold else '死叉'})")
        print(f"    DIF 位置    : {'零轴上(多头)' if last_macd > 0 else '零轴下(空头)'}")
        if just_gold:
            print(f"    今日信号    : 买入（MACD 刚金叉）")
            print("    建议操作    : 按最新价买入/申购")
        elif just_dead:
            print(f"    今日信号    : 卖出（MACD 刚死叉）")
            print("    建议操作    : 按最新价卖出/赎回")
        elif gold:
            print(f"    今日信号    : 持有（MACD 金叉状态）")
            print("    建议操作    : 持有不动，等待死叉再赎回")
        else:
            print(f"    今日信号    : 观望（MACD 死叉状态）")
            print("    建议操作    : 空仓，等待金叉再买入")
        print("\n=== 明日操作预案 ===")
        if gold:
            print(f"  若今日已持有  : 继续持有，观察 DIF 是否死叉 DEA")
            print(f"  若今日未操作  : 明日 DIF 仍在 DEA 之上则可买入")
            print(f"  止损参考      : DIF({last_macd:.6f}) 跌破 DEA({last_sig:.6f}) 或 DIF 转负")
        else:
            print(f"  若今日未操作  : 明日 DIF 上穿 DEA({last_sig:.6f}) 则买入")
            print(f"  回补参考      : DIF > DEA 且 DIF > 0（零轴上方金叉更可靠）")
        print(f"  趋势参考      : close vs MA20({last_ma20:.4f})  {'之上' if last_close > last_ma20 else '之下'}")
        return

    if best_name.startswith("波动率目标"):
        try:
            parts = best_name.split("(")[1].split(")")[0].split(",")
            vol_window = int(parts[0])
            target_vol = float(parts[1])
        except Exception:
            vol_window, target_vol = 20, 0.15
        close = df['close']
        vol = close.pct_change().rolling(vol_window).std() * (252 ** 0.5)
        last_vol = float(vol.iloc[-1])
        last_ma = float(close.rolling(20).mean().iloc[-1])
        vol_ok = last_vol < target_vol
        above_ma = last_close > last_ma
        print(f"    年化波动率  : {last_vol:.4f}  (目标 {target_vol})")
        print(f"    MA20        : {last_ma:.4f}  close {'之上' if above_ma else '之下'}")
        if vol_ok and above_ma:
            print(f"    今日信号    : 持有（低波动 + close 在 MA20 之上）")
            print("    建议操作    : 持有不动")
        elif not vol_ok:
            print(f"    今日信号    : 减仓/空仓（波动率 {last_vol:.4f} > 目标 {target_vol}）")
            print("    建议操作    : 高波动，考虑赎回减仓")
        else:
            print(f"    今日信号    : 观望（close 在 MA20 之下）")
            print("    建议操作    : 空仓，等待 close 重新站上 MA20 且波动率回落")
        print("\n=== 明日操作预案 ===")
        if vol_ok and above_ma:
            print(f"  若今日已持有  : 继续持有，观察波动率是否超 {target_vol}")
            print(f"  止损参考      : close 跌破 MA20({last_ma:.4f}) 或 波动率 > {target_vol}")
        elif not vol_ok:
            print(f"  若今日已赎回  : 观望，等待波动率回落至 {target_vol} 以下")
            print(f"  回补参考      : 波动率 < {target_vol} 且 close > MA20({last_ma:.4f})")
        else:
            print(f"  若今日未操作  : 明日 close > MA20({last_ma:.4f}) 且 波动率 < {target_vol} 则买入")
            print(f"  注意          : 波动率当前 {last_vol:.4f}，{'> 目标(高波动空仓)' if not vol_ok else '< 目标(可交易)'}")
        return

    if best_name.startswith("指数轮动"):
        try:
            parts = best_name.split("(")[1].split(")")[0].split("-")
            short, long = int(parts[0]), int(parts[1])
        except Exception:
            short, long = 10, 60
        ma_s = float(df['close'].rolling(short).mean().iloc[-1])
        ma_l = float(df['close'].rolling(long).mean().iloc[-1])
        mom = float(last_close / float(df['close'].iloc[-21]) - 1) if len(df) > 21 else 0.0
        gold = ma_s > ma_l
        print(f"    MA{short}        : {ma_s:.4f}")
        print(f"    MA{long:<8}: {ma_l:.4f}  ({'金叉' if gold else '死叉'})")
        print(f"    基准动量(20日): {mom:+.2%}  ({'门控开' if mom > 0 else '门控关(空仓)'})")
        if gold and mom > 0:
            print(f"    今日信号    : 持有（金叉 + 门控开）")
            print("    建议操作    : 持有不动")
        elif not gold:
            print(f"    今日信号    : 观望（MA{short} 在 MA{long} 之下）")
            print("    建议操作    : 空仓，等待金叉且门控开再买入")
        else:
            print(f"    今日信号    : 观望（门控关）")
            print("    建议操作    : 空仓，等待基准动量转正")
        print("\n=== 明日操作预案 ===")
        print(f"  买入触发      : MA{short} 金叉 MA{long} 且基准动量 > 0")
        print(f"  卖出触发      : MA{short} 死叉 MA{long} 或基准动量 < 0")
        return

    if best_name.startswith("持有"):
        # 持有(MA20)：价格在 MA20 之上持有，之下卖出
        above = last_close > last_ma20
        print(f"    今日信号    : {'持有' if above else '卖出/观望'}")
        print(f"    建议操作    : {'价格在 MA20 之上，持有不动' if above else '价格跌破 MA20，考虑赎回'}")
        print("\n=== 明日操作预案 ===")
        if above:
            print("  若今日已持有  : 继续持有，观察 close 是否跌破 MA20")
            print(f"  若今日未操作  : 明日 close > MA20({last_ma20:.4f}) 则买入持有")
            print(f"  止损参考      : close 跌破 MA20({last_ma20:.4f})")
        else:
            print("  若今日已赎回  : 观望，等待 close 重新站上 MA20")
            print(f"  若今日未操作  : 明日 close < MA20({last_ma20:.4f}) 则考虑赎回")
            print(f"  回补参考      : close 重新站上 MA20({last_ma20:.4f})")
        print(f"  趋势参考      : MA60={last_ma60:.4f}  close {'之上(中期多头)' if last_close > last_ma60 else '之下(中期空头)'}")
        return

    # 波段(RSI{lower}/{upper})：解析上下轨参数
    try:
        lower = int(best_name.split("RSI")[1].split("/")[0])
        upper = int(best_name.split("/")[1].split(")")[0])
    except Exception:
        lower, upper = 30, 70

    print("\n=== 关键指标观察 ===")
    print(f"  RSI 当前     : {last_rsi:.1f}  (下轨 {lower} / 上轨 {upper})")
    print(f"  距下轨       : {last_rsi - lower:+.1f}  ({'已触发买入' if last_rsi < lower else '未触发'})")
    print(f"  距上轨       : {upper - last_rsi:+.1f}  ({'已触发卖出' if last_rsi > upper else '未触发'})")

    if last_rsi < lower:
        print(f"\n    今日信号    : 买入（RSI {last_rsi:.1f} < 下轨 {lower}）")
        print("    建议操作    : 按最新价买入/申购")
    elif last_rsi > upper:
        print(f"\n    今日信号    : 卖出（RSI {last_rsi:.1f} > 上轨 {upper}）")
        print("    建议操作    : 按最新价卖出/赎回")
    else:
        # 中性区间：判断最近一次信号方向
        buy_dates = df.index[df['RSI'] < lower]
        sell_dates = df.index[df['RSI'] > upper]
        recent_buy = buy_dates[-1] if len(buy_dates) else None
        recent_sell = sell_dates[-1] if len(sell_dates) else None
        print(f"\n    今日信号    : 无（RSI 处于 [{lower}, {upper}] 中性区间）")
        print(f"    最近买入    : {recent_buy.date() if recent_buy else '无'}")
        print(f"    最近卖出    : {recent_sell.date() if recent_sell else '无'}")
        if recent_buy is not None and (
            recent_sell is None or recent_buy > recent_sell
        ):
            print(f"    当前状态    : 持仓（自 {recent_buy.date()} 起未出现卖出信号）")
            print("    建议操作    : 持有，等待 RSI 触上轨再赎回")
        else:
            print("    当前状态    : 空仓观望")
            print("    建议操作    : 等待 RSI 跌破下轨再买入")

    print("\n=== 明日操作预案 ===")
    if last_rsi < lower:
        print("  若今日已买入  : 持有，观察 RSI 反弹至上轨")
        print("  若今日未操作  : 明日 RSI 仍在下轨下方则继续买入")
        print(f"  卖出参考      : RSI 反弹至 {upper} 以上则赎回")
        print(f"  止损参考      : close 跌破近 20 日最低 或 RSI 再次下探")
    elif last_rsi > upper:
        print("  若今日已赎回  : 观望，等待 RSI 回落至下轨")
        print("  若今日未操作  : 明日 RSI 仍在上轨上方则继续赎回")
        print(f"  回补参考      : RSI 回落至 {lower} 以下则回补")
    else:
        print(f"  明日观察指标  : RSI 当前 {last_rsi:.1f}，关注是否触及 {lower} 或 {upper}")
        print(f"  买入触发      : RSI < {lower}")
        print(f"  卖出触发      : RSI > {upper}")
        print(f"  趋势参考      : close vs MA20({last_ma20:.4f})  {'之上' if last_close > last_ma20 else '之下'}")


def run_fund_tournament(code: str, detail: bool) -> None:
    print(f"\n=== 基金: {code} ===")
    df = fetch_fund_data(code)
    if df is None or len(df) < 60:
        print(f"数据不足（{len(df) if df is not None else 0} 条），至少需要 60 条")
        return
    print(f"数据区间: {df.index[0].date()} ~ {df.index[-1].date()}（{len(df)} 条）")
    df = calculate_indicators(df)

    print("运行多策略锦标赛...\n")
    results: list[tuple[str, dict]] = []
    # 原有 RSI 波段 + 持有
    for lower in [20, 25, 30, 35]:
        for upper in [65, 70, 75, 80]:
            params = {"rsi_lower": lower, "rsi_upper": upper}
            stats = _fund_backtest_with_stats(df, params, "swing")
            if stats:
                results.append((f"波段(RSI{lower}/{upper})", stats))
    hold_stats = _fund_backtest_with_stats(df, {"rsi_lower": 30, "rsi_upper": 70}, "hold")
    if hold_stats:
        results.append(("持有(MA20)", hold_stats))

    # P2 基金策略：双均线 / MACD / 波动率目标 / 指数增强轮动
    from fund_analysis.fund_strategy.p2_strategies import (
        FundMaStrategy, FundMacdStrategy, FundVolTargetStrategy,
        FundIndexRotationStrategy,
    )
    for short in [5, 10, 20]:
        for long in [60, 90, 120]:
            if short >= long:
                continue
            strat = FundMaStrategy(short=short, long=long)
            sig = strat.generate_signals(df)
            stats = _fund_backtest_signals(df, sig)
            if stats:
                results.append((strat.name, stats))
    for fast in [8, 12]:
        for slow in [20, 26]:
            for signal in [6, 9]:
                strat = FundMacdStrategy(fast=fast, slow=slow, signal=signal)
                sig = strat.generate_signals(df)
                stats = _fund_backtest_signals(df, sig)
                if stats:
                    results.append((strat.name, stats))
    for tv in [0.1, 0.15, 0.2]:
        for mp in [20, 30]:
            strat = FundVolTargetStrategy(target_vol=tv, ma_period=mp)
            sig = strat.generate_signals(df)
            stats = _fund_backtest_signals(df, sig)
            if stats:
                results.append((strat.name, stats))
    for short in [10, 20]:
        for long in [60, 90]:
            if short >= long:
                continue
            strat = FundIndexRotationStrategy(short=short, long=long)
            sig = strat.generate_signals(df)
            stats = _fund_backtest_signals(df, sig)
            if stats:
                results.append((strat.name, stats))

    # 评分：与股票侧同步，使用 Wilson score 下界（z=2.576, 99%置信）
    # + 最小交易次数门槛 MIN_TRADE_COUNT=10，避免小样本高胜率过拟合
    # 评分 = Wilson胜率*0.5 + 总收益*0.2 - 回撤*0.3
    from stock_analysis.backtest.analyzer import _wilson_lower_bound, MIN_TRADE_COUNT

    def _score(s: dict) -> float:
        if s["trade_count"] < MIN_TRADE_COUNT:
            return -999.0
        adj_wr = _wilson_lower_bound(s["win_rate"], s["trade_count"])
        return adj_wr * 0.5 + s["total_return"] * 0.2 - abs(s["max_drawdown"]) * 0.3

    results.sort(key=lambda x: _score(x[1]), reverse=True)

    best_name, best = results[0]
    print(f">>> 最优策略: {best_name}")
    print(f"    胜率      : {best['win_rate']:.2%}")
    print(f"    样本调整胜率: {_wilson_lower_bound(best['win_rate'], best['trade_count']):.2%}")
    print(f"    总收益    : {best['total_return']:.2%}")
    print(f"    最大回撤  : {best['max_drawdown']:.2%}")
    print(f"    交易次数  : {best['trade_count']}")

    # 当前操作建议
    _print_fund_current_advice(df, best_name)

    if detail:
        print("\n=== 全策略对比 ===")
        print(
            f"{'策略':<26}{'胜率':>8}{'总收益':>10}{'最大回撤':>10}{'交易次数':>8}"
        )
        for name, s in results:
            print(
                f"{name:<26}{s['win_rate']:>8.2%}{s['total_return']:>10.2%}"
                f"{s['max_drawdown']:>10.2%}{s['trade_count']:>8}"
            )


def main() -> None:
    # Windows 终端中文兼容
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except Exception:
        pass

    parser = argparse.ArgumentParser(
        description="股票/基金多策略回测：输入代码，输出胜率最高的操作策略",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="示例:\n  python cli.py 600519\n  python cli.py 510300 --type fund --detail",
    )
    parser.add_argument("code", help="股票或基金代码，如 600519 / 510300")
    parser.add_argument(
        "-t",
        "--type",
        choices=["auto", "stock", "fund"],
        default="auto",
        help="标的类型（auto 自动识别，默认 auto）",
    )
    parser.add_argument("--detail", action="store_true", help="显示全策略对比表")
    parser.add_argument("--no-regime", action="store_true", help="跳过市场环境加载")
    parser.add_argument("--start", default="20200101", help="数据起始日期，默认 20200101")
    parser.add_argument("--jobs", type=int, default=1, help="锦标赛并行数，默认1（P2加速可用4/8）")
    parser.add_argument("--opt", choices=["grid", "bayes"], default="grid", help="参数寻优模式：grid全网格 / bayes贝叶斯优化（P3）")
    parser.add_argument("--opt-iters", type=int, default=30, help="bayes模式迭代数，默认30")
    args = parser.parse_args()

    code = str(args.code).strip()
    kind = args.type if args.type != "auto" else detect_type(code)

    if kind == "fund":
        run_fund_tournament(code, args.detail)
    else:
        if args.opt == "bayes":
            from stock_analysis.backtest.optimizer import run_bayes_tournament
            run_bayes_tournament(code, args.start, not args.no_regime, args.detail, iters=args.opt_iters, jobs=args.jobs)
        else:
            run_stock_tournament(code, args.start, not args.no_regime, args.detail, jobs=args.jobs)


if __name__ == "__main__":
    main()
