"""OpenAI 兼容接口客户端（支持 function calling）。

内网 epoint-hub 等网关都提供该协议，因此这一实现同时适用于本地模型和云厂商。
只做一件事：把 harness 的消息/工具声明翻译成 HTTP 请求，再翻译回 `ModelResponse`。
"""

from __future__ import annotations

import json
import time
from collections.abc import Sequence
from typing import Any

import httpx

from workpanda.harness.clients import ModelError, ModelTimeout
from workpanda.harness.models import (
    HarnessMessage,
    MessageRole,
    ModelResponse,
    TokenUsage,
    ToolCall,
    ToolSpec,
)

# 本地模型对工具名与调用 id 有长度上限，上游却不受这个约束。超长值在这里截断，
# 不能留到构造模型时抛 ValidationError——那个异常会绕过留痕直接冒到调用方。
MAX_TOOL_NAME = 64
MAX_CALL_ID = 128


class OpenAICompatibleClient:
    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        timeout_seconds: float = 60.0,
        client: httpx.Client | None = None,
        temperature: float = 0.0,
        trust_env: bool = True,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._model = model
        self._temperature = temperature
        # 内网地址若被系统代理拦截，需要显式关闭 trust_env。
        self._client = client or httpx.Client(
            timeout=httpx.Timeout(timeout_seconds, connect=min(10.0, timeout_seconds)),
            trust_env=trust_env,
        )
        self._owns_client = client is None

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def complete(
        self,
        *,
        messages: Sequence[HarnessMessage],
        tools: Sequence[ToolSpec],
        temperature: float = 0.0,
    ) -> ModelResponse:
        payload: dict[str, Any] = {
            "model": self._model,
            "messages": [_to_wire(message) for message in messages],
            "temperature": self._temperature if temperature == 0.0 else temperature,
            "stream": False,
        }
        if tools:
            payload["tools"] = [_tool_wire(spec) for spec in tools]
            payload["tool_choice"] = "auto"

        # 只计这一段往返耗时：transport 错误和超时会抛异常、不产生响应，
        # 耗时应由失败路径自己记录，不能混进下一次调用的成功耗时里。
        started = time.monotonic()
        try:
            response = self._client.post(
                f"{self._base_url}/chat/completions",
                json=payload,
                headers={"Authorization": f"Bearer {self._api_key}"},
            )
        except httpx.TimeoutException as exc:
            raise ModelTimeout("模型调用超时") from exc
        except httpx.HTTPError as exc:
            # 连接被重置、TLS 抖动这类传输失败通常是瞬时的，值得再试一次；
            # 是否真的重试由执行循环按剩余预算决定。
            raise ModelError(
                "transport_error", f"无法连接模型服务：{type(exc).__name__}", retryable=True
            ) from exc
        duration_ms = int((time.monotonic() - started) * 1000)

        if response.status_code >= 400:
            raise ModelError(
                f"http_{response.status_code}",
                f"模型服务返回 {response.status_code}：{response.text[:200]}",
                retryable=_is_retryable_status(response.status_code),
            )

        try:
            data = response.json()
        except ValueError as exc:
            raise ModelError("invalid_response", "模型服务返回的不是 JSON") from exc

        return _parse_completion(data, duration_ms=duration_ms)


def _is_retryable_status(status_code: int) -> bool:
    """哪些 HTTP 状态值得再试一次。

    只认"服务端暂时不行"这一类：限流（429）、请求超时（408）、全部 5xx。
    鉴权、参数、余额、模型不存在这些再试一次也是一样的结果，重试只会放大问题。
    """

    return status_code in {408, 425, 429} or status_code >= 500


def _to_wire(message: HarnessMessage) -> dict[str, Any]:
    if message.role is MessageRole.TOOL:
        return {
            "role": "tool",
            "tool_call_id": message.tool_call_id or "",
            "content": message.content,
        }
    if message.role is MessageRole.ASSISTANT and message.tool_calls:
        return {
            "role": "assistant",
            "content": message.content or None,
            "tool_calls": [
                {
                    "id": call.call_id,
                    "type": "function",
                    "function": {
                        "name": call.name,
                        "arguments": json.dumps(call.arguments, ensure_ascii=False),
                    },
                }
                for call in message.tool_calls
            ],
        }
    return {"role": message.role.value, "content": message.content}


def _tool_wire(spec: ToolSpec) -> dict[str, Any]:
    parameters = spec.parameters or {"type": "object", "properties": {}}
    return {
        "type": "function",
        "function": {
            "name": spec.name,
            "description": spec.description,
            "parameters": parameters,
        },
    }


