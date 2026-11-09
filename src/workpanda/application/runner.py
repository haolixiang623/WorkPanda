"""固定流程推进器。

职责：按服务端流程定义逐步推进任务并生成事件，绝不调用流程白名单之外的执行器。
本模块不含任何具体业务事项。

与"取消"有关的一条硬约束：执行器在工作线程里跑，用户可以在它返回前后任意
时刻点取消。终态与结果事件因此不能分开写——`commit_guarded` 把两者放进同一个
条件事务，以进入本次推进时读到的状态为期望值。谁先写谁生效，迟到的一边连
事件一起丢弃，交回库里的权威状态。过程事件仍即时写，保证 SSE 不延迟。
"""

from collections.abc import Sequence
from typing import Any

from workpanda.application.ports import (
    EventPublisher,
    ExecutionOutcome,
    ExecutionRequest,
    ExecutionResult,
    Executor,
)
from workpanda.domain.confirmation import Confirmation, ConfirmationError, parameter_digest
from workpanda.domain.flow import FlowDefinition, FlowRegistry, StepDefinition, StepKind
from workpanda.domain.models import EventType, Task, TaskEvent, TaskStatus, utc_now
from workpanda.domain.repository import (
    ConfirmationStore,
    PendingEvent,
    StaleTaskState,
    TaskStore,
)
from workpanda.domain.state_machine import ensure_transition


def new_confirmation(task: Task, *, step_id: str, ttl_seconds: int) -> Confirmation:
    """构造绑定任务、流程版本、步骤和参数摘要的一次性凭证。"""

    digest = parameter_digest(task.flow_id, task.flow_version, step_id, task.slots)
    return Confirmation.issue(
        task_id=task.task_id,
        step_id=step_id,
        flow_version=task.flow_version,
        digest=digest,
        user_ref=task.user_ref,
        ttl_seconds=ttl_seconds,
    )


