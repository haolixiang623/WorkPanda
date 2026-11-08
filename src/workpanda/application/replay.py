"""流程回放：用固定的工具脚本把一条流程版本完整走一遍。

用途只有一个：**发布前**回答"这个版本还能不能按设计走完"。它既不是测试框架，也不是
演练真实系统的手段——回放全程不触达任何外部系统。

三条设计约束：

1. **不碰真实工具。** `ReplayExecutor` 是一个独立的 `Executor` 实现，手里只有用例
   给的脚本，没有工具注册表、没有 HTTP、没有模型客户端。它不会"顺便"调一个真实工具。
2. **走真实的推进链路。** 回放复用 `TaskService` / `FlowRunner` / 状态机 / 确认凭证，
   不另写一条"简化执行"。否则测的是另一个系统，通过与否没有意义。
3. **不知道就算未确认。** 用例没给某个工具的脚本结果时，该次调用返回 `unconfirmed`
   并把用例判为"用例不完整"，而不是假装成功——"编造一次成功再断言它成功"是这类
   工具最容易长出来的假阳性。

待测版本通常是**草稿**。回放会把它临时当作已发布塞进一份局部注册表，绝不移动真实
发布指针：回放失败不该影响线上，回放成功也不代表已经发布。

本模块只依赖应用层端口，仓库与事件总线由调用方注入（见 `bootstrap.build_replayer`），
以免应用层反向依赖基础设施。
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, Field

from workpanda.application.ports import (
    EventPublisher,
    ExecutionOutcome,
    ExecutionRequest,
    ExecutionResult,
    IntentMatch,
)
from workpanda.application.runner import FlowRunner
from workpanda.application.task_service import BLOCKED_STATUSES, TaskService
from workpanda.domain.flow import FlowDefinition, FlowRegistry, FlowStatus, StepKind
from workpanda.domain.models import (
    Task,
    TaskAnswerRequest,
    TaskConfirmRequest,
    TaskCreateRequest,
    TaskStatus,
)
from workpanda.domain.repository import ConfirmationStore, TaskStore

# 回放产生的任务不属于任何真实会话，用固定主体标记，避免与真实数据混淆。
REPLAY_SUBJECT = "replay"
# 单次回放的推进轮次上限。正常流程远小于它；超了说明用例或流程有环。
MAX_ROUNDS = 200


class ReplayToolResult(BaseModel):
    """用例为某个工具准备的返回值。同一工具被多次调用时复用同一条。

    刻意不支持"按第几次调用给不同结果"：那会让用例依赖调用顺序，回放就从
    "验证流程结构"变成"验证某次实现的调用时序"，一改实现就红，价值为负。
    """

    tool: str = Field(min_length=1, max_length=128)
    outcome: ExecutionOutcome = ExecutionOutcome.SUCCEEDED
    output: dict[str, Any] = Field(default_factory=dict)
    error_code: str | None = Field(default=None, max_length=128)
    message: str | None = Field(default=None, max_length=1000)


class ReplayCase(BaseModel):
    """一条回放用例：输入 + 工具脚本 + 期望。全部是数据，不含代码。"""

    case_id: str = Field(min_length=1, max_length=128)
    flow_id: str = Field(min_length=1, max_length=128)
    flow_version: str = Field(min_length=1, max_length=64)
    # 创建任务时就带上的字段。缺必填字段时流程会停在收集节点，判定为"用例不完整"。
    inputs: dict[str, str] = Field(default_factory=dict)
    # 多轮收集：按顺序喂给 `answer`。喂完仍缺字段 → 用例不完整。
    answers: tuple[dict[str, str], ...] = ()
    tool_results: tuple[ReplayToolResult, ...] = ()

    # 期望值一律可选：不写就不检查。写了就严格比对，不做"大致相等"。
    expect_steps: tuple[str, ...] | None = None
    expect_tools: tuple[str, ...] | None = None
    expect_outcome: ExecutionOutcome | None = None
    expect_status: TaskStatus | None = None
    # 需要群众确认几次。这条不只是计数：它证明流程里的确认节点还在。
    expect_confirmations: int | None = None


class ReplayCheck(BaseModel):
    name: str
    passed: bool
    detail: str


class ReplayReport(BaseModel):
    case_id: str
    flow_id: str
    flow_version: str
    observed_steps: list[str] = Field(default_factory=list)
    observed_tools: list[str] = Field(default_factory=list)
    observed_outcome: ExecutionOutcome | None = None
    final_status: TaskStatus
    confirmations_accepted: int = 0
    unscripted_tools: list[str] = Field(default_factory=list)
    checks: list[ReplayCheck] = Field(default_factory=list)
    # 非空表示"这次回放没有测到东西"：不是流程失败，也不是通过。
    incomplete_reason: str | None = None

    @property
    def passed(self) -> bool:
        return self.incomplete_reason is None and all(check.passed for check in self.checks)


class ReplayExecutor:
    """只按脚本回答的执行器。没有工具注册表，因此不可能触及真实系统。"""

    def __init__(self, scripted: Sequence[ReplayToolResult] = ()) -> None:
        self._scripted = {item.tool: item for item in scripted}
        self.calls: list[ExecutionRequest] = []
        self.outcomes: list[ExecutionOutcome] = []
        self.unscripted: list[str] = []

    def execute(self, request: ExecutionRequest) -> ExecutionResult:
        self.calls.append(request)
        scripted = self._scripted.get(request.tool)
        if scripted is None:
            # 没有脚本就不猜：未知 ≠ 成功。调用方据此把用例判为"不完整"。
            if request.tool not in self.unscripted:
                self.unscripted.append(request.tool)
            self.outcomes.append(ExecutionOutcome.UNCONFIRMED)
            return ExecutionResult(
                outcome=ExecutionOutcome.UNCONFIRMED,
                error_code="replay_unscripted_tool",
                message=f"用例未提供工具 {request.tool!r} 的回放结果",
            )
        self.outcomes.append(scripted.outcome)
        return ExecutionResult(
            outcome=scripted.outcome,
            output=dict(scripted.output),
            error_code=scripted.error_code,
            message=scripted.message,
        )


class _NoRouter:
    """回放按 flow_id 直接建任务，不走意图识别。"""

    def route(self, text: str) -> IntentMatch | None:  # noqa: ARG002 - 端口签名
        return None


class FlowReplayer:
    """把一条流程版本按用例走一遍，返回可判定的报告。

    仓储、确认凭证库与事件总线由调用方注入：回放要复用真实推进链路，但**不能**
    落在生产库上。每次 `replay` 都应传一套全新的内存实现。
    """

    def __init__(self, *, confirmation_ttl_seconds: int = 300) -> None:
        self.confirmation_ttl_seconds = confirmation_ttl_seconds

    def replay(
        self,
        flow: FlowDefinition,
        case: ReplayCase,
        *,
        store: TaskStore,
        confirmations: ConfirmationStore,
        publisher: EventPublisher,
    ) -> ReplayReport:
        if (case.flow_id, case.flow_version) != (flow.flow_id, flow.version):
            raise ValueError(
                f"用例指向 {case.flow_id}@{case.flow_version}，"
                f"传入的流程是 {flow.flow_id}@{flow.version}"
            )

        executor = ReplayExecutor(case.tool_results)
        # 局部注册表里把待测版本当作已发布：回放不移动真实发布指针。
        registry = FlowRegistry([flow.model_copy(update={"status": FlowStatus.PUBLISHED})])
        runner = FlowRunner(
            store=store,
            confirmations=confirmations,
            executor=executor,
            publisher=publisher,
            flows=registry,
            confirmation_ttl_seconds=self.confirmation_ttl_seconds,
        )
        service = TaskService(
            store=store,
            confirmations=confirmations,
            runner=runner,
            flows=registry,
            router=_NoRouter(),
        )

        task, confirmations_accepted, incomplete = _drive(service, flow, case)
        report = build_report(flow, case, task, executor, confirmations_accepted)
        if incomplete is not None:
            report.incomplete_reason = incomplete
        return report


def _drive(
    service: TaskService,
    flow: FlowDefinition,
    case: ReplayCase,
) -> tuple[Task, int, str | None]:
    """按真实接口把任务推到终态。停在"要人说话"的地方就算用例不完整。"""

    task = service.create_task(
        TaskCreateRequest(
            conversation_id=f"replay-{uuid4().hex}",
            input_text=case.case_id,
            flow_id=flow.flow_id,
            flow_version=flow.version,
            slots=dict(case.inputs),
        ),
        REPLAY_SUBJECT,
    )

    pending_answers = list(case.answers)
    confirmations_accepted = 0

    for _ in range(MAX_ROUNDS):
        # `OUTCOME_UNCONFIRMED` 不在 `is_terminal` 里（它还能被人工处置），但对回放
        # 而言已经停住了：继续推只会得到"未预期状态"。用同一份 blocked 集合判断。
        if task.is_terminal or task.status in BLOCKED_STATUSES:
            return task, confirmations_accepted, None

        if task.status is TaskStatus.WAITING_FOR_CONFIRMATION:
            step_id = task.current_step
            if step_id is None:
                return task, confirmations_accepted, "流程停在确认节点但没有当前步骤"
            try:
                task = service.confirm(
                    task.task_id,
                    TaskConfirmRequest(step_id=step_id, accepted=True),
                    REPLAY_SUBJECT,
                )
            except Exception as exc:  # noqa: BLE001 - 用例或流程本身的问题，如实返回
                return task, confirmations_accepted, f"确认节点推进失败：{exc}"
            confirmations_accepted += 1
            continue

        if task.status is TaskStatus.WAITING_FOR_USER:
            if not pending_answers:
                missing = "、".join(task.pending_fields) or "未声明字段"
                return task, confirmations_accepted, f"用例未提供必要字段：{missing}"
            values = pending_answers.pop(0)
            try:
                task = service.answer(
                    task.task_id,
                    TaskAnswerRequest(values=values),
                    REPLAY_SUBJECT,
                )
            except Exception as exc:  # noqa: BLE001 - 同上
                return task, confirmations_accepted, f"补充字段失败：{exc}"
            continue

        # 其它状态（运行中但没有可推进的动作）不应出现；如实返回而不是死循环。
        return task, confirmations_accepted, f"回放停在未预期的状态：{task.status}"

    return task, confirmations_accepted, f"超过 {MAX_ROUNDS} 轮仍未结束，疑似流程成环"


def build_report(
    flow: FlowDefinition,
    case: ReplayCase,
    task: Task,
    executor: ReplayExecutor,
    confirmations_accepted: int,
) -> ReplayReport:
    """把观测到的结果与期望比对成一份报告。纯函数，回放与测试共用。"""

    observed_tools = [request.tool for request in executor.calls]
    observed_outcome = executor.outcomes[-1] if executor.outcomes else None

    report = ReplayReport(
        case_id=case.case_id,
        flow_id=flow.flow_id,
        flow_version=flow.version,
        observed_steps=list(task.completed_steps),
        observed_tools=observed_tools,
        observed_outcome=observed_outcome,
        final_status=task.status,
        confirmations_accepted=confirmations_accepted,
        unscripted_tools=list(executor.unscripted),
    )

    # 未授权工具：不依赖用例是否写了期望，每次都查。流程声明的工具之外一律算越权。
    declared = {step.tool for step in flow.steps if step.kind is StepKind.EXECUTE and step.tool}
    unauthorized = sorted(set(observed_tools) - declared)
    report.checks.append(
        ReplayCheck(
            name="tools_declared",
            passed=not unauthorized,
            detail=(
                "所有调用都来自流程声明的工具"
                if not unauthorized
                else f"出现未声明工具：{', '.join(unauthorized)}"
            ),
        )
    )

    report.checks.append(
        ReplayCheck(
            name="tools_scripted",
            passed=not executor.unscripted,
            detail=(
                "每个被调用的工具都有用例脚本"
                if not executor.unscripted
                else f"缺脚本的工具：{', '.join(executor.unscripted)}"
            ),
        )
    )

    if case.expect_steps is not None:
        report.checks.append(_compare("steps", list(case.expect_steps), report.observed_steps))
    if case.expect_tools is not None:
        report.checks.append(_compare("tools", list(case.expect_tools), observed_tools))
    if case.expect_outcome is not None:
        report.checks.append(
            _compare("outcome", case.expect_outcome.value, _value(observed_outcome))
        )
    if case.expect_status is not None:
        report.checks.append(_compare("status", case.expect_status.value, task.status.value))
    if case.expect_confirmations is not None:
        report.checks.append(
            _compare("confirmations", str(case.expect_confirmations), str(confirmations_accepted))
        )

    if executor.unscripted:
        report.incomplete_reason = f"用例未覆盖工具：{', '.join(executor.unscripted)}"

    return report


def _value(outcome: ExecutionOutcome | None) -> str | None:
    return outcome.value if outcome is not None else None


def _compare(name: str, expected: Any, actual: Any) -> ReplayCheck:
    if expected == actual:
        return ReplayCheck(name=name, passed=True, detail=f"符合预期：{expected}")
    return ReplayCheck(name=name, passed=False, detail=f"期望 {expected}，实际 {actual}")
