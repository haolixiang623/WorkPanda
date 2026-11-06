"""脱敏：审计记录、任务事件与运维接口里不应该出现群众敏感信息。

两道防线：

1. **按键名**：`password`、`sms_code`、`id_card`、`token` 一类字段整体替换成掩码，
   工具还可以在 `ToolSpec.sensitive_fields` 里声明自己的敏感参数名；
2. **按文本模式**：手机号、身份证、银行卡、Bearer 令牌、常见云厂商密钥格式，
   兜住"被拼进自由文本"的情况（模型原文、工具返回值都可能这样）。

边界：脱敏只发生在**落盘/对外**的一侧。回灌给模型的工具结果保持原样，
否则模型会拿到被掩码的值继续推理，反而把任务做错。
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping
from typing import Any

MASK = "***"

# 键名精确命中即整体屏蔽（归一化后比较，忽略大小写与分隔符）。
DEFAULT_SENSITIVE_KEYS: frozenset[str] = frozenset(
    {
        "password",
        "passwd",
        "pwd",
        "secret",
        "token",
        "accesstoken",
        "refreshtoken",
        "apikey",
        "smscode",
        "otp",
        "verifycode",
        "verificationcode",
        "captcha",
        "idcard",
        "idnumber",
        "identityno",
        "bankcard",
        "cardno",
        "cvv",
        "cvc",
        "pin",
        "authorization",
        "cookie",
        "session",
    }
)

# 键名包含以下片段即视为敏感（例如 `user_password_hint`、`bank_card_no`）。
_SUBSTRING_HINTS: tuple[str, ...] = (
    "password",
    "passwd",
    "secret",
    "token",
    "apikey",
    "api_key",
    "smscode",
    "sms_code",
    "verifycode",
    "verify_code",
    "idcard",
    "id_card",
    "identityno",
    "bankcard",
    "bank_card",
)

_ID_CARD = re.compile(r"(?<!\d)(\d{6})\d{8}(\d{3}[\dXx])(?!\d)")
_MOBILE = re.compile(r"(?<!\d)(1[3-9]\d)\d{4}(\d{4})(?!\d)")
_BANK_CARD = re.compile(r"(?<!\d)(\d{4})\d{8,11}(\d{4})(?!\d)")
_BEARER = re.compile(r"(?i)\b(bearer\s+)[A-Za-z0-9._\-]{8,}")
_OPENAI_KEY = re.compile(r"\bsk-[A-Za-z0-9._\-]{16,}")
_GITHUB_TOKEN = re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}")
_AWS_KEY = re.compile(r"\bAKIA[0-9A-Z]{16}\b")


def normalize_key(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", name.lower())


def is_sensitive_key(name: str) -> bool:
    normalized = normalize_key(name)
    if not normalized:
        return False
    if normalized in DEFAULT_SENSITIVE_KEYS:
        return True
    return any(hint in normalized for hint in _SUBSTRING_HINTS)


def redact_text(text: str) -> str:
    """掩码自由文本中的个人标识与凭据，保留可读的前后片段。"""

    text = _ID_CARD.sub(lambda match: f"{match.group(1)}{'*' * 8}{match.group(2)}", text)
    text = _MOBILE.sub(lambda match: f"{match.group(1)}****{match.group(2)}", text)
    text = _BANK_CARD.sub(lambda match: f"{match.group(1)}********{match.group(2)}", text)
    text = _BEARER.sub(lambda match: f"{match.group(1)}{MASK}", text)
    text = _OPENAI_KEY.sub(MASK, text)
    text = _GITHUB_TOKEN.sub(MASK, text)
    text = _AWS_KEY.sub(MASK, text)
    return text


def mask_values(text: str, values: Iterable[str], *, min_length: int = 4) -> str:
    """把具体的敏感值从文本里抹掉。

    按键名只能遮住参数本身；如果工具把这个值原样返回（例如回显令牌），
    返回值里仍会出现明文。这里用"已知敏感值"再兜一道。
    长度太短的值不做替换，避免把正常文本拆碎。
    """

    candidates = {str(value) for value in values if len(str(value)) >= min_length}
    for value in sorted(candidates, key=len, reverse=True):
        text = text.replace(value, MASK)
    return text


class Redactor:
    """可组合的脱敏器。`extra_keys` 用于叠加工具声明的敏感参数名。"""

    def __init__(
        self,
        *,
        sensitive_keys: Iterable[str] = (),
        mask_text: bool = True,
        enabled: bool = True,
    ) -> None:
        self._extra_keys = {normalize_key(key) for key in sensitive_keys}
        self._mask_text = mask_text
        self._enabled = enabled

    @property
    def mask_text(self) -> bool:
        return self._mask_text

    @property
    def enabled(self) -> bool:
        return self._enabled

    def with_extra(self, keys: Iterable[str]) -> Redactor:
        return Redactor(
            sensitive_keys=self._extra_keys | {normalize_key(key) for key in keys},
            mask_text=self._mask_text,
            enabled=self._enabled,
        )

    def is_sensitive(self, key: str) -> bool:
        normalized = normalize_key(key)
        return normalized in self._extra_keys or is_sensitive_key(key)

    def redact(self, value: Any) -> Any:
        return self._redact(value, key=None)

    def preview(self, value: Any, *, limit: int = 240) -> str:
        """审计用的一行摘要：先脱敏再截断。"""

        try:
            text = json.dumps(self.redact(value), ensure_ascii=False, sort_keys=True, default=str)
        except (TypeError, ValueError):
            text = redact_text(str(value))
        return text if len(text) <= limit else text[:limit] + "…"

    def _redact(self, value: Any, key: str | None) -> Any:
        if not self._enabled:
            return value
        if key is not None and self.is_sensitive(key):
            return MASK
        if isinstance(value, Mapping):
            return {
                str(item_key): self._redact(item, str(item_key))
                for item_key, item in value.items()
            }
        if isinstance(value, (list, tuple, set)):
            return [self._redact(item, None) for item in value]
        if isinstance(value, str):
            return redact_text(value) if self._mask_text else value
        if isinstance(value, (int, float, bool)) or value is None:
            return value
        return redact_text(str(value)) if self._mask_text else str(value)


DEFAULT_REDACTOR = Redactor()

# 显式关闭脱敏时使用：仍然保持"返回可安全嵌入的新结构"这一语义。
NO_REDACTION = Redactor(enabled=False)
