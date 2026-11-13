# -*- coding: utf-8 -*-
"""a-stock-data 适配层：腾讯日线 / 实时快照 / 官方交易日历。

上游固定版：a-stock-data v3.10.4，commit
``f21534946c42be3ad048ac938eecb1bad84cb8ab``（见 ``vendor/a-stock-data/VERSION``）。
本模块只做**取数**，不含任何决策逻辑；上游函数由 ``tools/astock_extract.py``
抽取到 ``_astock_generated.py``（生成物入库，可用 ``--check`` 复验）。

设计要点
--------
* **日线**：腾讯不复权 K 线（§1.2）+ 新浪比例复权因子（§1.6），默认前复权。
  *不使用*腾讯自带 ``adjust='qfq'``：上游明确说明它是「逐次减去每股分红」的
  **等差**口径，高分红股长区间收益会被扭曲。实测 600519 2020-01-02：
  腾讯 qfq=870.741，新浪比例 qfq=956.408，二者相差 85 元。
  本适配器采用比例口径后，600519 与既有新浪缓存 956.41 一致；
  ``tools/check_source_parity.py --source sina`` 实测价格最大差 0.005 元
  （4 位小数舍入），成交量最大差 99 股（腾讯按整手取整，上游 #57 已知）。
* **成交量单位**：腾讯对科创板（688/689）返回「股」，其余返回「手」；
  上游 §1.2 明确写出该差异。本适配器统一归一为**股**（非科创板 ×100），
  与现有新浪通道（``stock_zh_a_daily``）的缓存口径一致，避免同一列混用两种单位。
* **实时快照**：可选辅助，不进入主流程；仅供后续任务/人工核查使用。
* **交易日历**：深交所官方整月日历（§12）。不可用时返回 ``None``，由调用方
  （``data_loader.meta._expected_latest_trade_date``）回退到原有周末推算。
  日历结果缓存在 ``stock_analysis/data/calendar/``（gitignore），进程内再 memo，
  因此一次运行最多触网一次；``ASTOCK_CALENDAR_OFFLINE=1`` 可强制离线。
"""
from __future__ import annotations

import contextlib
import datetime as _dt
import json
import os
import sys

import pandas as pd

from . import _astock_generated as _g
from . import tencent_throttle as _throttle

__all__ = [
    "AstockError",
    "fetch_daily",
    "fetch_index_daily",
    "fetch_quote",
    "get_month_calendar",
    "is_trading_day",
    "latest_trading_day_at",
    "calendar_days_from_disk",
    "calendar_source",
    "calendar_last_error",
    "OFFLINE_ENV",
    "TencentThrottled",
]

OFFLINE_ENV = "ASTOCK_CALENDAR_OFFLINE"
# 断源演练开关（issue #3 验收 3）：置 1 时腾讯通道必失败，用于验证渠道池降级。
FORCE_FAIL_ENV = "ASTOCK_FORCE_FAIL"

# 未指定 start_date 时的拉取下限。既有的 eastmoney / sina_stock 通道在
# start_date=None 时返回**全量历史**，本适配器必须保持同一语义：1990-01-01
# 早于 A 股任一标的的上市日，作为「全量」的哨兵值。
#
# 成本提示：``tencent_kline`` 按 ``_TENCENT_SPAN_DAYS['day'] = 700`` 天/窗口
# 分页，1990→今约 37 年 ≈ 19 个日线窗口 = 19 次 HTTP 请求。因此**只有冷启动
# （本地无缓存）才应走全量**；日常过期缓存由 ``data_loader.meta.get_stock_daily``
# 先取「缓存末日 - 40 天」的近端窗口，与旧缓存重叠比对后合并（通常 1 个窗口）。
FULL_HISTORY_START = "1990-01-01"

# 进程内日历缓存：{(year, month): {"YYYY-MM-DD": bool} | None}
# None 表示该月已判定不可用，用于避免同一次运行里反复重试超时。
_CALENDAR_MEMO: dict[tuple[int, int], dict[str, bool] | None] = {}
_CALENDAR_INFO = {"source": None, "last_error": None, "last_month": None}


