"""卡片深链确认的应用服务（Issue #65 / 提案 N5）。

三件事，刻意只做三件：

1. **发卡**：工作人员在**被授权**的流程上，为当前待确认步骤生成一张一次性深链；
2. **落地**：浏览器带着既有会话打开深链，服务端只回答"该确认哪一步"，
   **不签发任何凭证**；
3. **留痕**：谁发的、谁点开的、为什么打不开。

为什么不做第四件（"顺手把确认也做了"）：确认凭证必须绑定 `user_ref` 与参数摘要，
而"深链能落地"并不能证明"这就是任务所属的人"。把两步合成一步，等于让深链自己
变成了授权凭证——D3 红线要拦的正是这件事。凭证仍然只由
`FlowRunner.issue_confirmation` 在 `/api/tasks/{id}/confirm` 这条既有路径上签发，
本模块全程不碰 `ConfirmationStore`。

发卡为什么要 `handle` 授权：深链决定"把群众带到哪个确认点"，这是推进流程的动作；
只有 `review`（核对）或 `intake`（受理）的人不该能给别人发确认入口。

判定的顺序也是语义的一部分：先验**卡本身的属性**（绑定哪张任务、是否已用、是否过期），
再验**人**（会话是不是这张任务的），最后才去读**任务当前状态**。理由是前两层都是本地
事实，不需要碰任务库，绝大多数被拒的请求因此在最外面就结束了——少一次对不相关任务的
存在性探测。对外错误码始终是同一个，顺序不构成观察差异；分档只留给运维查。
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel

from workpanda.application.errors import TaskConflict
from workpanda.domain.deeplink import (
    ConfirmationLink,
    ConfirmationLinkAttempt,
    ConfirmationLinkOutcome,
    ConfirmationLinkRejected,
    ConfirmationLinkRejection,
    ConfirmationLinkStore,
    token_fingerprint,
)
from workpanda.domain.identity import Principal, PrincipalKind
from workpanda.domain.models import TaskStatus
from workpanda.domain.repository import TaskStore
from workpanda.domain.staff import StaffAction, StaffMember

# 卡片按钮的落地参数名。两边写在同一个常量里，改一处不会让卡片与接口分叉。
CONFIRM_PAGE_PATH = "/confirm"
TASK_PARAM = "task_id"
TOKEN_PARAM = "link"


class ConfirmationLinkForbidden(Exception):
    """身份有效，但没有在这条流程上发卡的授权。"""


class ConfirmationLinkTicket(BaseModel):
    """发卡结果。`token` 是给卡片按钮用的，属于"一次性导航凭据"，不是授权。"""

    token: str
    url: str
    task_id: UUID
    step_id: str
    flow_version: str
    expires_at: datetime


class ConfirmationLanding(BaseModel):
    """落地结果：只有"该确认哪一步"，**没有任何凭证字段**。

    这个响应体就是红线的形状证据——它多出任何一个 token 字段，深链就变成了
    第二条签发路径。`confirm_path` 是**既有**确认接口的地址，而不是一条新地址：
    浏览器带着自己的会话去调它，凭证由 `TaskService.confirm` 当场签发。
    """

    task_id: UUID
    step_id: str
    flow_version: str
    confirm_path: str


class ConfirmationLinkService:
    def __init__(
        self,
        *,
        store: TaskStore,
        links: ConfirmationLinkStore,
        ttl_seconds: int,
        base_url: str = "",
    ) -> None:
        self.store = store
        self.links = links
        self.ttl_seconds = ttl_seconds
        self.base_url = base_url.rstrip("/")

    # ---------- 发卡 ----------

    def issue(self, task_id: UUID, member: StaffMember) -> ConfirmationLinkTicket:
        task = self.store.get(task_id)
        if not member.allows(task.flow_id, StaffAction.HANDLE):
            raise ConfirmationLinkForbidden(
                f"{member.subject} 没有在流程 {task.flow_id!r} 上发确认深链的授权（需要 handle）。"
            )
        if task.status is not TaskStatus.WAITING_FOR_CONFIRMATION or not task.current_step:
            # 只有"正在等人确认"才谈得上发卡。别的状态发出去，卡片点开也只会
            # 撞上 `link_task_not_confirmable`——与其让用户点一次再失败，不如拒在这里。
            raise TaskConflict("task is not waiting for confirmation")

        link = ConfirmationLink.issue(
            task_id=task.task_id,
            step_id=task.current_step,
            flow_version=task.flow_version,
            issued_by=member.subject,
            ttl_seconds=self.ttl_seconds,
        )
        self.links.issue(link)
        self._record(
            ConfirmationLinkOutcome.ISSUED,
            task_id=link.task_id,
            step_id=link.step_id,
            actor=member.subject,
            actor_kind=PrincipalKind.STAFF.value,
            fingerprint=link.fingerprint(),
        )
        return ConfirmationLinkTicket(
            token=link.token,
            url=self._url(link),
            task_id=link.task_id,
            step_id=link.step_id,
            flow_version=link.flow_version,
            expires_at=link.expires_at,
        )

    # ---------- 落地 ----------

    def redeem(self, task_id: UUID, token: str, principal: Principal) -> ConfirmationLanding:
        """把一张深链换成"该确认哪一步"。

        故意不写 `issue_confirmation`：落地不是授权。这里唯一会产生副作用的是
        `consume`（把卡标记为已用）与留痕。
        """

        link = self.links.get(token)
        if link is None:
            raise self._reject(ConfirmationLinkRejection.UNKNOWN, None, token, principal)
        if link.task_id != task_id:
            # 跨任务使用：拿着 A 任务的卡去开 B 任务。哪怕 B 也是自己的任务也拒绝，
            # 否则"卡片属于哪张任务"这件事就不再被校验。
            raise self._reject(ConfirmationLinkRejection.TASK_MISMATCH, link, token, principal)
        if link.is_consumed:
            # 一次性是卡的属性，与谁在请求无关，所以在读任务之前就能下结论。
            raise self._reject(ConfirmationLinkRejection.ALREADY_USED, link, token, principal)
        if link.is_expired():
            raise self._reject(ConfirmationLinkRejection.EXPIRED, link, token, principal)
        if principal.kind is not PrincipalKind.CITIZEN:
            raise self._reject(
                ConfirmationLinkRejection.PRINCIPAL_NOT_CITIZEN, link, token, principal
            )

        task = self.store.get(task_id)
        if task.user_ref != principal.subject:
            # 不是这张任务的会话。转发给别人、群里"我也点一下"都在这里被拦下。
            raise self._reject(ConfirmationLinkRejection.SESSION_NOT_OWNER, link, token, principal)
        if task.status is not TaskStatus.WAITING_FOR_CONFIRMATION:
            raise self._reject(
                ConfirmationLinkRejection.TASK_NOT_CONFIRMABLE, link, token, principal
            )
        if task.current_step != link.step_id or task.flow_version != link.flow_version:
            # 任务已经推进到别的步骤（或换了流程版本）：这张卡指向的确认点不存在了。
            raise self._reject(ConfirmationLinkRejection.STEP_CHANGED, link, token, principal)

        if self.links.consume(token) is None:
            # 并发输掉的一方：另一个请求已经把它用掉了。一次性语义必须在这里兑现，
            # 而不是靠"前面已经查过 consumed_at"。
            raise self._reject(ConfirmationLinkRejection.ALREADY_USED, link, token, principal)

        self._record(
            ConfirmationLinkOutcome.REDEEMED,
            task_id=link.task_id,
            step_id=link.step_id,
            actor=principal.subject,
            actor_kind=principal.kind.value,
            fingerprint=link.fingerprint(),
        )
        return ConfirmationLanding(
            task_id=link.task_id,
            step_id=link.step_id,
            flow_version=link.flow_version,
            confirm_path=f"/api/tasks/{link.task_id}/confirm",
        )

    # ---------- 留痕 ----------

    def attempts(
        self, *, task_id: UUID | None = None, limit: int = 50
    ) -> list[ConfirmationLinkAttempt]:
        return self.links.list_attempts(task_id=task_id, limit=limit)

    def attempts_for(self, task_id: UUID, member: StaffMember) -> list[ConfirmationLinkAttempt]:
        """某张任务的深链留痕。要求在这条流程上至少有一个动作——看不了就别看。"""

        task = self.store.get(task_id)
        if not member.actions_on(task.flow_id):
            raise ConfirmationLinkForbidden(
                f"{member.subject} 未被授予流程 {task.flow_id!r} 上的任何动作。"
            )
        return self.links.list_attempts(task_id=task_id)

    # ---------- 内部 ----------

    def _url(self, link: ConfirmationLink) -> str:
        """卡片按钮的落地地址。

        未配置 `confirmation_link_base_url` 时只给相对路径：服务端不猜自己的
        对外域名（猜错的后果是卡片指向一个不存在的站点，而排障会先怀疑代码）。
        前端按 `CONFIRM_PAGE_PATH` / `TASK_PARAM` / `TOKEN_PARAM` 解析。
        """

        path = f"{CONFIRM_PAGE_PATH}?{TASK_PARAM}={link.task_id}&{TOKEN_PARAM}={link.token}"
        return f"{self.base_url}{path}" if self.base_url else path

    def _reject(
        self,
        reason: ConfirmationLinkRejection,
        link: ConfirmationLink | None,
        token: str,
        principal: Principal,
    ) -> ConfirmationLinkRejected:
        """先留痕再抛错：留痕失败不该掩盖"拒绝"这个结论，但必须留得下。"""

        self._record(
            ConfirmationLinkOutcome.REJECTED,
            reason=reason.value,
            task_id=link.task_id if link is not None else None,
            step_id=link.step_id if link is not None else "",
            actor=principal.subject,
            actor_kind=principal.kind.value,
            fingerprint=link.fingerprint() if link is not None else token_fingerprint(token),
        )
        return ConfirmationLinkRejected(reason)

    def _record(
        self,
        outcome: ConfirmationLinkOutcome,
        *,
        reason: str = "",
        task_id: UUID | None = None,
        step_id: str = "",
        actor: str = "",
        actor_kind: str = "",
        fingerprint: str = "",
    ) -> None:
        self.links.record_attempt(
            ConfirmationLinkAttempt(
                outcome=outcome,
                reason=reason,
                task_id=task_id,
                step_id=step_id,
                actor=actor,
                actor_kind=actor_kind,
                token_fingerprint=fingerprint,
            )
        )
