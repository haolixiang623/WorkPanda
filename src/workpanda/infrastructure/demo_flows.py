"""骨架自检流程。

它只验证"收集 → 确认 → 执行 → 完成"的工程链路，不代表任何真实业务事项。
正式接入前必须通过配置关闭。

这里的意图样例同时是**发布门禁的基线**：条数下限（3 条正例 / 2 条近似误例）就是按
这个流程定的。样例全部是合成话术，不含任何真实群众信息。
"""

from workpanda.domain.flow import FlowDefinition, FlowStatus, StepDefinition, StepKind
from workpanda.domain.intent import IntentRule
from workpanda.domain.intent_sample import IntentSample, IntentSampleCategory

# 自检流程的意图样例。正例要能被路由到本流程的**这一版**；近似误例与超范围例
# 必须一条都路由不出去——否则"自检"这个关键词就太宽了，真实事项上线后会把别的话
# 也捞进来。
DEMO_INTENT_SAMPLES: tuple[IntentSample, ...] = (
    IntentSample(
        sample_id="pos_selfcheck_topic",
        category=IntentSampleCategory.POSITIVE,
        text="帮我做一次自检，主题：链路自检",
    ),
    IntentSample(
        sample_id="pos_selfcheck_short",
        category=IntentSampleCategory.POSITIVE,
        text="跑个自检",
    ),
    IntentSample(
        sample_id="pos_demo_flow",
        category=IntentSampleCategory.POSITIVE,
        text="演示一下自检流程，主题：端到端演示",
    ),
    IntentSample(
        sample_id="near_miss_inspection",
        category=IntentSampleCategory.NEAR_MISS,
        text="我要做一次检查，主题：设备巡检",
    ),
    IntentSample(
        sample_id="near_miss_contact",
        category=IntentSampleCategory.NEAR_MISS,
        text="我要变更手机号，主题：更换联系方式",
    ),
    IntentSample(
        sample_id="oos_weather",
        category=IntentSampleCategory.OUT_OF_SCOPE,
        text="今天天气怎么样",
    ),
)

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
    intent_samples=DEMO_INTENT_SAMPLES,
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


def demo_intent_samples() -> tuple[IntentSample, ...]:
    return DEMO_INTENT_SAMPLES
