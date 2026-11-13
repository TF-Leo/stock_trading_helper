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

### 0.1 2026-11-13 挂死事故与修复（阶段 2 首次运行）

**现象**：14:52:25 起在 `575/5471` 后无任何输出，直到 19:50 被人工 kill；
进程存活 5h43m 但只消耗 31s CPU，`ss` 显示到 baostock 的 socket 处于
`ESTAB` 且 0 字节收发。595 个 CSV 完整，无 `sh.600711` 残片。

**根因**（非「没有超时」，而是超时后的恢复路径没有超时）：

1. 14:52:25 `sh.600711` 查询超过 90s，`SIGALRM` 确实触发了
   （console 日志有 `单请求超过 90s`）；
2. 但 baostock `util/socketutil.py::send_msg` 用 `except Exception` **吞掉了**
   我们抛出的 `RequestTimeout`，改报 `error_code=10002007`；
3. 重试分支随后调用**未加超时**的 `bs.logout()`；该函数在坏 socket 上
   `while True: recv(8192)`，对端无数据 → 永久阻塞；
4. 主循环、manifest 落盘、超时全部停摆 —— 静默挂死。

**修复**（`tools/build_universe.py`）：

| 机制 | 实现 |
|---|---|
| 逐原语硬超时 | 查询 90s、`logout` 30s、`login` 45s，分别用 `signal.setitimer(ITIMER_REAL)` 包裹 |
| socket 兜底 | `socket.setdefaulttimeout(90)`；每次 login 后对生产 socket 再 `settimeout` |
| 坏会话处理 | 任何失败先 `_close_bs_socket()` **强制关 socket**，再重新 `login`；不信任 baostock 的优雅 logout |
| 逐票重试 | 超时/校验失败 → logout → 重新登录 → 重试，最多 3 次；仍失败记录并继续 |
| 重登失败 | 抛 `SessionError`，保存断点后退出码 **3**，外层直接重跑续 |
| 进度看门狗 | 主循环记录 `last_progress_ts`；`--watchdog-seconds`（默认 600s）内无票完成 → 打印 `[WATCHDOG]`、保存 manifest、退出码 **4** |
| 孤儿 CSV 接管 | manifest 每 25 票落盘，挂死时磁盘会比 manifest 多出已完成票；`fetch` 对「CSV 存在但无 entry」的票用同一套 `validate_series` 复核后接管，不重抓 |
| 续跑复核 | `validate --existing-only` 只校验磁盘上已有 CSV，不把尚未抓取的票记为错误 |

单票最坏耗时上界 ≈ 3 × (90s 查询 + 30s logout + 45s login + 退避 2/4/8s)
≈ 9.5 分钟 < 看门狗 10 分钟；加上 socket 默认超时，不存在无界阻塞点。

**回归证据**：

```text
$ .venv/bin/python -I tools/build_universe.py selftest
... 新增 deadline_fires / watchdog_fires / watchdog_quiet_and_disable /
    ticket_timeout_relogin_retry（桩 bs：查询永挂 → 0.2s 超时 → 每轮 logout+login）
selftest：23/23 passed
EXIT=0
```

**续跑前复核 595 个既有 CSV**（含 17 个 manifest 未记录的孤儿）：

```text
$ .venv/bin/python -I tools/build_universe.py validate --existing-only
校验票数 595/595（选中 5471）；窗口内全池 5471，未选中 0
磁盘有 CSV 但 manifest 无记录：17 只（sh.600692 … sh.600710）
行数合计 1,217,469；每票行数 min=129 p10=1834 median=2149 p90=2149 max=2149
日期覆盖 2018-01-02 .. 2026-11-12
退市票 67 只，exit_reason={'merger': 8, 'price': 59}
错误 0，警告 0
EXIT=0
```

**小样本行为验证**（真实联网）：

