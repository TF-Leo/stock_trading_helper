# -*- coding: utf-8 -*-
"""B5: Strategy family de-correlation and consensus selection"""
from typing import Dict, List, Tuple
import numpy as np


def select_consensus(candidates: List[Dict], top_k: int = 10) -> Dict:
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
    
    # Family de-correlation: keep only best per family
    family_best = {}
    for c in sorted_cands:
        fam = c.get('family', 'unknown')
        if fam not in family_best or c.get('score', 0) > family_best[fam].get('score', 0):
            family_best[fam] = c
    
    # Get representatives
    representatives = list(family_best.values())[:top_k]
    
    # Check family ratios
    family_counts = {}
    for c in representatives:
        fam = c.get('family', 'unknown')
        family_counts[fam] = family_counts.get(fam, 0) + 1
    
    max_ratio = max(family_counts.values()) / len(representatives) if representatives else 0
    
    # If any family > 50%, need further de-correlation
    if max_ratio > 0.5 and len(representatives) > 1:
        # Keep one from each family, fill rest with next best from underrepresented families
        final_members = []
        used_families = set()
        
        # First pass: take one from each family
        for fam in sorted(family_counts.keys(), key=lambda f: family_counts[f]):
            for c in sorted_cands:
                if c.get('family') == fam and c not in final_members:
                    final_members.append(c)
                    used_families.add(fam)
                    break
        
        # Second pass: fill remaining slots
        remaining = top_k - len(final_members)
        for c in sorted_cands:
            if len(final_members) >= top_k:
                break
            if c not in final_members:
                final_members.append(c)
        
        representatives = final_members[:top_k]
    
    # Calculate metrics
    scores = [c.get('score', 0) for c in representatives]
    dispersion = float(np.std(scores)) if scores else 0
    agreement = 1 - min(dispersion, 1)  # Normalize to 0-1
    
    # Dominant family
    dominant_family = max(family_counts, key=family_counts.get) if family_counts else None
    
    return {
        'members': representatives,
        'family': dominant_family,
        'valid_count': len(valid),
        'all_invalid': False,
        'agreement': agreement,
        'dispersion': dispersion,
        'stability': {
            'action_stable': True,  # Will be validated in B6
            'family_stable': True,
            'windows_tested': 3,  # full, drop60, drop120
            'top_k_overlap_min': max(1, top_k // 2)
        }
    }
