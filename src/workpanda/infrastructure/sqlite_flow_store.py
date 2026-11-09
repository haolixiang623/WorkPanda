"""流程定义的 SQLite 持久化：版本不可变 + 发布指针。

两条表：

- `flow_definitions`：版本内容，主键 `(flow_id, version)`，**只增不改**；
- `flow_releases`：每个流程一行指针，指向当前发布的版本，并记下发布人 / 停用人。

为什么发布状态单独存成指针，而不是在版本行上加 `status` 列：版本行一旦"可改"，
回滚就只能靠覆盖，历史版本与审计对不上；指针只移动一行，历史版本天然不可变。
停用是**在指针行上打标记**而不是删行：删掉那一行会把"谁发布的、谁停用的"一起
抹掉，而这两条署名正是本轮要留下的东西。没有标记的行才是有效指针。

`published_by` / `disabled_by` 是后加的列：旧库启动时由 `_ensure_columns()` 幂等
补列，旧数据为 `NULL` 且不回填——把历史发布硬塞给某个主体，比留空更糟。

与其它存储共用同一个库文件（`WORKPANDA_SQLITE_PATH`），因为它们的事务语义相同：
一次发布必须整体生效，不能出现"指针移了、版本没写进去"。
"""

import json
import sqlite3
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

# 旧库补列。`CREATE TABLE IF NOT EXISTS` 对已存在的表什么都不做，所以必须显式检查。
# 只加列、不改已有列：迁移本身不能成为一次数据改写的机会。
_COLUMN_MIGRATIONS = {
    "published_by": "ALTER TABLE flow_releases ADD COLUMN published_by TEXT",
    "disabled_by": "ALTER TABLE flow_releases ADD COLUMN disabled_by TEXT",
}


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
        self._ensure_columns()
        self._conn.commit()

    def _ensure_columns(self) -> None:
        """给旧库补上署名列。幂等：每次启动都按实际列名核对一遍。

        用 `PRAGMA table_info` 而不是"试着重试 ALTER 并吞掉异常"：吞异常会把
        "表不存在""库是只读的"一起吞掉，启动看似成功、第一次发布才炸。
        """

        existing = {
            str(row["name"]) for row in self._conn.execute("PRAGMA table_info(flow_releases)")
        }
        for name, statement in _COLUMN_MIGRATIONS.items():
            if name not in existing:
                self._conn.execute(statement)

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
                # 必须回滚再抛：SQLite 在事务里遇到约束冲突只终止这一条语句，事务
                # 与它的写锁都还在。留着它，共用同一库文件的其它存储（回放用例、
                # 任务、留痕）会在 busy_timeout 后全部拿到 `database is locked`，
                # 症状是"建版时多点了一次，之后整台机器存不进去，要重启才好"。
                self._conn.rollback()
                raise FlowVersionExists(
                    f"flow {flow.flow_id!r} version {flow.version!r} already exists"
                ) from exc
            self._conn.commit()

    def publish(self, flow_id: str, version: str, *, actor: str) -> None:
        """移动发布指针并记下署名。目标版本必须已经存在，否则指针会指向一个空洞。

        重新发布（含停用后再发布）会清掉停用标记：此时"谁停用的"已不再是当前状态，
        留着它会让台账同时说"在跑"和"被停用"。
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
        """停用：在指针行上打标记，版本内容一行都不删，发布署名也留着。

        重复停用保持第一次的署名人：状态没有再次改变，就不该产生一个新事实。
        没有指针的流程是无操作（没有指针可标记）。
        """

        actor = require_actor(actor)
        with self._lock:
            self._conn.execute(
                "UPDATE flow_releases SET disabled_by = ?"
                " WHERE flow_id = ? AND disabled_by IS NULL",
                (actor, flow_id),
            )
            self._conn.commit()

    # ---------- 读 ----------

    def published_versions(self) -> dict[str, str]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT flow_id, published_version FROM flow_releases"
                " WHERE disabled_by IS NULL"
            ).fetchall()
        return {row["flow_id"]: row["published_version"] for row in rows}

    def releases(self) -> list[FlowRelease]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT flow_id, published_version, published_by, updated_at, disabled_by"
                " FROM flow_releases ORDER BY flow_id"
            ).fetchall()
        return [
            FlowRelease(
                flow_id=row["flow_id"],
                version=row["published_version"],
                published=row["disabled_by"] is None,
                published_by=row["published_by"],
                updated_at=row["updated_at"],
                disabled_by=row["disabled_by"],
            )
            for row in rows
        ]

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
