# -*- coding: utf-8 -*-
"""check_gates.ps1 的**等价 Python 版**（1:1 转写，判定不弱化）。

为什么存在：
    本机（2026-10-17 起）宿主 PowerShell 沙箱创建失败
    （`LsSDK_CreateSandbox(ConPTY) 失败(6): LsInject_CrossArch(WoW64 rundll32 bridge 注入) 失败`），
    `pwsh -File tools/check_gates.ps1` 无法执行。为了让"唯一完成定义"仍然可机器判定，
    这里把 ps1 的每一项判定逐条转写为 Python，**判定条件、阈值、退出码完全一致**。

维护约定：
    * 修改任一侧的门禁，必须同步修改另一侧；
    * 这里**只允许加强**，任何弱化（放宽阈值 / 删除断言 / 把 FAIL 变 SKIP）都视为作弊（AGENTS.md §4）；
    * 输出仍写 `gates-report.json`，字段与 ps1 版一致。

用法：
    python tools/check_gates.py            # 完整门禁
    python tools/check_gates.py --quick    # 跳过 6 项最慢的
    python tools/check_gates.py --full     # 额外跑 G14 全量性能
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "source")
CLI_PY = os.path.join(SRC, "cli.py")
README = os.path.join(ROOT, "README.md")
REPORT = os.path.join(ROOT, "gates-report.json")

RESULTS = []


def add_gate(gid, name, status, detail="", evidence=None):
    RESULTS.append({"Id": gid, "Name": name, "Status": status,
                    "Detail": re.sub(r"\s+", " ", str(detail)).strip(),
                    "Evidence": evidence})
    print(f"  {gid:<4} {status:<8} {name}")
    if detail and status != "PASS":
        print(f"        └─ {str(detail)[:240]}")


def run(cmd, cwd, timeout=900):
    p = subprocess.run(cmd, cwd=cwd, capture_output=True, timeout=timeout)
    return (p.returncode,
            p.stdout.decode("utf-8", "replace"),
            p.stderr.decode("utf-8", "replace"))


def gate_g01():
    code, out, _err = run([sys.executable, "-m", "pytest", "tests/", "-q", "--tb=line"], SRC, 1800)
    m = re.search(r"(\d+) passed", out)
    passed = int(m.group(1)) if m else 0
    failed = int(m.group(1)) if (m := re.search(r"(\d+) failed", out)) else 0
    errored = int(m.group(1)) if (m := re.search(r"(\d+) error", out)) else 0
    name = "测试网离线全绿（≥40 passed）"
    if failed == 0 and errored == 0 and passed >= 40:
        add_gate("G01", name, "PASS", f"{passed} passed / {failed} failed", out.strip())
    else:
        add_gate("G01", name, "FAIL",
                 f"{passed} passed / {failed} failed / {errored} error", out.strip())


def gate_g09():
    src = open(CLI_PY, encoding="utf-8").read()
    hits = [i + 1 for i, line in enumerate(src.splitlines()) if re.search(r"if best_name\s*==", line)]
    if not hits:
        add_gate("G09", "cli.py 无 if best_name == 硬编码分支", "PASS", "0 处匹配")
    else:
        add_gate("G09", "cli.py 无 if best_name == 硬编码分支", "FAIL",
                 f"{len(hits)} 处匹配: {hits[:3]}")


def gate_g10():
    n = len(open(CLI_PY, encoding="utf-8").read().splitlines())
    if n <= 700:
        add_gate("G10", "cli.py ≤ 700 行", "PASS", f"{n} 行")
    else:
        add_gate("G10", "cli.py ≤ 700 行", "FAIL", f"{n} 行 > 700")


def gate_g11():
    pat = re.compile(r"watchlist|save_config|load_config|stock_config|fund_config")
    hits = []
    for dirpath, _dirs, files in os.walk(SRC):
        if "strategy_master" in dirpath:
            continue
        for f in files:
            if not f.endswith(".py"):
                continue
            p = os.path.join(dirpath, f)
            for i, line in enumerate(open(p, encoding="utf-8", errors="replace").read().splitlines(), 1):
                if pat.search(line):
                    hits.append(f"{f}:{i}")
    name = "source/** 无 watchlist/save_config/load_config 残留"
    if not hits:
        add_gate("G11", name, "PASS", "0 处（strategy_master 白名单除外）")
    else:
        add_gate("G11", name, "FAIL", f"{len(hits)} 处: {hits[:3]}")


def gate_g13():
    txt = open(README, encoding="utf-8").read() if os.path.exists(README) else ""
    has_section = "给 Agent 的接口契约" in txt
    blocks = re.findall(r"```json\s*(.*?)```", txt, re.S)
    bad = 0
    for b in blocks:
        try:
            json.loads(b)
        except Exception:
            bad += 1
    name = "README 含 Agent 契约章节且示例 JSON 可解析"
    if has_section and blocks and bad == 0:
        add_gate("G13", name, "PASS", f"章节存在，{len(blocks)} 个 JSON 块全部可解析")
    else:
        add_gate("G13", name, "FAIL",
                 f"章节={has_section}, json块={len(blocks)}, 解析失败={bad}")


MAPPING = [
    ("json", "G02", "CLI --json 契约（C1 纯 JSON + C3 字段齐备 + 真数据）"),
    ("semantic", "G03", "输出语义自洽（C4：无效候选不得给 buy / triggers 非空）"),
    ("fund", "G04", "基金同契约（510300 --json，F21 已收紧）"),
    ("idempotent", "G05", "幂等确定性（C8：两次运行除时间戳外逐字节相同）"),
    ("windows", "G06", "三窗口 action/共识族一致（B6）"),
    ("advise", "G07", "单一真源 API（C6：advise() 键集合 == --json 键集合）"),
    ("a1", "G08", "A1 结构性不变式（自适应资金下 100% 可负担一手）"),
    ("fund_grid", "G15", "基金候选必须全部来自基金网格（F01）"),
    ("type_hint", "G16", "--type 生效（F02）"),
    ("n_trials", "G17", "n_trials ≥ 实际评分配置数（F03/B4）"),
    ("significance", "G18", "significant / no_edge 与超额收益 CI 自洽（F04/B8）"),
    ("exit_codes", "G19", "退出码与 status 一一对应（F05/C5）"),
    ("placeholders", "G20", "禁止占位常量（F06）"),
    ("family_map", "G21", "族名来自注册表 prefix（F14）"),
    ("family_ratio", "G22", "B5 任一族占比 ≤ 50%（F13）"),
    ("metrics_ci", "G23", "metrics CI 覆盖率 ≥5 且点估计在区间内（F10/B7）"),
    ("time_in_market", "G24", "time_in_market 非恒 0（F11/A4）"),
    ("sizing", "G25", "sizing 单一真源，cli 无硬编码止损（F12）"),
    ("win_rate_share", "G26", "胜率项贡献 ≤ 40%（F07/B1）"),
    ("atr_stop", "G27", "ATR 自适应止损生效（F08/B2）"),
    ("walk_forward", "G28", "walk-forward 逐折重新寻优（F09/B3）"),
    ("old_args", "G29", "D6 旧参数不报 usage error（F16）"),
    ("requirements", "G30", "requirements 与实测环境一致（F17/D3e）"),
    ("fund_grid_consistency", "G31", "基金网格单一真源自洽（F15）"),
    ("fund_cache", "G32", "失败拉取不写空壳缓存（F20）"),
]
QUICK_SKIP = {"fund", "idempotent", "windows", "exit_codes", "type_hint", "fund_cache"}


def main():
    quick = "--quick" in sys.argv
    full = "--full" in sys.argv
    print(f"\n=== Hermes 门禁检查 ({time.strftime('%Y-%m-%d %H:%M:%S')}) ===")
    print(f"仓库: {ROOT}\n")

    gate_g01()
    gate_g09()
    gate_g10()
    gate_g11()
    gate_g13()

    mode = "all-quick" if quick else "all"
    t0 = time.time()
    code, out, err = run([sys.executable, os.path.join(ROOT, "tools", "probe.py"), mode], ROOT, 3600)
    try:
        probe = json.loads(out)
    except Exception:
        probe = None
        print(f"[probe 输出非 JSON] rc={code} err={err[:300]}")

    if probe is None:
        add_gate("G02", "CLI --json 契约", "FAIL", "探针未产出 JSON")
        for _k, gid, nm in MAPPING[1:]:
            add_gate(gid, "（探针缺失，无法判定）", "BLOCKED", "probe.py 未产出结果")
    else:
        for key, gid, nm in MAPPING:
            r = probe.get(key)
            if r is None:
                add_gate(gid, nm, "BLOCKED", "探针无此键")
                continue
            if quick and key in QUICK_SKIP:
                add_gate(gid, nm, "SKIP", "-Quick 跳过了该项（完成声明前必须跑完整门禁）")
                continue
            detail = r.get("reason") or json.dumps(r.get("observed") or {}, ensure_ascii=False)
            add_gate(gid, nm, str(r.get("status")), detail, r)

        sec = (probe.get("json") or {}).get("elapsed_sec")
        if sec is None:
            add_gate("G12", "--fast 墙钟 ≤ 60s", "BLOCKED", "未取得耗时")
        elif float(sec) <= 60:
            add_gate("G12", "--fast 墙钟 ≤ 60s", "PASS", f"{sec} s")
        else:
            add_gate("G12", "--fast 墙钟 ≤ 60s", "FAIL", f"{sec} s > 60s")

    if full:
        sw = time.time()
        run([sys.executable, "cli.py", "600519", "--json", "--jobs", "4"], SRC, 900)
        full_sec = round(time.time() - sw, 1)
        if full_sec <= 300:
            add_gate("G14", "全量模式墙钟 ≤ 300s", "PASS", f"{full_sec} s")
        else:
            add_gate("G14", "全量模式墙钟 ≤ 300s", "FAIL", f"{full_sec} s")
    else:
        add_gate("G14", "全量模式墙钟 ≤ 300s", "SKIP", "需 --full 才执行")

    fail = [r for r in RESULTS if r["Status"] == "FAIL"]
    blocked = [r for r in RESULTS if r["Status"] == "BLOCKED"]
    skip = [r for r in RESULTS if r["Status"] == "SKIP"]
    passed = [r for r in RESULTS if r["Status"] == "PASS"]
    exit_code = len(fail) + len(blocked)

    report = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "repo": ROOT,
        "runner": "tools/check_gates.py (等价转写 tools/check_gates.ps1)",
        "probe_seconds": round(time.time() - t0, 1),
        "summary": {"pass": len(passed), "fail": len(fail), "blocked": len(blocked),
                    "skip": len(skip), "exit_code": exit_code},
        "gates": RESULTS,
    }
    with open(REPORT, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)

    print(f"\nPASS {len(passed)} | FAIL {len(fail)} | BLOCKED {len(blocked)} | "
          f"SKIP {len(skip)} → 退出码 {exit_code}")
    print(f"报告: {REPORT}")
    if exit_code:
        print("\n未通过项（这些就是当前必须做的工作，不得跳过）：")
        for r in fail + blocked:
            print(f"  {r['Id']} {r['Name']} — {r['Detail']}")
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
