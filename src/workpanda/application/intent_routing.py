"""识别规则的装配与自刷新。

规则从流程版本里来（`FlowDefinition.intent_rules`，D2=B）。这就带出两件事，本模块
各管一件：

1. **收集口径**：只从**已发布**版本收集。草稿版本的规则若参与路由，群众的话会被
   路由到一个 `resolve()` 必然拒绝的版本上——识别看起来成功，建任务却失败，报出来
   还像是群众说错了话（#73）。
2. **跟随发布**：发布 / 回滚 / 停用只改库里的发布指针。若路由器是装配期建好就不动的，
   运维在界面上看到"发布成功"，群众侧仍在按旧规则识别。这一条不能靠"记得在四个
   调用点都重建"来保证——本模块让重建变成不可能被忘记：`route()` 每次比对目录的
   代数，变了就用当前已发布版本重建。
"""

from __future__ import annotations

import threading
from collections.abc import Iterable

from workpanda.application.ports import IntentMatch
from workpanda.domain.flow import FlowDefinition, FlowRegistry
from workpanda.domain.intent import IntentRule, RouteDecision, RuleIntentRouter


def intent_rules_of(flows: Iterable[FlowDefinition]) -> tuple[IntentRule, ...]:
    """按 `(flow_id, version)` 顺序收集规则，保证装配结果可复现。

    顺序会影响打平时的裁决（同分先按 `intent_id` 排），所以这里也排序，而不是
    听凭调用方传进来的顺序。
    """

    ordered = sorted(flows, key=lambda flow: (flow.flow_id, flow.version))
    return tuple(rule for flow in ordered for rule in flow.intent_rules)


def router_for(flows: Iterable[FlowDefinition]) -> RuleIntentRouter:
    """按给定的一组流程定义装配路由器。发布门禁评"发布后的口径"时用它。"""

    return RuleIntentRouter(rules=intent_rules_of(flows))


class RegistryIntentRouter:
    """跟着流程目录自动重建的规则路由器。

    实现 `IntentRouter` 端口，可直接替换注入的确定性路由器。重建是**惰性**的：
    只在识别发生时才做，因此管理侧连发几次发布不会做无用的重建。

    这里仍然在锁内重建：识别会从工具线程与请求线程同时发生，两个线程各自重建
    虽然结果相同，但会让"这一句话用的是哪一版规则"在两次调用之间跳变。
    """

    def __init__(self, registry: FlowRegistry) -> None:
        self.registry = registry
        self._lock = threading.Lock()
        self._cached: RuleIntentRouter | None = None
        self._cached_generation = -1

    def _current(self) -> RuleIntentRouter:
        generation = self.registry.generation
        with self._lock:
            if self._cached is None or generation != self._cached_generation:
                self._cached = router_for(self.registry.published())
                self._cached_generation = generation
            return self._cached

    def route(self, text: str) -> IntentMatch | None:
        return self._current().route(text)

    def decide(self, text: str) -> RouteDecision:
        """带原因的识别结果。歧义与无命中在 `route()` 里是同一个动作，这里不丢原因。"""

        return self._current().decide(text)

    @property
    def has_rules(self) -> bool:
        return self._current().has_rules


__all__ = ["RegistryIntentRouter", "intent_rules_of", "router_for"]
