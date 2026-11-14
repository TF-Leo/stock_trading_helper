# -*- coding: utf-8 -*-
"""建议渲染层 v1 —— 把 CLI JSON 里已有、但从未到达用户眼前的数据送进每日建议。

对应 ``docs/ASTOCK_GAP_INVENTORY.md`` 的 T1–T5（issue #1 的 R11 Top 5）：

========  ==========================================================
T1        建议仓位 / 持有期 / 复审日（``recommendation.*``）
T2        可执行性闸门 + 数据新鲜度（``instrument.*`` / ``diagnostics.warnings``）
T3        个股 regime 与指数 regime 的口径冲突（``market_regime.index``）
T4        估值分位一句（``evidence.valuation``）
T5        资金流方向 + 口径标签（``evidence.flow``）
========  ==========================================================

设计边界（硬约束）
------------------
* **纯函数**：``render_advice(payload)`` 不修改入参、不读写文件、**不发起任何
  网络请求**（本模块只 import 标准库）。
* **只描述事实**：不重算 ``action`` / ``confidence`` / 价位，不把建议写成下单
  指令；仓位一律写作「建议仓位」，并保留免责行。
* **None 整段消失**：任何可选字段缺失时，对应句子/段落整体不出现，输出中
  不允许出现字符串 ``None``。
* **零新依赖**。

字段口径直接沿用 CLI 当前 JSON（``cli.py`` 的成功/错误响应同构）：

* ``position_fraction`` 是「策略建议权重」（小数，0.15 = 15%），不是用户应买
  金额；文案必须保留「建议仓位」四个字。
* ``horizon_days`` 当前是 ``[20, 20]``（低/高），渲染取第一个正值。
* ``instrument.limit_up`` / ``limit_down`` 是**价格**，不是布尔。一字板优先
  读取 ``instrument.one_word_limit``（若上游未来补上），否则从
  ``diagnostics.warnings`` 里识别 CLI 已写入的「一字涨停/一字跌停」事实。
* ``instrument.is_suspended`` 为 ``None`` 表示**无法判断**（盘前集合竞价前
  的正常现象），绝不能当成「确认停牌」；只有 ``True`` 才进不可执行闸门。
* 名称优先取 ``instrument.display_name`` / 顶层 ``display_name``，其次
  ``instrument.name``。场外基金的 ``name`` 常等于代码，此时只打印一次代码。
* 新鲜度是**任一**非当日信号：``instrument.quote_stale``、``data.stale``、
  ``data.lag_days >= 1``、``data.end < data.expected_date``。命中任意一项就
  改用「收盘为 {date}」并给出滞后提示，不再写「现价」。
* ``evidence.flow`` 的两种口径绝不混用：``eastmoney_main_net`` 用
  ``main_net_*``（元），``sina_total_net`` 用 ``net_*``（元）；口径缺失或两族
  同时有值时视为「混源」，只输出方向、不输出净额。
* ``evidence.valuation`` 的 ``percentile_samples`` 小于 3 年窗口（756）时，
  文案标注「样本 N 日」。
* ``compact=True`` 只压缩 ``diagnostics.warnings``：去掉 URL、异常类名/堆栈、
  内部代号，保留首句与关键事实；默认输出逐字节不变。
"""
from __future__ import annotations

import math
import re
from typing import Any, Dict, List, Optional

__all__ = [
    "render_advice",
    "CALIBER_LABELS",
    "PERCENTILE_WINDOW_DAYS",
    "DEFAULT_DISCLAIMER",
]

#: 与 ``data_loader.valuation.PERCENTILE_WINDOW_DAYS`` 保持同一口径。
#: 这里写常量而不是 import，是为了让渲染层不依赖数据层（零副作用、可单测）。
PERCENTILE_WINDOW_DAYS = 756

#: 资金流口径 → 用户可读标签。两种口径必须可区分（T5）。
CALIBER_LABELS = {
    "eastmoney_main_net": "东财主力口径",
    "sina_total_net": "新浪总净额口径",
}

DEFAULT_DISCLAIMER = "本结果不构成投资建议，市场有风险，投资需谨慎。"

