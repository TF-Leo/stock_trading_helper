# -*- coding: utf-8 -*-
"""requirements.txt 求值器（P2 / issue #31）。

**为什么不用 ``packaging``**：主 ``source/requirements.txt`` 里没有它 ——
本机实测 ``packaging`` 只是 **pytest 的传递依赖**。issue #31 的范围明确写着
「不改 requirements」，而门禁探针必须在**只装了 requirements.txt 的环境**里
也能跑（否则 requirements 检查自己就会因为缺依赖而误报）。因此这里用标准库
实现「环境 marker + 版本区间」这两个子集，行为与 ``packaging`` 对齐（测试里
用 ``packaging`` 交叉验证同一批用例）。

历史事故（V9 冒烟）：旧实现 ``^([\\w-]+)==([\\w.]+)`` 把所有 ``==`` 行当硬 pin，
``requirements.txt`` 里按 Python 版本分档的两行
``numpy==2.2.6; python_version < "3.14"`` 与
``numpy>=2.3.3,<3; python_version >= "3.14"`` 被压成 dict 里的**后写覆盖前写**，
只剩 ``numpy==2.2.6`` 一条硬 pin；3.14 机器实装 2.3.5/2.5.4 → 假 FAIL，
从 d4264a4 起长期掩盖真实依赖问题。

支持的语法（requirements.txt 实际用到的子集）：
  * 行尾注释 ``# ...``、空行；
  * 包名 + 可选 extras ``pkg[extra1,extra2]``；
  * 版本说明符 ``==`` ``!=`` ``>=`` ``<=`` ``>`` ``<`` ``~=``（逗号分隔可组合）；
  * 环境 marker ``; <expr>``，支持 ``and`` / ``or`` / 括号 / ``not``，
    比较变量 ``python_version`` / ``python_full_version`` / ``platform_machine``
    / ``sys_platform`` / ``platform_system`` / ``os_name`` / ``implementation_name``，
    以及 ``in`` / ``not in``。
遇到**无法解析**的行一律抛 ``RequirementsParseError``，由调用方记成 FAIL ——
「看不懂就当通过」会让这道门禁失去意义（本项目已因类似做法吃过亏）。
"""
from __future__ import annotations

import os
import platform
import re
import sys


class RequirementsParseError(ValueError):
    """requirements 行无法解析（或引用了不支持的语法）。"""


# ---------------------------------------------------------------------------
# 版本比较
# ---------------------------------------------------------------------------
_VERSION_RE = re.compile(
    r'^\s*v?(?P<epoch>\d+!)?'
    r'(?P<release>\d+(?:\.\d+)*)'
    r'(?P<pre>[-_.]?(?:a|b|c|rc|alpha|beta|pre|preview)[-_.]?\d*)?'
    r'(?P<post>[-_.]?(?:post|rev|r)[-_.]?\d*|-\.\d+)?'
    r'(?P<dev>[-_.]?dev[-_.]?\d*)?'
    r'(?:\+(?P<local>[a-z0-9]+(?:[-_.][a-z0-9]+)*))?\s*$',
    re.IGNORECASE,
)

_PRE_ORDER = {'a': 0, 'alpha': 0, 'b': 1, 'beta': 1, 'c': 2, 'rc': 2,
              'pre': 2, 'preview': 2}


def _parse_version(text):
    """返回可比较的元组 ``(epoch, release, pre, post, dev, local)``。

    ``pre`` / ``post`` / ``dev`` 用 ``None`` 表示「无」，排序时把「无」
    放在最终版位置（``2.3.5 > 2.3.5rc1``），因此元组里用哨兵值区分。
    """
    match = _VERSION_RE.match(str(text))
    if not match:
        raise RequirementsParseError(f"无法解析版本号: {text!r}")
    epoch = int(match.group('epoch')[:-1]) if match.group('epoch') else 0
    release = tuple(int(x) for x in match.group('release').split('.'))

    pre = match.group('pre')
    if pre:
        label = re.match(r'[-_.]?([a-zA-Z]+)', pre).group(1).lower()
        num = re.search(r'(\d+)$', pre)
        pre = (_PRE_ORDER[label], int(num.group(1)) if num else 0)

    post = match.group('post')
    if post:
        num = re.search(r'(\d+)$', post)
        post = int(num.group(1)) if num else 0

    dev = match.group('dev')
    if dev:
        num = re.search(r'(\d+)$', dev)
        dev = int(num.group(1)) if num else 0

    local = match.group('local')
    local = tuple(local.lower().replace('-', '.').replace('_', '.').split('.')) \
        if local else None
    return (epoch, release, pre, post, dev, local)


