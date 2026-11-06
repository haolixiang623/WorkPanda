"""有界执行循环：模型 → 工具 → 观察 → 再决定，直到给出结论或用尽预算。

不变式：

- 循环必须可终止：步数、工具调用次数、总耗时三重上限；
- 超预算不是"失败"，而是"未确认"——此刻可能已经调用过有副作用的工具；
- 每一次模型调用和工具调用都写进 trace，事后必须能完整重放发生了什么；
- 每次工具调用落账后回调进度快照，使中断后可以从最近一次已知状态续跑。
"""

from __future__ import annotations

import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from enum import StrEnum

from workpanda.harness.budget import Budget, BudgetExhausted
from workpanda.harness.checkpoint import ExecutionSnapshot
from workpanda.harness.clients import ModelClient, ModelError, ModelTimeout
from workpanda.harness.models import (
    EventKind,
    HarnessEvent,
    HarnessMessage,
    MessageRole,
    ModelResponse,
    ToolCallRecord,
)
from workpanda.harness.tools import ToolContext, ToolGrant


class StopReason(StrEnum):
    # 模型给出了最终结论（结论是否可信由契约判定，不是这里判断）。
    COMPLETED = "completed"
    MAX_STEPS = "max_steps"
    MAX_TOOL_CALLS = "max_tool_calls"
    MAX_TOKENS = "max_tokens"
    DEADLINE = "deadline"
    MODEL_ERROR = "model_error"
    MODEL_TIMEOUT = "model_timeout"
    NO_PROGRESS = "no_progress"


_BUDGET_REASON: dict[str, tuple[StopReason, str]] = {
    "max_steps": (StopReason.MAX_STEPS, "budget_max_steps"),
    "max_tool_calls": (StopReason.MAX_TOOL_CALLS, "budget_max_tool_calls"),
    "max_tokens": (StopReason.MAX_TOKENS, "budget_max_tokens"),
    "deadline": (StopReason.DEADLINE, "budget_deadline"),
}


@dataclass
class LoopResult:
    text: str = ""
    stop_reason: StopReason = StopReason.COMPLETED
    tool_calls: list[ToolCallRecord] = field(default_factory=list)
    events: list[HarnessEvent] = field(default_factory=list)
    model_calls: int = 0
    duration_ms: int = 0
    tokens: int = 0
    error_code: str | None = None
    # 上游给出的具体原因（例如网关 400 的原文），排障时必须能看到。
    error_message: str | None = None

    @property
    def has_successful_tool_call(self) -> bool:
        return any(record.succeeded for record in self.tool_calls)

    @property
    def stopped_by_budget(self) -> bool:
        return self.stop_reason in {
            StopReason.MAX_STEPS,
            StopReason.MAX_TOOL_CALLS,
            StopReason.MAX_TOKENS,
            StopReason.DEADLINE,
        }


