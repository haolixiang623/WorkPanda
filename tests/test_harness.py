"""通用 harness：白名单、参数校验、预算、留痕与"口头成功"守卫。"""

from __future__ import annotations

import json
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import httpx
import pytest

from workpanda.application.ports import ExecutionOutcome, ExecutionRequest
from workpanda.harness.builtin import HttpFetchTool, SystemClockTool
from workpanda.harness.clients import ModelError, ModelTimeout, ScriptedModelClient, tool_call
from workpanda.harness.contract import parse_contract
from workpanda.harness.executor import HarnessExecutor
from workpanda.harness.loop import AgentLoop, StopReason
from workpanda.harness.models import (
    EventKind,
    MessageRole,
    ModelResponse,
    RiskLevel,
    TokenUsage,
    ToolCall,
    ToolOutcome,
    ToolSpec,
    max_risk,
)
from workpanda.harness.openai_client import OpenAICompatibleClient
from workpanda.harness.redaction import MASK
from workpanda.harness.tools import (
    ToolContext,
    ToolError,
    ToolGrant,
    ToolRegistry,
    validate_arguments,
)
from workpanda.harness.trace import InMemoryTraceStore

SUCCESS = json.dumps(
    {"outcome": "succeeded", "summary": "已获取时间", "evidence": "utc=2026-01-01T00:00:00+00:00"},
    ensure_ascii=False,
)


class EchoTool:
    """最小可控工具：把参数原样返回。"""

    spec = ToolSpec(
        name="test_echo",
        description="回显输入",
        parameters={
            "type": "object",
            "properties": {"text": {"type": "string", "maxLength": 20}},
            "required": ["text"],
            "additionalProperties": False,
        },
        risk=RiskLevel.LOW,
    )

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def invoke(self, arguments: dict[str, Any], context: ToolContext) -> str:
        self.calls.append(arguments)
        return f"echo:{arguments['text']}"


class BoomTool:
    spec = ToolSpec(name="test_boom", description="总是失败")

    def invoke(self, arguments: dict[str, Any], context: ToolContext) -> str:
        raise ToolError("upstream_down", "上游不可用", retryable=True)


def registry_with(*tools: Any) -> ToolRegistry:
    registry = ToolRegistry()
    for tool in tools:
        registry.register(tool)
    return registry


def request_for(tool: str = "test_echo") -> ExecutionRequest:
    return ExecutionRequest(
        task_id=uuid.uuid4(),
        flow_id="test.flow",
        flow_version="1",
        step_id="run",
        tool=tool,
        instruction="把 text 回显出来",
        arguments={"text": "hi"},
    )


def context() -> ToolContext:
    return ToolContext(task_id=uuid.uuid4(), flow_id="f", step_id="s", remaining_seconds=30)


# ---------- 工具授权与参数校验 ----------


def test_unauthorized_tool_is_blocked_not_executed() -> None:
    echo = EchoTool()
    grant = ToolGrant(registry=registry_with(echo), allowed=frozenset())

    record = grant.call(ToolCall(call_id="1", name="test_echo", arguments={"text": "x"}), context())

    assert record.outcome.value == "blocked"
    assert record.error_code == "tool_not_authorized"
    assert echo.calls == []


def test_unknown_tool_is_blocked() -> None:
    grant = ToolGrant(registry=registry_with(EchoTool()), allowed=frozenset({"nope"}))

    record = grant.call(ToolCall(call_id="1", name="nope", arguments={}), context())

    assert record.error_code == "tool_not_registered"
    assert record.outcome.value == "blocked"


def test_invalid_arguments_are_rejected_before_invocation() -> None:
    echo = EchoTool()
    grant = ToolGrant(registry=registry_with(echo), allowed=frozenset({"test_echo"}))

    missing = grant.call(ToolCall(call_id="1", name="test_echo", arguments={}), context())
    extra = grant.call(
        ToolCall(call_id="2", name="test_echo", arguments={"text": "x", "inject": "y"}), context()
    )
    too_long = grant.call(
        ToolCall(call_id="3", name="test_echo", arguments={"text": "x" * 50}), context()
    )

    assert missing.error_code == "invalid_arguments"
    assert extra.error_code == "invalid_arguments"
    assert too_long.error_code == "invalid_arguments"
    assert echo.calls == []


def test_tool_error_is_recorded_not_raised() -> None:
    grant = ToolGrant(registry=registry_with(BoomTool()), allowed=frozenset({"test_boom"}))

    record = grant.call(ToolCall(call_id="1", name="test_boom", arguments={}), context())

    assert record.outcome.value == "error"
    assert record.error_code == "upstream_down"
    assert record.risk is RiskLevel.LOW


