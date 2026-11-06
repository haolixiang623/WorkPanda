"""SQLite 持久化实现，用于本地开发和单机试点。

生产环境应替换为 PostgreSQL 等具备并发控制、备份和审计能力的存储。
两种实现共享同一组协议，业务层不感知存储细节。
"""

import json
import sqlite3
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path
from threading import Lock
from typing import Any
from uuid import UUID

from workpanda.domain.confirmation import Confirmation, ConfirmationError
from workpanda.domain.models import EventType, Task, TaskEvent, TaskStatus, utc_now
from workpanda.domain.repository import TaskNotFoundError

SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    task_id TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL,
    user_ref TEXT,
    flow_id TEXT NOT NULL,
    flow_version TEXT NOT NULL,
    status TEXT NOT NULL,
    current_step TEXT,
    completed_steps TEXT NOT NULL,
    slots TEXT NOT NULL,
    pending_fields TEXT NOT NULL,
    result TEXT,
    failure_code TEXT,
    failure_message TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS task_events (
    task_id TEXT NOT NULL,
    sequence INTEGER NOT NULL,
    event_id TEXT NOT NULL,
    type TEXT NOT NULL,
    status TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    data TEXT NOT NULL,
    PRIMARY KEY (task_id, sequence)
);

CREATE TABLE IF NOT EXISTS confirmations (
    token TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    step_id TEXT NOT NULL,
    flow_version TEXT NOT NULL,
    parameter_digest TEXT NOT NULL,
    user_ref TEXT,
    issued_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    consumed_at TEXT
);
"""


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _dt(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


class SqliteStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        if self.path.parent and str(self.path.parent) not in {"", "."}:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = Lock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ---------- 任务 ----------

    def create(self, task: Task) -> Task:
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO tasks (
                    task_id, conversation_id, user_ref, flow_id, flow_version, status,
                    current_step, completed_steps, slots, pending_fields, result,
                    failure_code, failure_message, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                self._row_values(task),
            )
            self._conn.commit()
        return task

    def get(self, task_id: UUID) -> Task:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM tasks WHERE task_id = ?", (str(task_id),)
            ).fetchone()
        if row is None:
            raise TaskNotFoundError(str(task_id))
        return self._to_task(row)

    def save(self, task: Task) -> Task:
        with self._lock:
            cursor = self._conn.execute(
                """
                UPDATE tasks SET
                    conversation_id = ?, user_ref = ?, flow_id = ?, flow_version = ?,
                    status = ?, current_step = ?, completed_steps = ?, slots = ?,
                    pending_fields = ?, result = ?, failure_code = ?, failure_message = ?,
                    created_at = ?, updated_at = ?
                WHERE task_id = ?
                """,
                (
                    task.conversation_id,
                    task.user_ref,
                    task.flow_id,
                    task.flow_version,
                    task.status.value,
                    task.current_step,
                    json.dumps(task.completed_steps),
                    json.dumps(task.slots),
                    json.dumps(task.pending_fields),
                    json.dumps(task.result) if task.result is not None else None,
                    task.failure_code,
                    task.failure_message,
                    _iso(task.created_at),
                    _iso(task.updated_at),
                    str(task.task_id),
                ),
            )
            if cursor.rowcount == 0:
                self._conn.rollback()
                raise TaskNotFoundError(str(task.task_id))
            self._conn.commit()
        return task

    def append_event(
        self,
        task_id: UUID,
        event_type: EventType,
        status: TaskStatus,
        data: dict[str, Any] | None = None,
    ) -> TaskEvent:
        with self._lock:
            row = self._conn.execute(
                "SELECT COALESCE(MAX(sequence), 0) AS max_sequence"
                " FROM task_events WHERE task_id = ?",
                (str(task_id),),
            ).fetchone()
            sequence = int(row["max_sequence"]) + 1
            event = TaskEvent(
                task_id=task_id,
                type=event_type,
                status=status,
                sequence=sequence,
                data=data or {},
            )
            self._conn.execute(
                """
                INSERT INTO task_events (
                    task_id, sequence, event_id, type, status, occurred_at, data
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(task_id),
                    sequence,
                    str(event.event_id),
                    event.type.value,
                    event.status.value,
                    _iso(event.occurred_at),
                    json.dumps(event.data, ensure_ascii=False),
                ),
            )
            self._conn.commit()
        return event

    def events(self, task_id: UUID, after_sequence: int = 0) -> Sequence[TaskEvent]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT * FROM task_events
                WHERE task_id = ? AND sequence > ?
                ORDER BY sequence
                """,
                (str(task_id), after_sequence),
            ).fetchall()
        return tuple(self._to_event(row) for row in rows)

    def list_tasks(self, *, limit: int = 50, status: TaskStatus | None = None) -> Sequence[Task]:
        query = "SELECT * FROM tasks"
        params: list[Any] = []
        if status is not None:
            query += " WHERE status = ?"
            params.append(status.value)
        query += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        with self._lock:
            rows = self._conn.execute(query, params).fetchall()
        return tuple(self._to_task(row) for row in rows)

    # ---------- 确认凭证 ----------

    def issue(self, confirmation: Confirmation) -> Confirmation:
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO confirmations (
                    token, task_id, step_id, flow_version, parameter_digest,
                    user_ref, issued_at, expires_at, consumed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    confirmation.token,
                    str(confirmation.task_id),
                    confirmation.step_id,
                    confirmation.flow_version,
                    confirmation.parameter_digest,
                    confirmation.user_ref,
                    _iso(confirmation.issued_at),
                    _iso(confirmation.expires_at),
                    _iso(confirmation.consumed_at),
                ),
            )
            self._conn.commit()
        return confirmation

    def latest(self, task_id: UUID, step_id: str) -> Confirmation | None:
        with self._lock:
            row = self._conn.execute(
                """
                SELECT * FROM confirmations
                WHERE task_id = ? AND step_id = ?
                ORDER BY issued_at DESC
                LIMIT 1
                """,
                (str(task_id), step_id),
            ).fetchone()
        return self._to_confirmation(row) if row else None

    def revoke(self, task_id: UUID, step_id: str) -> None:
        with self._lock:
            self._conn.execute(
                """
                UPDATE confirmations SET consumed_at = ?
                WHERE task_id = ? AND step_id = ? AND consumed_at IS NULL
                """,
                (_iso(utc_now()), str(task_id), step_id),
            )
            self._conn.commit()

    def consume(self, token: str) -> Confirmation:
        now = utc_now()
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM confirmations WHERE token = ?", (token,)
            ).fetchone()
            if row is None:
                raise ConfirmationError("confirmation token not found")
            confirmation = self._to_confirmation(row)
            if confirmation.consumed_at is not None:
                raise ConfirmationError("confirmation token already used")
            if confirmation.expires_at <= now:
                raise ConfirmationError("confirmation token expired")
            self._conn.execute(
                "UPDATE confirmations SET consumed_at = ? WHERE token = ? AND consumed_at IS NULL",
                (_iso(now), token),
            )
            self._conn.commit()
        confirmation.consumed_at = now
        return confirmation

    # ---------- 映射 ----------

    @staticmethod
    def _row_values(task: Task) -> tuple[Any, ...]:
        return (
            str(task.task_id),
            task.conversation_id,
            task.user_ref,
            task.flow_id,
            task.flow_version,
            task.status.value,
            task.current_step,
            json.dumps(task.completed_steps),
            json.dumps(task.slots),
            json.dumps(task.pending_fields),
            json.dumps(task.result) if task.result is not None else None,
            task.failure_code,
            task.failure_message,
            _iso(task.created_at),
            _iso(task.updated_at),
        )

    @staticmethod
    def _to_task(row: sqlite3.Row) -> Task:
        return Task(
            task_id=UUID(row["task_id"]),
            conversation_id=row["conversation_id"],
            user_ref=row["user_ref"],
            flow_id=row["flow_id"],
            flow_version=row["flow_version"],
            status=TaskStatus(row["status"]),
            current_step=row["current_step"],
            completed_steps=json.loads(row["completed_steps"]),
            slots=json.loads(row["slots"]),
            pending_fields=json.loads(row["pending_fields"]),
            result=json.loads(row["result"]) if row["result"] else None,
            failure_code=row["failure_code"],
            failure_message=row["failure_message"],
            created_at=_dt(row["created_at"]),
            updated_at=_dt(row["updated_at"]),
        )

    @staticmethod
    def _to_event(row: sqlite3.Row) -> TaskEvent:
        return TaskEvent(
            event_id=UUID(row["event_id"]),
            task_id=UUID(row["task_id"]),
            type=EventType(row["type"]),
            status=TaskStatus(row["status"]),
            sequence=int(row["sequence"]),
            occurred_at=_dt(row["occurred_at"]),
            data=json.loads(row["data"]),
        )

    @staticmethod
    def _to_confirmation(row: sqlite3.Row) -> Confirmation:
        return Confirmation(
            token=row["token"],
            task_id=UUID(row["task_id"]),
            step_id=row["step_id"],
            flow_version=row["flow_version"],
            parameter_digest=row["parameter_digest"],
            user_ref=row["user_ref"],
            issued_at=_dt(row["issued_at"]),
            expires_at=_dt(row["expires_at"]),
            consumed_at=_dt(row["consumed_at"]),
        )
