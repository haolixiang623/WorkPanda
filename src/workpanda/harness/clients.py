"""模型端口与脚本化客户端。

harness 只依赖这个端口，因此换模型、换网关、换 Agent SDK 都不影响受控逻辑：
Octop 运行时、内网 OpenAI 兼容接口、测试用假模型都只是不同实现。
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import Any, Protocol

from workpanda.harness.models import (
    HarnessMessage,
    ModelResponse,
    ToolCall,
    ToolSpec,
)


class ModelTimeout(Exception):
    """模型调用超时。可能已产生副作用，不得当失败重试。"""


class ModelError(Exception):
    """模型调用失败。code 会进入任务事件，便于运维定位。

    `retryable` 表达的是"这次失败是不是瞬时抖动"，由客户端根据 HTTP 语义判定：
    429/5xx/连接失败可以再试一次，400/401/参数错误再试一万次也一样。
    重试上限、退避与总耗时仍由执行循环控制，客户端不自行重试。
    """

    def __init__(self, code: str, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable


class ModelClient(Protocol):
    def complete(
        self,
        *,
        messages: Sequence[HarnessMessage],
        tools: Sequence[ToolSpec],
        temperature: float = 0.0,
    ) -> ModelResponse:
        """一次补全。需要工具调用时返回 tool_calls，否则返回最终文本。"""
        ...


class ScriptedModelClient:
    """按脚本返回结果，用于在无网络、无模型的情况下验证循环与守卫。"""

    def __init__(self, responses: Iterable[ModelResponse | Exception]) -> None:
        self._responses = list(responses)
        self._index = 0
        # 每次调用的入参摘要，便于断言"模型看到了什么工具"。
        self.calls: list[dict[str, Any]] = []

    def complete(
        self,
        *,
        messages: Sequence[HarnessMessage],
        tools: Sequence[ToolSpec],
        temperature: float = 0.0,
    ) -> ModelResponse:
        self.calls.append(
            {
                "messages": list(messages),
                "tools": [spec.name for spec in tools],
                "temperature": temperature,
            }
        )
        if self._index >= len(self._responses):
            raise ModelError("script_exhausted", "脚本化客户端没有更多响应")

        item = self._responses[self._index]
        self._index += 1
        if isinstance(item, Exception):
            raise item
        return item


def tool_call(name: str, *, call_id: str | None = None, **arguments: Any) -> ToolCall:
    """构造工具调用的便捷函数，供测试与脚本使用。"""

    return ToolCall(
        call_id=call_id or f"call-{name}-{arguments.get('__seq__', 1)}",
        name=name,
        arguments={key: value for key, value in arguments.items() if key != "__seq__"},
    )
