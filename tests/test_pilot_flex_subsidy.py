"""首个真实试点事项：灵活就业社保补贴·材料清单自查。

这组用例守的不是"功能跑通"，而是三条**不许被悄悄放宽**的边界：

1. 口径只能来自`policy_checklist` 工具，且工具返回值自带来源地址与核对日期——
   换掉来源或改掉某一条材料，用例必须红；
2. 不收集任何证件信息。字段名、标签、提示语里出现"身份证号"这类入参就是越界；
3. 不判断资格。工具输出与流程指令里出现"你可以申请"这种结论就必须被拦下。

另外单独钉住输出侧：办结页拿到的只有流程作者声明的那一段正文，平台元数据
（trace、工具调用、token）一条都不能跟着出去。这条以前是"机制在、没接线"——
模型的结论埋在 `verification` 里被群众页跳过，而 `trace_id` 反倒印在群众脸上。
"""

from __future__ import annotations

import json
from uuid import uuid4

import pytest

from workpanda.application.intent_eval import IntentEvalRunner
from workpanda.bootstrap import build_container
from workpanda.config import Settings
from workpanda.domain.flow import (
    FlowDefinition,
    FlowFieldError,
    StepDefinition,
    StepKind,
    flow_field_errors,
)
from workpanda.domain.intent import RuleIntentRouter
from workpanda.domain.intent_eval import IntentEvalStatus
from workpanda.domain.intent_sample import (
    MIN_NEAR_MISS_SAMPLES,
    MIN_POSITIVE_SAMPLES,
    IntentSampleCategory,
)
from workpanda.domain.models import TaskConfirmRequest, TaskCreateRequest, TaskStatus
from workpanda.harness.builtin import default_registry
from workpanda.harness.clients import ScriptedModelClient, tool_call
from workpanda.harness.executor import HarnessExecutor
from workpanda.harness.models import MessageRole, ModelResponse
from workpanda.harness.policy import (
    CATALOG_VERSION,
    CHECKED_ON,
    FLEX_SUBSIDY_TOPIC,
    SOURCE_URL,
    PolicyChecklistTool,
    render_topic,
    topics,
)
from workpanda.harness.preflight import missing_step_tools
from workpanda.harness.tools import ToolContext, ToolError
from workpanda.infrastructure.demo_flows import DEMO_FLOW, DEMO_INTENT_RULES
from workpanda.infrastructure.pilot_flows import (
    PILOT_FLEX_FLOW,
    PILOT_FLEX_INTENT_RULES,
    PILOT_FLEX_INTENT_SAMPLES,
    pilot_flows,
)

# 官方页面给的 4 项材料。写成常量而不是从实现里取，否则实现改错了用例也跟着改。
OFFICIAL_MATERIALS = (
    "身份证",
    "户口簿",
    "灵活就业人员社保补贴申请表",
    "高校毕业生毕业证书",
)


def _execute_step(flow: FlowDefinition) -> StepDefinition:
    return next(step for step in flow.steps if step.kind is StepKind.EXECUTE)


# ---------- 口径数据：来源可追溯、内容不越界 ----------


def test_catalog_cites_its_source_and_check_date() -> None:
    text = render_topic(FLEX_SUBSIDY_TOPIC)
    assert SOURCE_URL in text
    assert CHECKED_ON in text
    assert CATALOG_VERSION in text


def test_catalog_lists_exactly_the_four_official_materials() -> None:
    text = render_topic(FLEX_SUBSIDY_TOPIC)
    for name in OFFICIAL_MATERIALS:
        assert name in text
    # 反向兜底：四项之外不许"顺手补充"材料。多出来的材料会让群众白带一堆东西，
    # 而且没有任何来源能证明它是对的。
    assert text.count("灵活就业人员社保补贴申请表") == 1
    assert "学位证" not in text
    assert "社保卡复印件" not in text


def test_catalog_marks_the_graduate_material_as_conditional() -> None:
    text = render_topic(FLEX_SUBSIDY_TOPIC)
    # ④ 只对高校毕业生。不标注就等于让就业困难人员也去找毕业证书。
    assert "高校毕业生毕业证书（仅离校两年内未就业高校毕业生）" in text


def test_catalog_never_judges_eligibility() -> None:
    text = render_topic(FLEX_SUBSIDY_TOPIC)
    assert "由经办机构核定" in text
    for forbidden in ("你符合", "可以申请", "你满足", "符合申请条件"):
        assert forbidden not in text


def test_catalog_carries_the_two_easy_to_miss_warnings() -> None:
    text = render_topic(FLEX_SUBSIDY_TOPIC)
    # 这两条不在材料清单里，但会让群众白跑一趟或领了又被追回。
    assert "需先认定为" in text
    assert "不能同时享受" in text


def test_render_is_deterministic() -> None:
    # 两次自查必须给出同一份证据，否则回放用例没法比对。
    assert render_topic(FLEX_SUBSIDY_TOPIC) == render_topic(FLEX_SUBSIDY_TOPIC)


