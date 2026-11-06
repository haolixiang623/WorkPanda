"""测试共用的流程定义与常量。"""

from workpanda.domain.flow import FlowDefinition, FlowStatus, StepDefinition, StepKind

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

TEST_FLOWS = [CONFIRM_FLOW, DIRECT_FLOW]
