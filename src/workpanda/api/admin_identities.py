"""管理侧身份维护接口 `/api/admin/identities/*`。

管的是两件事，都用**管理侧凭据**（`X-WorkPanda-Admin-Token`）：

1. **工作人员目录**：谁是工作人员、在哪几条流程上能做哪些动作；
2. **IM 身份映射**：`(channel, im_user_id) → 工作人员主体`。

为什么放在管理侧而不是运维侧：这两件事决定"谁能办事"，与改流程的确认点属于同一
量级的权限；运维凭据（`viewer`）的定位是"看有没有异常"，拿它改授权会把最小可见
范围直接拉平成"值班能给人开权限"。

`viewer` 仍然能读：排障时要回答"这个人为什么登不进来"，读一下就够，不需要写。
写入一律要求 admin 角色（`require_admin_role`）。

映射变更**立即生效**：解析每次请求都读目录，不存在把映射缓存进进程的路径，
所以没有"改完要重启"这一步——重启会顺带丢掉正在跑的任务上下文。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field

from workpanda.api.admin import get_admin_principal, require_admin_role
from workpanda.api.dependencies import get_container
from workpanda.api.ops_auth import OpsPrincipal
from workpanda.application.staff_access import StaffDirectoryError, StaffDirectoryService
from workpanda.bootstrap import Container
from workpanda.domain.identity import PrincipalKind
from workpanda.domain.staff import (
    ImAccessEntry,
    ImIdentity,
    ImIdentityRejected,
    StaffGrant,
    StaffMember,
)

router = APIRouter(prefix="/api/admin/identities", tags=["admin"])


def _directory(container: Container) -> StaffDirectoryService:
    service = container.staff_directory
    if service is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "admin_unavailable",
                "detail": "工作人员目录未启用，管理侧不可用。",
            },
        )
    return service


class StaffMemberRequest(BaseModel):
    """建/改一名工作人员。`subject` 只从路径取，不接受请求体自报。

    自报主体名等于让调用方决定"这次授权记在谁名下"，而审计要回答的正是"谁"。
    """

    display_name: str = Field(default="", max_length=64)
    enabled: bool = True
    grants: tuple[StaffGrant, ...] = ()


class ImIdentityRequest(BaseModel):
    """建/改一条 IM 映射。`channel` / `im_user_id` 同样只从路径取。"""

    principal_subject: str = Field(min_length=1, max_length=64)
    enabled: bool = True
    note: str = Field(default="", max_length=200)


class ImResolveRequest(BaseModel):
    channel: str = Field(min_length=1, max_length=32)
    im_user_id: str = Field(min_length=1, max_length=128)


class ImResolveResponse(BaseModel):
    """诊断结果：只回主体与授权，不回任何任务数据。"""

    subject: str
    kind: PrincipalKind = PrincipalKind.STAFF
    roles: tuple[str, ...] = ()


@router.get("/staff", response_model=list[StaffMember])
async def list_staff(
    container: Container = Depends(get_container),
    _principal: OpsPrincipal = Depends(get_admin_principal),
) -> list[StaffMember]:
    return _directory(container).list_members()


@router.put("/staff/{subject}", response_model=StaffMember)
async def upsert_staff(
    subject: str,
    request: StaffMemberRequest,
    container: Container = Depends(get_container),
    _principal: OpsPrincipal = Depends(require_admin_role),
) -> StaffMember:
    member = StaffMember(
        subject=subject,
        display_name=request.display_name,
        enabled=request.enabled,
        grants=request.grants,
    )
    return _directory(container).save_member(member)


@router.delete("/staff/{subject}")
async def delete_staff(
    subject: str,
    container: Container = Depends(get_container),
    _principal: OpsPrincipal = Depends(require_admin_role),
) -> dict:
    """删除工作人员，并级联删除指向他的 IM 映射。

    级联是刻意的：主体没了、映射还在，解析时就会一直命中 `im_principal_missing`，
    看起来像"配置错了"，而真相是"人已经删了"。留着这种半截状态的问题只会被
    反复排障，不会被修好。
    """

    deleted = _directory(container).delete_member(subject)
    return {"subject": subject, "deleted": deleted}


@router.get("/im", response_model=list[ImIdentity])
async def list_im_identities(
    channel: str | None = Query(default=None, max_length=32),
    container: Container = Depends(get_container),
    _principal: OpsPrincipal = Depends(get_admin_principal),
) -> list[ImIdentity]:
    return _directory(container).list_identities(channel)


@router.get("/im/access-log", response_model=list[ImAccessEntry])
async def list_im_access_log(
    limit: int = Query(default=50, ge=1, le=200),
    rejected_only: bool = Query(default=False),
    container: Container = Depends(get_container),
    _principal: OpsPrincipal = Depends(get_admin_principal),
) -> list[ImAccessEntry]:
    """解析留痕，新的在前。

    拒绝记录里带 IM 标识（渠道 + IM 用户 ID），这是"未映射者被拒绝"可追溯的依据；
    只有主体名的话，事后无法区分"同一个人换了 IM 号"和"两个人被拦"。
    """

    return _directory(container).list_access(limit=limit, rejected_only=rejected_only)


@router.put("/im/{channel}/{im_user_id}", response_model=ImIdentity)
async def upsert_im_identity(
    channel: str,
    im_user_id: str,
    request: ImIdentityRequest,
    container: Container = Depends(get_container),
    _principal: OpsPrincipal = Depends(require_admin_role),
) -> ImIdentity:
    identity = ImIdentity(
        channel=channel,
        im_user_id=im_user_id,
        principal_subject=request.principal_subject,
        enabled=request.enabled,
        note=request.note,
    )
    try:
        return _directory(container).save_identity(identity)
    except StaffDirectoryError as exc:
        raise HTTPException(
            status_code=422,
            detail={"code": "admin_identity_invalid", "detail": str(exc)},
        ) from exc


@router.delete("/im/{channel}/{im_user_id}")
async def delete_im_identity(
    channel: str,
    im_user_id: str,
    container: Container = Depends(get_container),
    _principal: OpsPrincipal = Depends(require_admin_role),
) -> dict:
    deleted = _directory(container).delete_identity(channel, im_user_id)
    return {"channel": channel, "im_user_id": im_user_id, "deleted": deleted}


@router.post("/im/resolve", response_model=ImResolveResponse)
async def resolve_im_identity(
    request: ImResolveRequest,
    container: Container = Depends(get_container),
    _principal: OpsPrincipal = Depends(require_admin_role),
) -> ImResolveResponse:
    """诊断一次身份解析，走的判定与真实入站**完全相同**。

    存在的意义是回答"这个人为什么进不来"，而不是提供一条能签发身份的旁路：
    调用它需要 admin 凭据，返回体只有主体与授权，拿不到任何会话或凭据。
    """

    try:
        principal = container.im_resolver.resolve(
            request.channel, request.im_user_id, origin="admin_probe"
        )
    except ImIdentityRejected as exc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "code": exc.reason.value,
                "detail": "该 IM 身份未映射到可用的工作人员。",
            },
        ) from exc
    return ImResolveResponse(
        subject=principal.subject, kind=principal.kind, roles=principal.roles
    )
