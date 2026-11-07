"""条件写：取消与迟到结果之间的仲裁。

文档第 16 节曾留了一个"守卫读状态 → 结果持久化"的极窄窗口：在那段里收到
取消，结果仍会先落一条记录并短暂覆盖状态。这一组盯着它，也盯着条件写本身
的原子性——状态与事件必须同生共死，不能出现"状态没写成、事件却留下了"。
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from tests.support import CONFIRM_FLOW, DIRECT_FLOW
from workpanda.application.errors import TaskConflict
from workpanda.application.ports import ExecutionOutcome, ExecutionRequest, ExecutionResult
from workpanda.bootstrap import Container, build_container
from workpanda.config import Settings
from workpanda.domain.models import (
    EventType,
    Task,
    TaskCreateRequest,
    TaskStatus,
    utc_now,
)
from workpanda.domain.repository import PendingEvent, StaleTaskState, TaskNotFoundError
from workpanda.infrastructure.adapters import KeywordIntentRouter
from workpanda.infrastructure.memory_store import InMemoryTaskStore
from workpanda.infrastructure.sqlite_store import SqliteStore


def _task(status: TaskStatus = TaskStatus.CREATED) -> Task:
    return Task(
        conversation_id="conversation-1",
        user_ref="subject-1",
        flow_id="test.direct",
        flow_version="1",
        status=status,
    )


@pytest.fixture(params=["memory", "sqlite"])
def task_store(request: pytest.FixtureRequest, workdir: Path) -> Iterator[Any]:
    """两种后端跑同一组契约：内存只挡同进程，SQLite 要能挡跨连接。"""
    if request.param == "memory":
        yield InMemoryTaskStore()
        return
    store = SqliteStore(workdir / "cas.db")
    try:
        yield store
    finally:
        store.close()


# ---------- 存储层契约 ----------


def test_commit_writes_status_and_events_in_one_shot(task_store: Any) -> None:
    task = _task()
    task_store.create(task)
    written = task_store.commit(
        task.model_copy(update={"status": TaskStatus.RUNNING}),
        [
            PendingEvent(type=EventType.ACCEPTED, status=TaskStatus.CREATED, data={"flow": "x"}),
            PendingEvent(type=EventType.STEP_STARTED, status=TaskStatus.RUNNING),
        ],
        expected_status=TaskStatus.CREATED,
    )
    assert [event.sequence for event in written] == [1, 2]
    # 事件状态是调用方给的快照，不能被存储层统一改成最终状态。
    assert [event.status for event in written] == [TaskStatus.CREATED, TaskStatus.RUNNING]
    assert written[0].data == {"flow": "x"}
    assert task_store.get(task.task_id).status is TaskStatus.RUNNING
    assert [event.type for event in task_store.events(task.task_id)] == [
        EventType.ACCEPTED,
        EventType.STEP_STARTED,
    ]


def test_commit_rejects_stale_status_without_writing_anything(task_store: Any) -> None:
    task = _task()
    task_store.create(task)
    # 另一个写者先把状态改了（等价于取消方抢先成功）。
    task_store.save(task.model_copy(update={"status": TaskStatus.RUNNING}))

    with pytest.raises(StaleTaskState):
        task_store.commit(
            task.model_copy(update={"status": TaskStatus.COMPLETED}),
            [PendingEvent(type=EventType.COMPLETED, status=TaskStatus.COMPLETED)],
            expected_status=TaskStatus.CREATED,
        )
    assert task_store.get(task.task_id).status is TaskStatus.RUNNING
    assert list(task_store.events(task.task_id)) == []


def test_commit_without_expectation_is_unconditional(task_store: Any) -> None:
    task = _task()
    task_store.create(task)
    task_store.save(task.model_copy(update={"status": TaskStatus.RUNNING}))
    written = task_store.commit(
        task.model_copy(update={"status": TaskStatus.COMPLETED}),
        [PendingEvent(type=EventType.COMPLETED, status=TaskStatus.COMPLETED)],
    )
    assert len(written) == 1
    assert task_store.get(task.task_id).status is TaskStatus.COMPLETED


def test_commit_on_missing_task_raises(task_store: Any) -> None:
    with pytest.raises(TaskNotFoundError):
        task_store.commit(_task(), expected_status=TaskStatus.CREATED)


def test_sqlite_commit_arbitrates_across_connections(workdir: Path) -> None:
    """两个连接指向同一文件：先写的赢，后写的整批丢弃，且不留下半条事件。"""

    path = workdir / "shared.db"
    first = SqliteStore(path)
    task = _task()
    first.create(task)
    second = SqliteStore(path)
    try:
        second.commit(
            task.model_copy(update={"status": TaskStatus.RUNNING}),
            [PendingEvent(type=EventType.STEP_STARTED, status=TaskStatus.RUNNING)],
            expected_status=TaskStatus.CREATED,
        )
        assert first.get(task.task_id).status is TaskStatus.RUNNING
        before = [event.type for event in first.events(task.task_id)]

        with pytest.raises(StaleTaskState):
            first.commit(
                task.model_copy(update={"status": TaskStatus.COMPLETED}),
                [PendingEvent(type=EventType.COMPLETED, status=TaskStatus.COMPLETED)],
                expected_status=TaskStatus.CREATED,
            )
        assert first.get(task.task_id).status is TaskStatus.RUNNING
        assert [event.type for event in first.events(task.task_id)] == before
    finally:
        first.close()
        second.close()


# ---------- 流程集成：守卫之后才来的取消 ----------


class FixedExecutor:
    def __init__(self, result: ExecutionResult) -> None:
        self.result = result

    def execute(self, request: ExecutionRequest) -> ExecutionResult:
        return self.result


class RaceBeforeCommitStore:
    """在第一次条件提交发生前，让另一个写者先落一个终态。

    这精确复刻了旧实现顶不住的窗口：`_execute` 已经读过状态并据此决定继续，
    结果还没落盘时，取消方（或另一个执行者）抢先改写了库里的状态。
    """

    def __init__(self, inner: Any, winner: TaskStatus) -> None:
        self.inner = inner
        self.winner = winner
        self.armed = False
        self.triggered = False

    def __getattr__(self, name: str) -> Any:
        return getattr(self.inner, name)

    def commit(
        self,
        task: Task,
        events: Any = (),
        *,
        expected_status: TaskStatus | None = None,
    ) -> Any:
        if self.armed and not self.triggered:
            self.triggered = True
            won = self.inner.get(task.task_id)
            won.status = self.winner
            won.updated_at = utc_now()
            self.inner.save(won)
        return self.inner.commit(task, events, expected_status=expected_status)


def _settings() -> Settings:
    return Settings(store_backend="memory", enable_demo_flow=False)


def _create(outcome: ExecutionOutcome, flows: list[Any]) -> tuple[Container, Any]:
    """建容器并把 task store 换成可控的竞态包装。"""
    container = build_container(
        _settings(),
        flows=flows,
        executor=FixedExecutor(
            ExecutionResult(
                outcome=outcome,
                output={"ok": True},
                error_code=None if outcome is ExecutionOutcome.SUCCEEDED else "test_failure",
                message=None if outcome is ExecutionOutcome.SUCCEEDED else "测试失败",
            )
        ),
        router=KeywordIntentRouter(),
    )
    return container, container.store


def _attach(container: Container, inner: Any, winner: TaskStatus) -> RaceBeforeCommitStore:
    wrapper = RaceBeforeCommitStore(inner, winner)
    wrapper.armed = True
    container.store = wrapper
    container.tasks.store = wrapper
    container.tasks.runner.store = wrapper
    return wrapper


def _request(flow_id: str) -> TaskCreateRequest:
    return TaskCreateRequest(
        conversation_id="conversation-1",
        input_text="我要办理测试事项",
        flow_id=flow_id,
    )


@pytest.mark.parametrize(
    "outcome, forbidden",
    [
        (ExecutionOutcome.SUCCEEDED, (EventType.COMPLETED,)),
        (ExecutionOutcome.UNCONFIRMED, (EventType.OUTCOME_UNCONFIRMED,)),
        (ExecutionOutcome.FAILED, (EventType.EXECUTION_FAILED, EventType.FAILED)),
    ],
)
def test_late_result_window_never_publishes_a_result(
    outcome: ExecutionOutcome, forbidden: tuple[EventType, ...]
) -> None:
    container, inner = _create(outcome, [DIRECT_FLOW])
    wrapper = _attach(container, inner, TaskStatus.CANCELLED)
    try:
        task = container.tasks.create_task(_request("test.direct"), "subject-1")
        assert wrapper.triggered is True, "竞态没有被触发，用例没有覆盖到目标窗口"
        assert task.status is TaskStatus.CANCELLED
        assert container.store.get(task.task_id).status is TaskStatus.CANCELLED
        types = [event.type for event in container.store.events(task.task_id)]
        assert all(kind not in types for kind in forbidden), types
    finally:
        container.close()


def test_cancel_that_loses_the_race_reports_conflict() -> None:
    """执行侧先写出终态时，取消不能反过来把完成覆盖掉。"""

    container, inner = _create(ExecutionOutcome.SUCCEEDED, [CONFIRM_FLOW])
    wrapper = RaceBeforeCommitStore(inner, TaskStatus.COMPLETED)
    container.store = wrapper
    container.tasks.store = wrapper
    container.tasks.runner.store = wrapper
    try:
        created = container.tasks.create_task(_request("test.confirm"), "subject-1")
        assert created.status is TaskStatus.WAITING_FOR_USER
        wrapper.armed = True

        with pytest.raises(TaskConflict):
            container.tasks.cancel(created.task_id, "subject-1")
        assert wrapper.triggered is True
        assert container.store.get(created.task_id).status is TaskStatus.COMPLETED
        assert EventType.CANCELLED not in [
            event.type for event in container.store.events(created.task_id)
        ]
    finally:
        container.close()


def test_normal_completion_still_commits_status_and_event() -> None:
    container, _ = _create(ExecutionOutcome.SUCCEEDED, [DIRECT_FLOW])
    try:
        task = container.tasks.create_task(_request("test.direct"), "subject-1")
        assert task.status is TaskStatus.COMPLETED
        events = list(container.store.events(task.task_id))
        assert EventType.COMPLETED in [event.type for event in events]
        # 条件提交分配的顺序号要接在即时事件之后，不能重号或跳号。
        assert [event.sequence for event in events] == list(range(1, len(events) + 1))
    finally:
        container.close()


def test_cancel_commits_status_and_event_together() -> None:
    container, _ = _create(ExecutionOutcome.SUCCEEDED, [CONFIRM_FLOW])
    try:
        created = container.tasks.create_task(_request("test.confirm"), "subject-1")
        cancelled = container.tasks.cancel(created.task_id, "subject-1")
        assert cancelled.status is TaskStatus.CANCELLED
        assert container.store.get(created.task_id).status is TaskStatus.CANCELLED
        assert EventType.CANCELLED in [
            event.type for event in container.store.events(created.task_id)
        ]
    finally:
        container.close()
