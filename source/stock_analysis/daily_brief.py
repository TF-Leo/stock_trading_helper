# -*- coding: utf-8 -*-
"""盘前简报 v2（issue #149）—— 结论优先 + 每条一句人话。

用户 2026-11-18 反馈「越来越看不懂」：旧投递把模型内部证据（策略族、t 值、
n、超额收益点估计、分位、置信百分比）与结论平铺，读者要自己从 10 行里推断
「今天到底要不要动」。本模块把**最终文本**直接产出，Hermes 侧原样发送，
prompt 不再改写内容（断根：prompt 压缩导致字段丢失/措辞漂移）。

输出形态（硬性）::

    📈 盘前 11/18

    结论：今天不用动。
    把握分高/中/低，表示模型对这次判断的信心。
    - 示例银行 12.00：不买，今天没出现买点。
    - 示例白酒 1701.23：继续持有，第 22 个交易日（建议 20 日，没跌破止损就不强制卖）。

    数据：…

设计边界（与 ``advice_render`` 同一套硬约束）
----------------------------------------------
* **纯函数**：``render_daily_brief(payloads, *, date)`` 不修改入参、不读写
  文件、**不发网络请求**（只 import 标准库与同包的常量）。
* **只描述事实**：不重算 ``action`` / ``confidence`` / 价位；所有值逐值取自
  ``cli.py --json`` 的 payload。
* **动作语义来自决策表**：``buy`` / ``sell`` 是「今天要下的单」，计入结论行；
  ``hold``（持仓）与 ``hold``（空仓）是「知道就行」，不计入动作条数 —— 与
  ``decision/actions.py::ACTION_MEANING`` 一致。
* **空仓不显示卖出提示；持仓不显示买入提示**（issue #149 硬规则 3）。若
  payload 自相矛盾（空仓却 ``action=sell``、持仓却 ``action=buy``），渲染层
  按仓位侧忽略该提示并在末尾「数据」行说明，绝不并列输出矛盾句。
* **术语黑名单**：:data:`BANNED_TERMS` 之外一律人话；扫描器
  :func:`find_banned_terms` 供测试与工具自检。标的名称属于白名单（用户给的
  简称可能含「策略」等字样），扫描前先剔除。
* **数据问题只在影响本条结论时出现**，并合并为末尾至多一行；资金流口径、
  估值缺失、事件缓存过期等**不影响动作**的事实不进入投递文本。
* **零新依赖**；``advice_render.render_advice`` 不变（其他用途仍可用）。
"""
from __future__ import annotations

import datetime as _dt
import math
import re
from typing import Any, Dict, List, Optional, Sequence

from .advice_render import CONFIDENCE_HIGH_MIN, CONFIDENCE_MEDIUM_MIN

#: 顶部标题前缀（Hermes / 测试按此前缀识别简报）。
HEADER_PREFIX = "📈 盘前 "

#: 置信分级（与 ``advice_render`` 同一阈值、同一标签，不另立口径）。
CONFIDENCE_GRADE_LABELS = {"high": "高", "medium": "中", "low": "低"}

#: 「把握」的唯一定义句，出现在首条之前（issue #149 硬规则 4）。
CONFIDENCE_EXPLAIN = "把握分高/中/低，表示模型对这次判断的信心。"

#: 术语黑名单：白名单外出现即测试失败。名称不在扫描范围内（见
#: :func:`find_banned_terms`），因为用户给的简称可能本身含这些字样。
BANNED_TERMS = (
    "策略族", "策略", "CMF", "RSI", "MACD", "KDJ", "DMI", "BOLL", "ATR",
    "分位", "超额收益", "点估计", "触发参考价", "买侧期望", "期望",
    "命中率", "显著性", "置信", "阈值", "样本", "回测", "指标", "触发器",
    "参数", "统计", "概率",
)

#: 黑名单的形态规则：``n=`` / ``t=`` / 英文缩写（≥2 个大写字母）。
_BANNED_PATTERNS = (
    r"(?<![A-Za-z])n\s*=",
    r"(?<![A-Za-z])t\s*=",
    r"(?<![A-Za-z])[A-Z]{2,}(?![A-Za-z])",
)

#: 置信低于该值时，才把它说成「模型也没把握」。这是一个**展示阈值**，不是
#: 决策阈值：0.436 仍是「低」，但模型并非毫无方向，写成「没出现买点」即可；
#: 0.021 这种接近零的置信才值得用一句「没把握（把握 低，2.1%）」提醒读者。
CONFIDENCE_NO_OPINION_MAX = 0.2