class AstockError(RuntimeError):
    """a-stock-data 取数失败（网络 / 格式 / 限流 / 空数据）。"""


class TencentThrottled(AstockError):
    """腾讯 K 线**整域冷却中**（跨进程状态文件未过期）—— 本次一个请求都没发。

    与普通 :class:`AstockError` 的区别对调用方很重要：这不是「这只票取不到」，
    而是「现在整个腾讯都别碰」。渠道池据此立刻切备胎，不必再付三次 HTTP 超时。
    """


# ---------------------------------------------------------------------------
# 日线
# ---------------------------------------------------------------------------
def _is_star_market(digits: str) -> bool:
    """科创板：腾讯按「股」返回成交量；其余按「手」。"""
    return digits.startswith(("688", "689"))


def _format_start(start_date) -> str:
    if start_date is None or str(start_date).strip() == "":
        return FULL_HISTORY_START
    try:
        return _g._v39_date(start_date)
    except ValueError as exc:
        raise AstockError(f"start_date 无法解析: {start_date!r}") from exc


def _normalize_adjust(adjust) -> str:
    if adjust is None:
        return "qfq"
    value = str(adjust).strip().lower()
    if value not in ("qfq", "hfq", ""):
        raise AstockError(f"adjust 只支持 'qfq' / 'hfq' / ''，收到 {adjust!r}")
    return value


_DEFAULT_HTTP_TIMEOUT = _g._ASTOCK_HTTP_TIMEOUT


@contextlib.contextmanager
def _http_timeout(timeout):
    """取数期间把生成物的 ``_ASTOCK_HTTP_TIMEOUT`` 收敛到 ``timeout``，退出还原。

    生成物没有「每次调用传超时」的入口（``_tencent_kline_call`` 直接读模块级
    常量），而本项目的取数按上游防封铁律**串行**执行，因此用「进程级常量 +
    finally 还原」实现。

    还原目标是**进程启动时的默认值**（而不是进入时的当前值）：被预算放弃的
    线程可能还握着「收紧后」的常量在跑，若还原成「当前值」会把它永久留在进程
    全局里，连累日历 / 指数 / 快照这些不走预算的调用。墙钟上限由调用方
    ``run_with_deadline`` 兜底（见 :func:`tencent_throttle.run_with_deadline`）。
    """
    _g._ASTOCK_HTTP_TIMEOUT = timeout
    try:
        yield
    finally:
        _g._ASTOCK_HTTP_TIMEOUT = _DEFAULT_HTTP_TIMEOUT


