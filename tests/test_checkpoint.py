"""断点续跑：能接着跑、不重复副作用、结果未知时拒绝续跑。"""

from __future__ import annotations

import json
import time
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from workpanda.application.ports import ExecutionOutcome, ExecutionRequest
from workpanda.harness.checkpoint import (
    ExecutionSnapshot,
    HarnessCheckpoint,
    InMemoryCheckpointStore,
)
from workpanda.harness.clients import ScriptedModelClient, tool_call
from workpanda.harness.executor import HarnessExecutor
from workpanda.harness.models import (
    HarnessMessage,
    MessageRole,
    ModelResponse,
    ToolSpec,
)
from workpanda.harness.tools import ToolContext, ToolRegistry
from workpanda.harness.trace import InMemoryTraceStore
from workpanda.infrastructure.sqlite_checkpoint_store import SqliteCheckpointStore

SUCCESS = json.dumps(
    {"outcome": "succeeded", "summary": "已提交", "evidence": "submitted:x"},
    ensure_ascii=False,
)


class SubmitTool:
    """非幂等的提交类工具：重复执行就是真实事故。"""

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
        return f"submitted:{arguments['value']}"


class SlowTool:
    """必然超时的工具：用于制造"结果未知"。"""

    spec = ToolSpec(
        name="test_slow",
        description="慢工具",
        parameters={"type": "object", "properties": {}, "additionalProperties": False},
        timeout_seconds=0.05,
    )

    def invoke(self, arguments: dict[str, Any], context: ToolContext) -> str:
        time.sleep(0.3)
        return "done"


def registry_with(*tools: Any) -> ToolRegistry:
    registry = ToolRegistry()
    for tool in tools:
        registry.register(tool)
    return registry


def request_for(tool: str = "test_submit") -> ExecutionRequest:
    return ExecutionRequest(
        task_id=uuid.uuid4(),
        flow_id="test.flow",
        flow_version="1",
        step_id="run",
        tool=tool,
        instruction="提交单据",
        arguments={"value": "x"},
    )


def crash_after_tool(
    tool: Any, *, arguments: dict[str, Any] | None = None, **kwargs: Any
) -> HarnessExecutor:
    """第一次调用执行工具，第二次模型调用直接崩溃，模拟进程中断。"""

    kwargs.setdefault("resume_enabled", True)
    client = ScriptedModelClient(
        [
            ModelResponse(
                tool_calls=[
                    tool_call(tool.spec.name, call_id="c1", **(arguments or {}))
                ]
            ),
            RuntimeError("process crashed"),
        ]
    )
    return HarnessExecutor(
        client,
        registry=registry_with(tool),
        deadline_seconds=30,
        **kwargs,
    )


def test_tool_call_is_checkpointed_before_the_next_model_turn() -> None:
    store = InMemoryCheckpointStore()
    submit = SubmitTool()
    request = request_for()

    with pytest.raises(RuntimeError):
        crash_after_tool(submit, arguments={"value": "x"}, checkpoints=store).execute(request)

    checkpoint = store.load(request.task_id, request.step_id)
    assert checkpoint is not None
    assert checkpoint.resumable
    assert checkpoint.blocked_reason is None
    assert len(checkpoint.snapshot.tool_calls) == 1
    assert checkpoint.snapshot.tool_calls[0].outcome.value == "ok"
    # 断点里必须带着"已成功的副作用"，否则续跑会再提交一次。
    assert checkpoint.grant_state["succeeded_effects"]