#: 一字板事实在 ``diagnostics.warnings`` 里的措辞（由 ``status.execution_block``
#: 产生）。渲染层只读 CLI 已经写出的事实，不重新判定行情。
_ONE_WORD_MARKERS = {
    "一字涨停": "up",
    "一字跌停": "down",
}

#: --compact 的告警压缩规则（只作用 ``diagnostics.warnings``）。
_COMPACT_URL_RE = re.compile(r"https?://[^\s，。；、（）()]+")
_COMPACT_INTERNAL_RE = re.compile(
    r"（[^）]*(?:Traceback|Error|Exception|\.py\b|line \d+)[^）]*）")
_COMPACT_CODE_RE = re.compile(
    r"（(?:[A-Z]{1,2}\d{1,2}[a-z]?(?:-\d+)?|issue #\d+)）")
_COMPACT_PATH_RE = re.compile(r"(?:/[\w.\-]+){2,}")

#: regime 标签的相对强弱（只用于判断「逆风/顺风」，不参与任何决策）。
_REGIME_RANK = {"Bull": 2, "Shock": 1, "Bear": 0}


# ---------------------------------------------------------------------------
# 取值与格式化（全部为 None 安全）
# ---------------------------------------------------------------------------
def _dict(value) -> Dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _text(value) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _num(value) -> Optional[float]:
    """有限数值 → float；bool / None / NaN / inf / 非数字 → None。"""
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _fmt_num(value) -> Optional[str]:
    """价格等普通数值：最多 4 位小数，去掉无意义的尾零。"""
    number = _num(value)
    if number is None:
        return None
    text = f"{number:.4f}" if abs(number) < 1000 else f"{number:.2f}"
    text = text.rstrip("0").rstrip(".")
    return "0" if text in ("", "-0") else text


def _fmt_pct(ratio) -> Optional[str]:
    """0.15 → ``15%``；0.155 → ``15.5%``（最多 1 位小数）。"""
    number = _num(ratio)
    if number is None:
        return None
    text = f"{number * 100.0:.1f}".rstrip("0").rstrip(".")
    return f"{text}%"


def _round_pct(value) -> Optional[int]:
    """分位取整：四舍五入（half-up），不用 Python 的银行家舍入。"""
    number = _num(value)
    if number is None:
        return None
    return int(math.floor(number + 0.5))


def _fmt_money(yuan) -> Optional[str]:
    """元 → ``1.23 亿``（只用于资金流净额，方向另行表述）。"""
    number = _num(yuan)
    if number is None:
        return None
    text = f"{abs(number) / 1e8:.2f}".rstrip("0").rstrip(".")
    return f"{text or '0'} 亿"


def _date_text(value) -> Optional[str]:
    """ISO 时间戳 → 日期；无法识别时原样返回（不编造日期）。"""
    text = _text(value)
    if text is None:
        return None
    if "T" in text:
        text = text.split("T", 1)[0]
    if len(text) >= 10 and text[4] == "-" and text[7] == "-":
        return text[:10]
    return text


def _warnings(payload: Dict[str, Any]) -> List[str]:
    raw = _dict(payload.get("diagnostics")).get("warnings")
    if not isinstance(raw, (list, tuple)):
        return []
    return [str(item).strip() for item in raw
            if isinstance(item, str) and item.strip()]


def _display_name(payload: Dict[str, Any], inst: Dict[str, Any]) -> Optional[str]:
    """名称优先级：``instrument.display_name`` → 顶层 ``display_name`` → ``name``。"""
    return (_text(inst.get("display_name"))
            or _text(payload.get("display_name"))
            or _text(inst.get("name")))


def _is_fund(payload: Dict[str, Any]) -> bool:
    """基金判定：board / request.type / status_source 任一指向基金即视为基金。"""
    inst = _dict(payload.get("instrument"))
    request = _dict(payload.get("request"))
    if _text(inst.get("board")) == "fund":
        return True
    if _text(request.get("type")) == "fund":
        return True
    return (_text(inst.get("status_source")) or "").startswith("fund")


