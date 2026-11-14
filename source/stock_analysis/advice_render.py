# -*- coding: utf-8 -*-
"""建议渲染层 v1 —— 把 CLI JSON 里已有、但从未到达用户眼前的数据送进每日建议。

对应 ``docs/ASTOCK_GAP_INVENTORY.md`` 的 T1–T5（issue #1 的 R11 Top 5）：

========  ==========================================================
T1        建议仓位 / 持有期 / 复审日（``recommendation.*``）+ 触发事实
          （``evidence.triggers[]`` / ``recommendation.distance_to_trigger``）
T2        可执行性闸门 + 数据新鲜度（``instrument.*`` / ``diagnostics.warnings``）
T3        个股 regime 与指数 regime 的口径冲突（``market_regime.index``）
T4        估值分位一句（``evidence.valuation``）
T5        资金流方向 + 口径标签（``evidence.flow``）
T6        交易所重点监控池命中（``evidence.market_watch``）—— 硬风险告警
T7        事件风险（``evidence.events``）：解禁 / 股东增减持 / 业绩预告 /
          股权质押 —— 只报 20 日内且达到阈值的重大事件
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
  读取显式字段 ``instrument.one_word_limit``（``"up"`` / ``"down"``），缺失时
  才从 ``diagnostics.warnings`` 里识别 CLI 已写入的「一字涨停/一字跌停」事实。
* ``evidence.triggers[]`` 只描述**已触发**的买卖信号（``state`` 为
  ``buy_triggered`` / ``sell_triggered``）；触发价取 ``buy_price`` /
  ``sell_price``，距触发百分比取 ``recommendation.distance_to_trigger`` 的
  ``buy_pct`` / ``sell_pct``，**逐值照抄、不重算**。字段缺失或没有已触发
  信号时整行消失。
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
* ``evidence.market_watch``（T6）：只有 ``available is True`` **且**
  ``hit.listed is True`` 才输出 ``🚫 交易所重点监控（{start}~{end}）``。
  ``available=False``（没取到名单）时**不输出**任何风险结论 —— 由
  ``diagnostics.warnings`` 说明原因，绝不把「没取到」写成「没被监控」。
  ``stale=True``（回退旧缓存）**照常输出**命中行，日期取该条目的生效窗口
  （``start~end``，与新鲜数据同一口径）；「名单截至哪天」由
  ``evidence.market_watch.as_of`` 与 warnings 说明 —— 命中是风险提示，
  宁可多报也不因缓存旧了一天就静默隐藏。命中行位置固定在可执行性闸门之后、
  价格/建议行之前。
* ``evidence.events``（T7）：四类子项（``lockup`` / ``holder_trades`` /
  ``forecast`` / ``pledge``）各自 ``available`` / ``stale`` / ``as_of`` /
  ``items[]``。只报**距快照日 20 个自然日内**且达到阈值的重大事件，每类最多
  一行；无事件则整类消失。阈值见 :data:`EVENT_THRESHOLDS`（写死在代码里，
  理由见 ``docs/EVENT_RISK.md``）。``available=False`` 或 ``stale=True`` 时
  必须先输出 ``⚠️ 事件数据过期（{D}）``，**绝不**把「没取到」写成「无事件」。
  专用文案：缓存未生成 → 「未生成」；探针失败 → 「接口疑似异常」；
  北交所 ``not_supported`` → 「ℹ️ …：未覆盖」。基金 / ``--as-of`` 回放的
  ``fund_not_supported`` / ``replay_not_supported`` 整段消失（由 warnings
  说明原因）。
* ``compact=True`` 只处理 ``diagnostics.warnings``，结果由 **封闭词表替换 +
  整段删除** 两部分组成：

  - 删除只允许三类：URL、堆栈/回溯段、**配对完整**的内部引用括号；括号不配对
    时不删括号，删除后若括号失衡则退回原文。删除不改写、不截句、不重排、不删
    斜杠枚举中的单项。
  - 替换只允许 :data:`COMPACT_GLOSSARY`（模块级封闭集合）里的项。命中必须满足
    词边界：前后不能粘连字母/数字/下划线/中文（例如 ``is_st`` 不得命中
    ``is_status``，也不得命中 ``仅is_st推断``）。

  不变量（issue #92 的裁定口径）：

  1. 告警**不含**任何词表 token 时，输出必须是原文的删除子序列；
  2. 告警**含**词表 token 时，把每个命中 token（必要时连同紧邻的一个 ASCII
     空格）替换为词表标签后，结果必须是该序列的删除子序列——除被替换 token
     外逐字保持原序；
  3. 替换/删除不得在中文与中文之间顶出空格，也不得留下连续两个空格
     （连续空格折叠、中文夹空格吸收）；
  4. 词表命中只对**原文位置**判定：删除段拼接出的 token（``is_（…）st`` →
     ``is_st``）不触发替换。

  默认输出逐字节不变。
"""
from __future__ import annotations

