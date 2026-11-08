"""发布署名（Issue #61）：发布 / 回滚 / 停用都必须留下主体名。

本系统不做双人复核，署名是这条链路**唯一**的责任锚点，因此这里钉住四件容易悄悄
失效的事：

1. `actor` 是仅关键字必填参数：漏传是类型错误，空串被拒——不会静默写下一行没有
   署名的记录（那和没记录分不开）；
2. 署名只能来自鉴权主体：请求体里的同名字段一律无效；
3. 回滚与停用同样留名，`disabled_by` 记在指针那一行上，重新发布要清掉它；
4. 旧库（建表时还没有这两列）启动自动迁移，旧数据为 `NULL` 且读取不报错。
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from workpanda.application.release import FlowReleaseService
from workpanda.application.replay import FlowReplay
from workpanda.bootstrap import Container, build_container
from workpanda.config import Settings
from workpanda.domain.flow import (
    FlowDefinition,
    FlowStatus,
    StepDefinition,
    StepKind,
)
from workpanda.infrastructure.memory_store import InMemoryFlowStore
from workpanda.infrastructure.replay_cases import InMemoryReplayCaseStore
from workpanda.infrastructure.sqlite_flow_store import SqliteFlowStore
from workpanda.main import create_app

FLOW_ID = "actor.demo"
ADMIN_TOKEN = "admin-secret-0123456789"
VIEWER_TOKEN = "viewer-secret-0123456789"
ADMIN_HEADER = {"X-WorkPanda-Admin-Token": ADMIN_TOKEN}

REPLAY_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "replay_flow.py"


def make_flow(version: str) -> FlowDefinition:
    return FlowDefinition(
        flow_id=FLOW_ID,
        version=version,
        title=f"署名测试流程 v{version}",
        status=FlowStatus.DRAFT,
        steps=(
            StepDefinition(
                step_id="collect",
                kind=StepKind.COLLECT,
                title="收集信息",
                required_fields=("subject",),
            ),
        ),
    )


def _store(backend: str, workdir: Path) -> SqliteFlowStore | InMemoryFlowStore:
    if backend == "sqlite":
        return SqliteFlowStore(workdir / f"{backend}.db")
    return InMemoryFlowStore()


# ---------- 签名约束：必填、仅关键字、不许空串 ----------


def test_publish_requires_a_keyword_only_actor(workdir: Path) -> None:
    store = SqliteFlowStore(workdir / "kw.db")
    try:
        store.add_version(make_flow("1"))
        # 漏传是类型错误：不会静默默认成空串或某个"系统"主体。
        with pytest.raises(TypeError):
            store.publish(FLOW_ID, "1")  # type: ignore[call-arg]
        # 位置传参也不行：署名必须显式命名，读代码时看得见"这里在署名"。
        with pytest.raises(TypeError):
            store.publish(FLOW_ID, "1", "ops-zhang")  # type: ignore[misc]
        assert store.published_versions() == {}
    finally:
        store.close()


def test_release_service_requires_an_actor(workdir: Path) -> None:
    store = SqliteFlowStore(workdir / "service.db")
    try:
        store.add_version(make_flow("1"))
        service = FlowReleaseService(
            flows=store, cases=InMemoryReplayCaseStore(), replay=FlowReplay()
        )
        with pytest.raises(TypeError):
            service.release(FLOW_ID, "1")  # type: ignore[call-arg]
        assert store.published_versions() == {}
    finally:
        store.close()


@pytest.mark.parametrize("backend", ["sqlite", "memory"])
def test_blank_actor_is_rejected(backend: str, workdir: Path) -> None:
    store = _store(backend, workdir)
    try:
        store.add_version(make_flow("1"))
        with pytest.raises(ValueError):
            store.publish(FLOW_ID, "1", actor="   ")
        store.publish(FLOW_ID, "1", actor="ops-zhang")
        with pytest.raises(ValueError):
            store.disable(FLOW_ID, actor="")
        # 被拒的写入没有留下任何痕迹：指针仍按上一次成功的那次生效。
        assert store.published_versions() == {FLOW_ID: "1"}
    finally:
        store.close()


# ---------- 谁移动了指针 ----------


@pytest.mark.parametrize("backend", ["sqlite", "memory"])
def test_publish_records_actor(backend: str, workdir: Path) -> None:
    store = _store(backend, workdir)
    try:
        store.add_version(make_flow("1"))
        store.publish(FLOW_ID, "1", actor="ops-zhang")

        [release] = store.releases()
        assert release.flow_id == FLOW_ID
        assert release.published_version == "1"
        assert release.published_by == "ops-zhang"
        assert release.enabled is True
        assert release.disabled_by is None
    finally:
        store.close()


@pytest.mark.parametrize("backend", ["sqlite", "memory"])
def test_rollback_overwrites_the_signature(backend: str, workdir: Path) -> None:
    """回滚就是移动指针，因此覆盖 `published_by`：一行只记"最后一次操作"。"""

    store = _store(backend, workdir)
    try:
        store.add_version(make_flow("1"))
        store.add_version(make_flow("2"))
        store.publish(FLOW_ID, "2", actor="ops-new")
        store.publish(FLOW_ID, "1", actor="ops-oncall")

        [release] = store.releases()
        assert release.published_version == "1"
        assert release.published_by == "ops-oncall"
        assert store.published_versions() == {FLOW_ID: "1"}
    finally:
        store.close()


@pytest.mark.parametrize("backend", ["sqlite", "memory"])
def test_disable_records_actor_and_republish_clears_it(backend: str, workdir: Path) -> None:
    """停用不删指针行：删掉就没人说得清是谁把这条流程下线的。"""

    store = _store(backend, workdir)
    try:
        store.add_version(make_flow("1"))
        store.publish(FLOW_ID, "1", actor="ops-zhang")
        store.disable(FLOW_ID, actor="ops-duty")

        [release] = store.releases()
        assert release.disabled_by == "ops-duty"
        # 停用前指向哪一版仍然可查：这是审计线索，但它不再算发布指针。
        assert release.published_version == "1"
        assert release.enabled is False
        assert store.published_versions() == {}

        store.publish(FLOW_ID, "1", actor="ops-zhang")
        [release] = store.releases()
        assert release.disabled_by is None
        assert release.published_by == "ops-zhang"
        assert store.published_versions() == {FLOW_ID: "1"}
    finally:
        store.close()


def test_disable_of_never_published_flow_does_not_invent_a_record(workdir: Path) -> None:
    """从未发布过的流程没有指针行可标记；不补写一行"发布记录"来充数。"""

    store = SqliteFlowStore(workdir / "never.db")
    try:
        store.add_version(make_flow("1"))
        store.disable(FLOW_ID, actor="ops-duty")
        assert store.releases() == []
        assert store.published_versions() == {}
    finally:
        store.close()


# ---------- 旧库迁移 ----------

LEGACY_SCHEMA = """
CREATE TABLE flow_definitions (
    flow_id TEXT NOT NULL,
    version TEXT NOT NULL,
    body TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (flow_id, version)
);