def fetch_daily(symbol: str, start_date=None, adjust="qfq", budget=None) -> pd.DataFrame:
    """拉取腾讯日线，按**比例口径**做前/后复权。

    ``start_date`` 为 ``None`` / 空字符串时返回**全量历史**（下限
    :data:`FULL_HISTORY_START` = 1990-01-01），与既有 eastmoney / sina_stock
    通道的 ``start_date=None`` 语义一致。注意全量分页成本：日线每窗口 700 天，
    1990→今约 19 次 HTTP 请求；日常增量更新请只传近端窗口。

    为什么不用腾讯自带的 ``qfq``：上游 §1.2 明确说明腾讯 qfq 是「逐次减去每股
    分红」的**等差**口径，高分红老股的长区间收益会被扭曲（实测 600519 2021-02
    与新浪比例复权差 130 元）。上游建议「取不复权 K 线，再用 §1.6 的比例因子
    复权」。本函数照此实现：腾讯 ``adjust=''`` + 新浪 ``sina_adjust_factor``。
    这样与既有新浪缓存数值同口径，回测不会被换源静默改变。

    返回 ``DataFrame``：``DatetimeIndex``（升序、无重复），列顺序固定为
    ``open/high/low/close/volume``。``volume`` 统一为**股**。
    ``adjust=''`` 时返回腾讯原始价（不做复权）。
    失败（网络/限流/空数据/格式变化/因子不可用）抛 :class:`AstockError`，
    绝不返回空壳或把未复权价冒充复权价。

    ``budget``（秒，可选）：调用方剩余的时间预算。给出时，本次 HTTP 的
    ``(connect, read)`` 会被 :func:`tencent_throttle.http_timeout_for_budget`
    收敛到预算内（issue #67 D2：避免「预算 3s、单请求 20s」错配）。这是
    **协作式**上限，墙钟仍由调用方的 ``run_with_deadline`` 兜底。
    """
    if str(os.getenv(FORCE_FAIL_ENV, "")).strip().lower() in ("1", "true", "yes", "on"):
        raise AstockError(f"{FORCE_FAIL_ENV}=1：腾讯源被强制失败（降级演练）")

    try:
        digits = _g.norm_ticker(symbol)
    except ValueError as exc:
        raise AstockError(f"股票代码无法解析: {symbol!r}") from exc

    start = _format_start(start_date)
    adjust_value = _normalize_adjust(adjust)
    needs_factor = adjust_value in ("qfq", "hfq")
    fetch_adjust = "" if needs_factor else adjust_value

    _throttle.sync_process_state()
    if _throttle.all_hosts_cooling():
        # issue #13 第 1 条：跨进程冷却文件显示三个入口都在冷却 → 一个请求都不发。
        # 抛专门的 TencentThrottled，让渠道池立刻切备胎（而不是再付三次超时）。
        raise TencentThrottled(
            f"腾讯 K 线整域冷却中（{_throttle.cooling_summary()}），本次未发起请求")

    request_timeout = (None if budget is None
                       else _throttle.http_timeout_for_budget(budget))
    timeout_scope = (contextlib.nullcontext() if request_timeout is None
                     else _http_timeout(request_timeout))

    try:
        with timeout_scope:
            frame = _g.tencent_kline(digits, period="day", adjust=fetch_adjust,
                                     start=start)
    except ValueError as exc:
        # 参数错误（区间过长 / 北交所 / 代码不存在）与来源格式错误区分开
        raise AstockError(f"腾讯K线参数错误({digits}): {exc}") from exc
    except Exception as exc:  # RuntimeError / requests 异常等
        raise AstockError(f"腾讯K线拉取失败({digits}): {exc}") from exc
    finally:
        # 生成物把失败入口写进进程内 `_tencent_host_down_until`；这里立刻镜像到
        # 磁盘，下一个进程（或同一进程的下一次运行）启动即可见，不必重复撞墙。
        _throttle.flush_process_state(reason=f"fetch_daily {digits}")

    if frame is None or frame.empty:
        raise AstockError(f"腾讯K线返回空数据({digits})")

    out = frame.copy()
    if "date" not in out.columns:
        out = out.reset_index()
        if "date" not in out.columns and "index" in out.columns:
            out = out.rename(columns={"index": "date"})
    out["date"] = pd.to_datetime(out["date"], errors="coerce")
    out = out.dropna(subset=["date"]).set_index("date").sort_index()
    out = out[~out.index.duplicated(keep="last")]

    for column in ("open", "high", "low", "close", "volume"):
        if column not in out.columns:
            raise AstockError(f"腾讯K线缺少列 {column}（实际列 {list(out.columns)}）")
        out[column] = pd.to_numeric(out[column], errors="coerce")

    if not _is_star_market(digits):
        out["volume"] = out["volume"] * 100.0

    if needs_factor:
        try:
            factors = _g.sina_adjust_factor(digits, adjust_value)
        except Exception as exc:
            raise AstockError(f"新浪复权因子拉取失败({digits}/{adjust_value}): {exc}") from exc
        try:
            adjusted = _g.apply_adjust(
                out.reset_index(), factors, kind=adjust_value,
                price_keys=("open", "high", "low", "close"))
        except Exception as exc:
            raise AstockError(f"比例复权失败({digits}/{adjust_value}): {exc}") from exc
        adjusted["date"] = pd.to_datetime(adjusted["date"], errors="coerce")
        out = adjusted.dropna(subset=["date"]).set_index("date").sort_index()
        out = out[~out.index.duplicated(keep="last")]

    out = out[["open", "high", "low", "close", "volume"]]
    out.index.name = "date"
    return out


