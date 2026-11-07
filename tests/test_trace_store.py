"""trace 外置存储：跨重启可查、聚合口径与内存实现一致。"""

from __future__ import annotations

import sqlite3
import uuid
from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from workpanda.bootstrap import build_container, resolve_trace_backend
from workpanda.config import Settings
from workpanda.harness.trace import (
    InMemoryTraceStore,
    TraceCursor,
    TraceQuery,
    TraceRecord,
    decode_cursor,
    encode_cursor,
    summarize,
)
from workpanda.infrastructure.sqlite_trace_store import SqliteTraceStore

BASE = datetime(2026, 11, 7, 4, 0, 0, tzinfo=UTC)
# 默认时间戳按 1 秒递增：用例于是不必依赖"两次 datetime.now() 是否刚好不同"。
# 真实时钟的粒度在多数平台上远粗于微秒，同一毫秒内的多条记录会撞时间戳，
# 排序就落到 trace_id 这个随机值上——那会让"新的在前"的用例变成随机通过。
_SEQUENCE = iter(range(100_000))


def record(
    *,
    outcome: str = "succeeded",
    trace_id: str | None = None,
    skipped_tool_calls: int = 0,
    created_at: datetime | None = None,
    stop_reason: str | None = None,
    risk: str = "low",
    task_id: uuid.UUID | None = None,
) -> TraceRecord:
    return TraceRecord(
        trace_id=trace_id or uuid.uuid4().hex,
        task_id=task_id or uuid.uuid4(),
        flow_id="test.flow",
        flow_version="1",
        step_id="run",
        outcome=outcome,
        stop_reason=stop_reason or ("completed" if outcome == "succeeded" else "model_error"),
        error_code=None if outcome == "succeeded" else "model_error",
        risk=risk,
        model_calls=2,
        tool_calls=3,
        successful_tool_calls=2,
        blocked_tool_calls=1,
        failed_tool_calls=0,
        timed_out_tool_calls=0,
        skipped_tool_calls=skipped_tool_calls,
        tokens=100,
        duration_ms=50,
        created_at=created_at or BASE + timedelta(seconds=next(_SEQUENCE)),
    )


@pytest.fixture(params=["memory", "sqlite"])
def store(request: pytest.FixtureRequest, workdir: Path) -> Iterator[object]:
    """同一组用例跑两套后端。

    筛选与分页是"两处实现一件事"的典型场景，只测一边等于没测——另一边漂移了
    也没人知道，而运维看到的数字会随部署的存储后端变化。
    """

    if request.param == "memory":
        yield InMemoryTraceStore(capacity=100)
        return
    backend = SqliteTraceStore(workdir / "traces.db", capacity=100)
    try:
        yield backend
    finally:
        backend.close()


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


def _walk(store, query: TraceQuery) -> list[str]:
    """按游标一路翻到底，返回 trace_id 顺序列表。"""

    seen: list[str] = []
    cursor = query.cursor
    for _ in range(200):  # 防呆：翻页不收敛就是缺陷，别让用例挂死
        page = store.query(replace(query, cursor=cursor))
        seen.extend(item.trace_id for item in page.items)
        if page.next_cursor is None:
            return seen
        cursor = decode_cursor(page.next_cursor)
    raise AssertionError("pagination did not terminate")


def test_query_filters_by_outcome_stop_reason_and_risk(store) -> None:
    ok = record(trace_id="ok", outcome="succeeded", stop_reason="completed")
    timed_out = record(trace_id="timeout", outcome="unconfirmed", stop_reason="deadline")
    errored = record(trace_id="error", outcome="unconfirmed", stop_reason="model_error")
    high_risk = record(trace_id="high", risk="high", stop_reason="completed")
    for item in (ok, timed_out, errored, high_risk):
        store.save(item)

    def ids(**kwargs) -> set[str]:
        return {item.trace_id for item in store.query(TraceQuery(**kwargs)).items}

    assert ids(outcome="unconfirmed") == {"timeout", "error"}
    assert ids(stop_reason="deadline") == {"timeout"}
    assert ids(risk="high") == {"high"}
    # 多个条件是与关系，不是或：把"超时且未确认"查成"超时或未确认"会放大事故规模。
    assert ids(outcome="unconfirmed", stop_reason="model_error") == {"error"}
    assert ids(outcome="failed") == set()


def test_time_window_is_half_open_so_adjacent_windows_tile_exactly(store) -> None:
    """`since <= t < until`：两个相邻窗口拼起来不重不漏。"""

    start = BASE
    middle = start + timedelta(hours=1)
    end = start + timedelta(hours=2)
    store.save(record(trace_id="before", created_at=start - timedelta(seconds=1)))
    store.save(record(trace_id="at-start", created_at=start))
    store.save(record(trace_id="inside", created_at=middle))
    store.save(record(trace_id="at-end", created_at=end))

    first = {
        item.trace_id
        for item in store.query(TraceQuery(since=start, until=end)).items
    }
    second = {item.trace_id for item in store.query(TraceQuery(since=end)).items}
    after_end = {
        item.trace_id for item in store.query(TraceQuery(until=start)).items
    }

    # 起点含、终点不含：at-start 归第一段，at-end 归第二段。
    assert first == {"at-start", "inside"}
    assert second == {"at-end"}
    assert after_end == {"before"}
    assert first & second == set()


def test_keyset_pagination_walks_every_record_once(store) -> None:
    expected = [record(trace_id=f"t{index:02d}") for index in range(7)]
    for item in expected:
        store.save(item)

    walked = _walk(store, TraceQuery(limit=2))

    # 逐条一致：不只是"条数对"，顺序也必须与一次性取全一致。
    assert walked == [item.trace_id for item in reversed(expected)]
    assert len(set(walked)) == len(walked)


