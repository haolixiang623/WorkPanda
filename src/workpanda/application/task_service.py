"""任务应用服务：编排流程解析、澄清、确认和取消。

服务端是唯一权威：客户端只能提交输入、回答和确认意图，
不能自行推进步骤、跳过确认或指定要执行的工具。
"""

from collections.abc import Sequence
from uuid import UUID

from workpanda.application.errors import IntentUnresolved, RequestRejected, TaskConflict
from workpanda.application.ports import IntentRouter
from workpanda.application.runner import FlowRunner
from workpanda.domain.flow import FlowDefinition, FlowNotFound, FlowNotRunnable, FlowRegistry
from workpanda.domain.models import (
    EventType,
    Task,
    TaskAnswerRequest,
    TaskConfirmRequest,
    TaskCreateRequest,
    TaskEvent,
    TaskStatus,
    utc_now,
)
from workpanda.domain.repository import ConfirmationStore, TaskStore

BLOCKED_STATUSES: frozenset[TaskStatus] = frozenset(
    {
        TaskStatus.COMPLETED,
        TaskStatus.FAILED,
        TaskStatus.CANCELLED,
        TaskStatus.HANDOFF_REQUIRED,
        TaskStatus.OUTCOME_UNCONFIRMED,
    }
)


class TaskService:
    def __init__(
        self,
        *,
        store: TaskStore,
        confirmations: ConfirmationStore,
        runner: FlowRunner,
        flows: FlowRegistry,
        router: IntentRouter,
        confidence_threshold: float = 0.6,
    ) -> None:
        self.store = store
        self.confirmations = confirmations
        self.runner = runner
        self.flows = flows
        self.router = router
        self.confidence_threshold = confidence_threshold

    # ---------- 命令 ----------

    def create_task(self, request: TaskCreateRequest) -> Task:
        flow = self._resolve_flow(request)
        task = self.store.create(
            Task(
                conversation_id=request.conversation_id,
                user_ref=request.user_ref,
                flow_id=flow.flow_id,
                flow_version=flow.version,
                slots={key: value for key, value in request.slots.items() if value},
            )
        )
        self.runner.emit(task, EventType.ACCEPTED, {"flow_id": flow.flow_id})
        return self.runner.advance(task)

    def answer(self, task_id: UUID, request: TaskAnswerRequest) -> Task:
        task = self._active_task(task_id)
        step = self._current_step(task)
        # 允许补充流程中任意收集节点的字段；当前节点决定提示什么，不代表只能改它。
        allowed = self._declared_fields(task)
        unknown = sorted(set(request.values) - allowed)
        if unknown:
            raise RequestRejected(f"fields are not accepted for this flow: {', '.join(unknown)}")

        task.slots.update({key: value for key, value in request.values.items() if value})
        task.pending_fields = []
        task.updated_at = utc_now()
        self.store.save(task)
        self.runner.emit(task, EventType.SLOTS_UPDATED, {"fields": sorted(request.values)})
        # 参数变化后旧凭证必须失效，执行节点会重新要求确认。
        self.confirmations.revoke(task.task_id, step.step_id)
        return self.runner.advance(task)

    def confirm(self, task_id: UUID, request: TaskConfirmRequest) -> Task:
        task = self._active_task(task_id)
        if task.status is not TaskStatus.WAITING_FOR_CONFIRMATION:
            raise TaskConflict("task is not waiting for confirmation")
        if task.current_step != request.step_id:
            raise TaskConflict("confirmation does not match the current step")

        step = self._current_step(task)
        if not request.accepted:
            task.pending_fields = []
            task.updated_at = utc_now()
            self.store.save(task)
            self.runner.emit(task, EventType.CONFIRMATION_DECLINED, {"step_id": step.step_id})
            self.confirmations.revoke(task.task_id, step.step_id)
            return task

        # 凭证绑定任务、流程版本、步骤和当前参数摘要，执行前会再次校验。
        self.runner.issue_confirmation(task, step.step_id)
        self.runner.emit(task, EventType.CONFIRMATION_ACCEPTED, {"step_id": step.step_id})
        return self.runner.advance(task)

    def cancel(self, task_id: UUID) -> Task:
        task = self.store.get(task_id)
        if task.status in BLOCKED_STATUSES:
            raise TaskConflict(f"task in {task.status} state cannot be cancelled")

        if task.current_step:
            self.confirmations.revoke(task.task_id, task.current_step)
        task.status = TaskStatus.CANCELLED
        task.updated_at = utc_now()
        self.store.save(task)
        self.runner.emit(task, EventType.CANCELLED, {"step_id": task.current_step})
        return task

    # ---------- 查询 ----------

    def get_task(self, task_id: UUID) -> Task:
        return self.store.get(task_id)

    def events(self, task_id: UUID, after_sequence: int = 0) -> Sequence[TaskEvent]:
        self.store.get(task_id)
        return self.store.events(task_id, after_sequence)

    # ---------- 内部 ----------

    def _resolve_flow(self, request: TaskCreateRequest) -> FlowDefinition:
        if request.flow_id:
            try:
                return self.flows.resolve(request.flow_id, request.flow_version)
            except (FlowNotFound, FlowNotRunnable) as exc:
                raise RequestRejected(str(exc)) from exc

        match = self.router.route(request.input_text)
        if match is None or match.confidence < self.confidence_threshold:
            raise IntentUnresolved("无法确定办理事项，需要先向群众澄清，或转人工处理。")
        try:
            return self.flows.require(match.flow_id, match.flow_version)
        except (FlowNotFound, FlowNotRunnable) as exc:
            raise RequestRejected(str(exc)) from exc

    def _active_task(self, task_id: UUID) -> Task:
        task = self.store.get(task_id)
        if task.status in BLOCKED_STATUSES:
            raise TaskConflict(f"task in {task.status} state cannot be modified")
        return task

    def _current_step(self, task: Task):
        flow = self.flows.require(task.flow_id, task.flow_version)
        if not task.current_step:
            raise TaskConflict("task has no active step")
        step = flow.step(task.current_step)
        if step is None:
            raise TaskConflict("task references an unknown step")
        return step

    def _declared_fields(self, task: Task) -> set[str]:
        flow = self.flows.require(task.flow_id, task.flow_version)
        return {field for step in flow.steps for field in step.required_fields}
