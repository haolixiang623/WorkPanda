"""请求主体与匿名会话身份。

当前阶段没有真实登录体系，用签名会话令牌代表"某个群众的一次会话"。
令牌带签名，客户端无法通过改 ID 冒充他人；但它不是实名身份，不能作为办事资格依据。

主体分四类（`PrincipalKind`），**四类通道凭据互不认账**：会话令牌只认
`X-WorkPanda-Session`，运维/管理/工作人员凭据各自只认自己的请求头，任何一类凭据
放进别的通道一律 401。没有"任一通过即放行"的分支——那种分支一旦出现，最弱的
那一类凭据就会成为整个系统实际的安全上限。
"""

import base64
import hashlib
import hmac
import secrets
import time
from enum import StrEnum

from pydantic import BaseModel, Field

SESSION_TTL_SECONDS = 12 * 3600


class PrincipalKind(StrEnum):
    """主体类型。与通道一一对应，不做跨通道的类型转换。"""

    # 匿名签名会话。不是实名身份，不能作为办事资格依据。
    CITIZEN = "citizen"
    # 工作人员：持 staff 凭据，权限来自工作人员目录里的流程授权。
    STAFF = "staff"
    # 运维：持 ops 凭据，只读任务与留痕。
    OPS = "ops"
    # 管理：持 admin 凭据，可改流程语义。
    ADMIN = "admin"


class Principal(BaseModel):
    """请求主体。`subject` 用于任务归属和确认凭证绑定。

    `kind` 与 `roles` 有默认值，是为了让"匿名会话"这个最常见的主体保持一行构造；
    需要区分的调用点必须显式传 `kind`，不要靠默认值猜——默认成 citizen 的
    staff 主体会被当成群众，而那种错误不会报错，只会让权限静默失效。
    """

    subject: str = Field(min_length=1, max_length=128)
    kind: PrincipalKind = PrincipalKind.CITIZEN
    # 角色/授权的字符串视图。判定权限时不要用它做前缀匹配，用各自主体的
    # `allows()`（例如 `StaffMember.allows`）。
    roles: tuple[str, ...] = ()
    authenticated: bool = False


class InvalidSession(ValueError):
    """会话令牌缺失、被篡改或已过期。"""


class SessionSigner:
    def __init__(self, secret: str | bytes, ttl_seconds: int = SESSION_TTL_SECONDS) -> None:
        raw = secret.encode("utf-8") if isinstance(secret, str) else secret
        if len(raw) < 16:
            raise ValueError("session secret must be at least 16 bytes")
        self._secret = raw
        self.ttl_seconds = ttl_seconds

    def issue(self) -> str:
        session_id = secrets.token_urlsafe(16)
        issued_at = int(time.time())
        return f"{session_id}.{issued_at}.{self._sign(session_id, issued_at)}"

    def verify(self, token: str) -> Principal:
        parts = token.split(".")
        if len(parts) != 3:
            raise InvalidSession("malformed session token")
        session_id, issued_raw, signature = parts
        try:
            issued_at = int(issued_raw)
        except ValueError as exc:
            raise InvalidSession("malformed session token") from exc

        if not hmac.compare_digest(signature, self._sign(session_id, issued_at)):
            raise InvalidSession("session signature mismatch")
        if time.time() - issued_at > self.ttl_seconds:
            raise InvalidSession("session expired")
        return Principal(subject=session_id, kind=PrincipalKind.CITIZEN)

    def _sign(self, session_id: str, issued_at: int) -> str:
        digest = hmac.new(
            self._secret, f"{session_id}.{issued_at}".encode(), hashlib.sha256
        ).digest()
        return base64.urlsafe_b64encode(digest).decode().rstrip("=")


def new_secret() -> str:
    """进程级随机密钥：未显式配置时使用，意味着重启后旧会话失效。"""

    return secrets.token_urlsafe(32)