class AgentLoop:
    def __init__(
        self,
        client: ModelClient,
        *,
        max_steps: int = 6,
        max_tool_calls: int = 8,
        max_tokens: int = 0,
        deadline_seconds: float = 45.0,
        max_empty_responses: int = 2,
    ) -> None:
        self._client = client
        self._max_steps = max_steps
        self._max_tool_calls = max_tool_calls
        self._max_tokens = max_tokens
        self._deadline_seconds = deadline_seconds
        self._max_empty_responses = max_empty_responses

    def run(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        grant: ToolGrant,
        context: ToolContext,
        resume: ExecutionSnapshot | None = None,
        on_progress: Callable[[ExecutionSnapshot], None] | None = None,
    ) -> LoopResult:
        """执行一次有界循环。

        `resume` 不为空时从断点继续：对话、已发生的工具调用与预算用量都接着算，
        不重新发一次"从头开始"的请求；`on_progress` 在每个工具调用落账后回调，
        供调用方落 checkpoint——回调失败不得击穿执行。
        """

        started = time.monotonic()
        budget = Budget(
            max_steps=self._max_steps,
            max_tool_calls=self._max_tool_calls,
            max_tokens=self._max_tokens,
            deadline_seconds=self._deadline_seconds,
            initial_steps=resume.steps if resume is not None else 0,
            initial_tool_calls=resume.tool_call_count if resume is not None else 0,
            initial_tokens=resume.tokens if resume is not None else 0,
        )
        if resume is not None:
            messages = [message.model_copy(deep=True) for message in resume.messages]
        else:
            messages = [
                HarnessMessage(role=MessageRole.SYSTEM, content=system_prompt),
                HarnessMessage(role=MessageRole.USER, content=user_prompt),
            ]
        events: list[HarnessEvent] = []
        records: list[ToolCallRecord] = (
            [record.model_copy(deep=True) for record in resume.tool_calls]
            if resume is not None
            else []
        )
        model_calls = 0
        empty_responses = 0

        def emit(kind: EventKind, data: dict) -> None:
            events.append(HarnessEvent(sequence=len(events) + 1, kind=kind, data=data))

        def report_progress() -> None:
            if on_progress is None:
                return
            snapshot = ExecutionSnapshot(
                messages=[message.model_copy(deep=True) for message in messages],
                tool_calls=[record.model_copy(deep=True) for record in records],
                steps=budget.steps,
                tool_call_count=budget.tool_calls,
                tokens=budget.tokens,
            )
            try:
                on_progress(snapshot)
            except Exception:  # noqa: BLE001 - 落盘失败只影响恢复能力，不该中断真实执行
                return

        def finish(*args, **kwargs) -> LoopResult:
            """统一收尾：把预算用量补进 stopped 事件，事后能判断是否被预算掐断。"""

            result = self._finish(*args, **kwargs)
            result.tokens = budget.tokens
            result.events[-1].data["budget"] = budget.snapshot()
            return result

        try:
            while True:
                budget.consume_step()
                specs = grant.specs
                emit(
                    EventKind.MODEL_REQUESTED,
                    {"step": budget.steps, "tools": [spec.name for spec in specs]},
                )
                response = self._client.complete(messages=messages, tools=specs)
                model_calls += 1
                emit(EventKind.MODEL_RESPONDED, _describe_response(response))

                if response.usage is not None:
                    # 模型服务可能不上报用量；不上报就不统计，绝不用字符数估算。
                    budget.consume_tokens(response.usage.total_tokens)

                if response.tool_calls:
                    messages.append(
                        HarnessMessage(
                            role=MessageRole.ASSISTANT,
                            content=response.text,
                            tool_calls=response.tool_calls,
                        )
                    )
                    for call in response.tool_calls:
                        budget.consume_tool_call()
                        record = grant.call(
                            call,
                            context.model_copy(
                                update={"remaining_seconds": budget.remaining_seconds}
                            ),
                        )
                        records.append(record)
                        emit(EventKind.TOOL_COMPLETED, _describe_record(record))
                        messages.append(
                            HarnessMessage(
                                role=MessageRole.TOOL,
                                tool_call_id=call.call_id,
                                name=call.name,
                                content=_tool_content(record),
                            )
                        )
                        # 工具调用一旦落账就可能是副作用，必须立刻可恢复。
                        report_progress()
                    continue

                text = response.text.strip()
                if text:
                    return finish(
                        text, StopReason.COMPLETED, records, events, model_calls, started
                    )

                # 空回复：给模型一次机会；连续空回复说明循环没有意义，立即终止。
                empty_responses += 1
                if empty_responses >= self._max_empty_responses:
                    return finish(
                        "",
                        StopReason.NO_PROGRESS,
                        records,
                        events,
                        model_calls,
                        started,
                        error_code="empty_model_response",
                    )

        except BudgetExhausted as exc:
            reason, error_code = _BUDGET_REASON.get(
                exc.reason, (StopReason.MAX_STEPS, f"budget_{exc.reason}")
            )
            return finish(
                "", reason, records, events, model_calls, started, error_code=error_code
            )
        except ModelTimeout:
            return finish(
                "",
                StopReason.MODEL_TIMEOUT,
                records,
                events,
                model_calls,
                started,
                error_code="model_timeout",
                error_message="模型调用超时",
            )
        except ModelError as exc:
            return finish(
                "",
                StopReason.MODEL_ERROR,
                records,
                events,
                model_calls,
                started,
                error_code=exc.code,
                error_message=exc.message,
            )

    def _finish(
        self,
        text: str,
        reason: StopReason,
        records: Sequence[ToolCallRecord],
        events: list[HarnessEvent],
        model_calls: int,
        started: float,
        *,
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> LoopResult:
        events.append(
            HarnessEvent(
                sequence=len(events) + 1,
                kind=EventKind.STOPPED,
                data={
                    "reason": reason.value,
                    "error_code": error_code,
                    "error_message": error_message,
                },
            )
        )
        return LoopResult(
            text=text,
            stop_reason=reason,
            tool_calls=list(records),
            events=events,
            model_calls=model_calls,
            duration_ms=int((time.monotonic() - started) * 1000),
            error_code=error_code,
            error_message=error_message,
        )


def _describe_response(response: ModelResponse) -> dict:
    data = {
        "text": response.text[:1000],
        "tool_calls": [call.name for call in response.tool_calls],
        "duration_ms": response.duration_ms,
    }
    if response.usage is not None:
        data["tokens"] = response.usage.total_tokens
    return data


def _describe_record(record: ToolCallRecord) -> dict:
    return {
        "name": record.name,
        "arguments": record.arguments,
        "outcome": record.outcome.value,
        "error_code": record.error_code,
        "duration_ms": record.duration_ms,
        "risk": record.risk.value,
        "attempts": record.attempts,
        "content": record.content[:500],
    }


def _tool_content(record: ToolCallRecord) -> str:
    """把工具结果回灌给模型。失败要说明原因，否则模型会重复同样的错误调用。"""

    if record.succeeded:
        return record.content or "（工具返回空结果）"
    return f"[{record.outcome.value}] {record.error_code or 'error'}：{record.content}"