def _release_key(release, width):
    """把 release 补零到等长，便于按元组逐位比较。"""
    return tuple(release) + (0,) * (width - len(release))


def _sort_key(parsed):
    """把 ``_parse_version`` 的结果转成可比较的排序键（PEP 440 顺序）。

    关键点：**dev 版本排在 pre 之前**，但仅限「既无 pre 也无 post」的情况。
    ``2.3.5.dev0`` 在 ``2.3.5rc1`` 之下；而 ``2.3.5.post1.dev2`` 有 post 段，
    它**高于** ``2.3.5``（PEP 440 的 pre 哨兵只在 post 也缺失时才取 -inf）。
    因此 pre 段用三档哨兵：dev-only=0 < pre=1 < 其余=2。
    """
    epoch, release, pre, post, dev, _local = parsed
    if pre is not None:
        pre_key = (1, pre)
    elif post is None and dev is not None:
        pre_key = (0,)
    else:
        pre_key = (2,)
    post_key = (0,) if post is None else (1, post)
    dev_key = (1,) if dev is None else (0, dev)
    return (epoch, release, pre_key, post_key, dev_key)


def _cmp_versions(left, right):
    """返回 -1 / 0 / 1。语义对齐 PEP 440 的子集（local 段不参与比较）。"""
    a, b = _sort_key(_parse_version(left)), _sort_key(_parse_version(right))
    width = max(len(a[1]), len(b[1]))
    a = (a[0], _release_key(a[1], width), a[2], a[3], a[4])
    b = (b[0], _release_key(b[1], width), b[2], b[3], b[4])
    if a == b:
        return 0
    return -1 if a < b else 1


def _is_version(text):
    try:
        _parse_version(text)
        return True
    except RequirementsParseError:
        return False


def _wildcard_match(have, want):
    """``==2.3.*`` 语义：release 前缀匹配（``~=`` 也用同一套）。"""
    prefix = want[:-2]
    parts = [p for p in prefix.split('.') if p]
    if not parts:
        raise RequirementsParseError(f"通配说明符缺少前缀: {want!r}")
    release = _parse_version(have)[1]
    have_key = _release_key(release, len(parts))
    return have_key[:len(parts)] == tuple(int(p) for p in parts)


def _string_compare(op, have, want):
    """非版本操作数（如 ``sys_platform``）按普通字符串比较。

    对齐 PEP 508 实现（packaging）：说明符无法解析成版本约束时退回字符串运算，
    而不是抛错 —— 否则 ``sys_platform != "win32"`` 这种常见 marker 会解析失败。
    """
    have, want = str(have), str(want)
    if op == '==':
        return have == want
    if op == '!=':
        return have != want
    if op == '>=':
        return have >= want
    if op == '<=':
        return have <= want
    if op == '>':
        return have > want
    if op == '<':
        return have < want
    raise RequirementsParseError(f"不支持的比较运算符: {op!r}")


def _compare(op, have, want):
    """按说明符比较 ``have op want``。

    操作数**都是合法版本号**才走版本语义（PEP 440 子集）；
    否则退回字符串比较 —— 与 packaging 的 marker 求值一致。
    """
    have, want = str(have), str(want)
    if op in ('==', '!=') and want.endswith('.*'):
        if not _is_version(have):
            raise RequirementsParseError(f"无法解析版本号: {have!r}")
        matched = _wildcard_match(have, want)
        return matched if op == '==' else not matched
    if not (_is_version(have) and _is_version(want)):
        return _string_compare(op, have, want)
    if op == '==':
        return _cmp_versions(have, want) == 0
    if op == '!=':
        return _cmp_versions(have, want) != 0
    if op == '>=':
        return _cmp_versions(have, want) >= 0
    if op == '<=':
        return _cmp_versions(have, want) <= 0
    if op == '>':
        return _cmp_versions(have, want) > 0
    if op == '<':
        return _cmp_versions(have, want) < 0
    if op == '~=':
        # ~=X.Y.Z  ⇔  >= X.Y.Z, == X.Y.*
        parsed = _parse_version(want)
        release = parsed[1]
        if len(release) < 2:
            raise RequirementsParseError(f"~= 至少需要两段版本号: {want!r}")
        if _cmp_versions(have, want) < 0:
            return False
        prefix = release[:-1]
        have_key = _release_key(_parse_version(have)[1], len(prefix))
        return have_key[:len(prefix)] == prefix
    raise RequirementsParseError(f"不支持的比较运算符: {op!r}")


