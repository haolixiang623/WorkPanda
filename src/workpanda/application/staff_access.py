"""工作人员目录维护与 IM 身份解析。

两层职责刻意分开：

- `StaffDirectoryService`：管理侧的维护动作（增删改主体与映射）。它只在
  `/api/admin/identities/*`（admin 凭据）下被调用，**不参与任何放行判定**；
- `ImIdentityResolver`：入站 IM 身份 → 工作人员主体。它是 N4 网关唯一该用的入口，
  网关自己不做身份判断，只把 `(channel, im_user_id)` 交给这里。

解析结果的三种去向（不允许第四种）：换成 `kind=staff` 的主体、抛
`ImIdentityRejected`、或者存储读取出错时抛错。**绝不降级成匿名会话**——降级会让
"没配好"表现为"能办事但记不到人"，而这种故障不会报错。
"""

from __future__ import annotations

from workpanda.domain.identity import Principal, PrincipalKind
from workpanda.domain.staff import (
    ImAccessEntry,
    ImAccessOutcome,
    ImIdentity,
    ImIdentityRejected,
    ImRejectionReason,
    StaffMember,
    StaffStore,
)


class StaffDirectoryError(ValueError):
    """维护动作非法（例如把 IM 映射指向一个不存在的主体）。"""


class StaffDirectoryService:
    """工作人员目录与 IM 映射的维护入口。变更立即生效，无需重启。"""

    def __init__(self, store: StaffStore) -> None:
        self._store = store

    @property
    def store(self) -> StaffStore:
        return self._store

    def list_members(self) -> list[StaffMember]:
        return self._store.list_members()

    def get_member(self, subject: str) -> StaffMember | None:
        return self._store.get_member(subject)

    def save_member(self, member: StaffMember) -> StaffMember:
        return self._store.save_member(member)

    def delete_member(self, subject: str) -> bool:
        """删除主体并级联删除指向它的 IM 映射（存储层同一事务内完成）。"""

        return self._store.delete_member(subject)

    def list_identities(self, channel: str | None = None) -> list[ImIdentity]:
        return self._store.list_identities(channel)

    def save_identity(self, identity: ImIdentity) -> ImIdentity:
        """写入映射。目标主体必须已存在：先配人、再配映射。

        允许目标主体处于停用状态（写入仍然成功），因为"先停用、过两天再删除"是
        正常流程；但解析时会按 `im_principal_disabled` 拒绝，而不是照常放行。
        """

        if self._store.get_member(identity.principal_subject) is None:
            raise StaffDirectoryError(
                f"主体 {identity.principal_subject!r} 不在工作人员目录里；"
                "请先把这个人建成工作人员，再把 IM 身份映射过去。"
            )
        return self._store.save_identity(identity)

    def delete_identity(self, channel: str, im_user_id: str) -> bool:
        return self._store.delete_identity(channel, im_user_id)

    def list_access(self, *, limit: int = 50, rejected_only: bool = False) -> list[ImAccessEntry]:
        return self._store.list_access(limit=limit, rejected_only=rejected_only)


class ImIdentityResolver:
    """把 IM 身份换成工作人员主体；换不出来就拒绝并留痕。"""

    def __init__(self, store: StaffStore) -> None:
        self._store = store

    def resolve(
        self, channel: str, im_user_id: str, *, origin: str = "gateway"
    ) -> Principal:
        """解析入站 IM 身份。

        `origin` 只影响留痕（真实入站 / 管理侧诊断），不影响判定：诊断请求走的是
        同一套规则，否则"诊断通过、入站被拒"就成了一种没人解释得清的现象。
        """

        channel = channel.strip().lower()
        im_user_id = im_user_id.strip()
        identity = self._store.resolve_identity(channel, im_user_id)
        if identity is None:
            raise self._reject(ImRejectionReason.UNMAPPED, channel, im_user_id, origin=origin)
        if not identity.enabled:
            raise self._reject(
                ImRejectionReason.DISABLED,
                channel,
                im_user_id,
                origin=origin,
                subject=identity.principal_subject,
            )

        member = self._store.get_member(identity.principal_subject)
        if member is None:
            raise self._reject(
                ImRejectionReason.PRINCIPAL_MISSING,
                channel,
                im_user_id,
                origin=origin,
                subject=identity.principal_subject,
            )
        if not member.enabled:
            raise self._reject(
                ImRejectionReason.PRINCIPAL_DISABLED,
                channel,
                im_user_id,
                origin=origin,
                subject=member.subject,
            )

        self._store.record_access(
            ImAccessEntry(
                origin=origin,
                channel=channel,
                im_user_id=im_user_id,
                subject=member.subject,
                outcome=ImAccessOutcome.GRANTED,
            )
        )
        return Principal(subject=member.subject, kind=PrincipalKind.STAFF, roles=member.roles)

    def _reject(
        self,
        reason: ImRejectionReason,
        channel: str,
        im_user_id: str,
        *,
        origin: str,
        subject: str = "",
    ) -> ImIdentityRejected:
        """先留痕再抛错：留痕失败不该掩盖"拒绝"这个结论，但必须留得下。"""

        self._store.record_access(
            ImAccessEntry(
                origin=origin,
                channel=channel,
                im_user_id=im_user_id,
                subject=subject,
                outcome=ImAccessOutcome.REJECTED,
                reason=reason.value,
            )
        )
        return ImIdentityRejected(reason, channel, im_user_id)
