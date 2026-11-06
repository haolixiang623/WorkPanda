"""Task Store 的最小抽象；后续可替换为 PostgreSQL 等持久化实现。"""

from collections.abc import Iterable
from uuid import UUID

from workpanda.domain.models import Task, TaskEvent


class TaskNotFoundError(LookupError):
    """任务不存在。"""


class TaskStore:
    """任务和事件存储协议。"""

    def create(self, task: Task) -> Task:
        raise NotImplementedError

    def get(self, task_id: UUID) -> Task:
        raise NotImplementedError

    def save(self, task: Task) -> Task:
        raise NotImplementedError

    def append_event(self, event: TaskEvent) -> TaskEvent:
        raise NotImplementedError

    def events(self, task_id: UUID, after_sequence: int = 0) -> Iterable[TaskEvent]:
        raise NotImplementedError
