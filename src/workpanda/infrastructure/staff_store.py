"""工作人员目录、IM 身份映射与解析留痕的存储实现。

三个后端共享同一份接口（`domain.staff.StaffStore`）：

- `InMemoryStaffStore`：默认后端，跟任务库一起"重启即丢"；
- `SqliteStaffStore`：与任务库同一个文件。放同一文件而不是单独库，是因为一次
  "改映射 + 读主体 + 写留痕"必须落在同一个事务语义里，分库之后就可能出现
  "拒绝记录写了、映射已经改了"这种对不上的现场。

内存版刻意**不做容量淘汰**：IM 映射是权限数据，被悄悄淘汰一条等于某个工作人员
突然登不进来；留痕被淘汰则等于"谁被拦过"查不到了。要控制规模请用 sqlite 后端
并配合外部清理，而不是让后端自己丢数据。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from threading import Lock

from workpanda.domain.staff import (
    ImAccessEntry,
    ImAccessOutcome,
    ImIdentity,
    StaffMember,
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS staff_members (
    subject TEXT PRIMARY KEY,
    body TEXT NOT NULL,
    enabled INTEGER NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS im_identities (
    channel TEXT NOT NULL,
    im_user_id TEXT NOT NULL,
    principal_subject TEXT NOT NULL,
    enabled INTEGER NOT NULL,
    body TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (channel, im_user_id)
);

CREATE TABLE IF NOT EXISTS im_access_log (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT NOT NULL,
    origin TEXT NOT NULL,
    channel TEXT NOT NULL,
    im_user_id TEXT NOT NULL,
    subject TEXT NOT NULL,
    outcome TEXT NOT NULL,
    reason TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS im_access_log_rejected
    ON im_access_log (outcome, seq DESC);
"""


class InMemoryStaffStore:
    """进程内实现。读操作返回副本，避免调用方改到库里那一份。"""

    def __init__(self) -> None:
        self._members: dict[str, StaffMember] = {}
        self._identities: dict[tuple[str, str], ImIdentity] = {}
        self._access: list[ImAccessEntry] = []
        self._lock = Lock()

    # ---------- 工作人员目录 ----------

    def get_member(self, subject: str) -> StaffMember | None:
        with self._lock:
            member = self._members.get(subject.strip())
        return member.model_copy(deep=True) if member is not None else None

    def list_members(self) -> list[StaffMember]:
        with self._lock:
            members = list(self._members.values())
        return sorted(
            (member.model_copy(deep=True) for member in members),
            key=lambda member: member.subject,
        )

    def save_member(self, member: StaffMember) -> StaffMember:
        # 存一份、回一份：读写不共享同一个对象，调用方改返回值不会改到库里那一份。
        with self._lock:
            self._members[member.subject] = member.model_copy(deep=True)
        return member.model_copy(deep=True)

    def delete_member(self, subject: str) -> bool:
        with self._lock:
            removed = self._members.pop(subject.strip(), None)
            if removed is None:
                return False
            # 主体没了，指向它的映射必须一起失效：留着一行指向不存在的主体的映射，
            # 会让"这个人已经被删了"和"映射还在"同时成立，事后无法判断哪一边是真相。
            stale = [
                key
                for key, value in self._identities.items()
                if value.principal_subject == removed.subject
            ]
            for key in stale:
                del self._identities[key]
            return True

    # ---------- IM 身份映射 ----------

    def resolve_identity(self, channel: str, im_user_id: str) -> ImIdentity | None:
        with self._lock:
            identity = self._identities.get((channel.strip().lower(), im_user_id.strip()))
        return identity.model_copy(deep=True) if identity is not None else None

    def list_identities(self, channel: str | None = None) -> list[ImIdentity]:
        wanted = channel.strip().lower() if channel else None
        with self._lock:
            identities = [
                value
                for key, value in self._identities.items()
                if wanted is None or key[0] == wanted
            ]
        return sorted(
            (identity.model_copy(deep=True) for identity in identities),
            key=lambda identity: (identity.channel, identity.im_user_id),
        )

    def save_identity(self, identity: ImIdentity) -> ImIdentity:
        with self._lock:
            self._identities[(identity.channel, identity.im_user_id)] = identity.model_copy(
                deep=True
            )
        return identity.model_copy(deep=True)

    def delete_identity(self, channel: str, im_user_id: str) -> bool:
        with self._lock:
            removed = self._identities.pop((channel.strip().lower(), im_user_id.strip()), None)
        return removed is not None

    # ---------- 解析留痕 ----------

    def record_access(self, entry: ImAccessEntry) -> None:
        with self._lock:
            self._access.append(entry.model_copy(deep=True))

    def list_access(self, *, limit: int = 50, rejected_only: bool = False) -> list[ImAccessEntry]:
        limit = max(0, limit)
        if limit == 0:
            return []
        with self._lock:
            entries = [
                entry
                for entry in self._access
                if not rejected_only or entry.outcome is ImAccessOutcome.REJECTED
            ]
        # 新的在前：排障先看最近被拦的是谁。
        return [entry.model_copy(deep=True) for entry in reversed(entries)][:limit]


