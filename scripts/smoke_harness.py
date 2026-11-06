"""端到端冒烟：通用 harness 的真实模型验证。

与 `smoke_octop.py` 的区别：这里验证的是**工具循环**是否真的跑起来——
模型必须实际调用工具，evidence 必须能追溯到工具返回值，而不是靠模型自述。

用法：

    python scripts/smoke_harness.py

三个场景：
1. 步骤声明了 `sys_clock` → 模型必须真的调用它，成功且留痕；
2. 步骤声明的工具未注册 → 模型没有工具可用，只能如实报告，不能假装完成；
3. 模型口头声称成功但零工具调用 → 必须被降级为"未确认"（由单元测试守卫，
   这里只验证真实模型下不会出现"没调用却报成功"）。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from workpanda.bootstrap import Container, build_container  # noqa: E402
from workpanda.config import get_settings  # noqa: E402
from workpanda.domain.flow import FlowDefinition, FlowStatus, StepDefinition, StepKind  # noqa: E402
from workpanda.domain.models import (  # noqa: E402
    Task,
    TaskConfirmRequest,
    TaskCreateRequest,
    TaskStatus,
)

SUBJECT = "smoke-user"

CLOCK_FLOW = FlowDefinition(
    flow_id="smoke.clock",
    version="1",
    title="冒烟：必须调用工具",
    status=FlowStatus.PUBLISHED,
    steps=(
        StepDefinition(
            step_id="collect",
            kind=StepKind.COLLECT,
            title="收集说明",
            required_fields=("subject",),
            prompt="请补充说明。",
        ),
        StepDefinition(
            step_id="read_clock",
            kind=StepKind.EXECUTE,
            title="读取当前时间",
            tool="sys_clock",
            instruction=(
                "调用 sys_clock 获取当前时间，并在 evidence 里原样写出工具返回的时间字符串。"
            ),
            requires_confirmation=True,
            confirm_message="即将读取系统时间，请确认。",
        ),
    ),
)

NO_TOOL_FLOW = FlowDefinition(
    flow_id="smoke.no_tool",
    version="1",
    title="冒烟：工具不可用",
    status=FlowStatus.PUBLISHED,
    steps=(
        StepDefinition(
            step_id="collect",
            kind=StepKind.COLLECT,
            title="收集说明",
            required_fields=("subject",),
            prompt="请补充说明。",
        ),
        StepDefinition(
            step_id="submit_external",
            kind=StepKind.EXECUTE,
            title="提交到外部系统",
            # 该工具没有注册，harness 不会授权，模型只能如实报告无法完成。
            tool="external.portal.submit",
            instruction="调用外部系统提交申请，并返回受理编号。",
            requires_confirmation=True,
            confirm_message="即将提交，请确认。",
        ),
    ),
)


def run_flow(container: Container, flow_id: str) -> Task:
    task = container.tasks.create_task(
        TaskCreateRequest(
            conversation_id="smoke",
            input_text="冒烟测试",
            flow_id=flow_id,
            slots={"subject": "冒烟"},
        ),
        SUBJECT,
    )
    if task.status is TaskStatus.WAITING_FOR_CONFIRMATION:
        assert task.current_step is not None
        task = container.tasks.confirm(
            task.task_id,
            TaskConfirmRequest(step_id=task.current_step, accepted=True),
            SUBJECT,
        )
    return task


def main() -> int:
    settings = get_settings()
    if not settings.harness_enabled or not settings.model_configured:
        print("跳过：WORKPANDA_HARNESS_ENABLED 或模型配置未开启")
        return 2

    container = build_container(settings, flows=[CLOCK_FLOW, NO_TOOL_FLOW])
    failures: list[str] = []
    try:
        clock = run_flow(container, "smoke.clock")
        clock_view = _describe(container, clock)
        print(json.dumps(clock_view, ensure_ascii=False, indent=2))

        if clock.status is not TaskStatus.COMPLETED:
            failures.append(f"工具步骤未完成，实际状态 {clock.status}")
        calls = (clock.result or {}).get("tool_calls") or []
        if not any(call["name"] == "sys_clock" and call["outcome"] == "ok" for call in calls):
            failures.append("模型没有真正调用 sys_clock，成功缺少工具依据")
        evidence = (clock.result or {}).get("verification", {}).get("tool_evidence") or []
        if not evidence:
            failures.append("成功结果里没有 tool_evidence，无法追溯")

        blocked = run_flow(container, "smoke.no_tool")
        print(json.dumps(_describe(container, blocked), ensure_ascii=False, indent=2))
        if blocked.status is TaskStatus.COMPLETED:
            failures.append("工具不可用却报完成（虚假成功）")
        if blocked.status not in {TaskStatus.OUTCOME_UNCONFIRMED, TaskStatus.HANDOFF_REQUIRED}:
            failures.append(f"工具不可用时应转待确认或转人工，实际 {blocked.status}")
    finally:
        container.close()

    if failures:
        print("\n冒烟失败：")
        for item in failures:
            print(f" - {item}")
        return 1

    print("\n冒烟通过：工具循环真实执行，成功可追溯到工具返回值，失败不伪装成功。")
    return 0


def _describe(container: Container, task: Task) -> dict:
    result = task.result or {}
    trail = [
        {
            "type": event.type.value,
            "failure_code": event.data.get("failure_code"),
            "model_text": (event.data.get("model_text") or "")[:200],
        }
        for event in container.tasks.events(task.task_id, SUBJECT)
        if event.type.value in {"execution_failed", "outcome_unconfirmed", "handoff_required"}
    ]
    return {
        "flow_id": task.flow_id,
        "status": task.status.value,
        "failure_code": task.failure_code,
        "failure_message": task.failure_message,
        "stop_reason": result.get("stop_reason"),
        "model_calls": result.get("model_calls"),
        "tool_calls": [
            {
                "name": call["name"],
                "outcome": call["outcome"],
                "content": call["content"][:120],
            }
            for call in result.get("tool_calls") or []
        ],
        "evidence": result.get("verification", {}).get("evidence"),
        "failure_trail": trail,
    }


if __name__ == "__main__":
    raise SystemExit(main())
