# -*- coding: utf-8 -*-
"""P2（issue #26）：diagnostics 脱敏加固。

PR #22 的 ``_sanitize_error_text`` 覆盖了绝对 URL、相对 ``url:`` 与
``token=``/``api_key=``/``secret=``，但 V7（issue #23）实测仍有三处漏网：

1. ``Authorization: Bearer <token>`` —— 旧规则只吃掉 ``Bearer``，token 原样留存；
2. JSON 引号形式 ``"api_key": "abc"`` —— 冒号后的引号让 ``[^\\s,;&]+`` 匹配不上；
3. ``ftp://`` / ``ws://`` 等非 http(s) scheme —— 旧 URL 规则只认 ``https?://``。

另：整条 warning 原先无上限（error_text 200 + 前缀 + 冷却说明，实测最长 234 字），
多通道失败时会刷屏。现在有 ``MAX_WARNING_TEXT``。

本文件覆盖 **21 种**凭据形态（issue 要求 ≥10），逐条断言：
  * 秘密原文不出现；
  * 输出里没有残留的 scheme / 认证方案；
  * 长度受限（单条 error_text ≤ 200，整条 warning ≤ 300）。
"""
import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1]
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from stock_analysis.data_loader import bars  # noqa: E402


def _sanitize(text):
    return bars._sanitize_error_text(RuntimeError(text))


# (原始错误文本, 绝不允许出现在输出里的秘密片段)
SECRET_CASES = [
    # --- issue #26 第 1 条：认证方案后的凭据本体 ---
    ("Authorization: Bearer sk-live-9f8e7d6c5b4a", "sk-live-9f8e7d6c5b4a"),
    ("Authorization: Basic dXNlcjpwYXNzd29yZA==", "dXNlcjpwYXNzd29yZA=="),
    ("authorization: bearer tok_abc123xyz", "tok_abc123xyz"),
    ("Proxy-Authorization: Bearer proxy-secret-42", "proxy-secret-42"),
    ("authorization=Basic Zm9vOmJhcg==", "Zm9vOmJhcg=="),
    # --- issue #26 第 2 条：JSON 引号形式 ---
    ('{"api_key": "json-secret-a1b2c3"}', "json-secret-a1b2c3"),
    ('{"token":"json-secret-d4e5f6"}', "json-secret-d4e5f6"),
    ('{"secret_key": "json-secret-g7h8i9"}', "json-secret-g7h8i9"),
    ('{"password": "json-secret-j0k1l2"}', "json-secret-j0k1l2"),
    ("{'access_key': 'json-secret-m3n4o5'}", "json-secret-m3n4o5"),
    # --- issue #26 第 3 条：非 http(s) scheme ---
    ("ftp://user:ftp-pass-6p7q8r@host/path", "ftp-pass-6p7q8r"),
    ("ws://host/p?token=ws-secret-s9t0u1", "ws-secret-s9t0u1"),
    ("wss://host/p?api_key=wss-secret-v2w3x4", "wss-secret-v2w3x4"),
    ("postgres://u:pg-secret-y5z6a7@db/x", "pg-secret-y5z6a7"),
    ("s3://AKIA:aws-secret-b8c9d0@bucket/key", "aws-secret-b8c9d0"),
    # --- 既有规则不得回退 ---
    ("https://a.example/p?token=https-secret-e1f2g3", "https-secret-e1f2g3"),
    ("token=plain-secret-h4i5j6", "plain-secret-h4i5j6"),
    ("api_key: plain-secret-k7l8m9", "plain-secret-k7l8m9"),
    ("x-api-key: header-secret-n0o1p2", "header-secret-n0o1p2"),
    ("SECRET_KEY=env-secret-q3r4s5", "env-secret-q3r4s5"),
    ("Cookie: session=cookie-secret-t6u7v8", "cookie-secret-t6u7v8"),
]


@pytest.mark.parametrize("text,secret", SECRET_CASES)
def test_secret_never_leaks(text, secret):
    assert secret in text, "用例自身失效：秘密片段不在输入里"
    out = _sanitize(text)
    assert secret not in out, f"凭据泄露：{secret!r} 仍在 {out!r}"
    assert len(out) <= bars.MAX_ATTEMPT_ERROR_TEXT


@pytest.mark.parametrize("text,secret", SECRET_CASES)
def test_scheme_and_auth_header_are_removed(text, secret):
    """不只是「秘密字符串消失」：scheme / 认证方案本身也不能留存。"""
    out = _sanitize(text).lower()
    for marker in ("ftp://", "ws://", "wss://", "postgres://", "s3://"):
        assert marker not in out, f"{marker} 未脱敏: {out!r}"
    if "authorization" in text.lower():
        # 认证头保留键名 + <redacted>，但不得残留方案名后的任何内容
        assert "bearer " not in out and "basic " not in out, out


