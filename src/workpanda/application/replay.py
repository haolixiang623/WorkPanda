"""回放执行：在隔离环境里把一个流程版本跑一遍，产出可断言的观测。

三条边界：

1. **不碰真实系统**：回放用 `ReplayExecutor`，它**不持有工具注册表**，因此从结构上
   就调不到任何真实工具——不是"记得别调"，而是"没有可调的对象"。带副作用的工具
   在回放里一律按 `unconfirmed` 记账并标注 `replay`：外部系统可能已被调用这件事在
   回放里没有发生，但结论必须与真实执行同形，否则断言的终态就没有意义。
2. **不写生产库**：每次回放用全新的进程内任务库与确认库，运行结束即丢弃。回放
   因此不会在真实任务流里留下任何幽灵任务。
3. **结论不折叠**：超时、崩溃、用例缺字段各有独立结论。任何"没有证据"的情形都
   不会变成 `pass`。
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from workpanda.application.errors import RequestRejected
from workpanda.application.ports import (
    ExecutionOutcome,
    ExecutionRequest,
    ExecutionResult,
    IntentMatch,
)
from workpanda.application.runner import FlowRunner
from workpanda.application.task_service import TaskService
from workpanda.domain.flow import FlowDefinition, FlowRegistry, FlowStatus
from workpanda.domain.models import (
    EventType,
    Task,
    TaskConfirmRequest,
    TaskCreateRequest,
    TaskEvent,
    TaskStatus,
)
from workpanda.domain.replay import (
    ReplayCase,
    ReplayCaseResult,
    ReplayFinding,
    ReplayFindingCode,
    ReplayReport,
    ReplayStepResult,
    ReplayToolCall,
    ReplayVerdict,
    StepOutcome,
    declared_tools,
    digest_arguments,
    evaluate,
    validate_case,
)
from workpanda.infrastructure.event_broker import EventBroker
from workpanda.infrastructure.memory_store import (
    InMemoryConfirmationStore,
    InMemoryTaskStore,
)

# 回放任务不隶属于任何真实群众。用一个固定前缀，便于在日志里一眼认出。
REPLAY_SUBJECT = "replay:preflight"


class ReplayAborted(RuntimeError):
    """回放被要求停止（运维中断或超时后的收尾）。不是流程缺陷。"""


def _all_side_effecting(_: str) -> bool:
    """默认把所有工具当作有副作用。fail-closed：不声明就按危险处理。"""

    return True


class ReplayExecutor:
    """回放专用执行器：只记录"流程要求对这个工具做什么"，不真的去做。

    刻意不接收 `ToolRegistry`：只要手上没有工具对象，就不存在"某条分支漏判了副作用
    于是真的发出请求"的可能。工具名取自流程定义的执行步骤，参数取自任务槽位。
    """

    def __init__(
        self,
        *,
        side_effecting: Callable[[str], bool] = _all_side_effecting,
        abort: threading.Event | None = None,
    ) -> None:
        self._side_effecting = side_effecting
        self._abort = abort
        self.calls: list[ReplayToolCall] = []

    def execute(self, request: ExecutionRequest) -> ExecutionResult:
        if self._abort is not None and self._abort.is_set():
            raise ReplayAborted("回放已被中断")

        side_effecting = self._side_effecting(request.tool)
        self.calls.append(
            ReplayToolCall(
                step_id=request.step_id,
                name=request.tool,
                fields=tuple(sorted(request.arguments)),
                argument_digest=digest_arguments(request.arguments),
                replay=True,
                side_effecting=side_effecting,
            )
        )
        output = {
            "replay": True,
            "step_id": request.step_id,
            "tool": request.tool,
            "simulated": True,
        }
        if side_effecting:
            return ExecutionResult(
                outcome=ExecutionOutcome.UNCONFIRMED,
                output=output,
                error_code="replay_side_effect",
                message="回放中带副作用的工具按未确认记账：本次没有真实调用，须人工核实。",
            )
        return ExecutionResult(outcome=ExecutionOutcome.SUCCEEDED, output=output)


class _SilentRouter:
    """回放不走意图识别：用例已指定 `(flow_id, version)`，猜流程只会引入噪声。"""

    def route(self, text: str) -> IntentMatch | None:  # pragma: no cover - 不会被调用
        return None


@dataclass(frozen=True, slots=True)
class _Observation:
    terminal: TaskStatus | None
    pending_fields: tuple[str, ...]
    steps: tuple[ReplayStepResult, ...]
    tools: tuple[ReplayToolCall, ...]


class FlowReplay:
    """按 `(flow_id, version)` 回放用例。

    `side_effecting` 由调用方给出：能拿到工具规格时按 `idempotent/risk` 判定，拿不到
    就保持默认（全部按有副作用）。`authorized_tools` 为 `None` 时只校验"步骤不许调用
    自己没声明的工具"这一层；给出时才额外校验"流程声明了部署未授权的工具"。
    """

    def __init__(
        self,
        *,
        timeout_seconds: float = 10.0,
        side_effecting: Callable[[str], bool] = _all_side_effecting,
        authorized_tools: frozenset[str] | None = None,
        max_confirmations: int = 16,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds 必须大于 0")
        self._timeout_seconds = timeout_seconds
        self._side_effecting = side_effecting
        self._authorized_tools = authorized_tools
        self._max_confirmations = max(1, max_confirmations)

    # ---------- 对外 ----------

    def run_cases(self, flow: FlowDefinition, cases: Sequence[ReplayCase]) -> ReplayReport:
        """回放一个版本的全部用例。单条用例失败不影响其余用例出结论。"""

        results = tuple(self.run_case(flow, case) for case in cases)
        return ReplayReport(flow_id=flow.flow_id, flow_version=flow.version, results=results)

    def run_case(self, flow: FlowDefinition, case: ReplayCase) -> ReplayCaseResult:
        started = time.monotonic()

        invalid = validate_case(case, flow)
        if invalid:
            return ReplayCaseResult(
                case_id=case.case_id,
                verdict=ReplayVerdict.INVALID,
                findings=tuple(invalid),
                duration_ms=_elapsed_ms(started),
            )

        blocked = self._unauthorized_declarations(flow)
        if blocked:
            # 一步都不跑：流程引用了一个部署没授权的工具，这时跑下去只会拿模拟结果
            # 给出"看起来通过"的假象。
            return ReplayCaseResult(
                case_id=case.case_id,
                verdict=ReplayVerdict.FAIL,
                findings=blocked,
                duration_ms=_elapsed_ms(started),
            )

        abort = threading.Event()
        box: dict[str, object] = {}

        def worker() -> None:
            try:
                box["observation"] = self._run(flow, case, abort)
            except ReplayAborted as exc:
                box["aborted"] = str(exc)
            except RequestRejected as exc:
                box["rejected"] = str(exc)
            except BaseException as exc:  # noqa: BLE001 - 回放里任何异常都不能变成 pass
                box["crashed"] = f"{type(exc).__name__}: {exc}"

        thread = threading.Thread(target=worker, name=f"replay-{case.case_id}", daemon=True)
        thread.start()
        thread.join(self._timeout_seconds)

        if thread.is_alive():
            # 先通知它停，再判未确认：超时的回放没有结论，但也不该继续跑下去。
            abort.set()
            return ReplayCaseResult(
                case_id=case.case_id,
                verdict=ReplayVerdict.UNCONFIRMED,
                findings=(
                    ReplayFinding(
                        code=ReplayFindingCode.REPLAY_TIMEOUT,
                        detail=f"回放超过 {self._timeout_seconds:g}s 未结束，没有结论，不判通过。",
                    ),
                ),
                duration_ms=_elapsed_ms(started),
            )

        if "aborted" in box:
            return ReplayCaseResult(
                case_id=case.case_id,
                verdict=ReplayVerdict.ABORTED,
                findings=(
                    ReplayFinding(
                        code=ReplayFindingCode.REPLAY_ABORTED,
                        detail=f"回放被中断：{box['aborted']}；本次结果不进发布依据。",
                    ),
                ),
                duration_ms=_elapsed_ms(started),
            )

        if "rejected" in box:
            return ReplayCaseResult(
                case_id=case.case_id,
                verdict=ReplayVerdict.INVALID,
                findings=(
                    ReplayFinding(
                        code=ReplayFindingCode.CASE_UNKNOWN_FIELD,
                        detail=f"用例输入被流程拒绝：{box['rejected']}",
                    ),
                ),
                duration_ms=_elapsed_ms(started),
            )

        crashed = box.get("crashed")
        observation = box.get("observation")
        if crashed or not isinstance(observation, _Observation):
            # 崩溃与"跑完了但什么都没拿到"同属"没有结论"，绝不降级成通过。
            detail = str(crashed) if crashed else "回放没有产出任何观测"
            return ReplayCaseResult(
                case_id=case.case_id,
                verdict=ReplayVerdict.UNCONFIRMED,
                findings=(
                    ReplayFinding(
                        code=ReplayFindingCode.REPLAY_CRASHED,
                        detail=f"回放异常结束：{detail}；没有结论，不判通过。",
                    ),
                ),
                duration_ms=_elapsed_ms(started),
            )

        if observation.terminal is TaskStatus.WAITING_FOR_USER:
            # 缺必填字段：这是用例写得不对，不是流程回归。
            return ReplayCaseResult(
                case_id=case.case_id,
                verdict=ReplayVerdict.INVALID,
                findings=(
                    ReplayFinding(
                        code=ReplayFindingCode.CASE_MISSING_FIELDS,
                        detail=(
                            "用例缺少必填字段，回放停在收集阶段："
                            f"{', '.join(observation.pending_fields) or '（未给出字段名）'}"
                        ),
                    ),
                ),
                steps=observation.steps,
                tools=observation.tools,
                terminal=observation.terminal.value,
                duration_ms=_elapsed_ms(started),
            )

        findings = evaluate(
            flow,
            case,
            steps=observation.steps,
            tools=observation.tools,
            terminal=observation.terminal,
        )
        return ReplayCaseResult(
            case_id=case.case_id,
            verdict=ReplayVerdict.FAIL if findings else ReplayVerdict.PASS,
            findings=tuple(findings),
            steps=observation.steps,
            tools=observation.tools,
            terminal=observation.terminal.value if observation.terminal else None,
            duration_ms=_elapsed_ms(started),
        )

    # ---------- 内部 ----------

    def _unauthorized_declarations(self, flow: FlowDefinition) -> list[ReplayFinding]:
        if self._authorized_tools is None:
            return []
        findings: list[ReplayFinding] = []
        for tool in declared_tools(flow):
            if tool not in self._authorized_tools:
                findings.append(
                    ReplayFinding(
                        code=ReplayFindingCode.UNAUTHORIZED_TOOL,
                        detail=(
                            f"流程声明了部署未授权的工具 {tool}；"
                            "回放不会替它模拟一个成功结果，请先补工具授权或改流程。"
                        ),
                    )
                )
        return findings

    def _run(
        self, flow: FlowDefinition, case: ReplayCase, abort: threading.Event
    ) -> _Observation:
        """在一次性环境里跑完一个用例。所有状态都随返回值一起丢弃。"""

        # 只注册这一个版本：回放要能跑**未发布的草稿**，但也不该顺带看到别的版本。
        #
        # 这里把状态改成 `published` 再注册，是唯一一处"故意绕过发布检查"的地方：
        # 回放的对象正是"还没发布、准备发布"的那个版本，`FlowRegistry.resolve` 的
        # 发布门禁拦的应该是群众的新请求，不是发布前的自检。发布与否由门禁
        # （`application/release.py`）在回放通过之后才决定，两件事不能互相顶替。
        runnable = flow.model_copy(update={"status": FlowStatus.PUBLISHED})
        registry = FlowRegistry([runnable])
        store = InMemoryTaskStore()
        confirmations = InMemoryConfirmationStore()
        executor = ReplayExecutor(side_effecting=self._side_effecting, abort=abort)
        runner = FlowRunner(
            store=store,
            confirmations=confirmations,
            executor=executor,
            publisher=EventBroker(),
            flows=registry,
        )
        service = TaskService(
            store=store,
            confirmations=confirmations,
            runner=runner,
            flows=registry,
            router=_SilentRouter(),
            confidence_threshold=1.0,
        )

        task = service.create_task(
            TaskCreateRequest(
                conversation_id=f"replay-{case.case_id}",
                input_text=case.note or f"replay {flow.flow_id}@{flow.version}",
                flow_id=flow.flow_id,
                flow_version=flow.version,
                slots=dict(case.slots),
            ),
            subject=REPLAY_SUBJECT,
        )
        task = self._accept_confirmations(service, task, abort)
        events = list(store.events(task.task_id))
        return _observe(flow, store.get(task.task_id), events, executor.calls)

    def _accept_confirmations(
        self, service: TaskService, task: Task, abort: threading.Event
    ) -> Task:
        """自动接受确认。

        回放模拟的是"群众点了确认"这条路径：确认凭证本身由 `domain/confirmation.py`
        的单测覆盖，这里再走一遍只会让回放卡在等待态，永远出不了结论。
        """

        rounds = 0
        while task.status is TaskStatus.WAITING_FOR_CONFIRMATION:
            if abort.is_set():
                raise ReplayAborted("回放已被中断")
            if rounds >= self._max_confirmations:
                raise ReplayAborted("确认轮次超出上限，疑似流程不收敛")
            if not task.current_step:
                raise ReplayAborted("任务处于待确认状态但没有当前步骤")
            task = service.confirm(
                task.task_id,
                TaskConfirmRequest(step_id=task.current_step, accepted=True),
                subject=REPLAY_SUBJECT,
            )
            rounds += 1
        return task


def _observe(
    flow: FlowDefinition,
    task: Task,
    events: Sequence[TaskEvent],
    calls: Sequence[ReplayToolCall],
) -> _Observation:
    """从事件流还原"走了哪些步骤、走到哪一步"。观测只读事件与执行请求。

    每一步取**最后一次**相关事件：确认之后会重新发出 `STEP_STARTED`，若按"出现过
    暂停事件"就判暂停，一个已经跑完（或被判未确认）的步骤会永远显示成"卡在确认"。
    """

    order: list[str] = []
    outcomes: dict[str, StepOutcome] = {}
    for event in events:
        step_id = event.data.get("step_id")
        if not isinstance(step_id, str) or not step_id:
            continue
        if event.type is EventType.STEP_STARTED:
            if step_id not in order:
                order.append(step_id)
            # 重新开始意味着上一次的暂停已经过去，回到"还没走完"。
            outcomes[step_id] = StepOutcome.INCOMPLETE
        elif event.type is EventType.STEP_COMPLETED:
            outcomes[step_id] = StepOutcome.COMPLETED
        elif event.type in {
            EventType.CLARIFICATION_REQUIRED,
            EventType.CONFIRMATION_REQUIRED,
        }:
            # 暂停只对"本轮还没走完"的步骤成立；确认之后重新 `STEP_STARTED` 会把它
            # 覆盖回 `incomplete`，因此这里直接赋值即可。
            if outcomes.get(step_id) is not StepOutcome.COMPLETED:
                outcomes[step_id] = StepOutcome.PAUSED

    steps: list[ReplayStepResult] = []
    for step_id in order:
        step = flow.step(step_id)
        steps.append(
            ReplayStepResult(
                step_id=step_id,
                kind=step.kind.value if step is not None else "unknown",
                outcome=outcomes.get(step_id, StepOutcome.INCOMPLETE),
            )
        )

    return _Observation(
        terminal=task.status,
        pending_fields=tuple(task.pending_fields),
        steps=tuple(steps),
        tools=tuple(calls),
    )


def _elapsed_ms(started: float) -> int:
    return max(0, int((time.monotonic() - started) * 1000))


__all__ = [
    "REPLAY_SUBJECT",
    "FlowReplay",
    "ReplayAborted",
    "ReplayExecutor",
]