import datetime as _dt
import math
import re
from typing import Any, Dict, List, Optional

__all__ = [
    "render_advice",
    "CALIBER_LABELS",
    "COMPACT_GLOSSARY",
    "EVENT_STALE_MARK",
    "EVENT_THRESHOLDS",
    "EVENT_WINDOW_DAYS",
    "MARKET_WATCH_MARK",
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

# ---------------------------------------------------------------------------
# T7：事件风险阈值（写死在代码里；理由见 docs/EVENT_RISK.md）
# ---------------------------------------------------------------------------
#: 事件窗口：距快照日 20 个**自然日**内（含第 20 日）。与 20 日持有期同一尺度。
EVENT_WINDOW_DAYS = 20
#: 解禁：占总股本 ≥1.0%，或解禁股数 ≥500 万股（任一即报）。
LOCKUP_RATIO_PCT_MIN = 1.0
LOCKUP_SHARES_MIN_10K = 500.0
#: 股东减持：窗口内合计占总股本 ≥0.5%，或合计 ≥500 万股（任一即报）。
HOLDER_REDUCE_PCT_MIN = 0.5
HOLDER_REDUCE_SHARES_MIN_10K = 500.0
#: 业绩预告：负面类型，或预告净利同比上限 ≤ -20%。
FORECAST_NEGATIVE_TYPES = frozenset({"首亏", "续亏", "预减", "增亏", "略减"})
FORECAST_CHANGE_PCT_MAX = -20.0
#: 股权质押：最新统计日质押比例 ≥20%。
PLEDGE_RATIO_PCT_MIN = 20.0
#: 用户可见的过期前缀（测试与 Hermes 侧按此识别）。
EVENT_STALE_MARK = "⚠️ 事件数据过期"
#: 阈值表的只读快照（测试与文档共用）。
EVENT_THRESHOLDS = {
    "window_days": EVENT_WINDOW_DAYS,
    "lockup_ratio_pct_min": LOCKUP_RATIO_PCT_MIN,
    "lockup_shares_10k_min": LOCKUP_SHARES_MIN_10K,
    "holder_reduce_pct_min": HOLDER_REDUCE_PCT_MIN,
    "holder_reduce_shares_10k_min": HOLDER_REDUCE_SHARES_MIN_10K,
    "forecast_change_pct_max": FORECAST_CHANGE_PCT_MAX,
    "pledge_ratio_pct_min": PLEDGE_RATIO_PCT_MIN,
}

#: 一字板事实在 ``diagnostics.warnings`` 里的措辞（由 ``status.execution_block``
#: 产生）。渲染层只读 CLI 已经写出的事实，不重新判定行情。
_ONE_WORD_MARKERS = {
    "一字涨停": "up",
    "一字跌停": "down",
}

#: --compact 的告警压缩规则（只作用 ``diagnostics.warnings``）。
#: 硬约束：只删整段，不做任何句内改写 —— 因此这里**没有**路径/空白/标点
#: 归一化规则，也**不**截断句子。历史上的 ``(?:/[\w.\-]+){2,}`` 会把中文
#: 斜杠枚举「停牌/退市/代码无效无法区分」当成路径删掉，把「无法判断」写成
#: 事实「停牌」，已删除该类规则。
_COMPACT_URL_RE = re.compile(r"https?://[^\s，。；、（）()<>\"']+")

#: 堆栈/回溯中的独立片段（``File "…", line N`` / ``During handling of…``）。
_COMPACT_STACK_SEGMENT_RES = (
    re.compile(r'File\s+"[^"]*",\s*line\s+\d+[^\n]*'),
    re.compile(r"During handling of the above exception[^\n]*"),
)

#: 括号内容命中这些标记即视为「内部引用 / 内部诊断」，整对括号连同内容删除。
#: 只作用于**配对完整**的括号；配对由 :func:`_internal_paren_deletions` 保证。
_COMPACT_INTERNAL_MARKERS_RE = re.compile(
    r"issue\s*#\d+"
    r"|^\s*(?:参见|详见|见)"
    r"|Traceback|Error|Exception|\.py\b|\bline\s+\d+"
    r"|During handling of"
    r"|\b(?:p_positive|trigger\.current|is_st|action|confidence)\b"
    r"|[A-Z]{1,2}\d{1,2}[a-z]?(?:-\d+)?"
)

#: ``--compact`` 允许的**固定词表替换**（模块级封闭集合）：只覆盖少量会直接
#: 漏到用户眼前的裸内部字段名/噪声词。词表之外仍只允许整段删除（见
#: :func:`_compact_warning`）。
#:
#: 安全红线：这些 token 与「停牌/退市/ST/一字」告警的限定词（「无法区分」
#: 「无法判断」「无法成交」）无关，替换不会删除或弱化任何限定词。
#:
#: ``no_edge`` / ``95% CI`` 属于 P4：``cli.py`` 的 no_edge 告警会直接把统计
#: 术语写给用户。替换只做同义白话化，不把「统计上无优势」改写成「没有机会」。
COMPACT_GLOSSARY: Dict[str, str] = {
    "p_positive": "买侧期望胜率",
    "trigger.current": "触发参考价",
    "is_st": "ST 标记",
    "ProxyError": "代理异常",
    "no_edge": "无统计优势",
    "95% CI": "置信区间",
    # N4：``cli.py`` 的 no_edge 告警原句是「统计上无优势，判 no_edge」，
    # 逐 token 替换会得到「统计上无优势，判无统计优势」的同义重复。短语优先
    # 匹配整段，保留前半句事实、删掉重复的判定分句（语义不变）。
    "统计上无优势，判 no_edge": "统计上无优势",
}


def _glossary_matcher(glossary: Dict[str, str]) -> re.Pattern:
    """把词表编译成一个带词边界的 alternation。

    边界口径：token 前后都不能粘连 ``\\w``（字母/数字/下划线，Python 的
    ``\\w`` 在 Unicode 下也包含中文）或 ``.``（避免命中 ``a.is_st`` 这类字段
    路径）。长 token 优先，避免前缀互相遮蔽。
    """
    tokens = sorted(glossary, key=len, reverse=True)
    return re.compile(
        r"(?<![\w.])(" + "|".join(re.escape(token) for token in tokens)
        + r")(?![\w.])"
    )


_COMPACT_GLOSSARY_RE = _glossary_matcher(COMPACT_GLOSSARY)

#: 判断「中文夹空格」用的 CJK 统一表意文字范围（URO / 扩展 A / 兼容表意）。
_CJK_CHAR_CLASS = r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]"

