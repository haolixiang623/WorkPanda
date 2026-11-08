"""回放与发布门禁的回归。

这些用例钉住四类结论，任何一类被改坏都会让"发布前检查"变成摆设：

1. 判定口径：通过 / 不符 / 用例无效 / 无结论 四种必须分得开；
2. 隔离：回放不经过生产执行器，也不真的调用工具；
3. 门禁：有用例且未通过时指针不许动，没有用例时门禁不假装拦得住；
4. 可重复：同一版本 + 同一用例跑两次，观测逐字节相同。
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from workpanda.application.release import FlowReleaseBlocked, FlowReleaseService
from workpanda.application.replay import FlowReplay
from workpanda.config import Settings
from workpanda.domain.flow import (
    FlowDefinition,
    FlowReleaseMissing,
    FlowStatus,
    StepDefinition,
    StepKind,
)
from workpanda.domain.models import TaskStatus
from workpanda.domain.replay import (
    ReplayCase,
    ReplayCaseExists,
    ReplayExpectation,
    ReplayFindingCode,
    ReplayToolCall,
    ReplayVerdict,
    StepOutcome,
    digest_arguments,
    evaluate,
)
from workpanda.infrastructure.replay_cases import (
    InMemoryReplayCaseStore,
    SqliteReplayCaseStore,
)
from workpanda.infrastructure.sqlite_flow_store import SqliteFlowStore

READ_TOOL = "replay_read"
SUBMIT_TOOL = "replay_submit"

READ_FLOW = FlowDefinition(
    flow_id="replay.read",
    version="2",
    title="回放：只读流程",
    status=FlowStatus.DRAFT,
    steps=(
        StepDefinition(
            step_id="collect",
            kind=StepKind.COLLECT,
            title="收集",
            required_fields=("subject",),
            prompt="请补充主题。",
        ),
        StepDefinition(
            step_id="lookup",
            kind=StepKind.EXECUTE,
            title="查询",
            tool=READ_TOOL,
        ),
    ),
)

SUBMIT_FLOW = FlowDefinition(
    flow_id="replay.submit",
    version="2",
    title="回放：带副作用的提交",
    status=FlowStatus.DRAFT,
    steps=(
        StepDefinition(
            step_id="collect",
            kind=StepKind.COLLECT,
            title="收集",
            required_fields=("subject",),
        ),
        StepDefinition(
            step_id="submit",
            kind=StepKind.EXECUTE,
            title="提交",
            tool=SUBMIT_TOOL,
            requires_confirmation=True,
            confirm_message="确认提交？",
        ),
    ),
)

TWO_CONFIRM_FLOW = FlowDefinition(
    flow_id="replay.two_confirm",
    version="1",
    title="回放：连续两次确认",
    status=FlowStatus.DRAFT,
    steps=(
        StepDefinition(
            step_id="first",
            kind=StepKind.EXECUTE,
            title="第一次提交",
            tool=READ_TOOL,
            requires_confirmation=True,
        ),
        StepDefinition(
            step_id="second",
            kind=StepKind.EXECUTE,
            title="第二次提交",
            tool=READ_TOOL,
            requires_confirmation=True,
        ),
    ),
)


def _read_replay(**kwargs) -> FlowReplay:
    """只读工具可安全模拟：回放里按成功记账，流程能走到终态。"""

    options = {"side_effecting": lambda name: name != READ_TOOL}
    options.update(kwargs)
    return FlowReplay(**options)


def _case(flow: FlowDefinition, **overrides) -> ReplayCase:
    payload = {
        "case_id": "happy",
        "flow_id": flow.flow_id,
        "flow_version": flow.version,
        "slots": {"subject": "自检"},
        "expect": ReplayExpectation(
            steps=tuple(step.step_id for step in flow.steps),
            tools=None,
            terminal=None,
        ),
    }
    payload.update(overrides)
    return ReplayCase(**payload)


# ---------- 判定口径 ----------


def test_read_only_flow_passes_with_per_step_conclusions() -> None:
    case = _case(
        READ_FLOW,
        expect=ReplayExpectation(
            steps=("collect", "lookup"), tools=(READ_TOOL,), terminal=None
        ),
    )
    result = _read_replay().run_case(READ_FLOW, case)

    assert result.verdict is ReplayVerdict.PASS
    assert result.findings == ()
    assert [step.step_id for step in result.steps] == ["collect", "lookup"]
    assert all(step.outcome is StepOutcome.COMPLETED for step in result.steps)
    assert result.terminal == "completed"
    # 只读工具被模拟成成功，但依然标注 replay：报告里不能出现"真实调用过"的错觉。
    assert result.tools[0].replay is True
    assert result.tools[0].side_effecting is False


def test_side_effecting_tool_is_recorded_as_unconfirmed() -> None:
    case = _case(
        SUBMIT_FLOW,
        expect=ReplayExpectation(
            steps=("collect", "submit"),
            tools=(SUBMIT_TOOL,),
            # 期望写的是"终态未确认"：提交类流程在回放里就该是这个结论。
            terminal=TaskStatus.OUTCOME_UNCONFIRMED,
        ),
    )
    result = FlowReplay().run_case(SUBMIT_FLOW, case)

    assert result.verdict is ReplayVerdict.PASS
    assert result.terminal == "outcome_unconfirmed"
    assert result.tools[0].side_effecting is True
    assert result.tools[0].replay is True


def test_step_after_confirmation_is_not_reported_as_paused() -> None:
    """确认过的步骤会被重新 `STEP_STARTED`，不能因为"出现过待确认事件"就永远显示暂停。

    这条是回归：早先按"有没有出现过暂停事件"判定，导致执行完但结果未确认的步骤
    一直在报告里写着 `paused`，运维会以为流程卡住了。
    """

    result = FlowReplay().run_case(SUBMIT_FLOW, _case(SUBMIT_FLOW))
    outcomes = {step.step_id: step.outcome for step in result.steps}

    assert outcomes["collect"] is StepOutcome.COMPLETED
    assert outcomes["submit"] is StepOutcome.INCOMPLETE
    assert result.terminal == "outcome_unconfirmed"


def test_terminal_mismatch_fails_and_is_not_a_pass() -> None:
    case = _case(SUBMIT_FLOW, expect=ReplayExpectation(terminal=TaskStatus.COMPLETED))
    result = FlowReplay().run_case(SUBMIT_FLOW, case)

    assert result.verdict is ReplayVerdict.FAIL
    assert ReplayFindingCode.TERMINAL_MISMATCH.value in result.codes


def test_missing_required_field_is_invalid_not_failed() -> None:
    case = _case(READ_FLOW, slots={})
    result = _read_replay().run_case(READ_FLOW, case)

    assert result.verdict is ReplayVerdict.INVALID
    assert result.codes == (ReplayFindingCode.CASE_MISSING_FIELDS.value,)
    # 停在收集阶段：这一步没走完，但不是流程缺陷。
    assert result.steps[0].outcome is StepOutcome.PAUSED


def test_case_bound_to_other_version_is_invalid() -> None:
    case = _case(READ_FLOW, flow_version="1")
    result = _read_replay().run_case(READ_FLOW, case)

    assert result.verdict is ReplayVerdict.INVALID
    assert result.codes == (ReplayFindingCode.CASE_FLOW_MISMATCH.value,)


def test_case_with_undeclared_field_is_invalid() -> None:
    case = _case(READ_FLOW, slots={"subject": "自检", "id_card": "不该出现在这里"})
    result = _read_replay().run_case(READ_FLOW, case)

    assert result.verdict is ReplayVerdict.INVALID
    assert result.codes == (ReplayFindingCode.CASE_UNKNOWN_FIELD.value,)


def test_step_assertions_distinguish_missing_extra_and_order() -> None:
    base = _read_replay().run_case(
        READ_FLOW,
        _case(READ_FLOW, expect=ReplayExpectation(steps=("collect", "lookup"))),
    )
    assert base.verdict is ReplayVerdict.PASS

    missing = _read_replay().run_case(
        READ_FLOW,
        _case(READ_FLOW, expect=ReplayExpectation(steps=("collect", "lookup", "audit"))),
    )
    assert missing.verdict is ReplayVerdict.FAIL
    assert ReplayFindingCode.STEP_MISSING.value in missing.codes

    extra = _read_replay().run_case(
        READ_FLOW, _case(READ_FLOW, expect=ReplayExpectation(steps=("collect",)))
    )
    assert extra.verdict is ReplayVerdict.FAIL
    assert ReplayFindingCode.STEP_UNEXPECTED.value in extra.codes

    reordered = _read_replay().run_case(
        READ_FLOW, _case(READ_FLOW, expect=ReplayExpectation(steps=("lookup", "collect")))
    )
    assert reordered.verdict is ReplayVerdict.FAIL
    assert ReplayFindingCode.STEP_ORDER_MISMATCH.value in reordered.codes


def test_tool_set_assertion_reports_missing_and_extra() -> None:
    extra = _read_replay().run_case(
        READ_FLOW, _case(READ_FLOW, expect=ReplayExpectation(tools=()))
    )
    assert extra.verdict is ReplayVerdict.FAIL
    assert ReplayFindingCode.TOOL_SET_MISMATCH.value in extra.codes

    missing = _read_replay().run_case(
        READ_FLOW, _case(READ_FLOW, expect=ReplayExpectation(tools=(READ_TOOL, "other")))
    )
    assert ReplayFindingCode.TOOL_SET_MISMATCH.value in missing.codes
    assert "other" in missing.findings[0].detail


def test_tool_call_outside_step_declaration_is_unauthorized() -> None:
    """断言器层面的负向：调用落在步骤声明之外，一律判未授权。"""

    findings = evaluate(
        READ_FLOW,
        _case(READ_FLOW, expect=ReplayExpectation()),
        steps=(),
        tools=(
            ReplayToolCall(
                step_id="lookup",
                name=SUBMIT_TOOL,
                fields=("subject",),
                argument_digest=digest_arguments({"subject": "自检"}),
            ),
        ),
        terminal=None,
    )

    assert [finding.code for finding in findings] == [ReplayFindingCode.UNAUTHORIZED_TOOL]


def test_flow_declaring_ungranted_tool_fails_before_running_anything() -> None:
    replay = FlowReplay(authorized_tools=frozenset())
    result = replay.run_case(READ_FLOW, _case(READ_FLOW))

    assert result.verdict is ReplayVerdict.FAIL
    assert result.codes == (ReplayFindingCode.UNAUTHORIZED_TOOL.value,)
    # 一步都没跑：拿模拟结果去证明"流程没问题"比不跑更危险。
    assert result.steps == ()
    assert result.tools == ()


# ---------- 隔离与无结论 ----------


def test_replay_never_reaches_production_executor() -> None:
    """给回放配一个真实的、会记录调用的执行器，它必须一次都没被调用。"""

    # 本例只关心"回放有没有借道生产执行器"，与识别样例门禁无关：`READ_FLOW` 在代码里
    # 没有意图规则，样例过不了门禁，会挡在回放之前。这里显式关掉那道门，被测行为不变。
    settings = Settings(store_backend="memory", enable_demo_flow=True, intent_sample_gate=False)
    from workpanda.bootstrap import build_container

    spy = _SpyExecutor()
    container = build_container(settings, executor=spy)
    try:
        assert container.flow_store is not None
        assert container.release is not None
        container.flow_store.add_version(READ_FLOW)
        container.replay_cases.add(_case(READ_FLOW))

        outcome = container.release.release(READ_FLOW.flow_id, READ_FLOW.version, actor="testsuite")

        assert outcome.published is True
        assert outcome.report is not None
        assert outcome.report.publishable is True
        # 关键断言：生产执行器零调用，回放完全走自己的隔离执行器。
        assert spy.calls == []
    finally:
        container.close()


def test_replay_timeout_is_unconfirmed() -> None:
    def slow(_: str) -> bool:
        import time

        time.sleep(0.3)
        return False

    result = FlowReplay(timeout_seconds=0.05, side_effecting=slow).run_case(
        READ_FLOW, _case(READ_FLOW)
    )

    assert result.verdict is ReplayVerdict.UNCONFIRMED
    assert result.codes == (ReplayFindingCode.REPLAY_TIMEOUT.value,)


def test_replay_crash_is_unconfirmed() -> None:
    def boom(_: str) -> bool:
        raise RuntimeError("boom")

    result = FlowReplay(side_effecting=boom).run_case(READ_FLOW, _case(READ_FLOW))

    assert result.verdict is ReplayVerdict.UNCONFIRMED
    assert result.codes == (ReplayFindingCode.REPLAY_CRASHED.value,)


def test_interrupted_case_is_aborted_and_not_a_release_basis() -> None:
    case = _case(TWO_CONFIRM_FLOW, slots={}, expect=ReplayExpectation())
    replay = _read_replay(max_confirmations=1)
    result = replay.run_case(TWO_CONFIRM_FLOW, case)

    assert result.verdict is ReplayVerdict.ABORTED
    report = replay.run_cases(TWO_CONFIRM_FLOW, [case])
    # 被中断的用例既不阻挡也不放行：没有通过的用例，就没有发布依据。
    assert report.blocking == ()
    assert report.passed == ()
    assert report.publishable is False


def test_replay_is_reproducible() -> None:
    case = _case(
        READ_FLOW,
        expect=ReplayExpectation(steps=("collect", "lookup"), tools=(READ_TOOL,)),
    )
    replay = _read_replay()
    first = replay.run_case(READ_FLOW, case)
    second = replay.run_case(READ_FLOW, case)

    assert first.verdict is second.verdict
    assert first.steps == second.steps
    assert first.tools == second.tools
    assert first.terminal == second.terminal
    # 参数摘要必须稳定：它不含时间、任务 ID 或随机量。
    assert first.tools[0].argument_digest == second.tools[0].argument_digest


# ---------- 发布门禁 ----------


def _release_service(
    flow_store, case_store, *, gate: bool = True, replay: FlowReplay | None = None
) -> FlowReleaseService:
    return FlowReleaseService(
        flows=flow_store,
        cases=case_store,
        replay=replay or _read_replay(),
        gate_enabled=gate,
    )


def test_release_without_cases_does_not_pretend_to_be_checked(workdir: Path) -> None:
    flows = _sqlite_flow_store(workdir)
    cases = InMemoryReplayCaseStore()
    flows.add_version(READ_FLOW)

    outcome = _release_service(flows, cases).release(
        READ_FLOW.flow_id, READ_FLOW.version, actor="testsuite"
    )

    assert outcome.published is True
    assert outcome.replay_ran is False
    assert flows.published_versions() == {READ_FLOW.flow_id: READ_FLOW.version}


def test_release_blocked_when_case_fails(workdir: Path) -> None:
    flows = _sqlite_flow_store(workdir)
    cases = InMemoryReplayCaseStore()
    flows.add_version(SUBMIT_FLOW)
    cases.add(_case(SUBMIT_FLOW, expect=ReplayExpectation(terminal=TaskStatus.COMPLETED)))

    with pytest.raises(FlowReleaseBlocked) as excinfo:
        _release_service(flows, cases).release(
            SUBMIT_FLOW.flow_id, SUBMIT_FLOW.version, actor="testsuite"
        )

    assert "replay.submit@2" in str(excinfo.value)
    assert excinfo.value.report.verdict is ReplayVerdict.FAIL
    # 门禁失败时指针不许动：不能出现"回放没过、但版本已经上线"。
    assert flows.published_versions() == {}


def test_release_publishes_when_cases_pass(workdir: Path) -> None:
    flows = _sqlite_flow_store(workdir)
    cases = InMemoryReplayCaseStore()
    flows.add_version(READ_FLOW)
    cases.add(
        _case(
            READ_FLOW,
            expect=ReplayExpectation(steps=("collect", "lookup"), tools=(READ_TOOL,)),
        )
    )

    outcome = _release_service(flows, cases).release(
        READ_FLOW.flow_id, READ_FLOW.version, actor="testsuite"
    )

    assert outcome.replay_ran is True
    assert outcome.report is not None
    assert outcome.report.publishable is True
    assert flows.published_versions() == {READ_FLOW.flow_id: READ_FLOW.version}


def test_dry_run_does_not_move_the_pointer(workdir: Path) -> None:
    flows = _sqlite_flow_store(workdir)
    cases = InMemoryReplayCaseStore()
    flows.add_version(READ_FLOW)
    cases.add(_case(READ_FLOW))

    outcome = _release_service(flows, cases).release(
        READ_FLOW.flow_id, READ_FLOW.version, apply=False, actor="testsuite")

    assert outcome.published is False
    assert outcome.report is not None
    assert flows.published_versions() == {}


def test_gate_disabled_skips_replay(workdir: Path) -> None:
    flows = _sqlite_flow_store(workdir)
    cases = InMemoryReplayCaseStore()
    flows.add_version(SUBMIT_FLOW)
    cases.add(_case(SUBMIT_FLOW, expect=ReplayExpectation(terminal=TaskStatus.COMPLETED)))

    outcome = _release_service(flows, cases, gate=False).release(
        SUBMIT_FLOW.flow_id, SUBMIT_FLOW.version, actor="testsuite")

    assert outcome.published is True
    assert outcome.replay_ran is False


def test_release_of_unknown_version_raises(workdir: Path) -> None:
    flows = _sqlite_flow_store(workdir)
    with pytest.raises(FlowReleaseMissing):
        _release_service(flows, InMemoryReplayCaseStore()).release(
            "replay.read", "9", actor="testsuite"
        )


# ---------- 用例存储 ----------


@pytest.mark.parametrize("backend", ["memory", "sqlite"])
def test_case_store_roundtrip_and_duplicate(backend, workdir: Path) -> None:
    store = _case_store(backend, workdir)
    try:
        case = _case(READ_FLOW)
        store.add(case)
        listed = store.list(READ_FLOW.flow_id, READ_FLOW.version)
        assert [item.case_id for item in listed] == ["happy"]
        assert listed[0].expect == case.expect

        with pytest.raises(ReplayCaseExists):
            store.add(case)

        assert store.delete(READ_FLOW.flow_id, READ_FLOW.version, "happy") is True
        assert store.delete(READ_FLOW.flow_id, READ_FLOW.version, "happy") is False
        assert store.list(READ_FLOW.flow_id, READ_FLOW.version) == []
    finally:
        store.close()


def test_cases_are_scoped_to_version(workdir: Path) -> None:
    store = _case_store("sqlite", workdir)
    try:
        store.add(_case(READ_FLOW))
        assert store.list(READ_FLOW.flow_id, "1") == []
    finally:
        store.close()


# ---------- 运维脚本 ----------


def test_replay_script_reports_and_exits_nonzero_on_block(workdir: Path) -> None:
    db = workdir / "replay.db"
    flows = SqliteFlowStore(db)
    cases = SqliteReplayCaseStore(db)
    try:
        flows.add_version(READ_FLOW)
        # 故意写错期望：必须让脚本以非零码退出，不能"跑了就算过"。
        cases.add(
            _case(
                READ_FLOW,
                expect=ReplayExpectation(steps=("collect", "lookup", "audit")),
            )
        )
    finally:
        cases.close()
        flows.close()

    script = Path(__file__).resolve().parents[1] / "scripts" / "replay_flow.py"
    completed = subprocess.run(
        [
            sys.executable,
            str(script),
            "--flow",
            READ_FLOW.flow_id,
            "--version",
            READ_FLOW.version,
            "--sqlite",
            str(db),
            "--side-effect-free",
            READ_TOOL,
        ],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )

    assert completed.returncode == 1, completed.stderr
    assert "FAIL" in completed.stdout
    assert "step_missing" in completed.stdout


def test_replay_script_adds_case_from_json(workdir: Path) -> None:
    db = workdir / "replay.db"
    flows = SqliteFlowStore(db)
    flows.add_version(READ_FLOW)
    flows.close()

    case_file = workdir / "happy.json"
    case_file.write_text(
        json.dumps(
            {
                "case_id": "happy",
                "slots": {"subject": "自检"},
                "expect": {"steps": ["collect", "lookup"], "tools": [READ_TOOL]},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    script = Path(__file__).resolve().parents[1] / "scripts" / "replay_flow.py"
    args = [
        sys.executable,
        str(script),
        "--flow",
        READ_FLOW.flow_id,
        "--version",
        READ_FLOW.version,
        "--sqlite",
        str(db),
    ]
    added = subprocess.run(
        [*args, "--add-case", str(case_file)],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert added.returncode == 0, added.stderr

    # `READ_FLOW` 在代码里没有意图规则，识别样例门禁必然拦下发布；本例验的是
    # "用例进库后回放能跑通"，所以只看回放结论（`--replay-only`，不作发布依据）。
    replayed = subprocess.run(
        [*args, "--side-effect-free", READ_TOOL, "--replay-only"],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert replayed.returncode == 0, replayed.stdout + replayed.stderr
    assert "PASS" in replayed.stdout

    store = SqliteReplayCaseStore(db)
    try:
        assert [case.case_id for case in store.list(READ_FLOW.flow_id, "2")] == ["happy"]
    finally:
        store.close()


# ---------- 辅助 ----------


class _SpyExecutor:
    """假的生产执行器：只记账，用来证明回放没有借道真实执行链路。"""

    def __init__(self) -> None:
        self.calls: list = []

    def execute(self, request):  # noqa: ANN001, ANN201 - 与 Executor 协议同形
        self.calls.append(request)
        raise AssertionError("回放不应调用生产执行器")


def _sqlite_flow_store(workdir: Path) -> SqliteFlowStore:
    """门禁用例需要一个真库：`release` 会从存储层重新读版本。"""

    return SqliteFlowStore(workdir / "flows.db")


def _case_store(backend: str, workdir: Path):
    if backend == "sqlite":
        return SqliteReplayCaseStore(workdir / "cases.db")
    return InMemoryReplayCaseStore()
