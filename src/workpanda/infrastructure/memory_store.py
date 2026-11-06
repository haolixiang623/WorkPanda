"""仅用于开发和测试的内存 Task Store。"""

from collections import defaultdict
from collections.abc import Iterable
from threading import Lock
from uuid import UUID

from workpanda.domain.models import Task, TaskEvent
from workpanda.domain.repository import TaskNotFoundError, TaskStore


class InMemoryTaskStore(TaskStore):
    def __init__(self) -> None:
        self._tasks: dict[UUID, Task] = {}
        self._events: dict[UUID, list[TaskEvent]] = defaultdict(list)
        self._lock = Lock()

    def create(self, task: Task) -> Task:
        with self._lock:
            self._tasks[task.task_id] = task
        return task

    def get(self, task_id: UUID) -> Task:
        with self._lock:
            try:
                return self._tasks[task_id]
            except KeyError as exc:
                raise TaskNotFoundError(str(task_id)) from exc

    def save(self, task: Task) -> Task:
        with self._lock:
            if task.task_id not in self._tasks:
                raise TaskNotFoundError(str(task.task_id))
            self._tasks[task.task_id] = task
        return task

    def append_event(self, event: TaskEvent) -> TaskEvent:
        with self._lock:
            self._events[event.task_id].append(event)
        return event

    def events(self, task_id: UUID, after_sequence: int = 0) -> Iterable[TaskEvent]:
        with self._lock:
            return tuple(
                event
                for event in self._events.get(task_id, ())
                if event.sequence > after_sequence
            )