class FlowRunner:
    def __init__(
        self,
        *,
        store: TaskStore,
        confirmations: ConfirmationStore,
        executor: Executor,
        publisher: EventPublisher,
        flows: FlowRegistry,
        confirmation_ttl_seconds: int = 300,
    ) -> None:
        self.store = store
        self.confirmations = confirmations
        self.executor = executor
        self.publisher = publisher
        self.flows = flows
        self.confirmation_ttl_seconds = confirmation_ttl_seconds

    def emit(
        self,
        task: Task,
        event_type: EventType,
        data: dict[str, Any] | None = None,
    ) -> TaskEvent:
        """即时写一条过程事件。终态事件不能走这里，必须随状态一起条件提交。"""
        event = self.store.append_event(task.task_id, event_type, task.status, data)
        self.publisher.publish(task.task_id, event.sequence)
        return event

    def commit_guarded(
        self,
        task: Task,
        events: Sequence[PendingEvent] = (),
        *,
        expected_status: TaskStatus,
    ) -> Sequence[TaskEvent]:
        """把「终态 + 一批结果事件」绑成一笔原子提交，成功后才发布推送。

        与 `emit` 的区别就在这里：状态条件不成立时一行状态、一条事件都不落，
        也就不会出现"取消之后又冒出一条完成记录"。`StaleTaskState` 原样抛给
        调用方——执行侧据此交回权威状态，取消侧据此报错让使用者刷新重试。
        """
        written = self.store.commit(task, events, expected_status=expected_status)
        for event in written:
            self.publisher.publish(task.task_id, event.sequence)
        return written

    def _commit_result(
        self,
        task: Task,
        events: Sequence[PendingEvent],
        expected_status: TaskStatus,
    ) -> Task:
        """执行结果落地。条件写输了就放弃整批结果，绝不拿旧快照重试覆盖。"""
        task.updated_at = utc_now()
        try:
            self.commit_guarded(task, events, expected_status=expected_status)
        except StaleTaskState:
            return self.store.get(task.task_id)
        return task

    def issue_confirmation(self, task: Task, step_id: str) -> None:
        """为当前确认步骤签发一次性凭证，绑定任务、流程版本和参数摘要。"""

        self.confirmations.issue(
            new_confirmation(task, step_id=step_id, ttl_seconds=self.confirmation_ttl_seconds)
        )

    def advance(self, task: Task) -> Task:
        flow = self.flows.require(task.flow_id, task.flow_version)
        if task.is_terminal:
            return task

        # 本次推进以库里的状态为准。工作线程与取消请求并发时，这个快照就是
        # 后面所有条件写的期望值：任何别的写者先动过它，我们的结果就不算数。
        authoritative = self.store.get(task.task_id)
        if authoritative.is_terminal:
            # 传进来的对象可能是上一条命令读到的旧快照：在它被处理到这里之前，
            # 任务已经完成或被取消。终态没有出边，更不该拿它当期望值硬推一步
            # ——那等于用"旧快照 + 库里恰好没变"把终态盖掉，只要库里正好就是
            # 这个终态，条件写反而会成功。
            return authoritative
        expected_status = authoritative.status
        self._move(task, TaskStatus.RUNNING)
        step = flow.step(task.current_step) if task.current_step else flow.next_step(None)

        while step is not None:
            task.current_step = step.step_id
            self.emit(task, EventType.STEP_STARTED, {"step_id": step.step_id, "kind": step.kind})

            paused = self._run_step(task, flow, step, expected_status)
            if paused is not None:
                return paused

            # 执行器刚跑完就可能已被取消。先确认状态还属于自己，避免在已取消
            # 的任务后面补一串"又完成了一步"的噪声事件；真正的仲裁仍在末尾
            # 的条件提交里，这一步只是减少误读。
            current = self.store.get(task.task_id)
            if current.status is not expected_status:
                return current

            if step.step_id not in task.completed_steps:
                task.completed_steps.append(step.step_id)
            self.emit(task, EventType.STEP_COMPLETED, {"step_id": step.step_id})

            following = flow.next_step(step.step_id)
            task.current_step = following.step_id if following else None
            step = following

        self._move(task, TaskStatus.COMPLETED)
        return self._commit_result(
            task,
            [
                PendingEvent(
                    type=EventType.COMPLETED,
                    status=TaskStatus.COMPLETED,
                    data={"result": task.result or {}},
                )
            ],
            expected_status,
        )

    def _run_step(
        self,
        task: Task,
        flow: FlowDefinition,
        step: StepDefinition,
        expected_status: TaskStatus,
    ) -> Task | None:
        """返回任务表示流程已暂停或终止；返回 None 表示该步骤已完成，可继续。"""

        if step.kind is StepKind.COLLECT:
            return self._collect(task, step, expected_status)
        if step.kind is StepKind.EXECUTE:
            return self._execute(task, flow, step, expected_status)
        return self._handoff(task, step, "flow_requested_handoff", expected_status)

    def _collect(
        self, task: Task, step: StepDefinition, expected_status: TaskStatus
    ) -> Task | None:
        missing = [field for field in step.required_fields if not task.slots.get(field)]
        if missing:
            task.pending_fields = missing
            self._move(task, TaskStatus.WAITING_FOR_USER)
            return self._commit_pause(
                task,
                [
                    PendingEvent(
                        type=EventType.CLARIFICATION_REQUIRED,
                        status=task.status,
                        data={
                            "step_id": step.step_id,
                            "fields": missing,
                            "prompt": step.prompt or "",
                        },
                    )
                ],
                expected_status,
            )

        task.pending_fields = []
        return None

    def _execute(
        self,
        task: Task,
        flow: FlowDefinition,
        step: StepDefinition,
        expected_status: TaskStatus,
    ) -> Task | None:
        credential = None
        if step.requires_confirmation:
            credential = self._usable_credential(task, flow, step)
            if credential is None:
                # 没有有效凭证，或参数在确认后发生变化：必须重新确认，不能直接执行。
                task.pending_fields = []
                self._move(task, TaskStatus.WAITING_FOR_CONFIRMATION)
                return self._commit_pause(
                    task,
                    [
                        PendingEvent(
                            type=EventType.CONFIRMATION_REQUIRED,
                            status=task.status,
                            data={
                                "step_id": step.step_id,
                                "summary": dict(task.slots),
                                "message": step.confirm_message or step.title,
                            },
                        )
                    ],
                    expected_status,
                )

        if credential is not None:
            try:
                self.confirmations.consume(credential.token)
            except ConfirmationError:
                self._move(task, TaskStatus.WAITING_FOR_CONFIRMATION)
                return self._commit_pause(
                    task,
                    [
                        PendingEvent(
                            type=EventType.CONFIRMATION_REQUIRED,
                            status=task.status,
                            data={
                                "step_id": step.step_id,
                                "reason": "credential_already_used",
                            },
                        )
                    ],
                    expected_status,
                )

        result = self.executor.execute(
            ExecutionRequest(
                task_id=task.task_id,
                flow_id=flow.flow_id,
                flow_version=flow.version,
                step_id=step.step_id,
                tool=step.tool or "",
                instruction=step.instruction,
                arguments=dict(task.slots),
            )
        )
        # 执行期间用户可能已经点了取消。执行离环之后这是真实并发，不是理论情况：
        # store 里的状态才是权威的。这里只是快速路径——真正的互斥由末尾的
        # 条件提交保证，因为"读状态"和"写结果"之间还有一段时间。
        current = self.store.get(task.task_id)
        if current.status is not expected_status:
            return current
        return self._apply_result(task, step, result, expected_status)

    def _usable_credential(
        self, task: Task, flow: FlowDefinition, step: StepDefinition
    ):
        digest = parameter_digest(flow.flow_id, flow.version, step.step_id, task.slots)
        credential = self.confirmations.latest(task.task_id, step.step_id)
        if credential is None:
            return None
        if not credential.is_usable(
            digest=digest, flow_version=flow.version, user_ref=task.user_ref
        ):
            return None
        return credential

    def _apply_result(
        self,
        task: Task,
        step: StepDefinition,
        result: ExecutionResult,
        expected_status: TaskStatus,
    ) -> Task | None:
        if result.outcome is ExecutionOutcome.SUCCEEDED:
            task.result = citizen_result(step, result.output)
            return None

        # 失败与未确认同样要留痕：事后必须能回答"当时模型说了什么、依据是什么"，
        # 否则无法复盘，也无法向群众解释为什么要转人工。但留痕与状态必须同生共死：
        # 条件写输了，这些记录一条都不该出现。
        diagnostics = _diagnostics(result)
        events: list[PendingEvent] = []

        if result.outcome is ExecutionOutcome.UNCONFIRMED:
            # 外部结果未知：不得重试，保留为待确认并引导人工核实。
            task.failure_code = result.error_code or "outcome_unconfirmed"
            task.failure_message = result.message
            self._move(task, TaskStatus.OUTCOME_UNCONFIRMED)
            events.append(
                PendingEvent(
                    type=EventType.OUTCOME_UNCONFIRMED,
                    status=task.status,
                    data={"step_id": step.step_id, **diagnostics},
                )
            )
            return self._commit_result(task, events, expected_status)

        task.failure_code = result.error_code or "execution_failed"
        task.failure_message = result.message
        # 这条早于状态翻转，事件里记录的状态仍是 RUNNING（与改造前一致）。
        events.append(
            PendingEvent(
                type=EventType.EXECUTION_FAILED,
                status=task.status,
                data={"step_id": step.step_id, **diagnostics},
            )
        )
        if step.fail_to_handoff:
            return self._handoff(task, step, "execution_failed", expected_status, events)

        self._move(task, TaskStatus.FAILED)
        events.append(
            PendingEvent(
                type=EventType.FAILED,
                status=task.status,
                data={"step_id": step.step_id},
            )
        )
        return self._commit_result(task, events, expected_status)

    def _handoff(
        self,
        task: Task,
        step: StepDefinition,
        reason: str,
        expected_status: TaskStatus,
        events: Sequence[PendingEvent] = (),
    ) -> Task:
        self._move(task, TaskStatus.HANDOFF_REQUIRED)
        batch = [
            *events,
            PendingEvent(
                type=EventType.HANDOFF_REQUIRED,
                status=task.status,
                data={
                    "step_id": step.step_id,
                    "reason": reason,
                    "completed_steps": list(task.completed_steps),
                    "failure_code": task.failure_code,
                },
            ),
        ]
        return self._commit_result(task, batch, expected_status)

    def _move(self, task: Task, status: TaskStatus) -> None:
        if status is not task.status:
            ensure_transition(task.status, status)
            task.status = status
            task.updated_at = utc_now()

    def _commit_pause(
        self,
        task: Task,
        events: Sequence[PendingEvent],
        expected_status: TaskStatus,
    ) -> Task:
        """把「暂停（等输入 / 等确认）+ 待办事件」绑成一笔条件提交。

        暂停看着只是过程中间态，但它同样是一次"谁说了算"的判定：任务若在推进
        途中被取消，这里就不该再落一条"请补充字段"的待办——否则界面上会出现
        一条挂在已取消任务上的待输入提示，人还会照着它再填一遍。

        与执行结果落地共用 `_commit_result` 的仲裁：条件写输了就整批丢弃，
        交回库里的权威状态。
        """
        return self._commit_result(task, events, expected_status)


