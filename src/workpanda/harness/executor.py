"""把通用 harness 接到应用层的 Executor 端口。

映射原则与 Octop 适配器一致：只有确定成功才报成功。
额外多一道守卫：本步骤有可用工具、模型却一次都没成功调用，却声称 succeeded，
一律降为"未确认"——这类"口头成功"最容易被当成办理完成的假象。

四条与"落盘"有关的约定：

1. 回灌给模型的内容保持原样，落盘/对外的一律脱敏（见 `redaction.py`）；
2. 每次执行生成一个 `trace_id`，并把摘要写入 TraceStore，供运维检索与聚合；
3. 每个工具调用落账后写 checkpoint，中断后可续跑；续跑成功时新 trace 记为
   `resumed_from`，事后能看出"这一次是从哪一次接着跑的"；
4. checkpoint 遇到"结果未知"（工具超时）即标为不可续跑，续跑请求一律拒绝，
   不做盲目重试。

与"取消"有关的两条：

5. 取消是人的决定，不是故障：结论同样是 `unconfirmed`（此前可能已有副作用），
   但错误码是 `cancelled`，且**保留断点**——用户取消的是"继续跑"，不是"刚才没发生"；
6. 取消请求可能先于执行到达：取令牌时先看登记表里有没有暂存的取消，
   有就一步都不跑，连模型都不调。
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping, Sequence
from typing import Any
from uuid import uuid4

from workpanda.application.ports import (
    ExecutionOutcome,
    ExecutionRequest,
    ExecutionResult,
    Executor,
)
from workpanda.harness.audit import AuditJournal, entries_for_records
from workpanda.harness.cancel import CancelRegistry, CancelToken
from workpanda.harness.checkpoint import (
    CheckpointStore,
    ExecutionSnapshot,
    HarnessCheckpoint,
)
from workpanda.harness.clients import ModelClient
from workpanda.harness.contract import OUTCOME_CONTRACT, parse_contract
from workpanda.harness.effects import build_effects, summarize_effects
from workpanda.harness.loop import AgentLoop, LoopResult, StopReason
from workpanda.harness.models import ToolCallRecord, ToolOutcome, max_risk
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
    StopReason.CANCELLED: "cancelled",
}

STOP_MESSAGES: dict[StopReason, str] = {
    StopReason.MAX_STEPS: "步骤数超出上限，结果无法确认。",
    StopReason.MAX_TOOL_CALLS: "工具调用次数超出上限，结果无法确认。",
    StopReason.MAX_TOKENS: "模型用量超出上限，结果无法确认。",
    StopReason.DEADLINE: "执行超出时间上限，结果无法确认。",
    StopReason.MODEL_TIMEOUT: "模型调用超时，结果无法确认。",
    StopReason.MODEL_ERROR: "模型调用失败，结果无法确认。",
    StopReason.NO_PROGRESS: "模型连续返回空内容，无法确认结果。",
    StopReason.CANCELLED: "执行已被取消，此前的动作可能已经生效，须人工核实。",
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
        tool_concurrency: int = 1,
        model_retries: int = 0,
        model_retry_backoff_seconds: float = 0.5,
        redactor: Redactor | None = None,
        traces: TraceStore | None = None,
        audit: AuditJournal | None = None,
        checkpoints: CheckpointStore | None = None,
        resume_enabled: bool = False,
        resume_ttl_seconds: float = 900.0,
        cancellations: CancelRegistry | None = None,
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
        # 审计归档独立于 trace：trace 会被淘汰，归档不会。
        self._audit = audit
        self._checkpoints = checkpoints
        # 默认关闭：续跑会让"一次执行的中间状态"留存下来，属于需要显式开启的能力。
        self._resume_enabled = resume_enabled and checkpoints is not None
        self._resume_ttl_seconds = resume_ttl_seconds
        # 取消登记表由装配层持有：取消方（HTTP 线程）与执行方（执行线程）
        # 必须看到同一个对象，否则取消只是账面上的一句话。
        self._cancellations = cancellations
        self._prompt_builder = prompt_builder or default_step_prompt
        self._loop = AgentLoop(
            client,
            max_steps=max_steps,
            max_tool_calls=max_tool_calls,
            max_tokens=max_tokens,
            deadline_seconds=deadline_seconds,
            model_retries=model_retries,
            model_retry_backoff_seconds=model_retry_backoff_seconds,
            tool_concurrency=tool_concurrency,
        )

    def execute(self, request: ExecutionRequest) -> ExecutionResult:
        """执行一个步骤。令牌的取用与释放都在这里，保证不会漏放。"""

        token = self._begin_cancel(request)
        try:
            if token is not None and token.cancelled:
                # 取消先于执行到达：一步都不跑，连模型都不调。
                return self._cancelled_before_start(request, token)
            return self._execute_step(request, token)
        finally:
            self._end_cancel(request)

    def _execute_step(
        self, request: ExecutionRequest, token: CancelToken | None
    ) -> ExecutionResult:
        # 拒绝路径也要有真实耗时：留痕里恒为 0 会让"快速拒绝"和"根本没执行"
        # 变成同一种记录，运维无法区分。
        started = time.monotonic()
        trace_id = uuid4().hex
        grant = self._grant(request)
        snapshot, resumed_from, refusal = self._resume_state(request, grant)
        if refusal is not None:
            return self._refuse(request, grant, trace_id, *refusal, started=started)

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
            resume=snapshot,
            on_progress=self._progress_recorder(request, grant, trace_id),
            cancel=token,
        )
        output = self._output(request, grant, result, trace_id, resumed_from)
        # trace 只统计"这次尝试"发生的调用；断点里带过来的记录仍然算依据，但不重复计数。
        executed = (
            result.tool_calls[len(snapshot.tool_calls) :]
            if snapshot is not None
            else result.tool_calls
        )

        if result.stop_reason is not StopReason.COMPLETED:
            # 预算耗尽、模型异常或被取消：这次执行没有结论，断点保留——后续要么接着跑，
            # 要么被明确拒绝，绝不能当"没发生过"重新执行一遍。
            outcome = ExecutionOutcome.UNCONFIRMED
            error_code = result.error_code or STOP_ERROR_CODES[result.stop_reason]
            message = result.error_message or STOP_MESSAGES[result.stop_reason]
            output["verification"] = {"reason": error_code, "summary": message}
            self._save_trace(
                request,
                grant,
                trace_id=trace_id,
                outcome=outcome,
                error_code=error_code,
                stop_reason=result.stop_reason.value,
                records=executed,
                model_calls=result.model_calls,
                tokens=result.tokens,
                duration_ms=result.duration_ms,
                resumed_from=resumed_from,
            )
            return ExecutionResult(
                outcome=outcome, output=output, error_code=error_code, message=message
            )

        # 模型给出了结论（成功/失败/未确认），本步骤已闭环，断点不再有意义。
        self._discard_checkpoint(request)

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
        self._save_trace(
            request,
            grant,
            trace_id=trace_id,
            outcome=outcome,
            error_code=error_code,
            stop_reason=result.stop_reason.value,
            records=executed,
            model_calls=result.model_calls,
            tokens=result.tokens,
            duration_ms=result.duration_ms,
            resumed_from=resumed_from,
        )

        return ExecutionResult(
            outcome=outcome,
            output=output,
            error_code=error_code,
            message=message,
        )

    # ---------- 内部 ----------

    def _begin_cancel(self, request: ExecutionRequest) -> CancelToken | None:
        """取本步骤的取消令牌。未配置登记表时不参与取消（行为与之前完全一致）。"""

        registry = self._cancellations
        if registry is None:
            return None
        return registry.begin(request.task_id, request.step_id)

    def _end_cancel(self, request: ExecutionRequest) -> None:
        registry = self._cancellations
        if registry is None:
            return
        try:
            registry.end(request.task_id, request.step_id)
        except Exception:  # noqa: BLE001 - 释放失败只影响内存占用，不该击穿结果
            return

    def _cancelled_before_start(
        self, request: ExecutionRequest, token: CancelToken
    ) -> ExecutionResult:
        """执行还没开始就被取消：不调模型、不碰工具，但仍然留一条痕。"""

        grant = self._grant(request)
        return self._refuse(
            request,
            grant,
            uuid4().hex,
            "cancelled",
            f"本次执行在开始前已被取消（{token.reason}），未调用模型或工具。",
            started=time.monotonic(),
            stop_reason=StopReason.CANCELLED.value,
        )

    def _refuse(
        self,
        request: ExecutionRequest,
        grant: ToolGrant,
        trace_id: str,
        error_code: str,
        message: str,
        *,
        started: float | None = None,
        stop_reason: str = "refused",
    ) -> ExecutionResult:
        """存在断点但不允许续跑（或被取消）：拒绝执行，交人工核实，绝不盲目重来。"""

        duration_ms = int((time.monotonic() - started) * 1000) if started is not None else 0
        output = {
            "trace_id": trace_id,
            "tool": request.tool,
            "step_id": request.step_id,
            "text": "",
            "verification": {"reason": error_code, "summary": message},
            "tool_calls": [],
            "trace": [],
            "model_calls": 0,
            "tokens": 0,
            "duration_ms": duration_ms,
            "stop_reason": stop_reason,
            "risk": max_risk(grant.risks).value,
        }
        self._save_trace(
            request,
            grant,
            trace_id=trace_id,
            outcome=ExecutionOutcome.UNCONFIRMED,
            error_code=error_code,
            stop_reason=stop_reason,
            duration_ms=duration_ms,
        )
        return ExecutionResult(
            outcome=ExecutionOutcome.UNCONFIRMED,
            output=output,
            error_code=error_code,
            message=message,
        )

    def _resume_state(
        self, request: ExecutionRequest, grant: ToolGrant
    ) -> tuple[ExecutionSnapshot | None, str | None, tuple[str, str] | None]:
        """决定能否续跑。返回 (快照, 来源 trace_id, 拒绝原因)。"""

        store = self._checkpoints
        if not self._resume_enabled or store is None:
            return None, None, None

        checkpoint = store.load(request.task_id, request.step_id)
        if checkpoint is None:
            return None, None, None

        if (
            checkpoint.flow_id != request.flow_id
            or checkpoint.flow_version != request.flow_version
            or checkpoint.step_id != request.step_id
            or tuple(checkpoint.allowed_tools) != grant.names
        ):
            return (
                None,
                None,
                (
                    "checkpoint_mismatch",
                    "断点与本次执行的流程版本或授权工具不一致，已拒绝续跑，请人工核实。",
                ),
            )
        if not checkpoint.resumable:
            return (
                None,
                None,
                (
                    "checkpoint_not_resumable",
                    "上次执行存在结果未知的工具调用，不能续跑；请人工确认外部系统状态。",
                ),
            )
        if checkpoint.is_expired(self._resume_ttl_seconds):
            return (
                None,
                None,
                (
                    "checkpoint_expired",
                    "断点已超过保留时限，可能已被其他操作影响，请人工核实后再处理。",
                ),
            )

        grant.restore(checkpoint.grant_state)
        return checkpoint.snapshot, checkpoint.trace_id, None

    def _progress_recorder(
        self, request: ExecutionRequest, grant: ToolGrant, trace_id: str
    ) -> Callable[[ExecutionSnapshot], None] | None:
        """每个工具调用落账后写一次 checkpoint。未开启续跑时不产生任何落盘。"""

        store = self._checkpoints
        if not self._resume_enabled or store is None:
            return None

        def save(snapshot: ExecutionSnapshot) -> None:
            # 任何一个工具出现"结果未知"，整次执行都不可续跑：外部可能已经执行。
            timed_out = any(
                record.outcome is ToolOutcome.TIMEOUT for record in snapshot.tool_calls
            )
            store.save(
                HarnessCheckpoint(
                    trace_id=trace_id,
                    task_id=request.task_id,
                    flow_id=request.flow_id,
                    flow_version=request.flow_version,
                    step_id=request.step_id,
                    allowed_tools=list(grant.names),
                    snapshot=snapshot,
                    grant_state=grant.state(),
                    resumable=not timed_out,
                    blocked_reason="tool_timeout" if timed_out else None,
                )
            )

        return save

    def _discard_checkpoint(self, request: ExecutionRequest) -> None:
        store = self._checkpoints
        if store is None:
            return
        try:
            store.delete(request.task_id, request.step_id)
        except Exception:  # noqa: BLE001 - 清理失败不应影响已经得出的结论
            return

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
        resumed_from: str | None = None,
    ) -> dict[str, Any]:
        output = {
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
        if resumed_from:
            output["resumed_from"] = resumed_from
        return output

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
        *,
        trace_id: str,
        outcome: ExecutionOutcome,
        error_code: str | None,
        stop_reason: str,
        records: Sequence[ToolCallRecord] = (),
        model_calls: int = 0,
        tokens: int = 0,
        duration_ms: int = 0,
        resumed_from: str | None = None,
    ) -> None:
        counts = ToolResultSummary.from_records(list(records))
        # trace 与审计归档是两条独立的留痕通道：关掉其中一条不该连带关掉另一条。
        if self._traces is not None:
            self._traces.save(
                TraceRecord(
                    trace_id=trace_id,
                    task_id=request.task_id,
                    flow_id=request.flow_id,
                    flow_version=request.flow_version,
                    step_id=request.step_id,
                    outcome=outcome.value,
                    stop_reason=stop_reason,
                    # 记最终错误码（含 unverified_success 这类契约降级），不是循环的原始错误码。
                    error_code=error_code,
                    risk=max_risk(grant.risks).value,
                    model_calls=model_calls,
                    tool_calls=counts.total,
                    successful_tool_calls=counts.succeeded,
                    blocked_tool_calls=counts.blocked,
                    failed_tool_calls=counts.failed,
                    timed_out_tool_calls=counts.timed_out,
                    tokens=tokens,
                    duration_ms=duration_ms,
                    resumed_from=resumed_from,
                )
            )
        self._write_audit(
            request,
            grant,
            trace_id=trace_id,
            outcome=outcome,
            error_code=error_code,
            stop_reason=stop_reason,
            records=list(records),
            counts=counts,
            model_calls=model_calls,
            tokens=tokens,
            duration_ms=duration_ms,
            resumed_from=resumed_from,
        )

    def _write_audit(
        self,
        request: ExecutionRequest,
        grant: ToolGrant,
        *,
        trace_id: str,
        outcome: ExecutionOutcome,
        error_code: str | None,
        stop_reason: str,
        records: Sequence[ToolCallRecord],
        counts: ToolResultSummary,
        model_calls: int,
        tokens: int,
        duration_ms: int,
        resumed_from: str | None,
    ) -> None:
        """把这一次执行与它产生的副作用追加进审计归档。

        与 trace 的区别只有一个但很关键：trace 会被容量淘汰，归档不会。事后要回答
        "那次提交到底发出去没有"，只能靠这份只追加的记录。

        归档失败一律吞掉：留痕是为了复盘，不能反过来把已经完成的执行判成失败。
        这里也刻意不写模型原文与工具参数原文——参数只留指纹，业务数据留在任务事件里。
        """

        journal = self._audit
        if journal is None:
            return
        try:
            specs = {spec.name: spec for spec in grant.specs}
            effects = build_effects(
                records,
                specs=specs,
                trace_id=trace_id,
                task_id=request.task_id,
                flow_id=request.flow_id,
                flow_version=request.flow_version,
                step_id=request.step_id,
            )
            summary = summarize_effects(effects)
            entries = entries_for_records(
                trace_id=trace_id,
                task_id=request.task_id,
                flow_id=request.flow_id,
                flow_version=request.flow_version,
                step_id=request.step_id,
                execution={
                    "outcome": outcome.value,
                    "stop_reason": stop_reason,
                    "error_code": error_code,
                    "risk": max_risk(grant.risks).value,
                    "model_calls": model_calls,
                    "tool_calls": counts.total,
                    "blocked_tool_calls": counts.blocked,
                    "failed_tool_calls": counts.failed,
                    "timed_out_tool_calls": counts.timed_out,
                    "tokens": tokens,
                    "duration_ms": duration_ms,
                    "resumed_from": resumed_from,
                    "effects_applied": summary.applied,
                    "effects_unknown": summary.unknown,
                    # 只要有一笔"结果未知"，这次执行就不能当交付完成，必须转人工核对。
                    "requires_manual_check": summary.requires_manual_check,
                },
                effects=effects,
            )
            for entry in entries:
                journal.append(entry)
        except Exception:  # noqa: BLE001 - 留痕失败不得击穿执行
            return


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
