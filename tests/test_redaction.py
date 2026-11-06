"""脱敏：按键名屏蔽敏感参数，按文本模式屏蔽个人标识与凭据。"""

from __future__ import annotations

import json

from workpanda.harness.redaction import (
    MASK,
    NO_REDACTION,
    Redactor,
    is_sensitive_key,
    mask_values,
    redact_text,
)


def test_sensitive_keys_are_recognized() -> None:
    assert is_sensitive_key("password")
    assert is_sensitive_key("api_key")
    assert is_sensitive_key("API-Key")
    assert is_sensitive_key("sms_code")
    assert is_sensitive_key("id_card_no")
    assert is_sensitive_key("Authorization")
    # 正常业务字段不能被误伤，否则留痕失去排障价值。
    assert not is_sensitive_key("order_id")
    assert not is_sensitive_key("subject")


def test_nested_structures_are_masked_by_key() -> None:
    redactor = Redactor()
    payload = {
        "user": {"name": "张三", "id_card": "110101199001011234"},
        "password": "p@ssw0rd",
        "items": [{"token": "abc"}, {"note": "正常"}],
    }

    masked = redactor.redact(payload)

    assert masked["password"] == MASK
    assert masked["user"]["id_card"] == MASK
    assert masked["items"][0]["token"] == MASK
    assert masked["user"]["name"] == "张三"
    assert masked["items"][1]["note"] == "正常"


def test_text_patterns_mask_personal_identifiers() -> None:
    text = (
        "手机 13812345678 身份证 110101199001011234 "
        "卡号 6222021234567890123 令牌 Bearer abcdefghijklmnop"
    )

    masked = redact_text(text)

    assert "13812345678" not in masked
    assert "138****5678" in masked
    assert "110101199001011234" not in masked
    assert "6222021234567890123" not in masked
    assert "abcdefghijklmnop" not in masked


def test_api_keys_are_masked() -> None:
    # 刻意在运行时拼接：仓库里不留形似真实凭据的字面量，否则会被密钥扫描门禁拦下。
    openai_like = "sk-" + "a" * 24
    github_like = "ghp_" + "b" * 24

    masked = redact_text(f"key={openai_like} token={github_like}")

    assert openai_like not in masked
    assert github_like not in masked
    assert MASK in masked


def test_extra_keys_can_be_added_per_tool() -> None:
    redactor = Redactor().with_extra(["callback_sign"])

    masked = redactor.redact({"callback_sign": "deadbeef", "amount": "100"})

    assert masked["callback_sign"] == MASK
    assert masked["amount"] == "100"


def test_disabled_redactor_changes_nothing() -> None:
    payload = {"password": "p@ssw0rd", "note": "电话 13812345678"}

    assert NO_REDACTION.redact(payload) == payload


def test_mask_values_removes_echoed_secret() -> None:
    text = "tool echo: tok-123456"

    assert "tok-123456" not in mask_values(text, ["tok-123456"])
    # 太短的值不替换，避免把正常文本拆碎。
    assert mask_values("abc", ["abc"]) == "abc"


def test_preview_is_masked_then_truncated() -> None:
    redactor = Redactor()

    preview = redactor.preview({"api_key": "p@ssw0rd", "blob": "x" * 100}, limit=80)

    assert MASK in preview
    assert "p@ssw0rd" not in preview
    assert len(preview) <= 81
    json.loads('{"ok": true}')
