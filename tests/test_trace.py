"""trace 独立存储：有界保留、倒序检索、聚合指标。"""

from __future__ import annotations

import uuid

from workpanda.harness.trace import InMemoryTraceStore, TraceRecord, summarize


def record(
    *, outcome: str = "succeeded", trace_id: str | None = None, tool_calls: int = 1
) -> TraceRecord:
    return TraceRecord(
        trace_id=trace_id or uuid.uuid4().hex,
        task_id=uuid.uuid4(),
        flow_id="test.flow",
        flow_version="1",
        step_id="run",
        outcome=outcome,
        stop_reason="completed" if outcome == "succeeded" else "model_error",
        model_calls=2,
        tool_calls=tool_calls,
        successful_tool_calls=tool_calls,
        blocked_tool_calls=0,
        failed_tool_calls=0,
        tokens=100,
        duration_ms=50,
    )


def test_store_returns_newest_first() -> None:
    store = InMemoryTraceStore()
    first, second = record(trace_id="a"), record(trace_id="b")

    store.save(first)
    store.save(second)

    assert [item.trace_id for item in store.list()] == ["b", "a"]


def test_store_is_bounded() -> None:
    store = InMemoryTraceStore(capacity=2)
    for index in range(5):
        store.save(record(trace_id=f"t{index}"))

    assert len(store) == 2
    assert [item.trace_id for item in store.list()] == ["t4", "t3"]


def test_store_filters_by_task() -> None:
    store = InMemoryTraceStore()
    mine = record(trace_id="mine")
    other = record(trace_id="other")
    store.save(mine)
    store.save(other)

    found = store.list(task_id=mine.task_id)

    assert [item.trace_id for item in found] == ["mine"]


def test_summary_counts_outcomes_and_rates() -> None:
    summary = summarize([record(), record(outcome="failed"), record(outcome="unconfirmed")])

    assert summary.total == 3
    assert summary.succeeded == 1
    assert summary.failed == 1
    assert summary.unconfirmed == 1
    assert summary.unconfirmed_rate == 0.3333
    assert summary.avg_model_calls == 2
    assert summary.tokens_total == 300


def test_empty_summary_is_all_zero() -> None:
    summary = InMemoryTraceStore().summary()

    assert summary.total == 0
    assert summary.unconfirmed_rate == 0.0