#: 风控出场类型（与 ``decision.actions.EXIT_LABELS`` 同一口径；此处只读
#: ``action_reason`` 里已有的「触发{类型}」字样，不重算判定）。
_EXIT_KINDS = ("时间止损", "止盈", "止损")
_EXIT_RE = re.compile(r"触发(" + "|".join(_EXIT_KINDS) + r")")

#: 条目排序：今天要下的单在前，其余保持输入顺序（stable sort）。
_ACTION_RANK = {"buy": 0, "sell": 0, "holding": 1, "flat": 1,
                "avoid": 1, "insufficient": 1, "neutral": 1}


# ---------------------------------------------------------------------------
# 基础取值 / 格式化
# ---------------------------------------------------------------------------
def _dict(value) -> Dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _text(value) -> Optional[str]:
    if isinstance(value, str):
        text = value.strip()
        return text or None
    return None


def _num(value) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _fmt_price(value) -> Optional[str]:
    """价格固定 2 位小数（issue #149 硬规则 6）。"""
    number = _num(value)
    return f"{number:.2f}" if number is not None else None


def _fmt_pct(ratio) -> Optional[str]:
    """比例 → 百分数，**最多 1 位小数**（0.021 → ``2.1%``，0.436 → ``43.6%``）。"""
    number = _num(ratio)
    if number is None:
        return None
    text = f"{number * 100:.1f}".rstrip("0").rstrip(".")
    return f"{text or '0'}%"


def _confidence_pct(value) -> Optional[str]:
    """置信百分比向下取整到 1 位，保证显示值与分级落在同一档。"""
    number = _num(value)
    if number is None:
        return None
    floored = math.floor(number * 1000) / 10.0
    text = f"{floored:.1f}".rstrip("0").rstrip(".")
    return f"{text or '0'}%"


def _confidence_grade(value) -> Optional[str]:
    number = _num(value)
    if number is None:
        return None
    if number >= CONFIDENCE_HIGH_MIN:
        return CONFIDENCE_GRADE_LABELS["high"]
    if number >= CONFIDENCE_MEDIUM_MIN:
        return CONFIDENCE_GRADE_LABELS["medium"]
    return CONFIDENCE_GRADE_LABELS["low"]


def _confidence_clause(rec: Dict[str, Any]) -> Optional[str]:
    """``（把握 中，72%）``；置信缺失时整段消失，绝不编造。"""
    value = _num(rec.get("confidence"))
    grade = _confidence_grade(value)
    pct = _confidence_pct(value)
    if grade is None:
        return None
    if pct is None:
        return f"（把握 {grade}）"
    return f"（把握 {grade}，{pct}）"


def _display_name(payload: Dict[str, Any]) -> str:
    """名称优先级：``instrument.display_name`` → 顶层 ``display_name`` →
    ``instrument.name`` → 代码。"""
    inst = _dict(payload.get("instrument"))
    request = _dict(payload.get("request"))
    name = (_text(inst.get("display_name"))
            or _text(payload.get("display_name"))
            or _text(inst.get("name")))
    code = _text(inst.get("code")) or _text(request.get("code"))
    if name and (code is None or name != code):
        return name
    return code or "未命名标的"


def _last_close(payload: Dict[str, Any]) -> Optional[float]:
    data = _dict(payload.get("data"))
    value = _num(data.get("last_close"))
    if value is not None:
        return value
    signal = _dict(_dict(payload.get("recommendation")).get("signal"))
    return _num(signal.get("last_close"))


def _holding_days(rec: Dict[str, Any]) -> Optional[int]:
    """只读结构化字段 ``recommendation.holding_days``（issue #144），不解析文案。"""
    days = _num(rec.get("holding_days"))
    if days is None:
        return None
    value = int(days)
    return value if value >= 1 else None


def _horizon(rec: Dict[str, Any]) -> Optional[int]:
    horizon = rec.get("horizon_days")
    if isinstance(horizon, (list, tuple)) and horizon:
        value = _num(horizon[0])
        if value is not None and value >= 1:
            return int(value)
    value = _num(horizon)
    return int(value) if value is not None and value >= 1 else None


