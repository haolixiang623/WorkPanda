"""仅用于开发和测试的内存存储实现。"""

from collections import defaultdict
from collections.abc import Sequence
from dataclasses import replace
from threading import Lock
from typing import Any
from uuid import UUID

from workpanda.domain.confirmation import Confirmation, ConfirmationError
from workpanda.domain.flow import (
    FlowDefinition,
    FlowRelease,
    FlowReleaseMissing,
    FlowVersionExists,
    require_actor,
    validate_flow,
)
from workpanda.domain.models import EventType, Task, TaskEvent, TaskStatus, utc_now
from workpanda.domain.paging import (
    EventPage,
    EventQuery,
    TaskPage,
    TaskQuery,
    canonical_stamp,
    encode_token,
    event_matches,
    task_matches,
    task_sort_key,
)
from workpanda.domain.repository import PendingEvent, StaleTaskState, TaskNotFoundError


class InMemoryTaskStore:
    def __init__(self) -> None:
        self._tasks: dict[UUID, Task] = {}
        self._events: dict[UUID, list[TaskEvent]] = defaultdict(list)
        self._lock = Lock()

    def create(self, task: Task) -> Task:
        with self._lock:
            self._tasks[task.task_id] = task.model_copy(deep=True)
        return task

    def get(self, task_id: UUID) -> Task:
        with self._lock:
            try:
                return self._tasks[task_id].model_copy(deep=True)
            except KeyError as exc:
                raise TaskNotFoundError(str(task_id)) from exc

    def save(self, task: Task) -> Task:
        with self._lock:
            if task.task_id not in self._tasks:
                raise TaskNotFoundError(str(task.task_id))
            self._tasks[task.task_id] = task.model_copy(deep=True)
        return task

    def commit(
        self,
        task: Task,
        events: Sequence[PendingEvent] = (),
        *,
        expected_status: TaskStatus | None = None,
    ) -> Sequence[TaskEvent]:
        with self._lock:
            current = self._tasks.get(task.task_id)
            if current is None:
                raise TaskNotFoundError(str(task.task_id))
            # 校验必须早于任何写入：状态不符时连一条事件都不能留下。
            if expected_status is not None and current.status is not expected_status:
                raise StaleTaskState(
                    f"task {task.task_id} is {current.status.value}, "
                    f"expected {expected_status.value}"
                )
            stream = self._events[task.task_id]
            sequence = len(stream)
            written: list[TaskEvent] = []
            for pending in events:
                sequence += 1
                event = TaskEvent(
                    task_id=task.task_id,
                    type=pending.type,
                    status=pending.status,
                    sequence=sequence,
                    data=dict(pending.data),
                )
                stream.append(event)
                written.append(event)
            self._tasks[task.task_id] = task.model_copy(deep=True)
        return tuple(written)

    def append_event(
        self,
        task_id: UUID,
        event_type: EventType,
        status: TaskStatus,
        data: dict[str, Any] | None = None,
    ) -> TaskEvent:
        with self._lock:
            events = self._events[task_id]
            event = TaskEvent(
                task_id=task_id,
                type=event_type,
                status=status,
                sequence=len(events) + 1,
                data=data or {},
            )
            events.append(event)
        return event

    def events(self, task_id: UUID, after_sequence: int = 0) -> Sequence[TaskEvent]:
        with self._lock:
            return tuple(
                event for event in self._events.get(task_id, ()) if event.sequence > after_sequence
            )

    def list_tasks(self, *, limit: int = 50, status: TaskStatus | None = None) -> Sequence[Task]:
        with self._lock:
            tasks = [
                task.model_copy(deep=True)
                for task in self._tasks.values()
                if status is None or task.status is status
            ]
        tasks.sort(key=lambda task: task.created_at, reverse=True)
        return tuple(tasks[:limit])

    def query_tasks(self, query: TaskQuery) -> TaskPage:
        with self._lock:
            tasks = [task.model_copy(deep=True) for task in self._tasks.values()]

        selected = [task for task in tasks if task_matches(task, query)]
        # 新的在前：排障时最关心最近发生了什么。
        selected.sort(key=task_sort_key, reverse=True)

        limit = max(0, query.limit)
        page = selected[:limit]
        next_cursor = None
        # 只有当"后面确实还有"时才给游标，否则调用方会为了一个空页多跑一轮。
        if len(selected) > limit and page:
            last = page[-1]
            next_cursor = encode_token(
                "tasks", [canonical_stamp(last.created_at), str(last.task_id)]
            )
        return TaskPage(items=tuple(page), next_cursor=next_cursor)

    def query_events(self, query: EventQuery) -> EventPage:
        with self._lock:
            events = [
                event.model_copy(deep=True) for event in self._events.get(query.task_id, ())
            ]

        selected = [event for event in events if event_matches(event, query)]
        # 事件按序号正序：回放一次执行时，先发生的事要先看到。
        selected.sort(key=lambda event: event.sequence)

        limit = max(0, query.limit)
        page = selected[:limit]
        next_cursor = str(page[-1].sequence) if len(selected) > limit and page else None
        return EventPage(items=tuple(page), next_cursor=next_cursor)


