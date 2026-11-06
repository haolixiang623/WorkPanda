"""工具协议、注册表与调用授权。

三条不变式：

1. 模型只能看到本步骤被授权的工具子集，看不到全局注册表；
2. 任何工具调用都要过三道：白名单 → 参数校验 → 执行并留痕；
3. 未授权、未注册、参数不合法的调用一律 BLOCKED，绝不"尽力执行"。
"""

from __future__ import annotations

import time
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any, Protocol
from uuid import UUID

from pydantic import BaseModel

from workpanda.harness.models import (
    MAX_TOOL_CONTENT,
    RiskLevel,
    ToolCall,
    ToolCallRecord,
    ToolOutcome,
    ToolSpec,
)


class ToolError(Exception):
    """工具自身报告的失败。harness 会记录成 ERROR，不影响其他调用。"""

    def __init__(self, code: str, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable


class ToolContext(BaseModel):
    """执行上下文。工具不得读取此对象之外的任何环境信息。"""

    task_id: UUID
    flow_id: str
    step_id: str
    # 本次执行剩余时间（秒）。工具应据此提前放弃，而不是拖到外层超时。
    remaining_seconds: float = 0.0


class Tool(Protocol):
    spec: ToolSpec

    def invoke(self, arguments: dict[str, Any], context: ToolContext) -> str:
        """返回供模型阅读的结果文本；失败请抛 ToolError。"""
        ...


class ToolRegistry:
    """工具注册表。注册即声明能力，不代表任何步骤都能调用。"""

    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        name = tool.spec.name
        if name in self._tools:
            raise ValueError(f"tool already registered: {name}")
        self._tools[name] = tool

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._tools))

    def specs(self, allowed: Iterable[str] | None = None) -> list[ToolSpec]:
        names = set(self._tools) if allowed is None else set(allowed)
        return [self._tools[name].spec for name in sorted(names) if name in self._tools]


@dataclass(frozen=True)
class ToolGrant:
    """一次执行中被授权的工具集合——模型唯一可执行的范围。"""

    registry: ToolRegistry
    allowed: frozenset[str]

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self.allowed))

    @property
    def specs(self) -> list[ToolSpec]:
        return self.registry.specs(self.allowed)

    @property
    def risks(self) -> list[RiskLevel]:
        return [spec.risk for spec in self.specs]

    def call(self, call: ToolCall, context: ToolContext) -> ToolCallRecord:
        """执行一次调用。无论成功失败都返回可留痕记录，不向调用方抛异常。"""

        started = time.monotonic()
        risk = RiskLevel.LOW

        if call.name not in self.allowed:
            return self._record(
                call,
                ToolOutcome.BLOCKED,
                f"工具 {call.name} 未获得本步骤授权。",
                error_code="tool_not_authorized",
                started=started,
            )

        tool = self.registry.get(call.name)
        if tool is None:
            return self._record(
                call,
                ToolOutcome.BLOCKED,
                f"工具 {call.name} 未注册。",
                error_code="tool_not_registered",
                started=started,
            )

        risk = tool.spec.risk
        problems = validate_arguments(tool.spec, call.arguments)
        if problems:
            return self._record(
                call,
                ToolOutcome.BLOCKED,
                "参数不合法：" + "；".join(problems),
                error_code="invalid_arguments",
                started=started,
                risk=risk,
            )

        try:
            content = tool.invoke(call.arguments, context)
        except ToolError as exc:
            return self._record(
                call,
                ToolOutcome.ERROR,
                exc.message,
                error_code=exc.code,
                started=started,
                risk=risk,
            )
        except Exception as exc:  # noqa: BLE001 - 工具异常不能击穿执行循环
            return self._record(
                call,
                ToolOutcome.ERROR,
                f"工具执行异常：{type(exc).__name__}",
                error_code="tool_crashed",
                started=started,
                risk=risk,
            )

        return self._record(
            call, ToolOutcome.OK, str(content), started=started, risk=risk
        )

    def _record(
        self,
        call: ToolCall,
        outcome: ToolOutcome,
        content: str,
        *,
        started: float,
        error_code: str | None = None,
        risk: RiskLevel = RiskLevel.LOW,
    ) -> ToolCallRecord:
        return ToolCallRecord(
            call_id=call.call_id,
            name=call.name,
            arguments=call.arguments,
            outcome=outcome,
            content=content[:MAX_TOOL_CONTENT],
            error_code=error_code,
            duration_ms=int((time.monotonic() - started) * 1000),
            risk=risk,
        )


# ---------- 参数校验 ----------

_TYPE_CHECKS: dict[str, tuple[type, ...]] = {
    "string": (str,),
    "integer": (int,),
    "number": (int, float),
    "boolean": (bool,),
    "array": (list,),
    "object": (dict,),
}


def validate_arguments(spec: ToolSpec, arguments: dict[str, Any]) -> list[str]:
    """按工具声明的 JSON Schema 做轻量校验。

    只覆盖实际会用到的部分：required、类型、enum、长度、额外字段。
    不做完整 JSON Schema 实现，避免引入额外依赖与语义偏差。
    """

    schema = spec.parameters or {}
    properties: dict[str, Any] = schema.get("properties") or {}
    required: Sequence[str] = schema.get("required") or ()
    # 默认严格：未声明的参数直接拒绝，防止模型塞入意外字段。
    allow_extra = bool(schema.get("additionalProperties", False))

    problems: list[str] = []
    for key in required:
        if key not in arguments or arguments[key] in (None, ""):
            problems.append(f"缺少必填参数 {key}")

    if not allow_extra:
        for key in arguments:
            if key not in properties:
                problems.append(f"不支持参数 {key}")

    for key, value in arguments.items():
        rule = properties.get(key)
        if not isinstance(rule, dict):
            continue
        expected = rule.get("type")
        if isinstance(expected, str) and expected in _TYPE_CHECKS:
            if not isinstance(value, _TYPE_CHECKS[expected]):
                problems.append(f"参数 {key} 应为 {expected}")
                continue
            if expected == "integer" and isinstance(value, bool):
                problems.append(f"参数 {key} 应为 integer")
                continue
        if "enum" in rule and value not in rule["enum"]:
            problems.append(f"参数 {key} 不在允许取值内")
        max_length = rule.get("maxLength")
        if isinstance(max_length, int) and isinstance(value, str) and len(value) > max_length:
            problems.append(f"参数 {key} 超过最大长度 {max_length}")

    return problems
