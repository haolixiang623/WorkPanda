"""SQLite 存储：持久化、事件顺序和凭证一次性。"""

import shutil
from collections.abc import Iterator
from pathlib import Path
from uuid import uuid4

import pytest

from workpanda.domain.confirmation import Confirmation, ConfirmationError, parameter_digest
from workpanda.domain.models import EventType, Task, TaskStatus
from workpanda.infrastructure.sqlite_store import SqliteStore


@pytest.fixture
def workdir() -> Iterator[Path]:
    """在仓库内创建临时目录，避免依赖系统临时目录的权限与清理策略。"""

    root = Path(".pytest-tmp") / uuid4().hex
    root.mkdir(parents=True, exist_ok=True)
    try:
        yield root
    finally:
        shutil.rmtree(root, ignore_errors=True)


@pytest.fixture
def store(workdir: Path) -> Iterator[SqliteStore]:
    instance = SqliteStore(workdir / "test.db")
    try:
        yield instance
    finally:
        instance.close()


def make_task() -> Task:
    return Task(
        conversation_id="conversation-1",
        flow_id="test.direct",
        flow_version="1",
        slots={"subject": "测试"},
    )


def test_task_roundtrip_survives_reopen(workdir: Path) -> None:
    path = workdir / "reopen.db"
    first = SqliteStore(path)
    task = first.create(make_task())
    first.save(task.model_copy(update={"status": TaskStatus.COMPLETED}))
    first.close()

    second = SqliteStore(path)
    try:
        restored = second.get(task.task_id)
        assert restored.status is TaskStatus.COMPLETED
        assert restored.slots == {"subject": "测试"}
    finally:
        second.close()


def test_event_sequence_is_monotonic(store: SqliteStore) -> None:
    task = store.create(make_task())

    first = store.append_event(task.task_id, EventType.ACCEPTED, TaskStatus.CREATED)
    second = store.append_event(task.task_id, EventType.STEP_STARTED, TaskStatus.RUNNING)

    assert (first.sequence, second.sequence) == (1, 2)
    assert [event.sequence for event in store.events(task.task_id, after_sequence=1)] == [2]


def test_confirmation_can_only_be_consumed_once(store: SqliteStore) -> None:
    task = store.create(make_task())
    digest = parameter_digest("test.direct", "1", "query", task.slots)
    credential = Confirmation.issue(
        task_id=task.task_id,
        step_id="query",
        flow_version="1",
        digest=digest,
        user_ref=None,
        ttl_seconds=60,
    )
    store.issue(credential)

    store.consume(credential.token)
    with pytest.raises(ConfirmationError):
        store.consume(credential.token)


def test_revoked_confirmation_is_unusable(store: SqliteStore) -> None:
    task = store.create(make_task())
    digest = parameter_digest("test.direct", "1", "query", task.slots)
    credential = Confirmation.issue(
        task_id=task.task_id,
        step_id="query",
        flow_version="1",
        digest=digest,
        user_ref=None,
        ttl_seconds=60,
    )
    store.issue(credential)
    store.revoke(task.task_id, "query")

    with pytest.raises(ConfirmationError):
        store.consume(credential.token)
