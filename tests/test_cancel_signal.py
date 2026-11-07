"""跨进程取消信号：另一个进程里正在跑的执行也必须看得见"人不要了"。

进程内的 `CancelRegistry` 只在自己进程里有效。接上共享信号后，取消写入数据库、
执行侧在边界上主动拉取；写不成功不能假装成功，读不出来按"已取消"停下。
"""

from __future__ import annotations

import threading
import time
import uuid
from pathlib import Path
from uuid import UUID

import pytest
from fastapi.testclient import TestClient

from tests.support import DIRECT_FLOW, TEST_FLOWS
from workpanda.api.security import SESSION_HEADER
from workpanda.application.errors import CancelNotDelivered
from workpanda.bootstrap import build_cancel_registry, build_container
from workpanda.config import Settings
from workpanda.domain.models import Task, TaskStatus
from workpanda.harness.cancel import (
    SIGNAL_UNAVAILABLE_REASON,
    Cancelled,
    CancelRegistry,
    CancelSignalError,
)
from workpanda.infrastructure import sqlite_cancel_store
from workpanda.infrastructure.adapters import KeywordIntentRouter, ScriptedExecutor
from workpanda.infrastructure.sqlite_cancel_store import SqliteCancelSignalStore
from workpanda.main import create_app


class FailingSignalStore:
    """共享后端不可用：写与读都失败。"""

    def signal(self, task_id: UUID, reason: str, *, ttl_seconds: float) -> None:
        raise OSError("cancel backend is down")

    def reason_for(self, task_id: UUID) -> str | None:
        raise OSError("cancel backend is down")


class FrozenClock:
    """只替换信号存储模块看到的时钟，不影响 pytest 自己计时。"""

    def __init__(self, value: float) -> None:
        self.value = value

    def time(self) -> float:
        return self.value


def _settings(workdir: Path, **overrides) -> Settings:
    return Settings(
        store_backend="sqlite",
        sqlite_path=workdir / "workpanda.db",
        harness_cancel_backend="sqlite",
        enable_demo_flow=False,
        **overrides,
    )


def _container(settings: Settings):
    return build_container(
        settings,
        flows=[DIRECT_FLOW],
        executor=ScriptedExecutor(),
        router=KeywordIntentRouter(),
    )


def test_sqlite_signal_is_visible_across_connections(workdir: Path) -> None:
    """两个独立连接模拟同机两个进程。"""

    task_id = uuid.uuid4()
    writer = SqliteCancelSignalStore(workdir / "signals.db")
    reader = SqliteCancelSignalStore(workdir / "signals.db")
    try:
        assert reader.reason_for(task_id) is None
        writer.signal(task_id, "user_cancelled", ttl_seconds=30.0)
        assert reader.reason_for(task_id) == "user_cancelled"
        assert reader.reason_for(uuid.uuid4()) is None, "信号不该跨任务扩散"
        writer.clear(task_id)
        assert reader.reason_for(task_id) is None
    finally:
        writer.close()
        reader.close()