def test_unknown_topic_is_rejected_and_lists_what_is_available() -> None:
    with pytest.raises(ToolError) as excinfo:
        render_topic("no_such_topic")
    assert FLEX_SUBSIDY_TOPIC in str(excinfo.value)
    assert topics() == (FLEX_SUBSIDY_TOPIC,)


# ---------- 工具：只读、无个人信息入参 ----------


def test_policy_checklist_tool_is_registered_by_default() -> None:
    registry, closers = default_registry()
    try:
        assert "policy_checklist" in registry.names()
    finally:
        for close in closers:
            close()


def test_tool_takes_no_personal_information() -> None:
    spec = PolicyChecklistTool().spec
    schema = spec.parameters
    assert set(schema["properties"]) == {"topic"}
    assert schema["required"] == ["topic"]
    # 只读、无副作用：这两项决定它能不能被随便重试。
    assert spec.idempotent is True
    assert spec.sensitive_fields == []


def _context() -> ToolContext:
    return ToolContext(task_id=uuid4(), flow_id="test.flow", step_id="step")


def test_tool_returns_the_catalog_for_the_requested_topic() -> None:
    tool = PolicyChecklistTool()
    text = tool.invoke({"topic": FLEX_SUBSIDY_TOPIC}, _context())
    assert text == render_topic(FLEX_SUBSIDY_TOPIC)


def test_tool_rejects_a_missing_topic() -> None:
    with pytest.raises(ToolError):
        PolicyChecklistTool().invoke({}, _context())


# ---------- 流程定义：字段、工具、规则的自洽 ----------


def test_pilot_flow_only_declares_registered_tools() -> None:
    registry, closers = default_registry()
    try:
        missing = missing_step_tools(pilot_flows(), registry.names())
    finally:
        for close in closers:
            close()
    assert missing == []
    # 反向兜底：若流程某天不再有执行步骤，上面的空列表就是假绿。
    assert _execute_step(PILOT_FLEX_FLOW).tool == "policy_checklist"


def test_pilot_flow_labels_every_field_it_asks_for() -> None:
    collect = next(step for step in PILOT_FLEX_FLOW.steps if step.kind is StepKind.COLLECT)
    assert set(collect.required_fields) <= set(collect.field_labels)
    assert collect.field_labels["applicant_type"] == "申请身份"


def test_pilot_flow_asks_for_no_document_numbers() -> None:
    collect = next(step for step in PILOT_FLEX_FLOW.steps if step.kind is StepKind.COLLECT)
    # 字段名与标签是"要收什么"的定义：出现证件号就是越界。
    declared = " ".join([*collect.required_fields, *collect.field_labels.values()])
    for forbidden in ("身份证号", "证件号", "身份证号码", "毕业证书编号"):
        assert forbidden not in declared
    # 提示语里要主动说明"不需要证件号码"，否则群众会主动填。这一句必须写成否定式，
    # 所以单独按上下文判断，而不是把整段当黑名单去扫。
    prompt = collect.prompt or ""
    assert "不需要提供任何证件号码" in prompt
    for name in ("身份证", "户口簿"):
        assert name not in declared


def test_pilot_execute_step_forbids_eligibility_judgements() -> None:
    instruction = _execute_step(PILOT_FLEX_FLOW).instruction or ""
    assert "不得判断群众是否符合申请条件" in instruction
    # 依据只能来自工具，不能凭记忆改写政策。
    assert "唯一的依据" in instruction
    assert _execute_step(PILOT_FLEX_FLOW).answer_label == "材料清单自查结果"


def test_pilot_intent_rules_point_at_this_version() -> None:
    for rule in PILOT_FLEX_INTENT_RULES:
        assert rule.flow_id == PILOT_FLEX_FLOW.flow_id
        assert rule.flow_version == PILOT_FLEX_FLOW.version


def test_pilot_flow_meets_the_sample_minimums() -> None:
    positives = [
        s for s in PILOT_FLEX_INTENT_SAMPLES if s.category is IntentSampleCategory.POSITIVE
    ]
    near_misses = [
        s for s in PILOT_FLEX_INTENT_SAMPLES if s.category is IntentSampleCategory.NEAR_MISS
    ]
    assert len(positives) >= MIN_POSITIVE_SAMPLES
    assert len(near_misses) >= MIN_NEAR_MISS_SAMPLES


def test_pilot_flow_passes_the_release_gate_against_the_demo_flow() -> None:
    """两个流程同时发布时，样例互不抢话。

    只测 pilot 自己是不够的：真正的失效是两个流程的规则在同一句话上打平，
    而那时门禁判的是"歧义"，不是"pilot 的样例没命中"。
    """

    router = RuleIntentRouter(rules=(*DEMO_INTENT_RULES, *PILOT_FLEX_INTENT_RULES))
    report = IntentEvalRunner().run([DEMO_FLOW, PILOT_FLEX_FLOW], router)
    assert report.status is IntentEvalStatus.PASS, report


