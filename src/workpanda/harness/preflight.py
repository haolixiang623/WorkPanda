"""装配期「流程声明的工具」与「进程里注册的工具」对账。

两边不会自己对上：流程定义存在库里（可能是别人经管理侧建的），工具注册发生在
`default_registry()`。对不上时**不会报错**，这才是问题：

1. `HarnessExecutor._grant()` 把"本步骤声明的工具"与"已注册工具"取交集，交集为空
   仍然照跑——模型依旧被调用；
2. `_unverified_success()` 里有 `if not grant.names: return False`，本意是"这一步
   本来就没有工具，只能依赖模型自述"，落到这里却变成"工具名写错了也放行"；
3. 于是一次**一个工具都没跑过**的执行以 `succeeded` 收尾，留痕里只是少了几行
   `tool_calls`。事后从结果里看不出来。

这与三态契约的用意相反：缺依据应当未确认，不该因为工具压根不存在就降级成"不需要
依据"。所以把对账前移到装配期——进程起不来，好过看起来跑通了。

本模块刻意只做纯计算，不 import 模型客户端、不碰网络：没有工具链的机器上也能回归。
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import NamedTuple

from workpanda.domain.flow import FlowDefinition, StepKind
from workpanda.harness.tools import ToolRegistry


class MissingStepTool(NamedTuple):
    """一处"流程里写了、进程里没有"的工具声明。"""

    flow_id: str
    flow_version: str
    step_id: str
    tool: str

    def describe(self) -> str:
        return (
            f"{self.flow_id}@{self.flow_version} 的步骤 {self.step_id} "
            f"声明了工具 {self.tool!r}"
        )


class UnavailableStepTool(RuntimeError):
    """已发布流程声明了进程里不存在的工具，装配终止。"""

    def __init__(
        self, missing: Iterable[MissingStepTool], available: Iterable[str]
    ) -> None:
        self.missing: tuple[MissingStepTool, ...] = tuple(missing)
        self.available: tuple[str, ...] = tuple(available)
        lines = "\n".join(f"  - {item.describe()}" for item in self.missing)
        known = ", ".join(self.available) or "（无）"
        super().__init__(
            "以下流程声明了未注册的工具，装配终止：\n"
            f"{lines}\n"
            f"当前注册的工具：{known}。\n"
            "先注册工具或修正流程再启动。缺工具时不要运行：执行步骤会在没有任何"
            "工具可用的情况下走到 succeeded，而这种成功无法与真实完成区分。"
        )


def missing_step_tools(
    flows: Iterable[FlowDefinition], available: Iterable[str]
) -> list[MissingStepTool]:
    """列出所有声明了未注册工具的执行步骤。

    只看 `EXECUTE` 步骤：其他类型即使误带 `tool` 也不参与授权
    （`validate_flow()` 已禁止，这里不重复报同一个错）。
    """

    names = set(available)
    missing: list[MissingStepTool] = []
    for flow in flows:
        for step in flow.steps:
            if step.kind is not StepKind.EXECUTE:
                continue
            if not step.tool:
                continue
            if step.tool not in names:
                missing.append(
                    MissingStepTool(
                        flow_id=flow.flow_id,
                        flow_version=flow.version,
                        step_id=step.step_id,
                        tool=step.tool,
                    )
                )
    return missing


def ensure_step_tools_available(
    flows: Iterable[FlowDefinition], registry: ToolRegistry
) -> None:
    """装配期闸门：有任何一个执行步骤的工具没注册，直接抛错。"""

    available = registry.names()
    missing = missing_step_tools(flows, available)
    if missing:
        raise UnavailableStepTool(missing, available)
