"""模型结果契约。

"模型有回复"不等于"业务成功"。任何执行步骤都必须返回结构化结论，
并且只有带可核验依据时才算成功——否则一律按"未确认"处理。

这段规则与厂商无关：Octop 运行时、直连模型、脚本化假模型都适用。
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
    ' "evidence": "判断该结果的依据，例如工具实际返回值或明确的外部响应"}\n'
    "无法真正完成该步骤时，outcome 必须为 failed 或 unconfirmed；"
    "没有可核验依据时不得填 succeeded。"
)


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

    if raw_outcome == ExecutionOutcome.SUCCEEDED.value:
        if not evidence:
            return ExecutionOutcome.UNCONFIRMED, {
                "reason": "missing_evidence",
                "summary": summary,
            }
        return ExecutionOutcome.SUCCEEDED, {"summary": summary, "evidence": evidence}

    if raw_outcome == ExecutionOutcome.FAILED.value:
        return ExecutionOutcome.FAILED, {"summary": summary, "evidence": evidence}

    return ExecutionOutcome.UNCONFIRMED, {
        "reason": raw_outcome or "unspecified_outcome",
        "summary": summary,
    }


def extract_json(text: str) -> dict[str, Any] | None:
    """从模型输出里取出一个 JSON 对象，容忍 ```json 代码块围栏。"""

    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`")
        if cleaned.lstrip().lower().startswith("json"):
            cleaned = cleaned.lstrip()[4:]
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        parsed = json.loads(cleaned[start : end + 1])
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None