def test_answer_label_is_refused_on_non_execute_steps() -> None:
    """收集步骤声明正文标签＝那一行永远空白。

    正文只由执行步骤产出，别的步骤声明了也没有人会往里写值。这类"机制在、没接线"
    必须保存时就拒掉，而不是等办结后才发现页面上少了一行。
    """

    flow = FlowDefinition(
        flow_id="test.answer-label",
        version="1",
        title="正文标签位置校验",
        steps=(
            StepDefinition(
                step_id="collect",
                kind=StepKind.COLLECT,
                title="收集",
                required_fields=("subject",),
                answer_label="办理结果",
            ),
            StepDefinition(
                step_id="run",
                kind=StepKind.EXECUTE,
                title="执行",
                tool="sys_clock",
            ),
        ),
    )
    errors = flow_field_errors(flow)
    assert FlowFieldError(
        loc=("steps", 0, "answer_label"),
        message="step 'collect' may only declare answer_label when executing",
    ) in errors


# ---------- 识别：一句话就命中 ----------


@pytest.mark.parametrize(
    "text",
    [
        "我想查一下灵活就业社保补贴要带什么材料",
        "灵活就业社会保险补贴申报材料有哪些",
        "灵活就业社保补贴需要哪些材料",
    ],
)
def test_pilot_intent_matches_from_one_sentence(text: str) -> None:
    router = RuleIntentRouter(rules=PILOT_FLEX_INTENT_RULES)
    match = router.route(text)
    assert match is not None
    assert match.flow_id == PILOT_FLEX_FLOW.flow_id
    assert match.flow_version == PILOT_FLEX_FLOW.version
    # 申请身份与已备材料不在识别阶段猜，交给收集步骤逐项问。
    assert match.slots == {}


@pytest.mark.parametrize(
    "text",
    ["灵活就业人员怎么参加养老保险", "我要申请失业保险金", "企业吸纳社保补贴怎么办"],
)
def test_neighbouring_topics_are_not_scooped_up(text: str) -> None:
    """相邻事项不许被捞进来。误路由的代价不是多问一句，而是话被塞进别的事件的参数里。"""

    router = RuleIntentRouter(rules=PILOT_FLEX_INTENT_RULES)
    assert router.route(text) is None


# ---------- 端到端：真执行器 + 真工具 + 办结页只给正文 ----------


def _answer_json(answer: str) -> str:
    return json.dumps(
        {
            "outcome": "succeeded",
            "summary": "已按指南核对材料清单",
            "answer": answer,
            "evidence": f"policy_checklist 返回：{render_topic(FLEX_SUBSIDY_TOPIC)}",
        },
        ensure_ascii=False,
    )


def test_pilot_flow_completes_and_shows_only_the_declared_answer() -> None:
    step = _execute_step(PILOT_FLEX_FLOW)
    answer = "需带：①身份证 ②户口簿 ③灵活就业人员社保补贴申请表；你未提到第 3 项。"
    registry, closers = default_registry()
    client = ScriptedModelClient(
        [
            ModelResponse(
                tool_calls=[
                    tool_call(
                        step.tool or "", call_id="c1", topic=FLEX_SUBSIDY_TOPIC
                    )
                ]
            ),
            ModelResponse(text=_answer_json(answer)),
        ]
    )
    container = build_container(
        Settings(store_backend="memory", enable_demo_flow=False, enable_pilot_flows=True),
        executor=HarnessExecutor(client, registry=registry),
    )
    subject = "session-pilot"
    try:
        task = container.tasks.create_task(
            TaskCreateRequest(
                conversation_id="conversation-pilot",
                input_text="灵活就业社保补贴需要哪些材料",
                flow_id=PILOT_FLEX_FLOW.flow_id,
                slots={
                    "applicant_type": "就业困难人员",
                    "prepared_materials": "身份证、户口簿",
                },
            ),
            subject,
        )
        assert task.status is TaskStatus.WAITING_FOR_CONFIRMATION

        task = container.tasks.confirm(
            task.task_id, TaskConfirmRequest(step_id=step.step_id), subject
        )

        assert task.status is TaskStatus.COMPLETED
        # 办结页只拿到作者声明的那一段正文，平台元数据一条都不出去。
        assert task.result == {step.answer_label: answer}
        # 工具**真的被调用了**，而且模型看到的工具清单只有这一个。
        assert client.calls[0]["tools"] == [step.tool]
        tool_messages = [
            m for m in client.calls[1]["messages"] if m.role is MessageRole.TOOL
        ]
        assert tool_messages and SOURCE_URL in tool_messages[0].content
        assert CHECKED_ON in tool_messages[0].content
    finally:
        container.close()
        for close in closers:
            close()


def test_pilot_flow_is_not_published_unless_switched_on() -> None:
    """试点事项默认不开：上线时间由运营决定，不由代码默认值代劳。"""

    off = build_container(Settings(store_backend="memory", enable_demo_flow=False))
    try:
        assert PILOT_FLEX_FLOW.flow_id not in {
            flow.flow_id for flow in off.flows.published()
        }
    finally:
        off.close()

    on = build_container(
        Settings(store_backend="memory", enable_demo_flow=False, enable_pilot_flows=True)
    )
    try:
        published = {flow.flow_id: flow for flow in on.flows.published()}
        assert PILOT_FLEX_FLOW.flow_id in published
        assert published[PILOT_FLEX_FLOW.flow_id].version == PILOT_FLEX_FLOW.version
    finally:
        on.close()