# ---------------------------------------------------------------------------
# 指数日线（P1-3，issue #32）
# ---------------------------------------------------------------------------
INDEX_OFFLINE_ENV = "ASTOCK_INDEX_OFFLINE"

# 指数走 **newfqkline** 而不是个股用的 fqkline。2026-11-12 实测：本机对
# ``/appstock/app/fqkline/get`` 三个入口一律 501（Tencent WAF 拦截页），而
# ``/appstock/app/newfqkline/get`` 同样三个入口全部 200、返回结构一致
# （``data[symbol]['day']``，行格式 [date, open, close, high, low, volume, ...]）。
# 指数不需要复权，所以不涉及 qfq/hfq 口径差异；fqkline 仍作为第二入口兜底，
# 以防哪天 newfqkline 反过来被拦。
_INDEX_KLINE_PATHS = ("/appstock/app/newfqkline/get",
                      "/appstock/app/fqkline/get")
_INDEX_PAGE_BARS = 640        # 与上游 §1.2 单次上限一致
_INDEX_MAX_PAGES = 40         # 1990→今约 9 页；40 页是防御性上限


def _index_offline() -> bool:
    """指数取数的离线开关（测试 / 断网演练）。

    ``ASTOCK_INDEX_OFFLINE=1`` 是专用开关（与 ``ASTOCK_STATUS_OFFLINE`` 同一套路）；
    ``ASTOCK_DISABLE_NETWORK=1`` 也生效，保持与日历一致的全局面断网语义。
    """
    for key in (INDEX_OFFLINE_ENV, "ASTOCK_DISABLE_NETWORK"):
        if str(os.getenv(key, "")).strip().lower() in ("1", "true", "yes", "on"):
            return True
    return False


def _index_symbol(symbol) -> str:
    """指数代码 → ``sh000300`` 形式（腾讯要求带市场前缀）。

    显式前缀/后缀原样透传；**裸 6 位码**按指数号段消歧：``000xxx`` 是沪市指数
    （000001 上证 / 000300 沪深300 / 000985 中证全指 / 000905 中证500 …），
    ``399xxx`` 是深市指数。上游 ``get_prefix`` 只认 ``SH_INDEX`` 里那几个固定
    名单，``000985`` 这类不在名单里的裸码会被判成 ``sz``，所以这里补一条指数
    专属规则；个股代码请勿走本函数。
    """
    raw = str(symbol).strip().lower()
    try:
        digits = _g.norm_ticker(symbol)
    except ValueError as exc:
        raise AstockError(f"指数代码无法解析: {symbol!r}") from exc
    has_explicit = (raw[:2] in ("sh", "sz", "bj")
                    or raw.endswith((".sh", ".sz", ".bj", ".xshg", ".xshe")))
    if has_explicit:
        return _g.get_prefix(symbol) + digits
    if digits.startswith("399"):
        return "sz" + digits
    if digits.startswith("000"):
        return "sh" + digits
    return _g.get_prefix(symbol) + digits


def _index_kline_call(param: str):
    """按 ``_INDEX_KLINE_PATHS`` 顺序取一段指数 K 线；返回 (data, host)。"""
    errors = []
    for path in _INDEX_KLINE_PATHS:
        try:
            data, host = _g._tencent_kline_call(path, param)
        except ValueError as exc:
            # 上游对「区间过长 / 代码不存在」抛 ValueError，属于参数错，换路径也一样
            raise AstockError(f"腾讯指数K线参数错误({param}): {exc}") from exc
        except Exception as exc:
            errors.append(f"{path}: {exc}")
            continue
        return data, host
    raise AstockError("腾讯指数K线入口均不可用: " + "; ".join(errors))


