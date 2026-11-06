"""任务应用服务；不包含具体业务事项。"""

from datetime import datetime, timezone
from uuid import UUID

from workpanda.domain.models import EventType, Task, TaskCreate, TaskEvent, TaskStatus
from workpanda.domain.repository import TaskStore


class InvalidTaskTransition(ValueError):
    """任务状态转换不合法。"""


class TaskService:
    def __init__(self, store: TaskStore) -> None:
        self.store = store

    def create_task(self, command: TaskCreate) -> Task:
        task = self.store.create(
            Task(
                conversation_id=command.conversation_id,
                flow_id=command.flow_id,
                flow_version=command.flow_version,
                input_text=command.input_text,
            )
        )
        self._emit(task, EventType.ACCEPTED)
        return task

    def get_task(self, task_id: UUID) -> Task:
        return self.store.get(task_id)

    def cancel_task(self, task_id: UUID) -> Task:
        task = self.store.get(task_id)
        if task.status in {TaskStatus.COMPLETED, TaskStatus.CANCELLED, TaskStatus.FAILED}:
            raise InvalidTaskTransition(f"cannot cancel task in {task.status} state")
        task.status = TaskStatus.CANCELLED
        task.updated_at = datetime.now(timezone.utc)
        self.store.save(task)
        self._emit(task, EventType.CANCELLED)
        return task

    def events(self, task_id: UUID, after_sequence: int = 0) -> tuple[TaskEvent, ...]:
        self.store.get(task_id)
        return tuple(self.store.events(task_id, after_sequence))

    def _emit(self, task: Task, event_type: EventType) -> TaskEvent:
        sequence = len(tuple(self.store.events(task.task_id))) + 1
        return self.store.append_event(
            TaskEvent(
                task_id=task.task_id,
                type=event_type,
                status=task.status,
                sequence=sequence,
            )
        )
