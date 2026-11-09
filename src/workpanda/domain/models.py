"""业务无关的任务、事件与状态模型。"""

from datetime import UTC, datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4

from pydantic import BaseModel, Field, computed_field

if TYPE_CHECKING:
    # 只为标注：`flow` 在运行时依赖 `models`（`StepKind` 之外还有状态枚举），
    # 顶层互相 import 会成环。
    from workpanda.domain.flow import FlowRegistry


def utc_now() -> datetime:
    return datetime.now(UTC)


class TaskStatus(StrEnum):
    CREATED = "created"
    RUNNING = "running"
    WAITING_FOR_USER = "waiting_for_user"
    WAITING_FOR_CONFIRMATION = "waiting_for_confirmation"
    OUTCOME_UNCONFIRMED = "outcome_unconfirmed"
    HANDOFF_REQUIRED = "handoff_required"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class EventType(StrEnum):
    ACCEPTED = "accepted"
    STEP_STARTED = "step_started"
    STEP_COMPLETED = "step_completed"
    CLARIFICATION_REQUIRED = "clarification_required"
    SLOTS_UPDATED = "slots_updated"
    CONFIRMATION_REQUIRED = "confirmation_required"
    CONFIRMATION_ACCEPTED = "confirmation_accepted"
    CONFIRMATION_DECLINED = "confirmation_declined"
    EXECUTION_SUCCEEDED = "execution_succeeded"
    EXECUTION_FAILED = "execution_failed"
    OUTCOME_UNCONFIRMED = "outcome_unconfirmed"
    HANDOFF_REQUIRED = "handoff_required"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    # 运营人员的人工处置备注。只追加事实，不改变任务状态，也不下发给群众。
    OPERATOR_NOTE = "operator_note"


class Task(BaseModel):
    task_id: UUID = Field(default_factory=uuid4)
    conversation_id: str = Field(min_length=1, max_length=128)
    user_ref: str | None = Field(default=None, max_length=128)
    flow_id: str = Field(min_length=1, max_length=128)
    flow_version: str = Field(min_length=1, max_length=64)
    status: TaskStatus = TaskStatus.CREATED
    current_step: str | None = None
    completed_steps: list[str] = Field(default_factory=list)
    slots: dict[str, str] = Field(default_factory=dict)
    pending_fields: list[str] = Field(default_factory=list)
    result: dict[str, Any] | None = None
    failure_code: str | None = None
    failure_message: str | None = None
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)

    @property
    def is_terminal(self) -> bool:
        return self.status in {
            TaskStatus.COMPLETED,
            TaskStatus.FAILED,
            TaskStatus.CANCELLED,
            TaskStatus.HANDOFF_REQUIRED,
        }


class TaskEvent(BaseModel):
    event_id: UUID = Field(default_factory=uuid4)
    task_id: UUID
    type: EventType
    status: TaskStatus
    sequence: int
    occurred_at: datetime = Field(default_factory=utc_now)
    data: dict[str, Any] = Field(default_factory=dict)


class TaskCreateRequest(BaseModel):
    conversation_id: str = Field(min_length=1, max_length=128)
    input_text: str = Field(min_length=1, max_length=10_000)
    flow_id: str | None = Field(default=None, max_length=128)
    flow_version: str | None = Field(default=None, max_length=64)
    # 归属由服务端从会话解析，客户端不能自行声明 user_ref，避免冒充他人。
    slots: dict[str, str] = Field(default_factory=dict)


class TaskAnswerRequest(BaseModel):
    values: dict[str, str] = Field(min_length=1)


class TaskConfirmRequest(BaseModel):
    step_id: str = Field(min_length=1, max_length=128)
    accepted: bool = True


class TaskNoteRequest(BaseModel):
    """运营人员追加的处置备注。

    刻意**没有** `status` 字段：这个入口只能补充事实，不能表达"把任务改成
    什么"。少一个字段就少一条覆写状态的路径，也就少一类"库里的结论和现实
    不一致、还查不出是谁改的"事故。

    也刻意**没有** `operator` 字段：署名由服务端从运维凭据主体取
    （`OpsPrincipal.subject`），不接受调用方自报。可自述的署名等于没有署名——
    真出事时它证明不了任何事。
    """

    note: str = Field(min_length=1, max_length=1000)
    # 外部依据（工单号、对方系统回执号等），便于日后复核时不必再问一遍。
    reference: str | None = Field(default=None, max_length=200)


