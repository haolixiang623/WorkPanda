"""把通用 harness 接到应用层的 Executor 端口。

映射原则与 Octop 适配器一致：只有确定成功才报成功。
额外多一道守卫：本步骤有可用工具、模型却一次都没成功调用，却声称 succeeded，
一律降为"未确认"——这类"口头成功"最容易被当成办理完成的假象。

两条与"落盘"有关的约定：

1. 回灌给模型的内容保持原样，落盘/对外的一律脱敏（见 `redaction.py`）；
2. 每次执行生成一个 `trace_id`，并把摘要写入 TraceStore，供运维检索与聚合。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import Any
from uuid import uuid4

from workpanda.application.ports import (
    ExecutionOutcome,
    ExecutionRequest,
    ExecutionResult,
    Executor,
)
from workpanda.harness.clients import ModelClient
from workpanda.harness.contract import OUTCOME_CONTRACT, parse_contract
from workpanda.harness.loop import AgentLoop, LoopResult, StopReason
from workpanda.harness.models import ToolCallRecord, max_risk
from workpanda.harness.redaction import DEFAULT_REDACTOR, Redactor, mask_values
from workpanda.harness.tools import (
    ToolContext,
    ToolGrant,
    ToolRegistry,
    ToolResultSummary,
)
from workpanda.harness.trace import TraceRecord, TraceStore

STOP_ERROR_CODES: dict[StopReason, str] = {
    StopReason.MAX_STEPS: "budget_max_steps",
    StopReason.MAX_TOOL_CALLS: "budget_max_tool_calls",
    StopReason.MAX_TOKENS: "budget_max_tokens",
    StopReason.DEADLINE: "budget_deadline",
    StopReason.MODEL_TIMEOUT: "model_timeout",
    StopReason.MODEL_ERROR: "model_error",
    StopReason.NO_PROGRESS: "empty_model_response",
}

STOP_MESSAGES: dict[StopReason, str] = {
    StopReason.MAX_STEPS: "步骤数超出上限，结果无法确认。",
    StopReason.MAX_TOOL_CALLS: "工具调用次数超出上限，结果无法确认。",
    StopReason.MAX_TOKENS: "模型用量超出上限，结果无法确认。",
    StopReason.DEADLINE: "执行超出时间上限，结果无法确认。",
    StopReason.MODEL_TIMEOUT: "模型调用超时，结果无法确认。",
    StopReason.MODEL_ERROR: "模型调用失败，结果无法确认。",
    StopReason.NO_PROGRESS: "模型连续返回空内容，无法确认结果。",
}


class HarnessExecutor(Executor):
    def __init__(
        self,
        client: ModelClient,
        *,
        registry: ToolRegistry,
        allow_request_tool: bool = True,
        step_tools: Mapping[str, Sequence[str]] | None = None,
        max_steps: int = 6,
        max_tool_calls: int = 8,
        max_tokens: int = 0,
        deadline_seconds: float = 45.0,
        require_tool_call: bool = True,
        tool_retries: int = 1,
        redactor: Redactor | None = None,
        traces: TraceStore | None = None,
        prompt_builder: Callable[[ExecutionRequest, ToolGrant], tuple[str, str]] | None = None,
    ) -> None:
        self._registry = registry
        self._allow_request_tool = allow_request_tool
        self._step_tools = step_tools or {}
        self._deadline_seconds = deadline_seconds
        self._require_tool_call = require_tool_call
        self._tool_retries = max(0, tool_retries)
        self._redactor = redactor or DEFAULT_REDACTOR
        self._traces = traces
        self._prompt_builder = prompt_builder or default_step_prompt
        self._loop = AgentLoop(
            client,
            max_steps=max_steps,
            max_tool_calls=max_tool_calls,
            max_tokens=max_tokens,
            deadline_seconds=deadline_seconds,
        )

    def execute(self, request: ExecutionRequest) -> ExecutionResult:
        trace_id = uuid4().hex
        grant = self._grant(request)
        system_prompt, user_prompt = self._prompt_builder(request, grant)
        result = self._loop.run(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            grant=grant,
            context=ToolContext(
                task_id=request.task_id,
                flow_id=request.flow_id,
                step_id=request.step_id,
                remaining_seconds=self._deadline_seconds,
            ),
        )
        output = self._output(request, grant, result, trace_id)

        if result.stop_reason is not StopReason.COMPLETED:
            # 预算耗尽或模型异常：可能已经调用过有副作用的工具，不能报失败后重试。
            outcome = ExecutionOutcome.UNCONFIRMED
            error_code = result.error_code or STOP_ERROR_CODES[result.stop_reason]
            message = result.error_message or STOP_MESSAGES[result.stop_reason]
            output["verification"] = {"reason": error_code, "summary": message}
            self._save_trace(request, grant, result, outcome, trace_id, error_code)
            return ExecutionResult(
                outcome=outcome, output=output, error_code=error_code, message=message
            )

        outcome, detail = parse_contract(result.text)

        if outcome is ExecutionOutcome.SUCCEEDED and self._unverified_success(grant, result):
            outcome = ExecutionOutcome.UNCONFIRMED
            detail = {
                "reason": "unverified_success",
                "summary": "模型声称完成，但本步骤没有任何成功的工具调用作为依据。",
            }

        if outcome is ExecutionOutcome.SUCCEEDED:
            detail = {**detail, "tool_evidence": self._tool_evidence(result)}

        output["verification"] = self._redact(detail)
        succeeded = outcome is ExecutionOutcome.SUCCEEDED
        error_code = None if succeeded else str(detail.get("reason") or outcome.value)
        message = None if succeeded else str(detail.get("summary") or "模型未给出可核验结果")
        self._save_trace(request, grant, result, outcome, trace_id, error_code)

        return ExecutionResult(
            outcome=outcome,
            output=output,
            error_code=error_code,
            message=message,
        )

    # ---------- 内部 ----------

    def _unverified_success(self, grant: ToolGrant, result: LoopResult) -> bool:
        if not self._require_tool_call:
            return False
        if not grant.names:
            # 本步骤本来就没有工具可用，只能依赖模型自身产出的依据。
            return False
        return not result.has_successful_tool_call

    def _grant(self, request: ExecutionRequest) -> ToolGrant:
        names = set(self._step_tools.get(request.step_id, ()))
        # 流程定义里写的 tool 就是本步骤的能力授权来源，模型只能调它。
        if self._allow_request_tool and request.tool:
            names.add(request.tool)
        allowed = frozenset(name for name in names if self._registry.get(name) is not None)
        return ToolGrant(
            registry=self._registry,
            allowed=allowed,
            max_attempts=self._tool_retries + 1,
            call_timeout_seconds=self._deadline_seconds,
        )

    def _output(
        self,
        request: ExecutionRequest,
        grant: ToolGrant,
        result: LoopResult,
        trace_id: str,
    ) -> dict[str, Any]:
        return {
            "trace_id": trace_id,
            "tool": request.tool,
            "step_id": request.step_id,
            # 模型原文只保留摘要；完整原文在事件里，且同样脱敏。
            "text": self._redact(result.text[:2000]),
            "verification": {},
            "tool_calls": [self._redact_record(record) for record in result.tool_calls],
            "trace": [self._redact_event(event.model_dump(mode="json")) for event in result.events],
            "model_calls": result.model_calls,
            "tokens": result.tokens,
            "duration_ms": result.duration_ms,
            "stop_reason": result.stop_reason.value,
            "risk": max_risk(grant.risks).value,
        }

    def _redact(self, value: Any) -> Any:
        return self._redactor.redact(value)

    def _redact_event(self, payload: dict[str, Any]) -> dict[str, Any]:
        # 敏感值必须从**未脱敏**的原始事件里取，否则拿到的是已经变成掩码的入参。
        raw_data = payload.get("data")
        data: dict[str, Any] = raw_data if isinstance(raw_data, dict) else {}
        secrets = self._sensitive_values(data.get("name"), data.get("arguments"))

        masked = self._redactor.redact(payload)
        masked_data = masked.get("data")
        # 工具返回值可能原样回显敏感入参，事件里的 content 也要按值再遮一道。
        if (
            secrets
            and isinstance(masked_data, dict)
            and isinstance(masked_data.get("content"), str)
        ):
            masked_data["content"] = mask_values(masked_data["content"], secrets)
        return masked

    def _redact_record(self, record: ToolCallRecord) -> dict[str, Any]:
        """按工具声明的敏感参数名叠加脱敏，再序列化。"""

        tool = self._registry.get(record.name)
        spec = tool.spec if tool is not None else None
        redactor = self._redactor
        if spec is not None and spec.sensitive_fields:
            redactor = redactor.with_extra(spec.sensitive_fields)
        payload = redactor.redact(record.model_dump(mode="json"))

        # 工具可能把敏感入参原样回显；按键名遮不住返回值，这里按值再遮一次。
        secrets = self._sensitive_values(record.name, record.arguments)
        if secrets and isinstance(payload.get("content"), str):
            payload["content"] = mask_values(payload["content"], secrets)
        return payload

    def _sensitive_values(self, name: Any, arguments: Any) -> list[str]:
        """取出某工具声明为敏感的入参值。必须在按字段名脱敏之前调用。"""

        if not isinstance(name, str) or not isinstance(arguments, dict):
            return []
        tool = self._registry.get(name)
        spec = tool.spec if tool is not None else None
        if spec is None or not spec.sensitive_fields:
            return []
        return [
            str(arguments[field])
            for field in spec.sensitive_fields
            if field in arguments and arguments[field] is not None
        ]

    def _tool_evidence(self, result: LoopResult) -> list[dict[str, str]]:
        evidence: list[dict[str, str]] = []
        for record in result.tool_calls:
            if not record.succeeded:
                continue
            masked = self._redact_record(record)
            evidence.append(
                {
                    "tool": record.name,
                    "outcome": record.outcome.value,
                    "content": str(masked.get("content", ""))[:300],
                }
            )
        return evidence

    def _save_trace(
        self,
        request: ExecutionRequest,
        grant: ToolGrant,
        result: LoopResult,
        outcome: ExecutionOutcome,
        trace_id: str,
        error_code: str | None,
    ) -> None:
        if self._traces is None:
            return
        counts = ToolResultSummary.from_records(result.tool_calls)
        self._traces.save(
            TraceRecord(
                trace_id=trace_id,
                task_id=request.task_id,
                flow_id=request.flow_id,
                flow_version=request.flow_version,
                step_id=request.step_id,
                outcome=outcome.value,
                stop_reason=result.stop_reason.value,
                # 记最终错误码（含 unverified_success 这类契约降级），不是循环的原始错误码。
                error_code=error_code,
                risk=max_risk(grant.risks).value,
                model_calls=result.model_calls,
                tool_calls=counts.total,
                successful_tool_calls=counts.succeeded,
                blocked_tool_calls=counts.blocked,
                failed_tool_calls=counts.failed,
                timed_out_tool_calls=counts.timed_out,
                tokens=result.tokens,
                duration_ms=result.duration_ms,
            )
        )


def default_step_prompt(request: ExecutionRequest, grant: ToolGrant) -> tuple[str, str]:
    """只给当前步骤所需信息：执行要求、参数、以及"能用哪些工具"。"""

    instruction = request.instruction or "执行该步骤并如实报告结果。"
    lines = [
        "你是受控执行器，只完成当前这一个步骤。",
        f"步骤标识：{request.step_id}",
        f"执行要求：{instruction}",
    ]
    if grant.names:
        lines.append(f"本步骤可调用的工具：{'、'.join(grant.names)}（仅限这些）。")
    else:
        # 明确告知无工具可用，避免模型去找不存在的工具而误报失败。
        lines.append("本步骤不需要调用任何工具，请直接按要求完成。")
    lines.append(OUTCOME_CONTRACT)

    arguments = "\n".join(f"- {key}：{value}" for key, value in sorted(request.arguments.items()))
    user = f"本步骤的已知参数：\n{arguments or '- 无'}"
    return "\n".join(lines), user
