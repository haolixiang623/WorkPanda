"""开发期适配器：规则意图匹配与可预测的执行器。

两者都是占位实现，用于在没有真实模型和外部系统时验证工程链路。
生产实现必须替换：意图匹配换成受约束模型 + 置信度阈值，执行器换成 API/浏览器适配器。
"""

from collections.abc import Iterable
from dataclasses import dataclass, field

from workpanda.application.ports import (
    ExecutionOutcome,
    ExecutionRequest,
    ExecutionResult,
    IntentMatch,
)


@dataclass
class KeywordIntentRouter:
    """按关键词匹配已注册流程；匹配不到返回 None，由上层澄清或转人工。"""

    rules: Iterable[tuple[tuple[str, ...], IntentMatch]] = ()
    confidence: float = 0.55
    _rules: list[tuple[tuple[str, ...], IntentMatch]] = field(init=False, default_factory=list)

    def __post_init__(self) -> None:
        self._rules = list(self.rules)

    def route(self, text: str) -> IntentMatch | None:
        best: IntentMatch | None = None
        for keywords, match in self._rules:
            hits = sum(1 for keyword in keywords if keyword and keyword in text)
            if hits == 0:
                continue
            score = min(0.99, self.confidence + 0.1 * (hits - 1))
            if best is None or score > best.confidence:
                best = match.model_copy(update={"confidence": score})
        return best


@dataclass
class ScriptedExecutor:
    """按顺序返回预设结果，用于测试失败、超时和结果未知路径。"""

    results: list[ExecutionResult] = field(default_factory=list)
    calls: list[ExecutionRequest] = field(default_factory=list)

    def execute(self, request: ExecutionRequest) -> ExecutionResult:
        self.calls.append(request)
        if self.results:
            return self.results.pop(0)
        return ExecutionResult(
            outcome=ExecutionOutcome.SUCCEEDED,
            output={"tool": request.tool, "step_id": request.step_id},
        )
