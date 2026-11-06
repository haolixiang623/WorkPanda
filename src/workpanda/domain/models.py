"""业务无关的任务、流程和事件模型。"""

from datetime import datetime, timezone
from enum import StrEnum
from uuid import UUID, uuid4

from pydantic import BaseModel, Field


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class TaskStatus(StrEnum):
    CREATED = "created"
    RUNNING = "running"
    WAITING_FOR_USER = "waiting_for_user"
    CANCELLED = "cancelled"
    COMPLETED = "completed"
    FAILED = "failed"
    OUTCOME_UNCONFIRMED = "outcome_unconfirmed"


class EventType(StrEnum):
    ACCEPTED = "accepted"
    STEP_STARTED = "step_started"
    USER_ACTION_REQUIRED = "user_action_required"
    STEP_COMPLETED = "step_completed"
    CANCELLED = "cancelled"
    FAILED = "failed"


class TaskCreate(BaseModel):
    conversation_id: str = Field(min_length=1, max_length=128)
    flow_id: str = Field(min_length=1, max_length=128)
    flow_version: str = Field(min_length=1, max_length=64)
    input_text: str | None = Field(default=None, max_length=10_000)


class Task(BaseModel):
    task_id: UUID = Field(default_factory=uuid4)
    conversation_id: str
    flow_id: str
    flow_version: str
    status: TaskStatus = TaskStatus.CREATED
    input_text: str | None = None
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class TaskEvent(BaseModel):
    event_id: UUID = Field(default_factory=uuid4)
    task_id: UUID
    type: EventType
    status: TaskStatus
    sequence: int
    occurred_at: datetime = Field(default_factory=utc_now)
    data: dict[str, str] = Field(default_factory=dict)
