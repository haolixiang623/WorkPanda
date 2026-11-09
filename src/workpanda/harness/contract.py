"""模型结果契约。

"模型有回复"不等于"业务成功"。任何执行步骤都必须返回结构化结论，
并且只有带可核验依据时才算成功——否则一律按"未确认"处理。

这段规则与执行器无关：直连模型、脚本化假模型都适用。
"""

from __future__ import annotations

import json
from typing import Any

from workpanda.application.ports import ExecutionOutcome

OUTCOME_CONTRACT = (
    "只执行该步骤，不要改变流程、不要调用未授权的工具、不要编造结果。\n"
    "必须只返回一个 JSON 对象，不要输出其他文字或代码块标记，字段如下：\n"
    '{"outcome": "succeeded|failed|unconfirmed",'
    ' "summary": "一句话说明做了什么",'
    ' "answer": "给群众看的正文（没有可说的就留空）",'
    ' "evidence": "判断该结果的依据，例如工具实际返回值或明确的外部响应"}\n'
    "answer 与 evidence 的区别：answer 是**结论本身**（群众要读的话），evidence 是"
    "**依据**（工具返回了什么、外部响应是什么）。二者不得互相顶替：不许把依据抄进 "
    "answer 当成结论，也不许用结论充当依据。answer 里不得出现 trace、步骤标识、"
    "模型自述等内部信息。\n"
    "无法真正完成该步骤时，outcome 必须为 failed 或 unconfirmed；"
    "没有可核验依据时不得填 succeeded。"
)

# `answer` 的长度上限。它是给群众读的正文，不是日志：超长既说明模型在堆砌，也会把
# 移动端页面撑成一堵墙。截断而不是拒收，因为"结论被截断"远好过"办完了却没结果"。
MAX_ANSWER_CHARS = 2000


def parse_contract(text: str) -> tuple[ExecutionOutcome, dict[str, Any]]:
    """解析模型返回的结构化契约。

    只有明确声明 succeeded 且给出可核验依据时才认为成功；
    解析失败、缺少依据或自述无法完成，一律按"未确认"处理，绝不默认成功。
    """

    payload = extract_json(text)
    if payload is None:
        return ExecutionOutcome.UNCONFIRMED, {
            "reason": "unparsable_output",
            "summary": text[:300],
        }

    raw_outcome = str(payload.get("outcome", "")).strip().lower()
    evidence = str(payload.get("evidence", "")).strip()
    summary = str(payload.get("summary", "")).strip()
    answer = str(payload.get("answer", "")).strip()[:MAX_ANSWER_CHARS]

    if raw_outcome == ExecutionOutcome.SUCCEEDED.value:
        if not evidence:
            return ExecutionOutcome.UNCONFIRMED, {
                "reason": "missing_evidence",
                "summary": summary,
            }
        return ExecutionOutcome.SUCCEEDED, {
            "summary": summary,
            "evidence": evidence,
            # 群众可见正文。允许为空：有些步骤（例如纯校验）本来就没有要给群众读的话，
            # 此时由流程决定是否退回 summary，而不是在这里编一句。
            "answer": answer,
        }

    if raw_outcome == ExecutionOutcome.FAILED.value:
        return ExecutionOutcome.FAILED, {"summary": summary, "evidence": evidence}

    return ExecutionOutcome.UNCONFIRMED, {
        "reason": raw_outcome or "unspecified_outcome",
        "summary": summary,
    }


def extract_json(text: str) -> dict[str, Any] | None:
    """从模型输出里取出**唯一一个** JSON 对象，容忍围栏与前导/尾随文字。

    以前用"第一个 { 到最后一个 }"整段 `json.loads`：模型只要多说一句话、
    加个示例或补一段解释，整段就解析失败，明明给了结论也变成 `unparsable_output`。
    现在按花括号逐个尝试 `raw_decode`，但严格保持一条不变式：

    **能解析出两个及以上 JSON 对象时一律返回 None。** 例如"这是示例 {...}，
    真正结果是 {...}"——此时无法判断哪个才是模型真正的结论，猜错就可能把
    `succeeded` 读进一个本该未确认的步骤。宁可转人工，不能猜。

    `raw_decode` 会一次吃掉整个对象（含嵌套），因此嵌套字段不会被重复计数；
    候选数量有上限，避免恶意/异常输出让解析变成 CPU 黑洞。
    """

    cleaned = _strip_fence(text)
    decoder = json.JSONDecoder()
    candidates: list[dict[str, Any]] = []
    cursor = 0
    while len(candidates) <= _MAX_JSON_CANDIDATES:
        start = cleaned.find("{", cursor)
        if start == -1:
            break
        try:
            parsed, end = decoder.raw_decode(cleaned, start)
        except json.JSONDecodeError:
            cursor = start + 1
            continue
        if isinstance(parsed, dict):
            candidates.append(parsed)
        cursor = max(end, start + 1)

    return candidates[0] if len(candidates) == 1 else None


_MAX_JSON_CANDIDATES = 20


def _strip_fence(text: str) -> str:
    """去掉一层 ``` 围栏；不是围栏的文本原样返回。"""

    cleaned = text.strip()
    if not cleaned.startswith("```"):
        return cleaned
    newline = cleaned.find("\n")
    cleaned = cleaned[newline + 1 :] if newline != -1 else cleaned.strip("`")
    closing = cleaned.rfind("```")
    if closing != -1:
        cleaned = cleaned[:closing]
    return cleaned.strip()