def _exit_kind(rec: Dict[str, Any]) -> Optional[str]:
    """``action=sell`` 时从 ``action_reason`` 读已有的风控类型。"""
    if _text(rec.get("action")) != "sell":
        return None
    reasons = rec.get("action_reason")
    if isinstance(reasons, (list, tuple)):
        for reason in reasons:
            match = _EXIT_RE.search(str(reason or ""))
            if match:
                return match.group(1)
    return None


def _one_word(payload: Dict[str, Any]) -> Optional[str]:
    """一字板：优先读显式字段，其次读 ``diagnostics.warnings`` 里的既有事实。"""
    inst = _dict(payload.get("instrument"))
    explicit = _text(inst.get("one_word_limit"))
    if explicit in ("up", "down"):
        return explicit
    raw = _dict(payload.get("diagnostics")).get("warnings")
    warnings = [str(item) for item in raw if isinstance(item, str)] \
        if isinstance(raw, (list, tuple)) else []
    for warning in warnings:
        if "一字涨停" in warning:
            return "up"
        if "一字跌停" in warning:
            return "down"
    return None


# ---------------------------------------------------------------------------
# 分类：今天这条到底要不要动手
# ---------------------------------------------------------------------------
def _classify(payload: Dict[str, Any]) -> str:
    """返回 ``buy/sell/holding/flat/avoid/insufficient/neutral``。

    仓位侧是硬闸门：``action=sell`` 只在持仓时算卖出；``action=buy`` 只在
    非持仓时算买入。矛盾 payload 降级为 ``neutral``（并在数据行说明），
    避免重现「暂不买 + 接近卖出触发」并列的矛盾句。
    """
    inst = _dict(payload.get("instrument"))
    rec = _dict(payload.get("recommendation"))
    status = _text(rec.get("status"))
    if inst.get("market_closed") is True or status == "market_closed":
        return "avoid"
    action = _text(rec.get("action"))
    position = _text(_dict(rec.get("signal")).get("position_state"))

    if action == "insufficient_data" or status == "insufficient_data":
        return "insufficient"
    if action == "avoid" or status == "untradeable":
        return "avoid"
    # 已确认不可成交的事实优先于买卖动作：停牌 / 一字板 / 风险警示股即使
    # payload 带了 buy/sell，也不能写成「今天下单」（与 advice_render 的
    # 可执行性闸门同口径，只认确认事实，None 不当作停牌）。
    if (inst.get("is_suspended") is True or _one_word(payload)
            or inst.get("is_st") is True):
        return "avoid"
    if action == "sell":
        if position == "flat":
            return "neutral"
        return "sell"
    if action == "buy":
        if position == "holding":
            return "holding"
        return "buy"
    if action == "hold":
        if position == "holding":
            return "holding"
        if position == "flat":
            return "flat"
        return "neutral"
    return "neutral"


# ---------------------------------------------------------------------------
# 每条的一句话人话理由
# ---------------------------------------------------------------------------
def _buy_reason(rec: Dict[str, Any]) -> str:
    text = "买入，今天出现了买点"
    position = _fmt_pct(rec.get("position_fraction"))
    if position is not None and position != "0%":
        text += f"，仓位约 {position}"
    clause = _confidence_clause(rec)
    return text + (clause or "")


def _sell_reason(rec: Dict[str, Any]) -> str:
    kind = _exit_kind(rec)
    if kind == "止损":
        stop = _fmt_price(rec.get("stop_loss"))
        return "卖出，跌破了止损价" + (f" {stop}" if stop else "")
    if kind == "止盈":
        target = _fmt_price(rec.get("take_profit"))
        return "卖出，到了止盈价" + (f" {target}" if target else "")
    if kind == "时间止损":
        return "卖出，持有时限到了，按规则离场"
    return "卖出，触发了保护性卖出条件"


def _holding_reason(rec: Dict[str, Any]) -> str:
    days = _holding_days(rec)
    horizon = _horizon(rec)
    if days is None:
        return "继续持有，没跌破止损就不用动"
    if horizon is not None and days > horizon:
        return (f"继续持有，第 {days} 个交易日"
                f"（建议 {horizon} 日，没跌破止损就不强制卖）")
    if horizon is not None:
        return f"继续持有，第 {days} 个交易日（建议 {horizon} 日）"
    return f"继续持有，第 {days} 个交易日"