#: N1：compact 输出不得出现连续两个空格，也不得在中文之间夹空格。
_COMPACT_SPACE_RUN_RE = re.compile(r" {2,}")
_COMPACT_CJK_SPACE_RE = re.compile(
    r"(?<=" + _CJK_CHAR_CLASS + r") (?=" + _CJK_CHAR_CLASS + r")")


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


def _paren_pairs(text: str):
    """扫描括号配对，返回 ``(spans, balanced)``。

    ``spans`` 为 ``(start, end)``（含两侧括号）；只认同种括号（``（）`` /
    ``()``）配对，其它情况视为不配对。``balanced=False`` 时调用方不得删除
    任何括号，避免留下孤立括号。
    """
    stack: List[tuple] = []
    spans: List[tuple] = []
    balanced = True
    for index, char in enumerate(text):
        if char in ("（", "("):
            stack.append((char, index))
        elif char in ("）", ")"):
            opener = "（" if char == "）" else "("
            if stack and stack[-1][0] == opener:
                _, start = stack.pop()
                spans.append((start, index + 1))
            else:
                balanced = False
    if stack:
        balanced = False
    return spans, balanced


def _parens_balanced(text: str) -> bool:
    """括号计数平衡（深度不为负且最终归零）。"""
    depth = 0
    for char in text:
        if char in ("（", "("):
            depth += 1
        elif char in ("）", ")"):
            depth -= 1
            if depth < 0:
                return False
    return depth == 0


def _internal_paren_deletions(text: str) -> List[tuple]:
    """返回可整对删除的内部引用括号区间；括号不配对时返回空列表。"""
    spans, balanced = _paren_pairs(text)
    if not balanced:
        return []
    return [(start, end) for start, end in spans
            if _COMPACT_INTERNAL_MARKERS_RE.search(text[start + 1:end - 1])]


def _merge_spans(spans: List[tuple]) -> List[List[int]]:
    """合并重叠/相邻的半开区间 ``[start, end)``。"""
    merged: List[List[int]] = []
    for start, end in sorted(spans):
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return merged


def _apply_deletions(text: str, deletions: List[tuple]) -> str:
    """按区间删除后拼接；区间可重叠/相邻。"""
    merged = _merge_spans(deletions)
    if not merged:
        return text
    parts: List[str] = []
    cursor = 0
    for start, end in merged:
        parts.append(text[cursor:start])
        cursor = end
    parts.append(text[cursor:])
    return "".join(parts)


def _normalize_compact_spaces(text: str) -> str:
    """折叠连续 ASCII 空格，并吸收两个中文字符之间的单个空格（N1）。

    契约：compact 输出不得出现连续两个空格，也不得出现中文夹空格。删除
    空格仍是原文的删除子序列，不引入新字符。
    """
    text = _COMPACT_SPACE_RUN_RE.sub(" ", text)
    return _COMPACT_CJK_SPACE_RE.sub("", text)


