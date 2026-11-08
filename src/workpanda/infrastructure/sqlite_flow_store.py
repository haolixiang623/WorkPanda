"""流程定义的 SQLite 持久化：版本不可变 + 发布指针。

两条表：

- `flow_definitions`：版本内容，主键 `(flow_id, version)`，**只增不改**；
- `flow_releases`：每个流程一行指针，指向当前发布的版本。

为什么发布状态单独存成指针，而不是在版本行上加 `status` 列：版本行一旦"可改"，
回滚就只能靠覆盖，历史版本与审计对不上；指针只移动一行，历史版本天然不可变。
清空指针即停用，历史版本仍在，回滚就是把指针移回去。

与其它存储共用同一个库文件（`WORKPANDA_SQLITE_PATH`），因为它们的事务语义相同：
一次发布必须整体生效，不能出现"指针移了、版本没写进去"。
"""

import json
import sqlite3
from pathlib import Path
from threading import Lock

from workpanda.domain.flow import (
    FlowDefinition,
    FlowReleaseInfo,
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

# 旧库缺的列。新库由 SCHEMA 直接带上，这里只为既有部署做幂等补齐。
_RELEASE_ACTOR_COLUMNS = ("published_by", "disabled_by")


def _ensure_release_actor_columns(conn: sqlite3.Connection) -> None:
    """旧库启动时自动加列，幂等且不改旧数据。

    按列名判断是否已经存在，绝不重复 `ALTER TABLE`；旧行两列自动为 `NULL`，
    读取侧按「迁移前发生的操作没有署名」处理，不猜、不回填。没有用「删表重建」
    是因为那会破坏与其它存储共用同一库文件的事务语义。
    """

    existing = {row["name"] for row in conn.execute("PRAGMA table_info(flow_releases)")}
    for column in _RELEASE_ACTOR_COLUMNS:
        if column not in existing:
            conn.execute(f"ALTER TABLE flow_releases ADD COLUMN {column} TEXT")


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
        _ensure_release_actor_columns(self._conn)
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
        """移动发布指针，并把这次发布 / 回滚的主体记在指针上。

        目标版本必须已经存在，否则指针会指向一个空洞。重新发布要清掉上一次的
        停用署名：否则「停用 → 再发布」之后界面会同时显示两个互相矛盾的责任人。
        """

        require_actor(actor)
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
        """停用指针并记下主体。版本内容一行都不删——回滚与审计都要靠它。

        不再 `DELETE` 那一行：删掉会把 `disabled_by` 一起抹掉，事后就查不到
        「谁停用的」。`published_versions()` 过滤带停用标记的行，对外「没有
        指针」的语义不变；`publish()` 重新发布时清掉标记。加 `disabled_by IS NULL`
        是为了让重复停用幂等——状态没变就不改署名和时间。
        """

        require_actor(actor)
        with self._lock:
            self._conn.execute(
                "UPDATE flow_releases SET disabled_by = ?, updated_at = ?"
                " WHERE flow_id = ? AND disabled_by IS NULL",
                (actor, canonical_stamp(utc_now()), flow_id),
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

    def release_info(self, flow_id: str) -> FlowReleaseInfo | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT published_by, disabled_by FROM flow_releases WHERE flow_id = ?",
                (flow_id,),
            ).fetchone()
        if row is None:
            return None
        return FlowReleaseInfo(
            published_by=row["published_by"],
            disabled_by=row["disabled_by"],
        )

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
