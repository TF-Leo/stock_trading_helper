# 外部验证交接单（v2）

> 更新：2026-10-25 12:00　代码：`main` @ `213edee`（领先 origin 10 个提交）
> **v1 的错误**：v1 写着"我无法运行权威门禁"。实测发现可以——只要给门禁指定带依赖的
> 解释器。该项已由我自证，见 §1。本版只保留**真正**需要你在有权限环境完成的事。

---

## 1. 我已经自证的（不需要你重跑，但欢迎交叉复核）

| 项 | 命令 | 结果 | 证据 |
|---|---|---|---|
| 权威门禁 | `check_gates.ps1 -Full -Python <py3.12>` | **PASS 48 / FAIL 0 / BLOCKED 0 / SKIP 0**，退出码 **0** | `evidence/20261025-120754/gates_full.txt` |
| 测试 | `pytest tests/ -q` | **67 passed / 11.1s** | `pytest.txt` |
| 探针 | `probe.py all` | **41 keys，0 non-PASS** | `probe_summary.json` |
| 幂等 | `cli.py 600519 --json --fast` ×2 | 除 `generated_at`/`elapsed_seconds` 外完全一致 | `idem_result.json` |
| 历史截面 | `--as-of 2026-10-22 / 10-23` | `hold` / 3927 根、`hold` / 3928 根；文案不含"卖出信号" | `asof_summary.json` |
| 横截面 IC | `tools/ic_report.py` | 20 日不重叠超额 **+2.55%，t=2.82，胜率 56.3%** | `evidence/ic_report.csv` |

> 门禁首次运行 `PASS 0` 式集体翻红，原因是它调用 PATH 上的 `python`（本机 3.13，无 pandas）。
> 已给脚本加 `-Python` 参数与 `PYTHON_BIN` 环境变量。**这是环境问题，不是代码问题**，
> 但它说明：门禁必须能指定解释器，否则在别的机器上会误判。

---

## 2. 仍需你做的四件事

### V2　冷克隆（10 分钟）

我这边无法证明"别人 clone 下来能跑"：缓存目录 `source/stock_analysis/data/` 被 gitignore，
干净 clone 没有数据。

```bash
git clone <repo-url> /tmp/sth && cd /tmp/sth/source
python -m pytest tests/ -q          # 期望 67 passed（测试全部用合成数据，不需要行情）
```

**回传**：`cold_clone.txt`（clone 输出 + pytest 汇总 + `python -c "import cli"` 的结果）。
**判定**：`67 passed`、`0 failed`；若失败，把第一条报错的完整栈贴回来。

---

### V3　Hermes 端到端（最重要，20 分钟）

最初的事故就发生在这里：Hermes 把 `action_reason` 里的"卖出信号"当成了正式建议。
现在 `action=hold`、文案是"持有中（第 4/20 个交易日…）"。需要确认它**不再误读**。

```bash
cd source
python cli.py 002415 --json --fast --as-of 2026-10-22 > /tmp/h22.json
python cli.py 002415 --json --fast --as-of 2026-10-23 > /tmp/h23.json
sha256sum /tmp/h22.json /tmp/h23.json
```

然后**用你平时的方式**让 Hermes 读这两份 JSON（或让它自己跑上面两条命令），回传 `hermes.txt`，内含：

1. **你给 Hermes 的完整提示词原文**（不要改写）；
2. **Hermes 渲染给用户的输出原文**；
3. 两份 JSON 的 sha256（或整份 JSON）；
4. Hermes 的版本/提交号，以及它**如何取动作**：读 `recommendation.action`，还是从文案抓关键词？

**判定**：Hermes 的输出里**不应出现**"卖出/减仓/风险提示"这类结论。
若出现，请把**它引用的那句原文**一起贴出来——那说明解析层还在按关键词判断，需要在它那侧改。

---

### V4　推送（2 分钟）

```bash
git log --oneline -12 > push.txt
git push origin main >> push.txt 2>&1
git status >> push.txt
```

**回传** `push.txt`。若 `github.com` 仍解析失败，原样保留报错即可，不影响其余验证。

---

### V5　20 日前向验证（需要等 20 个交易日）

全部结论目前都是**样本内**的。唯一能证伪"20 日有效期"的办法是把今天的建议冻住，等 20 个交易日。

```bash
python tools/forward_log.py record          # 今天跑一次；之后每个交易日都跑一次
# 20 个交易日后：
python tools/forward_log.py evaluate
```

**回传**：`evidence/forward_log.csv` 与 `evidence/forward_eval.csv`。

**判定**：看 `matured=true` 的行里，`action=buy` 的 `hit` 比例与 `horizon_return` 均值。
- 命中率 ≥ 55%、均值为正 → 20 日契约成立；
- 若接近 50%，说明单标的择时没有边际，应转向 V6 的横截面路线。

> 建议每天跑一次 `record`：只记录 `hold` 也能留下"当时的仓位与复审日"，
> 而且能捕捉到真正的 `buy`/`sell` 事件。

---

### V6　扩大票池复验 IC（可选，但决定下一步方向，1 小时）

14 只大盘股的横截面统计功效有限。这条决定"横截面排序"能不能作为下一版主线。

```bash
python tools/warm_cache.py 600000 600009 600016 600028 600030 ...   # 200–500 只，可分批
python tools/ic_report.py --min-bars 500
```

**回传**：`evidence/ic_report.csv`、`evidence/ic_spread.csv`，以及票池只数与时间区间。

**判定**：
- `mom60` 在 10/20 日的 IC 仍显著（|t|>2）→ 横截面路线成立，下一版做"排名 + 定期换仓"；
- 若显著但方向相反 → 说明 14 只票的结论是样本偏差，需重定因子；
- 若都不显著 → 放弃横截面，回到单标的风控路线（v3.0 现状）。

---

## 3. 一键取证（可选）

如果你想把 §1 的数字在自己机器上复现一遍：

```bash
python tools/collect_evidence.py            # 约 15 分钟，含门禁 370s + 探针 346s
```

产物在 `evidence/<时间戳>/`，`evidence/` 已 gitignore，不会污染仓库。
脚本只读代码，不修改任何源码。

---

## 4. 请不要做的事

1. **不要改代码或调参数**去让某一步变绿——失败就原样回传，那才是有用的信息。
2. **不要删 `source/stock_analysis/data/` 下的缓存**，否则 `--as-of` 的历史截面无法复现。
3. **不要手工摘录门禁输出**；`gates_full.txt` 必须是 `-Full` 的原始 stdout。
4. **不要把 `evidence/` 强行 add 进仓库**（已在 `.gitignore`）。

---

## 5. 只有 10 分钟的话：最小集

```bash
cd source
python cli.py 002415 --json --fast --as-of 2026-10-22 > /tmp/h22.json
python -c "import json;d=json.load(open('/tmp/h22.json'));r=d['recommendation'];print(r['action'], r['target_position'], r['signal']['position_state']);print(r['action_reason'])"
```

把输出贴回来即可。**我要确认的就一件事：`action=hold`、`position_state=holding`、
文案里没有"卖出信号"。** 这三点对了，最初的事故就算真正闭环。