CREATE TABLE flow_releases (
    flow_id TEXT PRIMARY KEY,
    published_version TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""


def _seed_legacy_database(path: Path, flow: FlowDefinition) -> None:
    conn = sqlite3.connect(path)
    try:
        conn.executescript(LEGACY_SCHEMA)
        conn.execute(
            "INSERT INTO flow_definitions (flow_id, version, body, created_at)"
            " VALUES (?, ?, ?, ?)",
            (
                flow.flow_id,
                flow.version,
                json.dumps(flow.model_dump(mode="json"), ensure_ascii=False),
                "2026-11-01T00:00:00.000000+00:00",
            ),
        )
        conn.execute(
            "INSERT INTO flow_releases (flow_id, published_version, updated_at)"
            " VALUES (?, ?, ?)",
            (flow.flow_id, flow.version, "2026-11-01T00:00:00.000000+00:00"),
        )
        conn.commit()
    finally:
        conn.close()


def test_legacy_database_is_migrated_and_readable(workdir: Path) -> None:
    """旧库启动自动加列，旧记录 `published_by` 为 `NULL` 且读取不报错。

    刻意不回填成某个默认主体：那时候确实没记录是谁做的，回填会把"没记录"伪装成
    "有署名"——比留空更容易误导复盘。
    """

    path = workdir / "legacy.db"
    _seed_legacy_database(path, make_flow("1"))

    store = SqliteFlowStore(path)
    try:
        assert store.published_versions() == {FLOW_ID: "1"}
        [release] = store.releases()
        assert release.published_by is None
        assert release.disabled_by is None
        assert release.enabled is True
    finally:
        store.close()

    columns = {
        row[1]
        for row in sqlite3.connect(path).execute("PRAGMA table_info(flow_releases)")
    }
    assert {"published_by", "disabled_by"} <= columns


def test_migration_is_idempotent_across_restarts(workdir: Path) -> None:
    path = workdir / "restart.db"
    first = SqliteFlowStore(path)
    first.add_version(make_flow("1"))
    first.publish(FLOW_ID, "1", actor="ops-zhang")
    first.close()

    second = SqliteFlowStore(path)
    try:
        # 第二次启动再跑一遍补列，不能清掉已有署名。
        [release] = second.releases()
        assert release.published_by == "ops-zhang"
    finally:
        second.close()


# ---------- 接口：署名取自已鉴权主体 ----------


def _draft(version: str = "1") -> dict:
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
                "prompt": "请补充主题。",
            }
        ],
    }