def test_case_count_meets_issue_requirement():
    """issue #26 明确要求 ≥10 种凭据形态；这里是 21 种。"""
    assert len(SECRET_CASES) >= 10
    assert len({c[1] for c in SECRET_CASES}) == len(SECRET_CASES), "秘密片段不得重复"


def test_redaction_marker_present():
    """脱敏后要留下可读标记，调用方知道这里被抹掉了什么。"""
    assert "<redacted>" in _sanitize("Authorization: Bearer abc")
    assert "<redacted>" in _sanitize('{"api_key": "abc"}')
    assert "<url>" in _sanitize("ftp://user:pw@host/x")


def test_key_name_is_preserved_for_diagnosis():
    """保留键名（token/api_key/...）与来源名，方便定位是哪类凭据。"""
    out = _sanitize("token=abc api_key=def password=ghi")
    for key in ("token", "api_key", "password"):
        assert key in out, f"键名 {key} 被误删: {out!r}"


def test_json_quotes_do_not_survive_around_redaction():
    """JSON 引号形式脱敏后不应残留成对引号包裹的空值（易被误读为「空凭据」）。"""
    out = _sanitize('{"api_key": "abc"}')
    assert '"abc"' not in out
    assert '""' not in out


def test_no_false_positive_on_plain_text():
    """普通错误文本不得被过度脱敏（误报会掩盖真实原因）。"""
    for text in (
        "返回空数据",
        "Read timed out after 30 seconds",
        "HTTP 429 too many requests",
        "Connection reset by peer",
        "600519 缓存文件不存在",
    ):
        assert _sanitize(text) == text, f"误脱敏: {_sanitize(text)!r}"


def test_error_text_length_cap_unchanged():
    out = _sanitize("x" * 500)
    assert len(out) == bars.MAX_ATTEMPT_ERROR_TEXT
    assert out.endswith("...")


# ---------------------------------------------------------------------------
# 整条 warning 的上限（issue #26 最后一段）
# ---------------------------------------------------------------------------
def test_warning_length_is_capped():
    attempts = [{
        "source": "astock_tencent", "ok": False, "error_class": "HTTPError",
        "error_text": "y" * bars.MAX_ATTEMPT_ERROR_TEXT,
        "cooldown": True, "cooldown_seconds": 120, "removed": True,
    }]
    line = bars.format_attempt_warnings(attempts)[0]
    assert len(line) <= bars.MAX_WARNING_TEXT
    # 截断必须保留来源名 + error_class + 摘要开头
    assert line.startswith("astock_tencent 失败：HTTPError ")
    assert "yyy" in line


def test_normal_warning_not_truncated():
    """常规长度（来源名 + 200 字 error_text）不该被截，避免无谓损失信息。"""
    attempts = [{
        "source": "eastmoney", "ok": False, "error_class": "ConnectionError",
        "error_text": "z" * 150, "cooldown": False, "removed": False,
    }]
    line = bars.format_attempt_warnings(attempts)[0]
    assert len(line) <= bars.MAX_WARNING_TEXT
    assert not line.endswith("...")
    assert line == "eastmoney 失败：ConnectionError " + "z" * 150


def test_warning_cap_applies_to_every_failure_line():
    """多通道失败时每一条都受限，而不只是第一条。"""
    attempts = [
        {"source": f"src_{i}", "ok": False, "error_class": "E",
         "error_text": "w" * 250, "cooldown": True, "cooldown_seconds": 60,
         "removed": False}
        for i in range(5)
    ]
    lines = bars.format_attempt_warnings(attempts)
    assert len(lines) == 5
    assert all(len(x) <= bars.MAX_WARNING_TEXT for x in lines)
    assert [x.split(" 失败")[0] for x in lines] == [f"src_{i}" for i in range(5)]


def test_skip_lines_are_not_capped_away():
    """「冷却中 / 已剔除」这类短行不受影响，语义完整。"""
    attempts = [
        {"source": "sina_stock", "ok": None, "error_class": None,
         "error_text": None, "cooldown": True, "removed": False},
        {"source": "eastmoney", "ok": None, "error_class": None,
         "error_text": None, "cooldown": False, "removed": True},
    ]
    lines = bars.format_attempt_warnings(attempts)
    assert lines == ["sina_stock 冷却中，本轮跳过", "eastmoney 已从渠道池剔除，本轮跳过"]
