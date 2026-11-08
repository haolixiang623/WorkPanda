"""回放用例存储：内存与 SQLite 两种后端，语义一致。

用例是**数据**，不是代码：它跟流程版本一起存在库里，运维改期望不必发版。两条实现
共用同一套键 `(flow_id, version, case_id)`，因此"换后端结果不一样"这件事在用例层
也不会发生。

与流程版本库（`sqlite_flow_store.py`）的区别：版本内容不可变，用例可以删。期望写错
了必须能改，否则运维会绕开回放直接把版本发出去——那比没有回放更糟。
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from threading import Lock

from workpanda.domain.models import utc_now
from workpanda.domain.paging import canonical_stamp
from workpanda.domain.replay import ReplayCase, ReplayCaseExists

SCHEMA = """
CREATE TABLE IF NOT EXISTS replay_cases (
    flow_id TEXT NOT NULL,
    version TEXT NOT NULL,
    case_id TEXT NOT NULL,
    body TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (flow_id, version, case_id)
);
"""


class InMemoryReplayCaseStore:
    """进程内实现，只用于 `store_backend=memory` 与测试。"""

    def __init__(self) -> None:
        self._items: dict[tuple[str, str, str], ReplayCase] = {}
        self._lock = Lock()

    def close(self) -> None:
        """与 SQLite 实现同形，便于容器统一登记关闭动作。"""

    def add(self, case: ReplayCase) -> None:
        key = (case.flow_id, case.flow_version, case.case_id)
        with self._lock:
            if key in self._items:
                raise ReplayCaseExists(
                    f"replay case {case.case_id!r} for "
                    f"{case.flow_id!r}@{case.flow_version} already exists"
                )
            self._items[key] = case.model_copy()

    def delete(self, flow_id: str, version: str, case_id: str) -> bool:
        with self._lock:
            return self._items.pop((flow_id, version, case_id), None) is not None

    def list(self, flow_id: str, version: str) -> list[ReplayCase]:
        with self._lock:
            items = [
                case.model_copy()
                for (registered_id, registered_version, _), case in sorted(self._items.items())
                if registered_id == flow_id and registered_version == version
            ]
        return items


class SqliteReplayCaseStore:
    """与任务库、流程版本库同文件：发布门禁要读用例与版本，同库才能一次读到底。"""

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

    def add(self, case: ReplayCase) -> None:
        with self._lock:
            try:
                self._conn.execute(
                    "INSERT INTO replay_cases (flow_id, version, case_id, body, created_at)"
                    " VALUES (?, ?, ?, ?, ?)",
                    (
                        case.flow_id,
                        case.flow_version,
                        case.case_id,
                        json.dumps(case.model_dump(mode="json"), ensure_ascii=False),
                        canonical_stamp(utc_now()),
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise ReplayCaseExists(
                    f"replay case {case.case_id!r} for "
                    f"{case.flow_id!r}@{case.flow_version} already exists"
                ) from exc
            self._conn.commit()

    def delete(self, flow_id: str, version: str, case_id: str) -> bool:
        with self._lock:
            cursor = self._conn.execute(
                "DELETE FROM replay_cases WHERE flow_id = ? AND version = ? AND case_id = ?",
                (flow_id, version, case_id),
            )
            self._conn.commit()
            return cursor.rowcount > 0

    def list(self, flow_id: str, version: str) -> list[ReplayCase]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT body FROM replay_cases"
                " WHERE flow_id = ? AND version = ? ORDER BY case_id",
                (flow_id, version),
            ).fetchall()
        # 与流程版本库同一口径：反序列化失败直接抛，不跳过。跳过等于把一条写坏的
        # 用例悄悄从门禁里摘掉，而摘掉之后版本反而更容易被放行。
        return [ReplayCase.model_validate(json.loads(row["body"])) for row in rows]
