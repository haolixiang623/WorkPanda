"""有界执行循环：模型 → 工具 → 观察 → 再决定，直到给出结论或用尽预算。

不变式：

- 循环必须可终止：步数、工具调用次数、总耗时三重上限；
- 超预算不是"失败"，而是"未确认"——此刻可能已经调用过有副作用的工具；
- 每一次模型调用和工具调用都写进 trace，事后必须能完整重放发生了什么；
- 每次工具调用落账后回调进度快照，使中断后可以从最近一次已知状态续跑；
- 模型调用的瞬时失败可以有限重试，但绝不重试工具：模型调用没有副作用，
  工具调用可能有；
- 取消只在边界生效，已提交的调用跑完并留痕，取消同样是"未确认"而不是"失败"。
"""

from __future__ import annotations

import time
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from enum import StrEnum

from workpanda.harness.budget import Budget, BudgetExhausted
from workpanda.harness.cancel import Cancelled, CancelToken
from workpanda.harness.checkpoint import ExecutionSnapshot
from workpanda.harness.clients import ModelClient, ModelError, ModelTimeout
from workpanda.harness.models import (
    CRASH_ERROR_CODE,
    EventKind,
    HarnessEvent,
    HarnessMessage,
    MessageRole,
    ModelResponse,
    RiskLevel,
    ToolCall,
    ToolCallRecord,
    ToolOutcome,
    ToolSpec,
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
    # 人取消了这次执行。既不是成功也不是失败：此前可能已经发生过副作用。
    CANCELLED = "cancelled"


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

    @property
    def cancelled(self) -> bool:
        """被人取消。与"失败""超预算"并列的第三种非结论收尾。"""

        return self.stop_reason is StopReason.CANCELLED


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
        model_retries: int = 0,
        model_retry_backoff_seconds: float = 0.5,
        tool_concurrency: int = 1,
    ) -> None:
        self._client = client
        self._max_steps = max_steps
        self._max_tool_calls = max_tool_calls
        self._max_tokens = max_tokens
        self._deadline_seconds = deadline_seconds
        self._max_empty_responses = max_empty_responses
        self._model_retries = max(0, model_retries)
        self._model_retry_backoff_seconds = max(0.0, model_retry_backoff_seconds)
        # 1 表示串行（默认）。并发只对幂等工具组生效，见 `_dispatch_tools`。
        self._tool_concurrency = max(1, tool_concurrency)

    def run(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        grant: ToolGrant,
        context: ToolContext,
        resume: ExecutionSnapshot | None = None,
        on_progress: Callable[[ExecutionSnapshot], None] | None = None,
        cancel: CancelToken | None = None,
    ) -> LoopResult:
        """执行一次有界循环。

        `resume` 不为空时从断点继续：对话、已发生的工具调用与预算用量都接着算，
        不重新发一次"从头开始"的请求；`on_progress` 在每个工具调用落账后回调，
        供调用方落 checkpoint——回调失败不得击穿执行。

        `cancel` 是外部（通常来自另一个线程的用户请求）的取消信号。它只在边界上
        生效——下一轮模型调用之前、下一条工具调用之前、重试退避等待中。已经提交
        的调用不会被中断也不会被假装成没发生：结果照常落账，结论是"未确认"。
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

        def check_cancel(stage: str, skipped: Sequence[str] = ()) -> None:
            """边界检查：已取消就留痕并中止。

            `stage` 说明停在哪一步，`skipped` 是被这一停省下的调用（如果此时已经
            知道有哪几条）——事后核对"取消之后还有没有发出去什么"全靠这两个字段。
            """

            if cancel is None or not cancel.cancelled:
                return
            emit(
                EventKind.CANCELLED,
                {
                    "stage": stage,
                    "reason": cancel.reason,
                    "skipped_calls": list(skipped),
                },
            )
            raise Cancelled(cancel.reason or "cancelled", stage)

        def count_model_call() -> None:
            # 失败的尝试同样是一次真实调用，必须计入：否则"重试了 3 次"在 trace 上看不出来。
            nonlocal model_calls
            model_calls += 1

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

        def accept(call: ToolCall, record: ToolCallRecord) -> None:
            """一条调用落账：先留痕，再回灌模型，最后写进度快照。

            顺序固定，串行与并行两条路径共用：并行的只是等待，顺序语义不变。
            """

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

        def skip(
            calls: Sequence[ToolCall],
            cause: BaseException,
            *,
            mirror_messages: bool = False,
        ) -> None:
            """把因预算用尽或取消而没执行的调用记成 SKIPPED。

            这件事以前是空缺的：预算在中途耗尽时，剩余调用一条记录都没有，
            事后只能看到"模型要求了 5 件事、做了 3 件"，却答不出另外 2 件是什么。
            取消路径稍好一点（事件里留了名字），但同样不可检索、不可计数。

            `mirror_messages=True` 表示助手那条"我要调这些工具"已经进了对话历史
            （批次执行到一半才中止）。此时必须给每条没跑的调用补一条工具回执：
            OpenAI 兼容接口要求 assistant.tool_calls 与 tool 消息一一对应，
            否则断点续跑会拿着一段畸形对话再发给模型，直接被网关 400 拒掉。
            取消发生在模型刚给出清单、工具一条未动时，助手消息还没入历史，
            反而不能补回执——补了就是凭空多出一条没有来源的观察。

            另外三条边界刻意保持不变：

            - 不消耗预算：它们没有被调用过；
            - 不当副作用：`build_effects` 只认 OK/TIMEOUT，SKIPPED 确定没改动外部世界；
            - 不算"成功依据"：`ToolCallRecord.succeeded` 只认 OK。
            """

            code = (
                "cancelled"
                if isinstance(cause, Cancelled)
                else f"budget_{getattr(cause, 'reason', 'exhausted')}"
            )
            for call in calls:
                spec = grant.spec_for(call.name)
                record = ToolCallRecord(
                    call_id=call.call_id,
                    name=call.name,
                    arguments=call.arguments,
                    outcome=ToolOutcome.SKIPPED,
                    content="本次执行已中止，该调用未执行。",
                    error_code=code,
                    risk=spec.risk if spec is not None else RiskLevel.LOW,
                )
                records.append(record)
                if mirror_messages:
                    messages.append(
                        HarnessMessage(
                            role=MessageRole.TOOL,
                            tool_call_id=call.call_id,
                            name=call.name,
                            content=_tool_content(record),
                        )
                    )
                emit(
                    EventKind.TOOL_SKIPPED,
                    {
                        "call_id": call.call_id,
                        "name": call.name,
                        "error_code": code,
                        "reason": _skip_reason(cause),
                    },
                )
            # 中止留下的状态也要可恢复：断点必须知道"这几件没做过"，
            # 否则续跑时会以为它们还在队列里，甚至重复提交。
            report_progress()

        def finish(*args, **kwargs) -> LoopResult:
            """统一收尾：把预算用量补进 stopped 事件，事后能判断是否被预算掐断。"""

            result = self._finish(*args, **kwargs)
            result.tokens = budget.tokens
            result.events[-1].data["budget"] = budget.snapshot()
            return result

        try:
            while True:
                # 取消优先于预算：用户已经不要这个结果了，没必要再消耗配额。
                check_cancel("before_model")
                budget.consume_step()
                specs = grant.specs
                emit(
                    EventKind.MODEL_REQUESTED,
                    {"step": budget.steps, "tools": [spec.name for spec in specs]},
                )
                response = self._complete(
                    messages=messages,
                    tools=specs,
                    budget=budget,
                    emit=emit,
                    on_attempt=count_model_call,
                    check_cancel=check_cancel,
                    cancel=cancel,
                )
                emit(EventKind.MODEL_RESPONDED, _describe_response(response))

                if response.usage is not None:
                    # 模型服务可能不上报用量；不上报就不统计，绝不用字符数估算。
                    budget.consume_tokens(response.usage.total_tokens)

                if response.tool_calls:
                    # 模型刚给出调用清单、工具还没动：取消赶在这时到达，一条都不执行。
                    # 这正是"点了取消却还是提交了"最容易发生的窗口。
                    try:
                        check_cancel(
                            "before_tool", skipped=[call.name for call in response.tool_calls]
                        )
                    except Cancelled as exc:
                        # 一条都没跑，但模型要求过：省下的这几条同样要留痕。
                        # 助手消息尚未入历史，不能补工具回执。
                        skip(response.tool_calls, exc)
                        raise
                    messages.append(
                        HarnessMessage(
                            role=MessageRole.ASSISTANT,
                            content=response.text,
                            tool_calls=response.tool_calls,
                        )
                    )
                    self._dispatch_tools(
                        response.tool_calls,
                        grant=grant,
                        context=context,
                        budget=budget,
                        accept=accept,
                        emit=emit,
                        check_cancel=check_cancel,
                        skip=skip,
                    )
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

        except Cancelled as exc:
            # 取消不是失败：此前可能已经发生过副作用，只能交给人工核对。
            return finish(
                "",
                StopReason.CANCELLED,
                records,
                events,
                model_calls,
                started,
                error_code="cancelled",
                error_message=(
                    f"执行已取消（原因 {exc.reason}，边界 {exc.stage}），"
                    "此前的动作可能已经生效，须人工核实。"
                ),
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

    def _dispatch_tools(
        self,
        calls: Sequence[ToolCall],
        *,
        grant: ToolGrant,
        context: ToolContext,
        budget: Budget,
        accept: Callable[[ToolCall, ToolCallRecord], None],
        emit: Callable[[EventKind, dict], None],
        check_cancel: Callable[..., None] | None = None,
                skip: Callable[..., None] | None = None,
    ) -> None:
        """执行同一轮的多个工具调用，逐条交给 `accept` 落账。

        并行只在**整批都是幂等工具、且整批放得进剩余调用预算**时开启：

        - 非幂等工具不能并行——两个线程会同时通过"重复副作用"判定，等于把
          「同一副作用只发生一次」这条不变式交给调度碰运气；
        - 预算放不下整批时退回串行——先执行的调用仍然留痕，与原有语义一致；
        - 并行只改变等待方式，不改变顺序：记录、事件、回灌消息都按模型给出的顺序。

        单条调用抛出任何异常都不能击穿整轮：记录成 `tool_crashed` 继续走。

        取消的边界在这里最窄：串行时每条调用之前检查一次，被省下的就是后面那几条；
        并行批次一旦提交就整批跑完（它们都是幂等工具，重放安全），结果照常落账，
        真正的停发生在下一轮模型调用之前。

        中止（预算用尽或取消）时，没执行的调用交给 `skip` 记成 SKIPPED：
        否则这段空档在留痕里完全不存在，事后无法回答"模型还要求了什么没做"。
        """

        guard = check_cancel or _no_cancel
        on_skip = skip or _no_skip
        workers = self._parallel_workers(calls, grant, budget)
        if workers <= 0:
            for index, call in enumerate(calls):
                pending = list(calls[index:])
                try:
                    guard("before_tool", [item.name for item in pending])
                    budget.consume_tool_call()
                except (Cancelled, BudgetExhausted) as exc:
                    # 助手消息已在历史里：补回执，续跑才拿得到合法对话。
                    on_skip(pending, exc, mirror_messages=True)
                    raise
                accept(call, grant.call(call, self._tool_context(context, budget)))
            return

        try:
            guard("before_tool", [call.name for call in calls])
        except Cancelled as exc:
            # 并发批次一条都没提交：整批都算省下的，不能凭空消失。
            on_skip(calls, exc, mirror_messages=True)
            raise
        emit(
            EventKind.TOOLS_PARALLEL,
            {
                "count": len(calls),
                "names": [call.name for call in calls],
                "workers": workers,
            },
        )
        # 先把整批预算扣掉：并发执行中途才发现预算不够，等于让部分调用"白跑"。
        # 预扣是原子的（全成或全不成）：半途失败会留下"扣了两条、只提交一条"的错账。
        # 扣不动时这一批一条都没提交，同样要留痕——助手消息已经在历史里，
        # 不补回执就会留下畸形对话，续跑必被网关 400 拒。
        try:
            budget.reserve_tool_calls(len(calls))
        except BudgetExhausted as exc:
            on_skip(calls, exc, mirror_messages=True)
            raise

        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="wp-tool") as pool:
            futures = [
                pool.submit(grant.call, call, self._tool_context(context, budget))
                for call in calls
            ]
            for call, future in zip(calls, futures, strict=True):
                try:
                    record = future.result()
                except Exception as exc:  # noqa: BLE001 - 单条异常不能击穿整轮
                    record = _crashed(call, exc, risk=_risk_of(grant, call))
                accept(call, record)

    def _parallel_workers(
        self, calls: Sequence[ToolCall], grant: ToolGrant, budget: Budget
    ) -> int:
        """返回可用的并发度；0 表示这批调用只能串行。"""

        if self._tool_concurrency <= 1 or len(calls) < 2:
            return 0
        if budget.remaining_tool_calls < len(calls):
            return 0
        for call in calls:
            spec = grant.spec_for(call.name)
            if spec is None or not spec.idempotent:
                return 0
        return min(self._tool_concurrency, len(calls))

    def _tool_context(self, context: ToolContext, budget: Budget) -> ToolContext:
        return context.model_copy(update={"remaining_seconds": budget.remaining_seconds})

    def _complete(
        self,
        *,
        messages: Sequence[HarnessMessage],
        tools: Sequence[ToolSpec],
        budget: Budget,
        emit: Callable[[EventKind, dict], None],
        on_attempt: Callable[[], None],
        check_cancel: Callable[..., None] | None = None,
        cancel: CancelToken | None = None,
    ) -> ModelResponse:
        """调用模型，对"瞬时失败"做有界重试。

        三条边界：

        - 只重试客户端判定为 retryable 的错误（429 / 5xx / 传输失败）。鉴权、参数、
          模型不存在这类错误重试一万次也是同一个结果，重试只会放大故障；
        - 重试次数与退避都有上限，且必须留在步骤总耗时以内。否则一次网关抖动
          会把整步时间耗光，最后连"为什么没结果"都说不清；
        - 模型调用本身没有副作用，重试不会重复提交任何业务动作。

        每次**尝试**都会先回调 `on_attempt`（失败的尝试同样是真实发生的调用，
        必须计入 `model_calls`），重试另留一条 `model_retried` 事件，
        避免事后看成"只调了一次模型"。

        退避等待可被取消打断：用户点了取消却还要等 2 秒退避，等于点了没反应。
        """

        guard = check_cancel or _no_cancel
        attempts = 0
        while True:
            attempts += 1
            on_attempt()
            try:
                return self._client.complete(messages=messages, tools=tools)
            except ModelError as exc:
                if not exc.retryable or attempts > self._model_retries:
                    raise
                delay = self._model_retry_backoff_seconds * attempts
                if budget.remaining_seconds <= 0 or delay >= budget.remaining_seconds:
                    # 没有时间退避就别假装还能救：直接按原错误上报。
                    raise
                emit(
                    EventKind.MODEL_RETRIED,
                    {
                        "attempt": attempts,
                        "error_code": exc.code,
                        "delay_ms": int(delay * 1000),
                    },
                )
                if cancel is not None and cancel.wait(delay):
                    # 用户已经不要结果了：不必把退避时间耗完。
                    guard("retry_backoff")
                    raise Cancelled(cancel.reason or "cancelled", "retry_backoff") from exc
                if cancel is None:
                    time.sleep(delay)

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


def _no_cancel(stage: str, skipped: Sequence[str] = ()) -> None:
    """未启用取消时的空闸门，免得每个调用点都判一次 None。"""

    return None


def _no_skip(
    calls: Sequence[ToolCall], cause: BaseException, *, mirror_messages: bool = False
) -> None:
    """未接入留痕时的空动作，保持 `_dispatch_tools` 可在测试里单独调用。"""

    return None


def _skip_reason(cause: BaseException) -> str:
    """把中止原因压成一句给人看的话，避免运维对着错误码猜。"""

    if isinstance(cause, Cancelled):
        return f"执行被取消（原因 {cause.reason}，边界 {cause.stage}）"
    reason = getattr(cause, "reason", "exhausted")
    return f"预算用尽（{reason}）"


def _risk_of(grant: ToolGrant, call: ToolCall) -> RiskLevel:
    """崩溃记录要用的风险等级，按工具声明取；查不到就按最高风险处理。

    取不到声明说明登记表与调用对不上（热更新、注册表落后），此时宁可把这次
    崩溃标成高风险让人看见，也不能默认 low 把它埋掉。
    """

    spec = grant.spec_for(call.name)
    return RiskLevel.HIGH if spec is None else spec.risk


def _crashed(call: ToolCall, exc: BaseException, *, risk: RiskLevel) -> ToolCallRecord:
    """并行调度里单条调用抛出未预期异常时的兜底记录。

    宁可留下一条"执行异常"，也不能让整轮带着"有条调用不知去向"继续跑。

    `risk` 由调用方带进来、且**不给默认值**：崩溃记录若一律记成 low，高风险提交
    出的事在归档里就看不出来了。`CRASH_ERROR_CODE` 也不只是"另一个错误码"——
    账本据此按"结果未知"入账，断点据此拒绝续跑。
    """

    return ToolCallRecord(
        call_id=call.call_id,
        name=call.name,
        arguments=call.arguments,
        outcome=ToolOutcome.ERROR,
        content=f"工具执行异常：{type(exc).__name__}",
        error_code=CRASH_ERROR_CODE,
        risk=risk,
    )


def _describe_response(response: ModelResponse) -> dict:
    data = {
        "text": response.text[:1000],
        "tool_calls": [call.name for call in response.tool_calls],
        "duration_ms": response.duration_ms,
    }
    if response.reasoning:
        # 推理过程单独留痕：只记长度与一小段预览，落盘前由执行器统一脱敏。
        # 它绝不写进 text，契约解析器因此看不到它，也就拿不到"口头成功"的机会。
        data["reasoning_chars"] = len(response.reasoning)
        data["reasoning_preview"] = response.reasoning[:200]
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