def citizen_result(step: StepDefinition, output: dict[str, Any]) -> dict[str, Any]:
    """把执行器的原始输出收成**办结页能显示的东西**。

    执行器的 `output` 里混着两类完全不同的东西：群众要读的结论，和平台自己的运行
    痕迹（trace、步骤标识、工具调用、token 数）。后者印到办结页上，群众看不懂，
    `trace_id` 更不该露面。

    所以放行规则是**白名单**：只有流程作者在步骤上声明过 `answer_label` 的那一段
    正文才会出去，其余一概不进 `task.result`。声明了标签却没拿到正文时退回
    `summary`（模型至少说了做了什么），两者都没有就给出空结果——空着比堆元数据好。

    未声明 `answer_label` 的流程保持原样返回，那是兼容旧流程的行为，不是推荐做法：
    它会把平台元数据一并带出去，这正是群众页已知的那条缺陷。
    """

    if not step.answer_label:
        return output

    answer = output.get("answer")
    if not (isinstance(answer, str) and answer.strip()):
        verification = output.get("verification")
        summary = verification.get("summary") if isinstance(verification, dict) else None
        answer = summary if isinstance(summary, str) else ""
    answer = answer.strip()
    return {step.answer_label: answer} if answer else {}


def _diagnostics(result: ExecutionResult) -> dict[str, Any]:
    """从执行结果里提取可留痕的诊断信息（只保留便于复盘的最小集合）。"""

    output = result.output or {}
    verification = output.get("verification") or {}
    return {
        "failure_code": result.error_code,
        "message": result.message,
        # 真实执行器会给出停止原因与工具调用，失败复盘时这两项最关键。
        "stop_reason": output.get("stop_reason"),
        "tool_calls": output.get("tool_calls") or [],
        "model_text": str(output.get("text", ""))[:1000],
        "verification": verification if isinstance(verification, dict) else {},
    }