def _apply_compact_edits(original: str, deletions: List[tuple]) -> str:
    """在**原文位置**上同时应用整段删除与封闭词表替换。

    N2：词表匹配只对原文做，且落进删除区间的命中由删除优先。若删除段把
    两侧文本拼成一个词表 token（例如 ``is_（…）st`` → ``is_st``），这个拼接
    产物不在原文里，因而不会触发替换 —— 避免「不含 token 的告警」被二次替换。
    词表是封闭集合：不在词表内的内部字段名一律不替换（只允许整段删除）。
    """
    merged = _merge_spans(deletions)
    operations: List[tuple] = [
        (start, end, "") for start, end in merged]

    span_index = 0
    for match in _COMPACT_GLOSSARY_RE.finditer(original):
        start, end = match.span()
        while span_index < len(merged) and merged[span_index][1] <= start:
            span_index += 1
        if span_index < len(merged) and merged[span_index][0] < end:
            continue  # 命中落在删除区间内：删除优先，不参与替换。
        operations.append((start, end, COMPACT_GLOSSARY[match.group(1)]))

    operations.sort(key=lambda item: item[0])
    pieces: List[str] = []
    cursor = 0
    for start, end, replacement in operations:
        if start < cursor:  # 区间已被合并/覆盖，跳过。
            continue
        pieces.append(original[cursor:start])
        pieces.append(replacement)
        cursor = end
    pieces.append(original[cursor:])
    return _normalize_compact_spaces("".join(pieces).strip())


def _compact_warning(text: str) -> str:
    """压缩单条 warning —— **整段删除 + 封闭词表替换**。

    删除只允许三类：URL、堆栈/回溯段、配对完整的内部引用括号。不改写措辞、
    不截断句子、不删斜杠枚举中的单项、不做单侧去括号；括号不配对时不删。
    删除后若括号失衡，整条退回原文。替换与删除在**原文位置**上一次完成：
    删除拼接出的 token 不会被再次替换（N2）。输出折叠连续空格并吸收中文
    夹空格（N1）；词表之外的内部引用仍只允许整段删除。返回空字符串表示
    整条都是内部细节，调用方应丢弃该行。
    """
    original = text
    deletions: List[tuple] = []

    for match in _COMPACT_URL_RE.finditer(original):
        deletions.append((match.start(), match.end()))

    # 堆栈从 Traceback 起整段丢弃（前面若有可读事实则保留）。
    traceback_at = original.find("Traceback")
    if traceback_at != -1:
        deletions.append((traceback_at, len(original)))
    for pattern in _COMPACT_STACK_SEGMENT_RES:
        for match in pattern.finditer(original):
            deletions.append((match.start(), match.end()))

    deletions.extend(_internal_paren_deletions(original))

    text = _apply_deletions(original, deletions).strip()
    if not _parens_balanced(text):
        return original.strip()
    return _apply_compact_edits(original, deletions)


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


def _st_note(inst: Dict[str, Any]) -> Optional[str]:
    """ST 状态不可得时的措辞（issue #106）。

    只有 ``is_st_source='unavailable'`` / ``is_st is None`` 时才输出
    「ST 状态未知」；**绝不**把未知写成「非 ST」。基金不参与 ST 判定。
    """
    if _text(inst.get("board")) == "fund":
        return None
    if (_text(inst.get("status_source")) or "").startswith("fund"):
        return None
    if inst.get("is_st") is True:
        return None  # 已由可执行性闸门写「ST 风险警示股」
    source = _text(inst.get("is_st_source"))
    if source == "unavailable":
        return "ST 状态未知（ST 名单、实时报价与本地名称均不可用）"
    if inst.get("is_st") is None and source:
        return "ST 状态未知（仅本地名称不含 ST 标记，未能复核）"
    return None


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


#: 已触发状态 → (用户可读方向, 触发价字段, 距触发字段)。
_TRIGGER_STATES = {
    "buy_triggered": ("买信号", "buy_price", "buy_pct"),
    "sell_triggered": ("卖信号", "sell_price", "sell_pct"),
}