class InMemoryConfirmationStore:
    def __init__(self) -> None:
        self._items: list[Confirmation] = []
        self._lock = Lock()

    def issue(self, confirmation: Confirmation) -> Confirmation:
        with self._lock:
            self._items.append(confirmation)
        return confirmation

    def latest(self, task_id: UUID, step_id: str) -> Confirmation | None:
        with self._lock:
            for item in reversed(self._items):
                if item.task_id == task_id and item.step_id == step_id:
                    return item
        return None

    def revoke(self, task_id: UUID, step_id: str) -> None:
        now = utc_now()
        with self._lock:
            for item in self._items:
                if (
                    item.task_id == task_id
                    and item.step_id == step_id
                    and item.consumed_at is None
                ):
                    item.consumed_at = now

    def consume(self, token: str) -> Confirmation:
        now = utc_now()
        with self._lock:
            for item in self._items:
                if item.token != token:
                    continue
                if item.consumed_at is not None:
                    raise ConfirmationError("confirmation token already used")
                if item.expires_at <= now:
                    raise ConfirmationError("confirmation token expired")
                item.consumed_at = now
                return item
        raise ConfirmationError("confirmation token not found")


class InMemoryFlowStore:
    """进程内流程版本库，语义与 `SqliteFlowStore` 一致（重启即丢）。

    只用于 `store_backend=memory` 与测试。两条表在内存里就是两个 dict：
    版本内容与发布指针。指针行带署名，停用是打标记而不是删键——与 SQLite 实现
    同一套语义，否则测试会用另一套真相骗过自己。
    """

    def __init__(self) -> None:
        self._versions: dict[tuple[str, str], FlowDefinition] = {}
        self._pointers: dict[str, FlowRelease] = {}
        self._lock = Lock()

    def close(self) -> None:
        """与 SQLite 实现同形，便于容器统一登记关闭动作（无资源可释放）。"""

    def add_version(self, flow: FlowDefinition) -> None:
        validate_flow(flow)
        key = (flow.flow_id, flow.version)
        with self._lock:
            if key in self._versions:
                raise FlowVersionExists(
                    f"flow {flow.flow_id!r} version {flow.version!r} already exists"
                )
            self._versions[key] = flow.model_copy()

    def publish(self, flow_id: str, version: str, *, actor: str) -> None:
        actor = require_actor(actor)
        with self._lock:
            if (flow_id, version) not in self._versions:
                raise FlowReleaseMissing(
                    f"flow {flow_id!r} version {version!r} is not registered; "
                    "先写入版本再发布"
                )
            self._pointers[flow_id] = FlowRelease(
                flow_id=flow_id,
                version=version,
                published=True,
                published_by=actor,
                updated_at=canonical_stamp(utc_now()),
                disabled_by=None,
            )

    def disable(self, flow_id: str, *, actor: str) -> None:
        actor = require_actor(actor)
        with self._lock:
            current = self._pointers.get(flow_id)
            if current is None or current.disabled_by is not None:
                return
            self._pointers[flow_id] = replace(
                current, published=False, disabled_by=actor
            )

    def published_versions(self) -> dict[str, str]:
        with self._lock:
            return {
                flow_id: release.version
                for flow_id, release in self._pointers.items()
                if release.published
            }

    def releases(self) -> list[FlowRelease]:
        with self._lock:
            return [self._pointers[flow_id] for flow_id in sorted(self._pointers)]

    def list_versions(self, flow_id: str | None = None) -> list[FlowDefinition]:
        with self._lock:
            items = [
                flow.model_copy()
                for (registered_id, _), flow in sorted(self._versions.items())
                if flow_id is None or registered_id == flow_id
            ]
        return items
