"""任务应用服务：编排流程解析、澄清、确认和取消。

服务端是唯一权威：客户端只能提交输入、回答和确认意图，
不能自行推进步骤、跳过确认或指定要执行的工具。
"""

from collections.abc import Sequence
from typing import Any
from uuid import UUID

from workpanda.application.errors import (
    CancelNotDelivered,
    IntentUnresolved,
    RequestRejected,
    TaskConflict,
)
from workpanda.application.ports import IntentRouter, StepCancellation
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
from workpanda.domain.paging import EventPage, EventQuery, TaskPage, TaskQuery
from workpanda.domain.repository import (
    ConfirmationStore,
    PendingEvent,
    StaleTaskState,
    TaskNotFoundError,
    TaskStore,
)

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
        cancellations: StepCancellation | None = None,
    ) -> None:
        self.store = store
        self.confirmations = confirmations
        self.runner = runner
        self.flows = flows
        self.router = router
        self.confidence_threshold = confidence_threshold
        # 没有在跑的执行时可以不给：取消仍然只做状态翻转，与之前行为一致。
        self.cancellations = cancellations

    # ---------- 命令 ----------

    def create_task(self, request: TaskCreateRequest, subject: str) -> Task:
        flow = self._resolve_flow(request)
        task = self.store.create(
            Task(
                conversation_id=request.conversation_id,
                user_ref=subject,
                flow_id=flow.flow_id,
                flow_version=flow.version,
                slots={key: value for key, value in request.slots.items() if value},
            )
        )
        self.runner.emit(task, EventType.ACCEPTED, {"flow_id": flow.flow_id})
        return self.runner.advance(task)

    def answer(self, task_id: UUID, request: TaskAnswerRequest, subject: str) -> Task:
        task = self._active_task(task_id, subject)
        step = self._current_step(task)
        # 允许补充流程中任意收集节点的字段；当前节点决定提示什么，不代表只能改它。
        allowed = self._declared_fields(task)
        unknown = sorted(set(request.values) - allowed)
        if unknown:
            raise RequestRejected(f"fields are not accepted for this flow: {', '.join(unknown)}")

        task.slots.update({key: value for key, value in request.values.items() if value})
        task.pending_fields = []
        task.updated_at = utc_now()
        # 回答不是无条件覆盖：期间任务可能已被取消或转人工。状态与"字段已更新"
        # 事件同生共死，输掉的一方整批丢弃，调用方拿到冲突去刷新。
        self._commit(task, EventType.SLOTS_UPDATED, {"fields": sorted(request.values)})
        # 参数变化后旧凭证必须失效，执行节点会重新要求确认。
        self.confirmations.revoke(task.task_id, step.step_id)
        return self.runner.advance(task)

    def confirm(self, task_id: UUID, request: TaskConfirmRequest, subject: str) -> Task:
        task = self._active_task(task_id, subject)
        if task.status is not TaskStatus.WAITING_FOR_CONFIRMATION:
            raise TaskConflict("task is not waiting for confirmation")
        if task.current_step != request.step_id:
            raise TaskConflict("confirmation does not match the current step")

        step = self._current_step(task)
        if not request.accepted:
            task.pending_fields = []
            task.updated_at = utc_now()
            self._commit(task, EventType.CONFIRMATION_DECLINED, {"step_id": step.step_id})
            self.confirmations.revoke(task.task_id, step.step_id)
            return task

        # 凭证绑定任务、流程版本、步骤和当前参数摘要，执行前会再次校验。
        self.runner.issue_confirmation(task, step.step_id)
        self.runner.emit(task, EventType.CONFIRMATION_ACCEPTED, {"step_id": step.step_id})
        return self.runner.advance(task)

    def cancel(self, task_id: UUID, subject: str) -> Task:
        task = self._owned(task_id, subject)
        expected_status = task.status
        if expected_status in BLOCKED_STATUSES:
            raise TaskConflict(f"task in {expected_status} state cannot be cancelled")

        if self.cancellations is not None:
            # 状态翻转只是账面上的。正在跑的执行必须收到信号，否则它会把剩下的
            # 模型轮次和工具调用做完——包括那笔用户刚说"不要了"的提交。
            # 信号下发失败时**不翻转状态**：多进程部署下别的进程仍在跑，
            # 界面显示"已取消"会让人误以为可以安全地重新提交。
            try:
                self.cancellations.cancel_task(task.task_id, "user_cancelled")
            except Exception as exc:  # noqa: BLE001 - 底层异常类型不属于应用层协议
                raise CancelNotDelivered("取消信号未送达，请重试") from exc
        if task.current_step:
            self.confirmations.revoke(task.task_id, task.current_step)
        task.status = TaskStatus.CANCELLED
        task.updated_at = utc_now()
        # 与执行结果落地用同一套仲裁：只有库里的状态仍是我们读到的那一版，
        # 这次取消才生效。执行侧若先一步写出终态，说明它已经完成，取消必须
        # 失败让调用方刷新，而不是反过来把完成覆盖掉。CANCELLED 事件与状态
        # 同生共死，绝不出现"界面显示已取消、事件里却没有这条记录"的错位。
        try:
            self.runner.commit_guarded(
                task,
                [
                    PendingEvent(
                        type=EventType.CANCELLED,
                        status=TaskStatus.CANCELLED,
                        data={"step_id": task.current_step},
                    )
                ],
                expected_status=expected_status,
            )
        except StaleTaskState as exc:
            raise TaskConflict("任务状态已变化，本次取消未生效，请刷新后重试") from exc
        return task

    # ---------- 查询 ----------

    def get_task(self, task_id: UUID, subject: str) -> Task:
        return self._owned(task_id, subject)

    def events(self, task_id: UUID, subject: str, after_sequence: int = 0) -> Sequence[TaskEvent]:
        self._owned(task_id, subject)
        return self.store.events(task_id, after_sequence)

    # ---------- 运维只读（由运维控制台使用，需独立凭据） ----------

    def list_tasks(self, *, limit: int = 50, status: TaskStatus | None = None) -> Sequence[Task]:
        return self.store.list_tasks(limit=limit, status=status)

    def query_tasks(self, query: TaskQuery) -> TaskPage:
        """运维控制台的完整检索：过滤齐了才算"看全了"，否则截断是静默的。"""

        return self.store.query_tasks(query)

    def ops_task(self, task_id: UUID) -> Task:
        return self.store.get(task_id)

    def ops_events(self, task_id: UUID, after_sequence: int = 0) -> Sequence[TaskEvent]:
        self.store.get(task_id)
        return self.store.events(task_id, after_sequence)

    def query_events(self, query: EventQuery) -> EventPage:
        """事件检索。任务不存在时仍抛 `TaskNotFoundError`，不返回空页。

        "没有这个任务"和"这个任务没有事件"是两件事，混成一个空列表会让运维
        以为任务确实安静，而实际是 ID 敲错了。
        """

        self.store.get(query.task_id)
        return self.store.query_events(query)

    # ---------- 内部 ----------

    def _commit(self, task: Task, event_type: EventType, data: dict[str, Any]) -> None:
        """把「当前状态 + 一条过程事件」绑成一笔条件提交。

        回答与拒绝确认都不改变任务状态（前者只补字段，后者只清掉待确认位），
        所以期望值就是入口读到的这个状态。条件不成立说明期间有别人改过任务，
        整批丢弃并报冲突——不拿旧快照覆盖，也不留下一条无主事件。
        """
        expected_status = task.status
        try:
            self.runner.commit_guarded(
                task,
                [PendingEvent(type=event_type, status=task.status, data=data)],
                expected_status=expected_status,
            )
        except StaleTaskState as exc:
            raise TaskConflict("任务状态已变化，本次操作未生效，请刷新后重试") from exc

    def _owned(self, task_id: UUID, subject: str) -> Task:
        """任务归属校验：不是自己的任务一律按不存在处理，避免泄露任务存在性。"""

        task = self.store.get(task_id)
        if task.user_ref != subject:
            raise TaskNotFoundError(str(task_id))
        return task

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

    def _active_task(self, task_id: UUID, subject: str) -> Task:
        task = self._owned(task_id, subject)
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
