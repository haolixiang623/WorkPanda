"""工具协议、注册表与调用授权。

四条不变式：

1. 模型只能看到本步骤被授权的工具子集，看不到全局注册表；
2. 任何工具调用都要过四道闸：白名单 → 调用次数/重复副作用 → 参数校验 → 限期执行并留痕；
3. 未授权、未注册、参数不合法、重复副作用的调用一律 BLOCKED，绝不"尽力执行"；
4. 工具必须在声明的超时内返回，超时按"结果未知"处理，绝不当失败重试。
"""

from __future__ import annotations

import json
import threading
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


class ToolTimeout(Exception):
    """工具未在限期内返回。此时结果未知：可能已产生副作用。"""


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


class ToolGrant:
    """一次执行中被授权的工具集合——模型唯一可执行的范围。

    对象本身带执行期状态（调用次数、已成功的副作用），因此**每次执行都要新建**，
    不能跨任务复用：否则调用限额与重复副作用判断会互相污染。
    """

    def __init__(
        self,
        *,
        registry: ToolRegistry,
        allowed: Iterable[str],
        max_attempts: int = 2,
        call_timeout_seconds: float = 30.0,
        retry_backoff_seconds: float = 0.05,
    ) -> None:
        self.registry = registry
        self.allowed = frozenset(allowed)
        self._max_attempts = max(1, max_attempts)
        self._call_timeout_seconds = call_timeout_seconds
        self._retry_backoff_seconds = max(0.0, retry_backoff_seconds)
        self._attempts: dict[str, int] = {}
        self._succeeded_effects: dict[str, str] = {}

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

        if call.name not in self.allowed:
            return self._record(
                call,
                ToolOutcome.BLOCKED,
                f"工具 {call.name} 未获得本步骤授权。",
                started=started,
                error_code="tool_not_authorized",
            )

        tool = self.registry.get(call.name)
        if tool is None:
            return self._record(
                call,
                ToolOutcome.BLOCKED,
                f"工具 {call.name} 未注册。",
                started=started,
                error_code="tool_not_registered",
            )

        spec = tool.spec
        fingerprint = _fingerprint(spec.name, call.arguments)
        if not spec.idempotent and fingerprint in self._succeeded_effects:
            # 同一个副作用已经成功过一次。再次执行会重复提交，只能拒绝。
            # 这条判断必须排在次数上限之前，否则同样的重复请求会被报成"次数用尽"，
            # 运维就看不出"模型在重复提交同一个操作"这个真正的问题。
            return self._record(
                call,
                ToolOutcome.BLOCKED,
                f"工具 {spec.name} 的非幂等操作已成功执行过，拒绝重复提交。",
                started=started,
                error_code="duplicate_side_effect",
                risk=spec.risk,
            )

        limit = spec.call_limit
        used = self._attempts.get(spec.name, 0)
        if limit is not None and used >= limit:
            return self._record(
                call,
                ToolOutcome.BLOCKED,
                f"工具 {spec.name} 在本次执行中已达调用上限 {limit} 次。",
                started=started,
                error_code="tool_call_limit",
                risk=spec.risk,
            )

        problems = validate_arguments(spec, call.arguments)
        if problems:
            return self._record(
                call,
                ToolOutcome.BLOCKED,
                "参数不合法：" + "；".join(problems),
                started=started,
                error_code="invalid_arguments",
                risk=spec.risk,
            )

        timeout = min(spec.timeout_seconds, self._call_timeout_seconds)
        if context.remaining_seconds > 0:
            timeout = min(timeout, context.remaining_seconds)
        if timeout <= 0:
            return self._record(
                call,
                ToolOutcome.BLOCKED,
                "本次执行已无剩余时间，未执行该工具。",
                started=started,
                error_code="no_time_budget",
                risk=spec.risk,
            )

        attempts = 0
        last_error: ToolError | None = None
        while attempts < self._max_attempts:
            attempts += 1
            self._attempts[spec.name] = self._attempts.get(spec.name, 0) + 1

            remaining = timeout - (time.monotonic() - started)
            if context.remaining_seconds > 0:
                remaining = min(remaining, context.remaining_seconds)
            if remaining <= 0:
                return self._record(
                    call,
                    ToolOutcome.TIMEOUT,
                    f"工具 {spec.name} 在剩余时间内未能完成，结果未知。",
                    started=started,
                    error_code="tool_timeout",
                    risk=spec.risk,
                    attempts=attempts,
                )
            try:
                content = _invoke_with_timeout(
                    tool, call.arguments, context, remaining, spec.name
                )
            except ToolTimeout:
                return self._record(
                    call,
                    ToolOutcome.TIMEOUT,
                    f"工具 {spec.name} 未在 {remaining:.1f} 秒内返回，结果未知。",
                    started=started,
                    error_code="tool_timeout",
                    risk=spec.risk,
                    attempts=attempts,
                )
            except ToolError as exc:
                last_error = exc
                # 重试只对"幂等且明确可重试"的失败开放：非幂等工具重试可能重复提交。
                if exc.retryable and spec.idempotent and attempts < self._max_attempts:
                    if self._retry_backoff_seconds:
                        time.sleep(self._retry_backoff_seconds * attempts)
                    continue
                return self._record(
                    call,
                    ToolOutcome.ERROR,
                    exc.message,
                    started=started,
                    error_code=exc.code,
                    risk=spec.risk,
                    attempts=attempts,
                )
            except Exception as exc:  # noqa: BLE001 - 工具异常不能击穿执行循环
                return self._record(
                    call,
                    ToolOutcome.ERROR,
                    f"工具执行异常：{type(exc).__name__}",
                    started=started,
                    error_code="tool_crashed",
                    risk=spec.risk,
                    attempts=attempts,
                )

            if not spec.idempotent:
                self._succeeded_effects[fingerprint] = call.call_id
            return self._record(
                call,
                ToolOutcome.OK,
                str(content),
                started=started,
                risk=spec.risk,
                attempts=attempts,
            )

        # 理论不可达；保留兜底，避免出现"没有结论就返回空记录"。
        return self._record(
            call,
            ToolOutcome.ERROR,
            last_error.message if last_error else "工具调用失败。",
            started=started,
            error_code=last_error.code if last_error else "tool_failed",
            risk=spec.risk,
            attempts=attempts,
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
        attempts: int = 1,
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
            attempts=attempts,
        )


