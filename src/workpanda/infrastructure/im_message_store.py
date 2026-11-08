"""入站消息幂等登记。

IM 平台会重投：我们回得慢、连接被中间设备掐断、对端超时，都会让同一条消息第二次
到达。判重必须是**存储层的原子操作**，不能靠"先查再写"——那中间正好是第二个请求
插进来的窗口，而这个窗口在重投密集时一定会被踩到。

两个后端语义一致：

- `InMemoryImMessageStore`：默认后端，重启即丢。适合单进程试点；重启后若平台还在
  重投，会重新处理一次（无副作用，因为这一层不做业务动作）。
- `SqliteImMessageStore`：与任务库同文件。`INSERT ... ON CONFLICT DO NOTHING` 由
  SQLite 保证原子，`rowcount` 就是"我是不是第一个"。

刻意不做容量淘汰：这是"这条消息处理过没有"的证据，被淘汰一条就等于允许对方再投一次。
规模控制交给外部清理。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from threading import Lock

from workpanda.domain.im_gateway import ImInboundRecord

SCHEMA = """
CREATE TABLE IF NOT EXISTS im_inbound_messages (
    channel TEXT NOT NULL,
    message_id TEXT NOT NULL,
    at REAL NOT NULL,
    body TEXT NOT NULL,
    PRIMARY KEY (channel, message_id)
);

CREATE INDEX IF NOT EXISTS im_inbound_messages_at
    ON im_inbound_messages (at DESC);
"""


class InMemoryImMessageStore:
    def __init__(self) -> None:
        self._records: dict[tuple[str, str], ImInboundRecord] = {}
        self._lock = Lock()

    def claim(self, record: ImInboundRecord) -> bool:
        key = (record.channel, record.message_id)
        with self._lock:
            if key in self._records:
                return False
            self._records[key] = record.model_copy(deep=True)
            return True

    def complete(self, record: ImInboundRecord) -> None:
        key = (record.channel, record.message_id)
        with self._lock:
            if key in self._records:
                self._records[key] = record.model_copy(deep=True)

    def get(self, channel: str, message_id: str) -> ImInboundRecord | None:
        with self._lock:
            record = self._records.get((channel, message_id))
        return record.model_copy(deep=True) if record is not None else None

    def count(self) -> int:
        with self._lock:
            return len(self._records)


class SqliteImMessageStore:
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

    def claim(self, record: ImInboundRecord) -> bool:
        with self._lock, self._conn:
            cursor = self._conn.execute(
                "INSERT INTO im_inbound_messages (channel, message_id, at, body)"
                " VALUES (?, ?, ?, ?) ON CONFLICT(channel, message_id) DO NOTHING",
                (record.channel, record.message_id, record.at, record.model_dump_json()),
            )
            # 条件写在一条语句里：并发时数据库自己裁决谁是第一个，
            # 不需要应用层再加一把锁（那把锁在多进程部署里根本无效）。
            return cursor.rowcount > 0

    def complete(self, record: ImInboundRecord) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE im_inbound_messages SET body = ? WHERE channel = ? AND message_id = ?",
                (record.model_dump_json(), record.channel, record.message_id),
            )

    def get(self, channel: str, message_id: str) -> ImInboundRecord | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT body FROM im_inbound_messages WHERE channel = ? AND message_id = ?",
                (channel, message_id),
            ).fetchone()
        return ImInboundRecord.model_validate_json(row["body"]) if row is not None else None

    def count(self) -> int:
        with self._lock:
            row = self._conn.execute("SELECT COUNT(*) AS total FROM im_inbound_messages").fetchone()
        return int(row["total"])
