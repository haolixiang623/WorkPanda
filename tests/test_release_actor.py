"""发布 / 回滚 / 停用的署名（Issue #61）。

本轮不做双人复核，因此署名是"谁把这一版推上线"唯一的责任锚点。这组用例按验收
标准逐条钉住：

- `actor` 是**仅关键字必填**参数：漏传是类型错误，空串/纯空白被拒，不会静默
  存成空署名；
- 发布后能读到主体名，回滚同样留下 `published_by`；
- 停用留下 `disabled_by`，且**不删行**——删行会把发布署名一起抹掉；
- 伪造请求体里的 `published_by` / `actor` 无效：署名只取已鉴权的管理主体；
- 旧库（无该列）启动自动迁移，旧数据为 `NULL` 且读取不报错，也不回填；
- 运维侧只读接口能查到署名（#61 补充验收：署名的可用范围不能只有管理侧一半）。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tests.support import TEST_FLOWS
from workpanda.bootstrap import Container, build_container
from workpanda.config import Settings
from workpanda.domain.flow import (
    FlowDefinition,
    FlowStatus,
    FlowStore,
    StepDefinition,
    StepKind,
)
from workpanda.infrastructure.memory_store import InMemoryFlowStore
from workpanda.infrastructure.sqlite_flow_store import SqliteFlowStore
from workpanda.main import create_app

FLOW_ID = "demo.actor"

# 旧库的 `flow_releases`：只有 #61 之前那三列。用它验证启动时的幂等补列。
LEGACY_RELEASES = """
CREATE TABLE flow_releases (
    flow_id TEXT PRIMARY KEY,
    published_version TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""

ADMIN_TOKEN = "admin-alice-secret-0123456789"
SECOND_ADMIN_TOKEN = "admin-bob-secret-0123456789"
VIEWER_TOKEN = "viewer-daisy-secret-0123456789"
OPS_ADMIN_TOKEN = "ops-audit-secret-0123456789"
OPS_VIEWER_TOKEN = "ops-duty-secret-0123456789"

ADMIN_HEADER = {"X-WorkPanda-Admin-Token": ADMIN_TOKEN}
SECOND_ADMIN_HEADER = {"X-WorkPanda-Admin-Token": SECOND_ADMIN_TOKEN}
VIEWER_HEADER = {"X-WorkPanda-Admin-Token": VIEWER_TOKEN}
OPS_ADMIN_HEADER = {"X-WorkPanda-Ops-Token": OPS_ADMIN_TOKEN}
OPS_VIEWER_HEADER = {"X-WorkPanda-Ops-Token": OPS_VIEWER_TOKEN}


def make_flow(version: str, *, flow_id: str = FLOW_ID) -> FlowDefinition:
    return FlowDefinition(
        flow_id=flow_id,
        version=version,
        title=f"署名测试流程 v{version}",
        status=FlowStatus.DRAFT,
        steps=(
            StepDefinition(
                step_id="collect_input",
                kind=StepKind.COLLECT,
                title="收集信息",
                required_fields=("subject",),
            ),
        ),
    )


@pytest.fixture(params=["sqlite", "memory"])
def store(request: pytest.FixtureRequest, workdir: Path) -> Iterator[FlowStore]:
    """两种后端跑同一组语义断言：内存实现不能靠另一套真相骗过自己。"""

    if request.param == "sqlite":
        built: FlowStore = SqliteFlowStore(workdir / "flows.db")
        yield built
        built.close()
    else:
        yield InMemoryFlowStore()


# ---------- 存储层：署名 ----------


def test_publish_records_actor(store: FlowStore) -> None:
    store.add_version(make_flow("1"))

    store.publish(FLOW_ID, "1", actor="ops.alice")

    release = store.releases()[0]
    assert (release.flow_id, release.version) == (FLOW_ID, "1")
    assert release.published is True
    assert release.published_by == "ops.alice"
    assert release.disabled_by is None
    assert release.updated_at != ""


def test_actor_is_keyword_only_and_required(store: FlowStore) -> None:
    store.add_version(make_flow("1"))

    # 漏传、按位置传都是类型错误：签名是唯一的守卫，不留一个默认空串。
    with pytest.raises(TypeError):
        store.publish(FLOW_ID, "1")  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        store.publish(FLOW_ID, "1", "ops.alice")  # type: ignore[misc]

    # 空白署名与"没有署名"无法区分，必须拒绝。
    for blank in ("", "   ", "\n"):
        with pytest.raises(ValueError):
            store.publish(FLOW_ID, "1", actor=blank)

    assert store.published_versions() == {}
    assert store.releases() == []


def test_disable_records_actor_without_deleting_the_row(store: FlowStore) -> None:
    store.add_version(make_flow("1"))
    store.publish(FLOW_ID, "1", actor="ops.alice")

    store.disable(FLOW_ID, actor="ops.bob")

    assert store.published_versions() == {}
    release = store.releases()[0]
    # 指针行还在：版本与发布人都保留，否则"谁发布的"会随停用一起消失。
    assert release.version == "1"
    assert release.published is False
    assert release.published_by == "ops.alice"
    assert release.disabled_by == "ops.bob"


def test_repeated_disable_keeps_the_first_actor(store: FlowStore) -> None:
    store.add_version(make_flow("1"))
    store.publish(FLOW_ID, "1", actor="ops.alice")
    store.disable(FLOW_ID, actor="ops.bob")

    store.disable(FLOW_ID, actor="ops.carol")

    # 状态没有再次改变，就不该改写"是谁停用的"。
    assert store.releases()[0].disabled_by == "ops.bob"


def test_disable_of_never_published_flow_is_a_no_op(store: FlowStore) -> None:
    store.add_version(make_flow("1"))

    store.disable(FLOW_ID, actor="ops.bob")

    assert store.releases() == []


def test_republish_after_disable_clears_the_marker(store: FlowStore) -> None:
    store.add_version(make_flow("1"))
    store.publish(FLOW_ID, "1", actor="ops.alice")
    store.disable(FLOW_ID, actor="ops.bob")

    store.publish(FLOW_ID, "1", actor="ops.carol")

    release = store.releases()[0]
    assert release.published is True
    assert release.published_by == "ops.carol"
    # 重新发布后"曾被停用"不再是当前状态，留着会同时说"在跑"和"被停用"。
    assert release.disabled_by is None
    assert store.published_versions() == {FLOW_ID: "1"}


def test_rollback_updates_the_publisher(store: FlowStore) -> None:
    store.add_version(make_flow("1"))
    store.add_version(make_flow("2"))
    store.publish(FLOW_ID, "2", actor="ops.alice")

    # 回滚走的是同一个 publish，因此留下的是"谁把它退回来的"。
    store.publish(FLOW_ID, "1", actor="ops.bob")

    release = store.releases()[0]
    assert (release.version, release.published_by) == ("1", "ops.bob")


# ---------- 旧库迁移 ----------


def test_legacy_database_is_migrated_without_backfilling(workdir: Path) -> None:
    path = workdir / "legacy.db"
    legacy = sqlite3.connect(path)
    try:
        legacy.executescript(LEGACY_RELEASES)
        legacy.execute(
            "INSERT INTO flow_releases (flow_id, published_version, updated_at)"
            " VALUES (?, ?, ?)",
            ("demo.legacy", "1", "2026-01-01T00:00:00Z"),
        )
        legacy.commit()
    finally:
        legacy.close()

    store = SqliteFlowStore(path)
    try:
        # 旧数据照旧可读，署名为 NULL——不回填成某个主体，凭空造一条责任记录更糟。
        assert store.published_versions() == {"demo.legacy": "1"}
        release = store.releases()[0]
        assert release.published is True
        assert release.published_by is None
        assert release.disabled_by is None
        assert release.updated_at == "2026-01-01T00:00:00Z"
    finally:
        store.close()

    # 幂等：第二次启动不该重复 ALTER，也不该报错。
    again = SqliteFlowStore(path)
    try:
        assert again.published_versions() == {"demo.legacy": "1"}
    finally:
        again.close()


def test_migrated_database_accepts_attribution(workdir: Path) -> None:
    path = workdir / "legacy-write.db"
    legacy = sqlite3.connect(path)
    try:
        legacy.executescript(LEGACY_RELEASES)
        legacy.commit()
    finally:
        legacy.close()

    store = SqliteFlowStore(path)
    try:
        store.add_version(make_flow("1"))
        store.publish(FLOW_ID, "1", actor="ops.alice")
        store.disable(FLOW_ID, actor="ops.bob")

        release = store.releases()[0]
        assert (release.published_by, release.disabled_by) == ("ops.alice", "ops.bob")
        assert store.published_versions() == {}
    finally:
        store.close()


# ---------- 管理侧接口：署名只取鉴权主体 ----------


@pytest.fixture
def admin_client() -> Iterator[tuple[TestClient, Container]]:
    settings = Settings(
        store_backend="memory",
        enable_demo_flow=False,
        admin_tokens=(
            f"ops.alice:admin:{ADMIN_TOKEN},"
            f"ops.bob:admin:{SECOND_ADMIN_TOKEN},"
            f"ops.daisy:viewer:{VIEWER_TOKEN}"
        ),
        ops_tokens=f"ops.audit:admin:{OPS_ADMIN_TOKEN},ops.duty:viewer:{OPS_VIEWER_TOKEN}",
        # 本文件验的是署名落点，与识别样例门禁无关；这里建的流程没有样例。
        intent_sample_gate=False,
    )
    container = build_container(settings)
    client = TestClient(create_app(container=container))
    yield client, container
    container.close()


def draft_payload(version: str = "1") -> dict:
    return {
        "flow_id": FLOW_ID,
        "version": version,
        "title": f"署名测试流程 {version}",
        "steps": [
            {
                "step_id": "collect",
                "kind": "collect",
                "title": "收集信息",
                "required_fields": ["subject"],
            },
            {
                "step_id": "submit",
                "kind": "execute",
                "title": "提交",
                "tool": "replay_submit",
                "requires_confirmation": True,
            },
        ],
    }


def create_version(client: TestClient, version: str = "1") -> None:
    response = client.post("/api/admin/flows", headers=ADMIN_HEADER, json=draft_payload(version))
    assert response.status_code == 201, response.text


def test_publish_ignores_self_reported_author(
    admin_client: tuple[TestClient, Container],
) -> None:
    """请求体里自报 `published_by` 必须无效：能自报的署名等于没有署名。"""

    client, container = admin_client
    create_version(client)

    response = client.post(
        f"/api/admin/flows/{FLOW_ID}/publish",
        headers=ADMIN_HEADER,
        json={"version": "1", "published_by": "attacker", "actor": "attacker"},
    )

    assert response.status_code == 200, response.text
    assert response.json()["published_by"] == "ops.alice"
    assert container.flow_store is not None
    release = container.flow_store.releases()[0]
    assert release.published_by == "ops.alice"


def test_viewer_cannot_publish_with_a_forged_author(
    admin_client: tuple[TestClient, Container],
) -> None:
    client, container = admin_client
    create_version(client)

    response = client.post(
        f"/api/admin/flows/{FLOW_ID}/publish",
        headers=VIEWER_HEADER,
        json={"version": "1", "published_by": "ops.alice"},
    )

    assert response.status_code == 403
    assert container.flow_store is not None
    assert container.flow_store.releases() == []


def test_dry_run_records_no_author(admin_client: tuple[TestClient, Container]) -> None:
    """预演不移动指针，因此没有"谁发布的"——不能把调用者名字回填进去。"""

    client, container = admin_client
    create_version(client)

    response = client.post(
        f"/api/admin/flows/{FLOW_ID}/publish",
        headers=ADMIN_HEADER,
        json={"version": "1", "apply": False},
    )

    assert response.status_code == 200, response.text
    assert response.json()["published"] is False
    assert response.json()["published_by"] is None
    assert container.flow_store is not None
    assert container.flow_store.releases() == []


def test_rollback_records_the_rolling_back_actor(
    admin_client: tuple[TestClient, Container],
) -> None:
    client, container = admin_client
    create_version(client, "1")
    create_version(client, "2")
    assert (
        client.post(
            f"/api/admin/flows/{FLOW_ID}/publish",
            headers=ADMIN_HEADER,
            json={"version": "2"},
        ).status_code
        == 200
    )

    response = client.post(
        f"/api/admin/flows/{FLOW_ID}/rollback",
        headers=SECOND_ADMIN_HEADER,
        json={"version": "1"},
    )

    assert response.status_code == 200, response.text
    assert response.json()["published_by"] == "ops.bob"
    assert container.flow_store is not None
    release = container.flow_store.releases()[0]
    assert (release.version, release.published_by) == ("1", "ops.bob")


def test_disable_records_the_actor_and_keeps_the_publisher(
    admin_client: tuple[TestClient, Container],
) -> None:
    client, container = admin_client
    create_version(client)
    assert (
        client.post(
            f"/api/admin/flows/{FLOW_ID}/publish",
            headers=ADMIN_HEADER,
            json={"version": "1"},
        ).status_code
        == 200
    )

    response = client.post(
        f"/api/admin/flows/{FLOW_ID}/disable", headers=SECOND_ADMIN_HEADER
    )

    assert response.status_code == 200, response.text
    assert response.json()["disabled_by"] == "ops.bob"
    assert container.flow_store is not None
    release = container.flow_store.releases()[0]
    assert release.published is False
    assert (release.published_by, release.disabled_by) == ("ops.alice", "ops.bob")

    # 管理侧版本列表也要能看出"这一版是被谁停的"，否则界面只剩一个"草稿"。
    overview = client.get("/api/admin/flows", headers=ADMIN_HEADER).json()
    row = next(item for item in overview if item["version"] == "1")
    assert row["published"] is False
    assert row["disabled_by"] == "ops.bob"
    assert row["published_by"] is None


def test_overview_shows_who_published(admin_client: tuple[TestClient, Container]) -> None:
    client, _ = admin_client
    create_version(client)
    assert (
        client.post(
            f"/api/admin/flows/{FLOW_ID}/publish",
            headers=ADMIN_HEADER,
            json={"version": "1"},
        ).status_code
        == 200
    )

    overview = client.get("/api/admin/flows", headers=ADMIN_HEADER).json()

    assert overview[0]["published"] is True
    assert overview[0]["published_by"] == "ops.alice"
    assert overview[0]["updated_at"] != ""


# ---------- 运维侧只读台账 ----------


def test_ops_release_ledger_exposes_attribution(
    admin_client: tuple[TestClient, Container],
) -> None:
    """管理侧看得见、运维侧查不到，等于署名的可用范围只有一半（#61 补充验收）。"""

    client, _ = admin_client
    create_version(client)
    assert (
        client.post(
            f"/api/admin/flows/{FLOW_ID}/publish",
            headers=ADMIN_HEADER,
            json={"version": "1"},
        ).status_code
        == 200
    )

    response = client.get("/api/ops/flows/releases", headers=OPS_ADMIN_HEADER)

    assert response.status_code == 200, response.text
    rows = response.json()
    assert len(rows) == 1
    assert rows[0]["flow_id"] == FLOW_ID
    assert rows[0]["version"] == "1"
    assert rows[0]["published"] is True
    assert rows[0]["published_by"] == "ops.alice"
    assert rows[0]["updated_at"] != ""
    assert rows[0]["disabled_by"] is None


def test_ops_release_ledger_keeps_disabled_rows(
    admin_client: tuple[TestClient, Container],
) -> None:
    """停用的行必须留在台账里：删掉它，"谁停的"就无从查起。"""

    client, _ = admin_client
    create_version(client)
    assert (
        client.post(
            f"/api/admin/flows/{FLOW_ID}/publish",
            headers=ADMIN_HEADER,
            json={"version": "1"},
        ).status_code
        == 200
    )
    assert (
        client.post(
            f"/api/admin/flows/{FLOW_ID}/disable", headers=SECOND_ADMIN_HEADER
        ).status_code
        == 200
    )

    rows = client.get("/api/ops/flows/releases", headers=OPS_ADMIN_HEADER).json()

    assert rows[0]["published"] is False
    assert rows[0]["version"] == "1"
    assert (rows[0]["published_by"], rows[0]["disabled_by"]) == ("ops.alice", "ops.bob")


def test_ops_release_ledger_requires_admin(
    admin_client: tuple[TestClient, Container],
) -> None:
    client, _ = admin_client
    create_version(client)

    assert (
        client.get("/api/ops/flows/releases", headers=OPS_VIEWER_HEADER).status_code
        == 403
    )
    assert client.get("/api/ops/flows/releases").status_code == 401


def test_ops_release_ledger_is_empty_without_a_version_store() -> None:
    """没配置版本库（直接注入流程定义）时返回空数组，而不是 404：

    接口存在性不该随配置变化，否则运维看到 404 会以为是"这版还没上"。
    """

    container = build_container(
        Settings(store_backend="memory", enable_demo_flow=False, ops_token=OPS_ADMIN_TOKEN),
        flows=TEST_FLOWS,
    )
    try:
        client = TestClient(create_app(container=container))
        response = client.get(
            "/api/ops/flows/releases", headers={"X-WorkPanda-Ops-Token": OPS_ADMIN_TOKEN}
        )
        assert response.status_code == 200
        assert response.json() == []
    finally:
        container.close()