@pytest.fixture
def admin_client() -> Iterator[tuple[TestClient, Container]]:
    settings = Settings(
        _env_file=None,
        store_backend="memory",
        enable_demo_flow=False,
        admin_tokens=f"boss:admin:{ADMIN_TOKEN},duty:viewer:{VIEWER_TOKEN}",
        # 本文件验的是署名与指针语义，与识别样例门禁无关（见 test_intent_eval.py）。
        intent_sample_gate=False,
    )
    container = build_container(settings)
    client = TestClient(create_app(container=container))
    yield client, container
    container.close()


def _create(client: TestClient, version: str) -> None:
    response = client.post("/api/admin/flows", headers=ADMIN_HEADER, json=_draft(version))
    assert response.status_code == 201, response.text


def test_forged_published_by_in_request_body_is_ignored(admin_client) -> None:
    """请求体自带 `published_by` 一律无效：可自报的署名等于没有署名。"""

    client, container = admin_client
    _create(client, "1")

    response = client.post(
        f"/api/admin/flows/{FLOW_ID}/publish",
        headers=ADMIN_HEADER,
        json={
            "version": "1",
            "apply": True,
            "published_by": "ceo",
            "disabled_by": "ceo",
        },
    )
    assert response.status_code == 200, response.text
    # 凭据是 boss，请求体自称 ceo：库里记下的必须是有凭据的那个主体。
    assert response.json()["published_by"] == "boss"
    [release] = container.flow_store.releases()
    assert release.published_by == "boss"

    overview = client.get("/api/admin/flows", headers=ADMIN_HEADER).json()
    assert overview[0]["published_by"] == "boss"
    assert overview[0]["disabled_by"] is None

    # 伪造的字段也不能被顺手写进停用列。
    disabled = client.post(
        f"/api/admin/flows/{FLOW_ID}/disable",
        headers=ADMIN_HEADER,
        json={"disabled_by": "ceo"},
    )
    assert disabled.status_code == 200, disabled.text
    assert disabled.json()["disabled_by"] == "boss"
    [release] = container.flow_store.releases()
    assert release.disabled_by == "boss"


def test_dry_run_produces_no_signature(admin_client) -> None:
    """预演不移动指针，因此没有署名——界面不能把它显示成"由某人发布"。"""

    client, container = admin_client
    _create(client, "1")

    response = client.post(
        f"/api/admin/flows/{FLOW_ID}/publish",
        headers=ADMIN_HEADER,
        json={"version": "1", "apply": False},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["published"] is False
    assert body["published_by"] is None
    assert container.flow_store.releases() == []


def test_rollback_and_disable_report_the_actor(admin_client) -> None:
    client, container = admin_client
    _create(client, "1")
    _create(client, "2")
    for version in ("1", "2"):
        assert (
            client.post(
                f"/api/admin/flows/{FLOW_ID}/publish",
                headers=ADMIN_HEADER,
                json={"version": version},
            ).status_code
            == 200
        )

    rolled = client.post(
        f"/api/admin/flows/{FLOW_ID}/rollback",
        headers=ADMIN_HEADER,
        json={"version": "1", "published_by": "ceo"},
    )
    assert rolled.status_code == 200, rolled.text
    assert rolled.json()["published_by"] == "boss"
    [release] = container.flow_store.releases()
    assert release.published_version == "1"
    assert release.published_by == "boss"

    # 署名只挂在指针那一版上：另一版没被谁发布过，不该复制同一份署名。
    overview = client.get("/api/admin/flows", headers=ADMIN_HEADER).json()
    signatures = {item["version"]: item["published_by"] for item in overview}
    assert signatures == {"1": "boss", "2": None}


def test_admin_ui_shows_the_signature() -> None:
    """管理侧界面必须把署名显示出来，否则库里记了也没人看得到。"""

    root = Path(__file__).resolve().parents[1] / "admin-ui"
    source = (root / "src" / "main.ts").read_text(encoding="utf-8")
    built = (root / "dist" / "main.js").read_text(encoding="utf-8")
    for marker in ("published_by", "disabled_by", "署名"):
        assert marker in source, marker
        assert marker in built, marker


# ---------- 运维脚本也不能匿名发布 ----------

def _run_script(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(REPLAY_SCRIPT), *args],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


def test_replay_script_refuses_to_publish_without_actor(workdir: Path) -> None:
    from workpanda.bootstrap import seed_demo_flows

    db = workdir / "script.db"
    store = SqliteFlowStore(db)
    try:
        seed_demo_flows(store, enabled=True)
    finally:
        store.close()

    completed = _run_script(
        "--flow", "demo.generic", "--version", "1", "--sqlite", str(db), "--publish"
    )

    assert completed.returncode == 2, completed.stdout
    assert "署名" in completed.stderr
    check = SqliteFlowStore(db)
    try:
        # 指针仍指向装配期那条记录：拒绝发生在写库之前。
        [release] = check.releases()
        assert release.published_by == "bootstrap"
    finally:
        check.close()