```text
$ .venv/bin/python -u tools/build_universe.py fetch --limit 5
   进度 5/5（跳过已完成 5，新取 0，失败 0）
==== fetch 结束：选中 5，跳过 5，接管 0，成功 0，失败 0，请求 0 ====
EXIT=0

$ .venv/bin/python -u tools/build_universe.py fetch --codes sh.600710,sh.600711
   进度 2/2（跳过 0，接管 1，成功 1，失败 0，请求 1）
==== fetch 结束：选中 2，跳过 0，接管 1，成功 1，失败 0 ====
EXIT=0
```

`sh.600710` 走「孤儿接管」；`sh.600711`（原挂死票）真实联网重新抓取成功，
耗时约 1s —— 该票本身的数据请求没有问题，挂死是故障瞬间的 socket 状态所致。

### 0.2 阶段 2 续跑起点（2026-11-13 19:58）

- 磁盘已完成：**596** 票（595 既有 + `sh.600711`），manifest 580 条 ok（17 孤儿 + 1 接管）
- 全池选中 5,471 → **剩余约 4,875 票**
- 按既有均值 4.4s/票估算，**约 6.0 小时**（串行，无并发）
- 命令：`nohup .venv/bin/python -u tools/build_universe.py fetch >> evidence/universe/stage2_console.log 2>&1`

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

## 2. 阶段 2：全量串行建池（完成）

> 首次运行于 2026-11-13 14:52 挂死（见 §0.1）；修复后于 19:58 从
> 「已完成 596 票 / 剩余约 4,875 票」续跑（§0.2），2026-11-14 02:47 完成。

### 2.1 命令与原始输出

```text
$ setsid nohup .venv/bin/python -u tools/build_universe.py fetch \
    >> evidence/universe/stage2_console.log 2>&1
[2026-11-14 02:47:03]   进度 5471/5471（跳过 580，接管 16，成功 4875，失败 0，请求 4877）
[2026-11-14 02:47:03] ==== fetch 结束：选中 5471，跳过 580，接管 16，成功 4875，失败 0，请求 4877，墙钟 408.4 分钟 ====
logout success!
EXIT=0
```

- 选中 **5,471** 票；断点跳过 **580**（阶段 1 的 50 + 首次运行已落盘）；
  孤儿 CSV 接管 **16**；新取 **4,875**；**失败 0**。
- 总请求 **4,877**；`request_seconds_mean=4.88s`；墙钟 **24,504.8s（6.82h）**。
- 全池总数与 R8 一致（5,471）。最终主表快照显示窗口内在市 5,224 + 退市 247，
  与 R8/阶段 1 的 5,225 + 246 相差 1 只（当日主表状态更新，总数不变）。

### 2.2 数据规模与行数分布

```text
rows_total       9,690,054
bytes_total        893,017,817   (≈852 MiB；du 866M)
每票行数           min=3 / p10=951 / median=2,149 / p90=2,149 / max=2,149
日期覆盖           2018-01-02 .. 2026-11-12（全池去重后 2,149 个交易日）
```

按年去重交易日数：2018:243、2019:244、2020:243、2021:243、2022:242、
2023:242、2024:242、2025:243、2026:207（截至 11-12）。

行数最小的票均为 2026 年新上市（如 `sh.688827` 3 行、`sz.001381` 8 行），
不是缺失；最大 2,149 = 窗口内完整交易日数。

### 2.3 全量离线校验

```text
$ .venv/bin/python tools/build_universe.py validate
校验票数 5471/5471（选中 5471）；窗口内全池 5471，未选中 0
行数合计 9,690,054；每票行数 min=3 p10=951 median=2149 p90=2149 max=2149
日期覆盖 2018-01-02 .. 2026-11-12
退市票 247 只，exit_reason={'merger': 24, 'price': 223}
错误 0，警告 0
EXIT=0
```

校验项：CSV 存在、行数与 manifest 一致、首末日期一致、日期唯一且升序、
末根日期缺口、退市退出价/原因与 manifest 复算一致。**0 error / 0 warning**。

