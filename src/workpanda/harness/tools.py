"""工具协议、注册表与调用授权。

四条不变式：

1. 模型只能看到本步骤被授权的工具子集，看不到全局注册表；
2. 任何工具调用都要过四道闸：白名单 → 调用次数/重复副作用 → 参数校验 → 限期执行并留痕；
3. 未授权、未注册、参数不合法、重复副作用的调用一律 BLOCKED，绝不"尽力执行"；
4. 工具必须在声明的超时内返回，超时按"结果未知"处理，绝不当失败重试。
"""

from __future__ import annotations

import json
import re
import threading
import time
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol
from uuid import UUID

from pydantic import BaseModel

from workpanda.harness.models import (
    CRASH_ERROR_CODE,
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


@dataclass(frozen=True)
class _Admission:
    """通过闸门的调用：已经预约过一次尝试，接下来只需要执行。"""

    tool: Tool
    spec: ToolSpec
    timeout: float
    fingerprint: str


class ToolGrant:
    """一次执行中被授权的工具集合——模型唯一可执行的范围。

    对象本身带执行期状态（调用次数、已成功的副作用），因此**每次执行都要新建**，
    不能跨任务复用：否则调用限额与重复副作用判断会互相污染。

    允许被多个线程同时调用（并行工具调用）：闸门判定与尝试计数在锁内完成，
    真正的工具执行在锁外——限额不会因为并发被绕过，慢工具也不会堵住别的工具。
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
        # 只保护下面的字典与闸门判定，不覆盖工具执行本身。
        self._lock = threading.Lock()

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self.allowed))

    @property
    def specs(self) -> list[ToolSpec]:
        return self.registry.specs(self.allowed)

    @property
    def risks(self) -> list[RiskLevel]:
        return [spec.risk for spec in self.specs]

    def spec_for(self, name: str) -> ToolSpec | None:
        """按名字取声明。用于判断"这批调用能不能并行"，取不到就当不可并行。"""

        if name not in self.allowed:
            return None
        tool = self.registry.get(name)
        return None if tool is None else tool.spec

    def state(self) -> dict[str, Any]:
        """导出执行期状态，供断点续跑时原样恢复。

        必须包含"已成功副作用指纹"：少了它，续跑就会把已经提交过的
        非幂等操作再提交一次。
        """

        with self._lock:
            return {
                "attempts": dict(self._attempts),
                "succeeded_effects": dict(self._succeeded_effects),
            }

    def restore(self, state: Mapping[str, Any]) -> None:
        """恢复执行期状态。只接受可解析的键值，异常数据一律忽略而不是击穿执行。"""

        attempts = state.get("attempts")
        effects = state.get("succeeded_effects")
        with self._lock:
            if isinstance(attempts, dict):
                self._attempts = {
                    str(name): int(count)
                    for name, count in attempts.items()
                    if isinstance(count, int) and count >= 0
                }
            if isinstance(effects, dict):
                self._succeeded_effects = {
                    str(fingerprint): str(call_id) for fingerprint, call_id in effects.items()
                }

    def call(self, call: ToolCall, context: ToolContext) -> ToolCallRecord:
        """执行一次调用。无论成功失败都返回可留痕记录，不向调用方抛异常。

        闸门（授权、重复副作用、限额、参数、时间预算）在锁内一次判完并预约第一次
        尝试——"检查通过"与"记一次尝试"之间若被别的线程插队，`max_calls` 就只是
        建议值。工具执行本身在锁外：慢工具不该堵住同一批里的其他调用。
        """

        started = time.monotonic()
        admission = self._admit(call, context, started)
        if isinstance(admission, ToolCallRecord):
            return admission

        spec = admission.spec
        tool = admission.tool
        # 第一次尝试已在闸门内预约过，这里从 1 开始计。
        attempts = 1
        while True:
            remaining = admission.timeout - (time.monotonic() - started)
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
                # 重试只对"幂等且明确可重试"的失败开放：非幂等工具重试可能重复提交。
                if exc.retryable and spec.idempotent and attempts < self._max_attempts:
                    if self._retry_backoff_seconds:
                        time.sleep(self._retry_backoff_seconds * attempts)
                    attempts += 1
                    with self._lock:
                        self._attempts[spec.name] = self._attempts.get(spec.name, 0) + 1
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
                    error_code=CRASH_ERROR_CODE,
                    risk=spec.risk,
                    attempts=attempts,
                )

            if not spec.idempotent:
                with self._lock:
                    self._succeeded_effects[admission.fingerprint] = call.call_id
            return self._record(
                call,
                ToolOutcome.OK,
                str(content),
                started=started,
                risk=spec.risk,
                attempts=attempts,
            )

    def _admit(
        self, call: ToolCall, context: ToolContext, started: float
    ) -> _Admission | ToolCallRecord:
        """四道闸的前三道。返回记录表示已拦下，返回 `_Admission` 表示放行。"""

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
        with self._lock:
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

            # 预约第一次尝试：与限额判定同处一个临界区，否则并发调用会同时通过检查。
            self._attempts[spec.name] = used + 1

        return _Admission(
            tool=tool, spec=spec, timeout=timeout, fingerprint=fingerprint
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
    """对一组调用记录的只读汇总，供 trace 与运维指标使用。

    `total` 包含所有记录，因此 `executed` 要单独给：被预算或取消省下的调用
    也占一条记录，但它们没有被调用过，不该算进"调了几次工具"。
    """

    total: int
    succeeded: int
    failed: int
    blocked: int
    timed_out: int
    skipped: int = 0

    @property
    def executed(self) -> int:
        """真正发起过的调用次数（不含被省下的）。"""

        return max(0, self.total - self.skipped)

    @classmethod
    def from_records(cls, records: Sequence[ToolCallRecord]) -> ToolResultSummary:
        return cls(
            total=len(records),
            succeeded=sum(1 for record in records if record.outcome is ToolOutcome.OK),
            failed=sum(1 for record in records if record.outcome is ToolOutcome.ERROR),
            blocked=sum(1 for record in records if record.outcome is ToolOutcome.BLOCKED),
            timed_out=sum(1 for record in records if record.outcome is ToolOutcome.TIMEOUT),
            skipped=sum(1 for record in records if record.outcome is ToolOutcome.SKIPPED),
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

    覆盖实际会用到的部分：required、类型、enum、长度、数值范围、正则、数组长度
    与元素、嵌套对象。不做完整 JSON Schema 实现（$ref、oneOf、依赖关系等一律
    不管），避免引入额外依赖与语义偏差。

    校验只做「模型给的参数能不能进这个工具」的判断，不替工具做业务规则——
    业务校验仍在工具内部，违反时抛 `ToolError`。
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
        if isinstance(rule, dict):
            _check_value(key, value, rule, problems)

    return problems


# 嵌套校验的最大深度。schema 由我们自己写，但配置写错时不该把执行拖成递归栈溢出。
_MAX_SCHEMA_DEPTH = 4


def _check_value(
    label: str, value: Any, rule: Mapping[str, Any], problems: list[str], depth: int = 0
) -> None:
    """校验单个值。`label` 是给人看的路径（如 `meta.k`），用于定位问题字段。"""

    if depth > _MAX_SCHEMA_DEPTH:
        return

    expected = rule.get("type")
    if isinstance(expected, str) and expected in _TYPE_CHECKS:
        # bool 是 int 的子类，`count: true` 必须当类型错误而不是整数。
        if not isinstance(value, _TYPE_CHECKS[expected]) or (
            expected == "integer" and isinstance(value, bool)
        ):
            problems.append(f"参数 {label} 应为 {expected}")
            return

    if "enum" in rule:
        allowed = rule["enum"]
        if isinstance(allowed, list) and value not in allowed:
            problems.append(f"参数 {label} 不在允许取值内")

    if isinstance(value, str):
        _check_string(label, value, rule, problems)
    elif isinstance(value, bool):
        pass
    elif isinstance(value, (int, float)):
        _check_number(label, value, rule, problems)
    elif isinstance(value, list):
        _check_array(label, value, rule, problems, depth)
    elif isinstance(value, dict):
        _check_object(label, value, rule, problems, depth)


def _check_string(label: str, value: str, rule: Mapping[str, Any], problems: list[str]) -> None:
    max_length = rule.get("maxLength")
    if isinstance(max_length, int) and len(value) > max_length:
        problems.append(f"参数 {label} 超过最大长度 {max_length}")
    min_length = rule.get("minLength")
    if isinstance(min_length, int) and len(value) < min_length:
        problems.append(f"参数 {label} 少于最小长度 {min_length}")
    pattern = rule.get("pattern")
    if isinstance(pattern, str):
        try:
            matched = re.search(pattern, value) is not None
        except re.error:
            # 声明的正则本身写错了属于工具定义问题，不能因此拒绝一次合法调用。
            return
        if not matched:
            problems.append(f"参数 {label} 不匹配格式要求")


def _check_number(label: str, value: float, rule: Mapping[str, Any], problems: list[str]) -> None:
    minimum = rule.get("minimum")
    if isinstance(minimum, (int, float)) and value < minimum:
        problems.append(f"参数 {label} 小于最小值 {minimum}")
    maximum = rule.get("maximum")
    if isinstance(maximum, (int, float)) and value > maximum:
        problems.append(f"参数 {label} 大于最大值 {maximum}")


def _check_array(
    label: str,
    value: list,
    rule: Mapping[str, Any],
    problems: list[str],
    depth: int,
) -> None:
    max_items = rule.get("maxItems")
    if isinstance(max_items, int) and len(value) > max_items:
        problems.append(f"参数 {label} 超过最大元素个数 {max_items}")
    min_items = rule.get("minItems")
    if isinstance(min_items, int) and len(value) < min_items:
        problems.append(f"参数 {label} 少于最小元素个数 {min_items}")
    items = rule.get("items")
    if isinstance(items, dict):
        for index, item in enumerate(value):
            _check_value(f"{label}[{index}]", item, items, problems, depth + 1)


def _check_object(
    label: str,
    value: dict,
    rule: Mapping[str, Any],
    problems: list[str],
    depth: int,
) -> None:
    nested = rule.get("properties")
    if not isinstance(nested, dict):
        return
    for name in rule.get("required") or ():
        if name not in value:
            problems.append(f"参数 {label}.{name} 为必填")
    if not rule.get("additionalProperties", False):
        for name in value:
            if name not in nested:
                problems.append(f"参数 {label} 不支持字段 {name}")
    for name, item in value.items():
        sub_rule = nested.get(name)
        if isinstance(sub_rule, dict):
            _check_value(f"{label}.{name}", item, sub_rule, problems, depth + 1)
