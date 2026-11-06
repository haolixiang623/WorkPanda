"""受控执行的公共数据结构。

这里只定义"模型调用工具"这件事的公共语言：工具声明、调用、结果、事件。
不含任何业务事项，也不绑定任何模型厂商。
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field

# 单次工具返回最多保留的字符数，避免把大响应写进任务事件。
MAX_TOOL_CONTENT = 4000


def utc_now() -> datetime:
    return datetime.now(UTC)


class RiskLevel(StrEnum):
    """工具风险等级。一次执行整体取最高等级。"""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


_RISK_ORDER: dict[RiskLevel, int] = {
    RiskLevel.LOW: 0,
    RiskLevel.MEDIUM: 1,
    RiskLevel.HIGH: 2,
}


def max_risk(levels: Iterable[RiskLevel]) -> RiskLevel:
    """按最高等级归并，用于判断一次执行是否触碰高风险动作。"""

    ordered = list(levels)
    if not ordered:
        return RiskLevel.LOW
    return max(ordered, key=lambda level: _RISK_ORDER[level])


class ToolSpec(BaseModel):
    """对外声明一个工具。模型只能看到被授权的子集。"""

    # 工具名会作为 function name 下发。OpenAI 兼容层只接受 ASCII 字母、数字、下划线、
    # 连字符（实测带点号会被上游直接 400 拒绝），因此这里用下划线而非点号分隔。
    name: str = Field(min_length=1, max_length=64, pattern=r"^[a-zA-Z][a-zA-Z0-9_-]{0,63}$")
    description: str = Field(min_length=1, max_length=500)
    # JSON Schema（object）。harness 只做轻量校验，不做完整 JSON Schema 实现。
    parameters: dict[str, Any] = Field(default_factory=dict)
    risk: RiskLevel = RiskLevel.LOW
    # 重复调用是否安全。不幂等的工具在同一任务内应被限制次数。
    idempotent: bool = True
    timeout_seconds: float = Field(default=10.0, gt=0, le=120)


class ToolCall(BaseModel):
    """模型请求的一次工具调用。"""

    call_id: str = Field(min_length=1, max_length=128)
    name: str = Field(min_length=1, max_length=64)
    arguments: dict[str, Any] = Field(default_factory=dict)


class ToolOutcome(StrEnum):
    OK = "ok"
    # 工具执行了但返回失败。
    ERROR = "error"
    # 未执行：未授权、未注册或参数不合法。
    BLOCKED = "blocked"
    TIMEOUT = "timeout"


class ToolCallRecord(BaseModel):
    """一次工具调用的完整留痕，是审计与复盘的唯一依据。"""

    call_id: str
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    outcome: ToolOutcome
    content: str = ""
    error_code: str | None = None
    duration_ms: int = 0
    risk: RiskLevel = RiskLevel.LOW

    @property
    def succeeded(self) -> bool:
        return self.outcome is ToolOutcome.OK


class MessageRole(StrEnum):
    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"


class HarnessMessage(BaseModel):
    """与厂商无关的对话消息。"""

    role: MessageRole
    content: str = ""
    tool_call_id: str | None = None
    name: str | None = None
    tool_calls: list[ToolCall] = Field(default_factory=list)


class ModelResponse(BaseModel):
    text: str = ""
    tool_calls: list[ToolCall] = Field(default_factory=list)
    finish_reason: str | None = None
    duration_ms: int = 0


class EventKind(StrEnum):
    MODEL_REQUESTED = "model_requested"
    MODEL_RESPONDED = "model_responded"
    TOOL_COMPLETED = "tool_completed"
    STOPPED = "stopped"


class HarnessEvent(BaseModel):
    """执行过程中的一条可追溯事件。sequence 从 1 开始单调递增。"""

    sequence: int
    kind: EventKind
    at: datetime = Field(default_factory=utc_now)
    data: dict[str, Any] = Field(default_factory=dict)
