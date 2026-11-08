"""配置层：回环约束、端口互斥、开关取值。

这些断言看起来琐碎，但它们钉的是同一件事——模拟站的启动参数没有「差不多」的余地。
配错就起不来，比起起来之后把假的登录页挂到网络上要安全得多。
"""

from __future__ import annotations

import pytest
from mock_site.config import DEFAULT_ACCOUNTS, MockConfig, MockConfigError


def test_defaults_are_two_distinct_loopback_origins() -> None:
    config = MockConfig()
    assert config.bind_host == "127.0.0.1"
    assert config.account_origin == "http://127.0.0.1:8101"
    assert config.apply_origin == "http://127.0.0.1:8102"
    assert config.account_origin != config.apply_origin
    assert config.accounts == DEFAULT_ACCOUNTS


@pytest.mark.parametrize("host", ["0.0.0.0", "192.168.1.10", "example.com", ""])
def test_non_loopback_bind_is_refused(host: str) -> None:
    with pytest.raises(MockConfigError, match="回环"):
        MockConfig(bind_host=host)


def test_same_port_is_refused() -> None:
    with pytest.raises(MockConfigError, match="必须不同"):
        MockConfig(account_port=8101, apply_port=8101)


@pytest.mark.parametrize("port", [0, 65536, -1])
def test_port_out_of_range_is_refused(port: int) -> None:
    with pytest.raises(MockConfigError, match="1-65535"):
        MockConfig(account_port=port)


def test_attachment_bounds_must_be_sane() -> None:
    with pytest.raises(MockConfigError, match="min_attachments"):
        MockConfig(min_attachments=6, max_attachments=5)


def test_submit_mode_must_be_known() -> None:
    with pytest.raises(MockConfigError, match="submit_mode"):
        MockConfig(submit_mode="maybe")


def test_from_env_reads_all_switches() -> None:
    config = MockConfig.from_env(
        {
            "MOCK_SITE_ACCOUNT_PORT": "9101",
            "MOCK_SITE_APPLY_PORT": "9102",
            "MOCK_SITE_QR_TTL_SECONDS": "1.5",
            "MOCK_SITE_CAPTCHA_MAX_FAILURES": "1",
            "MOCK_SITE_MAX_UPLOAD_BYTES": "1024",
            "MOCK_SITE_SUBMIT_MODE": "no_response",
            "MOCK_SITE_ACCOUNTS": "alice:pw1,bob:pw2",
            "MOCK_SITE_ALLOWED_UPLOAD_TYPES": "application/pdf",
        }
    )
    assert config.account_port == 9101
    assert config.apply_port == 9102
    assert config.qr_ttl_seconds == 1.5
    assert config.captcha_max_failures == 1
    assert config.max_upload_bytes == 1024
    assert config.submit_mode == "no_response"
    assert config.accounts == {"alice": "pw1", "bob": "pw2"}
    assert config.allowed_upload_types == ("application/pdf",)


def test_from_env_ignores_blank_values() -> None:
    config = MockConfig.from_env({"MOCK_SITE_ACCOUNT_PORT": "  ", "MOCK_SITE_ACCOUNTS": ""})
    assert config.account_port == 8101
    assert config.accounts == DEFAULT_ACCOUNTS


@pytest.mark.parametrize(
    ("environ", "message"),
    [
        ({"MOCK_SITE_ACCOUNT_PORT": "abc"}, "MOCK_SITE_ACCOUNT_PORT"),
        ({"MOCK_SITE_QR_TTL_SECONDS": "late"}, "MOCK_SITE_QR_TTL_SECONDS"),
        ({"MOCK_SITE_ACCOUNTS": "nopassword"}, "用户名:密码"),
        ({"MOCK_SITE_SUBMIT_MODE": "sometimes"}, "submit_mode"),
    ],
)
def test_from_env_rejects_bad_values(environ: dict[str, str], message: str) -> None:
    with pytest.raises(MockConfigError, match=message):
        MockConfig.from_env(environ)