def _parse_completion(data: dict[str, Any], *, duration_ms: int = 0) -> ModelResponse:
    choices = data.get("choices") or []
    if not choices:
        raise ModelError("empty_choices", "模型服务没有返回任何候选结果")

    message = choices[0].get("message") or {}
    content = message.get("content")
    raw_calls = message.get("tool_calls")
    calls = (
        _parse_tool_calls([raw for raw in raw_calls if isinstance(raw, dict)])
        if isinstance(raw_calls, list)
        else []
    )

    return ModelResponse(
        text=content if isinstance(content, str) else "",
        reasoning=_reasoning_of(message),
        tool_calls=calls,
        finish_reason=choices[0].get("finish_reason"),
        duration_ms=duration_ms,
        # 有的网关不返回 usage；拿不到就不统计，绝不用字符数估算。
        usage=TokenUsage.from_payload(data.get("usage")),
    )


def _reasoning_of(message: dict[str, Any]) -> str:
    """不同网关给推理过程起的字段名不一样，两个常见的都认。

    只取字符串；给错类型（列表、dict）时不猜测结构，按"没有"处理。
    这段内容只用于排障，不会进入 `text`，因此不可能被当成结论或依据。
    """

    for key in ("reasoning_content", "reasoning"):
        value = message.get(key)
        if isinstance(value, str) and value:
            return value
    return ""


def _parse_tool_calls(raw_calls: list[dict[str, Any]]) -> list[ToolCall]:
    """把上游给的一组调用转成 `ToolCall`，并保证 id 唯一。

    上游偶尔会给出重复的 `id`（甚至完全不给）。`call_id` 是回灌工具结果时的唯一
    关联键：两条调用共用一个 id，回执就会串到一起，模型看到的是"两条调用一个结果"，
    而留痕里也分不清哪条对应哪次执行。这里按出现顺序补后缀去重，语义保持"第一条
    原样保留"，只为后续重复项换键。

    单个调用解析失败（名字缺失、字段类型不对）只丢这一条，不让整轮响应作废——
    模型已经说了话，丢整条等于把一次可能的副作用变成"没发生过"。
    """

    calls: list[ToolCall] = []
    used: set[str] = set()
    for raw in raw_calls:
        call = _parse_tool_call(raw)
        if call is None:
            continue
        calls.append(_dedupe_call_id(call, used))
    return calls


def _dedupe_call_id(call: ToolCall, used: set[str]) -> ToolCall:
    if call.call_id not in used:
        used.add(call.call_id)
        return call

    # 后缀必须落在长度上限之内：先拼后缀再截断，恰好 128 字符的 id 会把后缀截掉，
    # 重命名结果退化成原 id——去重静默失效，两条调用仍共用同一个键。
    suffix = 2
    while True:
        tail = f"-{suffix}"
        renamed = f"{call.call_id[: MAX_CALL_ID - len(tail)]}{tail}"
        if renamed not in used:
            break
        suffix += 1
    used.add(renamed)
    return call.model_copy(update={"call_id": renamed})


def _parse_tool_call(raw: dict[str, Any]) -> ToolCall | None:
    function = raw.get("function") or {}
    name = function.get("name") if isinstance(function, dict) else None
    if not isinstance(name, str) or not name.strip():
        return None

    raw_arguments = function.get("arguments")
    arguments: dict[str, Any]
    if isinstance(raw_arguments, dict):
        arguments = raw_arguments
    else:
        text = raw_arguments if isinstance(raw_arguments, str) else ""
        try:
            parsed = json.loads(text) if text.strip() else {}
        except json.JSONDecodeError:
            # 畸形的参数不猜测：原样带入，让参数校验拒绝并把原因回灌给模型。
            arguments = {"__raw_arguments__": text[:500]}
        else:
            arguments = parsed if isinstance(parsed, dict) else {"__raw_arguments__": text[:500]}

    # 名字与 id 都要截断到本地模型允许的长度：上游给超长值时，如果直接构造
    # `ToolCall` 会抛 ValidationError，一路冒到执行器之外，连 trace 都留不下来。
    # 截断比整轮作废安全：工具名对不上会被闸门拦成 `tool_not_registered` 并留痕。
    call_id = str(raw.get("id") or f"call-{name}")
    return ToolCall(
        call_id=call_id[:MAX_CALL_ID] or f"call-{name}"[:MAX_CALL_ID],
        name=name[:MAX_TOOL_NAME],
        arguments=arguments,
    )