def fetch_index_daily(symbol="sh000300", start_date=None) -> pd.DataFrame:
    """拉取指数**不复权**日线（腾讯 §1.2，默认沪深 300）。

    ``start_date`` 为 None / 空时返回全量历史（下限 ``FULL_HISTORY_START``）。
    腾讯 ``newfqkline`` 只认 ``count`` + ``end``（``start`` 会被忽略），因此这里
    从最新一根开始**向前翻页**：每次取 ``end`` 之前的 640 根，直到覆盖
    ``start_date`` 或源端返回不足一页。日常 2020 起的区间通常 1–2 次 HTTP。

    返回 ``DataFrame``：``DatetimeIndex``（升序、无重复），列
    ``open/high/low/close/volume``。

    成交量**原样透传**（腾讯指数口径，不做个股那条 ×100 归一）；regime 判定只
    消费 ``close``，成交量仅作可选诊断，不能当个股成交量口径使用。

    失败（网络 / WAF / 空数据 / 格式变化 / 离线）抛 :class:`AstockError`。
    """
    if str(os.getenv(FORCE_FAIL_ENV, "")).strip().lower() in ("1", "true", "yes", "on"):
        raise AstockError(f"{FORCE_FAIL_ENV}=1：腾讯源被强制失败（降级演练）")
    if _index_offline():
        raise AstockError(f"{INDEX_OFFLINE_ENV}/ASTOCK_DISABLE_NETWORK 生效：指数取数已禁用")

    code = _index_symbol(symbol)
    start = _format_start(start_date)

    _throttle.sync_process_state()
    if _throttle.all_hosts_cooling():
        raise TencentThrottled(
            f"腾讯 K 线整域冷却中（{_throttle.cooling_summary()}），本次未发起请求")

    try:
        return _fetch_index_rows(code, start)
    finally:
        _throttle.flush_process_state(reason=f"fetch_index_daily {code}")


def _fetch_index_rows(code: str, start: str) -> pd.DataFrame:
    """``fetch_index_daily`` 的取数主体（分页循环）；冷却镜像由调用方负责。"""
    rows_by_date: dict[str, dict] = {}
    end = ""
    for _ in range(_INDEX_MAX_PAGES):
        data, _host = _index_kline_call(f"{code},day,,{end},{_INDEX_PAGE_BARS},")
        node = data.get(code)
        if not isinstance(node, dict):
            raise AstockError(f"腾讯指数K线 {code} 的返回里没有该代码节点，格式可能已变")
        items = node.get("day")
        if not isinstance(items, list):
            raise AstockError(f"腾讯指数K线 {code} 的返回里没有 day 列表，格式可能已变")
        if not items:
            break
        try:
            for item in items:
                day = _g._v39_src_date(item[0])
                if end and day > end:
                    raise AstockError(
                        f"腾讯指数K线 {code} 请求 end={end} 却返回了 {day}，结果不可信")
                rows_by_date[day] = {
                    "date": day,
                    "open": _g._v39_req_num(item[1], "open"),
                    "high": _g._v39_req_num(item[3], "high"),
                    "low": _g._v39_req_num(item[4], "low"),
                    "close": _g._v39_req_num(item[2], "close"),
                    "volume": _g._v39_req_num(item[5], "volume"),
                }
        except AstockError:
            raise
        except (ValueError, TypeError, IndexError, KeyError) as exc:
            raise AstockError(f"腾讯指数K线 {code} 行格式改变: {exc}") from exc

        earliest = min(rows_by_date)
        # 已覆盖 start，或源端这一页本来就不满（没有更早的数据了）→ 停止
        if earliest <= start or len(items) < _INDEX_PAGE_BARS:
            break
        end = (_dt.date.fromisoformat(earliest) - _dt.timedelta(days=1)).isoformat()
    else:
        raise AstockError(
            f"腾讯指数K线 {code} 分页超过 {_INDEX_MAX_PAGES} 页仍未覆盖 {start}，已放弃")

    if not rows_by_date:
        raise AstockError(f"腾讯指数K线 {code} 在 {start} 之后 0 根（未上市/区间内无交易日/代码有误）")

    rows = [rows_by_date[k] for k in sorted(rows_by_date) if k >= start]
    if not rows:
        raise AstockError(f"腾讯指数K线 {code} 在 {start} 之后 0 根（未上市/区间内无交易日/代码有误）")

    frame = pd.DataFrame(rows)
    frame["date"] = pd.to_datetime(frame["date"], errors="coerce")
    frame = frame.dropna(subset=["date"]).set_index("date").sort_index()
    frame = frame[~frame.index.duplicated(keep="last")]
    frame.index.name = "date"
    return frame[["open", "high", "low", "close", "volume"]]


