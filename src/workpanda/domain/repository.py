"""Task Store 与确认凭证存储的抽象。

业务任务状态以 Task Store 为权威；Runtime checkpoint 未来只用于恢复 Agent 会话。

`commit` 是取消与执行结果之间的仲裁点：两者都可能在同一瞬间落盘，只有先抢到
状态条件写的那一边生效，迟到的一边连事件一起丢掉。见 docs/HARNESS.md 第 17 节。
"""

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol
from uuid import UUID

from workpanda.domain.confirmation import Confirmation
from workpanda.domain.models import EventType, Task, TaskEvent, TaskStatus


class TaskNotFoundError(LookupError):
    """任务不存在。"""


class StaleTaskState(RuntimeError):
    """条件写未生效：库里的状态已不是调用方读到并据此决策的那一版。

    这不是"写失败"，而是"另一个写者先赢了"。调用方必须放弃这一整批结果并
    重新读取权威状态，绝不能拿旧快照重试覆盖。
    """


@dataclass(frozen=True)
class PendingEvent:
    """与任务状态一起原子落盘的事件。

    `status` 是事件落库时的状态快照，由调用方显式给出：同一次提交里的不同
    事件可能对应不同阶段（例如 `execution_failed` 早于 `FAILED`）。统一取
    最终状态会悄悄改写事件流语义，所以不能由存储层代猜。
    """

    type: EventType
    status: TaskStatus
    data: dict[str, Any] = field(default_factory=dict)


class TaskStore(Protocol):
    def create(self, task: Task) -> Task: ...

    def get(self, task_id: UUID) -> Task: ...

    def save(self, task: Task) -> Task: ...

    def commit(
        self,
        task: Task,
        events: Sequence[PendingEvent] = (),
        *,
        expected_status: TaskStatus | None = None,
    ) -> Sequence[TaskEvent]:
        """把「任务状态 + 一批事件」放进同一个事务原子提交。

        `expected_status` 不为 None 时，仅当库中当前状态与之相同才写入；不符则
        整批回滚（一行状态、一条事件都不落）并抛 `StaleTaskState`。返回已落盘
        的事件，供调用方在提交成功后发布推送。

        实现必须让「状态校验 → 事件追加 → 状态写入」对并发写者互斥：SQLite
        用带 `status` 条件的 UPDATE 抢写锁，不能先 SELECT 再 UPDATE——那中间
        恰好是另一个进程插进来的窗口。
        """
        ...

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

    def list_tasks(self, *, limit: int = 50, status: TaskStatus | None = None) -> Sequence[Task]:
        """按创建时间倒序列出任务，供运维控制台只读查询。"""
        ...


class ConfirmationStore(Protocol):
    def issue(self, confirmation: Confirmation) -> Confirmation: ...

    def latest(self, task_id: UUID, step_id: str) -> Confirmation | None: ...

    def revoke(self, task_id: UUID, step_id: str) -> None:
        """作废该任务该步骤尚未使用的凭证（参数变化或用户拒绝时调用）。"""
        ...

    def consume(self, token: str) -> Confirmation:
        """一次性消费凭证；已消费、过期或不存在时抛出 ConfirmationError。"""
        ...
