"""副作用账本：只有真正改动外部世界的调用才入账，且"结果未知"必须能被识别出来。"""

from __future__ import annotations

import uuid

from workpanda.harness.effects import (
    EffectStatus,
    build_effects,
    fingerprint_of,
    summarize_effects,
)
from workpanda.harness.models import RiskLevel, ToolCallRecord, ToolOutcome, ToolSpec

TASK_ID = uuid.uuid4()

SUBMIT_SPEC = ToolSpec(
    name="test_submit",
    description="提交单据",
    parameters={"type": "object", "properties": {"value": {"type": "string"}}},
    idempotent=False,
    risk=RiskLevel.HIGH,
)

LOOKUP_SPEC = ToolSpec(
    name="test_lookup",
    description="查询单据",
    parameters={"type": "object", "properties": {"order_id": {"type": "string"}}},
    idempotent=True,
)


def record(
    *,
    name: str = "test_submit",
    outcome: ToolOutcome = ToolOutcome.OK,
    arguments: dict | None = None,
    error_code: str | None = None,
) -> ToolCallRecord:
    return ToolCallRecord(
        call_id=f"call-{uuid.uuid4().hex[:6]}",
        name=name,
        arguments=arguments if arguments is not None else {"value": "x"},
        outcome=outcome,
        content="submitted",
        error_code=error_code,
        risk=RiskLevel.HIGH if name == "test_submit" else RiskLevel.LOW,
    )


def build(records: list[ToolCallRecord], specs: dict[str, ToolSpec] | None = None):
    return build_effects(
        records,
        specs=specs if specs is not None else {"test_submit": SUBMIT_SPEC},
        trace_id="trace-1",
        task_id=TASK_ID,
        flow_id="test.flow",
        flow_version="1",
        step_id="submit",
    )


def test_successful_non_idempotent_call_is_a_known_effect() -> None:
    effects = build([record()])

    assert len(effects) == 1
    effect = effects[0]
    assert effect.status is EffectStatus.APPLIED
    assert effect.needs_manual_check is False
    # 风险等级随记录带出来：高风险动作在归档里必须一眼可见。
    assert effect.risk is RiskLevel.HIGH
    assert effect.trace_id == "trace-1"
    assert effect.step_id == "submit"


def test_timed_out_non_idempotent_call_needs_manual_check() -> None:
    effects = build([record(outcome=ToolOutcome.TIMEOUT, error_code="tool_timeout")])

    assert len(effects) == 1
    assert effects[0].status is EffectStatus.UNKNOWN
    assert effects[0].needs_manual_check is True
    assert effects[0].error_code == "tool_timeout"


def test_idempotent_tool_never_counts_as_an_effect_even_when_it_times_out() -> None:
    specs = {"test_lookup": LOOKUP_SPEC}
    records = [
        record(name="test_lookup", outcome=ToolOutcome.OK, arguments={"order_id": "1"}),
        record(name="test_lookup", outcome=ToolOutcome.TIMEOUT, arguments={"order_id": "2"}),
    ]

    assert build(records, specs) == []


def test_blocked_and_failed_calls_are_not_effects() -> None:
    records = [
        record(outcome=ToolOutcome.BLOCKED, error_code="duplicate_side_effect"),
        record(outcome=ToolOutcome.ERROR, error_code="upstream_rejected"),
    ]

    assert build(records) == []


def test_skipped_calls_are_not_effects() -> None:
    """被预算/取消省下的非幂等调用确定没发生过，不能让人工再去核对一次。"""

    records = [
        record(outcome=ToolOutcome.SKIPPED, error_code="budget_max_tool_calls"),
        record(outcome=ToolOutcome.SKIPPED, error_code="cancelled"),
    ]

    assert build(records) == []
    assert summarize_effects(build(records)).requires_manual_check is False


def test_crashed_non_idempotent_call_is_unknown_not_absent() -> None:
    """工具没自报就崩了：请求可能已送达外部，必须按"结果未知"入账。

    典型根因是连接重置、响应体解析失败——这些恰恰发生在请求已经发出去之后。
    把它当成"确定没执行"，事后就再也没人知道那笔提交到底成没成。
    """

    effects = build([record(outcome=ToolOutcome.ERROR, error_code="tool_crashed")])

    assert len(effects) == 1
    assert effects[0].status is EffectStatus.UNKNOWN
    assert effects[0].needs_manual_check is True
    assert effects[0].error_code == "tool_crashed"


def test_crashed_idempotent_call_is_still_not_an_effect() -> None:
    """查询类工具崩了也不改变外部状态，不能拿它去烦人工核对。"""

    specs = {"test_lookup": LOOKUP_SPEC}
    records = [record(name="test_lookup", outcome=ToolOutcome.ERROR, error_code="tool_crashed")]

    assert build(records, specs) == []


def test_self_reported_failure_is_still_absent() -> None:
    """工具自己明确回话"我没成"，那才是确定没执行——与崩溃必须区分开。"""

    effects = build([record(outcome=ToolOutcome.ERROR, error_code="upstream_rejected")])

    assert effects == []


def test_unknown_tool_is_treated_as_side_effecting() -> None:
    # 登记表里查不到这个工具（例如热更新后注册表落后），宁可多一条待核对，
    # 也不能把一次真实提交当成"查询"漏掉。
    effects = build([record(name="mystery_tool", arguments={"a": 1})], {})

    assert len(effects) == 1
    assert effects[0].status is EffectStatus.APPLIED


def test_fingerprint_is_stable_and_does_not_leak_arguments() -> None:
    first = fingerprint_of("test_submit", {"b": 2, "a": 1})
    second = fingerprint_of("test_submit", {"a": 1, "b": 2})
    other = fingerprint_of("test_submit", {"a": 1, "b": 3})

    # 键序不同的同一动作必须算出同一个指纹，否则防重复副作用会失效。
    assert first == second
    assert first != other
    assert len(first) == 16
    # 指纹里不能出现参数原文，它会被写进只追加的审计文件。
    assert "test_submit" not in first


def test_summary_counts_and_flags_manual_check() -> None:
    effects = build([record(), record(outcome=ToolOutcome.TIMEOUT, arguments={"value": "y"})])

    summary = summarize_effects(effects)

    assert summary.applied == 1
    assert summary.unknown == 1
    assert summary.requires_manual_check is True
    assert summarize_effects([]).requires_manual_check is False
