"""步骤租约：同一 (task, step) 同时只允许一个执行者，拿不到就拒绝。"""

from __future__ import annotations

import json
import time
import uuid
from pathlib import Path
from typing import Any

import pytest

from workpanda.application.ports import ExecutionOutcome, ExecutionRequest
from workpanda.bootstrap import build_lease_store
from workpanda.config import Settings
from workpanda.harness.clients import ScriptedModelClient, tool_call
from workpanda.harness.executor import HarnessExecutor
from workpanda.harness.lease import InMemoryStepLeaseStore, SqliteStepLeaseStore
from workpanda.harness.models import ModelResponse, ToolSpec
from workpanda.harness.tools import ToolContext, ToolRegistry
from workpanda.harness.trace import InMemoryTraceStore

SUCCESS = json.dumps(
    {"outcome": "succeeded", "summary": "已回显", "evidence": "echo:hi"},
    ensure_ascii=False,
)


class SubmitTool:
    """非幂等提交：并发漏拦截就是一次真实的重复动作。"""

    spec = ToolSpec(
        name="test_submit",
        description="提交单据",
        parameters={
            "type": "object",
            "properties": {"value": {"type": "string"}},
            "required": ["value"],
            "additionalProperties": False,
        },
        idempotent=False,
    )

    def __init__(self) -> None:
        self.invocations = 0

    def invoke(self, arguments: dict[str, Any], context: ToolContext) -> str:
        self.invocations += 1
        return f"echo:{arguments['value']}"


def request_for() -> ExecutionRequest:
    return ExecutionRequest(
        task_id=uuid.uuid4(),
        flow_id="test.flow",
        flow_version="1",
        step_id="submit",
        tool="test_submit",
        instruction="提交",
        arguments={"value": "hi"},
    )


def executor_with(
    client: ScriptedModelClient, tool: SubmitTool, *, leases: Any, **kwargs: Any
) -> HarnessExecutor:
    registry = ToolRegistry()
    registry.register(tool)
    return HarnessExecutor(
        client, registry=registry, deadline_seconds=30, leases=leases, **kwargs
    )


def successful_client() -> ScriptedModelClient:
    return ScriptedModelClient(
        [
            ModelResponse(tool_calls=[tool_call("test_submit", call_id="c1", value="hi")]),
            ModelResponse(text=SUCCESS),
        ]
    )


# ---------- 租约本身 ----------


def test_first_writer_wins_and_release_frees_the_step() -> None:
    store = InMemoryStepLeaseStore()
    task = uuid.uuid4()

    assert store.acquire(task, "submit", "writer-a", 10) is True
    assert store.acquire(task, "submit", "writer-b", 10) is False
    # 错误持有者不能释放别人的锁。
    store.release(task, "submit", "writer-b")
    assert store.acquire(task, "submit", "writer-b", 10) is False

    store.release(task, "submit", "writer-a")
    assert store.acquire(task, "submit", "writer-b", 10) is True


def test_lease_is_scoped_to_task_and_step() -> None:
    store = InMemoryStepLeaseStore()
    task = uuid.uuid4()

    assert store.acquire(task, "submit", "a", 10) is True
    assert store.acquire(uuid.uuid4(), "submit", "b", 10) is True
    assert store.acquire(task, "collect", "c", 10) is True


def test_expired_lease_can_be_reclaimed() -> None:
    """TTL 是崩溃后的逃生口：进程没了也不能让这一步永远卡死。"""

    store = InMemoryStepLeaseStore()
    task = uuid.uuid4()

    assert store.acquire(task, "submit", "dead-writer", 0.01) is True
    time.sleep(0.02)
    assert store.acquire(task, "submit", "new-writer", 10) is True


def test_sqlite_lease_excludes_a_second_process(workdir: Path) -> None:
    path = workdir / "app.db"
    first = SqliteStepLeaseStore(path)
    second = SqliteStepLeaseStore(path)
    task = uuid.uuid4()
    try:
        assert first.acquire(task, "submit", "process-a", 60) is True
        assert second.acquire(task, "submit", "process-b", 60) is False
        second.release(task, "submit", "process-b")
        assert second.acquire(task, "submit", "process-b", 60) is False
        first.release(task, "submit", "process-a")
        assert second.acquire(task, "submit", "process-b", 60) is True
    finally:
        first.close()
        second.close()


def test_sqlite_expired_lease_can_be_reclaimed(workdir: Path) -> None:
    path = workdir / "app.db"
    store = SqliteStepLeaseStore(path)
    task = uuid.uuid4()
    try:
        assert store.acquire(task, "submit", "dead", 0.01) is True
        time.sleep(0.02)
        assert store.acquire(task, "submit", "live", 60) is True
    finally:
        store.close()


