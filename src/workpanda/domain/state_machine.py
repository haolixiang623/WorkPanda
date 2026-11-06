"""任务状态转换规则。

状态由服务端流程推进，客户端不能直接设置。任何未列出的转换都必须拒绝，
避免出现"从失败直接跳到完成"这类掩盖真实结果的路径。
"""

from workpanda.domain.models import TaskStatus

TERMINAL: frozenset[TaskStatus] = frozenset(
    {
        TaskStatus.COMPLETED,
        TaskStatus.FAILED,
        TaskStatus.CANCELLED,
        TaskStatus.HANDOFF_REQUIRED,
    }
)

TRANSITIONS: dict[TaskStatus, frozenset[TaskStatus]] = {
    TaskStatus.CREATED: frozenset(
        {
            TaskStatus.RUNNING,
            TaskStatus.WAITING_FOR_USER,
            TaskStatus.WAITING_FOR_CONFIRMATION,
            TaskStatus.FAILED,
            TaskStatus.CANCELLED,
        }
    ),
    TaskStatus.RUNNING: frozenset(
        {
            TaskStatus.RUNNING,
            TaskStatus.WAITING_FOR_USER,
            TaskStatus.WAITING_FOR_CONFIRMATION,
            TaskStatus.OUTCOME_UNCONFIRMED,
            TaskStatus.HANDOFF_REQUIRED,
            TaskStatus.COMPLETED,
            TaskStatus.FAILED,
            TaskStatus.CANCELLED,
        }
    ),
    TaskStatus.WAITING_FOR_USER: frozenset(
        {
            TaskStatus.RUNNING,
            TaskStatus.WAITING_FOR_CONFIRMATION,
            TaskStatus.HANDOFF_REQUIRED,
            TaskStatus.FAILED,
            TaskStatus.CANCELLED,
        }
    ),
    TaskStatus.WAITING_FOR_CONFIRMATION: frozenset(
        {
            TaskStatus.RUNNING,
            TaskStatus.WAITING_FOR_USER,
            TaskStatus.HANDOFF_REQUIRED,
            TaskStatus.FAILED,
            TaskStatus.CANCELLED,
        }
    ),
    # 结果未知时不能自动重试；只能转人工核实，或由后续人工对账流程显式处理。
    TaskStatus.OUTCOME_UNCONFIRMED: frozenset({TaskStatus.HANDOFF_REQUIRED}),
    TaskStatus.HANDOFF_REQUIRED: frozenset(),
    TaskStatus.COMPLETED: frozenset(),
    TaskStatus.FAILED: frozenset(),
    TaskStatus.CANCELLED: frozenset(),
}


class InvalidTransition(ValueError):
    """不允许的状态转换。"""


def ensure_transition(current: TaskStatus, target: TaskStatus) -> None:
    if target not in TRANSITIONS[current]:
        raise InvalidTransition(f"task status cannot move from {current} to {target}")


def is_terminal(status: TaskStatus) -> bool:
    return status in TERMINAL