def _trigger_line(payload: Dict[str, Any]) -> Optional[str]:
    """T1：已触发的买/卖信号一行事实；无已触发信号或字段缺失时整行消失。

    触发价与「现价距触发」百分比**逐值取自 JSON**（``buy_price`` /
    ``sell_price`` 与 ``recommendation.distance_to_trigger``），绝不重算。
    """
    triggers = _dict(payload.get("evidence")).get("triggers")
    if not isinstance(triggers, (list, tuple)):
        return None
    distances = _dict(_dict(payload.get("recommendation")).get("distance_to_trigger"))
    # 行情非当日时不得自称「现价」（与 _price_line 的口径一致）；距离值仍是
    # JSON 里基于 last_close 的原值，这里只调整措辞，不重算。
    stale = _freshness(payload)[0]
    distance_label = "距触发" if stale else "现价距触发"

    parts: List[str] = []
    for item in triggers:
        trigger = _dict(item)
        state = _text(trigger.get("state"))
        mapping = _TRIGGER_STATES.get(state or "")
        if mapping is None:
            continue
        label, price_field, pct_field = mapping
        indicator = _text(trigger.get("indicator"))
        price = _fmt_num(trigger.get(price_field))
        distance = _fmt_num(distances.get(pct_field))
        if not indicator or price is None or distance is None:
            continue
        parts.append(f"{indicator} {label}已触发"
                     f"（触发价 {price}，{distance_label} {distance}%）")
    return "触发：" + "；".join(parts) if parts else None


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


#: ``status == "no_edge"`` 的白话解释（不写预测、不作保证）。
_NO_EDGE_PLAIN = "历史样本不足以支持买入判断，建议观望"
_NO_EDGE_PLAIN_SELL = "历史样本不足以支持趋势判断；本条为风控离场，不代表看空预测"


def _reason_line(rec: Dict[str, Any]) -> Optional[str]:
    """依据行；``status=no_edge`` 时追加一句不带术语的白话解释。

    白话句只描述样本不足，不写成预测或保证；原有可核对理由保留在前。
    """
    reasons = rec.get("action_reason")
    first = None
    if isinstance(reasons, (list, tuple)):
        for reason in reasons:
            text = _text(reason)
            if text:
                first = text
                break

    if _text(rec.get("status")) == "no_edge":
        plain = (_NO_EDGE_PLAIN_SELL if _text(rec.get("action")) == "sell"
                 else _NO_EDGE_PLAIN)
        if first is None:
            return f"依据：{plain}"
        if plain in first:
            return f"依据：{first}"
        return f"依据：{first}；{plain}"

    return f"依据：{first}" if first else None


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
# T6：交易所重点监控池（硬风险告警）
# ---------------------------------------------------------------------------
#: 命中行必须带的前缀（测试与 Hermes 侧按此识别）。
MARKET_WATCH_MARK = "🚫 交易所重点监控"


def _market_watch_line(payload: Dict[str, Any]) -> Optional[str]:
    """命中时输出一行 ``🚫 交易所重点监控（{start}~{end}）``；否则整行消失。

    口径（T6 契约）：
    * 只有 ``available is True`` **且** ``hit.listed is True`` 才输出 ——
      ``available=False``（没取到名单）不产生风险结论，由
      ``diagnostics.warnings`` 说明；
    * ``stale=True``（回退旧缓存）**不抑制**本行：命中是风险提示，宁可多报；
      「名单截至哪天」由 ``evidence.market_watch.as_of`` 与 warnings 说明；
    * ``start`` / ``end`` 逐值照抄 JSON，缺失时用 ``?`` 占位（不编造日期）；
    * 只描述事实，不改写 ``action`` / ``confidence``。
    """
    block = _dict(_dict(payload.get("evidence")).get("market_watch"))
    if block.get("available") is not True:
        return None
    hit = _dict(block.get("hit"))
    if hit.get("listed") is not True:
        return None
    start = _date_text(hit.get("start")) or "?"
    end = _date_text(hit.get("end")) or "?"
    return f"{MARKET_WATCH_MARK}（{start}~{end}）"


# ---------------------------------------------------------------------------
# T7：事件风险（解禁 / 增减持 / 业绩预告 / 质押）
# ---------------------------------------------------------------------------
_EVENT_SECTION_ORDER = ("lockup", "holder_trades", "forecast", "pledge")
_EVENT_SECTION_LABELS = {
    "lockup": "解禁",
    "holder_trades": "股东增减持",
    "forecast": "业绩预告",
    "pledge": "股权质押",
}
#: 这些原因表示「该口径本就不适用」，不是取数失败：不进过期行。
#: P3-1：``not_supported`` 不再静默 —— 由 ``_events_coverage_line`` 明确写「未覆盖」。
_EVENT_HIDDEN_REASONS = frozenset(
    {"fund_not_supported", "replay_not_supported"})
#: P2-2：接口疑似异常有独立文案，不与「过期」混写。
_EVENT_SUSPECT_REASONS = frozenset(
    {"interface_suspected", "empty_pool_suspected"})
#: 缓存从未生成 / 无该标的：写「未生成」，不写「过期（未知）」。
_EVENT_MISSING_REASONS = frozenset({"cache_missing", "code_missing"})


