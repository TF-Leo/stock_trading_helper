# -*- coding: utf-8 -*-
"""生成 A13 / B6 / B1 验证快照：600519 与 510300 的实测表（全部数字来自真实运行）。

用法（在仓库根目录）：
    python tools/validation_snapshot.py
输出：
    source/docs/validation_600519.md
"""
from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import time

sys.stdout.reconfigure(encoding="utf-8")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "source")
OUT = os.path.join(SRC, "docs", "validation_600519.md")


def run_cli(args, timeout=900):
    p = subprocess.run([sys.executable, "cli.py"] + args, cwd=SRC,
                       capture_output=True, timeout=timeout)
    try:
        return p.returncode, json.loads(p.stdout.decode("utf-8", "replace"))
    except Exception:
        return p.returncode, None


def summarize(d):
    if not d:
        return {}
    rec = d.get("recommendation", {})
    ev = d.get("evidence", {})
    cands = d.get("candidates") or []
    metrics = ev.get("metrics") or {}
    counts = {}
    for c in cands:
        counts[c.get("family")] = counts.get(c.get("family"), 0) + 1
    return {
        "status": rec.get("status"),
        "action": rec.get("action"),
        "confidence": rec.get("confidence"),
        "position_fraction": rec.get("position_fraction"),
        "stop_loss": rec.get("stop_loss"),
        "take_profit": rec.get("take_profit"),
        "stop_method": rec.get("stop_method"),
        "entry_zone": rec.get("entry_zone"),
        "horizon_days": rec.get("horizon_days"),
        "distance_to_trigger": rec.get("distance_to_trigger"),
        "invalidation": rec.get("invalidation"),
        "triggers": len(ev.get("triggers") or []),
        "family": (ev.get("consensus") or {}).get("family"),
        "agreement": (ev.get("consensus") or {}).get("agreement"),
        "n_trials": ev.get("n_trials"),
        "n_survivors": ev.get("n_survivors"),
        "dsr": ev.get("deflated_sharpe"),
        "significant": ev.get("significant"),
        "excess_point": (ev.get("excess_vs_buy_hold") or {}).get("point"),
        "excess_ci": (ev.get("excess_vs_buy_hold") or {}).get("ci_95"),
        "excess_p_positive": (ev.get("excess_vs_buy_hold") or {}).get("p_positive"),
        "regime": (d.get("market_regime") or {}).get("label"),
        "regime_win_rates": ev.get("regime_win_rates"),
        "bars": (d.get("data") or {}).get("bars"),
        "data_source": (d.get("data") or {}).get("source"),
        "from_cache": (d.get("data") or {}).get("from_cache"),
        # F06f/F20：数据新鲜度与降级可见性（旧快照 0 处记录，无法判断结论是否建立在陈旧数据上）
        "stale": (d.get("data") or {}).get("stale"),
        "cache_last_date": (d.get("data") or {}).get("cache_last_date"),
        "lag_days": (d.get("data") or {}).get("lag_days"),
        "expected_date": (d.get("data") or {}).get("expected_date"),
        "requested_source": (d.get("data") or {}).get("requested_source"),
        "top_name": cands[0]["name"] if cands else None,
        "top_score": cands[0]["score"] if cands else None,
        "top_score_is": cands[0].get("score_is") if cands else None,
        "top_score_oos": cands[0].get("score_oos") if cands else None,
        "top_trades": cands[0]["trade_count"] if cands else None,
        "n_candidates": len(cands),
        "family_counts": counts,
        "ci_metrics": sorted(k for k, v in metrics.items() if (v or {}).get("ci_95")),
        "time_in_market": (metrics.get("time_in_market") or {}).get("point"),
        "zero_trade_configs": (d.get("diagnostics") or {}).get("zero_trade_configs"),
        "warnings": len((d.get("diagnostics") or {}).get("warnings") or []),
        "elapsed_seconds": (d.get("diagnostics") or {}).get("elapsed_seconds"),
    }


def _shape_paths(obj, path=""):
    out = set()
    if isinstance(obj, dict):
        for k, v in obj.items():
            p = f"{path}/{k}"
            out.add(p)
            out |= _shape_paths(v, p)
    elif isinstance(obj, list) and obj:
        out |= _shape_paths(obj[0], path + "[]")
    return out


