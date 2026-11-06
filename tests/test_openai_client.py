"""OpenAI 兼容客户端：线上格式转换与错误映射。"""

from __future__ import annotations

import json
import time
from typing import Any

import httpx
import pytest

from workpanda.harness.clients import ModelError, ModelTimeout
from workpanda.harness.models import HarnessMessage, MessageRole, ToolCall, ToolSpec
from workpanda.harness.openai_client import OpenAICompatibleClient

SPEC = ToolSpec(
    name="test_echo",
    description="回显",
    parameters={
        "type": "object",
        "properties": {"text": {"type": "string"}},
        "required": ["text"],
    },
)


def client_with(handler) -> OpenAICompatibleClient:
    return OpenAICompatibleClient(
        base_url="http://model.local/v1/",
        api_key="sk-test",
        model="test-model",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )


def test_request_uses_openai_wire_format() -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["auth"] = request.headers.get("authorization")
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    result = client_with(handler).complete(
        messages=[
            HarnessMessage(role=MessageRole.SYSTEM, content="sys"),
            HarnessMessage(
                role=MessageRole.ASSISTANT,
                content="",
                tool_calls=[ToolCall(call_id="c1", name="test_echo", arguments={"text": "hi"})],
            ),
            HarnessMessage(
                role=MessageRole.TOOL, tool_call_id="c1", name="test_echo", content="echo:hi"
            ),
        ],
        tools=[SPEC],
    )

    assert captured["url"] == "http://model.local/v1/chat/completions"
    assert captured["auth"] == "Bearer sk-test"
    body = captured["body"]
    assert body["model"] == "test-model"
    assert body["tools"][0]["function"]["name"] == "test_echo"
    assert body["tool_choice"] == "auto"
    # 助手消息里的工具调用必须回传，否则模型会丢失上下文
    assistant = body["messages"][1]
    assert assistant["tool_calls"][0]["function"]["name"] == "test_echo"
    assert json.loads(assistant["tool_calls"][0]["function"]["arguments"]) == {"text": "hi"}
    assert body["messages"][2] == {
        "role": "tool",
        "tool_call_id": "c1",
        "content": "echo:hi",
    }
    assert result.text == "ok"


def test_tool_calls_are_parsed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "call_a",
                                    "type": "function",
                                    "function": {
                                        "name": "test_echo",
                                        "arguments": '{"text": "hi"}',
                                    },
                                }
                            ],
                        },
                        "finish_reason": "tool_calls",
                    }
                ]
            },
        )

    result = client_with(handler).complete(messages=[], tools=[SPEC])

    assert result.text == ""
    assert result.tool_calls[0].call_id == "call_a"
    assert result.tool_calls[0].arguments == {"text": "hi"}
    assert result.finish_reason == "tool_calls"


def test_malformed_arguments_are_kept_for_validation_to_reject() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "tool_calls": [
                                {
                                    "id": "c",
                                    "function": {"name": "test_echo", "arguments": "{oops"},
                                }
                            ]
                        }
                    }
                ]
            },
        )

    result = client_with(handler).complete(messages=[], tools=[SPEC])

    # 不猜测畸形参数：原样带入，让参数校验拒绝并把原因回灌给模型
    assert result.tool_calls[0].arguments == {"__raw_arguments__": "{oops"}


def test_http_error_maps_to_model_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    with pytest.raises(ModelError) as excinfo:
        client_with(handler).complete(messages=[], tools=[])
    assert excinfo.value.code == "http_500"


def test_timeout_maps_to_model_timeout() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    with pytest.raises(ModelTimeout):
        client_with(handler).complete(messages=[], tools=[])


def test_empty_choices_is_an_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": []})

    with pytest.raises(ModelError) as excinfo:
        client_with(handler).complete(messages=[], tools=[])
    assert excinfo.value.code == "empty_choices"


def test_duration_ms_reflects_model_latency() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        time.sleep(0.03)
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    result = client_with(handler).complete(messages=[], tools=[])

    # 留痕要靠这个值区分"模型慢"还是"工具慢"，恒为 0 等于没有观测。
    assert isinstance(result.duration_ms, int)
    assert result.duration_ms >= 20


def test_duration_ms_is_measured_per_call_not_accumulated() -> None:
    """慢的第一次调用不得把耗时带进第二次。"""
    delays = [0.05, 0.0]

    def handler(request: httpx.Request) -> httpx.Response:
        time.sleep(delays.pop(0))
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    client = client_with(handler)
    slow = client.complete(messages=[], tools=[])
    fast = client.complete(messages=[], tools=[])

    assert slow.duration_ms >= 40
    assert fast.duration_ms < slow.duration_ms
