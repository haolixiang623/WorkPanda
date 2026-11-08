"""任务 API：群众侧只通过这些接口与业务后端交互。

所有接口都要求有效会话；任务归属由服务端判定，跨会话访问按不存在处理。

会推进流程的请求（创建 / 回答 / 确认）可能真的调用模型与工具，耗时可达数十秒。
它们一律放到工作线程执行：否则同步执行会占住事件循环，用户在这期间发出的
取消请求只能排队到执行结束——那时已经没有可取消的东西了。取消接口本身保持
轻量并留在事件循环上，就是为了让它在执行进行中也能立刻被处理。
"""

import asyncio
import json
from collections.abc import AsyncIterator
from uuid import UUID

from fastapi import APIRouter, Depends, Query, status
from pydantic import BaseModel, Field
from sse_starlette.sse import EventSourceResponse

from workpanda.api.dependencies import get_container
from workpanda.api.security import get_principal
from workpanda.application.deeplink_confirm import ConfirmationLanding
from workpanda.bootstrap import Container
from workpanda.domain.identity import Principal
from workpanda.domain.models import (
    EventType,
    TaskAnswerRequest,
    TaskConfirmRequest,
    TaskCreateRequest,
    TaskEvent,
    TaskView,
)

router = APIRouter(prefix="/api")

# 只给运维看的事件类型：绝不进入群众侧事件流。
# 运营备注里写的多是"疑似重复申请，已电话核实""对方系统显示已办结，待回单"
# 这类内部判断，直接推给群众等于把内部口径和核查线索一起泄出去。
_OPERATOR_ONLY_EVENTS: frozenset[EventType] = frozenset({EventType.OPERATOR_NOTE})


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
    task = await asyncio.to_thread(container.tasks.create_task, request, principal.subject)
    return TaskView.from_task(task, container.flows)


@router.get("/tasks/{task_id}", response_model=TaskView)
async def get_task(
    task_id: UUID,
    principal: Principal = Depends(get_principal),
    container: Container = Depends(get_container),
) -> TaskView:
    return TaskView.from_task(
        container.tasks.get_task(task_id, principal.subject), container.flows
    )


@router.post("/tasks/{task_id}/answers", response_model=TaskView)
async def answer_task(
    task_id: UUID,
    request: TaskAnswerRequest,
    principal: Principal = Depends(get_principal),
    container: Container = Depends(get_container),
) -> TaskView:
    task = await asyncio.to_thread(container.tasks.answer, task_id, request, principal.subject)
    return TaskView.from_task(task, container.flows)


@router.post("/tasks/{task_id}/confirm", response_model=TaskView)
async def confirm_task(
    task_id: UUID,
    request: TaskConfirmRequest,
    principal: Principal = Depends(get_principal),
    container: Container = Depends(get_container),
) -> TaskView:
    task = await asyncio.to_thread(container.tasks.confirm, task_id, request, principal.subject)
    return TaskView.from_task(task, container.flows)


@router.post("/tasks/{task_id}/cancel", response_model=TaskView)
async def cancel_task(
    task_id: UUID,
    principal: Principal = Depends(get_principal),
    container: Container = Depends(get_container),
) -> TaskView:
    # 刻意不放到工作线程：取消必须是执行进行中也能立刻被处理的短请求。
    # 它只翻转任务状态并下发执行侧信号，不做任何模型或工具调用。
    return TaskView.from_task(
        container.tasks.cancel(task_id, principal.subject), container.flows
    )


class ConfirmationLinkRedeemRequest(BaseModel):
    """深链落地请求。令牌放在请求体里而不是查询串：查询串会进访问日志与
    浏览器历史，而这张卡虽然不构成授权，也没有必要多留一份副本。"""

    token: str = Field(min_length=16, max_length=256)


@router.post("/tasks/{task_id}/confirmation-link", response_model=ConfirmationLanding)
async def redeem_confirmation_link(
    task_id: UUID,
    request: ConfirmationLinkRedeemRequest,
    principal: Principal = Depends(get_principal),
    container: Container = Depends(get_container),
) -> ConfirmationLanding:
    """卡片按钮落地：**只回答"该确认哪一步"，不签发凭证。**

    真正签发凭证的仍然是 `POST /api/tasks/{id}/confirm`，绑定 `user_ref` 与
    参数摘要。这条接口如果哪天开始返回凭证，就等于为 IM 消息开了一条签发捷径。
    """

    return await asyncio.to_thread(
        container.confirmation_links.redeem, task_id, request.token, principal
    )


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
                # 游标按读到的事件前进，即使这条不发送：否则备注一旦排在末尾，
                # last_sent 就原地不动，每轮 keepalive 都要把它重扫一遍。
                last_sent = event.sequence
                if event.type in _OPERATOR_ONLY_EVENTS:
                    continue
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
                    if event.type in _OPERATOR_ONLY_EVENTS:
                        continue
                    yield _sse(event)

    return EventSourceResponse(stream())