_SPEC_RE = re.compile(r'^\s*(~=|===|==|!=|<=|>=|<|>)\s*([^\s,]+)\s*$')


def parse_specifiers(text):
    """``">=2.3.3,<3"`` → ``[('>=', '2.3.3'), ('<', '3')]``。"""
    parts = [p for p in str(text).split(',') if p.strip()]
    out = []
    for part in parts:
        match = _SPEC_RE.match(part)
        if not match:
            raise RequirementsParseError(f"无法解析版本说明符: {part!r}")
        if match.group(1) == '===':
            raise RequirementsParseError("不支持 === 任意相等匹配（本仓库未使用）")
        out.append((match.group(1), match.group(2)))
    return out


def version_satisfies(have, specifiers):
    """``have`` 是否满足全部说明符（空说明符视为满足）。"""
    return all(_compare(op, have, want) for op, want in specifiers)


# ---------------------------------------------------------------------------
# 环境 marker
# ---------------------------------------------------------------------------
def _default_env():
    """当前解释器的 marker 变量（与 PEP 508 的取值一致）。"""
    impl = sys.implementation
    version_info = sys.version_info
    return {
        'python_version': f"{version_info.major}.{version_info.minor}",
        'python_full_version': platform.python_version(),
        'os_name': os.name,
        'sys_platform': sys.platform,
        'platform_machine': platform.machine(),
        'platform_system': platform.system(),
        'platform_release': platform.release(),
        'implementation_name': impl.name,
        'implementation_version': (
            f"{impl.version.major}.{impl.version.minor}.{impl.version.micro}"),
    }


_MARKER_TOKEN_RE = re.compile(r"""
    \s*(?:
        (?P<lparen>\() |
        (?P<rparen>\)) |
        (?P<op>===|==|!=|<=|>=|~=|<|>|not\s+in\b|in\b) |
        (?P<and>and\b) |
        (?P<or>or\b) |
        (?P<not>not\b) |
        (?P<string>'[^']*'|"[^"]*") |
        (?P<name>[A-Za-z_][A-Za-z0-9_.]*)
    )
""", re.VERBOSE)


def _tokenize_marker(text):
    tokens, pos = [], 0
    text = str(text)
    while pos < len(text):
        if text[pos].isspace():
            pos += 1
            continue
        match = _MARKER_TOKEN_RE.match(text, pos)
        if not match or match.end() == pos:
            raise RequirementsParseError(
                f"无法解析 marker（位置 {pos}）: {text!r}")
        pos = match.end()
        kind = match.lastgroup
        value = match.group(kind)
        if kind == 'op':
            value = re.sub(r'\s+', ' ', value)
        tokens.append((kind, value))
    return tokens


class _MarkerParser:
    """递归下降：``or`` > ``and`` > ``not`` > 比较。"""

    def __init__(self, tokens, env):
        self.tokens = tokens
        self.pos = 0
        self.env = env

    def _peek(self):
        return self.tokens[self.pos] if self.pos < len(self.tokens) else (None, None)

    def _next(self):
        token = self._peek()
        self.pos += 1
        return token

    def parse(self):
        value = self._parse_or()
        if self.pos != len(self.tokens):
            raise RequirementsParseError(
                f"marker 有多余内容: {self.tokens[self.pos:]}")
        return value

    def _parse_or(self):
        value = self._parse_and()
        while self._peek()[0] == 'or':
            self._next()
            value = self._parse_and() or value
        return value

    def _parse_and(self):
        value = self._parse_not()
        while self._peek()[0] == 'and':
            self._next()
            value = self._parse_not() and value
        return value

    def _parse_not(self):
        if self._peek()[0] == 'not':
            self._next()
            return not self._parse_not()
        return self._parse_atom()

    def _parse_atom(self):
        kind, value = self._peek()
        if kind == 'lparen':
            self._next()
            inner = self._parse_or()
            if self._next()[0] != 'rparen':
                raise RequirementsParseError("marker 括号不匹配")
            return inner
        return self._parse_comparison()

    def _operand(self):
        kind, value = self._next()
        if kind == 'string':
            return value[1:-1]
        if kind == 'name':
            if value not in self.env:
                raise RequirementsParseError(
                    f"marker 引用了未知变量: {value!r}（支持: {sorted(self.env)}）")
            return self.env[value]
        raise RequirementsParseError(f"marker 期望变量或字符串，收到 {value!r}")

    def _parse_comparison(self):
        left = self._operand()
        kind, op = self._next()
        if kind != 'op':
            raise RequirementsParseError(f"marker 期望比较运算符，收到 {op!r}")
        right = self._operand()
        if op == 'in':
            # PEP 508：`"x" in var` 是**子串**判断（`"linux" in sys_platform`）
            return str(left) in str(right)
        if op == 'not in':
            return str(left) not in str(right)
        return _compare(op, left, right)