# ---------------------------------------------------------------------------
# 实时快照（可选辅助）
# ---------------------------------------------------------------------------
def fetch_quote(symbol: str) -> dict:
    """腾讯实时行情快照（现价/PE/PB/涨跌停价等，§1.1）。

    仅作可选辅助，不接入主流程。返回的 dict 由上游原样给出；``is_stale``
    为 True 时表示报价定格（停牌 / 北交所老废码），不得用于估值。
    """
    try:
        digits = _g.norm_ticker(symbol)
    except ValueError as exc:
        raise AstockError(f"股票代码无法解析: {symbol!r}") from exc
    try:
        quotes = _g.tencent_quote([digits])
    except Exception as exc:
        raise AstockError(f"腾讯实时行情拉取失败({digits}): {exc}") from exc
    quote = quotes.get(digits) if isinstance(quotes, dict) else None
    if not quote:
        raise AstockError(f"腾讯实时行情未返回 {digits}")
    return quote


# ---------------------------------------------------------------------------
# 官方交易日历（§12，深交所）
# ---------------------------------------------------------------------------
def _calendar_dir() -> str:
    # <repo>/source/stock_analysis/data/calendar —— data/ 已在 .gitignore 中
    return os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "data", "calendar")


def _calendar_file(year: int, month: int) -> str:
    return os.path.join(_calendar_dir(), f"szse_{year:04d}-{month:02d}.json")


def _calendar_offline() -> bool:
    flag = os.getenv(OFFLINE_ENV, "") or os.getenv("ASTOCK_DISABLE_NETWORK", "")
    return str(flag).strip().lower() in ("1", "true", "yes", "on")


def _read_disk_calendar(year: int, month: int):
    path = _calendar_file(year, month)
    try:
        with open(path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
        days = payload.get("days")
        if isinstance(days, dict) and days:
            return {str(k): bool(v) for k, v in days.items()}
    except Exception:
        return None
    return None


def calendar_days_from_disk(year: int, month: int):
    """**只读**磁盘交易日历；不触网，缺失时返回 ``None``。

    给「绝不触网」的调用方（如 ``data_loader.flow`` 的缓存陈旧判定）使用：
    ``get_month_calendar`` 在磁盘缺失时会去请求深交所，不适用于盘前/离线路径。
    """
    return _read_disk_calendar(int(year), int(month))


def _write_disk_calendar(year: int, month: int, days: dict[str, bool]) -> None:
    try:
        os.makedirs(_calendar_dir(), exist_ok=True)
        path = _calendar_file(year, month)
        tmp = path + ".tmp"
        payload = {
            "source": "astock_calendar",
            "provider": "szse",
            "year": year,
            "month": month,
            "fetched_at": _dt.datetime.now().isoformat(),
            "days": days,
        }
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=1, sort_keys=True)
        os.replace(tmp, path)
    except Exception as exc:  # 缓存写失败不影响取数
        print(f"[WARN] 写交易日历缓存失败：{exc}", file=sys.stderr)


