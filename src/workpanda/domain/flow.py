"""服务端固定业务流程定义。

流程定义只存在于服务端，不下发到浏览器，也不允许模型生成或改写步骤。
本模块只描述结构与校验，不包含任何具体业务事项。
"""

from enum import StrEnum

from pydantic import BaseModel, Field


class FlowStatus(StrEnum):
    DRAFT = "draft"
    PUBLISHED = "published"
    DISABLED = "disabled"


class StepKind(StrEnum):
    COLLECT = "collect"
    EXECUTE = "execute"
    HANDOFF = "handoff"


class StepDefinition(BaseModel):
    step_id: str = Field(min_length=1, max_length=128)
    kind: StepKind
    title: str = Field(min_length=1, max_length=200)
    required_fields: tuple[str, ...] = ()
    prompt: str | None = Field(default=None, max_length=1000)
    # 执行步骤的明确指令；由流程定义给出，模型不得自行改写。
    instruction: str | None = Field(default=None, max_length=1000)
    tool: str | None = Field(default=None, max_length=128)
    # 需要群众确认的执行节点：暂停、签发一次性凭证、执行前二次校验。
    requires_confirmation: bool = False
    confirm_message: str | None = Field(default=None, max_length=1000)
    fail_to_handoff: bool = True


class FlowDefinition(BaseModel):
    flow_id: str = Field(min_length=1, max_length=128)
    version: str = Field(min_length=1, max_length=64)
    title: str = Field(min_length=1, max_length=200)
    status: FlowStatus = FlowStatus.PUBLISHED
    steps: tuple[StepDefinition, ...] = Field(min_length=1)

    def step(self, step_id: str) -> StepDefinition | None:
        for step in self.steps:
            if step.step_id == step_id:
                return step
        return None

    def next_step(self, step_id: str | None) -> StepDefinition | None:
        if step_id is None:
            return self.steps[0]
        for index, step in enumerate(self.steps):
            if step.step_id == step_id:
                return self.steps[index + 1] if index + 1 < len(self.steps) else None
        return None


class FlowNotFound(LookupError):
    """流程或版本不存在。"""


class FlowNotRunnable(ValueError):
    """流程存在但未发布。"""


class FlowRegistry:
    """进程内流程目录；生产环境应由持久化配置和发布审核替代。"""

    def __init__(self, flows: list[FlowDefinition] | None = None) -> None:
        self._flows: dict[tuple[str, str], FlowDefinition] = {}
        for flow in flows or []:
            self.register(flow)

    def register(self, flow: FlowDefinition) -> None:
        self._validate(flow)
        self._flows[(flow.flow_id, flow.version)] = flow

    def resolve(self, flow_id: str, version: str | None = None) -> FlowDefinition:
        key = (flow_id, version) if version else None
        if key is not None and key in self._flows:
            return self._published(self._flows[key])
        candidates = [
            flow
            for (registered_id, _), flow in self._flows.items()
            if registered_id == flow_id
        ]
        if not candidates:
            raise FlowNotFound(f"flow {flow_id!r} is not registered")
        published = [flow for flow in candidates if flow.status is FlowStatus.PUBLISHED]
        if not published:
            raise FlowNotRunnable(f"flow {flow_id!r} has no published version")
        return max(published, key=lambda flow: flow.version)

    def require(self, flow_id: str, version: str) -> FlowDefinition:
        try:
            flow = self._flows[(flow_id, version)]
        except KeyError as exc:
            raise FlowNotFound(f"flow {flow_id!r} version {version!r} is not registered") from exc
        return self._published(flow)

    @staticmethod
    def _published(flow: FlowDefinition) -> FlowDefinition:
        if flow.status is not FlowStatus.PUBLISHED:
            raise FlowNotRunnable(f"flow {flow.flow_id!r} is not published")
        return flow

    @staticmethod
    def _validate(flow: FlowDefinition) -> None:
        seen: set[str] = set()
        for step in flow.steps:
            if step.step_id in seen:
                raise ValueError(f"duplicate step_id {step.step_id!r} in flow {flow.flow_id!r}")
            seen.add(step.step_id)
            if step.kind is StepKind.EXECUTE:
                if not step.tool:
                    raise ValueError(f"execute step {step.step_id!r} must declare a tool")
            else:
                if step.tool:
                    raise ValueError(
                        f"step {step.step_id!r} may only declare a tool when executing"
                    )
                if step.requires_confirmation:
                    raise ValueError(
                        f"step {step.step_id!r} may only require confirmation when executing"
                    )