def test_lease_backend_follows_configuration(workdir: Path) -> None:
    disabled, _ = build_lease_store(Settings(store_backend="sqlite", sqlite_path=workdir / "a.db"))
    memory, _ = build_lease_store(Settings(harness_lease_enabled=True))
    sqlite_store, closers = build_lease_store(
        Settings(
            harness_lease_enabled=True,
            store_backend="sqlite",
            sqlite_path=workdir / "a.db",
        )
    )
    try:
        assert disabled is None
        assert isinstance(memory, InMemoryStepLeaseStore)
        assert isinstance(sqlite_store, SqliteStepLeaseStore)
    finally:
        for closer in closers:
            closer()


# ---------- 接进执行器 ----------


def test_busy_step_is_refused_without_calling_model_or_tool() -> None:
    store = InMemoryStepLeaseStore()
    traces = InMemoryTraceStore()
    request = request_for()
    assert store.acquire(request.task_id, request.step_id, "another-writer", 60) is True
    tool = SubmitTool()
    client = successful_client()
    executor = executor_with(client, tool, leases=store, traces=traces)

    result = executor.execute(request)

    assert result.outcome is ExecutionOutcome.UNCONFIRMED
    assert result.error_code == "checkpoint_busy"
    assert result.output["stop_reason"] == "refused"
    assert client.calls == [], "占用状态下连模型都不能调"
    assert tool.invocations == 0, "占用状态下绝不能产生副作用"
    assert traces.list(limit=1)[0].error_code == "checkpoint_busy"
    # 拒绝者不能把别人的租约放掉。
    assert store.acquire(request.task_id, request.step_id, "third", 60) is False


def test_lease_is_released_after_a_successful_run() -> None:
    store = InMemoryStepLeaseStore()
    tool = SubmitTool()
    result = executor_with(successful_client(), tool, leases=store).execute(request_for())

    assert result.outcome is ExecutionOutcome.SUCCEEDED
    assert len(store) == 0, "完成后不能把这一步永久锁住"


def test_lease_is_released_even_when_the_result_is_unconfirmed() -> None:
    """没有结论时断点还在，后续那次执行应该能接着跑。"""

    store = InMemoryStepLeaseStore()
    tool = SubmitTool()
    client = ScriptedModelClient([ModelResponse(text="") for _ in range(2)])
    result = executor_with(client, tool, leases=store).execute(request_for())

    assert result.outcome is ExecutionOutcome.UNCONFIRMED
    assert len(store) == 0


def test_unknown_lease_state_fails_closed() -> None:
    """存储异常时"抢不到"和"抢到了"无法区分，必须按抢不到处理。"""

    class BrokenLeases:
        def __init__(self) -> None:
            self.released = 0

        def acquire(self, *args: Any, **kwargs: Any) -> bool:
            raise RuntimeError("lease store unavailable")

        def release(self, *args: Any, **kwargs: Any) -> None:
            self.released += 1

    leases = BrokenLeases()
    tool = SubmitTool()
    client = successful_client()
    result = executor_with(client, tool, leases=leases).execute(request_for())

    assert result.outcome is ExecutionOutcome.UNCONFIRMED
    assert result.error_code == "checkpoint_busy"
    assert client.calls == []
    assert tool.invocations == 0
    assert leases.released == 0, "没拿到就不该释放"


def test_pre_cancelled_execution_does_not_take_a_lease() -> None:
    """取消优先于租约：一步都不跑时不需要占用身份。"""

    from workpanda.harness.cancel import CancelRegistry

    registry = CancelRegistry()
    request = request_for()
    registry.cancel_task(request.task_id, "user_cancelled")
    store = InMemoryStepLeaseStore()
    tool = SubmitTool()
    client = successful_client()
    executor = executor_with(client, tool, leases=store, cancellations=registry)

    result = executor.execute(request)

    assert result.outcome is ExecutionOutcome.UNCONFIRMED
    assert result.error_code == "cancelled"
    assert client.calls == []
    assert len(store) == 0


@pytest.mark.parametrize("ttl", [0, 30])
def test_ttl_configuration_is_honoured(ttl: int) -> None:
    """显式 TTL 与自动推导都只影响租约时长，不改变执行结论。"""

    store = InMemoryStepLeaseStore()
    tool = SubmitTool()
    executor = executor_with(successful_client(), tool, leases=store, lease_ttl_seconds=ttl)

    assert executor.execute(request_for()).outcome is ExecutionOutcome.SUCCEEDED
    assert len(store) == 0
