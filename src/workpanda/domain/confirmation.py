"""服务端一次性确认凭证。

关键动作不能只靠提示词或按钮样式控制。凭证绑定任务、流程版本、步骤和参数摘要，
在工具执行前由服务端重新校验并一次性消费；参数变化后旧凭证自动失效。
"""

import hashlib
import hmac
import json
import secrets
from datetime import datetime, timedelta
from uuid import UUID

from pydantic import BaseModel, Field

from workpanda.domain.models import utc_now


def parameter_digest(
    flow_id: str, flow_version: str, step_id: str, arguments: dict[str, str]
) -> str:
    """对影响副作用的参数做稳定摘要，用于绑定确认与执行。"""

    payload = json.dumps(
        {
            "flow_id": flow_id,
            "flow_version": flow_version,
            "step_id": step_id,
            "arguments": arguments,
        },
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class Confirmation(BaseModel):
    token: str = Field(default_factory=lambda: secrets.token_urlsafe(32))
    task_id: UUID
    step_id: str
    flow_version: str
    parameter_digest: str
    user_ref: str | None = None
    issued_at: datetime = Field(default_factory=utc_now)
    expires_at: datetime
    consumed_at: datetime | None = None

    @classmethod
    def issue(
        cls,
        *,
        task_id: UUID,
        step_id: str,
        flow_version: str,
        digest: str,
        user_ref: str | None,
        ttl_seconds: int,
    ) -> "Confirmation":
        now = utc_now()
        return cls(
            task_id=task_id,
            step_id=step_id,
            flow_version=flow_version,
            parameter_digest=digest,
            user_ref=user_ref,
            issued_at=now,
            expires_at=now + timedelta(seconds=ttl_seconds),
        )

    def is_usable(self, *, digest: str, flow_version: str, user_ref: str | None) -> bool:
        return (
            self.consumed_at is None
            and self.expires_at > utc_now()
            and hmac.compare_digest(self.parameter_digest, digest)
            and self.flow_version == flow_version
            and self.user_ref == user_ref
        )


class ConfirmationError(ValueError):
    """确认凭证不可用。"""
