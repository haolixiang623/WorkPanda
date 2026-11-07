"""执行留痕的 SQLite 实现。

进程内 trace 一重启就没了，只能回答"这次运行发生了什么"，回答不了
"上周三那批任务是不是都在超时"。这里把 trace 落到与任务库同一个文件：

- 检索与聚合走 SQL，不必把全部记录读进内存；
- 与任务数据同库，权限、备份、清理策略一致，不额外扩大数据面；
- 仍按容量上限淘汰旧记录——trace 用于排障，不承担审计归档职责。

生产多实例部署时应换成共享的关系库（同一张表结构即可迁移）。
"""

from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path
from threading import Lock
from uuid import UUID

from workpanda.harness.trace import (
    TraceCursor,
    TracePage,
    TraceQuery,
    TraceRecord,
    TraceSummary,
    canonical_stamp,
    encode_cursor,
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS harness_traces (
    trace_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    flow_id TEXT NOT NULL,
    flow_version TEXT NOT NULL,
    step_id TEXT NOT NULL,
    outcome TEXT NOT NULL,
    stop_reason TEXT NOT NULL,
    error_code TEXT,
    risk TEXT NOT NULL,
    model_calls INTEGER NOT NULL,
    tool_calls INTEGER NOT NULL,
    successful_tool_calls INTEGER NOT NULL,
    blocked_tool_calls INTEGER NOT NULL,
    failed_tool_calls INTEGER NOT NULL,
    timed_out_tool_calls INTEGER NOT NULL,
    skipped_tool_calls INTEGER NOT NULL DEFAULT 0,
    tokens INTEGER NOT NULL,
    duration_ms INTEGER NOT NULL,
    resumed_from TEXT,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_harness_traces_task
    ON harness_traces (task_id, created_at);
CREATE INDEX IF NOT EXISTS idx_harness_traces_created
    ON harness_traces (created_at);
-- 键集翻页按 (created_at, trace_id) 倒序走，这条索引让"比锚点更旧"不必全表扫。
CREATE INDEX IF NOT EXISTS idx_harness_traces_cursor
    ON harness_traces (created_at DESC, trace_id DESC);
"""

# 容量按 created_at 排序淘汰：同一秒内写入的多条用 rowid 兜底保证顺序稳定。
_PRUNE_SQL = """
DELETE FROM harness_traces WHERE trace_id IN (
    SELECT trace_id FROM harness_traces
    ORDER BY created_at DESC, rowid DESC
    LIMIT -1 OFFSET ?
)
"""

_SUMMARY_SQL = """
SELECT
    COUNT(*) AS total,
    COALESCE(SUM(outcome = 'succeeded'), 0) AS succeeded,
    COALESCE(SUM(outcome = 'failed'), 0) AS failed,
    COALESCE(SUM(tool_calls), 0) AS tool_calls,
    COALESCE(SUM(blocked_tool_calls), 0) AS blocked_tool_calls,
    COALESCE(SUM(failed_tool_calls), 0) AS failed_tool_calls,
    COALESCE(SUM(timed_out_tool_calls), 0) AS timed_out_tool_calls,
    COALESCE(SUM(skipped_tool_calls), 0) AS skipped_tool_calls,
    COALESCE(AVG(model_calls), 0) AS avg_model_calls,
    COALESCE(AVG(duration_ms), 0) AS avg_duration_ms,
    COALESCE(SUM(tokens), 0) AS tokens_total
FROM harness_traces
"""

_COLUMNS = (
    "trace_id, task_id, flow_id, flow_version, step_id, outcome, stop_reason, error_code,"
    " risk, model_calls, tool_calls, successful_tool_calls, blocked_tool_calls,"
    " failed_tool_calls, timed_out_tool_calls, skipped_tool_calls, tokens, duration_ms,"
    " resumed_from, created_at"
)


def _ensure_columns(conn: sqlite3.Connection) -> None:
    """老库补列。CREATE TABLE IF NOT EXISTS 不会修改已存在的表。"""

    existing = {row["name"] for row in conn.execute("PRAGMA table_info(harness_traces)")}
    if "resumed_from" not in existing:
        conn.execute("ALTER TABLE harness_traces ADD COLUMN resumed_from TEXT")
    if "skipped_tool_calls" not in existing:
        # 老记录里没有"被省下的调用"，按 0 处理；补列必须带 DEFAULT，
        # 否则 SQLite 无法给已有行填值。
        conn.execute(
            "ALTER TABLE harness_traces ADD COLUMN skipped_tool_calls INTEGER NOT NULL DEFAULT 0"
        )


def _normalize_timestamps(conn: sqlite3.Connection) -> None:
    """把 `created_at` 统一成可比较的文本。

    翻页靠字符串比较，而 `datetime.isoformat()` 的长度是变的：微秒恰好为 0 时是
    `...:26+00:00`，否则是 `...:26.000001+00:00`。两种写法在同一秒内比较，
    `'+'` 比 `'.'` 小，于是"同一时刻"会被排成两个位置——翻页恰好在那一刻经过时，
    要么漏一条、要么重复一条，而且不报错。老库里的行是旧格式，启动时改写一次。
    """

    rows = conn.execute("SELECT rowid AS rid, created_at FROM harness_traces").fetchall()
    for row in rows:
        current = row["created_at"]
        try:
            fixed = canonical_stamp(datetime.fromisoformat(current))
        except ValueError:
            # 读不出来的行保持原样：猜一个时间比留着异常值更危险。
            continue
        if fixed != current:
            conn.execute(
                "UPDATE harness_traces SET created_at = ? WHERE rowid = ?", (fixed, row["rid"])
            )


def _where(query: TraceQuery, *, with_cursor: bool) -> tuple[str, list[object]]:
    """把查询条件翻译成 WHERE。`with_cursor=False` 用于摘要：只要窗口，不要页位置。

    这里是内存后端 `matches()` 的 SQL 翻译，两边必须给出同样的集合；
    `tests/test_trace_store.py` 里的跨后端一致性用例盯着这件事。
    """

    clauses: list[str] = []
    params: list[object] = []
    if query.task_id is not None:
        clauses.append("task_id = ?")
        params.append(str(query.task_id))
    if query.outcome is not None:
        clauses.append("outcome = ?")
        params.append(query.outcome)
    if query.stop_reason is not None:
        clauses.append("stop_reason = ?")
        params.append(query.stop_reason)
    if query.risk is not None:
        clauses.append("risk = ?")
        params.append(query.risk)
    if query.since is not None:
        clauses.append("created_at >= ?")
        params.append(canonical_stamp(query.since))
    if query.until is not None:
        clauses.append("created_at < ?")
        params.append(canonical_stamp(query.until))
    if with_cursor and query.cursor is not None:
        # 严格"比锚点更旧"：时间更早，或同一时刻但 trace_id 更小。
        clauses.append("(created_at < ? OR (created_at = ? AND trace_id < ?))")
        anchor = query.cursor.stamp
        params.extend([anchor, anchor, query.cursor.trace_id])
    return (" WHERE " + " AND ".join(clauses)) if clauses else "", params


class SqliteTraceStore:
    """TraceStore 的 SQLite 实现，与任务库共用同一个文件。"""

    def __init__(self, path: str | Path, *, capacity: int = 500) -> None:
        if capacity < 0:
            raise ValueError("capacity must be >= 0")
        self.path = Path(path)
        if self.path.parent and str(self.path.parent) not in {"", "."}:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._capacity = capacity
        self._lock = Lock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        # 与任务库共用文件时，短暂写冲突应等待而不是直接抛 locked。
        self._conn.execute("PRAGMA busy_timeout = 3000")
        self._conn.executescript(SCHEMA)
        _ensure_columns(self._conn)
        _normalize_timestamps(self._conn)
        self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def save(self, record: TraceRecord) -> None:
        with self._lock:
            self._conn.execute(
                f"INSERT OR REPLACE INTO harness_traces ({_COLUMNS})"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    record.trace_id,
                    str(record.task_id),
                    record.flow_id,
                    record.flow_version,
                    record.step_id,
                    record.outcome,
                    record.stop_reason,
                    record.error_code,
                    record.risk,
                    record.model_calls,
                    record.tool_calls,
                    record.successful_tool_calls,
                    record.blocked_tool_calls,
                    record.failed_tool_calls,
                    record.timed_out_tool_calls,
                    record.skipped_tool_calls,
                    record.tokens,
                    record.duration_ms,
                    record.resumed_from,
                    record.created_at_stamp,
                ),
            )
            if self._capacity:
                self._conn.execute(_PRUNE_SQL, (self._capacity,))
            self._conn.commit()

    def list(self, *, task_id: UUID | None = None, limit: int = 50) -> list[TraceRecord]:
        return self.query(TraceQuery(task_id=task_id, limit=limit)).items

    def query(self, query: TraceQuery) -> TracePage:
        where, params = _where(query, with_cursor=True)
        limit = max(0, query.limit)
        # 多取一条用来判断"后面还有没有"。这条不会返回给调用方，
        # 只是避免再发一次 COUNT：多一次查询就多一个"两次结果不一致"的窗口。
        sql = (
            f"SELECT {_COLUMNS} FROM harness_traces{where}"
            " ORDER BY created_at DESC, trace_id DESC LIMIT ?"
        )
        with self._lock:
            rows = self._conn.execute(sql, [*params, limit + 1]).fetchall()

        items = [self._to_record(row) for row in rows[:limit]]
        next_cursor = None
        if len(rows) > limit and items:
            last = items[-1]
            next_cursor = encode_cursor(
                TraceCursor(created_at=last.created_at, trace_id=last.trace_id)
            )
        return TracePage(items=items, next_cursor=next_cursor)

    def summary(self, *, task_id: UUID | None = None) -> TraceSummary:
        return self.query_summary(TraceQuery(task_id=task_id))

    def query_summary(self, query: TraceQuery) -> TraceSummary:
        # 摘要覆盖整个过滤窗口，不受翻到第几页影响，因此不带游标。
        where, params = _where(query, with_cursor=False)
        with self._lock:
            row = self._conn.execute(_SUMMARY_SQL + where, params).fetchone()

        total = int(row["total"]) if row is not None else 0
        if total == 0:
            return TraceSummary()

        succeeded = int(row["succeeded"])
        failed = int(row["failed"])
        unconfirmed = total - succeeded - failed
        return TraceSummary(
            total=total,
            succeeded=succeeded,
            failed=failed,
            unconfirmed=unconfirmed,
            # 与内存实现保持同一口径，否则两套后端给出的健康指标不可比。
            unconfirmed_rate=round(unconfirmed / total, 4),
            tool_calls=int(row["tool_calls"]),
            blocked_tool_calls=int(row["blocked_tool_calls"]),
            failed_tool_calls=int(row["failed_tool_calls"]),
            timed_out_tool_calls=int(row["timed_out_tool_calls"]),
            skipped_tool_calls=int(row["skipped_tool_calls"]),
            avg_model_calls=round(float(row["avg_model_calls"]), 2),
            avg_duration_ms=round(float(row["avg_duration_ms"]), 1),
            tokens_total=int(row["tokens_total"]),
        )

    def __len__(self) -> int:
        with self._lock:
            row = self._conn.execute("SELECT COUNT(*) AS total FROM harness_traces").fetchone()
        return int(row["total"]) if row is not None else 0

    @staticmethod
    def _to_record(row: sqlite3.Row) -> TraceRecord:
        return TraceRecord(
            trace_id=row["trace_id"],
            task_id=UUID(row["task_id"]),
            flow_id=row["flow_id"],
            flow_version=row["flow_version"],
            step_id=row["step_id"],
            outcome=row["outcome"],
            stop_reason=row["stop_reason"],
            error_code=row["error_code"],
            risk=row["risk"],
            model_calls=int(row["model_calls"]),
            tool_calls=int(row["tool_calls"]),
            successful_tool_calls=int(row["successful_tool_calls"]),
            blocked_tool_calls=int(row["blocked_tool_calls"]),
            failed_tool_calls=int(row["failed_tool_calls"]),
            timed_out_tool_calls=int(row["timed_out_tool_calls"]),
            skipped_tool_calls=int(row["skipped_tool_calls"]),
            tokens=int(row["tokens"]),
            duration_ms=int(row["duration_ms"]),
            resumed_from=row["resumed_from"],
            created_at=datetime.fromisoformat(row["created_at"]),
        )
