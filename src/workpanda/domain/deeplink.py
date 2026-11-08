"""确认深链：把浏览器「导航」到确认页的一次性凭据，**不是授权凭据**。

D3 红线：确认凭证不得由 IM 消息签发。IM 卡片里只放一个一次性深链，点开回到
Web 后仍走**既有**的确认流程——凭证由服务端在用户确认那一刻签发，绑定
`task + flow_version + step + 参数摘要 + user_ref + TTL`（`domain.confirmation`）。

这条边界能成立，靠的是深链**什么都不签发**：

1. 深链只携带 `task_id + step_id + flow_version`，落地接口只回答"该确认哪一步"，
   响应体里没有任何凭证字段；
2. 落地的前提仍然是任务所属会话（`task.user_ref`）。深链不能替代会话，更不能
   把任意浏览器的会话变成这张任务的会话；
3. 因此重放、过期、跨任务使用都只是"打不开"，不存在"打开就给了一次授权"。

一个副产物是"转发给别人"不再有收益：别人点开深链，登录的是他自己的会话，
`session_not_owner` 直接拒绝——这正是 IM 文本回复（"我也同意"）做不到的事。

TTL 刻意比确认凭证更短：深链只是一次导航，用户点开之后就应当立刻落地；留一个
可用一整天的深链，等于把"一次性"让步给"方便"。
"""

from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Protocol
from uuid import UUID

from pydantic import BaseModel, Field

from workpanda.domain.models import utc_now

# 深链有效期上限：超过这个值就不再是"短 TTL"，一次性语义会被浏览历史稀释。
MAX_TTL_SECONDS = 900


def token_fingerprint(token: str) -> str:
    """留痕用的令牌指纹：**留痕里绝不出现令牌原文**。

    深链虽然不构成授权，但它能定位到"某张任务的某一步"；把原文落进留痕表，
    等于给留痕表本身加了一份"可被点开的清单"。指纹足够回答"两次失败是不是
    同一个链接"，却不还原任何可用值。

    深链表本身仍以令牌为查找键（否则无从校验），这是它和留痕的区别：那张表
    只被落地接口读，且一张卡的寿命不超过 `MAX_TTL_SECONDS`。
    """

    return hashlib.sha256(token.encode("utf-8")).hexdigest()[:12]


class ConfirmationLink(BaseModel):
    """一张一次性深链。`consumed_at` 一旦写入即不可再用（重放一律拒绝）。"""

    token: str = Field(default_factory=lambda: secrets.token_urlsafe(32))
    task_id: UUID
    step_id: str = Field(min_length=1, max_length=128)
    flow_version: str = Field(min_length=1, max_length=64)
    # 发卡人（工作人员主体）。留痕要能回答"这张卡是谁发出去的"。
    issued_by: str = Field(default="", max_length=64)
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
        issued_by: str,
        ttl_seconds: int,
    ) -> ConfirmationLink:
        if ttl_seconds <= 0 or ttl_seconds > MAX_TTL_SECONDS:
            raise ValueError(f"深链有效期必须在 1..{MAX_TTL_SECONDS} 秒之间：{ttl_seconds}")
        now = utc_now()
        return cls(
            task_id=task_id,
            step_id=step_id,
            flow_version=flow_version,
            issued_by=issued_by,
            issued_at=now,
            expires_at=now + timedelta(seconds=ttl_seconds),
        )

    def is_expired(self, now: datetime | None = None) -> bool:
        return self.expires_at <= (now or utc_now())

    @property
    def is_consumed(self) -> bool:
        return self.consumed_at is not None

    def fingerprint(self) -> str:
        return token_fingerprint(self.token)


class ConfirmationLinkRejection(StrEnum):
    """落地失败的分档。**只进留痕，不原样回给调用方。**

    对外统一回 `confirmation_link_invalid`：如果"链接有效但你不是这张任务的
    会话"能单独区分出来，就等于告诉持卡人"这张卡是真的，去搞到那个会话"。
    分档留给运维查——它要回答的是"为什么这个人打不开"。
    """

    UNKNOWN = "link_unknown"
    EXPIRED = "link_expired"
    ALREADY_USED = "link_already_used"
    TASK_MISMATCH = "link_task_mismatch"
    STEP_CHANGED = "link_step_changed"
    SESSION_NOT_OWNER = "link_session_not_owner"
    PRINCIPAL_NOT_CITIZEN = "link_principal_not_citizen"
    TASK_NOT_CONFIRMABLE = "link_task_not_confirmable"


class ConfirmationLinkRejected(Exception):
    """深链不可用。调用方不得把它当成"再来一次就成功"。"""

    def __init__(self, reason: ConfirmationLinkRejection) -> None:
        super().__init__(reason.value)
        self.reason = reason


class ConfirmationLinkOutcome(StrEnum):
    ISSUED = "issued"
    REDEEMED = "redeemed"
    REJECTED = "rejected"


class ConfirmationLinkAttempt(BaseModel):
    """一次深链生命周期的留痕：谁发的、谁点开的、为什么打不开。

    带 `task_id` 与 `token_fingerprint`：只有"失败了 N 次"的话，事后无法区分
    "同一个人反复点旧卡"和"有人拿着别人的卡在试"。
    """

    at: datetime = Field(default_factory=utc_now)
    outcome: ConfirmationLinkOutcome
    reason: str = ""
    task_id: UUID | None = None
    step_id: str = ""
    actor: str = ""
    actor_kind: str = ""
    token_fingerprint: str = ""


class ConfirmationLinkStore(Protocol):
    """深链与留痕的存储口径。

    `consume` 是唯一的原子点：两个并发的落地请求里只能有一个成功，否则同一张
    卡会被"打开"两次，一次性语义就成了纸面承诺。

    `issue` 对同一令牌必须幂等覆盖（令牌由 `secrets.token_urlsafe(32)` 生成，
    撞车概率可忽略；覆盖语义只是为了让重发同一张卡不报错）。
    """

    def issue(self, link: ConfirmationLink) -> ConfirmationLink: ...

    def get(self, token: str) -> ConfirmationLink | None: ...

    def consume(self, token: str, *, now: datetime | None = None) -> ConfirmationLink | None:
        """原子消费：已消费过返回 None（包括并发输掉的那一方）。"""
        ...

    def record_attempt(self, attempt: ConfirmationLinkAttempt) -> None: ...

    def list_attempts(
        self, *, task_id: UUID | None = None, limit: int = 50
    ) -> list[ConfirmationLinkAttempt]: ...
