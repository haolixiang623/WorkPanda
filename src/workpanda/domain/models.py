"""业务无关的任务、事件与状态模型。"""

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4

from pydantic import BaseModel, Field


def utc_now() -> datetime:
    return datetime.now(UTC)


class TaskStatus(StrEnum):
    CREATED = "created"
    RUNNING = "running"
    WAITING_FOR_USER = "waiting_for_user"
    WAITING_FOR_CONFIRMATION = "waiting_for_confirmation"
    OUTCOME_UNCONFIRMED = "outcome_unconfirmed"
    HANDOFF_REQUIRED = "handoff_required"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class EventType(StrEnum):
    ACCEPTED = "accepted"
    STEP_STARTED = "step_started"
    STEP_COMPLETED = "step_completed"
    CLARIFICATION_REQUIRED = "clarification_required"
    SLOTS_UPDATED = "slots_updated"
    CONFIRMATION_REQUIRED = "confirmation_required"
    CONFIRMATION_ACCEPTED = "confirmation_accepted"
    CONFIRMATION_DECLINED = "confirmation_declined"
    EXECUTION_SUCCEEDED = "execution_succeeded"
    EXECUTION_FAILED = "execution_failed"
    OUTCOME_UNCONFIRMED = "outcome_unconfirmed"
    HANDOFF_REQUIRED = "handoff_required"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class Task(BaseModel):
    task_id: UUID = Field(default_factory=uuid4)
    conversation_id: str = Field(min_length=1, max_length=128)
    user_ref: str | None = Field(default=None, max_length=128)
    flow_id: str = Field(min_length=1, max_length=128)
    flow_version: str = Field(min_length=1, max_length=64)
    status: TaskStatus = TaskStatus.CREATED
    current_step: str | None = None
    completed_steps: list[str] = Field(default_factory=list)
    slots: dict[str, str] = Field(default_factory=dict)
    pending_fields: list[str] = Field(default_factory=list)
    result: dict[str, Any] | None = None
    failure_code: str | None = None
    failure_message: str | None = None
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)

    @property
    def is_terminal(self) -> bool:
        return self.status in {
            TaskStatus.COMPLETED,
            TaskStatus.FAILED,
            TaskStatus.CANCELLED,
            TaskStatus.HANDOFF_REQUIRED,
        }


class TaskEvent(BaseModel):
    event_id: UUID = Field(default_factory=uuid4)
    task_id: UUID
    type: EventType
    status: TaskStatus
    sequence: int
    occurred_at: datetime = Field(default_factory=utc_now)
    data: dict[str, Any] = Field(default_factory=dict)


class TaskCreateRequest(BaseModel):
    conversation_id: str = Field(min_length=1, max_length=128)
    input_text: str = Field(min_length=1, max_length=10_000)
    flow_id: str | None = Field(default=None, max_length=128)
    flow_version: str | None = Field(default=None, max_length=64)
    user_ref: str | None = Field(default=None, max_length=128)
    slots: dict[str, str] = Field(default_factory=dict)


class TaskAnswerRequest(BaseModel):
    values: dict[str, str] = Field(min_length=1)


class TaskConfirmRequest(BaseModel):
    step_id: str = Field(min_length=1, max_length=128)
    accepted: bool = True


class TaskView(BaseModel):
    """面向客户端的任务视图，不暴露内部存储细节。"""

    task_id: UUID
    conversation_id: str
    flow_id: str
    flow_version: str
    status: TaskStatus
    current_step: str | None
    completed_steps: list[str]
    pending_fields: list[str]
    result: dict[str, Any] | None
    failure_code: str | None
    failure_message: str | None
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_task(cls, task: Task) -> "TaskView":
        return cls(
            task_id=task.task_id,
            conversation_id=task.conversation_id,
            flow_id=task.flow_id,
            flow_version=task.flow_version,
            status=task.status,
            current_step=task.current_step,
            completed_steps=list(task.completed_steps),
            pending_fields=task.pending_fields,
            result=task.result,
            failure_code=task.failure_code,
            failure_message=task.failure_message,
            created_at=task.created_at,
            updated_at=task.updated_at,
        )
