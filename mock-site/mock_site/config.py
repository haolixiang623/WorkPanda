"""模拟站配置：只读环境变量，不读文件，也不碰产品配置。

模拟站要能被单独拷出去跑——P2 的浏览器执行器不该因为 `WORKPANDA_*` 没配好就起不来。
所以这里刻意不用 pydantic-settings，也不加载任何 `.env`：配置只有 `MOCK_SITE_` 前缀的
环境变量，出厂值就是能跑的值。所有开关都在 `mock-site/README.md` 的表里。

绑定地址只接受回环。这不是「默认值」而是硬约束——模拟站里放着假的登录态和假的材料，
它一旦被挂到网络上，就会有人真的往里填东西。
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field

LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})
SUBMIT_MODES = ("normal", "slow", "no_response", "error_but_accepted")
DEFAULT_ACCOUNTS: dict[str, str] = {"demo": "demo1234"}
DEFAULT_ALLOWED_UPLOAD_TYPES = ("application/pdf", "image/jpeg", "image/png")


class MockConfigError(ValueError):
    """配置不满足模拟站的取值或安全前提。启动即失败，不静默降级。"""


def _env_str(env: Mapping[str, str], key: str, default: str) -> str:
    value = env.get(key)
    return default if value is None or not value.strip() else value.strip()


def _env_int(env: Mapping[str, str], key: str, default: int) -> int:
    raw = env.get(key)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw.strip())
    except ValueError as exc:
        raise MockConfigError(f"{key} 必须是整数，实际是 {raw!r}") from exc


def _env_float(env: Mapping[str, str], key: str, default: float) -> float:
    raw = env.get(key)
    if raw is None or not raw.strip():
        return default
    try:
        return float(raw.strip())
    except ValueError as exc:
        raise MockConfigError(f"{key} 必须是数字，实际是 {raw!r}") from exc


def _parse_accounts(raw: str) -> dict[str, str]:
    """`demo:demo1234,probe:probe1234` → 账号表。密码里允许再出现冒号。"""

    accounts: dict[str, str] = {}
    for item in raw.split(","):
        entry = item.strip()
        if not entry:
            continue
        username, sep, password = entry.partition(":")
        if not sep or not username.strip() or not password:
            raise MockConfigError(
                f"MOCK_SITE_ACCOUNTS 的每一项都必须是 用户名:密码，实际是 {entry!r}"
            )
        accounts[username.strip()] = password
    return accounts


@dataclass(frozen=True)
class MockConfig:
    """一份不可变的启动配置。运行期可变的开关在 `state.Switches` 里。"""

    bind_host: str = "127.0.0.1"
    account_port: int = 8101
    apply_port: int = 8102
    accounts: dict[str, str] = field(default_factory=lambda: dict(DEFAULT_ACCOUNTS))
    captcha_max_failures: int = 3
    qr_ttl_seconds: float = 90.0
    handoff_ttl_seconds: float = 120.0
    min_attachments: int = 2
    max_attachments: int = 5
    max_upload_bytes: int = 256 * 1024
    allowed_upload_types: tuple[str, ...] = DEFAULT_ALLOWED_UPLOAD_TYPES
    submit_mode: str = "normal"
    submit_delay_seconds: float = 1.5
    submit_hang_seconds: float = 30.0

    def __post_init__(self) -> None:
        host = self.bind_host.strip().lower()
        if host not in LOOPBACK_HOSTS:
            raise MockConfigError(
                f"bind_host 只能是回环地址（{', '.join(sorted(LOOPBACK_HOSTS))}），"
                f"实际是 {self.bind_host!r}；模拟站不对外暴露。"
            )
        for name in ("account_port", "apply_port"):
            port = getattr(self, name)
            if not 1 <= port <= 65535:
                raise MockConfigError(f"{name} 必须在 1-65535 之间，实际是 {port}")
        if self.account_port == self.apply_port:
            raise MockConfigError("account_port 与 apply_port 必须不同，否则构不出跨站")
        if self.captcha_max_failures < 1:
            raise MockConfigError("captcha_max_failures 至少为 1")
        if self.qr_ttl_seconds <= 0 or self.handoff_ttl_seconds <= 0:
            raise MockConfigError("qr_ttl_seconds 与 handoff_ttl_seconds 必须大于 0")
        if not 1 <= self.min_attachments <= self.max_attachments:
            raise MockConfigError("必须满足 1 <= min_attachments <= max_attachments")
        if self.max_upload_bytes <= 0:
            raise MockConfigError("max_upload_bytes 必须大于 0")
        if not self.allowed_upload_types:
            raise MockConfigError("allowed_upload_types 不能为空")
        if self.submit_mode not in SUBMIT_MODES:
            raise MockConfigError(
                f"submit_mode 只能是 {'/'.join(SUBMIT_MODES)}，实际是 {self.submit_mode!r}"
            )
        if self.submit_delay_seconds < 0 or self.submit_hang_seconds < 0:
            raise MockConfigError("submit_delay_seconds 与 submit_hang_seconds 不能为负")
        if not self.accounts:
            raise MockConfigError("accounts 不能为空，否则模拟站一进来就登不上")

    @property
    def account_origin(self) -> str:
        return f"http://{self.bind_host}:{self.account_port}"

    @property
    def apply_origin(self) -> str:
        return f"http://{self.bind_host}:{self.apply_port}"

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> MockConfig:
        env = os.environ if environ is None else environ
        accounts = _parse_accounts(env.get("MOCK_SITE_ACCOUNTS", ""))
        raw_types = env.get("MOCK_SITE_ALLOWED_UPLOAD_TYPES", "")
        allowed = tuple(item.strip() for item in raw_types.split(",") if item.strip())
        config = cls(
            bind_host=_env_str(env, "MOCK_SITE_BIND_HOST", "127.0.0.1"),
            account_port=_env_int(env, "MOCK_SITE_ACCOUNT_PORT", 8101),
            apply_port=_env_int(env, "MOCK_SITE_APPLY_PORT", 8102),
            accounts=accounts or dict(DEFAULT_ACCOUNTS),
            captcha_max_failures=_env_int(env, "MOCK_SITE_CAPTCHA_MAX_FAILURES", 3),
            qr_ttl_seconds=_env_float(env, "MOCK_SITE_QR_TTL_SECONDS", 90.0),
            handoff_ttl_seconds=_env_float(env, "MOCK_SITE_HANDOFF_TTL_SECONDS", 120.0),
            min_attachments=_env_int(env, "MOCK_SITE_MIN_ATTACHMENTS", 2),
            max_attachments=_env_int(env, "MOCK_SITE_MAX_ATTACHMENTS", 5),
            max_upload_bytes=_env_int(env, "MOCK_SITE_MAX_UPLOAD_BYTES", 256 * 1024),
            allowed_upload_types=allowed or DEFAULT_ALLOWED_UPLOAD_TYPES,
            submit_mode=_env_str(env, "MOCK_SITE_SUBMIT_MODE", "normal"),
            submit_delay_seconds=_env_float(env, "MOCK_SITE_SUBMIT_DELAY_SECONDS", 1.5),
            submit_hang_seconds=_env_float(env, "MOCK_SITE_SUBMIT_HANG_SECONDS", 30.0),
        )
        return config
