"""骨架自检流程。

它只验证"收集 → 确认 → 执行 → 完成"的工程链路，不代表任何真实业务事项。
正式接入前必须通过配置关闭。

这里的意图样例同时是**发布门禁的基线**：条数下限（3 条正例 / 2 条近似误例）就是按
这个流程定的。样例全部是合成话术，不含任何真实群众信息。

版本史：

- `1`：执行步骤声明 `tool="model_echo"`——一个只存在于脚本化执行器口头约定里的
  名字，装配层从不注册它。于是真实 harness 下 `_grant()` 的授权交集为空：模型
  照样被调用、照样回一段 `succeeded`，但**一个工具都没跑过**。而
  `_unverified_success()` 在"本步骤没有可用工具"时直接放行，这段自述就被当成
  可核验结论——"缺依据应当未确认"的契约在"工具压根不存在"这一档上失效了。
- `2`：改用内置的 `sys_clock`（真注册、只读、无副作用），并把"依据必须来自工具
  返回值"写进指令。老库里的 `1` 不会被覆盖（版本不可变），但也不再发布它。
"""

from typing import Final

from workpanda.domain.flow import FlowDefinition, FlowStatus, StepDefinition, StepKind
from workpanda.domain.intent import IntentRule
from workpanda.domain.intent_sample import IntentSample, IntentSampleCategory

# 当前要发布的版本。测试与运维脚本引用它，避免把版本号散落成字面量。
DEMO_FLOW_VERSION: Final[str] = "2"

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

# 自检流程的意图规则。只认自检话术，别的话一律不路由——占位规则越"宽容"，
# 真实试点上线时越容易被误当成业务识别能力。
#
# 它随版本内容一起入库、一起发布（`FlowDefinition.intent_rules`）：规则存在哪里，
# 决定了"发布"是不是一次生效。留在代码常量里时，发布只移动了指针。
DEMO_INTENT_RULES: tuple[IntentRule, ...] = (
    IntentRule(
        intent_id="demo.generic.selfcheck",
        flow_id="demo.generic",
        # 必须指向**本版**：规则随版本走，发布这一版等于同时换掉识别口径（#73）。
        flow_version=DEMO_FLOW_VERSION,
        keywords=("自检", "演示"),
        slot_patterns={
            "subject": (
                r"(?:主题|内容)[:：]\s*([^\s,，。;；]+)",
                r"自检[:：]\s*([^\s,，。;；]+)",
            )
        },
    ),
)

DEMO_FLOW = FlowDefinition(
    flow_id="demo.generic",
    version=DEMO_FLOW_VERSION,
    title="骨架自检流程",
    status=FlowStatus.PUBLISHED,
    steps=(
        StepDefinition(
            step_id="collect_input",
            kind=StepKind.COLLECT,
            title="收集必要信息",
            required_fields=("subject",),
            prompt="请补充需要办理的主题。",
            # 群众侧页面只按 `field_labels` 显示字段名，缺标签就回退成 `subject`。
            # 真实浏览器走查里这一屏确实印出了 `subject`——机制在，但没接线。
            field_labels={"subject": "办理主题"},
        ),
        StepDefinition(
            step_id="execute_demo",
            kind=StepKind.EXECUTE,
            title="工具链路自检",
            # 必须是装配层真的注册过的工具名，否则授权交集为空（见模块文档的版本史）。
            tool="sys_clock",
            instruction=(
                "调用 sys_clock 工具读取当前时间，把工具返回的时间字符串原样写进 "
                "evidence；不要自己推算时间，也不要用参数 subject 顶替依据。"
            ),
            requires_confirmation=True,
            confirm_message="即将提交以下信息，请确认。",
        ),
    ),
    intent_samples=DEMO_INTENT_SAMPLES,
    # 规则随版本走（D2=B）：发布这一版等于同时换掉识别口径。#73 之前它是装配期
    # 代码常量，发布新版本只移动了指针、识别仍按旧版本，群众侧直接 FlowNotRunnable。
    intent_rules=DEMO_INTENT_RULES,
)


def demo_flows() -> list[FlowDefinition]:
    return [DEMO_FLOW]


def demo_intent_samples() -> tuple[IntentSample, ...]:
    return DEMO_INTENT_SAMPLES
