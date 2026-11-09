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
from workpanda.domain.paging import (
    EventPage,
    EventQuery,
    TaskPage,
    TaskQuery,
    canonical_stamp,
    encode_token,
)
from workpanda.domain.repository import PendingEvent, StaleTaskState, TaskNotFoundError

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
    user_action_expires_at TEXT,
    user_action_confirmed_at TEXT,
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

-- 键集翻页按 (created_at, task_id) 倒序走，这条索引让"比锚点更旧"不必全表扫。
CREATE INDEX IF NOT EXISTS idx_tasks_cursor
    ON tasks (created_at DESC, task_id DESC);
-- 事件时间窗过滤按 (task_id, occurred_at)；序号翻页由主键覆盖。
CREATE INDEX IF NOT EXISTS idx_task_events_occurred
    ON task_events (task_id, occurred_at);
"""

# 旧库补列。`CREATE TABLE IF NOT EXISTS` 对已存在的表什么都不做，所以必须显式检查
# （与 `sqlite_flow_store` 同一套做法）。只加列、不改已有列：迁移本身不能成为一次
# 数据改写的机会。补出来的列在旧行上为 NULL，读出即 `None`，与"这任务从来没进过
# 用户操作节点"是同一种语义——不需要回填，回填反而会凭空造出一个到期时刻。
_COLUMN_MIGRATIONS: dict[str, str] = {
    "user_action_expires_at": "ALTER TABLE tasks ADD COLUMN user_action_expires_at TEXT",
    "user_action_confirmed_at": "ALTER TABLE tasks ADD COLUMN user_action_confirmed_at TEXT",
}


def _iso(value: datetime | None) -> str | None:
    """时间列统一写规范化文本。

    翻页与时间窗都靠字符串比较，而 `datetime.isoformat()` 的长度是变的：微秒恰好
    为 0 时是 `...:26+00:00`，否则是 `...:26.000001+00:00`。两种写法在同一秒内
    比较，`'+'`(43) 比 `'.'`(46) 小，于是"同一时刻"被排成两个位置，翻页经过那一刻
    时要么漏一条、要么重复一条——而且不报错。写入口只认 `canonical_stamp()`。
    """

    return canonical_stamp(value) if value else None


def _dt(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def _normalize_timestamps(conn: sqlite3.Connection) -> None:
    """把旧行的 `created_at` / `occurred_at` 改写一次成规范文本。

    老版本写的是裸 `isoformat()`，行长不一致，字符串排序在"微秒为 0"的行上会
    排错位置。打开库时改写一次（幂等）；读不出来的值保持原样——猜一个时间比
    留着一个异常值更危险。
    """

    for table, column in (
        ("tasks", "created_at"),
        ("tasks", "updated_at"),
        ("task_events", "occurred_at"),
    ):
        rows = conn.execute(f"SELECT rowid AS rid, {column} AS value FROM {table}").fetchall()
        for row in rows:
            current = row["value"]
            try:
                fixed = canonical_stamp(datetime.fromisoformat(current))
            except (ValueError, TypeError):
                continue
            if fixed != current:
                conn.execute(
                    f"UPDATE {table} SET {column} = ? WHERE rowid = ?", (fixed, row["rid"])
                )


def _task_where(query: TaskQuery) -> tuple[str, list[object]]:
    """把任务查询条件翻译成 WHERE。

    这里是内存后端 `task_matches()` 的 SQL 翻译，两边必须给出同样的集合；
    `tests/test_ops_paging.py` 里的跨后端一致性用例盯着这件事。
    """

    clauses: list[str] = []
    params: list[object] = []
    if query.flow_id is not None:
        clauses.append("flow_id = ?")
        params.append(query.flow_id)
    if query.status is not None:
        clauses.append("status = ?")
        params.append(query.status.value)
    if query.since is not None:
        clauses.append("created_at >= ?")
        params.append(canonical_stamp(query.since))
    if query.until is not None:
        clauses.append("created_at < ?")
        params.append(canonical_stamp(query.until))
    if query.cursor is not None:
        # 严格"比锚点更旧"：时间更早，或同一时刻但 task_id 更小。
        anchor = query.cursor.stamp
        clauses.append("(created_at < ? OR (created_at = ? AND task_id < ?))")
        params.extend([anchor, anchor, str(query.cursor.task_id)])
    return (" WHERE " + " AND ".join(clauses)) if clauses else "", params


def _event_where(query: EventQuery) -> tuple[str, list[object]]:
    """事件查询条件的 SQL 翻译，与内存后端 `event_matches()` 同一口径。"""

    clauses = ["task_id = ?", "sequence > ?"]
    params: list[object] = [str(query.task_id), max(0, query.after_sequence)]
    if query.kind is not None:
        clauses.append("type = ?")
        params.append(query.kind.value)
    if query.since is not None:
        clauses.append("occurred_at >= ?")
        params.append(canonical_stamp(query.since))
    if query.until is not None:
        clauses.append("occurred_at < ?")
        params.append(canonical_stamp(query.until))
    return " WHERE " + " AND ".join(clauses), params


class SqliteStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        if self.path.parent and str(self.path.parent) not in {"", "."}:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = Lock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        # trace / checkpoint 与任务数据共用同一个文件，短暂写冲突应等待而不是直接抛 locked。
        self._conn.execute("PRAGMA busy_timeout = 3000")
        self._conn.executescript(SCHEMA)
        self._ensure_columns()
        _normalize_timestamps(self._conn)
        self._conn.commit()

    def _ensure_columns(self) -> None:
        """给旧库补列。幂等：每次启动都按实际列名核对一遍。

        旧库是"能用但缺列"的库，直接 `ALTER TABLE ... ADD COLUMN` 撞上已补过的列
        会抛 `duplicate column name`，所以先读 `PRAGMA table_info` 再决定。
        只加列不加默认值：补出来的列在旧行上是 NULL，读出来是 `None`，语义就是
        "这任务从来没进过用户操作节点"，不需要也不应该回填。
        """

        existing = {
            row["name"] for row in self._conn.execute("PRAGMA table_info(tasks)").fetchall()
        }
        for column, statement in _COLUMN_MIGRATIONS.items():
            if column not in existing:
                self._conn.execute(statement)

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
                    current_step, completed_steps, slots, pending_fields,
                    user_action_expires_at, user_action_confirmed_at, result,
                    failure_code, failure_message, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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

    _UPDATE_TASK = """
        UPDATE tasks SET
            conversation_id = ?, user_ref = ?, flow_id = ?, flow_version = ?,
            status = ?, current_step = ?, completed_steps = ?, slots = ?,
            pending_fields = ?, user_action_expires_at = ?, user_action_confirmed_at = ?,
            result = ?, failure_code = ?, failure_message = ?,
            created_at = ?, updated_at = ?
        WHERE task_id = ?
    """

    def _write_task(self, task: Task, expected_status: TaskStatus | None) -> int:
        """写任务行并返回命中行数；`expected_status` 会并进同一条 UPDATE 的条件。"""
        sql = self._UPDATE_TASK
        params: list[Any] = [
            task.conversation_id,
            task.user_ref,
            task.flow_id,
            task.flow_version,
            task.status.value,
            task.current_step,
            json.dumps(task.completed_steps),
            json.dumps(task.slots),
            json.dumps(task.pending_fields),
            _iso(task.user_action_expires_at),
            _iso(task.user_action_confirmed_at),
            json.dumps(task.result) if task.result is not None else None,
            task.failure_code,
            task.failure_message,
            _iso(task.created_at),
            _iso(task.updated_at),
            str(task.task_id),
        ]
        if expected_status is not None:
            sql += " AND status = ?"
            params.append(expected_status.value)
        return self._conn.execute(sql, tuple(params)).rowcount

    def save(self, task: Task) -> Task:
        with self._lock:
            if self._write_task(task, None) == 0:
                self._conn.rollback()
                raise TaskNotFoundError(str(task.task_id))
            self._conn.commit()
        return task

    def commit(
        self,
        task: Task,
        events: Sequence[PendingEvent] = (),
        *,
        expected_status: TaskStatus | None = None,
    ) -> Sequence[TaskEvent]:
        """条件写与事件追加在同一事务里完成。

        关键点在顺序：先执行带 `status` 条件的 UPDATE。它一旦命中，SQLite 就
        持有写锁直到 `commit()`，因此随后分配顺序号、插入事件对别的进程也
        是同一个临界区。若反过来先 SELECT 再 UPDATE，中间恰好是另一个进程
        把状态翻成 `cancelled` 的窗口——那等于白做。
        """
        with self._lock:
            try:
                if self._write_task(task, expected_status) == 0:
                    exists = (
                        self._conn.execute(
                            "SELECT 1 FROM tasks WHERE task_id = ?", (str(task.task_id),)
                        ).fetchone()
                        is not None
                    )
                    if not exists:
                        raise TaskNotFoundError(str(task.task_id))
                    raise StaleTaskState(
                        f"task {task.task_id} no longer has the expected status"
                    )
                row = self._conn.execute(
                    "SELECT COALESCE(MAX(sequence), 0) AS max_sequence"
                    " FROM task_events WHERE task_id = ?",
                    (str(task.task_id),),
                ).fetchone()
                sequence = int(row["max_sequence"])
                written: list[TaskEvent] = []
                for pending in events:
                    sequence += 1
                    event = TaskEvent(
                        task_id=task.task_id,
                        type=pending.type,
                        status=pending.status,
                        sequence=sequence,
                        data=dict(pending.data),
                    )
                    self._conn.execute(
                        """
                        INSERT INTO task_events (
                            task_id, sequence, event_id, type, status, occurred_at, data
                        ) VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            str(task.task_id),
                            sequence,
                            str(event.event_id),
                            event.type.value,
                            event.status.value,
                            _iso(event.occurred_at),
                            json.dumps(event.data, ensure_ascii=False),
                        ),
                    )
                    written.append(event)
                self._conn.commit()
            except sqlite3.OperationalError as exc:
                # 另一个连接正持有写锁：本次状态无法确认，按"对方先赢"收口，
                # 绝不带着疑问继续产生结果事件。
                self._conn.rollback()
                raise StaleTaskState(f"task state could not be confirmed: {exc}") from exc
            except Exception:
                self._conn.rollback()
                raise
        return tuple(written)

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

    def query_tasks(self, query: TaskQuery) -> TaskPage:
        where, params = _task_where(query)
        limit = max(0, query.limit)
        # 多取一条用来判断"后面还有没有"：这条不返回给调用方，也避免再发一次
        # COUNT——多一次查询就多一个"两次结果不一致"的窗口。
        sql = (
            f"SELECT * FROM tasks{where}"
            " ORDER BY created_at DESC, task_id DESC LIMIT ?"
        )
        with self._lock:
            rows = self._conn.execute(sql, [*params, limit + 1]).fetchall()

        items = tuple(self._to_task(row) for row in rows[:limit])
        next_cursor = None
        if len(rows) > limit and items:
            last = items[-1]
            next_cursor = encode_token(
                "tasks", [canonical_stamp(last.created_at), str(last.task_id)]
            )
        return TaskPage(items=items, next_cursor=next_cursor)

    def query_events(self, query: EventQuery) -> EventPage:
        where, params = _event_where(query)
        limit = max(0, query.limit)
        sql = (
            f"SELECT * FROM task_events{where}"
            " ORDER BY sequence LIMIT ?"
        )
        with self._lock:
            rows = self._conn.execute(sql, [*params, limit + 1]).fetchall()

        items = tuple(self._to_event(row) for row in rows[:limit])
        next_cursor = str(items[-1].sequence) if len(rows) > limit and items else None
        return EventPage(items=items, next_cursor=next_cursor)

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
            _iso(task.user_action_expires_at),
            _iso(task.user_action_confirmed_at),
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
            user_action_expires_at=_dt(row["user_action_expires_at"]),
            user_action_confirmed_at=_dt(row["user_action_confirmed_at"]),
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
