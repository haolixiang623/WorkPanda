"""流程定义持久化：版本不可变、发布指针、装载时派生发布状态。

这一组用例钉的是 Issue #49 的核心语义。最需要防的不是"读不出来"，而是两种
静默行为：覆盖已有版本（回滚到 v1 却拿到另一个 v1）、以及指针为空时"挑一个
最大的版本凑合跑"。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from workpanda.bootstrap import build_container
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
# 发布 / 停用的署名主体。存储层要求仅关键字必填，测试统一用这个假主体。
TEST_ACTOR = "tester"


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
        store.publish(FLOW_ID, "1", actor=TEST_ACTOR)

    # 失败不能留下半个指针，否则下一次启动会指向一个空洞。
    assert store.published_versions() == {}


def test_rollback_is_moving_the_pointer_back(store: SqliteFlowStore) -> None:
    store.add_version(make_flow("1"))
    store.add_version(make_flow("2"))

    store.publish(FLOW_ID, "2", actor=TEST_ACTOR)
    assert FlowRegistry.from_store(store).resolve(FLOW_ID).version == "2"

    store.publish(FLOW_ID, "1", actor=TEST_ACTOR)
    assert FlowRegistry.from_store(store).resolve(FLOW_ID).version == "1"
    # 回滚不删历史：v2 仍在库里，随时可以再发回去。
    assert [flow.version for flow in store.list_versions(FLOW_ID)] == ["1", "2"]


def test_disabled_flow_does_not_fall_back_to_the_highest_version(
    store: SqliteFlowStore,
) -> None:
    store.add_version(make_flow("1"))
    store.add_version(make_flow("2"))
    store.publish(FLOW_ID, "2", actor=TEST_ACTOR)

    store.disable(FLOW_ID, actor=TEST_ACTOR)

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
    store.publish(FLOW_ID, "2", actor=TEST_ACTOR)

    registry = FlowRegistry.from_store(store)
    store.publish(FLOW_ID, "1", actor=TEST_ACTOR)
    rolled_back = FlowRegistry.from_store(store)

    with pytest.raises(FlowNotRunnable):
        rolled_back.resolve(FLOW_ID, "2")
    assert rolled_back.require(FLOW_ID, "2").version == "2"
    assert registry.require(FLOW_ID, "2").version == "2"


def test_resolving_a_missing_version_does_not_fall_back(store: SqliteFlowStore) -> None:
    """指定了一个不存在的版本时，不能"挑最大的凑合"返回。"""

    store.add_version(make_flow("1"))
    store.add_version(make_flow("3"))
    store.publish(FLOW_ID, "3", actor=TEST_ACTOR)

    with pytest.raises(FlowNotFound):
        FlowRegistry.from_store(store).resolve(FLOW_ID, "2")


# ---------- 跨重启 ----------


def test_versions_and_pointer_survive_a_restart(workdir: Path) -> None:
    path = workdir / "restart.db"
    first = SqliteFlowStore(path)
    first.add_version(make_flow("1"))
    first.add_version(make_flow("2"))
    first.publish(FLOW_ID, "1", actor=TEST_ACTOR)
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
        memory.publish(FLOW_ID, "9", actor=TEST_ACTOR)

    memory.publish(FLOW_ID, "1", actor=TEST_ACTOR)
    assert memory.published_versions() == {FLOW_ID: "1"}
    memory.disable(FLOW_ID, actor=TEST_ACTOR)
    assert memory.published_versions() == {}
