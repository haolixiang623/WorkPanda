"""步骤占用租约：同一 (task_id, step_id) 同时只允许一个执行者。

为什么需要：checkpoint 假设"同一 (task, step) 只有一个写者"。两个执行者同时
读到同一份断点时，各自都会认为自己可以继续；如果其中一步有非幂等工具，
防重复副作用的指纹是**各自内存里的 ToolGrant 状态**，互相看不见，外部动作
就可能真的发生两次。trace、审计归档都只能事后发现，拦不住。

租约是与断点独立的一层：即使没有断点（首次执行）也要能挡住并发。它只回答
"现在谁在跑这一步"，不参与流程推进、不决定业务结果，也不保存任何业务数据。

两条安全边界：

1. **拿不到就拒绝，不猜测**：租约存在、过期时间无法读取、存储异常，一律按
   `checkpoint_busy` 拒绝执行（未确认、不调模型、不调工具）。宁可误拒一次，
   也不能让两个执行者同时进入可能有副作用的步骤；
2. **执行结束必须释放**：正常完成、结论未确认、抛异常，都在 `finally` 里释放。
   未确认时释放是刻意的——断点还在，后续那次执行本就应该能接着跑。

TTL 必须长于一次执行的最坏耗时（步骤 deadline + 模型/工具超时余量），否则
租约会在执行中途过期，等于没保护。跨主机多实例部署需要把这里的 SQLite /
内存实现换成共享锁；单写者假设本身不变。
"""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Lock
from typing import Protocol
from uuid import UUID


class StepLeaseStore(Protocol):
    def acquire(self, task_id: UUID, step_id: str, owner: str, ttl_seconds: float) -> bool: ...

    def release(self, task_id: UUID, step_id: str, owner: str) -> None: ...


@dataclass
class _Lease:
    owner: str
    # monotonic 时钟：进程内不受系统时间被回拨影响。
    expires_at: float


class InMemoryStepLeaseStore:
    """进程内实现：只能挡住同一进程里的并发，重启即失效。"""

    def __init__(self) -> None:
        self._items: dict[tuple[UUID, str], _Lease] = {}
        self._lock = Lock()

    def acquire(self, task_id: UUID, step_id: str, owner: str, ttl_seconds: float) -> bool:
        now = time.monotonic()
        key = (task_id, step_id)
        with self._lock:
            current = self._items.get(key)
            if current is not None and current.expires_at > now and current.owner != owner:
                return False
            self._items[key] = _Lease(owner=owner, expires_at=now + max(0.001, ttl_seconds))
            return True

    def release(self, task_id: UUID, step_id: str, owner: str) -> None:
        key = (task_id, step_id)
        with self._lock:
            current = self._items.get(key)
            if current is not None and current.owner == owner:
                del self._items[key]

    def __len__(self) -> int:
        with self._lock:
            return len(self._items)


SCHEMA = """
CREATE TABLE IF NOT EXISTS harness_step_leases (
    task_id TEXT NOT NULL,
    step_id TEXT NOT NULL,
    owner TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    PRIMARY KEY (task_id, step_id)
);
"""


class SqliteStepLeaseStore:
    """与任务库同文件的实现：同一台机器上跨进程也能互斥。

    `acquire` 用 `BEGIN IMMEDIATE` + `INSERT ... ON CONFLICT` 包成一个事务，
    两个进程同时抢同一个键时，SQLite 会串行化事务，不会都返回成功。
    """

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

    def acquire(self, task_id: UUID, step_id: str, owner: str, ttl_seconds: float) -> bool:
        now = datetime.now(UTC)
        now_text = now.isoformat()
        expires_text = (now + timedelta(seconds=max(0.001, ttl_seconds))).isoformat()
        key = (str(task_id), step_id)
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                row = self._conn.execute(
                    "SELECT owner, expires_at FROM harness_step_leases"
                    " WHERE task_id = ? AND step_id = ?",
                    key,
                ).fetchone()
                allowed = (
                    row is None or row["owner"] == owner or row["expires_at"] <= now_text
                )
                if allowed:
                    self._conn.execute(
                        """
                        INSERT INTO harness_step_leases (task_id, step_id, owner, expires_at)
                        VALUES (?, ?, ?, ?)
                        ON CONFLICT (task_id, step_id) DO UPDATE SET
                            owner = excluded.owner, expires_at = excluded.expires_at
                        """,
                        (*key, owner, expires_text),
                    )
                self._conn.commit()
                return allowed
            except Exception:
                self._conn.rollback()
                raise

    def release(self, task_id: UUID, step_id: str, owner: str) -> None:
        with self._lock:
            self._conn.execute(
                "DELETE FROM harness_step_leases"
                " WHERE task_id = ? AND step_id = ? AND owner = ?",
                (str(task_id), step_id, owner),
            )
            self._conn.commit()
