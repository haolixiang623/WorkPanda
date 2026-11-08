"""管理侧 SOP 配置台接口 `/api/admin/flows/*`。

与 `/api/ops` 是**两套独立凭据**：运维只读凭据（`WORKPANDA_OPS_TOKENS`）能看任务
明细，但不能改"哪一步需要确认、哪一步能调哪个工具"。管理侧凭据单独发放
（`WORKPANDA_ADMIN_TOKENS`），`viewer` 只读、`admin` 才能写。未配置任何管理凭据时
整个前缀按不存在处理（404），不暴露接口存在性。

群众侧与运维侧的凭据放进这个头一律 401：三者各走各的比对逻辑，没有
"任一通过即放行"的分支。管理侧凭据本身从不出现在响应体、日志或前端 bundle 里
——页面由运维在浏览器里临时输入，只放在内存变量中随请求头发送。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Header, HTTPException, status
from pydantic import BaseModel, Field

from workpanda.api.dependencies import get_container
from workpanda.api.ops_auth import OpsDisabled, OpsPrincipal, OpsUnauthenticated
from workpanda.application.admin_flows import (
    AdminDraftInvalid,
    AdminFlowConflict,
    AdminFlowNotFound,
    AdminFlowService,
    FlowSummary,
    FlowVersionDetail,
    PublishOutcome,
    serialize_report,
)
from workpanda.application.intent_eval import IntentEvalBlocked
from workpanda.application.release import FlowReleaseBlocked
from workpanda.bootstrap import Container
from workpanda.domain.flow import FlowDefinition, FlowStatus, StepDefinition
from workpanda.domain.intent import IntentRule
from workpanda.domain.intent_sample import IntentSample
from workpanda.domain.replay import ReplayCase, ReplayExpectation

router = APIRouter(prefix="/api/admin/flows", tags=["admin"])

ADMIN_TOKEN_HEADER = "X-WorkPanda-Admin-Token"


def get_admin_principal(
    x_workpanda_admin_token: str | None = Header(default=None),
    container: Container = Depends(get_container),
) -> OpsPrincipal:
    try:
        return container.admin_access.authenticate(x_workpanda_admin_token)
    except OpsDisabled:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="not found"
        ) from None
    except OpsUnauthenticated:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "admin_token_invalid", "detail": "管理侧凭据无效。"},
        ) from None


def require_admin_role(
    principal: OpsPrincipal = Depends(get_admin_principal),
) -> OpsPrincipal:
    """写操作只对 admin 开放；viewer 是值班只读角色，不得移动发布指针。"""

    if not principal.is_admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "code": "admin_forbidden",
                "detail": "当前管理侧角色无权修改流程，请使用 admin 角色凭据。",
            },
        )
    return principal


def _service(container: Container) -> AdminFlowService:
    service = container.admin_flows
    if service is None:
        # 测试注入流程定义时没有版本库。这是装配问题，不是请求问题。
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"code": "admin_unavailable", "detail": "流程版本库未启用，管理侧不可用。"},
        )
    return service


def _not_found(exc: AdminFlowNotFound) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail={"code": "admin_flow_not_found", "detail": str(exc)},
    )


def _conflict(exc: AdminFlowConflict) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail={"code": "admin_flow_conflict", "detail": str(exc)},
    )


class FlowDraftRequest(BaseModel):
    """草稿提交体。没有 `status` 字段：发布与否只能由发布指针表达。

    `intent_samples` 随版本内容一起提交（#62）：管理页录入的样例必须真正落到版本里，
    否则界面建出的流程一定过不了发布门禁。条数下限**不在这里**裁决——草稿允许先
    建流程、后补样例，发布时由 `IntentReleaseGate` 用服务端阈值判定，避免前后端各写
    一份阈值而漂移。形状（id / text / category）与重复由 pydantic 和
    `flow_field_errors` 兜底，重复项会带 `intent_samples.<i>.<field>` 的 loc。

    `intent_rules` 同理随版本走（D2=B，#73）：识别规则是这一版的一部分，发布这一版
    就等于换掉识别口径。规则必须指向**自己这一版**，指错版本会被 `flow_field_errors`
    以 `intent_rules.<i>.flow_id` 拦下——留到运行时才发现，报出来会像是群众说错了话。
    """

    flow_id: str = Field(min_length=1, max_length=128)
    version: str = Field(min_length=1, max_length=64)
    title: str = Field(min_length=1, max_length=200)
    steps: tuple[StepDefinition, ...] = Field(min_length=1)
    intent_samples: tuple[IntentSample, ...] = ()
    intent_rules: tuple[IntentRule, ...] = ()


class CaseDraftRequest(BaseModel):
    case_id: str = Field(min_length=1, max_length=128)
    slots: dict[str, str] = Field(default_factory=dict)
    expect: ReplayExpectation = Field(default_factory=ReplayExpectation)
    note: str | None = Field(default=None, max_length=500)


class PublishRequest(BaseModel):
    version: str = Field(min_length=1, max_length=64)
    # False = 只跑门禁、不移动指针。界面据此区分"预演通过"与"已发布"。
    apply: bool = True


class VersionRequest(BaseModel):
    version: str = Field(min_length=1, max_length=64)


@router.get("", response_model=list[FlowSummary])
async def list_flows(
    container: Container = Depends(get_container),
    _principal: OpsPrincipal = Depends(get_admin_principal),
) -> list[FlowSummary]:
    """版本列表 + 当前发布指针。viewer 可读，用于核对"现在到底在跑哪一版"。"""

    return _service(container).overview()


@router.get("/{flow_id}/versions/{version}", response_model=FlowVersionDetail)
async def get_version(
    flow_id: str,
    version: str,
    container: Container = Depends(get_container),
    _principal: OpsPrincipal = Depends(get_admin_principal),
) -> FlowVersionDetail:
    try:
        return _service(container).detail(flow_id, version)
    except AdminFlowNotFound as exc:
        raise _not_found(exc) from exc


@router.post("", response_model=FlowDefinition, status_code=status.HTTP_201_CREATED)
async def create_draft(
    draft: FlowDraftRequest,
    _principal: OpsPrincipal = Depends(require_admin_role),
    container: Container = Depends(get_container),
) -> FlowDefinition:
    """新建草稿版本。版本不可变：同 (flow_id, version) 已存在一律 409，不覆盖。

    pydantic 先挡字段形状（422 带 loc），再由 `flow_field_errors` 挡语义
    （execute 必须有 tool、非执行步骤不得带 tool / 要求确认、step_id 不重复、
    intent sample 的 id / 文本不重复），错误同样带 `loc`，界面可标到具体输入框。
    """

    flow = FlowDefinition(
        flow_id=draft.flow_id,
        version=draft.version,
        title=draft.title,
        status=FlowStatus.DRAFT,
        steps=draft.steps,
        intent_samples=draft.intent_samples,
        intent_rules=draft.intent_rules,
    )
    service = _service(container)
    try:
        return service.create_draft(flow)
    except AdminDraftInvalid as exc:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "admin_draft_invalid",
                "detail": "流程草稿校验未通过。",
                "fields": [
                    {"loc": list(error.loc), "message": error.message}
                    for error in exc.errors
                ],
            },
        ) from exc
    except AdminFlowConflict as exc:
        raise _conflict(exc) from exc


@router.post(
    "/{flow_id}/versions/{version}/cases",
    response_model=ReplayCase,
    status_code=status.HTTP_201_CREATED,
)
async def add_case(
    flow_id: str,
    version: str,
    draft: CaseDraftRequest,
    _principal: OpsPrincipal = Depends(require_admin_role),
    container: Container = Depends(get_container),
) -> ReplayCase:
    """为该版本新增一条回放用例。flow_id / version 取自路径，不接受请求体自报。"""

    case = ReplayCase(
        flow_id=flow_id,
        flow_version=version,
        **draft.model_dump(),
    )
    try:
        return _service(container).add_case(flow_id, version, case)
    except AdminFlowNotFound as exc:
        raise _not_found(exc) from exc
    except AdminFlowConflict as exc:
        raise _conflict(exc) from exc


@router.delete("/{flow_id}/versions/{version}/cases/{case_id}")
async def delete_case(
    flow_id: str,
    version: str,
    case_id: str,
    _principal: OpsPrincipal = Depends(require_admin_role),
    container: Container = Depends(get_container),
) -> dict:
    try:
        _service(container).delete_case(flow_id, version, case_id)
    except AdminFlowNotFound as exc:
        raise _not_found(exc) from exc
    return {"flow_id": flow_id, "version": version, "case_id": case_id, "deleted": True}


@router.post("/{flow_id}/versions/{version}/replay")
async def replay_version(
    flow_id: str,
    version: str,
    _principal: OpsPrincipal = Depends(get_admin_principal),
    container: Container = Depends(get_container),
) -> dict:
    """跑一遍回放，返回逐 step 观测与总判定。只读，不改任何状态。"""

    try:
        return serialize_report(_service(container).replay(flow_id, version))
    except AdminFlowNotFound as exc:
        raise _not_found(exc) from exc


@router.post("/{flow_id}/publish", response_model=PublishOutcome)
async def publish(
    flow_id: str,
    request: PublishRequest,
    _principal: OpsPrincipal = Depends(require_admin_role),
    container: Container = Depends(get_container),
) -> PublishOutcome:
    """发布（或预演）。门禁拦下时返回 409 + 完整报告，指针保持不动。

    两道门各有独立错误码：识别样例不够（`admin_intent_gate_blocked`）与回放未通过
    （`admin_release_blocked`）是两种完全不同的补救动作，界面不该把它们混成一句话。
    """

    try:
        outcome = _service(container).publish(flow_id, request.version, apply=request.apply)
    except AdminFlowNotFound as exc:
        raise _not_found(exc) from exc
    except IntentEvalBlocked as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "admin_intent_gate_blocked",
                "detail": str(exc),
                "report": exc.report.model_dump(mode="json"),
            },
        ) from exc
    except FlowReleaseBlocked as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "admin_release_blocked",
                "detail": str(exc),
                "report": serialize_report(exc.report),
            },
        ) from exc
    return outcome


@router.post("/{flow_id}/disable")
async def disable(
    flow_id: str,
    _principal: OpsPrincipal = Depends(require_admin_role),
    container: Container = Depends(get_container),
) -> dict:
    """停用：清空发布指针，历史版本一行不删。"""

    _service(container).disable(flow_id)
    return {"flow_id": flow_id, "disabled": True}


@router.post("/{flow_id}/rollback")
async def rollback(
    flow_id: str,
    request: VersionRequest,
    _principal: OpsPrincipal = Depends(require_admin_role),
    container: Container = Depends(get_container),
) -> dict:
    """回滚：把指针移回一个已存在的历史版本，在途任务仍用各自的版本继续跑。"""

    try:
        _service(container).rollback(flow_id, request.version)
    except AdminFlowNotFound as exc:
        raise _not_found(exc) from exc
    return {"flow_id": flow_id, "version": request.version, "rolled_back": True}