def _flat_reason(rec: Dict[str, Any]) -> str:
    value = _num(rec.get("confidence"))
    if value is not None and value < CONFIDENCE_NO_OPINION_MAX:
        pct = _confidence_pct(value)
        suffix = f"，{pct}" if pct else ""
        return f"不买，模型也没把握（把握 低{suffix}）"
    return "不买，今天没出现买点"


def _avoid_reason(payload: Dict[str, Any]) -> str:
    inst = _dict(payload.get("instrument"))
    rec = _dict(payload.get("recommendation"))
    if inst.get("market_closed") is True:
        return "不参与，今天休市，不做交易"
    if inst.get("is_suspended") is True:
        return "不参与，停牌中，今天下不了单"
    one_word = _one_word(payload)
    if one_word == "up":
        return "不参与，一字涨停，今天买不进"
    if one_word == "down":
        return "不参与，一字跌停，今天卖不出"
    if inst.get("is_st") is True:
        return "不参与，风险警示股，风险太高"
    if _text(rec.get("status")) == "untradeable":
        return "不参与，确认不可交易，今天下不了单"
    return "不参与，今天没有可执行的动作"


def _insufficient_reason() -> str:
    return "不参与，数据不够，今天不给判断"


_NEUTRAL_REASON = "观望，今天没有给出买卖点"


# ---------------------------------------------------------------------------
# 数据行：只报「会影响本条结论」的问题，合并为至多一行
# ---------------------------------------------------------------------------
def _date_text(value) -> Optional[str]:
    if isinstance(value, str):
        text = value.strip()
        return text or None
    return None


def _freshness_note(payload: Dict[str, Any], name: str) -> List[str]:
    data = _dict(payload.get("data"))
    if data.get("stale") is True:
        end = _date_text(data.get("end"))
        return [f"{name}的价格是旧数据（到 {end}）" if end
                else f"{name}的价格是旧数据"]
    lag = _num(data.get("lag_days"))
    end = _date_text(data.get("end"))
    expected = _date_text(data.get("expected_date"))
    end_day, expected_day = _parse_date(end), _parse_date(expected)
    date_mismatch = bool(end_day and expected_day and end_day < expected_day)
    if (lag is not None and lag >= 1) or date_mismatch:
        if end:
            return [f"{name}的价格只更新到 {end[:10]}"]
        return [f"{name}的价格滞后 {int(lag)} 个交易日"]
    return []


def _data_notes(payload: Dict[str, Any], kind: str,
                name: str) -> List[str]:
    """该标的的数据问题；只有会改变「怎么做」的才返回。

    刻意不报：资金流口径、估值/分位缺失、事件缓存过期、ST 名单滞后、
    超额收益/显著性等信任度指标 —— 它们不改变本条动作（issue #149 硬规则 5）。
    """
    notes: List[str] = []
    price = _last_close(payload)
    if price is None and kind not in ("insufficient", "avoid"):
        notes.append(f"{name}缺价格，今天没法给价")

    if kind in ("buy", "sell", "holding", "flat"):
        notes.extend(_freshness_note(payload, name))

    if kind == "buy":
        rec = _dict(payload.get("recommendation"))
        zone = rec.get("entry_zone")
        if not (isinstance(zone, (list, tuple)) and len(zone) == 2
                and _num(zone[0]) is not None and _num(zone[1]) is not None):
            notes.append(f"{name}没有给出买入价位，只给方向")

    # 矛盾 payload：空仓却报卖出 / 持仓却报买入 —— 已按仓位侧忽略，必须说明。
    rec = _dict(payload.get("recommendation"))
    position = _text(_dict(rec.get("signal")).get("position_state"))
    action = _text(rec.get("action"))
    if action == "sell" and position == "flat":
        notes.append(f"{name}是空仓，卖出提示已忽略")
    if action == "buy" and position == "holding":
        notes.append(f"{name}已持仓，买入提示已忽略")
    return notes


# ---------------------------------------------------------------------------
# 术语扫描
# ---------------------------------------------------------------------------
def find_banned_terms(text: str, *,
                      names: Sequence[str] = ()) -> List[str]:
    """返回文本里命中的黑名单术语（去重、保持出现顺序）。

    标的名称先整体剔除：用户给的中文简称可能本身含「策略」等字样
    （例如「新华策略C」），名称属于白名单，不参与术语判定。
    """
    cleaned = text
    for name in sorted({str(item) for item in names if item},
                       key=len, reverse=True):
        cleaned = cleaned.replace(name, "")
    hits: List[str] = []

    def _push(term: str) -> None:
        if term and term not in hits:
            hits.append(term)

    for term in BANNED_TERMS:
        if term in cleaned:
            _push(term)
    for pattern in _BANNED_PATTERNS:
        for match in re.finditer(pattern, cleaned):
            _push(match.group(0).strip())
    return hits


