"""任务 API 路由。"""

import asyncio
import json
from collections.abc import AsyncIterator
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sse_starlette.sse import EventSourceResponse

from workpanda.api.dependencies import get_task_service
from workpanda.application.task_service import InvalidTaskTransition, TaskService
from workpanda.domain.models import Task, TaskCreate
from workpanda.domain.repository import TaskNotFoundError

router = APIRouter(prefix="/api")


def _not_found() -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="task not found")


@router.post("/tasks", response_model=Task, status_code=status.HTTP_201_CREATED)
def create_task(command: TaskCreate, service: TaskService = Depends(get_task_service)) -> Task:
    return service.create_task(command)


@router.get("/tasks/{task_id}", response_model=Task)
def get_task(task_id: UUID, service: TaskService = Depends(get_task_service)) -> Task:
    try:
        return service.get_task(task_id)
    except TaskNotFoundError as exc:
        raise _not_found() from exc


@router.post("/tasks/{task_id}/cancel", response_model=Task)
def cancel_task(task_id: UUID, service: TaskService = Depends(get_task_service)) -> Task:
    try:
        return service.cancel_task(task_id)
    except TaskNotFoundError as exc:
        raise _not_found() from exc
    except InvalidTaskTransition as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc


@router.get("/tasks/{task_id}/events")
async def task_events(
    task_id: UUID,
    after: int = Query(default=0, ge=0),
    service: TaskService = Depends(get_task_service),
) -> EventSourceResponse:
    try:
        service.get_task(task_id)
    except TaskNotFoundError as exc:
        raise _not_found() from exc

    async def stream() -> AsyncIterator[dict[str, str]]:
        sent = after
        idle_rounds = 0
        while idle_rounds < 2:
            events = service.events(task_id, after_sequence=sent)
            if events:
                for event in events:
                    sent = event.sequence
                    yield {"id": str(event.sequence), "event": event.type, "data": event.model_dump_json()}
                idle_rounds = 0
            else:
                idle_rounds += 1
                yield {"event": "keepalive", "data": json.dumps({"sequence": sent})}
            await asyncio.sleep(0.01)

    return EventSourceResponse(stream())
