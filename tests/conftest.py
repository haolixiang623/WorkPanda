"""测试夹具：使用真实流程定义 + 可控执行器，不依赖外部系统。"""

from collections.abc import Iterator

import pytest

from workpanda.bootstrap import Container, build_container
from workpanda.config import Settings
from workpanda.domain.flow import FlowDefinition, FlowStatus, StepDefinition, StepKind
from workpanda.infrastructure.adapters import KeywordIntentRouter, ScriptedExecutor

CONFIRM_FLOW = FlowDefinition(
    flow_id="test.confirm",
    version="1",
    title="需要确认的测试流程",
    status=FlowStatus.PUBLISHED,
    steps=(
        StepDefinition(
            step_id="collect",
            kind=StepKind.COLLECT,
            title="收集信息",
            required_fields=("subject", "contact"),
            prompt="请补充主题和联系方式。",
        ),
        StepDefinition(
            step_id="submit",
            kind=StepKind.EXECUTE,
            title="提交",
            tool="test.submit",
            requires_confirmation=True,
            confirm_message="即将提交，请确认。",
        ),
    ),
)

DIRECT_FLOW = FlowDefinition(
    flow_id="test.direct",
    version="1",
    title="无需确认的测试流程",
    status=FlowStatus.PUBLISHED,
    steps=(
        StepDefinition(
            step_id="query",
            kind=StepKind.EXECUTE,
            title="查询",
            tool="test.query",
        ),
    ),
)


@pytest.fixture
def executor() -> ScriptedExecutor:
    return ScriptedExecutor()


@pytest.fixture
def container(executor: ScriptedExecutor) -> Iterator[Container]:
    built = build_container(
        Settings(store_backend="memory", enable_demo_flow=False),
        flows=[CONFIRM_FLOW, DIRECT_FLOW],
        executor=executor,
        router=KeywordIntentRouter(),
    )
    yield built
    built.close()
