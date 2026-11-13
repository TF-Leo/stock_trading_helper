# R9 无偏日线池构建日志（issue #83）

> 研究基础设施任务。只新增 `tools/build_universe.py` 与本日志；
> 未改 `source/`、生产 CLI、`actions.py`、`requirements.txt`。
>
> - 基线 commit：`01e77eb`
> - 分支：`task/universe-build`
> - 数据源：baostock 0.9.4（`requirements-research.txt`，无新依赖）
> - 窗口：`2018-01-01` ～ `2026-11-12`（issue #83 验收口径）
> - 复权：`adjustflag=2`（前复权；R8 §2.3 实测退市票同样可用）
> - 落盘：`evidence/universe/`（`evidence/` 已 gitignore，**数据集不入库**）
> - 执行环境：Linux，Python 3.14.7（uv venv），pandas 3.0.1、numpy 2.5.4

---

## 0. 硬约束落实

| 约束 | 实现 |
|---|---|
| 禁止并发 | 全流程单线程串行；入口 `_assert_single_thread()` 断言 `threading.active_count()==1` |
| 逐请求校验 | `error_code=="0"`、非空、日期唯一/升序/不越界、行数 ≤ 区间工作日+2、**正好 2,000 行判截断**、末根日期缺口 >60 工作日判失败（>5 记 warning） |
| 失败重试 | 每票 3 次，指数退避 2/4/8s；每次失败后 `logout` + `login` |
| 断点续跑 | `manifest.json` 中 `status=="ok"` 且 CSV 字节数一致的票直接跳过 |
| 单会话 + 掉线重登 | 全程一个 `bs.login()`；任何异常/`error_code!=0` 先重登再重试 |
| 退市退出口径 | 最后 `tradestatus==1` 收盘；`exit_reason` ∈ `price` / `merger` / `unknown`；另记 `outDate`、最后一行、冻结期 |
| 不引入新依赖 | 仅 stdlib + pandas + baostock（延迟 import，非 fetch/validate 路径不加载） |

单请求另有 90s 进程内超时（`SIGALRM`；baostock 不暴露 socket timeout，R8 §2.4）。

---

## 1. 阶段 1：`--limit 50`（含 5 只退市票）

### 1.1 命令与原始输出

```text
$ .venv/bin/python tools/build_universe.py selftest
...
selftest：19/19 passed
EXIT=0
```

```text
$ .venv/bin/python tools/build_universe.py fetch --limit 50
[2026-11-13 14:01:06] ==== fetch 开始 start=2018-01-01 end=2026-11-12 limit=50 adjust=2 ====
login success!
[2026-11-13 14:01:31] 主表 type=1 共 5566 只；窗口内曾上市 5471 只；本次选中 50 只（limit）
[2026-11-13 14:03:26]   进度 25/50（跳过 0，成功 25，失败 0，请求 25）
[2026-11-13 14:04:53]   进度 50/50（跳过 0，成功 50，失败 0，请求 50）
[2026-11-13 14:04:53] ==== fetch 结束：选中 50，跳过 0，成功 50，失败 0，请求 50，墙钟 3.8 分钟 ====
logout success!
EXIT=0
```

```text
$ .venv/bin/python tools/build_universe.py validate --reconcile 10
...
校验票数 50/50；窗口内全池 5471，未选中 5421
行数合计 101,599；每票行数 min=720 p10=1350 median=2149 p90=2149 max=2149
日期覆盖 2018-01-02 .. 2026-11-12
退市票 5 只，exit_reason={'price': 5}
抽样对账 10 票：一致 10，不一致 0
错误 0，警告 0
EXIT=0
```

抽样对账的 10 票（串行重查，逐值比对 `date` 与 `close`）：

```text
sh.600000 OK rows=2149 2018-01-02..2026-11-12
sh.600009 OK rows=2149 2018-01-02..2026-11-12
sh.600016 OK rows=2149 2018-01-02..2026-11-12
sh.600021 OK rows=2149 2018-01-02..2026-11-12
sh.600027 OK rows=2149 2018-01-02..2026-11-12
sh.600032 OK rows=1327 2021-05-25..2026-11-12   ← 2021 年上市，起点正确
sh.600038 OK rows=2149 2018-01-02..2026-11-12
sh.600052 OK rows=2149 2018-01-02..2026-11-12
sh.600057 OK rows=2149 2018-01-02..2026-11-12
sz.002450 OK rows=827 2018-01-02..2021-05-31    ← 退市票，末根 = outDate
```

### 1.2 池口径核对（与 R8 一致）

```text
type=1 总数 5,566；窗口内曾上市 5,471（在市 5,225 + 退市 246）
```

与 `docs/UNIVERSE_FEASIBILITY.md` §4.1 的 5,471 / 246 完全一致。

### 1.3 退市票退出记录

| 代码 | 名称 | outDate | 行数 | 最后一行 | 最后 `tradestatus==1` | 退出价 | 末段30日 | 冻结期 | exit_reason |
|---|---|---|---:|---|---|---:|---:|---:|---|
| `sz.002450` | 康得退 | 2021-05-31 | 827 | 2021-05-31 | 2021-05-28 | 0.20 | −93.7% | 3 天 | `price` |
| `sz.000939` | 凯迪退 | 2020-12-17 | 720 | 2020-12-17 | 2020-12-16 | 0.15 | −84.2% | 1 天 | `price` |
| `sh.600077` | *ST宋都 | 2023-07-25 | 1,350 | 2023-07-25 | 2023-06-13 | 0.41 | −73.4% | 42 天 | `price` |
| `sh.600070` | *ST富润 | 2025-04-30 | 1,777 | 2025-04-30 | 2025-04-10 | 0.42 | −65.6% | 20 天 | `price` |
| `sz.301139` | 元道退 | 2026-10-28 | 1,042 | 2026-10-27 | 2026-10-27 | 0.73 | −80.0% | 1 天 | `price` |

- 前 4 只的最后一根落在 `outDate` 当日（`tradestatus=0`，不可成交），
  `sz.301139` 止于 `outDate` 前一日 —— 与 R8 §2.2 的两种形态一致。
- 5 只末段 30 日跌幅均远大于 10%，按 R8 规则归为 `price`（非换股/并购）。
- `merger` 分支由 `selftest` 的合成数据覆盖（人工例外表 + 平收疑似换股），
  本窗口内 5 只真实退市票不涉及。
- 冻结期 1～42 天（R8 实测区间 1～49 天），回测须按「最后可交易日结算 +
  冻结期不调仓」处理。

### 1.4 生产测试

```text
$ .venv/bin/python -m pytest source/tests -q
618 passed in 38.88s
EXIT=0
```

---

## 2. 阶段 2：全量串行建池

（运行中；完成后在此填写成功/失败票数、墙钟、行数分布、日期覆盖与失败清单。）

---

## 3. 失败票与影响

（待阶段 2 完成后填写。）

---

## 4. 复现

```bash
# 依赖（本机 python3 -m venv 缺 ensurepip，用 uv）
uv venv --python 3.14 .venv
uv pip install --python .venv/bin/python -r source/requirements.txt -r requirements-research.txt

.venv/bin/python tools/build_universe.py selftest
.venv/bin/python tools/build_universe.py fetch --limit 50      # 阶段 1
.venv/bin/python tools/build_universe.py fetch                 # 阶段 2（断点续跑）
.venv/bin/python tools/build_universe.py validate --reconcile 10
```