def test_pagination_is_stable_when_timestamps_are_identical(store) -> None:
    """同一时刻的多条记录也必须有不重复、不遗漏的确定顺序。

    真实系统里同一毫秒写下多条是很常见的（一轮并发工具调用各一条），
    只用 created_at 做锚点会在这一毫秒上原地打转或者直接跳过。
    """

    same = BASE + timedelta(minutes=5)
    for index in range(4):
        store.save(record(trace_id=f"same-{index}", created_at=same))

    walked = _walk(store, TraceQuery(limit=1))

    assert sorted(walked) == [f"same-{index}" for index in range(4)]
    assert len(set(walked)) == 4


def test_pagination_does_not_repeat_after_new_writes(store) -> None:
    """翻页途中来了新记录，已翻过的不会再回到下一页。

    新记录比锚点更新，锚点法天然把它排除在后续页之外；offset 法则会因为
    位置整体后移而把刚看过的那一条再取一次——调用方看到两条一样的记录，
    通常会判断成"重复执行"。
    """

    for index in range(4):
        store.save(record(trace_id=f"old-{index}"))

    first = store.query(TraceQuery(limit=2))
    assert [item.trace_id for item in first.items] == ["old-3", "old-2"]
    assert first.next_cursor is not None

    store.save(record(trace_id="newest"))

    rest = _walk(store, TraceQuery(limit=2, cursor=decode_cursor(first.next_cursor)))

    assert rest == ["old-1", "old-0"]


def test_pagination_survives_eviction_between_pages(workdir: Path) -> None:
    """容量淘汰把旧记录删掉后，翻页仍然不重不漏。

    淘汰正是 offset 分页最致命的场景：删掉几条之后，后面的记录整体前移，
    同一个 offset 指到的是另一条记录。锚点法只关心"比它更旧"，与前移无关。
    """

    store = SqliteTraceStore(workdir / "traces.db", capacity=4)
    try:
        for index in range(4):
            store.save(record(trace_id=f"old-{index}"))

        first = store.query(TraceQuery(limit=2))
        assert [item.trace_id for item in first.items] == ["old-3", "old-2"]

        # 再写两条：容量 4，最旧的两条（old-0、old-1）被淘汰。
        store.save(record(trace_id="new-1"))
        store.save(record(trace_id="new-2"))

        rest = _walk(store, TraceQuery(limit=2, cursor=decode_cursor(first.next_cursor)))

        assert rest == []  # 剩下的都比锚点新，已经翻过或被淘汰
        assert "new-1" not in rest and "new-2" not in rest
    finally:
        store.close()


def test_query_summary_uses_the_same_filters_as_the_page(store) -> None:
    store.save(record(trace_id="ok", outcome="succeeded"))
    store.save(record(trace_id="bad", outcome="unconfirmed", stop_reason="deadline"))
    store.save(record(trace_id="bad2", outcome="unconfirmed", stop_reason="deadline"))
    store.save(record(trace_id="other", outcome="unconfirmed", stop_reason="model_error"))

    query = TraceQuery(stop_reason="deadline")
    page = store.query(query)
    summary = store.query_summary(query)

    assert len(page.items) == 2
    # 窗口内占比，不是全局占比：2/2 而不是 2/4。
    assert summary.total == 2
    assert summary.unconfirmed == 2
    assert summary.unconfirmed_rate == 1.0


def test_summary_ignores_the_pagination_cursor(store) -> None:
    """摘要描述整个窗口，不能因为"翻到第二页"就变小。"""

    for index in range(4):
        store.save(record(trace_id=f"t{index}", outcome="unconfirmed"))

    first = store.query(TraceQuery(limit=2))
    query = TraceQuery(limit=2, cursor=decode_cursor(first.next_cursor))

    assert store.query_summary(query).total == 4


def test_invalid_cursor_is_rejected_rather_than_silently_restarting() -> None:
    for broken in ("", "not-base64!!", "YWJj", encode_cursor(TraceCursor(BASE, "x")) + "z"):
        with pytest.raises(ValueError):
            decode_cursor(broken)


def test_cursor_roundtrips_timezone_aware_timestamps() -> None:
    shifted = BASE.astimezone().astimezone(UTC)  # 非 UTC 表达、同一时刻
    cursor = decode_cursor(encode_cursor(TraceCursor(created_at=shifted, trace_id="t1")))

    assert cursor.trace_id == "t1"
    # 规范到 UTC 再比较，避免 +08:00 的写法与 +00:00 的写法排成两个位置。
    assert cursor.stamp == TraceCursor(BASE, "t1").stamp


def test_legacy_timestamp_text_is_normalized_on_open(workdir: Path) -> None:
    """老库里 `created_at` 可能缺微秒位，启动时统一，否则翻页会在那一秒错位。"""

    path = workdir / "legacy.db"
    store = SqliteTraceStore(path)
    store.save(record(trace_id="seed"))
    store.close()

    conn = sqlite3.connect(path)
    # 模拟旧格式：微秒恰好为 0 时 isoformat 不带小数部分。
    conn.execute("UPDATE harness_traces SET created_at = '2026-11-07T04:00:00+00:00'")
    conn.commit()
    conn.close()

    reopened = SqliteTraceStore(path)
    try:
        stamp = reopened.query(TraceQuery(limit=1)).items[0].created_at_stamp
    finally:
        reopened.close()

    assert stamp == "2026-11-07T04:00:00.000000+00:00"


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