def test_resume_continues_without_repeating_the_side_effect() -> None:
    store = InMemoryCheckpointStore()
    traces = InMemoryTraceStore()
    submit = SubmitTool()
    request = request_for()

    with pytest.raises(RuntimeError):
        crash_after_tool(
            submit, arguments={"value": "x"}, checkpoints=store, traces=traces
        ).execute(request)
    source = store.load(request.task_id, request.step_id)
    assert source is not None

    # 续跑的模型重复要求同一个提交：必须被拦下，而不是再提交一次。
    client = ScriptedModelClient(
        [
            ModelResponse(
                tool_calls=[tool_call("test_submit", call_id="c2", value="x")]
            ),
            ModelResponse(text=SUCCESS),
        ]
    )
    executor = HarnessExecutor(
        client,
        registry=registry_with(submit),
        deadline_seconds=30,
        resume_enabled=True,
        checkpoints=store,
        traces=traces,
    )

    result = executor.execute(request)

    assert result.outcome is ExecutionOutcome.SUCCEEDED
    assert submit.invocations == 1
    assert result.output["resumed_from"] == source.trace_id
    assert result.output["tool_calls"][-1]["error_code"] == "duplicate_side_effect"
    # 模型看到的是断点里的历史，而不是一个从零开始的对话。
    assert len(client.calls[0]["messages"]) == 4
    assert any(
        message.role is MessageRole.TOOL for message in client.calls[0]["messages"]
    )
    assert traces.list()[0].resumed_from == source.trace_id
    # trace 只统计这次尝试的调用（被拦的重复提交），不重复计断点里已有的那次。
    assert traces.list()[0].tool_calls == 1
    assert traces.list()[0].successful_tool_calls == 0
    # 有结论后断点应当清理，否则下次会被误判成"上次没跑完"。
    assert store.load(request.task_id, request.step_id) is None


def test_timeout_makes_checkpoint_unresumable_and_refuses_retry() -> None:
    store = InMemoryCheckpointStore()
    slow = SlowTool()
    request = request_for(tool="test_slow")

    with pytest.raises(RuntimeError):
        crash_after_tool(slow, checkpoints=store).execute(request)

    checkpoint = store.load(request.task_id, request.step_id)
    assert checkpoint is not None
    assert not checkpoint.resumable
    assert checkpoint.blocked_reason == "tool_timeout"

    client = ScriptedModelClient([])
    executor = HarnessExecutor(
        client,
        registry=registry_with(slow),
        deadline_seconds=30,
        resume_enabled=True,
        checkpoints=store,
    )

    result = executor.execute(request)

    assert result.outcome is ExecutionOutcome.UNCONFIRMED
    assert result.error_code == "checkpoint_not_resumable"
    # 关键：完全没有再调用模型或工具，避免盲目重试。
    assert client.calls == []
    # 断点保留给人工核实，不被静默丢弃。
    assert store.load(request.task_id, request.step_id) is not None


def test_resume_is_off_by_default_and_leaves_no_checkpoint() -> None:
    store = InMemoryCheckpointStore()
    request = request_for()

    with pytest.raises(RuntimeError):
        crash_after_tool(
            SubmitTool(), arguments={"value": "x"}, checkpoints=store, resume_enabled=False
        ).execute(request)

    assert len(store) == 0


def test_resume_does_not_grant_fresh_budget() -> None:
    store = InMemoryCheckpointStore()
    request = request_for()
    store.save(
        HarnessCheckpoint(
            trace_id="source-trace",
            task_id=request.task_id,
            flow_id=request.flow_id,
            flow_version=request.flow_version,
            step_id=request.step_id,
            allowed_tools=["test_submit"],
            snapshot=ExecutionSnapshot(
                messages=[
                    HarnessMessage(role=MessageRole.SYSTEM, content="s"),
                    HarnessMessage(role=MessageRole.USER, content="u"),
                ],
                steps=2,
            ),
            resumable=True,
        )
    )
    client = ScriptedModelClient([ModelResponse(text=SUCCESS)])
    executor = HarnessExecutor(
        client,
        registry=registry_with(SubmitTool()),
        max_steps=2,
        deadline_seconds=30,
        resume_enabled=True,
        checkpoints=store,
    )

    result = executor.execute(request)

    assert result.outcome is ExecutionOutcome.UNCONFIRMED
    assert result.error_code == "budget_max_steps"
    assert client.calls == []
    # 预算耗尽属于"没有结论"，断点必须留着，不能被当成可以重新开始。
    assert store.load(request.task_id, request.step_id) is not None


def test_checkpoint_from_other_grant_is_refused() -> None:
    store = InMemoryCheckpointStore()
    request = request_for()
    store.save(
        HarnessCheckpoint(
            trace_id="other-trace",
            task_id=request.task_id,
            flow_id=request.flow_id,
            flow_version=request.flow_version,
            step_id=request.step_id,
            allowed_tools=["some_other_tool"],
            snapshot=ExecutionSnapshot(),
        )
    )
    client = ScriptedModelClient([ModelResponse(text=SUCCESS)])
    executor = HarnessExecutor(
        client,
        registry=registry_with(SubmitTool()),
        deadline_seconds=30,
        resume_enabled=True,
        checkpoints=store,
    )

    result = executor.execute(request)

    assert result.error_code == "checkpoint_mismatch"
    assert client.calls == []


