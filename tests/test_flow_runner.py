"""固定流程推进的关键行为：澄清、确认、幂等和失败安全。"""

import pytest

from workpanda.application.errors import RequestRejected, TaskConflict
from workpanda.application.ports import ExecutionOutcome, ExecutionResult
from workpanda.bootstrap import Container
from workpanda.domain.models import (
    EventType,
    TaskAnswerRequest,
    TaskConfirmRequest,
    TaskCreateRequest,
    TaskStatus,
)


def start(container: Container, **overrides) -> object:
    request = TaskCreateRequest(
        conversation_id="conversation-1",
        input_text="我要办理测试事项",
        flow_id="test.confirm",
        **overrides,
    )
    return container.tasks.create_task(request)


def test_missing_fields_pause_without_calling_executor(container, executor) -> None:
    task = start(container)

    assert task.status is TaskStatus.WAITING_FOR_USER
    assert task.pending_fields == ["subject", "contact"]
    assert executor.calls == []
    events = container.tasks.events(task.task_id)
    assert EventType.CLARIFICATION_REQUIRED in [event.type for event in events]


def test_answer_then_requires_confirmation(container, executor) -> None:
    task = start(container)
    task = container.tasks.answer(task.task_id, TaskAnswerRequest(values={"subject": "测试主题"}))

    assert task.status is TaskStatus.WAITING_FOR_USER
    assert task.pending_fields == ["contact"]

    task = container.tasks.answer(task.task_id, TaskAnswerRequest(values={"contact": "12345"}))

    assert task.status is TaskStatus.WAITING_FOR_CONFIRMATION
    assert task.current_step == "submit"
    assert executor.calls == []


def test_confirmation_executes_once_and_completes(container, executor) -> None:
    task = start(container, slots={"subject": "测试主题", "contact": "12345"})
    assert task.status is TaskStatus.WAITING_FOR_CONFIRMATION

    task = container.tasks.confirm(task.task_id, TaskConfirmRequest(step_id="submit"))

    assert task.status is TaskStatus.COMPLETED
    assert len(executor.calls) == 1
    assert executor.calls[0].tool == "test.submit"
    assert executor.calls[0].arguments == {"subject": "测试主题", "contact": "12345"}
    assert task.result == {"tool": "test.submit", "step_id": "submit"}


def test_second_confirmation_is_rejected(container, executor) -> None:
    task = start(container, slots={"subject": "测试主题", "contact": "12345"})
    container.tasks.confirm(task.task_id, TaskConfirmRequest(step_id="submit"))

    with pytest.raises(TaskConflict):
        container.tasks.confirm(task.task_id, TaskConfirmRequest(step_id="submit"))

    assert len(executor.calls) == 1


def test_parameter_change_after_confirmation_requires_new_confirmation(
    container, executor
) -> None:
    task = start(container, slots={"subject": "测试主题", "contact": "12345"})
    assert task.status is TaskStatus.WAITING_FOR_CONFIRMATION

    # 群众在执行前修改了信息，旧凭证必须失效。
    task = container.tasks.answer(task.task_id, TaskAnswerRequest(values={"contact": "99999"}))

    assert task.status is TaskStatus.WAITING_FOR_CONFIRMATION
    assert executor.calls == []

    task = container.tasks.confirm(task.task_id, TaskConfirmRequest(step_id="submit"))
    assert task.status is TaskStatus.COMPLETED
    assert executor.calls[0].arguments["contact"] == "99999"


def test_unconfirmed_outcome_is_not_retried(container, executor) -> None:
    executor.results.append(
        ExecutionResult(
            outcome=ExecutionOutcome.UNCONFIRMED,
            error_code="external_timeout",
            message="外部系统未返回结果。",
        )
    )
    task = start(container, slots={"subject": "测试主题", "contact": "12345"})
    task = container.tasks.confirm(task.task_id, TaskConfirmRequest(step_id="submit"))

    assert task.status is TaskStatus.OUTCOME_UNCONFIRMED
    assert len(executor.calls) == 1

    events = container.tasks.events(task.task_id)
    assert events[-1].type is EventType.OUTCOME_UNCONFIRMED

    with pytest.raises(TaskConflict):
        container.tasks.answer(task.task_id, TaskAnswerRequest(values={"contact": "1"}))


def test_failed_execution_hands_off(container, executor) -> None:
    executor.results.append(
        ExecutionResult(
            outcome=ExecutionOutcome.FAILED,
            error_code="tool_failed",
            message="自动化执行失败。",
        )
    )
    task = start(container, slots={"subject": "测试主题", "contact": "12345"})
    task = container.tasks.confirm(task.task_id, TaskConfirmRequest(step_id="submit"))

    assert task.status is TaskStatus.HANDOFF_REQUIRED
    assert task.failure_code == "tool_failed"
    events = container.tasks.events(task.task_id)
    handoff = [event for event in events if event.type is EventType.HANDOFF_REQUIRED]
    assert handoff and handoff[0].data["completed_steps"] == ["collect"]


def test_unexpected_answer_field_is_rejected(container) -> None:
    task = start(container)

    with pytest.raises(RequestRejected):
        container.tasks.answer(task.task_id, TaskAnswerRequest(values={"unknown": "x"}))


def test_flow_without_confirmation_completes_directly(container, executor) -> None:
    task = container.tasks.create_task(
        TaskCreateRequest(
            conversation_id="conversation-1",
            input_text="查询公开信息",
            flow_id="test.direct",
        )
    )

    assert task.status is TaskStatus.COMPLETED
    assert len(executor.calls) == 1


def test_cancelled_task_cannot_be_modified(container) -> None:
    task = start(container)
    cancelled = container.tasks.cancel(task.task_id)

    assert cancelled.status is TaskStatus.CANCELLED
    assert container.tasks.events(task.task_id)[-1].type is EventType.CANCELLED

    with pytest.raises(TaskConflict):
        container.tasks.cancel(task.task_id)