def evaluate_marker(marker, env=None):
    """求值环境 marker（空 marker 视为 True）。"""
    if marker is None or not str(marker).strip():
        return True
    env = env or _default_env()
    return _MarkerParser(_tokenize_marker(marker), env).parse()


# ---------------------------------------------------------------------------
# requirements 行
# ---------------------------------------------------------------------------
_REQ_RE = re.compile(
    r'^\s*(?P<name>[A-Za-z0-9][A-Za-z0-9._-]*)'
    r'\s*(?:\[(?P<extras>[^\]]*)\])?'
    r'\s*(?P<spec>[^;#]*?)'
    r'\s*(?:;\s*(?P<marker>.+?))?\s*$'
)


def parse_requirement(line):
    """解析一行 → ``dict``；空行/注释返回 ``None``。

    返回 ``{'name', 'extras', 'specifiers', 'marker', 'raw'}``。
    """
    raw = str(line)
    # 去注释：`#` 起头，或前面是空白（避免把 URL fragment / 版本里的 # 当注释）
    text = re.split(r'(?:^|\s)#', raw, maxsplit=1)[0].strip()
    if not text:
        return None
    match = _REQ_RE.match(text)
    if not match:
        raise RequirementsParseError(f"无法解析 requirements 行: {raw!r}")
    spec_text = (match.group('spec') or '').strip()
    extras = [e.strip() for e in (match.group('extras') or '').split(',')
              if e.strip()]
    return {
        'name': match.group('name'),
        'extras': extras,
        'specifiers': parse_specifiers(spec_text),
        'marker': (match.group('marker') or '').strip() or None,
        'raw': raw.strip(),
    }


def parse_requirements(text):
    """把整份 requirements.txt 解析成需求列表（保持文件顺序）。"""
    out = []
    for line in str(text).splitlines():
        req = parse_requirement(line)
        if req is not None:
            out.append(req)
    return out


def check_environment(requirements, versions, env=None):
    """按当前环境求值。

    ``requirements``：``parse_requirements`` 的结果。
    ``versions``：``{包名: 已安装版本}``（由调用方查 importlib.metadata）。

    返回 ``(problems, observed)``：
      * marker 不适用当前解释器 → 该行**跳过**，记进 ``observed[...]['skipped']``；
      * 适用 → 用版本说明符判定（不再要求 ``==`` 硬相等）；
      * 包名大小写/``-``/``_`` 归一后再查已安装版本。
    """
    env = env or _default_env()
    problems = []
    observed = {}

    def _key(name):
        return re.sub(r'[-_.]+', '-', str(name)).lower()

    lookup = {_key(k): v for k, v in (versions or {}).items()}
    by_pkg = {}
    for req in requirements:
        pkg = _key(req['name'])
        entry = by_pkg.setdefault(pkg, {'lines': [], 'active': False,
                                        'skipped_markers': []})
        entry['lines'].append(req['raw'])
        try:
            applies = evaluate_marker(req['marker'], env)
        except RequirementsParseError as exc:
            problems.append(f"{req['name']} marker 无法解析: {exc}")
            continue
        if not applies:
            entry['skipped_markers'].append(req['marker'])
            continue
        entry['active'] = True
        have = lookup.get(pkg)
        if have is None:
            problems.append(f"{req['name']} 未安装（marker: {req['marker'] or '无'}）")
            continue
        try:
            ok = version_satisfies(have, req['specifiers'])
        except RequirementsParseError as exc:
            problems.append(f"{req['name']} 版本说明符无法解析: {exc}")
            continue
        if not ok:
            spec_text = ','.join(op + ver for op, ver in req['specifiers']) or '任意'
            problems.append(
                f"{req['name']} 要求 {spec_text}"
                f"{'（marker: ' + req['marker'] + '）' if req['marker'] else ''}"
                f" 实际={have}")

    for pkg, entry in by_pkg.items():
        observed[pkg] = {
            'requirements': entry['lines'],
            'active': entry['active'],
            'actual': lookup.get(pkg),
            'skipped_markers': entry['skipped_markers'],
        }
    return problems, observed
