"""识别链路的负向测试：不确定时，一步都不许往前走。

已有的 `test_flow_runner.py` 覆盖了"字段缺失 / 未确认 / 失败"三条路径，但都从
**已经确定流程**之后开始。这里补的是更前面的那一段：识别不出来、识别成两件事、
识别出流程没声明的字段时，系统必须停在原地——不建任务、不调执行器、不产生事件。
"""

from __future__ import annotations

import pytest

from tests.support import CONFIRM_FLOW, DIRECT_FLOW, TEST_FLOWS
from workpanda.application.errors import IntentUnresolved, RequestRejected
from workpanda.application.ports import (
    ExecutionOutcome,
    ExecutionRequest,
    ExecutionResult,
    IntentMatch,
)
from workpanda.bootstrap import Container, build_container
from workpanda.config import Settings
from workpanda.domain.intent import IntentRule, RuleIntentRouter
from workpanda.domain.models import (
    EventType,
    TaskConfirmRequest,
    TaskCreateRequest,
    TaskStatus,
)

SUBJECT = "session-a"

PHONE = r"((?:1[3-9]\d{9})|(?:\d{3}-\d{8}))"

RULE_SUBMIT = IntentRule(
    intent_id="test.confirm.submit",
    flow_id=CONFIRM_FLOW.flow_id,
    flow_version=CONFIRM_FLOW.version,
    keywords=("办理", "提交"),
    slot_patterns={
        "subject": (r"主题[:：]\s*([^\s,，。;；]+)",),
        "contact": (PHONE,),
    },
)

RULE_QUERY = IntentRule(
    intent_id="test.direct.query",
    flow_id=DIRECT_FLOW.flow_id,
    flow_version=DIRECT_FLOW.version,
    keywords=("查询", "进度"),
)


def build(router=None, *, executor=None) -> Container:
    return build_container(
        Settings(store_backend="memory", enable_demo_flow=False),
        flows=TEST_FLOWS,
        executor=executor or RecordingExecutor(),
        router=router if router is not None else RuleIntentRouter(rules=(RULE_SUBMIT, RULE_QUERY)),
    )


def create(container: Container, text: str, **overrides):
    return container.tasks.create_task(
        TaskCreateRequest(conversation_id="conversation-1", input_text=text, **overrides),
        SUBJECT,
    )


def test_unresolved_intent_creates_no_task() -> None:
    executor = RecordingExecutor()
    container = build(executor=executor)
    try:
        with pytest.raises(IntentUnresolved):
            create(container, "今天天气怎么样")

        assert list(container.tasks.list_tasks()) == []
        assert executor.calls == []
    finally:
        container.close()


def test_low_confidence_never_reaches_execution() -> None:
    """单个弱关键词（0.4）不足以开工：宁可问一句，也不能把话塞进别的事项。"""

    weak = IntentRule(
        intent_id="test.confirm.weak",
        flow_id=CONFIRM_FLOW.flow_id,
        flow_version=CONFIRM_FLOW.version,
        keywords=("办理",),
        base_confidence=0.4,
        slot_patterns={"subject": (r"主题[:：]\s*([^\s,，。;；]+)",)},
    )
    container = build(router=RuleIntentRouter(rules=(weak,)))
    try:
        decision = container.router.decide("我要办理，主题：更换联系方式")
        assert decision.match is not None
        assert decision.match.confidence < Settings().intent_confidence_threshold

        with pytest.raises(IntentUnresolved):
            create(container, "我要办理，主题：更换联系方式")
        assert list(container.tasks.list_tasks()) == []
    finally:
        container.close()


def test_ambiguous_intent_requires_clarification_without_side_effects() -> None:
    """两个流程打平：不能任选一个开工，也不能悄悄按"更靠前"的那条走。"""

    first = IntentRule(
        intent_id="test.confirm.topic",
        flow_id=CONFIRM_FLOW.flow_id,
        flow_version=CONFIRM_FLOW.version,
        keywords=("事项", "办理"),
    )
    second = IntentRule(
        intent_id="test.direct.topic",
        flow_id=DIRECT_FLOW.flow_id,
        flow_version=DIRECT_FLOW.version,
        keywords=("事项", "查询"),
    )
    container = build(router=RuleIntentRouter(rules=(first, second)))
    try:
        assert container.router.route("我要办个事项") is None
        with pytest.raises(IntentUnresolved):
            create(container, "我要办个事项")

        assert list(container.tasks.list_tasks()) == []
    finally:
        container.close()


def test_routed_slots_skip_manual_collection() -> None:
    """识别出两个字段后应当直接进入确认，而不是再问一遍。"""

    executor = RecordingExecutor()
    container = build(executor=executor)
    try:
        task = create(container, "我要办理提交，主题：更换联系方式，联系 13800001111")

        assert task.status is TaskStatus.WAITING_FOR_CONFIRMATION
        assert task.slots == {"subject": "更换联系方式", "contact": "13800001111"}
        assert executor.calls == []

        accepted = [
            event
            for event in container.tasks.events(task.task_id, SUBJECT)
            if event.type is EventType.ACCEPTED
        ]
        assert accepted[0].data["slots_from_text"] == ["contact", "subject"]
    finally:
        container.close()


def test_partial_routing_asks_only_for_the_missing_field() -> None:
    executor = RecordingExecutor()
    container = build(executor=executor)
    try:
        task = create(container, "我要办理提交，主题：更换联系方式")

        assert task.status is TaskStatus.WAITING_FOR_USER
        assert task.pending_fields == ["contact"]
        assert "contact" not in task.slots
        assert executor.calls == []
    finally:
        container.close()


