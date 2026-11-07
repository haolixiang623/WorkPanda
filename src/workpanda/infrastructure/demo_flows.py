"""骨架自检流程。

它只验证"收集 → 确认 → 执行 → 完成"的工程链路，不代表任何真实业务事项。
正式接入前必须通过配置关闭。
"""

from workpanda.domain.flow import FlowDefinition, FlowStatus, StepDefinition, StepKind
from workpanda.domain.intent import IntentRule

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
            title="模型回显自检",
            tool="model_echo",
            instruction=(
                "把参数 subject 的值原样返回，不要改写；"
                "并在 evidence 中给出你实际返回的内容，作为可核验依据。"
            ),
            requires_confirmation=True,
            confirm_message="即将提交以下信息，请确认。",
        ),
    ),
)

# 自检流程的意图规则。只认自检话术，别的话一律不路由——占位规则越"宽容"，
# 真实试点上线时越容易被误当成业务识别能力。
DEMO_INTENT_RULES: tuple[IntentRule, ...] = (
    IntentRule(
        intent_id="demo.generic.selfcheck",
        flow_id=DEMO_FLOW.flow_id,
        flow_version=DEMO_FLOW.version,
        keywords=("自检", "演示"),
        slot_patterns={
            "subject": (
                r"(?:主题|内容)[:：]\s*([^\s,，。;；]+)",
                r"自检[:：]\s*([^\s,，。;；]+)",
            )
        },
    ),
)


def demo_flows() -> list[FlowDefinition]:
    return [DEMO_FLOW]


def demo_intent_rules() -> tuple[IntentRule, ...]:
    return DEMO_INTENT_RULES

