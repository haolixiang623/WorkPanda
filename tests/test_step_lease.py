"""步骤租约：同一 (task, step) 同时只允许一个执行者，拿不到就拒绝。"""

from __future__ import annotations

import json
import threading
import time
import uuid
from pathlib import Path
from typing import Any

import pytest

from workpanda.application.ports import ExecutionOutcome, ExecutionRequest
from workpanda.bootstrap import build_lease_store
from workpanda.config import Settings
from workpanda.harness.clients import ScriptedModelClient, tool_call
from workpanda.harness.executor import HarnessExecutor, auto_lease_ttl
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


class GatedClient:
    """第一次补全先卡住，直到测试放行：把"执行仍在进行中"这一刻定死。

    压测（`scripts/stress_tool_threads.py`）量到：一次执行可能比 deadline 多跑
    一段"模型调用时长"，因为已发出的模型调用不会被 deadline 掐断。所以租约
    TTL 只按 deadline 取时，执行还没结束、租约却已过期——第二个写者就此进来。
    """

    def __init__(self) -> None:
        self.started = threading.Event()
        self.proceed = threading.Event()
        self._inner = successful_client()

    def complete(
        self,
        *,
        messages: list[Any],
        tools: list[Any],
        temperature: float = 0.0,
    ) -> ModelResponse:
        self.started.set()
        assert self.proceed.wait(5.0), "测试没放行，客户端一直卡着"
        return self._inner.complete(messages=messages, tools=tools, temperature=temperature)


@pytest.mark.parametrize(("ttl", "stealable"), [(0.01, True), (60, False)])
def test_lease_ttl_must_outlast_the_whole_execution(ttl: float, stealable: bool) -> None:
    """TTL 短于一次执行的实际耗时时，另一个写者能在执行中途抢到同一步骤。

    这是"TTL 必须长于最坏耗时"的可验证版本：短 TTL 下 `stealable` 必须为真
    （问题存在），长 TTL 下必须为假（保护生效）。修坏任何一边都会红。
    """

    store = InMemoryStepLeaseStore()
    client = GatedClient()
    tool = SubmitTool()
    executor = executor_with(client, tool, leases=store, lease_ttl_seconds=ttl)
    request = request_for()

    results: list[Any] = []
    runner = threading.Thread(target=lambda: results.append(executor.execute(request)))
    runner.start()
    try:
        assert client.started.wait(5.0), "执行没进入模型调用"
        # 等过 ttl：租约此时是否还挡得住第二个写者，就是要验证的事。
        time.sleep(0.05 if stealable else 0.0)
        stolen = store.acquire(request.task_id, request.step_id, "second-writer", 60)
        assert stolen is stealable
        if stolen:
            # 抢到的一方要自己还，否则下面的"持有者退出即释放"断言会误报。
            store.release(request.task_id, request.step_id, "second-writer")
    finally:
        client.proceed.set()
        runner.join(timeout=5.0)

    assert results and results[0].outcome is ExecutionOutcome.SUCCEEDED
    # 不论中途发生过什么，持有者退出时都要把租约释放掉。
    assert len(store) == 0


def test_auto_lease_ttl_covers_model_timeout_and_one_retry() -> None:
    """自动推导的 TTL 必须盖住「deadline + 模型超时×(1+重试)」。

    已发出的模型调用不受 deadline 约束，旧推导（deadline + 90s）在默认配置下
    只有 135s，而最坏是 165s——租约会在执行中途过期。这条用例防止余量被改回去。
    """

    settings = Settings()
    worst_case = settings.harness_deadline_seconds + settings.model_timeout_seconds * (
        1 + settings.model_retries
    )

    assert auto_lease_ttl(settings.harness_deadline_seconds) >= worst_case