def _fingerprint(name: str, arguments: dict[str, Any]) -> str:
    """把工具名与参数压成一个可比对的指纹，用于识别重复副作用。"""

    try:
        payload = json.dumps(arguments, ensure_ascii=False, sort_keys=True, default=str)
    except (TypeError, ValueError):
        payload = repr(sorted(arguments.items()))
    return f"{name}:{payload}"


def _invoke_with_timeout(
    tool: Tool,
    arguments: dict[str, Any],
    context: ToolContext,
    timeout: float,
    name: str,
) -> str:
    """在独立线程里执行工具并等待，超时即放弃等待。

    说明（已知代价）：超时后工作线程无法被强制终止，它会继续跑到自然结束。
    这比"主流程被一个卡死的工具拖住"要好，但工具本身仍必须自带超时与幂等。
    """

    outcome: dict[str, Any] = {}
    finished = threading.Event()

    def runner() -> None:
        try:
            outcome["value"] = tool.invoke(arguments, context)
        except BaseException as exc:  # noqa: BLE001 - 原样带回调用线程再分类
            outcome["error"] = exc
        finally:
            finished.set()

    thread = threading.Thread(target=runner, name=f"tool-{name}", daemon=True)
    thread.start()
    if not finished.wait(timeout):
        raise ToolTimeout(name)

    error = outcome.get("error")
    if isinstance(error, BaseException):
        raise error
    return str(outcome.get("value", ""))


@dataclass(frozen=True)
class ToolResultSummary:
    """对一组调用记录的只读汇总，供 trace 与运维指标使用。"""

    total: int
    succeeded: int
    failed: int
    blocked: int
    timed_out: int

    @classmethod
    def from_records(cls, records: Sequence[ToolCallRecord]) -> ToolResultSummary:
        return cls(
            total=len(records),
            succeeded=sum(1 for record in records if record.outcome is ToolOutcome.OK),
            failed=sum(1 for record in records if record.outcome is ToolOutcome.ERROR),
            blocked=sum(1 for record in records if record.outcome is ToolOutcome.BLOCKED),
            timed_out=sum(1 for record in records if record.outcome is ToolOutcome.TIMEOUT),
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