class TaskView(BaseModel):
    """面向客户端的任务视图，不暴露内部存储细节。

    群众侧页面要用到的东西在这里**一次给全**，不让页面去猜：状态、待填字段、
    已填内容、当前步骤的说明与确认文案。页面拿不到就会自己编（把 `subject` 印成
    "subject"，或者按旧槽位拼一句确认摘要），而那是界面在替服务端说话——正是
    `domain/presentation.py` 要避免的那类分叉（#77）。
    """

    task_id: UUID
    conversation_id: str
    flow_id: str
    flow_version: str
    # 事项名（流程版本的 `title`）。群众侧开场要说"您要办的是哪件事"，而
    # `current_step_title` 是步骤名（"收集报修信息"），不是事项名（"报修"）。
    # 页面唯一的替代品是复述群众原话，但 `Task` 根本不保存 `input_text`——
    # 那句话在任何地方都取不回来。版本缺失时为 None，页面退回不显示，不猜。
    flow_title: str | None = None
    status: TaskStatus
    current_step: str | None
    # 当前步骤的作者文案（来自流程版本，不是模型生成的）。页面直接展示，不改写。
    current_step_title: str | None = None
    current_step_prompt: str | None = None
    confirm_message: str | None = None
    completed_steps: list[str]
    pending_fields: list[str]
    # 字段的群众可读名字，覆盖**这一版流程声明过的全部字段**，不只是待填的那几个：
    # 确认页要把已收集的值逐条列出来核对，返回修改时要能指回具体字段。只给待填
    # 字段的话，确认页只能把 `subject` 原样印出来。缺标签时回退成字段名本身——
    # 页面不需要第二套规则。同一字段在多个步骤里给了不同标签时，先出现的明确
    # 标签优先，不让后一个步骤把前一个的说法改掉。
    field_labels: dict[str, str] = Field(default_factory=dict)
    # 需要打码的字段名，覆盖这一版流程声明过的全部字段（语义同 `field_labels`：
    # 逐步骤声明，按版本合并，先声明者在前）。页面据此决定回显时打不打码，
    # **不维护自己的字段名黑名单**——按名字猜"哪个像身份证"必然两头落空：猜漏了
    # 明文常驻屏幕，猜错了把订单号涂掉。未声明时为空列表，页面明文回显；那是
    # 流程作者的显式取舍，平台不替他断定敏感级。
    sensitive_fields: list[str] = Field(default_factory=list)
    # 已收集的字段值。确认摘要要能核对"到底提交什么"，所以必须给到客户端；
    # 但也只给当前任务自己的值，不跨任务、不跨会话。
    slots: dict[str, str] = Field(default_factory=dict)
    result: dict[str, Any] | None
    failure_code: str | None
    failure_message: str | None
    created_at: datetime
    updated_at: datetime

    @computed_field  # type: ignore[prop-decorator]
    @property
    def ui_state(self) -> str:
        """群众可见状态键，由服务端唯一决定（见 `domain/presentation.py`）。

        放在响应里而不是让前端自己从 `status` 推：前端各推一份，就等于每个端各
        有一套"什么算办完"的口径，而其中最要命的那条（结果待确认不能显示成功）
        恰恰是最容易被"看起来更友好"的写法改坏的。

        局部导入是为了避免 `models ↔ presentation` 的循环依赖：presentation 需要
        `TaskStatus`，而这里只需要它的翻译函数。
        """

        from workpanda.domain.presentation import ui_state_for

        return ui_state_for(self.status).value

    # 群众可见文案也由服务端下发，而不是让页面自己维护一张 11 项的表：
    # 页面一旦有第二份措辞，"结果待确认"就可能被写成"已办结"，而这正是
    # `domain/presentation.py` 存在的理由。页面只负责原样显示。
    @computed_field  # type: ignore[prop-decorator]
    @property
    def ui_title(self) -> str:
        from workpanda.domain.presentation import copy_for_status

        return copy_for_status(self.status).title

    @computed_field  # type: ignore[prop-decorator]
    @property
    def ui_hint(self) -> str:
        from workpanda.domain.presentation import copy_for_status

        return copy_for_status(self.status).hint

    @classmethod
    def from_task(cls, task: Task, flows: "FlowRegistry | None" = None) -> "TaskView":
        """从任务投影出视图。`flows` 给了才补当前步骤的作者文案与字段标签。

        刻意做成可选：运维/工作人员的视图不需要群众文案，而展示层不该因为
        "库里查不到这个版本"就整条接口 500——那种任务照样要能看、能取消。
        """

        flow = None
        if flows is not None:
            flow = flows.find(task.flow_id, task.flow_version)
        step = None if flow is None or task.current_step is None else flow.step(task.current_step)
        labels: dict[str, str] = {}
        sensitive: list[str] = []
        if flow is not None:
            for candidate in flow.steps:
                for name in candidate.required_fields:
                    label = candidate.field_labels.get(name)
                    if label is not None:
                        labels[name] = label
                    else:
                        labels.setdefault(name, name)
                # 同一字段被多个步骤标记时只留一条：页面问的是"这个名字要不要打码"，
                # 不是"被声明了几次"。用列表而不是集合，是因为页面的输出要与服务端
                # 的声明顺序一致（测试要能断言具体顺序，而不是一个无序集合）。
                for name in candidate.sensitive_fields:
                    if name not in sensitive:
                        sensitive.append(name)
        return cls(
            task_id=task.task_id,
            conversation_id=task.conversation_id,
            flow_id=task.flow_id,
            flow_version=task.flow_version,
            flow_title=None if flow is None else flow.title,
            status=task.status,
            current_step=task.current_step,
            current_step_title=None if step is None else step.title,
            current_step_prompt=None if step is None else step.prompt,
            confirm_message=None if step is None else step.confirm_message,
            completed_steps=list(task.completed_steps),
            pending_fields=task.pending_fields,
            field_labels=labels,
            sensitive_fields=sensitive,
            slots=dict(task.slots),
            result=task.result,
            failure_code=task.failure_code,
            failure_message=task.failure_message,
            created_at=task.created_at,
            updated_at=task.updated_at,
        )
