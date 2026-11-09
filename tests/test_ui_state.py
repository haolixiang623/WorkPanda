"""UI 状态回归集：群众看到的状态，必须只有一套口径。

`design/DESIGN.md` 第 3.1 节要求"界面能表达哪些状态"，但最容易被改坏的不是
多一个少一个状态，而是**语义漂移**：把"结果待确认"写成"办理成功"、把"转人工"
写成"稍后再试"。这类改动在代码审查里看着像文案优化，在线上就是虚假办结。

因此这里把三件事固定成回归集：

1. `TaskStatus` → UI 状态是一张**穷尽且单值**的映射表；
2. 只有"已完成"允许使用成功语义，其余一律不许；
3. 文案里不许出现内部术语，也不许出现数字/百分比（那会读成进度或时限承诺）。
"""

from __future__ import annotations

import pytest

from tests.support import TEST_FLOWS
from workpanda.application.ports import ExecutionOutcome, ExecutionResult
from workpanda.bootstrap import Container, build_container
from workpanda.config import Settings
from workpanda.domain.models import (
    TaskConfirmRequest,
    TaskCreateRequest,
    TaskStatus,
    TaskView,
)
from workpanda.domain.presentation import (
    STATE_COPY,
    STATUS_UI,
    UiState,
    copy_for_status,
    ui_state_for,
)
from workpanda.infrastructure.adapters import ScriptedExecutor

# 文案或映射改动时递增，PR 里要说明"评的是哪一版"。
UI_COPY_VERSION = "2026-11-09.1"

SUBJECT = "session-a"

# 内部术语：群众看不懂，也不该看到。
FORBIDDEN_TERMS = (
    "harness",
    "agent",
    "智能体",
    "模型",
    "trace",
    "token",
    "槽位",
    "意图",
    "workflow",
    "runtime",
    "接口",
    "api",
    "服务端",
    "提示词",
    "llm",
    "mcp",
    "slot",
    "step",
)

# 成功语义。只有"已完成"能用。
SUCCESS_WORDING = ("成功", "已经办成", "已办结", "已办理完成")

FIRST_PERSON_FORBIDDEN = ("subject", "contact", "slots", "step_id", "flow_id")


def build(executor=None) -> Container:
    return build_container(
        Settings(store_backend="memory", enable_demo_flow=False),
        flows=TEST_FLOWS,
        executor=executor or ScriptedExecutor(),
    )


def start(container: Container, **overrides) -> TaskView:
    task = container.tasks.create_task(
        TaskCreateRequest(
            conversation_id="conversation-1",
            input_text="我要办理测试事项",
            flow_id="test.confirm",
            **overrides,
        ),
        SUBJECT,
    )
    return TaskView.from_task(task)


def test_copy_version_is_pinned() -> None:
    assert UI_COPY_VERSION == "2026-11-09.1"


def test_every_task_status_has_exactly_one_ui_state() -> None:
    """新增状态却不加映射 = 界面上"卡在正在处理"，必须直接测挂。"""

    assert set(STATUS_UI) == set(TaskStatus)


def test_no_two_statuses_share_a_ui_state() -> None:
    """两个内部状态挤进同一个展示状态，等于把差异藏起来。"""

    assert len(set(STATUS_UI.values())) == len(TaskStatus)


def test_every_ui_state_has_copy() -> None:
    assert set(STATE_COPY) == set(UiState)


def test_unmapped_status_raises_instead_of_defaulting() -> None:
    with pytest.raises(ValueError):
        ui_state_for("not_a_status")  # type: ignore[arg-type]


@pytest.mark.parametrize("status", list(TaskStatus), ids=lambda status: status.value)
def test_copy_is_plain_language(status: TaskStatus) -> None:
    copy = copy_for_status(status)
    text = f"{copy.title}{copy.hint}"
    lowered = text.lower()

    assert copy.title.strip() and copy.hint.strip(), "每个状态都要有标题和下一步"
    assert len(copy.title) <= 12
    assert len(copy.hint) <= 40
    for term in FORBIDDEN_TERMS:
        assert term not in lowered, f"{status} 文案含内部术语：{term}"
    for term in FIRST_PERSON_FORBIDDEN:
        assert term not in lowered, f"{status} 文案暴露内部字段名：{term}"
    assert not any(character.isdigit() for character in text), "不许出现数字，避免被读成进度或时限"
    assert "%" not in text
    assert "http" not in lowered
    assert not any(ord(character) > 0x1F000 for character in text), "文案不许带表情符号"


@pytest.mark.parametrize("status", list(TaskStatus), ids=lambda status: status.value)
def test_success_wording_belongs_to_completed_only(status: TaskStatus) -> None:
    copy = copy_for_status(status)
    text = f"{copy.title}{copy.hint}"

    if status is TaskStatus.COMPLETED:
        return
    for wording in SUCCESS_WORDING:
        assert wording not in text, f"{status} 不得使用成功语义：{wording}"


def test_unconfirmed_and_handoff_never_look_successful() -> None:
    """两条最容易"看起来办成了"的路径，措辞必须留出核实空间。"""

    unconfirmed = copy_for_status(TaskStatus.OUTCOME_UNCONFIRMED)
    assert "成功" not in f"{unconfirmed.title}{unconfirmed.hint}"
    assert "重复提交" in unconfirmed.hint

    handoff = copy_for_status(TaskStatus.HANDOFF_REQUIRED)
    assert "人工" in f"{handoff.title}{handoff.hint}"


def test_terminal_statuses_map_to_terminal_ui_states() -> None:
    terminal_ui = {
        UiState.COMPLETED,
        UiState.FAILED,
        UiState.OUTCOME_UNCONFIRMED,
        UiState.HANDOFF,
        UiState.CANCELLED,
    }
    terminal_statuses = {
        TaskStatus.COMPLETED,
        TaskStatus.FAILED,
        TaskStatus.CANCELLED,
        TaskStatus.HANDOFF_REQUIRED,
    }

    assert {STATUS_UI[status] for status in terminal_statuses} <= terminal_ui


@pytest.mark.parametrize(
    "outcome,expected",
    [
        (ExecutionOutcome.SUCCEEDED, UiState.COMPLETED),
        (ExecutionOutcome.FAILED, UiState.HANDOFF),
        (ExecutionOutcome.UNCONFIRMED, UiState.OUTCOME_UNCONFIRMED),
    ],
    ids=["成功", "失败转人工", "结果未知"],
)
def test_view_exposes_ui_state_for_real_execution(
    outcome: ExecutionOutcome, expected: UiState
) -> None:
    """走一遍真实执行路径，断言序列化出去的 `ui_state` 与内部状态一致。"""

    executor = ScriptedExecutor(
        results=[ExecutionResult(outcome=outcome, error_code="test", message="测试")]
    )
    container = build(executor)
    try:
        view = start(container, slots={"subject": "主题", "contact": "12345"})
        assert view.ui_state == UiState.AWAITING_CONFIRMATION.value

        task = container.tasks.confirm(
            view.task_id, TaskConfirmRequest(step_id="submit"), SUBJECT
        )
        payload = TaskView.from_task(task).model_dump()
        assert payload["ui_state"] == expected.value
        assert payload["status"] == task.status.value
        if outcome is not ExecutionOutcome.SUCCEEDED:
            assert payload["result"] is None, "没办成就不该有结果"
    finally:
        container.close()


def test_cancelled_task_shows_cancelled_state() -> None:
    container = build()
    try:
        view = start(container)
        task = container.tasks.cancel(view.task_id, SUBJECT)
        assert TaskView.from_task(task).ui_state == UiState.CANCELLED.value
    finally:
        container.close()
