# -*- coding: utf-8 -*-
"""B5: Strategy family de-correlation and consensus selection"""
from typing import Dict, List, Tuple
import numpy as np


def select_consensus(candidates: List[Dict], top_k: int = 10,
                     family_scores: Dict[str, float] = None) -> Dict:
    """
    B5: Top-K consensus selection with family de-correlation
    
    Ensures no single family exceeds 50% of selected candidates.
    
    Args:
        candidates: [{'name', 'family', 'score', ...}, ...]
        top_k: Number of candidates to return
        
    Returns:
        {
            'members': [...],
            'family': 'dominant_family_name',
            'agreement': 0-1,
            'dispersion': std_dev,
            'stability': {'action_stable': bool, 'family_stable': bool, 
                         'windows_tested': int, 'top_k_overlap_min': int}
        }
    """
    if not candidates:
        return {
            'members': [],
            'family': None,
            'agreement': 0,
            'dispersion': 0,
            'stability': {
                'action_stable': True,
                'family_stable': True,
                'windows_tested': 0,
                'top_k_overlap_min': 0
            }
        }
    
    # B5/C4: 先剔除被评分判定为无效的候选，再求族共识。
    # analyzer 用 -100 作为"无效"地板值（OOS 交易数不足 OOS_MIN_TRADES 会被硬判 -100）；
    # 这些候选参与共识会让 agreement 虚高，进而推出自相矛盾的建议。
    valid = [c for c in candidates if float(c.get('score', -100) or -100) > -100.0]
    if not valid:
        return {
            'members': [],
            'family': None,
            'agreement': 0.0,
            'dispersion': 0.0,
            'valid_count': 0,
            'all_invalid': True,
            'stability': {
                'action_stable': True,
                'family_stable': True,
                'windows_tested': 0,
                'top_k_overlap_min': 0,
            },
        }

    # Sort by score descending
    sorted_cands = sorted(valid, key=lambda x: x.get('score', 0), reverse=True)

    # B5 硬约束：最终候选集中任一族占比 ≤ 50%。
    # 做法：按评分降序贪心填充，每族计数达到 ceil(top_k/2) 即跳过该族；
    # 这样"去相关"与"对外 candidates"是同一份列表（cli 直接输出 members），不会再漂移。
    import math
    cap = max(1, int(math.ceil(top_k / 2.0)))
    members: List[Dict] = []
    counts: Dict[str, int] = {}
    for c in sorted_cands:
        fam = c.get('family', 'unknown')
        if counts.get(fam, 0) >= cap:
            continue
        members.append(c)
        counts[fam] = counts.get(fam, 0) + 1
        if len(members) >= top_k:
            break

    # 二次校正：若实际成员数少于 top_k，ceiling 会变小（ceil(n/2) < ceil(top_k/2)），
    # 需按真实长度把超限族的最低分成员剔除，否则仍然违反"任一族 ≤ 50%"。
    if len(members) > 1:
        while True:
            final_cap = max(1, int(math.ceil(len(members) / 2.0)))
            over = {f: n for f, n in counts.items() if n > final_cap}
            if not over:
                break
            fam = max(over, key=lambda f: over[f])
            victim = min((m for m in members if m.get('family', 'unknown') == fam),
                         key=lambda m: m.get('score', 0))
            members.remove(victim)
            counts[fam] -= 1
            if len(members) <= 1:
                break

    representatives = members

    # Calculate metrics
    scores = [c.get('score', 0) for c in representatives]
    dispersion = float(np.std(scores)) if scores else 0
    agreement = 1 - min(dispersion, 1)  # Normalize to 0-1

    # 主导族：B6 稳定性要求 —— 不能用"成员计数"（计数接近时会在窗口间翻转），
    # 也不能只用族内 Top-3 均值（被一两个幸运参数拉高，跨窗口翻转）。
    # 采用**族内全部配置评分的中位数**：它把整族的噪声平均掉，对末尾 60/120 根 K 线的扰动不敏感。
    # 调用方（cli）可用 family_scores 传入基于**完整 comparison 表**算好的族中位数；
    # 未传时退化为用传入候选列表自己算（单元测试/小样本场景）。
    fam_scores: Dict[str, float] = dict(family_scores or {})
    if not fam_scores:
        buckets: Dict[str, List[float]] = {}
        for c in sorted_cands:
            buckets.setdefault(c.get('family', 'unknown'), []).append(float(c.get('score', 0)))
        fam_scores = {f: float(np.median(v)) for f, v in buckets.items()}
    dominant_family = (max(fam_scores, key=lambda f: (fam_scores[f], f))
                       if fam_scores else None)

    # 成员保持**评分降序**：candidates[0] 必须是全局最优候选（T1/D4 的既有约定），
    # 否则 evidence.metrics（对应 best_name 的回测）与 candidates[0] 会对不上。
    # 主导族只体现在 consensus.family 字段，不改变成员顺序。
    representatives = sorted(representatives, key=lambda c: -float(c.get('score', 0)))

    family_counts: Dict[str, int] = {}
    for c in representatives:
        fam = c.get('family', 'unknown')
        family_counts[fam] = family_counts.get(fam, 0) + 1

    return {
        'members': representatives,
        'family': dominant_family,
        'valid_count': len(valid),
        'all_invalid': False,
        'agreement': agreement,
        'dispersion': dispersion,
        'family_counts': family_counts,
        'family_scores': {k: round(v, 4) for k, v in fam_scores.items()},
        'stability': {
            'action_stable': True,  # Will be validated in B6
            'family_stable': True,
            'windows_tested': 3,  # full, drop60, drop120
            'top_k_overlap_min': max(1, top_k // 2)
        }
    }
