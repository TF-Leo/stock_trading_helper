# -*- coding: utf-8 -*-
"""P1: 统一策略网格注册表 — 单一事实来源。

消除 analyzer._build_strategy_grid 与 cli._build_stock_strategies 约130行重复。
所有参数网格只在此定义，两处均委托到本模块。
"""
from __future__ import annotations
import itertools
from typing import Dict, Tuple, Type


def _grid_defs():
    """返回 [(prefix, StrategyClass, [kwargs dict, ...], namer)]，顺序即锦标赛顺序。"""
    from stock_analysis.strategy import (
        TrendStrategy, VolumeStrategy, ChipStrategy,
        BollStrategy, MacdStrategy, ComprehensiveStrategy,
        KdjStrategy, ObvStrategy, VrStrategy, KellyStrategy,
        DonchianStrategy, AdxStrategy, AtrChannelStrategy,
        EmaCrossStrategy, KamaStrategy, HmaCrossStrategy, VwapStrategy,
        BollSqueezeStrategy, AtrFilterStrategy, ZscoreStrategy,
        WilliamsRStrategy, CciStrategy, StochRsiStrategy, SarStrategy, DiCrossStrategy,
        MfiStrategy, CmfStrategy, GapStrategy, EngulfingStrategy,
        HammerStrategy, DojiStrategy,
        GridArithmeticStrategy, GridGeometricStrategy, GridAtrStrategy,
        VolTargetStrategy, PairsStrategy, SpreadZscoreStrategy, StrategyVoting,
        EtfRotationStrategy, RiskParityStrategy,
    )
    defs = []

    # 1. 均线趋势 C(9,3)=84
    ma_windows = [3, 5, 8, 10, 13, 20, 30, 60, 120]
    defs.append(("均线趋势", TrendStrategy,
                 [{"short_w": s, "mid_w": m, "long_w": l} for s, m, l in itertools.combinations(ma_windows, 3)],
                 lambda k: f"均线趋势({k['short_w']}-{k['mid_w']}-{k['long_w']})"))
    # 2. 布林 7x6=42
    defs.append(("布林", BollStrategy,
                 [{"window": w, "num_std": std} for w in [5, 10, 15, 20, 25, 30, 40] for std in [1.0, 1.5, 2.0, 2.5, 3.0, 3.5]],
                 lambda k: f"布林({k['window']},{k['num_std']})"))
    # 3. MACD
    macd_params = []
    for fast in [3, 5, 8, 10, 12, 15]:
        for slow in [12, 20, 26, 34, 40]:
            if fast >= slow:
                continue
            for signal in [3, 5, 9]:
                macd_params.append({"fast": fast, "slow": slow, "signal": signal})
    defs.append(("MACD", MacdStrategy, macd_params,
                 lambda k: f"MACD({k['fast']}-{k['slow']}-{k['signal']})"))
    # 4. 筹码
    chip_params = [{"winner_lower": wl, "winner_upper": wu}
                   for wl in [10, 15, 20, 25, 30, 35, 40, 45]
                   for wu in [55, 60, 65, 70, 75, 80, 85, 90] if wl < wu]
    defs.append(("筹码", ChipStrategy, chip_params,
                 lambda k: f"筹码({k['winner_lower']}-{k['winner_upper']})"))
    # 5/6 固定
    defs.append(("量价突破", VolumeStrategy, [{}], lambda k: "量价突破"))
    defs.append(("三维共振", ComprehensiveStrategy, [{}], lambda k: "三维共振"))
    # 7. KDJ 3x3x2=18
    defs.append(("KDJ", KdjStrategy,
                 [{"n": n, "buy_k": b, "sell_k": s} for n in [9, 14, 21] for b in [15, 20, 25] for s in [75, 80]],
                 lambda k: f"KDJ({k['n']},{k['buy_k']}/{k['sell_k']})"))
    # 8. OBV 5
    defs.append(("OBV", ObvStrategy,
                 [{"obv_ma": m} for m in [10, 15, 20, 30, 60]],
                 lambda k: f"OBV({k['obv_ma']})"))
    # 9. VR 3x2x2=12
    defs.append(("VR", VrStrategy,
                 [{"window": w, "buy_vr": b, "sell_vr": s} for w in [14, 26, 52] for b in [40, 70] for s in [150, 200]],
                 lambda k: f"VR({k['window']},{k['buy_vr']}/{k['sell_vr']})"))
    # 10. Kelly
    kelly_params = [{"ma_short": s, "ma_long": l, "lookback": lb}
                    for s in [3, 5, 10] for l in [20, 30, 60] if s < l for lb in [60, 120]]
    defs.append(("Kelly", KellyStrategy, kelly_params,
                 lambda k: f"Kelly({k['ma_short']}-{k['ma_long']},{k['lookback']})"))
    # 11. Donchian
    don_params = [{"entry_n": e, "exit_n": x} for e in [10, 15, 20, 30, 55] for x in [5, 10, 20] if x < e]
    defs.append(("唐奇安", DonchianStrategy, don_params,
                 lambda k: f"唐奇安({k['entry_n']}/{k['exit_n']})"))
    # 12. ADX 3x3=9
    defs.append(("ADX", AdxStrategy,
                 [{"adx_period": p, "adx_threshold": t} for p in [10, 14, 20] for t in [20, 25, 30]],
                 lambda k: f"ADX({k['adx_period']},{k['adx_threshold']})"))
    # 13. ATR通道 2x2x4=16
    defs.append(("ATR通道", AtrChannelStrategy,
                 [{"atr_period": a, "ma_period": m, "multiplier": mu} for a in [14, 20] for m in [10, 20] for mu in [2.0, 2.5, 3.0, 3.5]],
                 lambda k: f"ATR通道({k['atr_period']},{k['ma_period']},{k['multiplier']})"))
    # 14. EMA 5x3=15
    defs.append(("EMA交叉", EmaCrossStrategy,
                 [{"fast": f, "slow": s} for f in [3, 5, 8, 10, 12] for s in [20, 26, 34] if f < s],
                 lambda k: f"EMA交叉({k['fast']}-{k['slow']})"))
    # 15. KAMA 4
    defs.append(("KAMA", KamaStrategy,
                 [{"period": p} for p in [5, 10, 20, 30]],
                 lambda k: f"KAMA({k['period']})"))
    # 16. HMA 3x3=9
    defs.append(("HMA交叉", HmaCrossStrategy,
                 [{"fast": f, "slow": s} for f in [5, 10, 15] for s in [20, 30, 60]],
                 lambda k: f"HMA交叉({k['fast']}-{k['slow']})"))
    # 17. VWAP 3
    defs.append(("VWAP", VwapStrategy,
                 [{"rolling_window": rw} for rw in [None, 20, 60]],
                 lambda k: f"VWAP({'累计' if k['rolling_window'] is None else k['rolling_window']})"))
    # 18. Squeeze 3x2x2=12
    defs.append(("布林Squeeze", BollSqueezeStrategy,
                 [{"window": w, "num_std": std, "squeeze_pct": sq} for w in [10, 20, 30] for std in [1.5, 2.0] for sq in [0.1, 0.2]],
                 lambda k: f"布林Squeeze({k['window']},{k['num_std']},{k['squeeze_pct']})"))
    # 19. ATR过滤 2x2x2=8
    defs.append(("ATR过滤", AtrFilterStrategy,
                 [{"atr_period": a, "ma_period": m, "vol_high_pct": v} for a in [14, 20] for m in [10, 20] for v in [0.04, 0.05]],
                 lambda k: f"ATR过滤({k['atr_period']},{k['ma_period']},{k['vol_high_pct']})"))
    # 20. Z-Score 3x2x2=12
    defs.append(("Z-Score", ZscoreStrategy,
                 [{"window": w, "entry_z": e, "exit_z": x} for w in [10, 20, 30] for e in [-1.5, -2.0] for x in [0.5, 1.0]],
                 lambda k: f"Z-Score({k['window']},{k['entry_z']},{k['exit_z']})"))
    # 21. WilliamsR
    defs.append(("WilliamsR", WilliamsRStrategy,
                 [{"period": p, "buy_wr": b, "sell_wr": s} for p in [9, 14, 21] for b in [-80, -85] for s in [-20, -15]],
                 lambda k: f"WilliamsR({k['period']},{k['buy_wr']}/{k['sell_wr']})"))
    # 22. CCI
    defs.append(("CCI", CciStrategy,
                 [{"period": p, "lower": lo, "upper": up} for p in [10, 14, 20] for lo in [-100, -150] for up in [100, 150]],
                 lambda k: f"CCI({k['period']},{k['lower']}/{k['upper']})"))
    # 23. StochRSI
    defs.append(("StochRSI", StochRsiStrategy,
                 [{"rsi_period": p, "stoch_period": p, "lower": lo, "upper": up} for p in [9, 14, 21] for lo in [20, 25] for up in [75, 80]],
                 lambda k: f"StochRSI({k['rsi_period']},{k['lower']}/{k['upper']})"))
    # 24. SAR 2x2=4
    defs.append(("SAR", SarStrategy,
                 [{"af_start": a, "af_max": m} for a in [0.02, 0.03] for m in [0.2, 0.3]],
                 lambda k: f"SAR({k['af_start']}/{k['af_max']})"))
    # 25. DI 3x3=9
    defs.append(("DI交叉", DiCrossStrategy,
                 [{"adx_period": p, "adx_threshold": t} for p in [10, 14, 20] for t in [15, 20, 25]],
                 lambda k: f"DI交叉({k['adx_period']},{k['adx_threshold']})"))
    # 26. MFI
    defs.append(("MFI", MfiStrategy,
                 [{"period": p, "buy_mfi": b, "sell_mfi": s} for p in [9, 14, 20] for b in [20, 25] for s in [75, 80]],
                 lambda k: f"MFI({k['period']},{k['buy_mfi']}/{k['sell_mfi']})"))
    # 27. CMF
    defs.append(("CMF", CmfStrategy,
                 [{"period": p, "buy_cmf": b, "sell_cmf": s} for p in [10, 20, 30] for b in [0.05, 0.1] for s in [-0.05, -0.1]],
                 lambda k: f"CMF({k['period']},{k['buy_cmf']}/{k['sell_cmf']})"))
    # 28. 缺口 3
    defs.append(("缺口跳空", GapStrategy,
                 [{"gap_threshold": g} for g in [0.005, 0.01, 0.02]],
                 lambda k: f"缺口跳空({k['gap_threshold']})"))
    # 29. 吞没 1
    defs.append(("吞没形态", EngulfingStrategy, [{}], lambda k: "吞没形态"))
    # 30. 锤子 2x2=4
    defs.append(("锤子线", HammerStrategy,
                 [{"body_ratio": b, "shadow_ratio": s} for b in [0.2, 0.3] for s in [1.5, 2.0]],
                 lambda k: f"锤子线({k['body_ratio']},{k['shadow_ratio']})"))
    # 31. 十字星 2x2=4
    defs.append(("十字星", DojiStrategy,
                 [{"body_ratio": b, "trend_window": t} for b in [0.1, 0.15] for t in [3, 5]],
                 lambda k: f"十字星({k['body_ratio']},{k['trend_window']})"))
    # 32. 等差 2x2x2=8
    defs.append(("等差网格", GridArithmeticStrategy,
                 [{"ma_period": m, "grid_steps": g, "grid_pct": p} for m in [20, 30] for g in [3, 5] for p in [0.02, 0.03]],
                 lambda k: f"等差网格({k['ma_period']},{k['grid_steps']},{k['grid_pct']})"))
    # 33. 等比 8
    defs.append(("等比网格", GridGeometricStrategy,
                 [{"ma_period": m, "grid_steps": g, "grid_pct": p} for m in [20, 30] for g in [3, 5] for p in [0.02, 0.03]],
                 lambda k: f"等比网格({k['ma_period']},{k['grid_steps']},{k['grid_pct']})"))
    # 34. ATR网格 8
    defs.append(("ATR网格", GridAtrStrategy,
                 [{"atr_period": a, "ma_period": m, "grid_mult": g} for a in [14, 20] for m in [20, 30] for g in [1.5, 2.0]],
                 lambda k: f"ATR网格({k['atr_period']},{k['ma_period']},{k['grid_mult']})"))
    # 35. 波动率目标 3x2x2=12
    defs.append(("波动率目标", VolTargetStrategy,
                 [{"target_vol": t, "ma_short": s, "ma_long": l} for t in [0.1, 0.15, 0.2] for s in [5, 10] for l in [20, 30]],
                 lambda k: f"波动率目标({k['target_vol']},{k['ma_short']}-{k['ma_long']})"))
    # 36. 配对 3x2x2=12
    defs.append(("配对交易", PairsStrategy,
                 [{"window": w, "entry_z": e, "exit_z": x} for w in [10, 20, 30] for e in [-1.5, -2.0] for x in [0.0, 0.5]],
                 lambda k: f"配对交易({k['window']},{k['entry_z']},{k['exit_z']})"))
    # 37. 价差 12
    defs.append(("价差ZScore", SpreadZscoreStrategy,
                 [{"window": w, "entry_z": e, "exit_z": x} for w in [10, 20, 30] for e in [-1.5, -2.0] for x in [0.0, 0.5]],
                 lambda k: f"价差ZScore({k['window']},{k['entry_z']},{k['exit_z']})"))
    # 38. 组合 2
    defs.append(("策略组合", StrategyVoting,
                 [{"vote_threshold": v} for v in [2, 3]],
                 lambda k: f"策略组合({k['vote_threshold']}票)"))
    # 39. 指数轮动门控：ma_short × ma_long（2×2=4 组，mom_window固定20）
    defs.append(("指数轮动", EtfRotationStrategy,
                 [{"ma_short": s, "ma_long": l, "mom_window": 20} for s in [5, 10] for l in [20, 30]],
                 lambda k: f"指数轮动({k['ma_short']}-{k['ma_long']})"))
    # 40. 风险平价：vol_window（2 组）
    defs.append(("风险平价", RiskParityStrategy,
                 [{"vol_window": w} for w in [20, 60]],
                 lambda k: f"风险平价({k['vol_window']})"))
    return defs


def build_strategy_instances() -> Dict[str, object]:
    """{策略名: 策略实例}，供CLI详细建议打印用。"""
    strategies: Dict[str, object] = {}
    for _prefix, cls, params_list, namer in _grid_defs():
        for kw in params_list:
            inst = cls(**kw)
            strategies[namer(kw)] = inst
    return strategies


def build_strategy_signals() -> Dict[str, object]:
    """{策略名: generate_trade_signals callable}，供锦标赛回测用。"""
    strategies: Dict[str, object] = {}
    for _prefix, cls, params_list, namer in _grid_defs():
        for kw in params_list:
            inst = cls(**kw)
            strategies[namer(kw)] = inst.generate_trade_signals
    return strategies


def grid_size() -> int:
    return sum(len(p) for _, _, p, _ in _grid_defs())