### 2.4 抽样 10 票与 baostock 直查对账

```text
$ .venv/bin/python tools/build_universe.py validate --reconcile 10
sh.600000 OK rows=2149 2018-01-02..2026-11-12
sh.600661 OK rows=2149 2018-01-02..2026-11-12
sh.603036 OK rows=2149 2018-01-02..2026-11-12
sh.603927 OK rows=1738 2019-09-09..2026-11-12
sh.688499 OK rows=1301 2021-07-01..2026-11-12
sz.000795 OK rows=2149 2018-01-02..2026-11-12
sz.002280 OK rows=1609 2018-01-02..2024-08-16   ← 退市票
sz.002839 OK rows=2149 2018-01-02..2026-11-12
sz.300360 OK rows=2149 2018-01-02..2026-11-12
sz.300923 OK rows=1425 2020-12-24..2026-11-12
抽样对账 10 票：一致 10，不一致 0
EXIT=0
```

对账口径：同一区间/同一 `adjustflag=2` 串行重查，逐行比对 `date` 与 `close`。

### 2.5 退市票退出记录汇总

| 项 | 值 |
|---|---|
| 窗口内退市票 | 247 |
| `exit_reason=price` | 223 |
| `exit_reason=merger` | 24（全部为 `flat_last30_suspect_merger`，**待人工复核**） |
| `exit_reason=unknown` | 0（所有退市票都有至少一个 `tradestatus==1` 行） |
| 冻结期（最后可交易日 → outDate） | min 1 天 / median 7 天 / max 407 天 |
| 按 outDate 年份 | 2018:6、2019:11、2020:16、2021:20、2022:43、2023:45、2024:52、2025:30、2026:24 |

24 只换股/并购候选（末段 30 个可交易日跌幅 < 10%，R8 §6 半自动判据）：

```text
sh.600068 sh.600213 sh.600270 sh.600317 sh.600565 sh.600652
sh.600680 sh.600705 sh.600723 sh.600837 sh.601028 sh.601059
sh.601198 sh.601989 sh.603056 sz.000016 sz.000022 sz.000043
sz.000418 sz.000666 sz.000780 sz.002013 sz.300114 sz.300362
```

其中如 `sh.600068`（葛洲坝）、`sh.600270`（外运发展）、`sh.600317`（营口港）
确为换股/吸收合并；按 R8 规则 2，这些票的价格序列不能直接当退出价值，
**预注册前必须逐只填例外表**。这不是数据缺失，是口径待办。

### 2.6 生产测试

```text
$ .venv/bin/python -m pytest source/tests -q
618 passed in 31.55s
EXIT=0
```

---

## 3. 失败票与影响

- **失败票：0。** 5,471 票全部 `status=ok`，`manifest.failures=[]`。
- 16 只孤儿 CSV 在续跑时经 `validate_series` 复核后接管，未重复请求；
  1 只（`sh.600711`）在挂死点之后重新联网抓取成功。
- 因此**不存在「失败票影响后续研究」的问题**；全池可直接用于
  R6/R7 的横截面复算。
- 残余口径风险（非数据缺失）：①24 只换股/并购候选的退出价待人工例外表；
  ②qfq 为 2026-11-13 快照，后续除权会使整段前复权价格整体平移；
  ③不含北交所（355 只）与 B 股，研究结论须声明范围。

---

## 4. 复现

```bash
# 依赖（本机 python3 -m venv 缺 ensurepip，用 uv）
uv venv --python 3.14 .venv
uv pip install --python .venv/bin/python -r source/requirements.txt -r requirements-research.txt

.venv/bin/python tools/build_universe.py selftest
.venv/bin/python tools/build_universe.py fetch --limit 50      # 阶段 1
.venv/bin/python tools/build_universe.py validate --existing-only   # 续跑前复核已落盘
.venv/bin/python tools/build_universe.py fetch                 # 阶段 2（断点续跑）
.venv/bin/python tools/build_universe.py validate --reconcile 10
```
