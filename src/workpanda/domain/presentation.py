"""群众可见状态：服务端唯一的展示真相。

`design/DESIGN.md` 第 3.1 节定义了"界面应能表达哪些状态"，但没有说它们和
`TaskStatus` 的对应关系。对应关系一旦散落在前端，就会出现"后端已完成、界面显示
办理中"，或者更糟的"后端结果待确认、界面显示办理成功"。因此这里把它固定成一张
**穷尽**的映射表，并由 `tests/test_ui_state.py` 当回归集守住：

- 每个 `TaskStatus` 恰好映射到一个 UI 状态（新增状态而不加映射会直接测挂）；
- 只有"已完成"允许使用成功语义，`outcome_unconfirmed` / `handoff_required` 一律
  用"待确认 / 需人工"的措辞，绝不让群众以为办成了；
- 文案使用群众语言：不出现 Agent、Harness、模型、trace、槽位这类内部术语。

这一层不做任何判断，只做翻译。任何"根据动画结束或请求超时推断完成"的写法都属于
违规，不在本模块提供入口。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from workpanda.domain.models import TaskStatus


class UiState(StrEnum):
    """面向群众的稳定状态键。前端按这个键取文案与交互，不按内部状态名。"""

    IDLE = "idle"
    RECEIVED = "received"
    PROCESSING = "processing"
    # 无法确定要办什么（未命中、歧义、置信度不足）：必须先澄清，不产生任何副作用。
    NEEDS_CLARIFICATION = "needs_clarification"
    AWAITING_INPUT = "awaiting_input"
    AWAITING_CONFIRMATION = "awaiting_confirmation"
    COMPLETED = "completed"
    FAILED = "failed"
    OUTCOME_UNCONFIRMED = "outcome_unconfirmed"
    HANDOFF = "handoff"
    CANCELLED = "cancelled"


@dataclass(frozen=True)
class StateCopy:
    """一个 UI 状态的群众可见文案。`title` 必须短，`hint` 必须说下一步。"""

    title: str
    hint: str


# 穷尽映射：`TaskStatus` 的每个成员都必须在这里出现一次。
STATUS_UI: dict[TaskStatus, UiState] = {
    TaskStatus.CREATED: UiState.RECEIVED,
    TaskStatus.RUNNING: UiState.PROCESSING,
    TaskStatus.WAITING_FOR_USER: UiState.AWAITING_INPUT,
    TaskStatus.WAITING_FOR_CONFIRMATION: UiState.AWAITING_CONFIRMATION,
    TaskStatus.OUTCOME_UNCONFIRMED: UiState.OUTCOME_UNCONFIRMED,
    TaskStatus.HANDOFF_REQUIRED: UiState.HANDOFF,
    TaskStatus.COMPLETED: UiState.COMPLETED,
    TaskStatus.FAILED: UiState.FAILED,
    TaskStatus.CANCELLED: UiState.CANCELLED,
}

STATE_COPY: dict[UiState, StateCopy] = {
    UiState.IDLE: StateCopy("可以开始", "请描述您要办理的事情，说得口语一点也可以。"),
    UiState.RECEIVED: StateCopy("已收到您的请求", "正在为您确认办理事项，请不要重复发送。"),
    UiState.PROCESSING: StateCopy("正在处理", "系统正在处理，请不要重复提交。"),
    UiState.NEEDS_CLARIFICATION: StateCopy(
        "需要再确认一下", "请说明您要办理的具体事情，或者选择相近的事项。"
    ),
    UiState.AWAITING_INPUT: StateCopy("需要您补充信息", "请补充提示的内容，补齐后会继续办理。"),
    UiState.AWAITING_CONFIRMATION: StateCopy(
        "等待您确认", "请核对信息后确认提交，也可以返回修改。"
    ),
    UiState.COMPLETED: StateCopy("已办理完成", "请查看办理结果，如需其他帮助可以继续提问。"),
    UiState.FAILED: StateCopy("办理未完成", "当前步骤没有完成，您可以重试，或转人工协助。"),
    # 刻意不用"失败"：外部结果未知时既不是成功也不是失败，措辞必须留出核实空间。
    UiState.OUTCOME_UNCONFIRMED: StateCopy(
        "状态待确认", "暂时无法核实结果，请不要重复提交，可查询进度或转人工。"
    ),
    UiState.HANDOFF: StateCopy("已转人工", "已交由人工继续处理，请留意后续联系。"),
    UiState.CANCELLED: StateCopy("已取消", "本次办理已取消，如仍有需要可以重新发起。"),
}


def ui_state_for(status: TaskStatus) -> UiState:
    """状态翻译。没有映射时抛错而不是默认成某个状态：

    默认值会把"新增状态没人处理"变成"界面显示正在处理"，是最难查的一类线上问题。
    """

    try:
        return STATUS_UI[status]
    except KeyError as exc:  # pragma: no cover - 由回归集保证不可达
        raise ValueError(f"no UI state for task status {status!r}") from exc


def copy_for(state: UiState) -> StateCopy:
    return STATE_COPY[state]


def copy_for_status(status: TaskStatus) -> StateCopy:
    return STATE_COPY[ui_state_for(status)]
