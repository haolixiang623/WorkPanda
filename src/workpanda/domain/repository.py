"""Task Store 与确认凭证存储的抽象。

业务任务状态以 Task Store 为权威；Runtime checkpoint 未来只用于恢复 Agent 会话。
"""

from collections.abc import Sequence
from typing import Any, Protocol
from uuid import UUID

from workpanda.domain.confirmation import Confirmation
from workpanda.domain.models import EventType, Task, TaskEvent, TaskStatus


class TaskNotFoundError(LookupError):
    """任务不存在。"""


class TaskStore(Protocol):
    def create(self, task: Task) -> Task: ...

    def get(self, task_id: UUID) -> Task: ...

    def save(self, task: Task) -> Task: ...

    def append_event(
        self,
        task_id: UUID,
        event_type: EventType,
        status: TaskStatus,
        data: dict[str, Any] | None = None,
    ) -> TaskEvent:
        """追加事件并返回带顺序号的事件；顺序号由存储原子分配。"""
        ...

    def events(self, task_id: UUID, after_sequence: int = 0) -> Sequence[TaskEvent]: ...


class ConfirmationStore(Protocol):
    def issue(self, confirmation: Confirmation) -> Confirmation: ...

    def latest(self, task_id: UUID, step_id: str) -> Confirmation | None: ...

    def revoke(self, task_id: UUID, step_id: str) -> None:
        """作废该任务该步骤尚未使用的凭证（参数变化或用户拒绝时调用）。"""
        ...

    def consume(self, token: str) -> Confirmation:
        """一次性消费凭证；已消费、过期或不存在时抛出 ConfirmationError。"""
        ...
