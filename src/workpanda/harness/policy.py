"""内置政策口径：办事指南的确定性摘录，供执行步骤作为**证据**引用。

为什么单开一个模块，而不是让模型"凭知识回答"：

执行步骤的结论必须来自工具返回值（见 `harness/contract.py` 的三态契约）。政策类
事项如果让模型复述，最先失控的不是措辞而是**来源**——"我记得是这个材料"和"指南
上写的是这个材料"在输出里长得一样，事后没人能分辨。所以口径作为数据入库，由
`policy_checklist` 工具原样吐出，`evidence` 里带来源与核对日期，可追溯。

三条硬约束，改这个文件时不能破：

1. **只摘录，不改写。** 每一条都必须能在 `SOURCE_URL` 上找到对应原文。摘录时允许
   合并同类项（例如把"①身份证；②户口簿"拆成两条），不允许补充解释性判断。
2. **不判断资格。** 清单回答"要带什么"，不回答"你够不够条件"。后者是经办机构的
   核定权，写进工具返回值就成了系统在替政府部门下结论。
3. **来源与内容同处一文件。** 拆成两个文件后，"清单改了、来源没改"这种漂移没有任何
   机制能发现。核对日期与 URL 必须和正文挨着。

已知边界：这里是**内置**口径通道，改动需要发版。事项变多以后应当外置成可运维的
数据源，但那时仍要保住第 3 条——来源必须跟内容存在一起、一起发布。
"""

from __future__ import annotations

from typing import Any, Final

from workpanda.harness.models import RiskLevel, ToolSpec
from workpanda.harness.tools import ToolContext, ToolError

# 口径内容自身的版本号。内容一改就要跟着改，并在 `CHECKED_ON` 上重记核对日期；
# 两者一起动，才能回答"这句话是哪天、按哪一版指南说的"。
CATALOG_VERSION: Final[str] = "1"

# 苏州市人民政府 · 灵活就业登记服务页。核对日期是人工打开该页逐条比对的日期，
# 不是抓取程序运行的日期——后者会被误读成"内容自动保持最新"。
SOURCE_TITLE: Final[str] = "苏州市人民政府·灵活就业登记（灵活就业社保补贴申领）"
SOURCE_URL: Final[str] = "https://www.suzhou.gov.cn/szsrmzf/lhjydj/lhjydj.shtml?para=sjsx"
CHECKED_ON: Final[str] = "2026-11-09"

# 官方页面只给了上述服务页链接，**没有**为补贴条款标注文件号。不要在这里补一个
# "看起来对"的文号：编错的文号比没有文号更难被发现，也更难被纠正。
LEGAL_BASIS_NOTE: Final[str] = "该页未标注补贴条款的文件号，如需正式依据请向经办机构索取。"

FLEX_SUBSIDY_TOPIC: Final[str] = "flexible_employment_social_insurance_subsidy"

# 材料项：(编号, 名称, 仅适用于)。`None` 表示两种申请身份都要带。
#
# 编号沿用官方页面的 ①②③④，这样群众拿着输出去对照指南时能一眼对上；自己另起一套
# 编号会让"第 3 项"在两边指的是不同东西。
_MATERIALS: Final[tuple[tuple[str, str, str | None], ...]] = (
    ("①", "身份证", None),
    ("②", "户口簿", None),
    ("③", "灵活就业人员社保补贴申请表", None),
    ("④", "高校毕业生毕业证书", "离校两年内未就业高校毕业生"),
)

_APPLICANT_TYPES: Final[tuple[str, ...]] = (
    "就业困难人员",
    "离校两年内未就业高校毕业生",
)

_TOPIC_TEXT: Final[dict[str, str]] = {
    FLEX_SUBSIDY_TOPIC: "灵活就业人员社会保险补贴·申报材料自查",
}


def _render_materials() -> list[str]:
    lines = ["材料清单（编号沿用指南原文）："]
    for code, name, only_for in _MATERIALS:
        suffix = f"（仅{only_for}）" if only_for else ""
        lines.append(f"  {code} {name}{suffix}")
    return lines


def render_topic(topic: str) -> str:
    """把一条口径渲染成纯文本证据。

    输出必须是确定性的：同样的输入永远得到同样的字符串。不加时间戳、不加随机 id、
    不调用系统时钟——否则同一份材料两次自查会得到两份"不同"的证据，回放用例也就
    没法比对。
    """

    if topic not in _TOPIC_TEXT:
        available = "、".join(sorted(_TOPIC_TEXT))
        raise ToolError("unknown_topic", f"未收录的口径主题：{topic}。可用：{available}")

    lines = [
        f"口径主题：{_TOPIC_TEXT[topic]}",
        f"来源：{SOURCE_TITLE}",
        f"来源地址：{SOURCE_URL}",
        f"核对日期：{CHECKED_ON}（口径版本 {CATALOG_VERSION}）",
        "",
        f"申请对象：{'；'.join(_APPLICANT_TYPES)}",
        "",
        *_render_materials(),
        "",
        "补贴标准：按照实际缴纳的社会保险费给予 50% 的社保补贴。",
        (
            "补贴期限：一般最长不超过 3 年（以初次核定其享受社会保险补贴时年龄为准）；"
            "距法定退休年龄不足 5 年的可延长至退休、最长不超过 5 年；"
            "离校 2 年内未就业高校毕业生最长不超过 2 年。"
        ),
        "",
        "前置条件：若以就业困难人员身份申请，需先认定为“就业困难人员”。",
        (
            "互斥提醒：灵活就业社会保险补贴与失业保险待遇（失业金、失业补助金、"
            "失业保险支付的医保待遇等）不能同时享受。"
        ),
        "",
        (
            "办理渠道：江苏省人社网办大厅（选“苏州”地区）→ 就业创业 → 就业补贴 → "
            "灵活就业社保补贴申领；或江苏政务服务网苏州市站点“灵活就业人员社保补贴申领”页面。"
        ),
        "",
        LEGAL_BASIS_NOTE,
        "说明：以上为办事指南摘录，用于准备材料；是否符合条件由经办机构核定。",
    ]
    return "\n".join(lines)


class PolicyChecklistTool:
    """按主题返回办事指南摘录。只读、无参数副作用、不触网。

    刻意**不接受**任何个人信息入参：一旦参数里出现身份证号，它就会落到工具调用
    日志、模型请求与审计记录三处，而清单查询根本不需要它们。少一个入参，就少一条
    需要定留存期限的路径。
    """

    def __init__(self) -> None:
        self.spec = ToolSpec(
            name="policy_checklist",
            description=(
                "查询内置的办事指南摘录（申请对象、材料清单、标准、期限、前置条件、办理渠道）。"
                "只读、无副作用；返回内容自带来源地址与核对日期。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "topic": {
                        "type": "string",
                        "enum": sorted(_TOPIC_TEXT),
                        "description": "口径主题标识；取值见 enum，不要自行拼写。",
                    }
                },
                "required": ["topic"],
                "additionalProperties": False,
            },
            risk=RiskLevel.LOW,
            idempotent=True,
            timeout_seconds=2.0,
        )

    def invoke(self, arguments: dict[str, Any], context: ToolContext) -> str:
        topic = arguments.get("topic")
        if not isinstance(topic, str) or not topic:
            raise ToolError("invalid_arguments", "缺少参数 topic。")
        return render_topic(topic)


def topics() -> tuple[str, ...]:
    """已收录的主题标识。发布期核对与测试用。"""

    return tuple(sorted(_TOPIC_TEXT))
