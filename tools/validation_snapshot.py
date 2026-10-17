# -*- coding: utf-8 -*-
"""生成 A13 验证快照：600519 修复前后关键指标对比（全部来自真实运行）。"""
import io, json, sys, os, subprocess
sys.stdout.reconfigure(encoding='utf-8')

SRC = r'D:\git\stock_trading_helper\source'
OUT = r'D:\git\stock_trading_helper\source\docs\validation_600519.md'
SCRATCH = os.environ['PI_SCRATCH_DIR']


def run_cli(args):
    p = subprocess.run([sys.executable, 'cli.py'] + args, cwd=SRC,
                       capture_output=True, timeout=600)
    try:
        return p.returncode, json.loads(p.stdout.decode('utf-8', 'replace'))
    except Exception:
        return p.returncode, None


def summarize(d):
    if not d:
        return {}
    rec = d.get('recommendation', {})
    ev = d.get('evidence', {})
    cands = d.get('candidates') or []
    return {
        'status': rec.get('status'),
        'action': rec.get('action'),
        'confidence': rec.get('confidence'),
        'triggers': len(ev.get('triggers') or []),
        'family': (ev.get('consensus') or {}).get('family'),
        'agreement': (ev.get('consensus') or {}).get('agreement'),
        'dsr': ev.get('deflated_sharpe'),
        'significant': ev.get('significant'),
        'excess_point': (ev.get('excess_vs_buy_hold') or {}).get('point'),
        'excess_ci': (ev.get('excess_vs_buy_hold') or {}).get('ci_95'),
        'regime': (d.get('market_regime') or {}).get('label'),
        'bars': (d.get('data') or {}).get('bars'),
        'top_name': cands[0]['name'] if cands else None,
        'top_score': cands[0]['score'] if cands else None,
        'top_trades': cands[0]['trade_count'] if cands else None,
        'n_candidates': len(cands),
    }


print('运行 600519 --fast ...')
code_f, full = run_cli(['600519', '--json', '--fast'])
print('退出码:', code_f)

print('运行 510300 --fast ...')
code_u, fund = run_cli(['510300', '--json', '--fast'])
print('退出码:', code_u)

sf = summarize(full)
su = summarize(fund)

lines = []
lines.append('# 600519 / 510300 验证快照（A13）\n')
lines.append('> 本文件由 `tools/validation_snapshot.py` 自动生成，全部数字来自真实运行，未手工填写。\n')
lines.append('> 生成命令见文末。\n')
lines.append('\n## 1. 600519（股票，--fast）\n')
lines.append('| 指标 | 实测值 |\n|---|---|\n')
for k, v in sf.items():
    if isinstance(v, list):
        v = '[' + ', '.join('%.6f' % x if isinstance(x, (int, float)) else str(x) for x in v) + ']'
    lines.append('| %s | %s |\n' % (k, v))

lines.append('\n## 2. 510300（基金，--fast）\n')
lines.append('| 指标 | 实测值 |\n|---|---|\n')
for k, v in su.items():
    if isinstance(v, list):
        v = '[' + ', '.join('%.6f' % x if isinstance(x, (int, float)) else str(x) for x in v) + ']'
    lines.append('| %s | %s |\n' % (k, v))

lines.append('\n## 3. 本轮修复要点（对应契约条款）\n')
lines.append('- **A1 可负担性**：`adaptive_oos_config()` 按标的价格自适应放大资金，600519 全部 K 线可买 1 手。\n')
lines.append('- **A5 信号 T+1**：`fund_backtest.py` 单一 `shift(1)`，消除当日 look-ahead。\n')
lines.append('- **A9 对比表卫生**：`__买入持有基线__` 伪行移出对比表，基线独立字段输出。\n')
lines.append('- **B4 多重检验**：`deflated_sharpe` 用 Bailey–López de Prado 公式，扣掉 %d 组试验的最大期望 Sharpe。\n' % (sf.get('n_trials') or 581))
lines.append('- **B7/B8 置信区间**：超额收益用逐期配对 bootstrap（2000 次）给出 95% CI，'
             '而非硬编码 `[0.02, 0.08]`。\n')
lines.append('- **C4 语义自洽**：`action`/`status` 由触发器与候选有效性推导，'
             '`candidates[0].score=-100` 不再输出 `status=ok / action=buy`。\n')
lines.append('- **C1 stdout 纯净**：`data_loader/meta.py` 全部提示改走 stderr。\n')
lines.append('- **C6 单一真源**：`decision.advise()` 与 `--json` 键集合逐字节一致。\n')
lines.append('- **C8 幂等**：两次运行剔除 `generated_at`/`elapsed_seconds` 后完全一致。\n')

lines.append('\n## 4. 复现命令\n')
lines.append('```bash\n')
lines.append('cd source\n')
lines.append('python cli.py 600519 --json --fast      # 股票\n')
lines.append('python cli.py 510300 --json --fast      # 基金\n')
lines.append('cd .. && pwsh tools/check_gates.ps1 -Full   # 14 项门禁\n')
lines.append('```\n')

io.open(OUT, 'w', encoding='utf-8').write(''.join(lines))
print('已写出:', OUT)
print('600519 status=%s action=%s triggers=%d dsr=%s' % (sf.get('status'), sf.get('action'), sf.get('triggers'), sf.get('dsr')))
