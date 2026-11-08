"""流程定义持久化：版本不可变、发布指针、装载时派生发布状态。

这一组用例钉的是 Issue #49 的核心语义。最需要防的不是"读不出来"，而是两种
静默行为：覆盖已有版本（回滚到 v1 却拿到另一个 v1）、以及指针为空时"挑一个
最大的版本凑合跑"。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from workpanda.bootstrap import BOOTSTRAP_ACTOR, build_container
from workpanda.config import Settings
from workpanda.domain.flow import (
    FlowDefinition,
    FlowNotFound,
    FlowNotRunnable,
    FlowRegistry,
    FlowReleaseMissing,
    FlowStatus,
    FlowVersionExists,
    StepDefinition,
    StepKind,
)
from workpanda.infrastructure.memory_store import InMemoryFlowStore
from workpanda.infrastructure.sqlite_flow_store import SqliteFlowStore

FLOW_ID = "demo.flowstore"


def make_flow(version: str, *, status: FlowStatus = FlowStatus.PUBLISHED) -> FlowDefinition:
    return FlowDefinition(
        flow_id=FLOW_ID,
        version=version,
        title=f"测试流程 v{version}",
        status=status,
        steps=(
            StepDefinition(
                step_id="collect_input",
                kind=StepKind.COLLECT,
                title="收集信息",
                required_fields=("subject",),
            ),
        ),
    )


@pytest.fixture
def store(workdir: Path) -> SqliteFlowStore:
    built = SqliteFlowStore(workdir / "flows.db")
    yield built
    built.close()


# ---------- 版本不可变 ----------


def test_adding_same_version_twice_is_rejected(store: SqliteFlowStore) -> None:
    store.add_version(make_flow("1"))

    with pytest.raises(FlowVersionExists):
        store.add_version(make_flow("1"))

    assert [flow.version for flow in store.list_versions(FLOW_ID)] == ["1"]


def test_existing_version_content_is_not_overwritten(store: SqliteFlowStore) -> None:
    """同版本号的另一份内容必须写不进去：否则"回滚到 v1"回滚的是别的东西。"""

    store.add_version(make_flow("1"))
    changed = make_flow("1").model_copy(update={"title": "被我改过的 v1"})

    with pytest.raises(FlowVersionExists):
        store.add_version(changed)

    assert store.list_versions(FLOW_ID)[0].title == "测试流程 v1"


# ---------- 发布指针 ----------


def test_publish_requires_an_existing_version(store: SqliteFlowStore) -> None:
    with pytest.raises(FlowReleaseMissing):
        store.publish(FLOW_ID, "1", actor="testsuite")

    # 失败不能留下半个指针，否则下一次启动会指向一个空洞。
    assert store.published_versions() == {}


def test_rollback_is_moving_the_pointer_back(store: SqliteFlowStore) -> None:
    store.add_version(make_flow("1"))
    store.add_version(make_flow("2"))

    store.publish(FLOW_ID, "2", actor="testsuite")
    assert FlowRegistry.from_store(store).resolve(FLOW_ID).version == "2"

    store.publish(FLOW_ID, "1", actor="testsuite")
    assert FlowRegistry.from_store(store).resolve(FLOW_ID).version == "1"
    # 回滚不删历史：v2 仍在库里，随时可以再发回去。
    assert [flow.version for flow in store.list_versions(FLOW_ID)] == ["1", "2"]


def test_disabled_flow_does_not_fall_back_to_the_highest_version(
    store: SqliteFlowStore,
) -> None:
    store.add_version(make_flow("1"))
    store.add_version(make_flow("2"))
    store.publish(FLOW_ID, "2", actor="testsuite")

    store.disable(FLOW_ID, actor="testsuite")

    registry = FlowRegistry.from_store(store)
    with pytest.raises(FlowNotRunnable):
        registry.resolve(FLOW_ID)
    # 版本还在，只是没有指针指向它。
    assert len(store.list_versions(FLOW_ID)) == 2


# ---------- 装载时派生状态 ----------


def test_publish_status_is_derived_from_the_pointer(store: SqliteFlowStore) -> None:
    """版本内容里写着 published 也不算数——只有指针说了算。"""

    store.add_version(make_flow("1", status=FlowStatus.PUBLISHED))

    registry = FlowRegistry.from_store(store)
    assert registry.versions()[0].status is FlowStatus.DRAFT
    with pytest.raises(FlowNotRunnable):
        registry.resolve(FLOW_ID)


def test_require_still_returns_a_version_after_rollback(store: SqliteFlowStore) -> None:
    """回滚后，在途任务必须还能拿到它创建时的那个版本。

    这是 `require` 与 `resolve` 分工的原因：`require` 只问到不到，`resolve` 才问
    能不能新建。若 `require` 也检查发布状态，一次回滚就会让在途任务的回答与确认
    全部失败。
    """

    store.add_version(make_flow("1"))
    store.add_version(make_flow("2"))
    store.publish(FLOW_ID, "2", actor="testsuite")

    registry = FlowRegistry.from_store(store)
    store.publish(FLOW_ID, "1", actor="testsuite")
    rolled_back = FlowRegistry.from_store(store)

    with pytest.raises(FlowNotRunnable):
        rolled_back.resolve(FLOW_ID, "2")
    assert rolled_back.require(FLOW_ID, "2").version == "2"
    assert registry.require(FLOW_ID, "2").version == "2"


def test_resolving_a_missing_version_does_not_fall_back(store: SqliteFlowStore) -> None:
    """指定了一个不存在的版本时，不能"挑最大的凑合"返回。"""

    store.add_version(make_flow("1"))
    store.add_version(make_flow("3"))
    store.publish(FLOW_ID, "3", actor="testsuite")

    with pytest.raises(FlowNotFound):
        FlowRegistry.from_store(store).resolve(FLOW_ID, "2")


# ---------- 跨重启 ----------


def test_versions_and_pointer_survive_a_restart(workdir: Path) -> None:
    path = workdir / "restart.db"
    first = SqliteFlowStore(path)
    first.add_version(make_flow("1"))
    first.add_version(make_flow("2"))
    first.publish(FLOW_ID, "1", actor="testsuite")
    first.close()

    second = SqliteFlowStore(path)
    try:
        assert second.published_versions() == {FLOW_ID: "1"}
        assert FlowRegistry.from_store(second).resolve(FLOW_ID).version == "1"
    finally:
        second.close()


# ---------- 装配 ----------


def test_demo_flow_is_seeded_and_published_when_enabled(workdir: Path) -> None:
    container = build_container(
        Settings(
            store_backend="sqlite",
            sqlite_path=workdir / "seed-on.db",
            enable_demo_flow=True,
            harness_enabled=False,
        )
    )
    try:
        assert container.flows.resolve("demo.generic").flow_id == "demo.generic"
    finally:
        container.close()


def test_demo_flow_is_disabled_when_the_switch_is_off(workdir: Path) -> None:
    """关掉自检流程必须真的关掉：上一次开着的库里还留着指针，只"不写入"等于没关。"""

    path = workdir / "seed-off.db"
    first = build_container(
        Settings(
            store_backend="sqlite",
            sqlite_path=path,
            enable_demo_flow=True,
            harness_enabled=False,
        )
    )
    try:
        assert first.flows.resolve("demo.generic").flow_id == "demo.generic"
    finally:
        first.close()

    second = build_container(
        Settings(
            store_backend="sqlite",
            sqlite_path=path,
            enable_demo_flow=False,
            harness_enabled=False,
        )
    )
    try:
        with pytest.raises(FlowNotRunnable):
            second.flows.resolve("demo.generic")
    finally:
        second.close()


def test_in_memory_store_matches_the_sqlite_contract() -> None:
    """内存实现必须给出同样的拒绝行为，否则测试会用另一套语义骗过自己。"""

    memory = InMemoryFlowStore()
    memory.add_version(make_flow("1"))

    with pytest.raises(FlowVersionExists):
        memory.add_version(make_flow("1"))
    with pytest.raises(FlowReleaseMissing):
        memory.publish(FLOW_ID, "9", actor="testsuite")

    memory.publish(FLOW_ID, "1", actor="testsuite")
    assert memory.published_versions() == {FLOW_ID: "1"}
    memory.disable(FLOW_ID, actor="testsuite")
    assert memory.published_versions() == {}


# ---------- 发布署名（Issue #61）----------


def test_publish_records_actor(store: SqliteFlowStore) -> None:
    """发布后能从库里读回主体名：不做双人复核时，署名是唯一责任锚点。"""

    store.add_version(make_flow("1"))
    assert store.release_info(FLOW_ID) is None

    store.publish(FLOW_ID, "1", actor="sop.alice")
    info = store.release_info(FLOW_ID)
    assert info is not None
    assert info.published_by == "sop.alice"
    assert info.disabled_by is None
    assert store.published_versions() == {FLOW_ID: "1"}


def test_rollback_overwrites_published_by(store: SqliteFlowStore) -> None:
    """回滚就是一次 publish：published_by 要覆盖成「谁回滚的」。"""

    store.add_version(make_flow("1"))
    store.add_version(make_flow("2"))
    store.publish(FLOW_ID, "2", actor="sop.alice")
    store.publish(FLOW_ID, "1", actor="sop.bob")

    info = store.release_info(FLOW_ID)
    assert info is not None
    assert info.published_by == "sop.bob"
    assert FlowRegistry.from_store(store).resolve(FLOW_ID).version == "1"


def test_disable_records_actor_and_can_be_republished(store: SqliteFlowStore) -> None:
    """停用留下 disabled_by；重新发布要清掉它，不能同时显示两个责任人。"""

    store.add_version(make_flow("1"))
    store.publish(FLOW_ID, "1", actor="sop.alice")
    store.disable(FLOW_ID, actor="sop.bob")

    assert store.published_versions() == {}
    info = store.release_info(FLOW_ID)
    assert info is not None
    assert info.published_by == "sop.alice"
    assert info.disabled_by == "sop.bob"

    store.publish(FLOW_ID, "1", actor="sop.carol")
    info = store.release_info(FLOW_ID)
    assert info is not None
    assert info.published_by == "sop.carol"
    assert info.disabled_by is None
    assert store.published_versions() == {FLOW_ID: "1"}


def test_disabling_twice_keeps_the_first_actor(store: SqliteFlowStore) -> None:
    """重复停用是空动作，不该把署名换成第二个人。"""

    store.add_version(make_flow("1"))
    store.publish(FLOW_ID, "1", actor="sop.alice")
    store.disable(FLOW_ID, actor="sop.bob")
    store.disable(FLOW_ID, actor="sop.eve")
    assert store.release_info(FLOW_ID).disabled_by == "sop.bob"
    assert store.published_versions() == {}


def test_actor_is_keyword_only_and_required(store: SqliteFlowStore) -> None:
    """漏传 actor 必须是 TypeError，绝不能静默记成空串或某个默认值。"""

    store.add_version(make_flow("1"))
    with pytest.raises(TypeError):
        store.publish(FLOW_ID, "1")
    with pytest.raises(TypeError):
        store.publish(FLOW_ID, "1", "sop.alice")
    with pytest.raises(TypeError):
        store.disable(FLOW_ID)
    assert store.published_versions() == {}
    assert store.release_info(FLOW_ID) is None


def test_empty_actor_is_rejected(store: SqliteFlowStore) -> None:
    """空串署名等于没有署名：两个实现都必须在写之前挡下。"""

    store.add_version(make_flow("1"))
    with pytest.raises(ValueError):
        store.publish(FLOW_ID, "1", actor="")
    with pytest.raises(ValueError):
        store.disable(FLOW_ID, actor="   ")
    assert store.published_versions() == {}

    memory = InMemoryFlowStore()
    memory.add_version(make_flow("1"))
    with pytest.raises(ValueError):
        memory.publish(FLOW_ID, "1", actor="")
    with pytest.raises(ValueError):
        memory.disable(FLOW_ID, actor="")


def test_old_schema_migrates_idempotently(workdir: Path) -> None:
    """旧库（无署名列）启动自动加列，旧数据为 NULL 且读取 / 再发布都不报错。"""

    path = workdir / "legacy.db"
    conn = sqlite3.connect(path)
    try:
        conn.executescript(
            "CREATE TABLE flow_definitions ("
            " flow_id TEXT NOT NULL, version TEXT NOT NULL,"
            " body TEXT NOT NULL, created_at TEXT NOT NULL,"
            " PRIMARY KEY (flow_id, version));"
            "CREATE TABLE flow_releases ("
            " flow_id TEXT PRIMARY KEY, published_version TEXT NOT NULL,"
            " updated_at TEXT NOT NULL);"
        )
        conn.execute(
            "INSERT INTO flow_releases (flow_id, published_version, updated_at)"
            " VALUES (?, ?, ?)",
            (FLOW_ID, "legacy", "2026-01-01T00:00:00.000000Z"),
        )
        conn.commit()
    finally:
        conn.close()

    first = SqliteFlowStore(path)
    try:
        info = first.release_info(FLOW_ID)
        assert info is not None
        assert info.published_by is None
        assert info.disabled_by is None
        assert first.published_versions() == {FLOW_ID: "legacy"}
    finally:
        first.close()

    # 再开一次：迁移必须幂等，不能因为列已存在就报错。
    second = SqliteFlowStore(path)
    try:
        assert second.release_info(FLOW_ID).published_by is None
        second.add_version(make_flow("1"))
        second.publish(FLOW_ID, "1", actor="sop.alice")
        assert second.release_info(FLOW_ID).published_by == "sop.alice"
    finally:
        second.close()


def test_in_memory_store_records_actors_like_sqlite() -> None:
    """内存实现给出同样的署名语义，否则测试会用另一套口径骗过自己。"""

    memory = InMemoryFlowStore()
    memory.add_version(make_flow("1"))
    memory.add_version(make_flow("2"))
    memory.publish(FLOW_ID, "1", actor="sop.alice")
    assert memory.release_info(FLOW_ID).published_by == "sop.alice"
    memory.publish(FLOW_ID, "2", actor="sop.bob")
    assert memory.release_info(FLOW_ID).published_by == "sop.bob"
    memory.disable(FLOW_ID, actor="sop.carol")
    assert memory.published_versions() == {}
    info = memory.release_info(FLOW_ID)
    assert info.published_by == "sop.bob"
    assert info.disabled_by == "sop.carol"
    memory.publish(FLOW_ID, "1", actor="sop.dave")
    info = memory.release_info(FLOW_ID)
    assert info.published_by == "sop.dave"
    assert info.disabled_by is None


def test_demo_seed_uses_a_non_human_actor(workdir: Path) -> None:
    """装配期播种是系统动作，署名不能伪装成某位运维的发布。"""

    container = build_container(
        Settings(
            store_backend="sqlite",
            sqlite_path=workdir / "seed-actor.db",
            enable_demo_flow=True,
            harness_enabled=False,
        )
    )
    try:
        info = container.flow_store.release_info("demo.generic")
        assert info is not None
        assert info.published_by == BOOTSTRAP_ACTOR
    finally:
        container.close()