def test_expired_signal_does_not_block_a_later_execution(
    workdir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = SqliteCancelSignalStore(workdir / "signals.db")
    task_id = uuid.uuid4()
    try:
        store.signal(task_id, "user_cancelled", ttl_seconds=1.0)
        assert store.reason_for(task_id) == "user_cancelled"
        monkeypatch.setattr(sqlite_cancel_store, "time", FrozenClock(time.time() + 10))
        assert store.reason_for(task_id) is None
    finally:
        store.close()


def test_signal_from_another_registry_is_seen_at_the_next_boundary(workdir: Path) -> None:
    store = SqliteCancelSignalStore(workdir / "signals.db")
    executor_side = CancelRegistry(shared=store, poll_interval=0.02)
    http_side = CancelRegistry(shared=store, poll_interval=0.02)
    task_id = uuid.uuid4()
    try:
        token = executor_side.begin(task_id, "submit")
        assert token.cancelled is False
        assert http_side.cancel_task(task_id, "user_cancelled") == 0
        # 没有本进程内的令牌被置位；执行侧靠下一次边界拉取才发现。
        assert token.cancelled is True
        assert token.reason == "user_cancelled"
        with pytest.raises(Cancelled) as caught:
            token.raise_if_cancelled("before_tool")
        assert "user_cancelled" in str(caught.value)
        executor_side.end(task_id, "submit")
    finally:
        store.close()


def test_signal_written_before_begin_stops_execution_immediately(workdir: Path) -> None:
    store = SqliteCancelSignalStore(workdir / "signals.db")
    http_side = CancelRegistry(shared=store, poll_interval=0.02)
    executor_side = CancelRegistry(shared=store, poll_interval=0.02)
    task_id = uuid.uuid4()
    try:
        http_side.cancel_task(task_id, "user_cancelled")
        token = executor_side.begin(task_id, "submit")
        assert token.cancelled is True, "执行开始前的取消不能被无声丢掉"
        assert token.reason == "user_cancelled"
    finally:
        store.close()


def test_backoff_wait_is_interrupted_by_a_cross_process_signal(workdir: Path) -> None:
    store = SqliteCancelSignalStore(workdir / "signals.db")
    executor_side = CancelRegistry(shared=store, poll_interval=0.02)
    http_side = CancelRegistry(shared=store, poll_interval=0.02)
    task_id = uuid.uuid4()
    token = executor_side.begin(task_id, "submit")
    timer = threading.Timer(0.1, http_side.cancel_task, args=(task_id,))
    timer.start()
    try:
        started = time.monotonic()
        assert token.wait(5.0) is True
        assert time.monotonic() - started < 1.0, "跨进程取消不该等完整个退避"
        assert token.reason == "user_cancelled"
    finally:
        timer.join()
        executor_side.end(task_id, "submit")
        store.close()


def test_unreadable_signal_fails_closed() -> None:
    """共享信号读不出来时停下，而不是赌"大概没有取消"。"""

    registry = CancelRegistry(shared=FailingSignalStore(), poll_interval=0.02)
    token = registry.begin(uuid.uuid4(), "submit")
    assert token.cancelled is True
    assert token.reason == SIGNAL_UNAVAILABLE_REASON


def test_signal_write_failure_is_reported_not_swallowed() -> None:
    registry = CancelRegistry(shared=FailingSignalStore(), poll_interval=0.02)
    with pytest.raises(CancelSignalError):
        registry.cancel_task(uuid.uuid4(), "user_cancelled")


def test_cancel_not_delivered_leaves_task_state_untouched() -> None:
    settings = Settings(store_backend="memory", enable_demo_flow=False)
    container = build_container(
        settings,
        flows=TEST_FLOWS,
        executor=ScriptedExecutor(),
        router=KeywordIntentRouter(),
    )
    client = TestClient(create_app(container=container))
    client.headers.update({SESSION_HEADER: container.signer.issue()})
    subject = container.signer.verify(client.headers[SESSION_HEADER]).subject
    try:
        task = Task(
            conversation_id="conversation-1",
            user_ref=subject,
            flow_id="test.confirm",
            flow_version="1",
        )
        container.store.create(task)
        container.tasks.cancellations = CancelRegistry(shared=FailingSignalStore())

        with pytest.raises(CancelNotDelivered):
            container.tasks.cancel(task.task_id, subject)
        assert container.store.get(task.task_id).status is TaskStatus.CREATED

        response = client.post(f"/api/tasks/{task.task_id}/cancel")
        assert response.status_code == 503
        assert response.json()["code"] == "cancel_not_delivered"
        assert client.get(f"/api/tasks/{task.task_id}").json()["status"] == "created"
        assert container.store.get(task.task_id).status is TaskStatus.CREATED
    finally:
        container.close()


def test_two_containers_share_the_cancel_signal(workdir: Path) -> None:
    settings = _settings(workdir)
    writer = _container(settings)
    reader = _container(settings)
    task_id = uuid.uuid4()
    try:
        assert writer.tasks.cancellations.cancel_task(task_id, "user_cancelled") == 0
        token = reader.cancellations.begin(task_id, "query")
        assert token.cancelled is True
        assert token.reason == "user_cancelled"
    finally:
        writer.close()
        reader.close()


def test_unknown_backend_fails_loudly() -> None:
    with pytest.raises(ValueError, match="unsupported harness_cancel_backend"):
        build_cancel_registry(Settings(harness_cancel_backend="redis"))
