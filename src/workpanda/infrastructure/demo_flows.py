"""骨架自检流程。

它只验证"收集 → 确认 → 执行 → 完成"的工程链路，不代表任何真实业务事项。
正式接入前必须通过配置关闭。
"""

from workpanda.domain.flow import FlowDefinition, FlowStatus, StepDefinition, StepKind

DEMO_FLOW = FlowDefinition(
    flow_id="demo.generic",
    version="1",
    title="骨架自检流程",
    status=FlowStatus.PUBLISHED,
    steps=(
        StepDefinition(
            step_id="collect_input",
            kind=StepKind.COLLECT,
            title="收集必要信息",
            required_fields=("subject",),
            prompt="请补充需要办理的主题。",
        ),
        StepDefinition(
            step_id="execute_demo",
            kind=StepKind.EXECUTE,
            title="执行自检动作",
            tool="demo.echo",
            requires_confirmation=True,
            confirm_message="即将提交以下信息，请确认。",
        ),
    ),
)


def demo_flows() -> list[FlowDefinition]:
    return [DEMO_FLOW]
