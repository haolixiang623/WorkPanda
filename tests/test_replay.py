"""流程回放：不碰真实系统、走真实推进链路、缺脚本不假装成功。

这组用例守的是三件容易在"测试工具"里悄悄退化的事：

1. 回放**真的**没有外部副作用（执行器手里没有工具注册表）；
2. 回放走的是真实状态机与确认凭证，不是另一条简化路径；
3. 用例没覆盖的地方判"不完整"，而不是判"通过"。
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

from workpanda.application.ports import ExecutionOutcome, ExecutionRequest
from workpanda.application.replay import (
    ReplayCase,
    ReplayExecutor,
    ReplayToolResult,
    build_report,
)
from workpanda.bootstrap import build_replayer, replay_ports
from workpanda.config import Settings
from workpanda.domain.flow import FlowDefinition, FlowStatus, StepDefinition, StepKind
from workpanda.domain.models import Task, TaskStatus
from workpanda.infrastructure.sqlite_flow_store import SqliteFlowStore

# `scripts/` 不是包（与 `test_issue_claim.py` 一致，用 importlib 直接加载）。
_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "replay_flow.py"
_spec = importlib.util.spec_from_file_location("workpanda_replay_flow", _SCRIPT)
replay_flow = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = replay_flow
_spec.loader.exec_module(replay_flow)

FLOW_ID = "replay.test"


def make_flow(*, tool: str = "demo_tool", needs_confirm: bool = True) -> FlowDefinition:
    return FlowDefinition(
        flow_id=FLOW_ID,
        version="1",
        title="回放测试流程",
        status=FlowStatus.DRAFT,
        steps=(
            StepDefinition(
                step_id="collect_input",
                kind=StepKind.COLLECT,
                title="收集信息",
                required_fields=("subject",),
            ),
            StepDefinition(
                step_id="do_it",
                kind=StepKind.EXECUTE,
                title="执行",
                tool=tool,
                instruction="按 subject 执行",
                requires_confirmation=needs_confirm,
            ),
        ),
    )


def make_case(**overrides: object) -> ReplayCase:
    base: dict[str, object] = {
        "case_id": "case.1",
        "flow_id": FLOW_ID,
        "flow_version": "1",
        "inputs": {"subject": "测试主题"},
        "tool_results": (
            ReplayToolResult(tool="demo_tool", output={"echo": "测试主题"}),
        ),
    }
    base.update(overrides)
    return ReplayCase.model_validate(base)


def run(flow: FlowDefinition, case: ReplayCase):
    store, confirmations, publisher = replay_ports()
    return build_replayer(Settings(store_backend="memory")).replay(
        flow,
        case,
        store=store,
        confirmations=confirmations,
        publisher=publisher,
    )


# ---------- 正常路径 ----------


def test_replay_walks_the_whole_flow_without_touching_real_systems() -> None:
    report = run(
        make_flow(),
        make_case(
            expect_steps=("collect_input", "do_it"),
            expect_tools=("demo_tool",),
            expect_outcome=ExecutionOutcome.SUCCEEDED,
            expect_status=TaskStatus.COMPLETED,
            expect_confirmations=1,
        ),
    )

    assert report.passed
    assert report.final_status is TaskStatus.COMPLETED
    assert report.observed_steps == ["collect_input", "do_it"]
    # 确认节点真的走了一遍：说明回放没有绕过状态机。
    assert report.confirmations_accepted == 1


def test_replay_reports_every_check_it_ran() -> None:
    report = run(
        make_flow(),
        make_case(expect_steps=("collect_input", "do_it")),
    )

    names = {check.name for check in report.checks}
    # 这两个检查永远在：不写期望也要守住"只用声明的工具""不许缺脚本"。
    assert {"tools_declared", "tools_scripted"} <= names


# ---------- 不完整 ≠ 通过 ----------


def test_unscripted_tool_is_not_treated_as_success() -> None:
    """用例没给脚本时，回放必须判"不完整"，不能编造一次成功。"""

    report = run(make_flow(), make_case(tool_results=()))

    assert not report.passed
    assert report.unscripted_tools == ["demo_tool"]
    assert report.incomplete_reason is not None
    assert "demo_tool" in report.incomplete_reason
    assert report.observed_outcome is ExecutionOutcome.UNCONFIRMED


def test_missing_field_stops_at_collect_and_is_incomplete() -> None:
    report = run(make_flow(), make_case(inputs={}))

    assert not report.passed
    assert report.final_status is TaskStatus.WAITING_FOR_USER
    assert report.incomplete_reason is not None
    assert "subject" in report.incomplete_reason


def test_answers_can_fill_fields_over_multiple_rounds() -> None:
    report = run(
        make_flow(),
        make_case(
            inputs={},
            answers=({"subject": "补充的主题"},),
            expect_steps=("collect_input", "do_it"),
        ),
    )

    assert report.passed
    assert report.final_status is TaskStatus.COMPLETED


# ---------- 断言真的会失败 ----------


def test_expectation_mismatch_fails_the_check() -> None:
    report = run(make_flow(), make_case(expect_steps=("collect_input",)))

    assert not report.passed
    failed = [check for check in report.checks if not check.passed]
    assert [check.name for check in failed] == ["steps"]
    assert "do_it" in failed[0].detail


def test_unconfirmed_result_lands_on_the_unconfirmed_state() -> None:
    report = run(
        make_flow(),
        make_case(
            tool_results=(
                ReplayToolResult(
                    tool="demo_tool",
                    outcome=ExecutionOutcome.UNCONFIRMED,
                    error_code="timed_out",
                ),
            ),
            expect_outcome=ExecutionOutcome.UNCONFIRMED,
            expect_status=TaskStatus.OUTCOME_UNCONFIRMED,
        ),
    )

    assert report.passed
    assert report.final_status is TaskStatus.OUTCOME_UNCONFIRMED


def test_failure_goes_to_handoff_and_is_not_reported_as_success() -> None:
    report = run(
        make_flow(),
        make_case(
            tool_results=(
                ReplayToolResult(tool="demo_tool", outcome=ExecutionOutcome.FAILED),
            ),
            expect_outcome=ExecutionOutcome.FAILED,
            expect_status=TaskStatus.HANDOFF_REQUIRED,
        ),
    )

    assert report.passed
    assert report.final_status is TaskStatus.HANDOFF_REQUIRED


# ---------- 未授权工具 ----------


def test_calls_outside_the_flow_declaration_are_flagged() -> None:
    """执行器被喂了流程没声明的工具时，报告必须拦下来。

    这条走的是 `build_report` 而不是 `replay`：真实链路上 `FlowRunner` 只会用
    声明过的工具，所以只有直接构造观测结果才能覆盖到这道兜底。
    """

    flow = make_flow()
    case = make_case()
    executor = ReplayExecutor(case.tool_results)
    executor.execute(
        ExecutionRequest(
            task_id=Task(conversation_id="c", flow_id=FLOW_ID, flow_version="1").task_id,
            flow_id=FLOW_ID,
            flow_version="1",
            step_id="do_it",
            tool="undeclared_tool",
        )
    )
    task = Task(conversation_id="c", flow_id=FLOW_ID, flow_version="1")

    report = build_report(flow, case, task, executor, confirmations_accepted=0)

    declared = next(check for check in report.checks if check.name == "tools_declared")
    assert not declared.passed
    assert "undeclared_tool" in declared.detail


# ---------- 不移动真实发布指针 ----------


def test_replaying_a_draft_does_not_publish_it(workdir: Path) -> None:
    store = SqliteFlowStore(workdir / "replay.db")
    try:
        store.add_version(make_flow())

        report = run(make_flow(), make_case(expect_status=TaskStatus.COMPLETED))

        assert report.passed
        assert store.published_versions() == {}
    finally:
        store.close()


# ---------- 命令行 ----------


def test_cli_runs_the_sample_case_against_a_real_flow_library(
    workdir: Path, tmp_path: Path
) -> None:
    db = workdir / "cli.db"
    store = SqliteFlowStore(db)
    try:
        store.add_version(_demo_style_flow())
        store.publish("demo.replay", "1")
    finally:
        store.close()

    case_path = tmp_path / "case.json"
    case_path.write_text(
        json.dumps(
            {
                "case_id": "cli.case",
                "flow_id": "demo.replay",
                "flow_version": "1",
                "inputs": {"subject": "主题"},
                "tool_results": [{"tool": "echo_tool", "output": {"echo": "主题"}}],
                "expect_steps": ["collect_input", "do_it"],
                "expect_status": "completed",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    assert replay_flow.main([str(case_path), "--sqlite-path", str(db)]) == 0


def test_cli_returns_2_when_the_case_points_at_a_missing_version(
    workdir: Path, tmp_path: Path
) -> None:
    db = workdir / "cli-missing.db"
    case_path = tmp_path / "case.json"
    case_path.write_text(
        json.dumps(
            {"case_id": "x", "flow_id": "nope", "flow_version": "1"},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    assert replay_flow.main([str(case_path), "--sqlite-path", str(db)]) == 2


def _demo_style_flow() -> FlowDefinition:
    return FlowDefinition(
        flow_id="demo.replay",
        version="1",
        title="命令行回放",
        status=FlowStatus.DRAFT,
        steps=(
            StepDefinition(
                step_id="collect_input",
                kind=StepKind.COLLECT,
                title="收集信息",
                required_fields=("subject",),
            ),
            StepDefinition(
                step_id="do_it",
                kind=StepKind.EXECUTE,
                title="执行",
                tool="echo_tool",
            ),
        ),
    )