# ---------------------------------------------------------------------------
# 日期 / 标题
# ---------------------------------------------------------------------------
def _parse_date(value) -> Optional[_dt.date]:
    if isinstance(value, _dt.datetime):
        return value.date()
    if isinstance(value, _dt.date):
        return value
    text = _date_text(value)
    if not text:
        return None
    try:
        return _dt.datetime.fromisoformat(text).date()
    except ValueError:
        pass
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%m/%d", "%m-%d"):
        try:
            parsed = _dt.datetime.strptime(text, fmt)
        except ValueError:
            continue
        if fmt in ("%m/%d", "%m-%d"):
            return parsed.replace(year=_dt.date.today().year).date()
        return parsed.date()
    return None


def _header(date) -> str:
    parsed = _parse_date(date)
    if parsed is None:
        label = _date_text(date) or ""
        return f"{HEADER_PREFIX}{label}".rstrip()
    return f"{HEADER_PREFIX}{parsed.month}/{parsed.day}"


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------
def _item_line(payload: Dict[str, Any], kind: str) -> str:
    name = _display_name(payload)
    price = _fmt_price(_last_close(payload))
    rec = _dict(payload.get("recommendation"))

    if kind == "buy":
        reason = _buy_reason(rec)
    elif kind == "sell":
        reason = _sell_reason(rec)
    elif kind == "holding":
        reason = _holding_reason(rec)
    elif kind == "flat":
        reason = _flat_reason(rec)
    elif kind == "insufficient":
        reason = _insufficient_reason()
    elif kind == "avoid":
        reason = _avoid_reason(payload)
    else:
        reason = _NEUTRAL_REASON

    head = name + (f" {price}" if price else "")
    return f"- {head}：{reason}。"


def render_daily_brief(payloads: List[Dict[str, Any]], *,
                       date) -> str:
    """把一组 ``cli.py --json`` payload 渲染成盘前投递的最终文本。

    Args:
        payloads: 每只标的一次成功响应的完整 payload（顺序即投递顺序；
            今天要下的单会被稳定前移）。
        date: 投递日期（``datetime.date`` / ``datetime.datetime`` /
            ``YYYY-MM-DD`` / ``M/D``），只用于标题。

    纯函数：不修改入参、不读文件、不触网。缺失字段对应的短语整体消失，
    输出中不会出现 ``None``；术语黑名单之外不出现统计口径。
    """
    items: List[tuple] = []
    for index, payload in enumerate(payloads or []):
        if not isinstance(payload, dict):
            continue
        kind = _classify(payload)
        items.append((_ACTION_RANK.get(kind, 1), index, payload, kind))
    items.sort(key=lambda row: (row[0], row[1]))

    action_count = sum(1 for _rank, _idx, _payload, kind in items
                       if kind in ("buy", "sell"))
    all_closed = bool(items) and all(
        _dict(payload.get("instrument")).get("market_closed") is True
        for _rank, _idx, payload, _kind in items)

    if all_closed:
        conclusion = "今天休市，不用动。"
    elif action_count == 0:
        conclusion = "今天不用动。"
    elif action_count == 1:
        conclusion = "今天有 1 件事要做。"
    else:
        conclusion = f"今天有 {action_count} 件事要做。"

    # 休市日不逐只列「不参与」，避免 10 行噪音；结论已说明今天没有事。
    shown = [] if all_closed else items

    item_lines = [_item_line(payload, kind)
                  for _rank, _idx, payload, kind in shown]
    lines: List[str] = [_header(date), "", f"结论：{conclusion}"]
    if any("（把握 " in line for line in item_lines):
        lines.append(CONFIDENCE_EXPLAIN)
    lines.extend(item_lines)

    notes: List[str] = []
    for _rank, _idx, payload, kind in shown:
        name = _display_name(payload)
        for note in _data_notes(payload, kind, name):
            if note not in notes:
                notes.append(note)

    if notes:
        lines.append("")
        lines.append("数据：" + "；".join(notes) + "。")

    return "\n".join(lines)