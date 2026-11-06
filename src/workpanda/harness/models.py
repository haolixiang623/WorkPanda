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
    # 重复调用是否安全。不幂等的工具在一次执行里只允许成功一次，防止重复副作用。
    idempotent: bool = True
    timeout_seconds: float = Field(default=10.0, gt=0, le=120)
    # 单次执行内最多调用几次。None 表示按 idempotent 推导：
    # 幂等工具交给总调用预算约束，非幂等工具固定为 1。
    max_calls: int | None = Field(default=None, ge=1, le=64)
    # 该工具的参数名里哪些必须脱敏（例如凭据类入参）。
    sensitive_fields: list[str] = Field(default_factory=list)

    @property
    def call_limit(self) -> int | None:
        if self.max_calls is not None:
            return self.max_calls
        return None if self.idempotent else 1


class ToolCall(BaseModel):
    """模型请求的一次工具调用。"""

    call_id: str = Field(min_length=1, max_length=128)
    name: str = Field(min_length=1, max_length=64)
    arguments: dict[str, Any] = Field(default_factory=dict)


class ToolOutcome(StrEnum):
    OK = "ok"
    # 工具执行了但返回失败。
    ERROR = "error"
    # 未执行：未授权、未注册、参数不合法或重复副作用——是闸门拒绝了它。
    BLOCKED = "blocked"
    # 执行未在限期内返回。结果未知，绝不能当失败重试。
    TIMEOUT = "timeout"
    # 未执行，但不是因为闸门拒绝：预算用尽或执行被取消，这次没轮到它。
    # 必须单独留痕，否则事后只能看到"模型要求了 5 件事、做了 3 件"，
    # 却答不出另外 2 件是什么、为什么没做。
    SKIPPED = "skipped"


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
    # 实际尝试次数（含重试）。>1 说明发生过可重试失败。
    attempts: int = 1

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


class TokenUsage(BaseModel):
    """模型服务上报的用量。缺失时不猜测，一律按 0 计。"""

    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0

    @classmethod
    def from_payload(cls, payload: object) -> TokenUsage | None:
        if not isinstance(payload, dict):
            return None

        def as_int(value: object) -> int:
            return value if isinstance(value, int) and value >= 0 else 0

        usage = cls(
            prompt_tokens=as_int(payload.get("prompt_tokens")),
            completion_tokens=as_int(payload.get("completion_tokens")),
            total_tokens=as_int(payload.get("total_tokens")),
        )
        if usage.total_tokens == 0:
            usage.total_tokens = usage.prompt_tokens + usage.completion_tokens
        return usage


class ModelResponse(BaseModel):
    text: str = ""
    tool_calls: list[ToolCall] = Field(default_factory=list)
    finish_reason: str | None = None
    duration_ms: int = 0
    usage: TokenUsage | None = None


class EventKind(StrEnum):
    MODEL_REQUESTED = "model_requested"
    # 瞬时失败后的一次重试。单独成事件，避免"看起来只调了一次模型"。
    MODEL_RETRIED = "model_retried"
    MODEL_RESPONDED = "model_responded"
    # 一轮里有多个工具并发执行。留痕里必须能看出"这几条是同时在跑"，
    # 否则事后按耗时排查会以为它们串行。
    TOOLS_PARALLEL = "tools_parallel"
    TOOL_COMPLETED = "tool_completed"
    # 模型要求过、但因为预算用尽或取消而没有执行的调用。与 BLOCKED 区分开：
    # 前者是"系统拒绝了你"，后者是"这次没轮到"，两种情况的处置完全不同。
    TOOL_SKIPPED = "tool_skipped"
    # 观察到取消请求，以及在哪个边界上停下的。取消也是事实，必须留痕：
    # 否则事后只看到"执行提前结束"，会误判成预算耗尽或模型抖动。
    CANCELLED = "cancelled"
    STOPPED = "stopped"


class HarnessEvent(BaseModel):
    """执行过程中的一条可追溯事件。sequence 从 1 开始单调递增。"""

    sequence: int
    kind: EventKind
    at: datetime = Field(default_factory=utc_now)
    data: dict[str, Any] = Field(default_factory=dict)