def _event_ref_date(section: Dict[str, Any]) -> Optional[str]:
    """事件窗口的基准日：缓存快照日（不是事件自身日期）。"""
    return (_date_text(section.get("fetched_at"))
            or _date_text(section.get("as_of")))


def _days_between(later: Optional[str], earlier: Optional[str]) -> Optional[int]:
    """``later - earlier`` 的自然日数；任一不可解析返回 ``None``。"""
    if not later or not earlier:
        return None
    try:
        return (_dt.date.fromisoformat(later)
                - _dt.date.fromisoformat(earlier)).days
    except ValueError:
        return None


def _fmt_event_pct(value) -> Optional[str]:
    """百分数数值（1.5 表示 1.5%）→ ``1.5%``。"""
    number = _num(value)
    if number is None:
        return None
    text = f"{number:.2f}".rstrip("0").rstrip(".")
    return f"{text or '0'}%"


def _fmt_10k(value) -> Optional[str]:
    """万股数值 → ``1200 万股``（只取绝对量，方向由文案表达）。"""
    number = _num(value)
    if number is None:
        return None
    text = f"{abs(number):.2f}".rstrip("0").rstrip(".")
    return f"{text or '0'} 万股"


def _fmt_change_span(item: Dict[str, Any]) -> Optional[str]:
    low = _num(item.get("change_pct_lower"))
    high = _num(item.get("change_pct_upper"))
    if low is None and high is None:
        return None
    if low is not None and high is not None:
        return f"{low:.1f}%~{high:.1f}%"
    value = low if low is not None else high
    return f"{value:.1f}%"


def _fmt_amount_span(item: Dict[str, Any]) -> Optional[str]:
    """金额区间用**同一单位**（按较大绝对值选亿/万/元），避免 ``-1.2亿~-8000万``。"""
    low = _num(item.get("amount_lower"))
    high = _num(item.get("amount_upper"))
    values = [value for value in (low, high) if value is not None]
    if not values:
        return None
    scale = max(abs(value) for value in values)
    if scale >= 1e8:
        unit, digits, suffix = 1e8, 2, "亿"
    elif scale >= 1e4:
        unit, digits, suffix = 1e4, 0, "万"
    else:
        unit, digits, suffix = 1.0, 0, "元"

    def _one(value):
        text = f"{value / unit:.{digits}f}".rstrip("0").rstrip(".")
        return f"{text or '0'}{suffix}"

    low_text = _one(low) if low is not None else None
    high_text = _one(high) if high is not None else None
    if low_text and high_text and low_text != high_text:
        return f"{low_text}~{high_text}"
    return low_text or high_text


def _events_stale_line(events: Dict[str, Any]) -> Optional[str]:
    """任一子项取数失败 / 过期 → 一行 ``⚠️ 事件数据过期（{D}）：{labels}``。

    P2-2 / P3-1 / P3-2 的专用原因（接口疑似异常、未覆盖、未生成）各有独立
    文案，不在这里重复出现。
    """
    stale_keys = []
    for key in _EVENT_SECTION_ORDER:
        section = _dict(events.get(key))
        reason = _text(section.get("reason"))
        if (reason in _EVENT_HIDDEN_REASONS
                or reason in _EVENT_SUSPECT_REASONS
                or reason in _EVENT_MISSING_REASONS
                or reason == "not_supported"):
            continue
        if section.get("stale") is True or section.get("available") is False:
            stale_keys.append(key)
    if not stale_keys:
        return None
    dates = [_date_text(_dict(events.get(key)).get("as_of")) for key in stale_keys]
    dates = [day for day in dates if day]
    day = min(dates) if dates else (_date_text(events.get("as_of")) or "未知")
    labels = "、".join(_EVENT_SECTION_LABELS[key] for key in stale_keys)
    return f"{EVENT_STALE_MARK}（{day}）：{labels}"


def _events_missing_line(events: Dict[str, Any]) -> Optional[str]:
    """P3-2：缓存从未生成 → 「未生成」，绝不渲染成「过期（未知）」。"""
    reason = _text(events.get("reason"))
    if reason not in _EVENT_MISSING_REASONS:
        return None
    labels = "、".join(_EVENT_SECTION_LABELS[key]
                     for key in _EVENT_SECTION_ORDER
                     if _text(_dict(events.get(key)).get("reason"))
                     not in (None, ""))
    labels = labels or "解禁、股东增减持、业绩预告、股权质押"
    if reason == "cache_missing":
        detail = "预计算未运行，请先运行 tools/precompute_events.py"
    else:
        detail = "缓存存在但该标的未包含在内，请重跑 tools/precompute_events.py"
    return f"⚠️ 事件数据未生成：{labels}（{detail}）"


