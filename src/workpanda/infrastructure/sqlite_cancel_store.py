"""取消信号的 SQLite 实现：让同机另一个进程里正在跑的执行也能看见取消。

与任务库共用一个文件，因此多进程部署时不需要再引入第二种共享状态。
一个任务只留一条信号（重复取消不覆盖第一个原因由上层令牌保证，这里只负责
"这个任务被取消过"这个事实）。信号带绝对过期时间：进程崩溃没人清理时，
一条陈旧取消也不会永久拦住后来的执行。

注意：SQLite 只解决同机多进程。跨主机仍需换成 Redis / 数据库 advisory lock
之类的共享后端，`CancelSignalStore` 协议不变。
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from threading import Lock
from uuid import UUID

SCHEMA = """
CREATE TABLE IF NOT EXISTS harness_cancel_signals (
    task_id TEXT PRIMARY KEY,
    reason TEXT NOT NULL,
    signalled_at REAL NOT NULL,
    expires_at REAL NOT NULL
);
"""


class SqliteCancelSignalStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        if self.path.parent and str(self.path.parent) not in {"", "."}:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = Lock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        # 另一个进程可能正在写任务事件；短暂锁冲突应等待，而不是让取消直接失败。
        self._conn.execute("PRAGMA busy_timeout = 3000")
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def signal(self, task_id: UUID, reason: str, *, ttl_seconds: float) -> None:
        now = time.time()
        with self._lock:
            # 顺手清掉过期信号，避免"取消过的任务"无限累积在库里。
            self._conn.execute(
                "DELETE FROM harness_cancel_signals WHERE expires_at <= ?", (now,)
            )
            self._conn.execute(
                """
                INSERT INTO harness_cancel_signals (
                    task_id, reason, signalled_at, expires_at
                ) VALUES (?, ?, ?, ?)
                ON CONFLICT (task_id) DO UPDATE SET
                    reason = excluded.reason,
                    signalled_at = excluded.signalled_at,
                    expires_at = excluded.expires_at
                """,
                (str(task_id), reason, now, now + max(1.0, ttl_seconds)),
            )
            self._conn.commit()

    def reason_for(self, task_id: UUID) -> str | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT reason, expires_at FROM harness_cancel_signals WHERE task_id = ?",
                (str(task_id),),
            ).fetchone()
        if row is None or float(row["expires_at"]) <= time.time():
            return None
        return str(row["reason"])

    def clear(self, task_id: UUID) -> None:
        """人工确认后清理，让同一任务可以重新执行（等价于取消 TTL 到期）。"""

        with self._lock:
            self._conn.execute(
                "DELETE FROM harness_cancel_signals WHERE task_id = ?", (str(task_id),)
            )
            self._conn.commit()
