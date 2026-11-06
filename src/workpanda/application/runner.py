"""固定流程推进器。

职责：按服务端流程定义逐步推进任务并生成事件，绝不调用流程白名单之外的执行器。
本模块不含任何具体业务事项。
"""

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
from workpanda.domain.repository import ConfirmationStore, TaskStore
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
        event = self.store.append_event(task.task_id, event_type, task.status, data)
        self.publisher.publish(task.task_id, event.sequence)
        return event

    def issue_confirmation(self, task: Task, step_id: str) -> None:
        """为当前确认步骤签发一次性凭证，绑定任务、流程版本和参数摘要。"""

        self.confirmations.issue(
            new_confirmation(task, step_id=step_id, ttl_seconds=self.confirmation_ttl_seconds)
        )

    def advance(self, task: Task) -> Task:
        flow = self.flows.require(task.flow_id, task.flow_version)
        if task.is_terminal:
            return task

        self._move(task, TaskStatus.RUNNING)
        step = flow.step(task.current_step) if task.current_step else flow.next_step(None)

        while step is not None:
            task.current_step = step.step_id
            self.emit(task, EventType.STEP_STARTED, {"step_id": step.step_id, "kind": step.kind})

            paused = self._run_step(task, flow, step)
            if paused is not None:
                return paused

            if step.step_id not in task.completed_steps:
                task.completed_steps.append(step.step_id)
            self.emit(task, EventType.STEP_COMPLETED, {"step_id": step.step_id})

            following = flow.next_step(step.step_id)
            task.current_step = following.step_id if following else None
            step = following

        self._move(task, TaskStatus.COMPLETED)
        self.emit(task, EventType.COMPLETED, {"result": task.result or {}})
        return self._persist(task)

    def _run_step(self, task: Task, flow: FlowDefinition, step: StepDefinition) -> Task | None:
        """返回任务表示流程已暂停或终止；返回 None 表示该步骤已完成，可继续。"""

        if step.kind is StepKind.COLLECT:
            return self._collect(task, step)
        if step.kind is StepKind.EXECUTE:
            return self._execute(task, flow, step)
        return self._handoff(task, step, reason="flow_requested_handoff")

    def _collect(self, task: Task, step: StepDefinition) -> Task | None:
        missing = [field for field in step.required_fields if not task.slots.get(field)]
        if missing:
            task.pending_fields = missing
            self._move(task, TaskStatus.WAITING_FOR_USER)
            self.emit(
                task,
                EventType.CLARIFICATION_REQUIRED,
                {"step_id": step.step_id, "fields": missing, "prompt": step.prompt or ""},
            )
            return self._persist(task)

        task.pending_fields = []
        return None

    def _execute(self, task: Task, flow: FlowDefinition, step: StepDefinition) -> Task | None:
        credential = None
        if step.requires_confirmation:
            credential = self._usable_credential(task, flow, step)
            if credential is None:
                # 没有有效凭证，或参数在确认后发生变化：必须重新确认，不能直接执行。
                task.pending_fields = []
                self._move(task, TaskStatus.WAITING_FOR_CONFIRMATION)
                self.emit(
                    task,
                    EventType.CONFIRMATION_REQUIRED,
                    {
                        "step_id": step.step_id,
                        "summary": dict(task.slots),
                        "message": step.confirm_message or step.title,
                    },
                )
                return self._persist(task)

        if credential is not None:
            try:
                self.confirmations.consume(credential.token)
            except ConfirmationError:
                self._move(task, TaskStatus.WAITING_FOR_CONFIRMATION)
                self.emit(
                    task,
                    EventType.CONFIRMATION_REQUIRED,
                    {"step_id": step.step_id, "reason": "credential_already_used"},
                )
                return self._persist(task)

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
        return self._apply_result(task, step, result)

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
        self, task: Task, step: StepDefinition, result: ExecutionResult
    ) -> Task | None:
        if result.outcome is ExecutionOutcome.SUCCEEDED:
            task.result = result.output
            return None

        # 失败与未确认同样要留痕：事后必须能回答"当时模型说了什么、依据是什么"，
        # 否则无法复盘，也无法向群众解释为什么要转人工。
        diagnostics = _diagnostics(result)

        if result.outcome is ExecutionOutcome.UNCONFIRMED:
            # 外部结果未知：不得重试，保留为待确认并引导人工核实。
            task.failure_code = result.error_code or "outcome_unconfirmed"
            task.failure_message = result.message
            self._move(task, TaskStatus.OUTCOME_UNCONFIRMED)
            self.emit(
                task,
                EventType.OUTCOME_UNCONFIRMED,
                {"step_id": step.step_id, **diagnostics},
            )
            return self._persist(task)

        task.failure_code = result.error_code or "execution_failed"
        task.failure_message = result.message
        self.emit(task, EventType.EXECUTION_FAILED, {"step_id": step.step_id, **diagnostics})
        if step.fail_to_handoff:
            return self._handoff(task, step, reason="execution_failed")

        self._move(task, TaskStatus.FAILED)
        self.emit(task, EventType.FAILED, {"step_id": step.step_id})
        return self._persist(task)

    def _handoff(self, task: Task, step: StepDefinition, reason: str) -> Task:
        self._move(task, TaskStatus.HANDOFF_REQUIRED)
        self.emit(
            task,
            EventType.HANDOFF_REQUIRED,
            {
                "step_id": step.step_id,
                "reason": reason,
                "completed_steps": list(task.completed_steps),
                "failure_code": task.failure_code,
            },
        )
        return self._persist(task)

    def _move(self, task: Task, status: TaskStatus) -> None:
        if status is not task.status:
            ensure_transition(task.status, status)
            task.status = status
            task.updated_at = utc_now()

    def _persist(self, task: Task) -> Task:
        task.updated_at = utc_now()
        return self.store.save(task)


def _diagnostics(result: ExecutionResult) -> dict[str, Any]:
    """从执行结果里提取可留痕的诊断信息（只保留便于复盘的最小集合）。"""

    output = result.output or {}
    verification = output.get("verification") or {}
    return {
        "failure_code": result.error_code,
        "message": result.message,
        "model_text": str(output.get("text", ""))[:1000],
        "verification": verification if isinstance(verification, dict) else {},
    }
