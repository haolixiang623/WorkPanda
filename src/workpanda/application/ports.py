"""应用层端口：模型、执行器和事件发布都以协议接入，便于替换和测试。"""

from enum import StrEnum
from typing import Any, Protocol
from uuid import UUID

from pydantic import BaseModel, Field


class IntentMatch(BaseModel):
    intent_id: str
    flow_id: str
    flow_version: str
    confidence: float = Field(ge=0.0, le=1.0)
    slots: dict[str, str] = Field(default_factory=dict)


class IntentRouter(Protocol):
    """只做意图匹配建议；不得选择未注册流程或跳过澄清。"""

    def route(self, text: str) -> IntentMatch | None: ...


class ExecutionOutcome(StrEnum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    # 外部系统可能已经执行，但本地无法确认；禁止自动重试。
    UNCONFIRMED = "unconfirmed"


class ExecutionRequest(BaseModel):
    task_id: UUID
    flow_id: str
    flow_version: str
    step_id: str
    tool: str
    arguments: dict[str, str] = Field(default_factory=dict)


class ExecutionResult(BaseModel):
    outcome: ExecutionOutcome
    output: dict[str, Any] = Field(default_factory=dict)
    error_code: str | None = None
    message: str | None = None


class Executor(Protocol):
    """受控工具执行器。真实实现应校验工具白名单、域名、超时和会话隔离。"""

    def execute(self, request: ExecutionRequest) -> ExecutionResult: ...


class EventPublisher(Protocol):
    def publish(self, task_id: UUID, sequence: int) -> None: ...
