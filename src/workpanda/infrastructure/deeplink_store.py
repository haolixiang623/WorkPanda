"""确认深链的存储实现。

两个后端共享 `domain.deeplink.ConfirmationLinkStore`：

- `InMemoryConfirmationLinkStore`：默认后端，跟任务库一起"重启即丢"；
- `SqliteConfirmationLinkStore`：与任务库同一个文件。深链本身可以丢（丢了只是
  让旧卡片打不开，用户重新发一张即可），但**留痕不能和业务数据分家**：排障时
  要回答"这张卡到底发出去没有"，两边不在一个事务语义里就永远对不上。

内存版同样不做容量淘汰，理由与 IM 留痕一致：被悄悄淘汰的那条恰好可能是"谁在
反复点别人的卡"。
"""

from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path
from threading import Lock
from uuid import UUID

from workpanda.domain.deeplink import ConfirmationLink, ConfirmationLinkAttempt
from workpanda.domain.models import utc_now

SCHEMA = """
CREATE TABLE IF NOT EXISTS confirmation_links (
    token TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    consumed_at TEXT,
    body TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS confirmation_links_task
    ON confirmation_links (task_id);

CREATE TABLE IF NOT EXISTS confirmation_link_attempts (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT NOT NULL,
    outcome TEXT NOT NULL,
    reason TEXT NOT NULL,
    task_id TEXT,
    step_id TEXT NOT NULL,
    actor TEXT NOT NULL,
    actor_kind TEXT NOT NULL,
    token_fingerprint TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS confirmation_link_attempts_task
    ON confirmation_link_attempts (task_id, seq DESC);
"""


class InMemoryConfirmationLinkStore:
    def __init__(self) -> None:
        self._links: dict[str, ConfirmationLink] = {}
        self._attempts: list[ConfirmationLinkAttempt] = []
        self._lock = Lock()

    def issue(self, link: ConfirmationLink) -> ConfirmationLink:
        with self._lock:
            self._links[link.token] = link.model_copy(deep=True)
        return link.model_copy(deep=True)

    def get(self, token: str) -> ConfirmationLink | None:
        with self._lock:
            link = self._links.get(token)
        return link.model_copy(deep=True) if link is not None else None

    def consume(self, token: str, *, now: datetime | None = None) -> ConfirmationLink | None:
        stamp = now or utc_now()
        with self._lock:
            link = self._links.get(token)
            if link is None or link.consumed_at is not None:
                return None
            link.consumed_at = stamp
            return link.model_copy(deep=True)

    def record_attempt(self, attempt: ConfirmationLinkAttempt) -> None:
        with self._lock:
            self._attempts.append(attempt.model_copy(deep=True))

    def list_attempts(
        self, *, task_id: UUID | None = None, limit: int = 50
    ) -> list[ConfirmationLinkAttempt]:
        limit = max(0, limit)
        if limit == 0:
            return []
        with self._lock:
            entries = [
                entry for entry in self._attempts if task_id is None or entry.task_id == task_id
            ]
        # 新的在前：排障先看最近发生的那几次。
        return [entry.model_copy(deep=True) for entry in reversed(entries)][:limit]


class SqliteConfirmationLinkStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        if self.path.parent and str(self.path.parent) not in {"", "."}:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = Lock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        # 与任务库同文件：短暂写冲突应等待，而不是把"点开一张卡"直接判失败。
        self._conn.execute("PRAGMA busy_timeout = 3000")
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def issue(self, link: ConfirmationLink) -> ConfirmationLink:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO confirmation_links (token, task_id, consumed_at, body)"
                " VALUES (?, ?, ?, ?)"
                " ON CONFLICT(token) DO UPDATE SET"
                " consumed_at = excluded.consumed_at, body = excluded.body",
                (
                    link.token,
                    str(link.task_id),
                    link.consumed_at.isoformat() if link.consumed_at else None,
                    link.model_dump_json(),
                ),
            )
        return link

    def get(self, token: str) -> ConfirmationLink | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT body FROM confirmation_links WHERE token = ?", (token,)
            ).fetchone()
        return ConfirmationLink.model_validate_json(row["body"]) if row is not None else None

    def consume(self, token: str, *, now: datetime | None = None) -> ConfirmationLink | None:
        stamp = now or utc_now()
        with self._lock, self._conn:
            row = self._conn.execute(
                "SELECT body FROM confirmation_links WHERE token = ?", (token,)
            ).fetchone()
            if row is None:
                return None
            link = ConfirmationLink.model_validate_json(row["body"])
            if link.consumed_at is not None:
                return None
            link.consumed_at = stamp
            # 条件写是唯一的一次性保证：两个并发落地请求里只有一个 UPDATE 命中。
            # 先读后写的窗口由 `WHERE consumed_at IS NULL` 收口，不靠进程内锁的运气。
            cursor = self._conn.execute(
                "UPDATE confirmation_links SET consumed_at = ?, body = ?"
                " WHERE token = ? AND consumed_at IS NULL",
                (stamp.isoformat(), link.model_dump_json(), token),
            )
            if cursor.rowcount == 0:
                return None
        return link

    def record_attempt(self, attempt: ConfirmationLinkAttempt) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO confirmation_link_attempts"
                " (at, outcome, reason, task_id, step_id, actor, actor_kind, token_fingerprint)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    attempt.at.isoformat(),
                    attempt.outcome.value,
                    attempt.reason,
                    str(attempt.task_id) if attempt.task_id is not None else None,
                    attempt.step_id,
                    attempt.actor,
                    attempt.actor_kind,
                    attempt.token_fingerprint,
                ),
            )

    def list_attempts(
        self, *, task_id: UUID | None = None, limit: int = 50
    ) -> list[ConfirmationLinkAttempt]:
        limit = max(0, limit)
        if limit == 0:
            return []
        query = (
            "SELECT at, outcome, reason, task_id, step_id, actor, actor_kind,"
            " token_fingerprint FROM confirmation_link_attempts"
        )
        params: list[object] = []
        if task_id is not None:
            query += " WHERE task_id = ?"
            params.append(str(task_id))
        query += " ORDER BY seq DESC LIMIT ?"
        params.append(limit)
        with self._lock:
            rows = self._conn.execute(query, params).fetchall()
        return [
            ConfirmationLinkAttempt(
                at=row["at"],
                outcome=row["outcome"],
                reason=row["reason"],
                task_id=row["task_id"],
                step_id=row["step_id"],
                actor=row["actor"],
                actor_kind=row["actor_kind"],
                token_fingerprint=row["token_fingerprint"],
            )
            for row in rows
        ]