def test_validate_arguments_checks_types_and_enum() -> None:
    spec = ToolSpec(
        name="test_typed",
        description="类型校验",
        parameters={
            "type": "object",
            "properties": {
                "count": {"type": "integer"},
                "mode": {"type": "string", "enum": ["fast", "slow"]},
            },
            "required": ["count"],
        },
    )

    assert validate_arguments(spec, {"count": 3, "mode": "fast"}) == []
    assert validate_arguments(spec, {"count": "3"})
    assert validate_arguments(spec, {"count": 1, "mode": "other"})
    assert validate_arguments(spec, {})
    # bool 是 int 的子类，不能当成 integer 放过。
    assert validate_arguments(spec, {"count": True})


def test_risk_merges_to_highest() -> None:
    assert max_risk([]) is RiskLevel.LOW
    assert max_risk([RiskLevel.LOW, RiskLevel.HIGH, RiskLevel.MEDIUM]) is RiskLevel.HIGH


# ---------- 循环与预算 ----------


def test_loop_executes_tool_then_returns_final_text() -> None:
    echo = EchoTool()
    client = ScriptedModelClient(
        [
            ModelResponse(tool_calls=[tool_call("test_echo", call_id="c1", text="hi")]),
            ModelResponse(text=SUCCESS),
        ]
    )
    loop = AgentLoop(client, max_steps=4)
    grant = ToolGrant(registry=registry_with(echo), allowed=frozenset({"test_echo"}))

    result = loop.run(
        system_prompt="sys", user_prompt="user", grant=grant, context=context()
    )

    assert result.stop_reason is StopReason.COMPLETED
    assert result.text == SUCCESS
    assert result.has_successful_tool_call
    assert echo.calls == [{"text": "hi"}]
    # 模型在第二轮应当看到工具结果
    tool_messages = [
        message for message in client.calls[1]["messages"] if message.role is MessageRole.TOOL
    ]
    assert tool_messages and "echo:hi" in tool_messages[0].content


