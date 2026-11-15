"""敏感运行数据脱敏。

约束：运行日志、错误信息、失败证据会被采集到集中日志平台，且经常被贴进工单。
因此**任何可能含业务数据的字符串在离开进程前都要过一遍**：

  * 键名命中敏感名单的字段打码（输入框里填的身份证、账号、手机号等）；
  * 值本身是长数字串、邮箱、证件号形态的，按正则打码；
  * URL 里的 query 参数按敏感键名打码；
  * 分隔符 `value`（形如 `input#uscc` → 值）中值部分打码。

脱敏只做"不可逆打码"，不保留可解密副本——需要复用敏感值时从上游系统取，
不在 RPA 运行时落盘。
"""

from __future__ import annotations

import re
from typing import Any

MASK = "***"

# 长数字串（>=11 位，去掉分隔符后），覆盖手机号、证件号、银行卡号。
_LONG_DIGITS = re.compile(r"\b([0-9][0-9\-_. ]{9,}[0-9])\b")
# 邮箱：local part 可能是中文或任意 Unicode，不能用 \b + ASCII 字符类。
_EMAIL = re.compile(r"[^\s@<>\"'，。；、]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_USCC = re.compile(r"\b(?:91|92|93)[0-9A-Z]{15,17}\b")
_QUERY_PARAM = re.compile(r"(?P<key>[?&](?P<name>[A-Za-z0-9_\-]+))=([^&#\s]*)")
# `input#uscc` / `uscc: xxx` 这类"键 # 值"对里，值部打码。
_KEY_VALUE_PAIR = re.compile(r"(?P<key>#?[A-Za-z0-9_\-\.]+)[=:]\s*(?P<value>[^,;，；\n]*)")

_SENSITIVE_HINTS = (
    "password",
    "passwd",
    "secret",
    "token",
    "authorization",
    "cookie",
    "card",
    "account",
    "phone",
    "mobile",
    "email",
    "uscc",
    "id_card",
    "id_number",
    "bank",
    "身份证",
    "账号",
    "手机",
    "邮箱",
    "密码",
)


def is_sensitive_key(key: str) -> bool:
    low = key.lower()
    if any(hint in low for hint in _SENSITIVE_HINTS):
        return True
    # 允许通过 REDACT_KEYS / Settings.redact_keys 追加业务特有的敏感字段名。
    from .config import settings

    return any(hint and hint.lower() in low for hint in settings.redact_keys)


def redact_text(text: str) -> str:
    """对自由文本做形态脱敏。幂等：已打码的内容保持打码。"""
    if not text:
        return text
    text = _QUERY_PARAM.sub(
        lambda m: f"{m.group('key')}{m.group('name')}={MASK}"
        if is_sensitive_key(m.group("name"))
        else m.group(0),
        text,
    )

    def _pair(m: re.Match[str]) -> str:
        key = m.group("key").lstrip("#")
        if is_sensitive_key(key):
            sep = m.group(0)[len(m.group("key"))]  # '=' or ':' or '#'
            return f"{m.group('key')}{sep}{MASK}"
        return m.group(0)

    text = _KEY_VALUE_PAIR.sub(_pair, text)
    text = _EMAIL.sub(MASK, text)
    text = _USCC.sub(MASK, text)
    text = _LONG_DIGITS.sub(MASK, text)
    return text


def redact_value(value: Any, key: str = "", depth: int = 0) -> Any:
    """结构化数据的脱敏。容器递归处理；敏感键直接打码。"""
    if depth > 6:
        return MASK
    if isinstance(value, dict):
        return {
            k: (MASK if is_sensitive_key(str(k)) else redact_value(v, str(k), depth + 1))
            for k, v in value.items()
        }
    if isinstance(value, (list, tuple, set)):
        return [redact_value(v, key, depth + 1) for v in value]
    if isinstance(value, str):
        return MASK if is_sensitive_key(key) else redact_text(value)
    return value