def _freshness(payload: Dict[str, Any]):
    """返回 ``(stale, lag_days, quote_stale, data_stale, end, expected, date_mismatch)``。

    任一项成立即视为「非当日数据」：报价定格、行情回退旧缓存、lag_days >= 1、
    行情末日早于期望交易日。只有 ``quote_stale`` 一项曾是旧行为。
    """
    inst = _dict(payload.get("instrument"))
    data = _dict(payload.get("data"))
    quote_stale = inst.get("quote_stale") is True
    data_stale = data.get("stale") is True
    lag_days = _num(data.get("lag_days"))
    lagging = lag_days is not None and lag_days >= 1
    end = _date_text(data.get("end"))
    expected = _date_text(data.get("expected_date"))
    date_mismatch = bool(end and expected and end < expected)
    stale = quote_stale or data_stale or lagging or date_mismatch
    return stale, lag_days, quote_stale, data_stale, end, expected, date_mismatch


def _compact_warning(text: str) -> str:
    """压缩单条 warning：去 URL / 异常堆栈 / 内部代号，保留首句与关键事实。"""
    original = text
    # 堆栈从 Traceback 起整段丢弃（前面若有可读事实则保留）。
    if "Traceback" in text:
        text = text.split("Traceback", 1)[0]
    text = _COMPACT_URL_RE.sub("", text)
    text = _COMPACT_INTERNAL_RE.sub("", text)
    text = _COMPACT_PATH_RE.sub("", text)
    text = _COMPACT_CODE_RE.sub("", text)

    # 只保留首句（中文句号）；不在小数点/英文缩写上切分。
    if "。" in text:
        text = text.split("。", 1)[0] + "。"

    text = re.sub(r"\s+", " ", text)
    text = re.sub(r"\s+([，。；、）])", r"\1", text)
    text = re.sub(r"([（])\s+", r"\1", text)
    text = text.strip(" ；，、")
    if not text:
        return "内部错误详情已省略"
    # 压缩后若把原文的事实全删光（只剩标点），回退到首句原文。
    if not re.search(r"[一-鿿A-Za-z0-9]", text):
        return original.split("。", 1)[0].strip() or "内部错误详情已省略"
    return text


def _horizon_value(rec: Dict[str, Any]) -> Optional[int]:
    raw = rec.get("horizon_days")
    candidates = raw if isinstance(raw, (list, tuple)) else [raw]
    for item in candidates:
        number = _num(item)
        if number is not None and number > 0:
            return int(round(number))
    return None


def _one_word(inst: Dict[str, Any], warnings: List[str]) -> Optional[str]:
    """一字板方向：优先读显式字段，其次读 CLI 已写入的 warning 事实。"""
    explicit = _text(inst.get("one_word_limit"))
    if explicit in ("up", "down"):
        return explicit
    for warning in warnings:
        for marker, direction in _ONE_WORD_MARKERS.items():
            if marker in warning:
                return direction
    return None


# ---------------------------------------------------------------------------
# T2：可执行性闸门 + 数据新鲜度
# ---------------------------------------------------------------------------
def _gate_reason(payload: Dict[str, Any]) -> Optional[str]:
    """返回「今日不建议执行」的具体原因；``None`` 表示不拦截。

    只有 **确认** 的事实才拦截（``is_suspended is True`` / 一字板 / ``is_st
    is True`` / 收盘涨跌停 / ``status == untradeable``）。``is_suspended is
    None`` 是盘前无法判断，绝不在此误判为停牌。
    """
    inst = _dict(payload.get("instrument"))
    rec = _dict(payload.get("recommendation"))
    data = _dict(payload.get("data"))
    warnings = _warnings(payload)

    if inst.get("is_suspended") is True:
        if any("疑似退市" in warning for warning in warnings):
            return "疑似退市/长期停牌（行情与报价同时停滞），今日不可执行"
        return "停牌（确认无当日成交），今日不可执行"

    one_word = _one_word(inst, warnings)
    if one_word == "up":
        return "一字涨停（最高=最低=涨停价），买单无法成交，今日不可执行"
    if one_word == "down":
        return "一字跌停（最高=最低=跌停价），卖单无法成交，今日不可执行"

    if inst.get("is_st") is True:
        return "ST 风险警示股（退市与流动性风险显著高于普通股）"

    last_close = _num(data.get("last_close"))
    limit_up = _num(inst.get("limit_up"))
    limit_down = _num(inst.get("limit_down"))
    if last_close is not None and limit_down is not None \
            and abs(last_close - limit_down) < 0.005:
        return f"收盘跌停（{_fmt_num(last_close)}），按该价格卖出不可执行"
    if last_close is not None and limit_up is not None \
            and abs(last_close - limit_up) < 0.005:
        return f"收盘涨停（{_fmt_num(last_close)}），按该价格买入不可执行"

    if _text(rec.get("status")) == "untradeable":
        return "策略判定不可交易（不可执行）"
    return None


