"""首个真实试点事项：灵活就业社保补贴·材料清单自查。

**它做什么**：按群众说的申请身份和手上已有的材料，把办事指南上的材料清单摆出来，
逐项指出"指南要求、但你没提到"的那几项。

**它刻意不做什么**（这三条比做什么更重要）：

1. **不判断资格。** "你符合申请条件"是经办机构的核定权。系统说这句话，等于替政府
   部门下了结论；说错了，群众会带着错误预期去办事。输出里只允许出现"由经办机构
   核定"。
2. **不收集任何证件信息。** 清单自查不需要身份证号、毕业证书编号。少收一项，就少
   一条要定留存期限、脱敏策略和删除流程的路径。
3. **不自己复述政策。** 清单由 `policy_checklist` 工具原样吐出，`evidence` 带来源
   地址与核对日期。模型只能引用，不能凭记忆改写（见 `harness/policy.py`）。

第二件容易漏的事写进了执行指令：官方页面明确"若以就业困难人员身份申请，需先认定为
就业困难人员"，以及"灵活就业社会保险补贴与失业保险待遇不能同时享受"。这两条不在
材料清单里，但会让群众白跑一趟或领了补贴又被追回，所以必须在结论里主动提。
"""

from typing import Final

from workpanda.domain.flow import FlowDefinition, FlowStatus, StepDefinition, StepKind
from workpanda.domain.intent import IntentRule
from workpanda.domain.intent_sample import IntentSample, IntentSampleCategory
from workpanda.harness.policy import FLEX_SUBSIDY_TOPIC

PILOT_FLEX_FLOW_ID: Final[str] = "pilot.flex_subsidy_checklist"
PILOT_FLEX_FLOW_VERSION: Final[str] = "1"

# 意图样例。三条正例覆盖三种常见说法；两条近似误例都**含"灵活就业"但不含"社保补贴"**
# ——它们是最可能被宽关键词误捞的一类话，专门用来证明关键词没有放宽。
#
# 全部为合成话术，不含任何真实群众信息：样例随版本入库，还会进评测报告与 CI 日志。
PILOT_FLEX_INTENT_SAMPLES: tuple[IntentSample, ...] = (
    IntentSample(
        sample_id="pos_materials_ask",
        category=IntentSampleCategory.POSITIVE,
        text="我想查一下灵活就业社保补贴要带什么材料",
    ),
    IntentSample(
        sample_id="pos_materials_formal",
        category=IntentSampleCategory.POSITIVE,
        text="灵活就业社会保险补贴申报材料有哪些",
    ),
    IntentSample(
        sample_id="pos_materials_short",
        category=IntentSampleCategory.POSITIVE,
        text="灵活就业社保补贴需要哪些材料",
    ),
    IntentSample(
        sample_id="near_miss_pension",
        category=IntentSampleCategory.NEAR_MISS,
        text="灵活就业人员怎么参加养老保险",
    ),
    IntentSample(
        sample_id="near_miss_unemployment_benefit",
        category=IntentSampleCategory.NEAR_MISS,
        text="我要申请失业保险金",
    ),
    IntentSample(
        sample_id="oos_weather",
        category=IntentSampleCategory.OUT_OF_SCOPE,
        text="今天天气怎么样",
    ),
)

# 关键词取的是**完整事项名**，不是"社保补贴"这种半截词。
#
# 理由：路由器只做关键词计数，没有"并且"语义。用"社保补贴"当关键词，"企业吸纳社保
# 补贴""失业保险金"这类相邻事项都会被捞进本流程——而误路由的代价不是多问一句，是
# 群众的话被塞进另一个事项的执行参数里。
PILOT_FLEX_INTENT_RULES: tuple[IntentRule, ...] = (
    IntentRule(
        intent_id="pilot.flex_subsidy_checklist.materials",
        flow_id=PILOT_FLEX_FLOW_ID,
        # 必须指向**本版**：规则随版本走，发布这一版等于同时换掉识别口径（#73）。
        flow_version=PILOT_FLEX_FLOW_VERSION,
        keywords=("灵活就业社保补贴", "灵活就业社会保险补贴"),
        # 这两个槽位不在这里抽：申请身份是二选一，已备材料是自由列举，用正则从一句话
        # 里抠出来必然抠错（"我是高校毕业生，有身份证"里"有"字后面跟什么？）。交给
        # 收集步骤逐项问，比猜一个值写进执行参数安全。
        slot_patterns={},
    ),
)

PILOT_FLEX_FLOW = FlowDefinition(
    flow_id=PILOT_FLEX_FLOW_ID,
    version=PILOT_FLEX_FLOW_VERSION,
    title="灵活就业社保补贴·材料清单自查",
    status=FlowStatus.PUBLISHED,
    steps=(
        StepDefinition(
            step_id="collect_application",
            kind=StepKind.COLLECT,
            title="告知申请身份与已备材料",
            required_fields=("applicant_type", "prepared_materials"),
            prompt=(
                "请告诉我两件事：你打算以哪种身份申请（就业困难人员 / 离校两年内未就业"
                "高校毕业生），以及手上已经有哪些材料。不需要提供任何证件号码。"
            ),
            field_labels={
                "applicant_type": "申请身份",
                "prepared_materials": "已准备的证明材料",
            },
        ),
        StepDefinition(
            step_id="lookup_checklist",
            kind=StepKind.EXECUTE,
            title="对照材料清单",
            # 必须与装配层真的注册过的工具名一致，否则授权交集为空（见 harness/preflight.py）。
            tool="policy_checklist",
            instruction=(
                "第一步：调用 policy_checklist 工具，参数 topic 必须是 "
                f"{FLEX_SUBSIDY_TOPIC}，不要自己拼写主题名。"
                "第二步：把工具返回的清单原文写进 evidence，这是本步骤唯一的依据；"
                "不要凭记忆补充指南以外的材料，也不要改动编号。"
                "第三步：在 answer 里按三段写："
                "① 指南要求的材料清单（照抄编号与名称，注明哪几项仅高校毕业生需要）；"
                "② 对照参数 prepared_materials 中群众提到已备的材料，逐项说明哪几项没提到；"
                "③ 提醒两件容易漏的事：以就业困难人员身份申请前需先完成“就业困难人员”"
                "认定；该补贴与失业保险待遇（失业金等）不能同时享受。"
                "不得判断群众是否符合申请条件，也不要替经办机构下结论；"
                "凡涉及符合与否，只能表述为“由经办机构核定”。"
            ),
            requires_confirmation=True,
            confirm_message="即将按你填写的身份与已备材料查询材料清单，请确认。",
            # 办结页只显示这一段；平台元数据（trace、工具调用）不会跟着出去。
            answer_label="材料清单自查结果",
        ),
    ),
    intent_samples=PILOT_FLEX_INTENT_SAMPLES,
    intent_rules=PILOT_FLEX_INTENT_RULES,
)


def pilot_flows() -> list[FlowDefinition]:
    return [PILOT_FLEX_FLOW]
