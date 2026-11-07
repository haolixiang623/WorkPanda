"""取消能不能真的送到执行侧。

这一组针对文档第 14 节里最实的部署前提：执行是同步的，若它占住事件循环，
用户发出的取消请求只能排队到执行结束。离环之后才谈得上"边界生效"。
"""

from __future__ import annotations

import asyncio
import time
import uuid
from threading import Event as ThreadEvent
from uuid import UUID

import httpx
from httpx import ASGITransport

from tests.support import DIRECT_FLOW
from workpanda.api.security import SESSION_HEADER
from workpanda.application.ports import ExecutionOutcome, ExecutionRequest, ExecutionResult
from workpanda.bootstrap import build_container
from workpanda.config import Settings
from workpanda.domain.models import EventType, TaskCreateRequest, TaskStatus
from workpanda.infrastructure.adapters import KeywordIntentRouter
from workpanda.infrastructure.event_broker import EventBroker
from workpanda.main import create_app


def _settings() -> Settings:
    return Settings(store_backend="memory", enable_demo_flow=False)


class WaitingExecutor:
    """模拟一个真在跑的慢步骤：一直等到取消令牌被置位才收尾。"""

    def __init__(self) -> None:
        self.started = ThreadEvent()
        self.task_id: UUID | None = None
        self.observed = False
        self.registry = None

    def bind(self, registry) -> None:
        self.registry = registry

    def execute(self, request: ExecutionRequest) -> ExecutionResult:
        self.task_id = request.task_id
        token = self.registry.begin(request.task_id, request.step_id)
        self.started.set()
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            if token.cancelled:
                self.observed = True
                break
            time.sleep(0.01)
        self.registry.end(request.task_id, request.step_id)
        return ExecutionResult(
            outcome=ExecutionOutcome.UNCONFIRMED,
            error_code="cancelled",
            message="执行已取消",
        )


class StoreCancellingExecutor:
    """执行中途把 store 里的任务状态改成 CANCELLED，模拟并发的取消请求。"""

    def __init__(self, store=None) -> None:
        self.store = store

    def execute(self, request: ExecutionRequest) -> ExecutionResult:
        task = self.store.get(request.task_id)
        task.status = TaskStatus.CANCELLED
        self.store.save(task)
        return ExecutionResult(outcome=ExecutionOutcome.SUCCEEDED, output={"ok": True})


async def test_event_broker_delivers_from_a_worker_thread() -> None:
    """执行放在工作线程后，publish 会从非事件循环线程调用。"""

    broker = EventBroker()
    task_id = uuid.uuid4()
    async with broker.subscribe(task_id) as queue:
        await asyncio.to_thread(broker.publish, task_id, 7)
        assert await asyncio.wait_for(queue.get(), timeout=1.0) == 7
        await asyncio.to_thread(broker.publish, task_id, 8)
        assert await asyncio.wait_for(queue.get(), timeout=1.0) == 8


async def test_cancel_is_served_while_a_step_is_executing() -> None:
    """执行进行中发出的取消请求要立刻返回，并把信号交给执行线程。"""

    executor = WaitingExecutor()
    container = build_container(
        _settings(),
        flows=[DIRECT_FLOW],
        executor=executor,
        router=KeywordIntentRouter(),
    )
    executor.bind(container.cancellations)
    session = container.signer.issue()
    transport = ASGITransport(app=create_app(container=container))
    try:
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test", headers={SESSION_HEADER: session}
        ) as client:
            running = asyncio.create_task(
                client.post(
                    "/api/tasks",
                    json={
                        "conversation_id": "conversation-1",
                        "input_text": "我要办理测试事项",
                        "flow_id": "test.direct",
                    },
                )
            )
            # 执行线程已经开跑，但请求还没返回。
            assert await asyncio.to_thread(executor.started.wait, 2.0)
            assert executor.task_id is not None

            started = time.monotonic()
            cancelled = await asyncio.wait_for(
                client.post(f"/api/tasks/{executor.task_id}/cancel"), timeout=2.0
            )
            elapsed = time.monotonic() - started

            assert cancelled.status_code == 200, cancelled.text
            assert elapsed < 1.0, "取消请求被正在跑的执行堵在事件循环外了"

            created = await asyncio.wait_for(running, timeout=5.0)
            assert created.status_code == 201, created.text
            assert executor.observed is True, "执行侧没收到取消信号"
            # 一次迟到的执行结果不能把已取消的任务改回完成或未确认。
            assert created.json()["status"] == TaskStatus.CANCELLED.value
            assert container.store.get(executor.task_id).status is TaskStatus.CANCELLED
    finally:
        container.close()


def test_late_result_does_not_overwrite_a_cancelled_task() -> None:
    executor = StoreCancellingExecutor()
    container = build_container(
        _settings(),
        flows=[DIRECT_FLOW],
        executor=executor,
        router=KeywordIntentRouter(),
    )
    executor.store = container.store
    try:
        task = container.tasks.create_task(
            TaskCreateRequest(
                conversation_id="conversation-1",
                input_text="我要办理测试事项",
                flow_id="test.direct",
            ),
            "subject-1",
        )
        assert task.status is TaskStatus.CANCELLED
        assert container.store.get(task.task_id).status is TaskStatus.CANCELLED
        types = [event.type for event in container.store.events(task.task_id)]
        assert EventType.COMPLETED not in types
        assert EventType.OUTCOME_UNCONFIRMED not in types
    finally:
        container.close()