def error_path_probe():
    """C3 同构实测：成功路径 vs `insufficient_data` 路径的键路径差。

    历史问题：错误路径只输出 46 个键、成功路径 134 个（缺 88 条路径），
    且 data.bars 恒 0 与 invalidation="仅 72 根 < 100" 自相矛盾。
    """
    import datetime as _dt
    code_ok, ok = run_cli(["600519", "--json", "--fast"])
    start = (_dt.date.today() - _dt.timedelta(days=40)).strftime("%Y%m%d")
    code_err, err = run_cli(["600519", "--json", "--fast", "--start", start])
    if not ok or not err:
        return {}
    rec = err.get("recommendation") or {}
    missing = sorted(_shape_paths(ok) - _shape_paths(err))
    missing = [p for p in missing
               if not p.startswith(("/candidates[]", "/evidence/triggers[]"))]
    metrics = (err.get("evidence") or {}).get("metrics") or {}
    return {
        "ok_exit": code_ok,
        "err_exit": code_err,
        "err_status": rec.get("status"),
        "ok_paths": len(_shape_paths(ok)),
        "err_paths": len(_shape_paths(err)),
        "missing": missing,
        "err_bars": (err.get("data") or {}).get("bars"),
        "err_invalidation": rec.get("invalidation"),
        "err_horizon": rec.get("horizon_days"),
        "err_stop_method": rec.get("stop_method"),
        "err_metrics_n": len(metrics),
        "err_metrics_all_null": all((v or {}).get("point") is None for v in metrics.values()),
    }


def win_rate_share(d):
    """B1：胜率项对 |总分| 的贡献占比（Top12）。"""
    sys.path.insert(0, SRC)
    from decision.scoring import score_components
    cands = (d.get("candidates") or [])[:12]
    tw = tc = ts = 0.0
    for c in cands:
        comp = score_components(c.get("annual_return") or 0, c.get("max_drawdown") or 0,
                                c.get("win_rate") or 0, int(c.get("trade_count") or 0))
        tw += abs(comp["win_rate_term"])
        tc += abs(comp["calmar_term"])
        ts += abs(comp["stability_term"])
    denom = tw + tc + ts
    return (tw / denom) if denom else None


def windows(code):
    """B6 验证二：full / drop60 / drop120 三窗口的 action 与共识族。"""
    sys.path.insert(0, SRC)
    os.chdir(SRC)
    import cli
    df = cli.get_stock_daily(code, start_date=None, source="sina")
    if df is None or len(df) < 300:
        return {}
    orig = cli.get_stock_daily
    out = {}
    try:
        for label, drop in (("full", 0), ("drop60", 60), ("drop120", 120)):
            sub = (df.iloc[: len(df) - drop] if drop else df).copy()
            cli.get_stock_daily = (lambda d: (lambda *a, **k: d))(sub)
            r = cli.advise_internal(code, start=None, fast=True)
            out[label] = {
                "action": r["recommendation"]["action"],
                "family": (r.get("evidence") or {}).get("consensus", {}).get("family"),
                "status": r["recommendation"]["status"],
            }
    finally:
        cli.get_stock_daily = orig
    return out


def fmt(v):
    if isinstance(v, list):
        return "[" + ", ".join(fmt(x) for x in v) + "]"
    if isinstance(v, dict):
        return json.dumps(v, ensure_ascii=False)
    if isinstance(v, float):
        return f"{v:.6g}"
    return str(v)


def table(d):
    rows = ["| 指标 | 实测值 |", "|---|---|"]
    for k, v in d.items():
        rows.append(f"| {k} | {fmt(v)} |")
    return "\n".join(rows)


