"""工作人员（staff）主体与 IM 身份映射。

系统里原本只有三类主体：匿名签名会话（服务对象）、运维凭据、管理凭据。
「工作人员」是第四类，办的事和那三类都不同——受理、复核、处理业务，
既不是「看留痕」，也不是「改流程」。因此它有自己的凭据、自己的角色，
**不继承 admin 的流程编辑能力**（见 `docs/STAFF-IDENTITY.md` 第 1 节）。

两条刻意的边界：

1. **权限按「能对哪些流程做什么动作」授予**（`StaffGrant`），不给一个粗粒度的
   `staff` 角色就完事。这样"某个窗口只能受理，不能复核"不需要新加一类主体。
2. **IM 身份是显式数据，不是推断结论**（`ImIdentity`）。企业微信同时挂外部联系人
   与内部员工，外部联系人的身份完全不受控；用"消息来自 IM 网关"推断员工身份，
   等于把外部联系人一起提权。未映射者一律拒绝并留痕，**不降级为匿名会话**——
   降级会让"没配好"表现为"能办事但记不到人"，而这类故障不会报错。
"""

from __future__ import annotations

import re
from datetime import datetime
from enum import StrEnum
from typing import Protocol

from pydantic import BaseModel, Field, field_validator

from workpanda.domain.models import utc_now

# 凭据里的角色名，与 `docs/OPS-ACCESS-CONTROL.md` 的角色表同源。
STAFF_ROLE = "staff"
# 授权里的"全部流程"。用它表达"这个人在所有流程上都能受理"，
# 但不等于"能改流程"——流程编辑始终只属于 admin。
ALL_FLOWS = "*"

_SUBJECT_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
# IM 渠道名只允许小写标识（wecom / dingtalk 之类），避免同一条渠道写出两种拼法后
# 映射表里出现两行互相不认识的数据。
_CHANNEL_PATTERN = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")


class StaffAction(StrEnum):
    """工作人员对一条流程能做的动作。三档对应业务上的受理 / 复核 / 处理。"""

    INTAKE = "intake"
    REVIEW = "review"
    HANDLE = "handle"


def normalize_subject(subject: str) -> str:
    """主体名规范化：只允许字母、数字、点、下划线、连字符。

    与运维/管理侧的主体名规则保持一致——同一个名字在这三套凭据里必须是同一个
    意思，否则"谁的凭据、谁的授权"在事后核对时会分叉。
    """

    value = subject.strip()
    if not _SUBJECT_PATTERN.match(value):
        raise ValueError(
            f"主体名非法：{subject!r}，只允许字母、数字、点、下划线和连字符，且以字母或数字开头。"
        )
    return value


def normalize_channel(channel: str) -> str:
    value = channel.strip().lower()
    if not _CHANNEL_PATTERN.match(value):
        raise ValueError(f"IM 渠道名非法：{channel!r}，只允许小写字母、数字、下划线和连字符。")
    return value


def normalize_im_user_id(im_user_id: str) -> str:
    """IM 侧用户标识：保留原样（区分大小写），只去空白并要求非空。

    不在这里做格式假设：企业微信的 userid 是账号名，外部联系人是另一套
    `external_userid`，钉钉又有自己的形态。写死一种格式，接第二个渠道时就会
    把合法标识判成非法。
    """

    value = im_user_id.strip()
    if not value:
        raise ValueError("IM 用户标识不能为空。")
    if len(value) > 128:
        raise ValueError("IM 用户标识过长（上限 128 字符）。")
    return value


class StaffGrant(BaseModel):
    """一条授权：在 `flow_id` 上可以做 `actions` 里的动作。"""

    flow_id: str = Field(min_length=1, max_length=128)
    actions: frozenset[StaffAction] = Field(default_factory=frozenset)

    def covers(self, flow_id: str, action: StaffAction) -> bool:
        if self.flow_id not in {ALL_FLOWS, flow_id}:
            return False
        return action in self.actions