def _status_note(inst: Dict[str, Any]) -> Optional[str]:
    """``is_suspended=None`` 时区分「盘前无法判断」与「报价不可用」。

    T2 的硬要求：盘前（集合竞价前）腾讯报价定格是正常现象，不能把正常标的
    误标成停牌。因此这里只输出**状态说明**，不输出「不建议执行」。
    """
    if inst.get("is_suspended") is not None:
        return None
    source = _text(inst.get("status_source")) or ""
    if source.startswith("fund"):
        return None
    quote_stale = inst.get("quote_stale")
    if quote_stale is True:
        return "交易状态：盘前无法判断是否停牌（报价定格，未确认停牌）"
    if quote_stale is False:
        return "交易状态：未给出停牌判定（报价可用，未确认停牌）"
    return "交易状态：未知（无实时报价，不能确认是否停牌）"


def _price_line(payload: Dict[str, Any]) -> str:
    inst = _dict(payload.get("instrument"))
    data = _dict(payload.get("data"))
    request = _dict(payload.get("request"))

    code = _text(inst.get("code")) or _text(request.get("code")) or "未知标的"
    name = _display_name(payload, inst)
    # 场外基金的 name 常等于代码：只打印一次代码，绝不出现「代码 代码」。
    head = code if not name or name == code else f"{code} {name}"
    price = _fmt_num(data.get("last_close"))

    stale, lag_days, quote_stale, data_stale, end, expected, date_mismatch = \
        _freshness(payload)
    if stale:
        day = (end or _date_text(data.get("cache_last_date"))
               or expected or "未知日期")
        hints: List[str] = []
        if quote_stale:
            hints.append("报价定格")
        if lag_days is not None and lag_days >= 1:
            hints.append(f"滞后 {int(lag_days)} 日")
        elif date_mismatch:
            hints.append("非最新交易日")
        if data_stale and not hints:
            hints.append("数据回退旧缓存")
        inner = "，".join(([f"价格 {price}"] if price else []) + hints)
        return f"{head} 收盘为 {day}（{inner}）"
    if price:
        return f"{head} 现价 {price}"
    return head


def _action_line(rec: Dict[str, Any]) -> Optional[str]:
    action = _text(rec.get("action"))
    confidence = _fmt_pct(rec.get("confidence"))
    parts = []
    if action:
        parts.append(f"动作 {action}")
    if confidence:
        parts.append(f"置信 {confidence}")
    return "｜".join(parts) or None


def _position_line(rec: Dict[str, Any]) -> Optional[str]:
    """T1：建议仓位 / 持有期 / 复审日；任一为 None 时该段整体消失。"""
    parts = []
    position = _fmt_pct(rec.get("position_fraction"))
    if position is not None:
        parts.append(f"建议仓位 {position}")

    horizon = _horizon_value(rec)
    review_date = _date_text(rec.get("review_date"))
    if horizon is not None:
        text = f"持有 {horizon} 日"
        if review_date:
            text += f"，{review_date} 复审"
        parts.append(text)
    elif review_date:
        parts.append(f"{review_date} 复审")
    return "｜".join(parts) or None


