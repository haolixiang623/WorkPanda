"""取消：只在边界生效、已提交的调用照常留痕、结论一律"未确认"。

取消与超时、预算耗尽最容易混为一谈，但它们的语义完全不同：
超时是"不知道外部发生了什么"，预算是"没时间了"，取消是"人不要这个结果了"。
共同点是三者都不能当成功，也都不能自动重来。
"""

from __future__ import annotations

import json
import threading
import time
import uuid
from typing import Any

from fastapi.testclient import TestClient

from tests.support import TEST_FLOWS
from workpanda.api.security import SESSION_HEADER
from workpanda.application.ports import ExecutionOutcome, ExecutionRequest
from workpanda.bootstrap import build_container
from workpanda.config import Settings
from workpanda.harness.audit import AuditKind, JsonlAuditJournal
from workpanda.harness.cancel import Cancelled, CancelRegistry, CancelToken
from workpanda.harness.checkpoint import InMemoryCheckpointStore
from workpanda.harness.clients import ModelError, ScriptedModelClient, tool_call
from workpanda.harness.executor import HarnessExecutor
from workpanda.harness.loop import AgentLoop, StopReason
from workpanda.harness.models import EventKind, ModelResponse, RiskLevel, ToolSpec
from workpanda.harness.tools import ToolContext, ToolGrant, ToolRegistry
from workpanda.harness.trace import InMemoryTraceStore
from workpanda.infrastructure.adapters import KeywordIntentRouter
from workpanda.main import create_app

SUCCESS = json.dumps(
    {"outcome": "succeeded", "summary": "已完成", "evidence": "echo:hi"}, ensure_ascii=False
)


# ---------- 测试用工具 ----------