class SqliteStaffStore:
    """SQLite 实现。所有写操作串行，读走同一个连接。"""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        if self.path.parent and str(self.path.parent) not in {"", "."}:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = Lock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        # 与任务库同文件：短暂写冲突应等待，而不是把"改一条映射"直接判失败。
        self._conn.execute("PRAGMA busy_timeout = 3000")
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ---------- 工作人员目录 ----------

    def get_member(self, subject: str) -> StaffMember | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT body FROM staff_members WHERE subject = ?", (subject.strip(),)
            ).fetchone()
        return StaffMember.model_validate_json(row["body"]) if row is not None else None

    def list_members(self) -> list[StaffMember]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT body FROM staff_members ORDER BY subject"
            ).fetchall()
        return [StaffMember.model_validate_json(row["body"]) for row in rows]

    def save_member(self, member: StaffMember) -> StaffMember:
        body = member.model_dump_json()
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO staff_members (subject, body, enabled, updated_at)"
                " VALUES (?, ?, ?, ?)"
                " ON CONFLICT(subject) DO UPDATE SET"
                " body = excluded.body, enabled = excluded.enabled,"
                " updated_at = excluded.updated_at",
                (member.subject, body, int(member.enabled), member.updated_at.isoformat()),
            )
        return member

    def delete_member(self, subject: str) -> bool:
        with self._lock, self._conn:
            cursor = self._conn.execute(
                "DELETE FROM staff_members WHERE subject = ?", (subject.strip(),)
            )
            if cursor.rowcount == 0:
                return False
            self._conn.execute(
                "DELETE FROM im_identities WHERE principal_subject = ?", (subject.strip(),)
            )
        return True

    # ---------- IM 身份映射 ----------

    def resolve_identity(self, channel: str, im_user_id: str) -> ImIdentity | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT body FROM im_identities WHERE channel = ? AND im_user_id = ?",
                (channel.strip().lower(), im_user_id.strip()),
            ).fetchone()
        return ImIdentity.model_validate_json(row["body"]) if row is not None else None

    def list_identities(self, channel: str | None = None) -> list[ImIdentity]:
        wanted = channel.strip().lower() if channel else None
        with self._lock:
            if wanted is None:
                rows = self._conn.execute(
                    "SELECT body FROM im_identities ORDER BY channel, im_user_id"
                ).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT body FROM im_identities WHERE channel = ? ORDER BY im_user_id",
                    (wanted,),
                ).fetchall()
        return [ImIdentity.model_validate_json(row["body"]) for row in rows]

    def save_identity(self, identity: ImIdentity) -> ImIdentity:
        body = identity.model_dump_json()
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO im_identities"
                " (channel, im_user_id, principal_subject, enabled, body, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?)"
                " ON CONFLICT(channel, im_user_id) DO UPDATE SET"
                " principal_subject = excluded.principal_subject,"
                " enabled = excluded.enabled, body = excluded.body,"
                " updated_at = excluded.updated_at",
                (
                    identity.channel,
                    identity.im_user_id,
                    identity.principal_subject,
                    int(identity.enabled),
                    body,
                    identity.updated_at.isoformat(),
                ),
            )
        return identity

    def delete_identity(self, channel: str, im_user_id: str) -> bool:
        with self._lock, self._conn:
            cursor = self._conn.execute(
                "DELETE FROM im_identities WHERE channel = ? AND im_user_id = ?",
                (channel.strip().lower(), im_user_id.strip()),
            )
        return cursor.rowcount > 0

    # ---------- 解析留痕 ----------

    def record_access(self, entry: ImAccessEntry) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO im_access_log"
                " (at, origin, channel, im_user_id, subject, outcome, reason)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    entry.at.isoformat(),
                    entry.origin,
                    entry.channel,
                    entry.im_user_id,
                    entry.subject,
                    entry.outcome.value,
                    entry.reason,
                ),
            )

    def list_access(self, *, limit: int = 50, rejected_only: bool = False) -> list[ImAccessEntry]:
        limit = max(0, limit)
        if limit == 0:
            return []
        query = (
            "SELECT at, origin, channel, im_user_id, subject, outcome, reason FROM im_access_log"
        )
        params: list[object] = []
        if rejected_only:
            query += " WHERE outcome = ?"
            params.append(ImAccessOutcome.REJECTED.value)
        query += " ORDER BY seq DESC LIMIT ?"
        params.append(limit)
        with self._lock:
            rows = self._conn.execute(query, params).fetchall()
        return [
            ImAccessEntry(
                at=row["at"],
                origin=row["origin"],
                channel=row["channel"],
                im_user_id=row["im_user_id"],
                subject=row["subject"],
                outcome=row["outcome"],
                reason=row["reason"],
            )
            for row in rows
        ]