def _levels_line(rec: Dict[str, Any]) -> Optional[str]:
    """价位原样展示（不重算）；缺失字段整段消失。"""
    parts = []
    zone = rec.get("entry_zone")
    if isinstance(zone, (list, tuple)) and len(zone) == 2:
        lower, upper = _fmt_num(zone[0]), _fmt_num(zone[1])
        if lower is not None and upper is not None:
            parts.append(f"入场区 {lower}-{upper}")
    stop_loss = _fmt_num(rec.get("stop_loss"))
    if stop_loss is not None:
        parts.append(f"止损 {stop_loss}")
    take_profit = _fmt_num(rec.get("take_profit"))
    if take_profit is not None:
        parts.append(f"止盈 {take_profit}")
    return "价位：" + "｜".join(parts) if parts else None


def _reason_line(rec: Dict[str, Any]) -> Optional[str]:
    reasons = rec.get("action_reason")
    if not isinstance(reasons, (list, tuple)):
        return None
    for reason in reasons:
        text = _text(reason)
        if text:
            return f"依据：{text}"
    return None


# ---------------------------------------------------------------------------
# T3：regime 冲突
# ---------------------------------------------------------------------------
def _regime_line(payload: Dict[str, Any]) -> Optional[str]:
    """个股与指数 label 不一致时才输出；一致 / 指数缺失时整行消失。"""
    regime = _dict(payload.get("market_regime"))
    index = _dict(regime.get("index"))
    stock_label = _text(regime.get("label"))
    index_label = _text(index.get("label"))
    if not stock_label or not index_label or stock_label == index_label:
        return None
    if stock_label not in _REGIME_RANK or index_label not in _REGIME_RANK:
        return None
    if _REGIME_RANK[stock_label] == _REGIME_RANK[index_label]:
        return None

    kind = "逆风" if _REGIME_RANK[index_label] < _REGIME_RANK[stock_label] else "顺风"
    stock_conf = _fmt_num(regime.get("confidence"))
    index_conf = _fmt_num(index.get("confidence"))
    subject = "标的" if _is_fund(payload) else "个股"
    stock_text = f"{subject} {stock_label}" + (f"({stock_conf})" if stock_conf else "")
    index_text = f"指数 {index_label}" + (f"({index_conf})" if index_conf else "")
    return (f"⚠️ 大盘{kind}：{index_text} vs {stock_text}"
            "（仅提示口径差异，不改变建议）")


# ---------------------------------------------------------------------------
# T4：估值分位
# ---------------------------------------------------------------------------
def _valuation_line(payload: Dict[str, Any]) -> Optional[str]:
    """PE/PB 3 年分位一句；无数据时整句消失（绝不给「估值适中」）。"""
    valuation = _dict(_dict(payload.get("evidence")).get("valuation"))
    if valuation.get("available") is not True:
        return None
    if _text(valuation.get("reason")) == "cache_missing":
        return None

    samples = _dict(valuation.get("percentile_samples"))
    parts = []
    for metric, field, label in (("pe", "pe_ttm", "PE"),
                                 ("pb", "pb", "PB")):
        percentile = _round_pct(valuation.get(f"{metric}_percentile_3y"))
        if percentile is None:
            continue
        window = ["3 年"]
        if metric == "pe":
            window.append("TTM")
        current = _fmt_num(valuation.get(field))
        if current is not None:
            window.append(f"当前 {current}")
        sample_count = _num(samples.get(metric))
        sample_note = ""
        if sample_count is not None and sample_count < PERCENTILE_WINDOW_DAYS:
            sample_note = f"（样本 {int(sample_count)} 日）"
        parts.append(f"{label} 分位 {percentile}%（{'；'.join(window)}）{sample_note}")
    return "估值：" + "；".join(parts) if parts else None


# ---------------------------------------------------------------------------
# T5：资金流
# ---------------------------------------------------------------------------
def _flow_direction(flow: Dict[str, Any]) -> Optional[str]:
    direction = _text(flow.get("direction"))
    if direction in ("inflow", "outflow", "flat"):
        return direction
    # 方向字段缺失时，按该口径的 5 日净额兜底（不跨口径猜）。
    caliber = _text(flow.get("caliber"))
    if caliber == "eastmoney_main_net":
        value = _num(flow.get("main_net_5d"))
    elif caliber == "sina_total_net":
        value = _num(flow.get("net_5d"))
    else:
        value = None
    if value is None:
        return None
    return "flat" if value == 0 else ("inflow" if value > 0 else "outflow")


