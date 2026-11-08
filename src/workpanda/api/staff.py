"""工作人员接口 `/api/staff/*`。

第四类主体：工作人员用**自己的凭据**（`X-WorkPanda-Staff-Token`，来自
`WORKPANDA_STAFF_TOKENS`）访问，四类通道凭据互不认账——把 staff 凭据放进
`X-WorkPanda-Admin-Token` 拿不到任何管理侧能力，反过来也一样。

三道判定分开，是因为它们的补救动作完全不同：

- **404**：没配置任何 staff 凭据，整个前缀按不存在处理，不暴露接口存在性；
- **401**：没带凭据，或凭据不匹配任何主体（`staff_token_invalid`）；
- **403**：凭据有效，但这个主体没有开通工作人员身份（停用或未建），
  或者有身份但没有该动作的授权（`staff_not_provisioned` / `staff_forbidden`）。

本轮（N5）在"我是谁"之外只多一件事：为待确认步骤发**一次性深链**。它不改任务
状态、不签发确认凭证，落地后仍由群众自己的会话走既有的 `/confirm`。除此之外
仍然没有任何能改动任务状态的 staff 入口——**没有入口的权限，比有入口但靠自觉更可靠**。
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, status
from pydantic import BaseModel

from workpanda.api.dependencies import get_container
from workpanda.api.ops_auth import OpsDisabled, OpsUnauthenticated
from workpanda.application.deeplink_confirm import (
    ConfirmationLinkForbidden,
    ConfirmationLinkTicket,
)
from workpanda.bootstrap import Container
from workpanda.domain.deeplink import ConfirmationLinkAttempt
from workpanda.domain.identity import Principal, PrincipalKind
from workpanda.domain.staff import StaffAction, StaffGrant, StaffMember

router = APIRouter(prefix="/api/staff", tags=["staff"])

STAFF_TOKEN_HEADER = "X-WorkPanda-Staff-Token"


class StaffProfile(BaseModel):
    """"我是谁"：主体、显示名与授权原样回显，方便工作人员自查配得对不对。"""

    subject: str
    display_name: str
    kind: PrincipalKind = PrincipalKind.STAFF
    roles: tuple[str, ...] = ()
    grants: tuple[StaffGrant, ...] = ()


class StaffFlowGrant(BaseModel):
    """"我能对哪条流程做什么"：来自目录授权与当前版本列表的交集。"""

    flow_id: str
    version: str
    title: str
    actions: tuple[StaffAction, ...]


def get_staff_member(
    x_workpanda_staff_token: str | None = Header(default=None),
    container: Container = Depends(get_container),
) -> StaffMember:
    try:
        staff = container.staff_access.authenticate(x_workpanda_staff_token)
    except OpsDisabled:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="not found"
        ) from None
    except OpsUnauthenticated:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "staff_token_invalid", "detail": "工作人员凭据无效。"},
        ) from None

    member = container.staff_store.get_member(staff.subject)
    if member is None or not member.enabled:
        # 凭据是真的，人却不在目录里（或已停用）。这不是"密钥错了"，所以不用 401：
        # 401 会让人反复换密钥，而真正要改的是工作人员目录。
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "code": "staff_not_provisioned",
                "detail": "该主体尚未开通工作人员身份或已被停用，请联系管理侧。",
            },
        )
    return member


def get_staff_principal(member: StaffMember = Depends(get_staff_member)) -> Principal:
    return Principal(subject=member.subject, kind=PrincipalKind.STAFF, roles=member.roles)


@router.get("/me", response_model=StaffProfile)
async def read_profile(member: StaffMember = Depends(get_staff_member)) -> StaffProfile:
    return StaffProfile(
        subject=member.subject,
        display_name=member.display_name,
        roles=member.roles,
        grants=member.grants,
    )


@router.get("/flows", response_model=list[StaffFlowGrant])
async def list_granted_flows(
    member: StaffMember = Depends(get_staff_member),
    container: Container = Depends(get_container),
) -> list[StaffFlowGrant]:
    """按授权裁剪的流程清单。

    只返回"本人能做点什么的那些流程"，不返回全量再让前端过滤：全量清单本身就是
    信息（有哪些流程在跑、叫什么名字），而工作人员不需要知道与自己无关的流程。
    """

    granted: list[StaffFlowGrant] = []
    for flow in container.flows.versions():
        actions = member.actions_on(flow.flow_id)
        if not actions:
            continue
        granted.append(
            StaffFlowGrant(
                flow_id=flow.flow_id,
                version=flow.version,
                title=flow.title,
                actions=actions,
            )
        )
    return granted


# ---------- 卡片深链（N5） ----------
#
# 这里**没有**、也不应该有 `/api/staff/tasks/{id}/confirm`：工作人员的主体不签发
# 确认凭证（D3 红线）。IM 文本回复（"确认""yes""同意"）在结构上就没有落点，
# 因为能处理它的只有 `/api/tasks/{id}/confirm`，而那条路径只认任务所属的会话。
# `tests/test_confirmation_deeplink.py::test_im_text_reply_never_issues_confirmation`
# 把"这个端点不存在"与"凭证没被签发"一起钉在 CI 里。


def _forbidden(message: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail={"code": "staff_forbidden", "detail": message},
    )


@router.post(
    "/tasks/{task_id}/confirmation-links",
    response_model=ConfirmationLinkTicket,
    status_code=status.HTTP_201_CREATED,
)
async def create_confirmation_link(
    task_id: UUID,
    member: StaffMember = Depends(get_staff_member),
    container: Container = Depends(get_container),
) -> ConfirmationLinkTicket:
    """为当前待确认步骤发一张一次性深链。

    发出去的东西**不是**确认凭证：它只是"去哪一页确认"的导航，落地后仍由群众
    自己的会话说 `/confirm`。所以这里既不需要、也不允许调用确认签发路径。
    """

    try:
        return container.confirmation_links.issue(task_id, member)
    except ConfirmationLinkForbidden as exc:
        raise _forbidden(str(exc)) from exc


@router.get(
    "/tasks/{task_id}/confirmation-links/attempts",
    response_model=list[ConfirmationLinkAttempt],
)
async def list_confirmation_link_attempts(
    task_id: UUID,
    member: StaffMember = Depends(get_staff_member),
    container: Container = Depends(get_container),
) -> list[ConfirmationLinkAttempt]:
    """这张任务的深链留痕：谁发的、谁点开的、为什么打不开。"""

    try:
        return container.confirmation_links.attempts_for(task_id, member)
    except ConfirmationLinkForbidden as exc:
        raise _forbidden(str(exc)) from exc
