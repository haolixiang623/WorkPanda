"""运维只读接口，供 Octop 控制台读取任务与事件。

控制台替代自建运维界面，但不持有业务状态写入权：
这里只提供查询，流程推进、确认和副作用仍只能通过群众侧接口完成。

接口默认关闭；必须配置 `WORKPANDA_OPS_TOKEN` 才能访问，避免开放内部数据。
"""

import hmac
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Query, status

from workpanda.api.dependencies import get_container
from workpanda.bootstrap import Container
from workpanda.domain.models import TaskEvent, TaskStatus, TaskView
from workpanda.harness.audit import AuditEntry
from workpanda.harness.trace import TraceRecord, TraceSummary

router = APIRouter(prefix="/api/ops", tags=["ops"])


def require_ops_token(
    x_workpanda_ops_token: str | None = Header(default=None),
    container: Container = Depends(get_container),
) -> None:
    expected = container.settings.ops_token
    if not expected:
        # 未启用时按不存在处理，避免暴露内部接口存在性。
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not found")
    if not x_workpanda_ops_token or not hmac.compare_digest(x_workpanda_ops_token, expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "ops_token_invalid", "detail": "运维凭据无效。"},
        )


@router.get("/tasks", response_model=list[TaskView], dependencies=[Depends(require_ops_token)])
async def list_tasks(
    limit: int = Query(default=50, ge=1, le=200),
    task_status: TaskStatus | None = Query(default=None, alias="status"),
    container: Container = Depends(get_container),
) -> list[TaskView]:
    return [
        TaskView.from_task(task)
        for task in container.tasks.list_tasks(limit=limit, status=task_status)
    ]


@router.get(
    "/tasks/{task_id}", response_model=TaskView, dependencies=[Depends(require_ops_token)]
)
async def get_task(task_id: UUID, container: Container = Depends(get_container)) -> TaskView:
    return TaskView.from_task(container.tasks.ops_task(task_id))


@router.get(
    "/tasks/{task_id}/events",
    response_model=list[TaskEvent],
    dependencies=[Depends(require_ops_token)],
)
async def list_events(
    task_id: UUID,
    after: int = Query(default=0, ge=0),
    container: Container = Depends(get_container),
) -> list[TaskEvent]:
    return list(container.tasks.ops_events(task_id, after_sequence=after))


@router.get(
    "/traces/summary",
    response_model=TraceSummary,
    dependencies=[Depends(require_ops_token)],
)
async def trace_summary(
    task_id: UUID | None = Query(default=None),
    container: Container = Depends(get_container),
) -> TraceSummary:
    """聚合指标：未确认占比是最关键的健康信号，不是任务数量。"""

    return container.traces.summary(task_id=task_id)


@router.get(
    "/traces",
    response_model=list[TraceRecord],
    dependencies=[Depends(require_ops_token)],
)
async def list_traces(
    task_id: UUID | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    container: Container = Depends(get_container),
) -> list[TraceRecord]:
    return container.traces.list(task_id=task_id, limit=limit)


@router.get(
    "/audit",
    response_model=list[AuditEntry],
    dependencies=[Depends(require_ops_token)],
)
async def list_audit(
    task_id: UUID | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    container: Container = Depends(get_container),
) -> list[AuditEntry]:
    """审计归档：只追加、不被容量淘汰，用于核对"某次提交到底发出去没有"。

    未配置归档时返回空列表，而不是 404：接口存在性不随配置变化，
    免得运维看到 404 时误判成"这版还没上这个能力"。
    """

    return container.audit.read(task_id=task_id, limit=limit)
