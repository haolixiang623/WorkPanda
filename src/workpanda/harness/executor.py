"""把通用 harness 接到应用层的 Executor 端口。

映射原则与 Octop 适配器一致：只有确定成功才报成功。
额外多一道守卫：本步骤有可用工具、模型却一次都没成功调用，却声称 succeeded，
一律降为"未确认"——这类"口头成功"是最容易被当成办理完成的假象。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import Any

from workpanda.application.ports import (
    ExecutionOutcome,
    ExecutionRequest,
    ExecutionResult,
    Executor,
)
from workpanda.harness.clients import ModelClient
from workpanda.harness.contract import OUTCOME_CONTRACT, parse_contract
from workpanda.harness.loop import AgentLoop, LoopResult, StopReason
from workpanda.harness.models import max_risk
from workpanda.harness.tools import ToolContext, ToolGrant, ToolRegistry

STOP_ERROR_CODES: dict[StopReason, str] = {
    StopReason.MAX_STEPS: "budget_max_steps",
    StopReason.MAX_TOOL_CALLS: "budget_max_tool_calls",
    StopReason.DEADLINE: "budget_deadline",
    StopReason.MODEL_TIMEOUT: "model_timeout",
    StopReason.MODEL_ERROR: "model_error",
    StopReason.NO_PROGRESS: "empty_model_response",
}

STOP_MESSAGES: dict[StopReason, str] = {
    StopReason.MAX_STEPS: "步骤数超出上限，结果无法确认。",
    StopReason.MAX_TOOL_CALLS: "工具调用次数超出上限，结果无法确认。",
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
        deadline_seconds: float = 45.0,
        require_tool_call: bool = True,
        prompt_builder: Callable[[ExecutionRequest, ToolGrant], tuple[str, str]] | None = None,
    ) -> None:
        self._registry = registry
        self._allow_request_tool = allow_request_tool
        self._step_tools = step_tools or {}
        self._deadline_seconds = deadline_seconds
        self._require_tool_call = require_tool_call
        self._prompt_builder = prompt_builder or default_step_prompt
        self._loop = AgentLoop(
            client,
            max_steps=max_steps,
            max_tool_calls=max_tool_calls,
            deadline_seconds=deadline_seconds,
        )

    def execute(self, request: ExecutionRequest) -> ExecutionResult:
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
        output = self._output(request, grant, result)

        if result.stop_reason is not StopReason.COMPLETED:
            # 预算耗尽或模型异常：可能已经调用过有副作用的工具，不能报失败后重试。
            return ExecutionResult(
                outcome=ExecutionOutcome.UNCONFIRMED,
                output=output,
                error_code=result.error_code or STOP_ERROR_CODES[result.stop_reason],
                message=result.error_message or STOP_MESSAGES[result.stop_reason],
            )

        outcome, detail = parse_contract(result.text)

        if outcome is ExecutionOutcome.SUCCEEDED and self._unverified_success(grant, result):
            outcome = ExecutionOutcome.UNCONFIRMED
            detail = {
                "reason": "unverified_success",
                "summary": "模型声称完成，但本步骤没有任何成功的工具调用作为依据。",
            }

        if outcome is ExecutionOutcome.SUCCEEDED:
            detail = {**detail, "tool_evidence": _tool_evidence(result)}

        output["verification"] = detail
        succeeded = outcome is ExecutionOutcome.SUCCEEDED
        return ExecutionResult(
            outcome=outcome,
            output=output,
            error_code=None if succeeded else str(detail.get("reason") or outcome.value),
            message=None if succeeded else str(detail.get("summary") or "模型未给出可核验结果"),
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
        return ToolGrant(registry=self._registry, allowed=allowed)

    def _output(
        self, request: ExecutionRequest, grant: ToolGrant, result: LoopResult
    ) -> dict[str, Any]:
        return {
            "tool": request.tool,
            "step_id": request.step_id,
            "text": result.text[:2000],
            "verification": {},
            "tool_calls": [record.model_dump(mode="json") for record in result.tool_calls],
            "trace": [event.model_dump(mode="json") for event in result.events],
            "model_calls": result.model_calls,
            "duration_ms": result.duration_ms,
            "stop_reason": result.stop_reason.value,
            "risk": max_risk(grant.risks).value,
        }


def _tool_evidence(result: LoopResult) -> list[dict[str, str]]:
    return [
        {
            "tool": record.name,
            "outcome": record.outcome.value,
            "content": record.content[:300],
        }
        for record in result.tool_calls
        if record.succeeded
    ]


def default_step_prompt(
    request: ExecutionRequest, grant: ToolGrant
) -> tuple[str, str]:
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
