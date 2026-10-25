# -*- coding: utf-8 -*-
"""取证脚本：把"只能在有权限的环境里跑"的验证一次性收集成可回传的证据包。

背景
----
本项目的权威门禁（`tools/check_gates.ps1`）需要 PowerShell 宿主；在部分受限环境里
沙箱创建失败（`LsSDK_CreateSandbox(ConPTY) 0x6`），只能跑 `tools/probe.py`。
此外冷克隆、真实取数、Hermes 端到端解析、20 日前向验证都无法在单机内自证。

用法
----
    cd <repo root>
    python tools/collect_evidence.py              # 全量（约 6–10 分钟）
    python tools/collect_evidence.py --skip-gates # 跳过权威门禁（先验证脚本本身）
    python tools/collect_evidence.py --only pytest,probe

产出
----
    evidence/<时间戳>/
        REPORT.md            人读的汇总（含每步判定与关键数字）
        manifest.json        机读的汇总
        env.txt              环境与 git 状态
        gates_full.txt       权威门禁原始输出（stdout+stderr）
        pytest.txt           测试原始输出
        probe_raw.json       探针原始输出
        probe_summary.json   探针逐项状态
        idem_a.json / idem_b.json / idem_result.json
        asof_2026-10-22.json / asof_2026-10-23.json / asof_summary.json
        timing.json          全量/--fast 墙钟

脚本**只读仓库**，不修改任何源码；唯一写入是 `evidence/` 目录。
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "source"
OUT = ROOT / "evidence" / datetime.now().strftime("%Y%m%d-%H%M%S")

PY = sys.executable
ENV = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1")


def run(cmd, cwd=None, timeout=1800):
    """跑命令，返回 (exit, stdout, stderr, seconds)。不抛异常。"""
    t0 = time.perf_counter()
    try:
        p = subprocess.run(cmd, cwd=str(cwd or ROOT), env=ENV, timeout=timeout,
                           capture_output=True, text=True, encoding="utf-8", errors="replace")
        return p.returncode, p.stdout, p.stderr, round(time.perf_counter() - t0, 2)
    except subprocess.TimeoutExpired:
        return -9, "", f"TIMEOUT after {timeout}s", round(time.perf_counter() - t0, 2)
    except FileNotFoundError as e:
        return -8, "", f"NOT FOUND: {e}", 0.0
    except Exception as e:  # noqa: BLE001
        return -7, "", f"{type(e).__name__}: {e}", 0.0


def write(name: str, text: str):
    (OUT / name).write_text(text, encoding="utf-8")


def step_env():
    lines = [
        f"time: {datetime.now().isoformat()}",
        f"python: {sys.version}",
        f"executable: {PY}",
        f"platform: {platform.platform()}",
        f"cwd: {ROOT}",
        "",
    ]
    for label, cmd in (("git_head", ["git", "log", "-1", "--format=%H %ci"]),
                       ("git_status", ["git", "status", "--short"]),
                       ("git_ahead", ["git", "rev-list", "--count", "origin/main..HEAD"]),
                       ("git_remote", ["git", "remote", "-v"])):
        code, out, err, _ = run(cmd)
        lines.append(f"[{label}] exit={code}")
        lines.append((out or err).strip())
        lines.append("")
    write("env.txt", "\n".join(lines))
    return {"name": "env", "ok": True, "file": "env.txt"}


def step_gates():
    ps = None
    for cand in ("pwsh", "powershell"):
        code, _, _, _ = run([cand, "-NoProfile", "-Command", "exit 0"], timeout=60)
        if code == 0:
            ps = cand
            break
    if ps is None:
        write("gates_full.txt", "未找到 pwsh / powershell，无法运行权威门禁。")
        return {"name": "gates", "ok": False, "file": "gates_full.txt",
                "note": "no powershell available"}
    code, out, err, sec = run([ps, "-NoProfile", "-ExecutionPolicy", "Bypass",
                               "-File", str(ROOT / "tools" / "check_gates.ps1"), "-Full",
                               "-Python", PY],
                              timeout=3600)
    write("gates_full.txt", f"$ {ps} -File tools/check_gates.ps1 -Full -Python {PY}\n"
                            f"exit={code}  seconds={sec}\n"
                            f"----- stdout -----\n{out}\n----- stderr -----\n{err}\n")
    return {"name": "gates", "ok": code == 0, "exit": code, "seconds": sec,
            "file": "gates_full.txt"}


def step_pytest():
    code, out, err, sec = run([PY, "-m", "pytest", "tests/", "-q"], cwd=SRC, timeout=1800)
    write("pytest.txt", f"exit={code} seconds={sec}\n{out}\n{err}\n")
    tail = (out or "").strip().splitlines()[-1:] or [""]
    return {"name": "pytest", "ok": code == 0, "exit": code, "seconds": sec,
            "summary": tail[0], "file": "pytest.txt"}


def step_probe():
    code, out, err, sec = run([PY, "tools/probe.py", "all"], timeout=3600)
    write("probe_raw.json", out + ("\n--- stderr ---\n" + err if err.strip() else ""))
    try:
        data = json.loads(out[out.find("{"):])
    except Exception as e:  # noqa: BLE001
        write("probe_summary.json", json.dumps({"parse_error": str(e)}, ensure_ascii=False, indent=2))
        return {"name": "probe", "ok": False, "exit": code, "seconds": sec,
                "note": f"parse failed: {e}", "file": "probe_raw.json"}
    bad = {k: {"status": v.get("status"), "reason": v.get("reason")}
           for k, v in data.items() if isinstance(v, dict) and v.get("status") != "PASS"}
    summary = {"keys": len(data), "non_pass": bad, "seconds": sec}
    write("probe_summary.json", json.dumps(summary, ensure_ascii=False, indent=2))
    return {"name": "probe", "ok": not bad, "exit": code, "seconds": sec,
            "keys": len(data), "non_pass": len(bad), "file": "probe_summary.json"}


def _strip_volatile(d: dict) -> dict:
    d = json.loads(json.dumps(d))
    d.pop("generated_at", None)
    (d.get("diagnostics") or {}).pop("elapsed_seconds", None)
    return d


def step_idempotent():
    cmd = [PY, "cli.py", "600519", "--json", "--fast"]
    c1, o1, e1, s1 = run(cmd, cwd=SRC, timeout=900)
    c2, o2, e2, s2 = run(cmd, cwd=SRC, timeout=900)
    write("idem_a.json", o1 or e1)
    write("idem_b.json", o2 or e2)
    try:
        a = _strip_volatile(json.loads(o1))
        b = _strip_volatile(json.loads(o2))
        same = json.dumps(a, sort_keys=True, ensure_ascii=False) == json.dumps(b, sort_keys=True, ensure_ascii=False)
    except Exception as e:  # noqa: BLE001
        same = False
        write("idem_result.json", json.dumps({"error": str(e)}, ensure_ascii=False))
        return {"name": "idempotent", "ok": False, "note": str(e)}
    write("idem_result.json", json.dumps({"identical": same, "exit": [c1, c2],
                                          "seconds": [s1, s2]}, ensure_ascii=False, indent=2))
    # 退出码 10 = no_edge，仍是"正常产出"，只有 2x/30/40 才算失败
    return {"name": "idempotent", "ok": bool(same and c1 in (0, 10) and c2 in (0, 10)),
            "identical": same, "file": "idem_result.json"}


def step_asof():
    rows = {}
    ok = True
    for day in ("2026-10-22", "2026-10-23"):
        cmd = [PY, "cli.py", "002415", "--json", "--fast", "--as-of", day]
        code, out, err, sec = run(cmd, cwd=SRC, timeout=900)
        write(f"asof_{day}.json", out or err)
        try:
            d = json.loads(out)
            rec, dd, sg = d["recommendation"], d["data"], d["recommendation"]["signal"]
            rows[day] = {
                "exit": code, "seconds": sec,
                "bars": dd.get("bars"), "end": str(dd.get("end"))[:10],
                "last_close": dd.get("last_close"),
                "status": rec.get("status"), "action": rec.get("action"),
                "target_position": rec.get("target_position"),
                "review_date": rec.get("review_date"),
                "position_state": sg.get("position_state"),
                "state": sg.get("state"),
                "current_value": sg.get("current_value"),
                "action_reason": rec.get("action_reason"),
                "execution": dd.get("execution"),
                "window_note": dd.get("window_note"),
                "expectancy": rec.get("side_expectancy"),
            }
            if code not in (0, 10):
                ok = False
        except Exception as e:  # noqa: BLE001
            rows[day] = {"exit": code, "error": str(e)}
            ok = False
    write("asof_summary.json", json.dumps(rows, ensure_ascii=False, indent=2))
    return {"name": "asof", "ok": ok, "file": "asof_summary.json",
            "observed": {k: {"action": v.get("action"), "bars": v.get("bars")} for k, v in rows.items()}}


def step_timing():
    out = {}
    for label, extra in (("full", []), ("fast", ["--fast"])):
        cmd = [PY, "cli.py", "600519", "--json"] + extra
        code, _, err, sec = run(cmd, cwd=SRC, timeout=1800)
        out[label] = {"exit": code, "seconds": sec, "error": (err or "")[:200]}
    write("timing.json", json.dumps(out, ensure_ascii=False, indent=2))
    return {"name": "timing", "ok": all(v["exit"] in (0, 10) for v in out.values()),
            "observed": {k: v["seconds"] for k, v in out.items()}, "file": "timing.json"}


STEPS = {
    "env": step_env,
    "gates": step_gates,
    "pytest": step_pytest,
    "probe": step_probe,
    "idempotent": step_idempotent,
    "asof": step_asof,
    "timing": step_timing,
}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default="", help="逗号分隔的步骤名，默认全跑")
    ap.add_argument("--skip-gates", action="store_true")
    args = ap.parse_args()

    OUT.mkdir(parents=True, exist_ok=True)
    names = [s.strip() for s in args.only.split(",") if s.strip()] or list(STEPS)
    if args.skip_gates and "gates" in names:
        names.remove("gates")

    results = []
    for name in names:
        print(f"[{name}] ...", flush=True)
        try:
            r = STEPS[name]()
        except Exception as e:  # noqa: BLE001
            r = {"name": name, "ok": False, "note": f"{type(e).__name__}: {e}"}
        results.append(r)
        print(f"    -> {'OK' if r.get('ok') else 'FAIL'} {r.get('note', '')}", flush=True)

    manifest = {"generated_at": datetime.now().isoformat(),
                "python": sys.version, "root": str(ROOT), "results": results}
    (OUT / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    lines = ["# 取证报告", "",
             f"- 生成时间：{manifest['generated_at']}",
             f"- Python：{sys.version.split()[0]}",
             f"- 目录：`{OUT}`", "",
             "| 步骤 | 结果 | 关键数字 | 文件 |", "|---|---|---|---|"]
    for r in results:
        num = ""
        if "keys" in r:
            num = f"probe keys={r['keys']}, non_pass={r.get('non_pass')}"
        elif "seconds" in r and r["name"] == "gates":
            num = f"exit={r.get('exit')}, {r['seconds']}s"
        elif "observed" in r:
            num = json.dumps(r["observed"], ensure_ascii=False)[:160]
        elif "summary" in r:
            num = r["summary"]
        lines.append(f"| {r['name']} | {'PASS' if r.get('ok') else 'FAIL'} | {num} | {r.get('file','')} |")

    lines += ["", "## 需要人工补充的三项", "",
              "1. `hermes.txt`：Hermes 对 002415（`--as-of 2026-10-22` 与 `2026-10-23`）的**实际输出原文**，",
              "   以及它收到的那份 JSON（或其 hash）。用于确认下游不再把 `hold` 读成卖出风险提示。",
              "2. `push.txt`：`git push origin main` 的完整输出与 `git log --oneline -12`。",
              "3. `forward.csv`：20 个交易日后，把这 14 只标的的**实际收益**与今日的建议对照（见 VERIFICATION_HANDOFF.md §5）。",
              "", "把整个 `evidence/` 目录压缩回传即可。"]
    (OUT / "REPORT.md").write_text("\n".join(lines), encoding="utf-8")
    print(f"\n证据目录：{OUT}")


if __name__ == "__main__":
    main()
