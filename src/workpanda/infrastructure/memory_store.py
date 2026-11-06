"""仅用于开发和测试的内存存储实现。"""

from collections import defaultdict
from collections.abc import Sequence
from threading import Lock
from typing import Any
from uuid import UUID

from workpanda.domain.confirmation import Confirmation, ConfirmationError
from workpanda.domain.models import EventType, Task, TaskEvent, TaskStatus, utc_now
from workpanda.domain.repository import TaskNotFoundError


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
