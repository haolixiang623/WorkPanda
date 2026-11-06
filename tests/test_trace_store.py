"""trace 外置存储：跨重启可查、聚合口径与内存实现一致。"""

from __future__ import annotations

import sqlite3
import uuid
from pathlib import Path

import pytest

from workpanda.bootstrap import build_container, resolve_trace_backend
from workpanda.config import Settings
from workpanda.harness.trace import InMemoryTraceStore, TraceRecord, summarize
from workpanda.infrastructure.sqlite_trace_store import SqliteTraceStore


def record(
    *, outcome: str = "succeeded", trace_id: str | None = None, skipped_tool_calls: int = 0
) -> TraceRecord:
    return TraceRecord(
        trace_id=trace_id or uuid.uuid4().hex,
        task_id=uuid.uuid4(),
        flow_id="test.flow",
        flow_version="1",
        step_id="run",
        outcome=outcome,
        stop_reason="completed" if outcome == "succeeded" else "model_error",
        error_code=None if outcome == "succeeded" else "model_error",
        model_calls=2,
        tool_calls=3,
        successful_tool_calls=2,
        blocked_tool_calls=1,
        failed_tool_calls=0,
        timed_out_tool_calls=0,
        skipped_tool_calls=skipped_tool_calls,
        tokens=100,
        duration_ms=50,
    )


def test_records_survive_reopen(workdir: Path) -> None:
    path = workdir / "traces.db"
    store = SqliteTraceStore(path)
    saved = record(trace_id="persisted")
    store.save(saved)
    store.close()

    reopened = SqliteTraceStore(path)
    try:
        items = reopened.list()
    finally:
        reopened.close()

    assert [item.trace_id for item in items] == ["persisted"]
    assert items[0].task_id == saved.task_id
    assert items[0].blocked_tool_calls == 1


def test_list_is_newest_first_and_filters_by_task(workdir: Path) -> None:
    store = SqliteTraceStore(workdir / "traces.db")
    try:
        mine = record(trace_id="mine")
        other = record(trace_id="other")
        store.save(mine)
        store.save(other)

        assert [item.trace_id for item in store.list()] == ["other", "mine"]
        assert [item.trace_id for item in store.list(task_id=mine.task_id)] == ["mine"]
        assert store.list(limit=1)[0].trace_id == "other"
    finally:
        store.close()


def test_summary_matches_in_memory_implementation(workdir: Path) -> None:
    records = [record(), record(outcome="failed"), record(outcome="unconfirmed")]

    memory = InMemoryTraceStore()
    sqlite_store = SqliteTraceStore(workdir / "traces.db")
    try:
        for item in records:
            memory.save(item)
            sqlite_store.save(item)
        from_sql = sqlite_store.summary()
    finally:
        sqlite_store.close()

    expected = summarize(records)
    assert from_sql.model_dump() == memory.summary().model_dump()
    assert from_sql.total == expected.total == 3
    assert from_sql.unconfirmed_rate == expected.unconfirmed_rate


def test_empty_summary_is_zero(workdir: Path) -> None:
    store = SqliteTraceStore(workdir / "traces.db")
    try:
        summary = store.summary()
    finally:
        store.close()

    assert summary.total == 0
    assert summary.unconfirmed_rate == 0.0


def test_capacity_prunes_oldest(workdir: Path) -> None:
    store = SqliteTraceStore(workdir / "traces.db", capacity=2)
    try:
        for index in range(4):
            store.save(record(trace_id=f"t{index}"))

        assert len(store) == 2
        assert [item.trace_id for item in store.list()] == ["t3", "t2"]
    finally:
        store.close()


def test_zero_capacity_keeps_everything(workdir: Path) -> None:
    store = SqliteTraceStore(workdir / "traces.db", capacity=0)
    try:
        for index in range(3):
            store.save(record(trace_id=f"t{index}"))

        assert len(store) == 3
    finally:
        store.close()


def test_existing_table_is_migrated_for_resume_lineage(workdir: Path) -> None:
    """老库没有 resumed_from 列，升级后必须自动补列而不是启动即失败。"""

    path = workdir / "old.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE harness_traces (
            trace_id TEXT PRIMARY KEY, task_id TEXT NOT NULL, flow_id TEXT NOT NULL,
            flow_version TEXT NOT NULL, step_id TEXT NOT NULL, outcome TEXT NOT NULL,
            stop_reason TEXT NOT NULL, error_code TEXT, risk TEXT NOT NULL,
            model_calls INTEGER NOT NULL, tool_calls INTEGER NOT NULL,
            successful_tool_calls INTEGER NOT NULL, blocked_tool_calls INTEGER NOT NULL,
            failed_tool_calls INTEGER NOT NULL, timed_out_tool_calls INTEGER NOT NULL,
            tokens INTEGER NOT NULL, duration_ms INTEGER NOT NULL, created_at TEXT NOT NULL
        );
        """
    )
    conn.commit()
    conn.close()

    store = SqliteTraceStore(path)
    try:
        store.save(record(trace_id="after-migration"))
        items = store.list()
    finally:
        store.close()

    assert [item.trace_id for item in items] == ["after-migration"]
    assert items[0].resumed_from is None
    # 同一套补列机制也要覆盖新增的 skipped_tool_calls，老行按 0 算。
    assert items[0].skipped_tool_calls == 0


def test_skipped_calls_are_persisted_and_aggregated(workdir: Path) -> None:
    """被省下的调用要能跨重启检索，且 SQL 与内存聚合口径一致。"""

    records = [record(), record(skipped_tool_calls=2, trace_id="with-skips")]
    memory = InMemoryTraceStore()
    store = SqliteTraceStore(workdir / "traces.db")
    try:
        for item in records:
            memory.save(item)
            store.save(item)
        from_sql = store.summary()
        saved = store.list(limit=1)[0]
    finally:
        store.close()

    assert saved.skipped_tool_calls == 2
    assert from_sql.skipped_tool_calls == 2
    assert from_sql.model_dump() == memory.summary().model_dump()
    # 只有真正发起过的调用计数，跳过的不能混进"调了几次"。
    assert from_sql.tool_calls == 6


def test_trace_backend_resolution() -> None:
    assert resolve_trace_backend(Settings(store_backend="memory")) == "memory"
    assert resolve_trace_backend(Settings(store_backend="sqlite")) == "sqlite"
    assert (
        resolve_trace_backend(Settings(store_backend="sqlite", harness_trace_backend="memory"))
        == "memory"
    )
    with pytest.raises(ValueError):
        resolve_trace_backend(Settings(harness_trace_backend="postgres"))


def test_container_wires_trace_store_to_selected_backend(workdir: Path) -> None:
    sqlite_container = build_container(
        Settings(
            store_backend="sqlite",
            sqlite_path=workdir / "app.db",
            enable_demo_flow=False,
        )
    )
    memory_container = build_container(
        Settings(store_backend="memory", enable_demo_flow=False)
    )
    try:
        assert isinstance(sqlite_container.traces, SqliteTraceStore)
        assert isinstance(memory_container.traces, InMemoryTraceStore)
    finally:
        sqlite_container.close()
        memory_container.close()