def _events_suspect_line(events: Dict[str, Any]) -> Optional[str]:
    """P2-2：对照探针失败 / 全票池皆空 → 「不可用」，不是「无事件」。"""
    keys = []
    for key in _EVENT_SECTION_ORDER:
        if _text(_dict(events.get(key)).get("reason")) in _EVENT_SUSPECT_REASONS:
            keys.append(key)
    if not keys:
        return None
    labels = "、".join(_EVENT_SECTION_LABELS[key] for key in keys)
    return f"{EVENT_STALE_MARK}：{labels}（接口疑似异常，本轮空结果不采信）"


def _events_coverage_line(events: Dict[str, Any]) -> Optional[str]:
    """P3-1：``not_supported`` 显式写「未覆盖」，不得渲染成「无事件」。"""
    keys = [key for key in _EVENT_SECTION_ORDER
            if _text(_dict(events.get(key)).get("reason")) == "not_supported"]
    if not keys:
        return None
    labels = "、".join(_EVENT_SECTION_LABELS[key] for key in keys)
    return f"ℹ️ {labels}：未覆盖（该市场无此统计口径）"


def _lockup_line(events: Dict[str, Any]) -> Optional[str]:
    """未来 20 日内、达到额度阈值的解禁；只报最近的一批。"""
    section = _dict(events.get("lockup"))
    if section.get("available") is not True:
        return None
    ref = _event_ref_date(section)
    if not ref:
        return None
    best = None
    for raw in section.get("items") or []:
        item = _dict(raw)
        day = _date_text(item.get("date"))
        days = _days_between(day, ref)
        if days is None or days < 0 or days > EVENT_WINDOW_DAYS:
            continue
        ratio = _num(item.get("ratio_pct"))
        shares = _num(item.get("shares_10k"))
        qualifies = ((ratio is not None and ratio >= LOCKUP_RATIO_PCT_MIN)
                     or (shares is not None
                         and abs(shares) >= LOCKUP_SHARES_MIN_10K))
        if not qualifies:
            continue
        rank = (days, -(ratio or 0.0),
                -(abs(shares) if shares is not None else 0.0))
        if best is None or rank < best[0]:
            best = (rank, day, ratio, shares)
    if best is None:
        return None
    _rank, day, ratio, shares = best
    share_text, ratio_text = _fmt_10k(shares), _fmt_event_pct(ratio)
    if share_text and ratio_text:
        detail = f"解禁 {share_text}（占总股本 {ratio_text}）"
    elif share_text:
        detail = f"解禁 {share_text}"
    elif ratio_text:
        detail = f"解禁（占总股本 {ratio_text}）"
    else:
        return None
    return f"⚠️ 解禁：{day} {detail}"


def _holder_trade_line(events: Dict[str, Any]) -> Optional[str]:
    """近 20 日减持公告合计达到阈值；按公告条数 + 总股数/占比聚合。"""
    section = _dict(events.get("holder_trades"))
    if section.get("available") is not True:
        return None
    ref = _event_ref_date(section)
    if not ref:
        return None
    rows = []
    for raw in section.get("items") or []:
        item = _dict(raw)
        if _text(item.get("direction")) != "减持":
            continue
        days = _days_between(ref, _date_text(item.get("notice_date")))
        if days is None or days < 0 or days > EVENT_WINDOW_DAYS:
            continue
        rows.append(item)
    if not rows:
        return None
    total_pct = sum(abs(value) for value in
                    (_num(item.get("change_pct_total")) for item in rows)
                    if value is not None)
    total_shares = sum(abs(value) for value in
                       (_num(item.get("change_shares_10k")) for item in rows)
                       if value is not None)
    if not (total_pct >= HOLDER_REDUCE_PCT_MIN
            or total_shares >= HOLDER_REDUCE_SHARES_MIN_10K):
        return None
    share_text = _fmt_10k(total_shares) if total_shares else None
    pct_text = _fmt_event_pct(total_pct) if total_pct else None
    if share_text and pct_text:
        detail = f"合计 {share_text}（占总股本 {pct_text}）"
    elif share_text:
        detail = f"合计 {share_text}"
    elif pct_text:
        detail = f"合计占总股本 {pct_text}"
    else:
        return None
    return f"⚠️ 股东减持：近 {EVENT_WINDOW_DAYS} 日 {len(rows)} 笔，{detail}"


