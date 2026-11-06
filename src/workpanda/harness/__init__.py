"""通用受控执行层（harness）。

边界：本层只解决"如何让模型在受控范围内调用工具并如实报告结果"，
不认识任何业务事项，也不绑定任何模型厂商或 Agent SDK。

对外只暴露一件事：把一次步骤执行交给 `HarnessExecutor`，
由它返回 succeeded / failed / unconfirmed 三态和完整留痕。
"""

from workpanda.harness.budget import Budget, BudgetExhausted
from workpanda.harness.clients import ModelClient, ModelError, ModelTimeout
from workpanda.harness.contract import OUTCOME_CONTRACT, parse_contract
from workpanda.harness.executor import HarnessExecutor
from workpanda.harness.loop import AgentLoop, LoopResult, StopReason
from workpanda.harness.models import (
    HarnessEvent,
    HarnessMessage,
    MessageRole,
    ModelResponse,
    RiskLevel,
    ToolCall,
    ToolCallRecord,
    ToolOutcome,
    ToolSpec,
    max_risk,
)
from workpanda.harness.tools import (
    Tool,
    ToolContext,
    ToolError,
    ToolGrant,
    ToolRegistry,
    validate_arguments,
)

__all__ = [
    "OUTCOME_CONTRACT",
    "AgentLoop",
    "Budget",
    "BudgetExhausted",
    "HarnessEvent",
    "HarnessExecutor",
    "HarnessMessage",
    "LoopResult",
    "MessageRole",
    "ModelClient",
    "ModelError",
    "ModelResponse",
    "ModelTimeout",
    "RiskLevel",
    "StopReason",
    "Tool",
    "ToolCall",
    "ToolCallRecord",
    "ToolContext",
    "ToolError",
    "ToolGrant",
    "ToolOutcome",
    "ToolRegistry",
    "ToolSpec",
    "max_risk",
    "parse_contract",
    "validate_arguments",
]