def _flow_line(payload: Dict[str, Any]) -> Optional[str]:
    flow = _dict(_dict(payload.get("evidence")).get("flow"))
    if flow.get("available") is not True:
        return None
    direction = _flow_direction(flow)
    if direction is None:
        return None
    phrase = {"inflow": "净流入", "outflow": "净流出", "flat": "基本持平"}[direction]

    caliber = _text(flow.get("caliber"))
    main_5d, main_20d = _num(flow.get("main_net_5d")), _num(flow.get("main_net_20d"))
    net_5d, net_20d = _num(flow.get("net_5d")), _num(flow.get("net_20d"))

    # 混源：口径缺失，或两族字段同时有值 —— 只给方向，不给净额。
    mixed = (caliber not in CALIBER_LABELS
             or (main_5d is not None and net_5d is not None)
             or (main_20d is not None and net_20d is not None))
    stale = flow.get("stale") is True
    as_of = _date_text(flow.get("as_of"))
    stale_note = f"，{as_of} 数据" if stale and as_of else ""
    if stale and not as_of:
        stale_note = "，数据已过期"

    if mixed:
        return f"资金流：{phrase}方向（混源口径，不提供净额{stale_note}）"

    if caliber == "eastmoney_main_net":
        label = "主力"
        window, amount = ((5, main_5d) if main_5d is not None
                          else (20, main_20d))
    else:  # sina_total_net
        label = "总"
        window, amount = ((5, net_5d) if net_5d is not None
                          else (20, net_20d))

    caliber_text = CALIBER_LABELS[caliber]
    if direction == "flat" or amount is None:
        # 没有可用净额时不编造窗口/数值，只描述方向。
        return f"资金流：{label}{phrase}（{caliber_text}{stale_note}）"
    return (f"资金流：{label} {window} 日{phrase} {_fmt_money(amount)}"
            f"（{caliber_text}{stale_note}）")


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------
def render_advice(payload: dict, compact: bool = False) -> str:
    """把 ``cli.py --json`` 的完整响应渲染成一段可直接投递的文本。

    纯函数：不改入参、不读文件、不触网。缺失字段对应的句子/段落整体消失，
    输出中不会出现 ``None``。

    ``compact=True`` 只压缩 ``diagnostics.warnings``（去 URL / 堆栈 / 内部代号，
    保留首句与关键事实）；其余行与默认输出完全一致。
    """
    document = _dict(payload)
    rec = _dict(document.get("recommendation"))
    inst = _dict(document.get("instrument"))

    lines: List[str] = []

    # T2（前置）：数据问题必须先于建议报告。
    for warning in _warnings(document):
        shown = _compact_warning(warning) if compact else warning
        lines.append(f"⚠️ 数据: {shown}")
    if lines:
        lines.append("")

    # T2：可执行性闸门是建议块的首行。
    gate = _gate_reason(document)
    if gate:
        lines.append(f"【今日不建议执行】{gate}")

    lines.append(_price_line(document))

    action = _action_line(rec)
    if action:
        lines.append(action)

    position = _position_line(rec)
    if position:
        lines.append(position)

    note = _status_note(inst)
    if note:
        lines.append(note)

    levels = _levels_line(rec)
    if levels:
        lines.append(levels)

    regime = _regime_line(document)
    if regime:
        lines.append(regime)

    valuation = _valuation_line(document)
    if valuation:
        lines.append(valuation)

    flow = _flow_line(document)
    if flow:
        lines.append(flow)

    reason = _reason_line(rec)
    if reason:
        lines.append(reason)

    disclaimer = _text(document.get("disclaimer")) or DEFAULT_DISCLAIMER
    lines.append(f"免责声明：{disclaimer}")
    return "\n".join(lines)
