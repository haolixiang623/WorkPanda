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

from workpanda.harness.trace import TraceRecord, TraceSummary

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
    tokens INTEGER NOT NULL,
    duration_ms INTEGER NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_harness_traces_task
    ON harness_traces (task_id, created_at);
CREATE INDEX IF NOT EXISTS idx_harness_traces_created
    ON harness_traces (created_at);
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
    COALESCE(AVG(model_calls), 0) AS avg_model_calls,
    COALESCE(AVG(duration_ms), 0) AS avg_duration_ms,
    COALESCE(SUM(tokens), 0) AS tokens_total
FROM harness_traces
"""

_COLUMNS = (
    "trace_id, task_id, flow_id, flow_version, step_id, outcome, stop_reason, error_code,"
    " risk, model_calls, tool_calls, successful_tool_calls, blocked_tool_calls,"
    " failed_tool_calls, timed_out_tool_calls, tokens, duration_ms, created_at"
)


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
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def save(self, record: TraceRecord) -> None:
        with self._lock:
            self._conn.execute(
                f"INSERT OR REPLACE INTO harness_traces ({_COLUMNS})"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
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
                    record.tokens,
                    record.duration_ms,
                    record.created_at.isoformat(),
                ),
            )
            if self._capacity:
                self._conn.execute(_PRUNE_SQL, (self._capacity,))
            self._conn.commit()

    def list(self, *, task_id: UUID | None = None, limit: int = 50) -> list[TraceRecord]:
        query = f"SELECT {_COLUMNS} FROM harness_traces"
        params: list[object] = []
        if task_id is not None:
            query += " WHERE task_id = ?"
            params.append(str(task_id))
        query += " ORDER BY created_at DESC, rowid DESC LIMIT ?"
        params.append(max(0, limit))
        with self._lock:
            rows = self._conn.execute(query, params).fetchall()
        return [self._to_record(row) for row in rows]

    def summary(self, *, task_id: UUID | None = None) -> TraceSummary:
        query = _SUMMARY_SQL
        params: list[object] = []
        if task_id is not None:
            query += " WHERE task_id = ?"
            params.append(str(task_id))
        with self._lock:
            row = self._conn.execute(query, params).fetchone()

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
            tokens=int(row["tokens"]),
            duration_ms=int(row["duration_ms"]),
            created_at=datetime.fromisoformat(row["created_at"]),
        )
