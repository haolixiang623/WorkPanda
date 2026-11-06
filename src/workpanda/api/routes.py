"""任务 API：群众侧只通过这些接口与业务后端交互。

所有接口都要求有效会话；任务归属由服务端判定，跨会话访问按不存在处理。
"""

import asyncio
import json
from collections.abc import AsyncIterator
from uuid import UUID

from fastapi import APIRouter, Depends, Query, status
from sse_starlette.sse import EventSourceResponse

from workpanda.api.dependencies import get_container
from workpanda.api.security import get_principal
from workpanda.bootstrap import Container
from workpanda.domain.identity import Principal
from workpanda.domain.models import (
    TaskAnswerRequest,
    TaskConfirmRequest,
    TaskCreateRequest,
    TaskEvent,
    TaskView,
)

router = APIRouter(prefix="/api")


def _sse(event: TaskEvent) -> dict[str, str]:
    return {
        "id": str(event.sequence),
        "event": event.type.value,
        "data": event.model_dump_json(),
    }


@router.post("/tasks", response_model=TaskView, status_code=status.HTTP_201_CREATED)
async def create_task(
    request: TaskCreateRequest,
    principal: Principal = Depends(get_principal),
    container: Container = Depends(get_container),
) -> TaskView:
    return TaskView.from_task(container.tasks.create_task(request, principal.subject))


@router.get("/tasks/{task_id}", response_model=TaskView)
async def get_task(
    task_id: UUID,
    principal: Principal = Depends(get_principal),
    container: Container = Depends(get_container),
) -> TaskView:
    return TaskView.from_task(container.tasks.get_task(task_id, principal.subject))


@router.post("/tasks/{task_id}/answers", response_model=TaskView)
async def answer_task(
    task_id: UUID,
    request: TaskAnswerRequest,
    principal: Principal = Depends(get_principal),
    container: Container = Depends(get_container),
) -> TaskView:
    return TaskView.from_task(container.tasks.answer(task_id, request, principal.subject))


@router.post("/tasks/{task_id}/confirm", response_model=TaskView)
async def confirm_task(
    task_id: UUID,
    request: TaskConfirmRequest,
    principal: Principal = Depends(get_principal),
    container: Container = Depends(get_container),
) -> TaskView:
    return TaskView.from_task(container.tasks.confirm(task_id, request, principal.subject))


@router.post("/tasks/{task_id}/cancel", response_model=TaskView)
async def cancel_task(
    task_id: UUID,
    principal: Principal = Depends(get_principal),
    container: Container = Depends(get_container),
) -> TaskView:
    return TaskView.from_task(container.tasks.cancel(task_id, principal.subject))


@router.get("/tasks/{task_id}/events")
async def task_events(
    task_id: UUID,
    after: int = Query(default=0, ge=0),
    principal: Principal = Depends(get_principal),
    container: Container = Depends(get_container),
) -> EventSourceResponse:
    container.tasks.get_task(task_id, principal.subject)
    keepalive = container.settings.event_keepalive_seconds

    async def stream() -> AsyncIterator[dict[str, str]]:
        last_sent = after
        async with container.broker.subscribe(task_id) as queue:
            for event in container.tasks.events(task_id, principal.subject, after_sequence=after):
                last_sent = event.sequence
                yield _sse(event)

            while True:
                try:
                    sequence = await asyncio.wait_for(queue.get(), timeout=keepalive)
                except TimeoutError:
                    yield {"event": "keepalive", "data": json.dumps({"sequence": last_sent})}
                    continue
                if sequence <= last_sent:
                    continue
                for event in container.tasks.events(
                    task_id, principal.subject, after_sequence=last_sent
                ):
                    last_sent = event.sequence
                    yield _sse(event)

    return EventSourceResponse(stream())
