"""IM 网关的存储：入站消息认领、发卡去重、投递留痕。

两个后端实现同一份协议（`domain.im_gateway.ImGatewayStore`）：

- `InMemoryImGatewayStore`：默认后端，随任务库一起"重启即丢"；
- `SqliteImGatewayStore`：与任务库同一个文件。同库的理由不是省事——"这条消息
  处理过没有"如果存在别处，重启后重投就会被当成新消息，重复回卡片。

两条认领都用主键冲突来表达"我是第一个"：`INSERT OR IGNORE` 的 `rowcount`
就是判定结果。**不先 SELECT 再 INSERT**——中间的窗口正好是 IM 重投进来的时机。

留痕**不做容量淘汰**：投递记录是"这张卡到底发出去没有"的唯一证据，被自动淘汰
等于事后无法对账。规模控制交给外部清理（见 docs/IM-GATEWAY.md 的已知限制）。
"""

from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path
from threading import Lock
from uuid import UUID

from workpanda.domain.im_gateway import CardDelivery, CardOutcome

SCHEMA = """
CREATE TABLE IF NOT EXISTS im_inbound_messages (
    channel TEXT NOT NULL,
    message_id TEXT NOT NULL,
    at TEXT NOT NULL,
    PRIMARY KEY (channel, message_id)
);

CREATE TABLE IF NOT EXISTS im_notification_marks (
    task_id TEXT NOT NULL,
    status TEXT NOT NULL,
    step_id TEXT NOT NULL,
    at TEXT NOT NULL,
    PRIMARY KEY (task_id, status, step_id)
);

CREATE TABLE IF NOT EXISTS im_card_deliveries (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT NOT NULL,
    task_id TEXT,
    status TEXT NOT NULL,
    recipient TEXT NOT NULL,
    outcome TEXT NOT NULL,
    reason TEXT NOT NULL,
    detail TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS im_card_deliveries_task
    ON im_card_deliveries (task_id, seq DESC);
"""


class InMemoryImGatewayStore:
    """进程内实现。多实例部署时替换成 sqlite 或共享后端，否则重投可能被处理两次。"""

    def __init__(self) -> None:
        self._messages: set[tuple[str, str]] = set()
        self._notifications: set[tuple[str, str, str]] = set()
        self._deliveries: list[CardDelivery] = []
        self._lock = Lock()

    def claim_message(self, channel: str, message_id: str) -> bool:
        key = (channel.strip().lower(), message_id.strip())
        with self._lock:
            if key in self._messages:
                return False
            self._messages.add(key)
            return True

    def has_message(self, channel: str, message_id: str) -> bool:
        with self._lock:
            return (channel.strip().lower(), message_id.strip()) in self._messages

    def claim_notification(self, task_id: UUID, status: str, step_id: str) -> bool:
        key = (str(task_id), status, step_id)
        with self._lock:
            if key in self._notifications:
                return False
            self._notifications.add(key)
            return True

    def record_delivery(self, delivery: CardDelivery) -> None:
        with self._lock:
            self._deliveries.append(delivery.model_copy(deep=True))

    def list_deliveries(
        self, *, task_id: UUID | None = None, limit: int = 50
    ) -> list[CardDelivery]:
        limit = max(0, limit)
        if limit == 0:
            return []
        with self._lock:
            entries = [
                delivery
                for delivery in self._deliveries
                if task_id is None or delivery.task_id == task_id
            ]
        # 新的在前：排障先看最近发出去的。
        return [entry.model_copy(deep=True) for entry in reversed(entries)][:limit]


class SqliteImGatewayStore:
    """SQLite 实现，与任务库同文件、同连接串行写。"""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        if self.path.parent and str(self.path.parent) not in {"", "."}:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = Lock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        # 与其它 store 同口径：短暂写冲突应等待，而不是把一条投递记录判失败。
        self._conn.execute("PRAGMA busy_timeout = 3000")
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def claim_message(self, channel: str, message_id: str) -> bool:
        with self._lock, self._conn:
            cursor = self._conn.execute(
                "INSERT OR IGNORE INTO im_inbound_messages (channel, message_id, at)"
                " VALUES (?, ?, ?)",
                (channel.strip().lower(), message_id.strip(), datetime.now().isoformat()),
            )
        return cursor.rowcount > 0

    def has_message(self, channel: str, message_id: str) -> bool:
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM im_inbound_messages WHERE channel = ? AND message_id = ?",
                (channel.strip().lower(), message_id.strip()),
            ).fetchone()
        return row is not None

    def claim_notification(self, task_id: UUID, status: str, step_id: str) -> bool:
        with self._lock, self._conn:
            cursor = self._conn.execute(
                "INSERT OR IGNORE INTO im_notification_marks (task_id, status, step_id, at)"
                " VALUES (?, ?, ?, ?)",
                (str(task_id), status, step_id, datetime.now().isoformat()),
            )
        return cursor.rowcount > 0

    def record_delivery(self, delivery: CardDelivery) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO im_card_deliveries"
                " (at, task_id, status, recipient, outcome, reason, detail)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    delivery.at.isoformat(),
                    str(delivery.task_id) if delivery.task_id is not None else None,
                    delivery.status,
                    delivery.recipient,
                    delivery.outcome.value,
                    delivery.reason,
                    delivery.detail,
                ),
            )

    def list_deliveries(
        self, *, task_id: UUID | None = None, limit: int = 50
    ) -> list[CardDelivery]:
        limit = max(0, limit)
        if limit == 0:
            return []
        query = (
            "SELECT at, task_id, status, recipient, outcome, reason, detail"
            " FROM im_card_deliveries"
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
            CardDelivery(
                at=row["at"],
                task_id=UUID(row["task_id"]) if row["task_id"] else None,
                status=row["status"],
                recipient=row["recipient"],
                outcome=CardOutcome(row["outcome"]),
                reason=row["reason"],
                detail=row["detail"],
            )
            for row in rows
        ]
