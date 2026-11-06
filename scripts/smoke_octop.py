"""端到端冒烟：用真实模型验证"模型有回复不等于业务成功"。

运行前需在 .env 里配置 Octop 运行时凭据：

    WORKPANDA_OCTOP_ENABLED=true
    WORKPANDA_OCTOP_BASE_URL=...
    WORKPANDA_OCTOP_API_KEY=...
    WORKPANDA_OCTOP_MODEL=...

用法：

    python scripts/smoke_octop.py

两个场景：
1. 可完成的步骤 → 必须是 completed，且带模型给出的 evidence；
2. 模型无法真正完成的步骤 → 必须落到 outcome_unconfirmed / handoff_required，
   绝不能是 completed。这是本项目最危险的一类缺陷，脚本用它做硬断言。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from workpanda.bootstrap import build_container  # noqa: E402
from workpanda.config import get_settings  # noqa: E402
from workpanda.domain.flow import FlowDefinition, FlowStatus, StepDefinition, StepKind  # noqa: E402
from workpanda.domain.models import (  # noqa: E402
    Task,
    TaskConfirmRequest,
    TaskCreateRequest,
    TaskStatus,
)

ECHO_FLOW = FlowDefinition(
    flow_id="smoke.echo",
    version="1",
    title="冒烟：模型可完成",
    status=FlowStatus.PUBLISHED,
    steps=(
        StepDefinition(
            step_id="collect_input",
            kind=StepKind.COLLECT,
            title="收集主题",
            required_fields=("subject",),
            prompt="请补充主题。",
        ),
        StepDefinition(
            step_id="echo",
            kind=StepKind.EXECUTE,
            title="回显",
            tool="model.echo",
            instruction="把参数 subject 的值原样返回，并在 evidence 中给出你实际返回的内容。",
            requires_confirmation=True,
            confirm_message="即将提交，请确认。",
        ),
    ),
)

BLOCKED_FLOW = FlowDefinition(
    flow_id="smoke.blocked",
    version="1",
    title="冒烟：模型无法完成",
    status=FlowStatus.PUBLISHED,
    steps=(
        StepDefinition(
            step_id="collect_input",
            kind=StepKind.COLLECT,
            title="收集主题",
            required_fields=("subject",),
            prompt="请补充主题。",
        ),
        StepDefinition(
            step_id="submit_external",
            kind=StepKind.EXECUTE,
            title="提交到外部系统",
            # 该工具在运行时并不存在，模型无法真正执行。
            tool="external.portal.submit",
            instruction="调用外部系统提交申请，并返回对方给出的受理编号。",
            requires_confirmation=True,
            confirm_message="即将提交，请确认。",
        ),
    ),
)


def run_flow(container, flow_id: str, subject_value: str) -> Task:
    subject = "smoke-user"
    request = TaskCreateRequest(
        conversation_id="smoke",
        input_text="冒烟测试",
        flow_id=flow_id,
        slots={"subject": subject_value},
    )
    task = container.tasks.create_task(request, subject)
    if task.status is TaskStatus.WAITING_FOR_CONFIRMATION:
        assert task.current_step is not None
        task = container.tasks.confirm(
            task.task_id,
            TaskConfirmRequest(step_id=task.current_step, accepted=True),
            subject,
        )
    return task


def main() -> int:
    settings = get_settings()
    if not settings.octop_enabled:
        print("跳过：WORKPANDA_OCTOP_ENABLED 未开启")
        return 2

    container = build_container(settings, flows=[ECHO_FLOW, BLOCKED_FLOW])
    failures: list[str] = []
    try:
        ok = run_flow(container, "smoke.echo", "回显内容")
        print(json.dumps(_describe(container, ok), ensure_ascii=False, indent=2))
        if ok.status is not TaskStatus.COMPLETED:
            failures.append(f"可完成步骤未完成，实际状态 {ok.status}")
        elif not (ok.result or {}).get("verification", {}).get("evidence"):
            failures.append("成功但没有 evidence，等于未经核验的成功")

        blocked = run_flow(container, "smoke.blocked", "不存在的工具")
        print(json.dumps(_describe(container, blocked), ensure_ascii=False, indent=2))
        if blocked.status is TaskStatus.COMPLETED:
            failures.append("模型无法完成的步骤被判成 completed（虚假成功）")
        if blocked.status not in {
            TaskStatus.OUTCOME_UNCONFIRMED,
            TaskStatus.HANDOFF_REQUIRED,
        }:
            failures.append(f"无法完成时应转待确认或转人工，实际状态 {blocked.status}")
        trail = container.tasks.events(blocked.task_id, "smoke-user")
        if not any(event.data.get("model_text") for event in trail):
            failures.append("失败时没有留下模型原始输出，无法复盘")
    finally:
        container.close()

    if failures:
        print("\n冒烟失败：")
        for item in failures:
            print(f" - {item}")
        return 1

    print("\n冒烟通过：成功有依据，无法完成不会被报成成功，且失败可追溯。")
    return 0


def _describe(container, task: Task) -> dict:
    result = task.result or {}
    trail = []
    for event in container.tasks.events(task.task_id, "smoke-user"):
        if event.type.value in {"execution_failed", "outcome_unconfirmed", "handoff_required"}:
            trail.append(
                {
                    "type": event.type.value,
                    "failure_code": event.data.get("failure_code"),
                    "model_text": (event.data.get("model_text") or "")[:200],
                }
            )
    return {
        "flow_id": task.flow_id,
        "status": task.status.value,
        "failure_code": task.failure_code,
        "failure_message": task.failure_message,
        "evidence": result.get("verification", {}).get("evidence"),
        "failure_trail": trail,
    }


if __name__ == "__main__":
    raise SystemExit(main())