class EchoTool:
    spec = ToolSpec(
        name="test_echo",
        description="回显输入",
        parameters={
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
            "additionalProperties": False,
        },
        risk=RiskLevel.LOW,
    )

    def __init__(self, on_call: Any = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self._on_call = on_call

    def invoke(self, arguments: dict[str, Any], context: ToolContext) -> str:
        self.calls.append(arguments)
        if self._on_call is not None:
            self._on_call()
        return f"echo:{arguments['text']}"


class SubmitTool:
    """非幂等提交：调用一次就是一笔真实外部动作，取消不该让它"提前发生"。"""

    spec = ToolSpec(
        name="test_submit",
        description="提交单据",
        parameters={
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
            "additionalProperties": False,
        },
        idempotent=False,
    )

    def __init__(self, on_call: Any = None) -> None:
        self.invocations = 0
        self._on_call = on_call

    def invoke(self, arguments: dict[str, Any], context: ToolContext) -> str:
        self.invocations += 1
        if self._on_call is not None:
            self._on_call()
        return f"submitted:{arguments['text']}"


def registry_with(*tools: Any) -> ToolRegistry:
    registry = ToolRegistry()
    for tool in tools:
        registry.register(tool)
    return registry


def grant_for(registry: ToolRegistry, *names: str) -> ToolGrant:
    return ToolGrant(registry=registry, allowed=frozenset(names), call_timeout_seconds=5.0)


def request_for(task_id: uuid.UUID, tool: str = "test_echo") -> ExecutionRequest:
    return ExecutionRequest(
        task_id=task_id,
        flow_id="test.flow",
        flow_version="1",
        step_id="run",
        tool=tool,
        instruction="回显",
        arguments={"text": "hi"},
    )


def context() -> ToolContext:
    return ToolContext(task_id=uuid.uuid4(), flow_id="f", step_id="s", remaining_seconds=10)


def run_loop(
    client: Any, registry: ToolRegistry, *, cancel: CancelToken | None = None, **kwargs: Any
) -> Any:
    loop = AgentLoop(client, **kwargs)
    return loop.run(
        system_prompt="system",
        user_prompt="user",
        grant=grant_for(registry, *registry.names()),
        context=context(),
        cancel=cancel,
    )


# ---------- 令牌与登记表 ----------


def test_token_keeps_first_reason_and_is_idempotent() -> None:
    token = CancelToken()

    assert token.cancel("user_cancelled") is True
    assert token.cancel("second_reason") is False, "重复取消不该覆盖第一个原因"
    assert token.cancelled is True
    assert token.reason == "user_cancelled"
    assert token.cancelled_at is not None


def test_token_raise_if_cancelled_carries_stage() -> None:
    token = CancelToken()

    token.raise_if_cancelled("before_model")  # 未取消：不抛
    token.cancel("user_cancelled")

    try:
        token.raise_if_cancelled("before_tool")
    except Cancelled as exc:
        assert exc.stage == "before_tool"
        assert exc.reason == "user_cancelled"
    else:  # pragma: no cover - 显式失败比 silent pass 好
        raise AssertionError("已取消的令牌必须抛出 Cancelled")


def test_token_wait_returns_early_when_cancelled() -> None:
    token = CancelToken()
    threading.Timer(0.05, token.cancel, args=("user_cancelled",)).start()

    started = time.monotonic()
    interrupted = token.wait(5.0)
    elapsed = time.monotonic() - started

    assert interrupted is True
    assert elapsed < 1.0, "取消后不该继续等完退避"


def test_registry_cancels_live_token_and_ignores_other_tasks() -> None:
    registry = CancelRegistry()
    task_id, other = uuid.uuid4(), uuid.uuid4()
    token = registry.begin(task_id, "run")
    registry.begin(other, "run")

    assert registry.cancel_task(task_id, "user_cancelled") == 1
    assert token.cancelled is True
    assert registry.begin(other, "run").cancelled is False, "取消不该跨任务扩散"

    registry.end(task_id, "run")
    assert len(registry) == 1


def test_registry_remembers_cancellation_that_arrives_before_start() -> None:
    registry = CancelRegistry()
    task_id = uuid.uuid4()

    # 执行还没开始（排队中）：此时取消必须留下痕迹，而不是无声丢弃。
    assert registry.cancel_task(task_id, "user_cancelled") == 0
    assert registry.pending_reason(task_id) == "user_cancelled"
    assert registry.pending_reason(task_id) == "user_cancelled", "只读查询不该消费掉它"

    token = registry.begin(task_id, "run")
    assert token.cancelled is True
    assert token.reason == "user_cancelled"
    assert registry.pending_reason(task_id) is None, "取令牌后暂存项应被消费"


# ---------- 执行循环 ----------


def test_loop_stops_before_first_model_call_when_already_cancelled() -> None:
    echo = EchoTool()
    client = ScriptedModelClient([ModelResponse(text=SUCCESS)])
    token = CancelToken()
    token.cancel("user_cancelled")

    result = run_loop(client, registry_with(echo), cancel=token)

    assert result.stop_reason is StopReason.CANCELLED
    assert result.cancelled is True
    assert result.error_code == "cancelled"
    assert client.calls == [], "已取消就不该再花一次模型调用"
    stages = [event.data["stage"] for event in result.events if event.kind is EventKind.CANCELLED]
    assert stages == ["before_model"]


def test_loop_skips_remaining_tool_calls_after_cancel() -> None:
    """第一条工具调用里点了取消：第二条不能执行，第一条要照常留痕。"""

    token = CancelToken()
    echo = EchoTool(on_call=lambda: token.cancel("user_cancelled"))
    other = EchoTool()
    other.spec = other.spec.model_copy(update={"name": "test_echo_second"})
    client = ScriptedModelClient(
        [
            ModelResponse(
                tool_calls=[
                    tool_call("test_echo", call_id="1", text="a"),
                    tool_call("test_echo_second", call_id="2", text="b"),
                ]
            ),
            ModelResponse(text=SUCCESS),
        ]
    )

    result = run_loop(client, registry_with(echo, other), cancel=token, tool_concurrency=1)

    assert result.stop_reason is StopReason.CANCELLED
    assert [record.call_id for record in result.tool_calls] == ["1"], "只应留下已执行的那一条"
    assert echo.calls == [{"text": "a"}]
    assert other.calls == [], "取消之后不该再发起新的调用"
    assert len(client.calls) == 1, "停止后不该再请求模型"
    cancelled = [event for event in result.events if event.kind is EventKind.CANCELLED]
    assert cancelled[0].data["stage"] == "before_tool"
    assert cancelled[0].data["skipped_calls"] == ["test_echo_second"]


def test_parallel_batch_that_already_started_is_recorded_then_stops() -> None:
    """并发批次一旦提交就整批跑完：它们是幂等工具，重放安全，结果必须照常留痕。"""

    token = CancelToken()
    first = EchoTool(on_call=lambda: token.cancel("user_cancelled"))
    second = EchoTool()
    second.spec = second.spec.model_copy(update={"name": "test_echo_second"})
    client = ScriptedModelClient(
        [
            ModelResponse(
                tool_calls=[
                    tool_call("test_echo", call_id="1", text="a"),
                    tool_call("test_echo_second", call_id="2", text="b"),
                ]
            ),
            ModelResponse(text=SUCCESS),
        ]
    )

    result = run_loop(client, registry_with(first, second), cancel=token, tool_concurrency=4)

    assert result.stop_reason is StopReason.CANCELLED
    assert len(result.tool_calls) == 2
    assert len(client.calls) == 1, "批次跑完后应停在下一轮模型调用之前"


def test_cancel_interrupts_model_retry_backoff() -> None:
    token = CancelToken()

    class CancelThenFail:
        def __init__(self) -> None:
            self.calls = 0

        def complete(self, **kwargs: Any) -> ModelResponse:
            self.calls += 1
            token.cancel("user_cancelled")
            raise ModelError("http_503", "网关不可用", retryable=True)

    client = CancelThenFail()
    started = time.monotonic()
    result = run_loop(
        client, registry_with(EchoTool()), cancel=token, model_retries=3,
        model_retry_backoff_seconds=3.0,
    )
    elapsed = time.monotonic() - started

    assert result.stop_reason is StopReason.CANCELLED
    assert client.calls == 1
    assert elapsed < 1.5, "取消必须打断退避等待，而不是等退避结束"


# ---------- 执行器 ----------


def test_executor_does_nothing_when_cancelled_before_start() -> None:
    task_id = uuid.uuid4()
    registry = CancelRegistry()
    registry.cancel_task(task_id, "user_cancelled")
    echo = EchoTool()
    client = ScriptedModelClient([ModelResponse(text=SUCCESS)])
    traces = InMemoryTraceStore()
    executor = HarnessExecutor(
        client,
        registry=registry_with(echo),
        traces=traces,
        cancellations=registry,
    )

    result = executor.execute(request_for(task_id))

    assert result.outcome is ExecutionOutcome.UNCONFIRMED
    assert result.error_code == "cancelled"
    assert client.calls == []
    assert echo.calls == []
    assert len(registry) == 0, "执行结束后必须释放令牌"
    record = traces.list()[0]
    assert record.stop_reason == "cancelled"
    assert record.outcome == "unconfirmed"


def test_executor_reports_unconfirmed_and_keeps_checkpoint_on_cancel(tmp_path: Any) -> None:
    task_id = uuid.uuid4()
    registry = CancelRegistry()
    token_holder: dict[str, CancelToken] = {}

    # 工具执行成功之后才收到取消：那笔提交是真的发生了，结论只能是"未确认"。
    submit = SubmitTool(on_call=lambda: token_holder["token"].cancel("user_cancelled"))
    client = ScriptedModelClient(
        [
            ModelResponse(tool_calls=[tool_call("test_submit", call_id="1", text="x")]),
            ModelResponse(text=SUCCESS),
        ]
    )
    checkpoints = InMemoryCheckpointStore()
    journal = JsonlAuditJournal(tmp_path / "audit.jsonl")
    executor = HarnessExecutor(
        client,
        registry=registry_with(submit),
        traces=InMemoryTraceStore(),
        audit=journal,
        checkpoints=checkpoints,
        resume_enabled=True,
        cancellations=registry,
    )

    request = request_for(task_id, tool="test_submit")
    original = registry.begin

    def begin_and_expose(task: uuid.UUID, step_id: str) -> CancelToken:
        token = original(task, step_id)
        token_holder["token"] = token
        return token

    registry.begin = begin_and_expose  # type: ignore[method-assign]
    result = executor.execute(request)

    assert result.outcome is ExecutionOutcome.UNCONFIRMED
    assert result.error_code == "cancelled"
    assert submit.invocations == 1, "已提交的调用不能被撤销，只能如实留痕"
    assert len(checkpoints) == 1, "取消不清理断点：用户取消的是继续跑，不是刚才没发生"
    assert checkpoints.load(task_id, "run").resumable is True  # type: ignore[union-attr]
    assert len(registry) == 0

    entries = journal.read(task_id=task_id)
    execution = [entry for entry in entries if entry.kind is AuditKind.EXECUTION][0]
    assert execution.payload["stop_reason"] == "cancelled"
    assert execution.payload["outcome"] == "unconfirmed"


def test_cancel_endpoint_signals_the_running_executor() -> None:
    """取消不只是把任务状态改成 cancelled：执行侧必须收到同一个信号。"""

    container = build_container(
        Settings(store_backend="memory", enable_demo_flow=False),
        flows=TEST_FLOWS,
        router=KeywordIntentRouter(),
    )
    client = TestClient(create_app(container=container))
    client.headers.update({SESSION_HEADER: container.signer.issue()})
    try:
        created = client.post(
            "/api/tasks",
            json={
                "conversation_id": "conversation-1",
                "input_text": "我要办理测试事项",
                "flow_id": "test.confirm",
            },
        )
        assert created.status_code == 201, created.text
        task_id = uuid.UUID(created.json()["task_id"])

        response = client.post(f"/api/tasks/{task_id}/cancel")

        assert response.status_code == 200, response.text
        assert response.json()["status"] == "cancelled"
        # 此刻没有正在跑的步骤，取消会暂存，等这一步真正开始时立刻生效。
        assert container.cancellations.pending_reason(task_id) == "user_cancelled"
    finally:
        container.close()
