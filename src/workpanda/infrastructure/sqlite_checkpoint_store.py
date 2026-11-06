"""断点的 SQLite 实现：进程重启后仍能接着跑。

只存一份快照（同一 task+step 覆盖写），因为 checkpoint 描述"现在到哪了"，
不是审计账本；要复盘执行过程看 trace，不看 checkpoint。

与任务库共用一个文件：槽位数据本来就在库里，checkpoint 不额外扩大数据面。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from threading import Lock
from uuid import UUID

from workpanda.harness.checkpoint import HarnessCheckpoint

SCHEMA = """
CREATE TABLE IF NOT EXISTS harness_checkpoints (
    task_id TEXT NOT NULL,
    step_id TEXT NOT NULL,
    trace_id TEXT NOT NULL,
    resumable INTEGER NOT NULL,
    updated_at TEXT NOT NULL,
    payload TEXT NOT NULL,
    PRIMARY KEY (task_id, step_id)
);
"""


class SqliteCheckpointStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        if self.path.parent and str(self.path.parent) not in {"", "."}:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = Lock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA busy_timeout = 3000")
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def save(self, checkpoint: HarnessCheckpoint) -> None:
        payload = checkpoint.model_dump_json()
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO harness_checkpoints (
                    task_id, step_id, trace_id, resumable, updated_at, payload
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT (task_id, step_id) DO UPDATE SET
                    trace_id = excluded.trace_id,
                    resumable = excluded.resumable,
                    updated_at = excluded.updated_at,
                    payload = excluded.payload
                """,
                (
                    str(checkpoint.task_id),
                    checkpoint.step_id,
                    checkpoint.trace_id,
                    int(checkpoint.resumable),
                    checkpoint.updated_at.isoformat(),
                    payload,
                ),
            )
            self._conn.commit()

    def load(self, task_id: UUID, step_id: str) -> HarnessCheckpoint | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT payload FROM harness_checkpoints WHERE task_id = ? AND step_id = ?",
                (str(task_id), step_id),
            ).fetchone()
        if row is None:
            return None
        # 结构演进后旧快照可能无法解析；此时按"没有断点"处理，由上层拒绝续跑而非崩溃。
        try:
            return HarnessCheckpoint.model_validate_json(row["payload"])
        except ValueError:
            return None

    def delete(self, task_id: UUID, step_id: str) -> None:
        with self._lock:
            self._conn.execute(
                "DELETE FROM harness_checkpoints WHERE task_id = ? AND step_id = ?",
                (str(task_id), step_id),
            )
            self._conn.commit()

    def __len__(self) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) AS total FROM harness_checkpoints"
            ).fetchone()
        return int(row["total"]) if row is not None else 0