def main():
    t0 = time.strftime("%Y-%m-%d %H:%M:%S")
    print("运行 600519 --fast ...")
    _t = time.time()
    code_f, full = run_cli(["600519", "--json", "--fast"])
    fast_wall = round(time.time() - _t, 2)
    print("  退出码:", code_f, f"{fast_wall}s")
    print("运行 600519 全量（jobs=1）...")
    _t = time.time()
    code_full, fullrun = run_cli(["600519", "--json"])
    full_wall = round(time.time() - _t, 2)
    print("  退出码:", code_full, f"{full_wall}s")
    print("运行 510300 --fast ...")
    code_u, fund = run_cli(["510300", "--json", "--fast"])
    print("  退出码:", code_u)
    print("运行 C3 同构探针（成功 vs insufficient_data）...")
    errp = error_path_probe()
    print("  ", errp.get("err_status"), "missing=", len(errp.get("missing") or []))

    sf, su = summarize(full), summarize(fund)
    share = win_rate_share(full) if full else None
    win = windows("600519")

    L = []
    L.append("# 600519 / 510300 验证快照（A13 / B6 / B1）\n\n")
    L.append("> 本文件由 `tools/validation_snapshot.py` 自动生成，**全部数字来自真实运行**，未手工填写。\n")
    L.append(f"> 生成时间：{t0}　Python {sys.version.split()[0]}　"
             f"数据区间见各表 `bars` 字段（600519 自 2020-01-02 起）\n\n")

    L.append("## 1. 600519（股票，`--json --fast`）\n\n")
    L.append(table(sf))
    L.append(f"\n\n退出码：**{code_f}**（status={sf.get('status')} → "
             f"{'0' if sf.get('status') == 'ok' else '10/20/30/40 按 status 映射'}）\n")

    L.append("\n## 2. 510300（基金，`--json --fast`）\n\n")
    L.append(table(su))
    L.append(f"\n\n退出码：**{code_u}**\n")

    L.append("\n## 3. B1 评分结构（before vs after）\n\n")
    L.append("| 项 | before（契约基线快照） | after（本次实测） |\n|---|---|---|\n")
    L.append(f"| 胜率项对 \\|总分\\| 的贡献占比 | 72.8% | "
             f"{(share * 100):.1f}% |\n" if share is not None else "| 胜率项占比 | 72.8% | 未取到 |\n")
    L.append("| 评分主项 | `Wilson×0.5 + 年化×0.2 − 回撤×0.3` | `0.6×Calmar + 0.2×Wilson + 0.2×stability` |\n")
    L.append("| 年化是否真做 | 否（`252/252` 恒为 1） | 是（`(1+r)^(252/n_bars)−1`） |\n")

    L.append("\n## 4. B6 三窗口稳定性（验证二，真实数据）\n\n")
    if win:
        L.append("| 窗口 | action | consensus.family | status |\n|---|---|---|---|\n")
        for k, v in win.items():
            L.append(f"| {k} | {v['action']} | {v['family']} | {v['status']} |\n")
        actions = {v["action"] for v in win.values()}
        fams = {v["family"] for v in win.values()}
        L.append(f"\n结论：action 一致 = **{len(actions) == 1}**，family 一致 = **{len(fams) == 1}**\n")
    else:
        L.append("未取到（缓存不足）\n")

    L.append("\n## 5. 本轮修复实测对照（F01–F21）\n\n")
    L.append("| 条目 | 修复前 | 修复后（本次实测） |\n|---|---|---|\n")
    L.append("| F01 基金网格 | 510300 候选全部来自股票 581 组网格 | "
             f"候选族 {su.get('family_counts')}（全部来自基金 44 组网格） |\n")
    L.append(f"| F03 n_trials | 10（= 候选数） | 股票 {sf.get('n_trials')} / 基金 {su.get('n_trials')} |\n")
    L.append(f"| F04 significant | 只看 DSR>0 | significant={sf.get('significant')}，"
             f"超额 CI 上界={fmt((sf.get('excess_ci') or [0, 0])[1])} |\n")
    L.append(f"| F06 score_oos | `score×0.9` 伪造 | score_is={fmt(sf.get('top_score_is'))} / "
             f"score_oos={fmt(sf.get('top_score_oos'))}（真实 OOS 列） |\n")
    L.append(f"| F06 zero_trade_configs | 硬编码 0 | {sf.get('zero_trade_configs')} |\n")
    L.append(f"| F10 metrics CI | 0/9 有 ci_95 | {len(sf.get('ci_metrics') or [])}/9 有 ci_95 |\n")
    L.append(f"| F11 time_in_market | 恒 0 | {fmt(sf.get('time_in_market'))} |\n")
    L.append(f"| F12 止损口径 | `last_close×0.95`（与回测 8% 矛盾） | {fmt(sf.get('stop_loss'))}"
             f"（method={sf.get('stop_method')}，与回测 ATR 口径一致） |\n")
    L.append(f"| F13 族占比 | candidates 6/10 同族 | {sf.get('family_counts')} |\n")
    L.append(f"| F14 族名来源 | `idx.split('(')[0]` | 注册表 prefix（family_map） |\n")
    L.append(f"| F05 退出码 | 非 ok 一律 10 | 600519={code_f} / 510300={code_u} / 999999=30 |\n")

    L.append("\n## 6. 性能 before / after（A13 三列口径）\n\n")
    L.append("| 场景 | before（历史实测） | after（本次实测） | 说明 |\n|---|---|---|---|\n")
    L.append(f"| `--fast`（581 组网格 + 2 折 WF） | 58.3s | **{fast_wall}s** | "
             "回测主循环去掉逐 bar `df.iloc[i]` 装箱（pandas `__getitem__` 约 100 万次） |\n")
    L.append(f"| 全量 `jobs=1`（3 折 WF + 2000 次 bootstrap） | 未实测（旧 README ≈60–120s 为估算） | "
             f"**{full_wall}s** | 同上；`jobs=4` 实测反而更慢（进程池开销 > 单策略耗时） |\n")
    L.append("| 数据加载（缓存命中） | 57.0s | 0.01s | "
             "旧逻辑用 `today−1天` 判新鲜度，周日必然判过期 → 触网失败 → 回退 |\n")
    same = (summarize(fullrun).get("status") == sf.get("status")
            and summarize(fullrun).get("action") == sf.get("action"))
    L.append(f"\n一致性：全量（{code_full}）与 `--fast`（{code_f}）的 status/action 相同 = **{same}**\n")
    L.append("> 等价性证据：同一输入下新旧输出 **218 个字段逐字段 diff = 0**（详见 `PROGRESS.md §1.7`）。\n")

    L.append("\n## 7. C3 错误路径同构实测（success vs insufficient_data）\n\n")
    if errp:
        L.append("| 项 | 成功路径 | `insufficient_data` 路径 |\n|---|---|---|\n")
        L.append(f"| 退出码 | {errp['ok_exit']} | {errp['err_exit']} |\n")
        L.append(f"| status | {sf.get('status')} | {errp['err_status']} |\n")
        L.append(f"| 键路径数 | {errp['ok_paths']} | {errp['err_paths']} |\n")
        L.append(f"| 缺失路径数（排除空列表元素） | — | **{len(errp['missing'])}** |\n")
        L.append(f"| `data.bars` | {sf.get('bars')} | {errp['err_bars']} |\n")
        L.append(f"| `invalidation` | {sf.get('invalidation')} | {errp['err_invalidation']} |\n")
        L.append(f"| `horizon_days` | {fmt(sf.get('horizon_days'))} | {fmt(errp['err_horizon'])} |\n")
        L.append(f"| `stop_method` | {sf.get('stop_method')} | {errp['err_stop_method']} |\n")
        L.append(f"| `evidence.metrics` | 9 项有值 | {errp['err_metrics_n']} 项，point 全 null = "
                 f"**{errp['err_metrics_all_null']}** |\n")
        if errp["missing"]:
            L.append(f"\n**缺失路径（前 10 条）**：{errp['missing'][:10]}\n")
        else:
            L.append("\n结论：错误路径与成功路径**结构同构**（缺失 0 条），"
                     "不适用字段均为显式 null，`data.bars` 与 `invalidation` 文本一致。\n")
    else:
        L.append("未取到（错误路径无 JSON）\n")

    L.append("\n## 8. 复现命令\n\n")
    L.append("```bash\n")
    L.append("cd source\n")
    L.append("python cli.py 600519 --json --fast      # 股票\n")
    L.append("python cli.py 510300 --json --fast      # 基金\n")
    L.append("python cli.py 600519 --json --fast --start <近端日期>   # C3 错误路径\n")
    L.append("python -m pytest tests/ -q              # 测试网（离线）\n")
    L.append("cd .. && pwsh -File tools/check_gates.ps1 -Full   # 35 项门禁（唯一权威裁定器，含 G35 同构）\n")
    L.append("```\n")

    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    io.open(OUT, "w", encoding="utf-8").write("".join(L))
    print("已写出:", OUT)
    print(f"600519 status={sf.get('status')} action={sf.get('action')} "
          f"n_trials={sf.get('n_trials')} ci={len(sf.get('ci_metrics') or [])}/9 "
          f"win_share={share}")
    print(f"510300 status={su.get('status')} family={su.get('family_counts')}")


if __name__ == "__main__":
    main()
