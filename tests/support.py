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


def harness_flow(tool: str = "sys_clock") -> FlowDefinition:
    """给"只需要走 harness 分支"的用例用：执行步骤声明一个**真注册过**的工具。

    `TEST_FLOWS` 里的工具名（`test.submit` / `test.query`）只服务于脚本化执行器，
    真 harness 下授权交集会是空的。装配期对账（`harness/preflight.py`）会把这种
    缺口当错误拦下——那是对的，所以在这些用例里换成 `sys_clock`。
    """

    return FlowDefinition(
        flow_id="test.harness",
        version="1",
        title="harness 分支用例流程",
        status=FlowStatus.PUBLISHED,
        steps=(
            StepDefinition(
                step_id="run",
                kind=StepKind.EXECUTE,
                title="执行",
                tool=tool,
                instruction="调用工具并给出依据。",
                requires_confirmation=True,
                confirm_message="即将提交，请确认。",
            ),
        ),
    )