class StaffMember(BaseModel):
    """一名工作人员。`enabled=False` 时凭据仍然有效，但不再被授予任何权限。

    停用与删除的区别要留着：删除之后这条主体名可以被别人重新占用，
    而停用保留"这个人曾经存在"的事实，审计记录里的主体名才不会指向另一个人。
    """

    subject: str = Field(min_length=1, max_length=64)
    display_name: str = Field(default="", max_length=64)
    enabled: bool = True
    grants: tuple[StaffGrant, ...] = ()
    updated_at: datetime = Field(default_factory=utc_now)

    @field_validator("subject")
    @classmethod
    def _check_subject(cls, value: str) -> str:
        return normalize_subject(value)

    @property
    def roles(self) -> tuple[str, ...]:
        """给 `Principal.roles` 用的字符串视图：`staff` + `flow:<flow_id>:<action>`。

        权限判定的唯一依据是 `allows()`；这个属性只用于把"能做什么"原样
        交给调用方展示，不参与任何放行判断。
        """

        pairs = sorted(
            {
                f"flow:{grant.flow_id}:{action.value}"
                for grant in self.grants
                for action in grant.actions
            }
        )
        return (STAFF_ROLE, *pairs)

    def allows(self, flow_id: str, action: StaffAction) -> bool:
        if not self.enabled:
            return False
        return any(grant.covers(flow_id, action) for grant in self.grants)

    def actions_on(self, flow_id: str) -> tuple[StaffAction, ...]:
        return tuple(action for action in StaffAction if self.allows(flow_id, action))


class ImIdentity(BaseModel):
    """`(channel, im_user_id) → 主体` 的显式映射。

    映射到的主体必须是工作人员（`StaffMember`）：这是本轮的唯一用途。把 IM 身份
    映射到 admin 会让"在群里点一下"等同于"能改流程"，而群成员名单本身不受本系统
    控制。
    """

    channel: str = Field(min_length=1, max_length=32)
    im_user_id: str = Field(min_length=1, max_length=128)
    principal_subject: str = Field(min_length=1, max_length=64)
    enabled: bool = True
    note: str = Field(default="", max_length=200)
    updated_at: datetime = Field(default_factory=utc_now)

    @field_validator("channel")
    @classmethod
    def _check_channel(cls, value: str) -> str:
        return normalize_channel(value)

    @field_validator("im_user_id")
    @classmethod
    def _check_im_user_id(cls, value: str) -> str:
        return normalize_im_user_id(value)

    @field_validator("principal_subject")
    @classmethod
    def _check_subject(cls, value: str) -> str:
        return normalize_subject(value)


class ImRejectionReason(StrEnum):
    """未映射/未开通时拒绝的原因。分档是为了让排障知道该改哪一处配置。"""

    UNMAPPED = "im_identity_unmapped"
    DISABLED = "im_identity_disabled"
    PRINCIPAL_MISSING = "im_principal_missing"
    PRINCIPAL_DISABLED = "im_principal_disabled"


class ImIdentityRejected(Exception):
    """IM 身份没能换成工作人员主体。**调用方不得降级为匿名会话。**"""

    def __init__(self, reason: ImRejectionReason, channel: str, im_user_id: str) -> None:
        super().__init__(f"{reason.value}: {channel}/{im_user_id}")
        self.reason = reason
        self.channel = channel
        self.im_user_id = im_user_id


class ImAccessOutcome(StrEnum):
    GRANTED = "granted"
    REJECTED = "rejected"


class ImAccessEntry(BaseModel):
    """一次 IM 身份解析的留痕。拒绝记录里必须带 IM 标识，才能回答"是谁被拦了"。"""

    at: datetime = Field(default_factory=utc_now)
    # gateway：真实入站；admin_probe：管理侧诊断。分开是为了不让人拿诊断请求
    # 把真实拒绝记录淹掉。
    origin: str = Field(default="gateway", max_length=32)
    channel: str = Field(max_length=32)
    im_user_id: str = Field(max_length=128)
    subject: str = ""
    outcome: ImAccessOutcome
    reason: str = ""


class StaffStore(Protocol):
    """工作人员目录 + IM 映射 + 解析留痕的存储口径。

    三件事放一个接口，是因为它们必须同时可用：解析一次 IM 身份要"读映射 + 读主体
    + 写留痕"，分散到三个后端时，sqlite 场景就要开三个连接去同一个文件。
    """

    def get_member(self, subject: str) -> StaffMember | None: ...

    def list_members(self) -> list[StaffMember]: ...

    def save_member(self, member: StaffMember) -> StaffMember: ...

    def delete_member(self, subject: str) -> bool: ...

    def resolve_identity(self, channel: str, im_user_id: str) -> ImIdentity | None: ...

    def list_identities(self, channel: str | None = None) -> list[ImIdentity]: ...

    def save_identity(self, identity: ImIdentity) -> ImIdentity: ...

    def delete_identity(self, channel: str, im_user_id: str) -> bool: ...

    def record_access(self, entry: ImAccessEntry) -> None: ...

    def list_access(
        self, *, limit: int = 50, rejected_only: bool = False
    ) -> list[ImAccessEntry]: ...
