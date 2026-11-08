"""流程定义的 SQLite 持久化：版本不可变 + 发布指针。

两条表：

- `flow_definitions`：版本内容，主键 `(flow_id, version)`，**只增不改**；
- `flow_releases`：每个流程一行指针，指向当前发布的版本。

为什么发布状态单独存成指针，而不是在版本行上加 `status` 列：版本行一旦"可改"，
回滚就只能靠覆盖，历史版本与审计对不上；指针只移动一行，历史版本天然不可变。
停用是把指针行标记为"已停用"（`disabled_by` 非空），历史版本仍在，回滚就是把指针移回去。

指针行同时是**发布记录**：`published_by` / `disabled_by` 记录谁移动的指针。停用
不删行——删掉就没人说得清"是谁把这条流程下线的"，因此停用只把 `disabled_by`
写上，`published_version` 保留"停用前指向哪一版"作为审计线索。`disabled_by`
非空的行不算发布指针（见 `published_versions`）。

与其它存储共用同一个库文件（`WORKPANDA_SQLITE_PATH`），因为它们的事务语义相同：
一次发布必须整体生效，不能出现"指针移了、版本没写进去"。
"""

import json
import sqlite3
from datetime import datetime
from pathlib import Path
from threading import Lock

from workpanda.domain.flow import (
    FlowDefinition,
    FlowRelease,
    FlowReleaseMissing,
    FlowVersionExists,
    require_actor,
    validate_flow,
)
from workpanda.domain.models import utc_now
from workpanda.domain.paging import canonical_stamp

SCHEMA = """
CREATE TABLE IF NOT EXISTS flow_definitions (
    flow_id TEXT NOT NULL,
    version TEXT NOT NULL,
    body TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (flow_id, version)
);

CREATE TABLE IF NOT EXISTS flow_releases (
    flow_id TEXT PRIMARY KEY,
    published_version TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    published_by TEXT,
    disabled_by TEXT
);
"""


def _ensure_columns(conn: sqlite3.Connection) -> None:
    """老库补列。`CREATE TABLE IF NOT EXISTS` 不会修改已存在的表。

    补出来的列对旧数据是 `NULL`：那时候确实没有记录是谁做的，**不回填**成
    "system" 之类的默认主体——回填会把"没记录"伪装成"有署名"，比留空更糟。
    """

    existing = {row["name"] for row in conn.execute("PRAGMA table_info(flow_releases)")}
    for column in ("published_by", "disabled_by"):
        if column not in existing:
            conn.execute(f"ALTER TABLE flow_releases ADD COLUMN {column} TEXT")


def _release(row: sqlite3.Row) -> FlowRelease:
    return FlowRelease(
        flow_id=row["flow_id"],
        published_version=row["published_version"],
        updated_at=datetime.fromisoformat(row["updated_at"]),
        published_by=row["published_by"],
        disabled_by=row["disabled_by"],
    )


class SqliteFlowStore:
    """流程版本库。所有写操作串行，读走同一个连接。"""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        if self.path.parent and str(self.path.parent) not in {"", "."}:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = Lock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        # 与任务库同文件：短暂写冲突应等待，而不是把"发布"直接判失败。
        self._conn.execute("PRAGMA busy_timeout = 3000")
        self._conn.executescript(SCHEMA)
        # 旧库（本次改动之前建的）没有 published_by / disabled_by：CREATE TABLE
        # IF NOT EXISTS 不会补列，必须显式迁移。放在 commit 之前，一次事务完成。
        _ensure_columns(self._conn)
        self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ---------- 写 ----------

    def add_version(self, flow: FlowDefinition) -> None:
        """新增版本。已存在即报错——覆盖会让"回滚到 v1"变成回滚到"另一个 v1"。"""

        validate_flow(flow)
        with self._lock:
            try:
                self._conn.execute(
                    "INSERT INTO flow_definitions (flow_id, version, body, created_at)"
                    " VALUES (?, ?, ?, ?)",
                    (
                        flow.flow_id,
                        flow.version,
                        json.dumps(flow.model_dump(mode="json"), ensure_ascii=False),
                        canonical_stamp(utc_now()),
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise FlowVersionExists(
                    f"flow {flow.flow_id!r} version {flow.version!r} already exists"
                ) from exc
            self._conn.commit()

    def publish(self, flow_id: str, version: str, *, actor: str) -> None:
        """移动发布指针。目标版本必须已经存在，否则指针会指向一个空洞。

        重新发布一条已停用的流程会清掉 `disabled_by`：指回指针就等于重新上线，
        留着一个"已停用"的标记会让下一次读取又把它当成没上线。
        """

        actor = require_actor(actor)
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM flow_definitions WHERE flow_id = ? AND version = ?",
                (flow_id, version),
            ).fetchone()
            if row is None:
                raise FlowReleaseMissing(
                    f"flow {flow_id!r} version {version!r} is not registered; "
                    "先写入版本再发布"
                )
            self._conn.execute(
                "INSERT INTO flow_releases"
                " (flow_id, published_version, updated_at, published_by, disabled_by)"
                " VALUES (?, ?, ?, ?, NULL)"
                " ON CONFLICT(flow_id) DO UPDATE SET"
                " published_version = excluded.published_version,"
                " updated_at = excluded.updated_at,"
                " published_by = excluded.published_by,"
                " disabled_by = NULL",
                (flow_id, version, canonical_stamp(utc_now()), actor),
            )
            self._conn.commit()

    def disable(self, flow_id: str, *, actor: str) -> None:
        """停用：把指针标记为已停用并记下主体。版本内容一行都不删。

        不删指针行，否则"是谁把这条流程下线的"就没有落点了。从未发布过的流程没有
        指针行可标记，此时无事可做——**也不补写**一行：那会凭空造出一条"发布记录"，
        而它对应的版本指针从未存在过。
        """

        actor = require_actor(actor)
        with self._lock:
            self._conn.execute(
                "UPDATE flow_releases SET disabled_by = ?, updated_at = ? WHERE flow_id = ?",
                (actor, canonical_stamp(utc_now()), flow_id),
            )
            self._conn.commit()

    # ---------- 读 ----------

    def releases(self) -> list[FlowRelease]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT flow_id, published_version, updated_at, published_by, disabled_by"
                " FROM flow_releases ORDER BY flow_id"
            ).fetchall()
        return [_release(row) for row in rows]

    def published_versions(self) -> dict[str, str]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT flow_id, published_version FROM flow_releases"
                " WHERE disabled_by IS NULL"
            ).fetchall()
        return {row["flow_id"]: row["published_version"] for row in rows}

    def list_versions(self, flow_id: str | None = None) -> list[FlowDefinition]:
        with self._lock:
            if flow_id is None:
                rows = self._conn.execute(
                    "SELECT body FROM flow_definitions ORDER BY flow_id, version"
                ).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT body FROM flow_definitions WHERE flow_id = ? ORDER BY version",
                    (flow_id,),
                ).fetchall()
        # 只做反序列化，不再校验：写入口已经校验过。这里抛错说明库被外部改过，
        # 应当直接失败而不是跳过——跳过等于把一条跑不起来的流程悄悄藏起来。
        return [FlowDefinition.model_validate(json.loads(row["body"])) for row in rows]