def test_expired_checkpoint_is_refused() -> None:
    store = InMemoryCheckpointStore()
    request = request_for()
    stale = HarnessCheckpoint(
        trace_id="stale-trace",
        task_id=request.task_id,
        flow_id=request.flow_id,
        flow_version=request.flow_version,
        step_id=request.step_id,
        allowed_tools=["test_submit"],
        snapshot=ExecutionSnapshot(),
    )
    stale.updated_at = datetime.now(UTC) - timedelta(hours=1)
    store.save(stale)

    client = ScriptedModelClient([ModelResponse(text=SUCCESS)])
    executor = HarnessExecutor(
        client,
        registry=registry_with(SubmitTool()),
        deadline_seconds=30,
        resume_enabled=True,
        resume_ttl_seconds=60,
        checkpoints=store,
    )

    result = executor.execute(request)

    assert result.error_code == "checkpoint_expired"
    assert client.calls == []


def test_refused_run_records_measured_duration(monkeypatch: pytest.MonkeyPatch) -> None:
    """拒绝续跑也要有真实耗时。

    此前这条路径把 `duration_ms` 写死为 0，留痕里"快速拒绝"和"压根没执行"是
    同一条记录，运维无法判断到底是哪一种。
    """

    class FakeClock:
        def __init__(self) -> None:
            self._value = 1000.0

        def monotonic(self) -> float:
            self._value += 0.25
            return self._value

    monkeypatch.setattr("workpanda.harness.executor.time", FakeClock())

    store = InMemoryCheckpointStore()
    traces = InMemoryTraceStore()
    request = request_for()
    store.save(
        HarnessCheckpoint(
            trace_id="other-trace",
            task_id=request.task_id,
            flow_id=request.flow_id,
            flow_version=request.flow_version,
            step_id=request.step_id,
            allowed_tools=["some_other_tool"],
            snapshot=ExecutionSnapshot(),
        )
    )
    executor = HarnessExecutor(
        ScriptedModelClient([ModelResponse(text=SUCCESS)]),
        registry=registry_with(SubmitTool()),
        deadline_seconds=30,
        resume_enabled=True,
        checkpoints=store,
        traces=traces,
    )

    result = executor.execute(request)

    assert result.error_code == "checkpoint_mismatch"
    assert result.output["duration_ms"] == 250
    assert traces.list(limit=1)[0].duration_ms == 250


def test_checkpoint_is_cleared_after_a_normal_run() -> None:
    store = InMemoryCheckpointStore()
    submit = SubmitTool()
    client = ScriptedModelClient(
        [
            ModelResponse(tool_calls=[tool_call("test_submit", call_id="c1", value="x")]),
            ModelResponse(text=SUCCESS),
        ]
    )
    executor = HarnessExecutor(
        client,
        registry=registry_with(submit),
        deadline_seconds=30,
        resume_enabled=True,
        checkpoints=store,
    )

    result = executor.execute(request_for())

    assert result.outcome is ExecutionOutcome.SUCCEEDED
    assert len(store) == 0


def test_sqlite_checkpoint_survives_reopen(workdir: Path) -> None:
    path = workdir / "checkpoints.db"
    request = request_for()
    checkpoint = HarnessCheckpoint(
        trace_id="trace-1",
        task_id=request.task_id,
        flow_id=request.flow_id,
        flow_version=request.flow_version,
        step_id=request.step_id,
        allowed_tools=["test_submit"],
        snapshot=ExecutionSnapshot(steps=1),
    )

    store = SqliteCheckpointStore(path)
    store.save(checkpoint)
    store.close()

    reopened = SqliteCheckpointStore(path)
    try:
        loaded = reopened.load(request.task_id, request.step_id)
        assert loaded is not None
        assert loaded.trace_id == "trace-1"
        assert loaded.snapshot.steps == 1

        updated = checkpoint.model_copy(update={"trace_id": "trace-2"})
        reopened.save(updated)
        assert reopened.load(request.task_id, request.step_id).trace_id == "trace-2"
        assert len(reopened) == 1

        reopened.delete(request.task_id, request.step_id)
        assert reopened.load(request.task_id, request.step_id) is None
    finally:
        reopened.close()