class StubRouter:
    """直接返回给定匹配结果，用来模拟"路由器越权产出字段"这一情形。"""

    def __init__(self, match: IntentMatch) -> None:
        self.match = match

    def route(self, text: str) -> IntentMatch | None:
        return self.match


def test_router_cannot_inject_undeclared_fields() -> None:
    """识别侧多给的字段必须在应用层被丢掉，否则会顺着参数流进执行器。"""

    stub = StubRouter(
        IntentMatch(
            intent_id="test.confirm.submit",
            flow_id=CONFIRM_FLOW.flow_id,
            flow_version=CONFIRM_FLOW.version,
            confidence=0.9,
            slots={"subject": "更换联系方式", "contact": "13800001111", "role": "admin"},
        )
    )
    executor = RecordingExecutor()
    container = build(router=stub, executor=executor)
    try:
        task = create(container, "我要办理提交")
        assert task.status is TaskStatus.WAITING_FOR_CONFIRMATION
        assert task.slots == {"subject": "更换联系方式", "contact": "13800001111"}

        container.tasks.confirm(task.task_id, TaskConfirmRequest(step_id="submit"), SUBJECT)
        assert executor.calls[0].arguments == {
            "subject": "更换联系方式",
            "contact": "13800001111",
        }
    finally:
        container.close()


def test_client_supplied_unknown_field_is_rejected_at_creation() -> None:
    container = build()
    try:
        with pytest.raises(RequestRejected):
            create(container, "我要办理提交", slots={"subject": "x", "role": "admin"})

        assert list(container.tasks.list_tasks()) == []
    finally:
        container.close()


def test_explicit_flow_id_does_not_run_extraction() -> None:
    """显式指定流程属于内部/调试用法：不跑识别，也就不会回填字段。"""

    container = build()
    try:
        task = create(
            container,
            "我要办理提交，主题：更换联系方式，联系 13800001111",
            flow_id=CONFIRM_FLOW.flow_id,
        )

        assert task.slots == {}
        assert task.pending_fields == ["subject", "contact"]
    finally:
        container.close()


def test_match_pointing_to_unregistered_flow_is_rejected() -> None:
    stub = StubRouter(
        IntentMatch(
            intent_id="test.ghost",
            flow_id="test.ghost",
            flow_version="1",
            confidence=0.99,
        )
    )
    container = build(router=stub)
    try:
        with pytest.raises(RequestRejected):
            create(container, "随便说说")
        assert list(container.tasks.list_tasks()) == []
    finally:
        container.close()


def test_client_supplied_slot_wins_over_extraction() -> None:
    """群众自己填的值优先于从话里抽的值：后者只是猜测。"""

    container = build()
    try:
        task = create(
            container,
            "我要办理提交，主题：更换联系方式，联系 13800001111",
            slots={"subject": "本人填写"},
        )

        assert task.slots["subject"] == "本人填写"
        assert task.slots["contact"] == "13800001111"
    finally:
        container.close()


class RecordingExecutor:
    """记录调用并按顺序返回结果；用于端到端七段路径。"""

    def __init__(self, results: list[ExecutionResult] | None = None) -> None:
        self.calls: list[ExecutionRequest] = []
        self._results = list(results or [])

    def execute(self, request: ExecutionRequest) -> ExecutionResult:
        self.calls.append(request)
        if self._results:
            return self._results.pop(0)
        return ExecutionResult(
            outcome=ExecutionOutcome.SUCCEEDED,
            output={"tool": request.tool, "step_id": request.step_id},
        )


def test_end_to_end_happy_path_reaches_completed_with_result() -> None:
    """输入 → 识别 → （无需澄清）→ 确认 → 结果。"""

    executor = RecordingExecutor()
    container = build(executor=executor)  # type: ignore[arg-type]
    try:
        task = create(container, "我要办理提交，主题：更换联系方式，联系 13800001111")
        assert task.status is TaskStatus.WAITING_FOR_CONFIRMATION
        assert executor.calls == []

        task = container.tasks.confirm(task.task_id, TaskConfirmRequest(step_id="submit"), SUBJECT)
        assert task.status is TaskStatus.COMPLETED
        assert task.result is not None
        assert len(executor.calls) == 1

        types = [event.type for event in container.tasks.events(task.task_id, SUBJECT)]
        assert types[-1] is EventType.COMPLETED
        assert types.count(EventType.COMPLETED) == 1
    finally:
        container.close()


@pytest.mark.parametrize(
    "outcome,expected_status,forbidden_events",
    [
        (ExecutionOutcome.UNCONFIRMED, TaskStatus.OUTCOME_UNCONFIRMED, (EventType.COMPLETED,)),
        (ExecutionOutcome.FAILED, TaskStatus.HANDOFF_REQUIRED, (EventType.COMPLETED,)),
    ],
    ids=["结果未知", "执行失败"],
)
def test_failure_paths_never_report_completion(
    outcome: ExecutionOutcome,
    expected_status: TaskStatus,
    forbidden_events: tuple[EventType, ...],
) -> None:
    """失败与结果未知都必须"不办成"：没有 completed 事件，也没有成功结果。"""

    executor = RecordingExecutor(
        [ExecutionResult(outcome=outcome, error_code="test_failure", message="测试失败")]
    )
    container = build(executor=executor)  # type: ignore[arg-type]
    try:
        task = create(container, "我要办理提交，主题：更换联系方式，联系 13800001111")
        task = container.tasks.confirm(task.task_id, TaskConfirmRequest(step_id="submit"), SUBJECT)

        assert task.status is expected_status
        assert task.result is None
        assert len(executor.calls) == 1, "结果未知也不得自动重试"
        types = [event.type for event in container.tasks.events(task.task_id, SUBJECT)]
        for forbidden in forbidden_events:
            assert forbidden not in types
    finally:
        container.close()
