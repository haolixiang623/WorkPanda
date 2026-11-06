"""状态机只允许显式许可的转换。"""

import pytest

from workpanda.domain.models import TaskStatus
from workpanda.domain.state_machine import InvalidTransition, ensure_transition, is_terminal


def test_allowed_transition() -> None:
    ensure_transition(TaskStatus.CREATED, TaskStatus.RUNNING)
    ensure_transition(TaskStatus.WAITING_FOR_USER, TaskStatus.RUNNING)


def test_outcome_unconfirmed_cannot_silently_succeed() -> None:
    with pytest.raises(InvalidTransition):
        ensure_transition(TaskStatus.OUTCOME_UNCONFIRMED, TaskStatus.COMPLETED)


def test_terminal_states_are_sticky() -> None:
    for status in (TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED):
        assert is_terminal(status)
        with pytest.raises(InvalidTransition):
            ensure_transition(status, TaskStatus.RUNNING)


def test_handoff_does_not_restart_automation() -> None:
    with pytest.raises(InvalidTransition):
        ensure_transition(TaskStatus.HANDOFF_REQUIRED, TaskStatus.RUNNING)