def _forecast_line(events: Dict[str, Any]) -> Optional[str]:
    """近 20 日负面业绩预告：取最新公告日，优先「归母净利润」指标。"""
    section = _dict(events.get("forecast"))
    if section.get("available") is not True:
        return None
    ref = _event_ref_date(section)
    if not ref:
        return None
    candidates = []
    for raw in section.get("items") or []:
        item = _dict(raw)
        days = _days_between(ref, _date_text(item.get("notice_date")))
        if days is None or days < 0 or days > EVENT_WINDOW_DAYS:
            continue
        forecast_type = _text(item.get("forecast_type"))
        upper = _num(item.get("change_pct_upper"))
        negative = (forecast_type in FORECAST_NEGATIVE_TYPES
                    or (upper is not None
                        and upper <= FORECAST_CHANGE_PCT_MAX))
        if negative:
            candidates.append(item)
    if not candidates:
        return None
    latest = max((_date_text(item.get("notice_date")) or "")
                 for item in candidates)
    rows = [item for item in candidates
            if _date_text(item.get("notice_date")) == latest]
    primary = next(
        (item for item in rows
         if "归母" in (_text(item.get("indicator")) or "")), rows[0])
    forecast_type = _text(primary.get("forecast_type")) or "业绩预告"
    indicator = _text(primary.get("indicator")) or "业绩"
    detail = _fmt_amount_span(primary) or _fmt_change_span(primary)
    text = f"{indicator} {detail}" if detail else indicator
    return f"⚠️ 业绩预告：{forecast_type}（{text}，{latest} 公告）"


def _pledge_line(events: Dict[str, Any]) -> Optional[str]:
    """最新中国结算统计日的质押比例；达到阈值才报。"""
    section = _dict(events.get("pledge"))
    if section.get("available") is not True:
        return None
    items = section.get("items") or []
    if not items:
        return None
    item = _dict(items[0])
    ratio = _num(item.get("ratio_pct"))
    if ratio is None or ratio < PLEDGE_RATIO_PCT_MIN:
        return None
    day = _date_text(item.get("date"))
    suffix = f"（{day} 中国结算）" if day else "（中国结算）"
    return f"⚠️ 股权质押：质押比例 {_fmt_event_pct(ratio)}{suffix}"


def _events_lines(payload: Dict[str, Any]) -> List[str]:
    """``evidence.events`` → 至多 1 条过期提示 + 每类至多 1 行事件。

    整块缺失、基金 / 回放口径、或没有任何达阈值事件时返回 ``[]``。
    """
    events = _dict(_dict(payload.get("evidence")).get("events"))
    if not events:
        return []
    lines: List[str] = []
    for builder in (_events_missing_line, _events_suspect_line,
                    _events_stale_line, _events_coverage_line):
        line = builder(events)
        if line:
            lines.append(line)
    for builder in (_lockup_line, _holder_trade_line,
                    _forecast_line, _pledge_line):
        line = builder(events)
        if line:
            lines.append(line)
    return lines


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------
def render_advice(payload: dict, compact: bool = False) -> str:
    """把 ``cli.py --json`` 的完整响应渲染成一段可直接投递的文本。

    纯函数：不改入参、不读文件、不触网。缺失字段对应的句子/段落整体消失，
    输出中不会出现 ``None``。

    ``compact=True`` 只处理 ``diagnostics.warnings``：先整段删除（URL /
    堆栈回溯段 / 配对完整的内部引用括号），再按封闭词表
    :data:`COMPACT_GLOSSARY` 做固定替换（词边界匹配、必要时吸收中文间的
    空格）；其余行与默认输出完全一致。整条 warning 都是内部细节时该行整体
    消失，绝不改写词表之外的原措辞。不变量见模块 docstring。
    """
    document = _dict(payload)
    rec = _dict(document.get("recommendation"))
    inst = _dict(document.get("instrument"))

    lines: List[str] = []

    # T2（前置）：数据问题必须先于建议报告。
    for warning in _warnings(document):
        shown = _compact_warning(warning) if compact else warning
        # 整条都是内部细节（如纯堆栈）时整行不出现；空串仍是原文的子序列。
        if shown:
            lines.append(f"⚠️ 数据: {shown}")
    if lines:
        lines.append("")

    # T2：可执行性闸门是建议块的首行。
    gate = _gate_reason(document)
    if gate:
        lines.append(f"【今日不建议执行】{gate}")

    # T6：硬风险告警 —— 放在可执行性闸门之后、价格/建议行之前；未命中整行消失。
    market_watch = _market_watch_line(document)
    if market_watch:
        lines.append(market_watch)

    # T7：事件风险（解禁 / 增减持 / 预告 / 质押）—— 每类至多一行；
    # 取数失败 / 过期时先出「事件数据过期（{D}）」，绝不写成「无事件」。
    for event_line in _events_lines(document):
        lines.append(event_line)

    lines.append(_price_line(document))

    action = _action_line(rec)
    if action:
        lines.append(action)

    position = _position_line(rec)
    if position:
        lines.append(position)

    # T1：触发事实放在建议行之后、依据行之前；无已触发信号则整行消失。
    trigger = _trigger_line(document)
    if trigger:
        lines.append(trigger)

    note = _status_note(inst)
    if note:
        lines.append(note)

    st_note = _st_note(inst)
    if st_note:
        lines.append(st_note)

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