def get_month_calendar(year: int, month: int):
    """返回 ``{"YYYY-MM-DD": is_open}``；不可用时返回 ``None``（调用方回退推算）。

    优先级：进程内 memo → 磁盘缓存 → 深交所官方接口。失败不抛异常，
    只记录 ``calendar_last_error()``，因为日历是**辅助信号**而非主数据。
    """
    key = (int(year), int(month))
    if key in _CALENDAR_MEMO:
        return _CALENDAR_MEMO[key]

    cached = _read_disk_calendar(*key)
    if cached is not None:
        _CALENDAR_MEMO[key] = cached
        _CALENDAR_INFO.update(source="astock_calendar", last_error=None,
                              last_month=f"{key[0]:04d}-{key[1]:02d}")
        return cached

    if _calendar_offline():
        _CALENDAR_INFO.update(source=None, last_error="offline env set",
                              last_month=f"{key[0]:04d}-{key[1]:02d}")
        return None

    try:
        frame = _g.trading_calendar(key[0], key[1])
    except Exception as exc:
        _CALENDAR_MEMO[key] = None
        _CALENDAR_INFO.update(source=None, last_error=f"{type(exc).__name__}: {exc}",
                              last_month=f"{key[0]:04d}-{key[1]:02d}")
        return None

    try:
        days = {str(row.date): bool(row.is_open)
                for row in frame.itertuples(index=False)}
    except Exception as exc:
        _CALENDAR_MEMO[key] = None
        _CALENDAR_INFO.update(source=None, last_error=f"format: {exc}",
                              last_month=f"{key[0]:04d}-{key[1]:02d}")
        return None
    if not days:
        _CALENDAR_MEMO[key] = None
        _CALENDAR_INFO.update(source=None, last_error="empty calendar",
                              last_month=f"{key[0]:04d}-{key[1]:02d}")
        return None

    _CALENDAR_MEMO[key] = days
    _write_disk_calendar(key[0], key[1], days)
    _CALENDAR_INFO.update(source="astock_calendar", last_error=None,
                          last_month=f"{key[0]:04d}-{key[1]:02d}")
    return days


def is_trading_day(day) -> bool | None:
    """判断某日是否交易日；日历不可用返回 ``None``（不是 False）。"""
    if isinstance(day, _dt.datetime):
        day = day.date()
    if not isinstance(day, _dt.date):
        day = _g._v39_date(day)
        day = _dt.date.fromisoformat(day)
    days = get_month_calendar(day.year, day.month)
    if days is None:
        return None
    return days.get(day.isoformat())


def latest_trading_day_at(now=None, lookback_days: int = 40):
    """返回 ``now`` 时刻之前（含，按 15:00 收盘规则）最近的官方交易日。

    与 ``_expected_latest_trade_date`` 旧语义一致：15:00 前视为当日尚未收盘，
    从昨天开始找；随后按**官方日历**（而非仅周末）向前回退。
    日历不可用时返回 ``None``，由调用方回退到周末推算。
    """
    now = now or _dt.datetime.now()
    candidate = now.date()
    if now.hour < 15:
        candidate = candidate - _dt.timedelta(days=1)
    for _ in range(max(1, int(lookback_days))):
        days = get_month_calendar(candidate.year, candidate.month)
        if days is None:
            _CALENDAR_INFO["source"] = None
            return None
        if days.get(candidate.isoformat()):
            _CALENDAR_INFO["source"] = "astock_calendar"
            return candidate
        candidate = candidate - _dt.timedelta(days=1)
    return None


def calendar_source():
    """最近一次日历判定的来源：``astock_calendar`` / ``None``（不可用）。"""
    return _CALENDAR_INFO.get("source")


def calendar_last_error():
    """最近一次日历失败原因（供 stderr / 运维排查；成功时为 ``None``）。"""
    return _CALENDAR_INFO.get("last_error")