def test_model_call_duration_reaches_trace_event() -> None:
    """单次模型耗时必须来自真实往返：既不能恒为 0，也不能把整步耗时算进去。"""

    def handler(request: httpx.Request) -> httpx.Response:
        time.sleep(0.03)
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": "done"}, "finish_reason": "stop"}]},
        )

    client = OpenAICompatibleClient(
        base_url="http://model.local/v1",
        api_key="sk-test",
        model="test-model",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    loop = AgentLoop(client, max_steps=1)
    grant = ToolGrant(registry=registry_with(), allowed=frozenset())

    result = loop.run(system_prompt="s", user_prompt="u", grant=grant, context=context())

    responded = [event for event in result.events if event.kind is EventKind.MODEL_RESPONDED]
    assert len(responded) == 1
    assert responded[0].data["duration_ms"] >= 20
    # 模型耗时是整步耗时的一部分，不可能超过它。
    assert responded[0].data["duration_ms"] <= result.duration_ms


def test_multiple_tool_calls_in_one_response_run_in_order() -> None:
    """模型一轮里要多个工具时，必须按序执行并把每一条结果都回灌。"""

    echo = EchoTool()
    client = ScriptedModelClient(
        [
            ModelResponse(
                tool_calls=[
                    tool_call("test_echo", call_id="a", text="1"),
                    tool_call("test_echo", call_id="b", text="2"),
                ]
            ),
            ModelResponse(text=SUCCESS),
        ]
    )
    loop = AgentLoop(client, max_steps=4, max_tool_calls=4)
    grant = ToolGrant(registry=registry_with(echo), allowed=frozenset({"test_echo"}))

    result = loop.run(system_prompt="s", user_prompt="u", grant=grant, context=context())

    assert echo.calls == [{"text": "1"}, {"text": "2"}]
    assert [record.call_id for record in result.tool_calls] == ["a", "b"]
    tool_messages = [
        message for message in client.calls[1]["messages"] if message.role is MessageRole.TOOL
    ]
    assert [message.content for message in tool_messages] == ["echo:1", "echo:2"]


def test_loop_stops_on_max_steps_and_reports_budget() -> None:
    endless = ModelResponse(tool_calls=[tool_call("test_echo", call_id="c", text="hi")])
    client = ScriptedModelClient([endless] * 5)
    loop = AgentLoop(client, max_steps=2, max_tool_calls=10)
    grant = ToolGrant(registry=registry_with(EchoTool()), allowed=frozenset({"test_echo"}))

    result = loop.run(system_prompt="s", user_prompt="u", grant=grant, context=context())

    assert result.stop_reason is StopReason.MAX_STEPS
    assert result.error_code == "budget_max_steps"
    assert result.stopped_by_budget


def test_loop_stops_on_max_tool_calls() -> None:
    two_calls = ModelResponse(
        tool_calls=[
            tool_call("test_echo", call_id="a", text="1"),
            tool_call("test_echo", call_id="b", text="2"),
        ]
    )
    client = ScriptedModelClient([two_calls] * 5)
    loop = AgentLoop(client, max_steps=5, max_tool_calls=1)
    grant = ToolGrant(registry=registry_with(EchoTool()), allowed=frozenset({"test_echo"}))

    result = loop.run(system_prompt="s", user_prompt="u", grant=grant, context=context())

    assert result.stop_reason is StopReason.MAX_TOOL_CALLS
    assert len(result.tool_calls) == 1


def test_loop_maps_model_failures() -> None:
    grant = ToolGrant(registry=registry_with(EchoTool()), allowed=frozenset())

    timeout = AgentLoop(ScriptedModelClient([ModelTimeout("slow")])).run(
        system_prompt="s", user_prompt="u", grant=grant, context=context()
    )
    error = AgentLoop(ScriptedModelClient([ModelError("bad_gateway", "502")])).run(
        system_prompt="s", user_prompt="u", grant=grant, context=context()
    )

    assert timeout.stop_reason is StopReason.MODEL_TIMEOUT
    assert error.stop_reason is StopReason.MODEL_ERROR
    assert error.error_code == "bad_gateway"


# ---------- 模型调用的有界重试 ----------


def test_retryable_model_error_is_retried_then_succeeds() -> None:
    client = ScriptedModelClient(
        [ModelError("http_429", "限流", retryable=True), ModelResponse(text=SUCCESS)]
    )
    loop = AgentLoop(client, model_retries=1, model_retry_backoff_seconds=0)

    result = loop.run(
        system_prompt="s",
        user_prompt="u",
        grant=ToolGrant(registry=registry_with(EchoTool()), allowed=frozenset({"test_echo"})),
        context=context(),
    )

    assert result.stop_reason is StopReason.COMPLETED
    # 两次尝试都要计数：否则事后看 trace 会以为"只调了一次模型"。
    assert result.model_calls == 2
    assert EventKind.MODEL_RETRIED in [event.kind for event in result.events]


def test_non_retryable_model_error_is_not_retried() -> None:
    client = ScriptedModelClient([ModelError("http_400", "参数错误")])
    loop = AgentLoop(client, model_retries=3, model_retry_backoff_seconds=0)

    result = loop.run(
        system_prompt="s",
        user_prompt="u",
        grant=ToolGrant(registry=registry_with(), allowed=frozenset()),
        context=context(),
    )

    assert result.stop_reason is StopReason.MODEL_ERROR
    assert result.error_code == "http_400"
    assert len(client.calls) == 1
    assert EventKind.MODEL_RETRIED not in [event.kind for event in result.events]


def test_model_retries_are_bounded() -> None:
    client = ScriptedModelClient(
        [ModelError("transport_error", "连接被重置", retryable=True) for _ in range(4)]
    )
    loop = AgentLoop(client, model_retries=1, model_retry_backoff_seconds=0)

    result = loop.run(
        system_prompt="s",
        user_prompt="u",
        grant=ToolGrant(registry=registry_with(), allowed=frozenset()),
        context=context(),
    )

    # 1 次首调 + 1 次重试，之后不再纠缠：重试不能变成无限循环。
    assert len(client.calls) == 2
    assert result.stop_reason is StopReason.MODEL_ERROR
    assert result.error_code == "transport_error"
    assert result.model_calls == 2


def test_model_retry_is_skipped_when_no_time_is_left() -> None:
    client = ScriptedModelClient(
        [ModelError("http_503", "暂不可用", retryable=True), ModelResponse(text=SUCCESS)]
    )
    loop = AgentLoop(client, model_retries=1, model_retry_backoff_seconds=5, deadline_seconds=0.5)

    result = loop.run(
        system_prompt="s",
        user_prompt="u",
        grant=ToolGrant(registry=registry_with(), allowed=frozenset()),
        context=context(),
    )

    # 退避时间放不进剩余预算时直接上报原错误，别把整步时间耗在一次等待上。
    assert len(client.calls) == 1
    assert result.stop_reason is StopReason.MODEL_ERROR
    assert result.error_code == "http_503"


def test_model_retry_does_not_repeat_tool_calls() -> None:
    echo = EchoTool()
    client = ScriptedModelClient(
        [
            ModelResponse(tool_calls=[tool_call("test_echo", call_id="c1", text="hi")]),
            ModelError("http_429", "限流", retryable=True),
            ModelResponse(text=SUCCESS),
        ]
    )
    loop = AgentLoop(client, model_retries=1, model_retry_backoff_seconds=0)

    result = loop.run(
        system_prompt="s",
        user_prompt="u",
        grant=ToolGrant(registry=registry_with(echo), allowed=frozenset({"test_echo"})),
        context=context(),
    )

    # 重试的是"模型调用"，不是整个步骤：已发生的工具调用不会被重放。
    assert result.stop_reason is StopReason.COMPLETED
    assert [call["text"] for call in echo.calls] == ["hi"]
    assert len(result.tool_calls) == 1


def test_loop_stops_on_repeated_empty_responses() -> None:
    client = ScriptedModelClient([ModelResponse(text="  "), ModelResponse(text="")])

    result = AgentLoop(client).run(
        system_prompt="s",
        user_prompt="u",
        grant=ToolGrant(registry=registry_with(), allowed=frozenset()),
        context=context(),
    )

    assert result.stop_reason is StopReason.NO_PROGRESS
    assert result.error_code == "empty_model_response"


# ---------- 执行器：三态映射与"口头成功"守卫 ----------


def make_executor(client: ScriptedModelClient, *tools: Any, **kwargs: Any) -> HarnessExecutor:
    return HarnessExecutor(
        client,
        registry=registry_with(*tools),
        deadline_seconds=30,
        **kwargs,
    )


def test_success_with_tool_evidence_is_trusted() -> None:
    client = ScriptedModelClient(
        [
            ModelResponse(tool_calls=[tool_call("test_echo", call_id="c1", text="hi")]),
            ModelResponse(text=SUCCESS),
        ]
    )
    result = make_executor(client, EchoTool()).execute(request_for())

    assert result.outcome is ExecutionOutcome.SUCCEEDED
    assert result.output["verification"]["evidence"]
    assert result.output["verification"]["tool_evidence"][0]["tool"] == "test_echo"
    assert result.output["stop_reason"] == "completed"
    json.dumps(result.output)


def test_claimed_success_without_any_tool_call_is_downgraded() -> None:
    """本步骤有工具可用，模型却一次没调用就声称成功——这是最危险的假象。"""

    client = ScriptedModelClient([ModelResponse(text=SUCCESS)])
    result = make_executor(client, EchoTool()).execute(request_for())

    assert result.outcome is ExecutionOutcome.UNCONFIRMED
    assert result.error_code == "unverified_success"


def test_tool_call_failure_does_not_count_as_evidence() -> None:
    client = ScriptedModelClient(
        [
            ModelResponse(tool_calls=[tool_call("test_boom", call_id="c1")]),
            ModelResponse(text=SUCCESS),
        ]
    )
    result = make_executor(client, BoomTool()).execute(request_for(tool="test_boom"))

    assert result.outcome is ExecutionOutcome.UNCONFIRMED
    assert result.error_code == "unverified_success"
    assert result.output["tool_calls"][0]["outcome"] == "error"


def test_require_tool_call_can_be_relaxed() -> None:
    client = ScriptedModelClient([ModelResponse(text=SUCCESS)])
    result = make_executor(
        client, EchoTool(), require_tool_call=False
    ).execute(request_for())

    assert result.outcome is ExecutionOutcome.SUCCEEDED


def test_no_tools_granted_keeps_text_only_contract() -> None:
    """步骤没有可用工具时，只能依赖模型自身给出的依据。"""

    client = ScriptedModelClient([ModelResponse(text=SUCCESS)])
    result = make_executor(client).execute(request_for(tool="unregistered.tool"))

    assert result.outcome is ExecutionOutcome.SUCCEEDED
    assert result.output["verification"]["tool_evidence"] == []
    # 提示词必须明确告知"不需要调用工具"，否则模型会去找不存在的工具。
    assert "不需要调用任何工具" in client.calls[0]["messages"][0].content


def test_budget_exhaustion_maps_to_unconfirmed() -> None:
    endless = ModelResponse(tool_calls=[tool_call("test_echo", call_id="c", text="hi")])
    client = ScriptedModelClient([endless] * 6)
    executor = HarnessExecutor(
        client, registry=registry_with(EchoTool()), max_steps=2, deadline_seconds=30
    )

    result = executor.execute(request_for())

    assert result.outcome is ExecutionOutcome.UNCONFIRMED
    assert result.error_code == "budget_max_steps"


def test_model_timeout_maps_to_unconfirmed() -> None:
    client = ScriptedModelClient([ModelTimeout("slow")])

    result = make_executor(client, EchoTool()).execute(request_for())

    assert result.outcome is ExecutionOutcome.UNCONFIRMED
    assert result.error_code == "model_timeout"


def test_trace_records_every_step() -> None:
    client = ScriptedModelClient(
        [
            ModelResponse(tool_calls=[tool_call("test_echo", call_id="c1", text="hi")]),
            ModelResponse(text=SUCCESS),
        ]
    )
    result = make_executor(client, EchoTool()).execute(request_for())

    kinds = [event["kind"] for event in result.output["trace"]]
    assert kinds == [
        "model_requested",
        "model_responded",
        "tool_completed",
        "model_requested",
        "model_responded",
        "stopped",
    ]
    assert result.output["model_calls"] == 2


def test_contract_parsing_rules() -> None:
    assert parse_contract(SUCCESS)[0] is ExecutionOutcome.SUCCEEDED
    assert parse_contract('{"outcome":"succeeded","evidence":""}')[0] is (
        ExecutionOutcome.UNCONFIRMED
    )
    assert parse_contract('{"outcome":"failed","summary":"x"}')[0] is ExecutionOutcome.FAILED
    assert parse_contract("我做不到")[1]["reason"] == "unparsable_output"


# ---------- 内置工具 ----------


def test_system_clock_returns_real_time() -> None:
    text = SystemClockTool().invoke({}, context())

    assert "utc=" in text
    assert "unix=" in text


def test_http_fetch_rejects_non_allowlisted_host() -> None:
    tool = HttpFetchTool(allowed_hosts=["example.com"])

    with pytest.raises(ToolError) as excinfo:
        tool.invoke({"url": "https://evil.test/page"}, context())
    assert excinfo.value.code == "host_not_allowed"


def test_http_fetch_rejects_non_https() -> None:
    tool = HttpFetchTool(allowed_hosts=["example.com"])

    with pytest.raises(ToolError) as excinfo:
        tool.invoke({"url": "http://example.com/page"}, context())
    assert excinfo.value.code == "scheme_not_allowed"


def test_http_fetch_rejects_private_address(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "workpanda.harness.builtin.socket.getaddrinfo",
        lambda *args, **kwargs: [(2, 1, 6, "", ("127.0.0.1", 443))],
    )
    tool = HttpFetchTool(allowed_hosts=["example.com"])

    with pytest.raises(ToolError) as excinfo:
        tool.invoke({"url": "https://example.com/page"}, context())
    assert excinfo.value.code == "private_address_blocked"


def test_http_fetch_returns_body_without_following_redirects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "workpanda.harness.builtin.socket.getaddrinfo",
        lambda *args, **kwargs: [(2, 1, 6, "", ("93.184.216.34", 443))],
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="hello")

    tool = HttpFetchTool(
        allowed_hosts=["example.com"],
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    assert "hello" in tool.invoke({"url": "https://example.com/p"}, context())

    redirect = HttpFetchTool(
        allowed_hosts=["example.com"],
        client=httpx.Client(
            transport=httpx.MockTransport(lambda request: httpx.Response(302, headers={"location": "https://evil.test"}))
        ),
    )
    with pytest.raises(ToolError) as excinfo:
        redirect.invoke({"url": "https://example.com/p"}, context())
    assert excinfo.value.code == "redirect_not_allowed"


# ---------- 执行硬化：超时、重试、重复副作用、调用上限 ----------


class SleepTool:
    """模拟卡死的上游：invoke 会阻塞，必须被超时掐断。"""

    spec = ToolSpec(
        name="test_sleep",
        description="慢工具",
        parameters={"type": "object", "properties": {}, "additionalProperties": False},
        timeout_seconds=0.1,
    )

    def invoke(self, arguments: dict[str, Any], context: ToolContext) -> str:
        time.sleep(0.5)
        return "done"


class FlakyTool:
    """第一次可重试失败，第二次成功。"""

    spec = ToolSpec(
        name="test_flaky",
        description="不稳定工具",
        parameters={"type": "object", "properties": {}, "additionalProperties": False},
    )

    def __init__(self) -> None:
        self.attempts = 0

    def invoke(self, arguments: dict[str, Any], context: ToolContext) -> str:
        self.attempts += 1
        if self.attempts == 1:
            raise ToolError("upstream_busy", "上游繁忙", retryable=True)
        return "ok"


class FlakySubmitTool:
    """非幂等的提交类工具，第一次就失败。"""

    spec = ToolSpec(
        name="test_submit_flaky",
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
        self.attempts = 0

    def invoke(self, arguments: dict[str, Any], context: ToolContext) -> str:
        self.attempts += 1
        raise ToolError("upstream_busy", "上游繁忙", retryable=True)


class SubmitTool:
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


class SecretEchoTool:
    """回显令牌：用于验证"敏感参数落盘前必须脱敏"。"""

    spec = ToolSpec(
        name="test_secret",
        description="回显令牌",
        parameters={
            "type": "object",
            "properties": {"token": {"type": "string"}},
            "required": ["token"],
            "additionalProperties": False,
        },
        sensitive_fields=["token"],
    )

    def invoke(self, arguments: dict[str, Any], context: ToolContext) -> str:
        return f"echo:{arguments['token']}"


def test_tool_timeout_is_reported_as_unknown_not_failed() -> None:
    grant = ToolGrant(registry=registry_with(SleepTool()), allowed=frozenset({"test_sleep"}))

    started = time.monotonic()
    result = grant.call(ToolCall(call_id="1", name="test_sleep", arguments={}), context())
    elapsed = time.monotonic() - started

    assert result.outcome is ToolOutcome.TIMEOUT
    assert result.error_code == "tool_timeout"
    # 必须真的按声明的超时掐断，而不是陪工具跑完。
    assert elapsed < 0.4


def test_retryable_error_is_retried_for_idempotent_tool() -> None:
    flaky = FlakyTool()
    grant = ToolGrant(
        registry=registry_with(flaky),
        allowed=frozenset({"test_flaky"}),
        max_attempts=2,
        retry_backoff_seconds=0,
    )

    result = grant.call(ToolCall(call_id="1", name="test_flaky", arguments={}), context())

    assert result.outcome is ToolOutcome.OK
    assert result.attempts == 2
    assert flaky.attempts == 2


def test_non_idempotent_tool_is_never_retried() -> None:
    tool = FlakySubmitTool()
    grant = ToolGrant(
        registry=registry_with(tool),
        allowed=frozenset({"test_submit_flaky"}),
        max_attempts=3,
        retry_backoff_seconds=0,
    )

    result = grant.call(
        ToolCall(call_id="1", name="test_submit_flaky", arguments={"value": "x"}), context()
    )

    assert result.outcome is ToolOutcome.ERROR
    assert result.attempts == 1
    assert tool.attempts == 1


def test_duplicate_non_idempotent_call_is_blocked() -> None:
    submit = SubmitTool()
    grant = ToolGrant(registry=registry_with(submit), allowed=frozenset({"test_submit"}))

    first = grant.call(
        ToolCall(call_id="1", name="test_submit", arguments={"value": "x"}), context()
    )
    repeated = grant.call(
        ToolCall(call_id="2", name="test_submit", arguments={"value": "x"}), context()
    )
    different = grant.call(
        ToolCall(call_id="3", name="test_submit", arguments={"value": "y"}), context()
    )

    assert first.outcome is ToolOutcome.OK
    assert repeated.error_code == "duplicate_side_effect"
    assert different.error_code == "tool_call_limit"
    # 关键断言：副作用只发生了一次。
    assert submit.invocations == 1


def test_per_tool_max_calls_is_enforced() -> None:
    tool = EchoTool()
    tool.spec = tool.spec.model_copy(update={"max_calls": 1})
    grant = ToolGrant(registry=registry_with(tool), allowed=frozenset({"test_echo"}))

    grant.call(ToolCall(call_id="1", name="test_echo", arguments={"text": "a"}), context())
    blocked = grant.call(
        ToolCall(call_id="2", name="test_echo", arguments={"text": "b"}), context()
    )

    assert blocked.error_code == "tool_call_limit"
    assert len(tool.calls) == 1


def test_token_budget_exhaustion_maps_to_unconfirmed() -> None:
    client = ScriptedModelClient(
        [ModelResponse(text=SUCCESS, usage=TokenUsage(total_tokens=100))]
    )
    executor = HarnessExecutor(
        client,
        registry=registry_with(EchoTool()),
        max_tokens=50,
        deadline_seconds=30,
        require_tool_call=False,
    )

    result = executor.execute(request_for())

    assert result.outcome is ExecutionOutcome.UNCONFIRMED
    assert result.error_code == "budget_max_tokens"


def test_sensitive_arguments_are_masked_on_disk_but_not_for_the_model() -> None:
    """落盘/对外必须脱敏；回灌给模型必须保持原值，否则模型无法继续推理。"""

    client = ScriptedModelClient(
        [
            ModelResponse(
                tool_calls=[tool_call("test_secret", call_id="c1", token="tok-123456")]
            ),
            ModelResponse(text=SUCCESS),
        ]
    )

    result = make_executor(client, SecretEchoTool()).execute(request_for(tool="test_secret"))

    assert result.outcome is ExecutionOutcome.SUCCEEDED
    dumped = json.dumps(result.output, ensure_ascii=False)
    assert "tok-123456" not in dumped
    assert result.output["tool_calls"][0]["arguments"]["token"] == MASK

    tool_messages = [
        message for message in client.calls[1]["messages"] if message.role is MessageRole.TOOL
    ]
    assert "tok-123456" in tool_messages[0].content


def test_executor_writes_trace_for_both_success_and_unconfirmed() -> None:
    traces = InMemoryTraceStore()

    ok_executor = make_executor(
        ScriptedModelClient(
            [
                ModelResponse(tool_calls=[tool_call("test_echo", call_id="c1", text="hi")]),
                ModelResponse(text=SUCCESS),
            ]
        ),
        EchoTool(),
        traces=traces,
    )
    ok_executor.execute(request_for())

    unconfirmed_executor = make_executor(
        ScriptedModelClient([ModelResponse(text=SUCCESS)]), EchoTool(), traces=traces
    )
    unconfirmed_executor.execute(request_for())

    items = traces.list()
    assert [item.outcome for item in items] == ["unconfirmed", "succeeded"]
    assert items[0].error_code == "unverified_success"
    assert items[1].error_code is None
    assert items[0].blocked_tool_calls == 0
    assert traces.summary().unconfirmed_rate == 0.5


# ---------- 并行工具调用 ----------


class TimedTool:
    """可控耗时的幂等工具：记录每次执行的起止时刻，用于判断是否真的并发。"""

    def __init__(
        self, name: str, *, idempotent: bool = True, max_calls: int | None = None
    ) -> None:
        self.windows: list[tuple[float, float]] = []
        self._lock = threading.Lock()
        self.spec = ToolSpec(
            name=name,
            description="按参数睡一会儿",
            parameters={
                "type": "object",
                "properties": {"seconds": {"type": "number"}},
                "required": ["seconds"],
                "additionalProperties": False,
            },
            risk=RiskLevel.LOW,
            idempotent=idempotent,
            max_calls=max_calls,
            timeout_seconds=5.0,
        )

    def invoke(self, arguments: dict[str, Any], context: ToolContext) -> str:
        started = time.monotonic()
        time.sleep(float(arguments["seconds"]))
        with self._lock:
            self.windows.append((started, time.monotonic()))
        return f"slept:{arguments['seconds']}"


def test_idempotent_tool_calls_run_in_parallel() -> None:
    first = TimedTool("test_sleep_a")
    second = TimedTool("test_sleep_b")
    client = ScriptedModelClient(
        [
            ModelResponse(
                tool_calls=[
                    tool_call("test_sleep_a", call_id="a", seconds=0.3),
                    tool_call("test_sleep_b", call_id="b", seconds=0.3),
                ]
            ),
            ModelResponse(text=SUCCESS),
        ]
    )
    loop = AgentLoop(client, tool_concurrency=2)

    result = loop.run(
        system_prompt="s",
        user_prompt="u",
        grant=ToolGrant(
            registry=registry_with(first, second),
            allowed=frozenset({"test_sleep_a", "test_sleep_b"}),
        ),
        context=context(),
    )

    assert result.stop_reason is StopReason.COMPLETED
    # 两条调用的执行窗口必须重叠：串行时后一条一定晚于前一条结束。
    ((a_start, a_end),) = first.windows
    ((b_start, b_end),) = second.windows
    assert b_start < a_end
    assert a_start < b_end
    # 并发事实要留痕，否则事后按耗时排查会以为它们串行。
    assert EventKind.TOOLS_PARALLEL in [event.kind for event in result.events]


def test_parallel_calls_keep_model_order() -> None:
    slow = TimedTool("test_sleep_a")
    fast = TimedTool("test_sleep_b")
    client = ScriptedModelClient(
        [
            ModelResponse(
                tool_calls=[
                    tool_call("test_sleep_a", call_id="a", seconds=0.25),
                    tool_call("test_sleep_b", call_id="b", seconds=0.01),
                ]
            ),
            ModelResponse(text=SUCCESS),
        ]
    )
    loop = AgentLoop(client, tool_concurrency=2)

    result = loop.run(
        system_prompt="s",
        user_prompt="u",
        grant=ToolGrant(
            registry=registry_with(slow, fast),
            allowed=frozenset({"test_sleep_a", "test_sleep_b"}),
        ),
        context=context(),
    )

    # 先完成的是 b，但记录与回灌顺序必须与模型给出的顺序一致。
    assert [record.call_id for record in result.tool_calls] == ["a", "b"]
    tool_messages = [
        message for message in client.calls[1]["messages"] if message.role is MessageRole.TOOL
    ]
    assert [message.content for message in tool_messages] == ["slept:0.25", "slept:0.01"]


def test_non_idempotent_tool_keeps_whole_round_serial() -> None:
    reader = TimedTool("test_sleep_a")
    writer = TimedTool("test_sleep_b", idempotent=False)
    client = ScriptedModelClient(
        [
            ModelResponse(
                tool_calls=[
                    tool_call("test_sleep_a", call_id="a", seconds=0.15),
                    tool_call("test_sleep_b", call_id="b", seconds=0.15),
                ]
            ),
            ModelResponse(text=SUCCESS),
        ]
    )
    loop = AgentLoop(client, tool_concurrency=4)

    result = loop.run(
        system_prompt="s",
        user_prompt="u",
        grant=ToolGrant(
            registry=registry_with(reader, writer),
            allowed=frozenset({"test_sleep_a", "test_sleep_b"}),
        ),
        context=context(),
    )

    assert result.stop_reason is StopReason.COMPLETED
    # 只要这批里有一条非幂等，整批退回串行：并发会让两条同时通过重复副作用判定。
    assert EventKind.TOOLS_PARALLEL not in [event.kind for event in result.events]
    ((a_start, a_end),) = reader.windows
    ((b_start, _),) = writer.windows
    assert b_start >= a_end


def test_parallel_falls_back_when_budget_cannot_fit_batch() -> None:
    tool = TimedTool("test_sleep_a")
    batch = ModelResponse(
        tool_calls=[
            tool_call("test_sleep_a", call_id="a", seconds=0.01),
            tool_call("test_sleep_a", call_id="b", seconds=0.01),
        ]
    )
    client = ScriptedModelClient([batch] * 3)
    loop = AgentLoop(client, max_steps=4, max_tool_calls=1, tool_concurrency=4)

    result = loop.run(
        system_prompt="s",
        user_prompt="u",
        grant=ToolGrant(registry=registry_with(tool), allowed=frozenset({"test_sleep_a"})),
        context=context(),
    )

    # 预算放不下整批时退回串行，保持"已执行的调用仍然留痕"的既有语义。
    assert result.stop_reason is StopReason.MAX_TOOL_CALLS
    assert len(result.tool_calls) == 1
    assert len(tool.windows) == 1
    assert EventKind.TOOLS_PARALLEL not in [event.kind for event in result.events]


def test_parallel_call_failure_does_not_break_the_batch() -> None:
    ok = TimedTool("test_sleep_a")
    client = ScriptedModelClient(
        [
            ModelResponse(
                tool_calls=[
                    tool_call("test_sleep_a", call_id="a", seconds=0.01),
                    tool_call("test_boom", call_id="b"),
                ]
            ),
            ModelResponse(text=SUCCESS),
        ]
    )
    loop = AgentLoop(client, tool_concurrency=2)

    result = loop.run(
        system_prompt="s",
        user_prompt="u",
        grant=ToolGrant(
            registry=registry_with(ok, BoomTool()),
            allowed=frozenset({"test_sleep_a", "test_boom"}),
        ),
        context=context(),
    )

    assert [record.outcome for record in result.tool_calls] == [ToolOutcome.OK, ToolOutcome.ERROR]
    assert result.tool_calls[1].error_code == "upstream_down"
    assert result.stop_reason is StopReason.COMPLETED


def test_tool_call_limit_holds_under_concurrency() -> None:
    tool = TimedTool("test_sleep_a", max_calls=1)
    grant = ToolGrant(registry=registry_with(tool), allowed=frozenset({"test_sleep_a"}))
    calls = [
        ToolCall(call_id="a", name="test_sleep_a", arguments={"seconds": 0.15}),
        ToolCall(call_id="b", name="test_sleep_a", arguments={"seconds": 0.15}),
    ]

    with ThreadPoolExecutor(max_workers=2) as pool:
        records = list(pool.map(lambda call: grant.call(call, context()), calls))

    # "检查限额"和"记一次尝试"必须原子，否则并发下 max_calls 只是建议值。
    assert sorted(record.outcome for record in records) == [
        ToolOutcome.BLOCKED,
        ToolOutcome.OK,
    ]
    assert len(tool.windows) == 1
