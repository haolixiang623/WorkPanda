"""执行步骤声明的工具必须真的在进程里。

这组用例守的是一条**没有报错**的失效路径：流程写了一个装配层没注册的工具名，
`HarnessExecutor._grant()` 的授权交集静默变空，模型照样被调用、照样回 `succeeded`，
而一个工具都没跑过。旧版自检流程（`demo.generic@1`）的 `tool="model_echo"` 就是
这个形状——它只在脚本化执行器下"跑得通"。

因此这里钉住三件事：
1. 自检流程声明的执行工具全都注册过（否则 §1 的装配期闸门会把它拦下）；
2. 缺口能被指名报出来，且装配直接失败，而不是留到运行时；
3. 真执行器（非脚本化）跑自检流程时，工具**真的被调用**，依据来自工具返回值。
"""

from __future__ import annotations

import json
import uuid

import pytest

from workpanda.application.ports import ExecutionOutcome, ExecutionRequest
from workpanda.bootstrap import build_container
from workpanda.config import Settings
from workpanda.domain.flow import (
    FlowDefinition,
    FlowRegistry,
    FlowStatus,
    StepDefinition,
    StepKind,
)
from workpanda.domain.models import (
    TaskConfirmRequest,
    TaskCreateRequest,
    TaskStatus,
)
from workpanda.harness.builtin import default_registry
from workpanda.harness.clients import ScriptedModelClient, tool_call
from workpanda.harness.executor import HarnessExecutor
from workpanda.harness.models import MessageRole, ModelResponse
from workpanda.harness.preflight import (
    UnavailableStepTool,
    ensure_step_tools_available,
    missing_step_tools,
)
from workpanda.infrastructure.demo_flows import DEMO_FLOW, demo_flows

# 模型自述成功时的产物。依据必须写成"工具返回了什么"，否则契约层会判成未确认。
SUCCESS = json.dumps(
    {
        "outcome": "succeeded",
        "summary": "已读取系统时间",
        "evidence": "sys_clock 返回 utc=2026-01-01T00:00:00+00:00",
    },
    ensure_ascii=False,
)


def execute_step(flow: FlowDefinition) -> StepDefinition:
    """取流程里唯一的执行步骤；没有就说明用例前提不成立了。"""

    steps = [step for step in flow.steps if step.kind is StepKind.EXECUTE]
    assert steps, f"{flow.flow_id}@{flow.version} 没有执行步骤，用例前提不成立"
    assert len(steps) == 1, "这组用例假定单一执行步骤"
    return steps[0]


def flow_with_tool(flow_id: str, tool: str, *, version: str = "1") -> FlowDefinition:
    """构造一条只声明工具的迷你流程，用于制造"工具没注册"的现场。"""

    return FlowDefinition(
        flow_id=flow_id,
        version=version,
        title=f"{flow_id} 用例流程",
        status=FlowStatus.PUBLISHED,
        steps=(
            StepDefinition(
                step_id="run",
                kind=StepKind.EXECUTE,
                title="执行",
                tool=tool,
                instruction="调用工具并给出依据。",
            ),
        ),
    )


# ---------- §1 自检流程的工具必须都注册过 ----------


def test_demo_flow_only_declares_registered_tools() -> None:
    """旧版的 `model_echo` 从未注册过，这条断言本该在那个 PR 里就存在。"""

    registry, closers = default_registry()
    try:
        missing = missing_step_tools(demo_flows(), registry.names())

        assert missing == []
        # 反向兜底：若 demo_flows() 某天变成空列表或不再有执行步骤，上面的
        # `missing == []` 会因为"什么都没检查"而假绿。
        assert execute_step(DEMO_FLOW).tool in registry.names()
    finally:
        for close in closers:
            close()


# ---------- §2 缺口要指名报出来，并且拦住装配 ----------


def test_unknown_tool_is_reported_with_its_location() -> None:
    """报错必须能直接定位到"哪一版哪一步写了哪个工具"，否则等于让运维去猜。"""

    registry, closers = default_registry()
    try:
        # 正是旧版自检流程的形状：工具名只在脚本化执行器里存在。
        missing = missing_step_tools(
            [flow_with_tool("demo.generic", "model_echo", version="1")], registry.names()
        )

        assert len(missing) == 1
        item = missing[0]
        assert (item.flow_id, item.flow_version, item.step_id, item.tool) == (
            "demo.generic",
            "1",
            "run",
            "model_echo",
        )

        with pytest.raises(UnavailableStepTool) as caught:
            ensure_step_tools_available(
                [flow_with_tool("demo.generic", "model_echo", version="1")], registry
            )

        message = str(caught.value)
        assert "model_echo" in message
        # 同时说出"有哪些可用"，否则修复只能靠翻代码。
        assert "sys_clock" in message
        assert caught.value.missing[0].tool == "model_echo"
    finally:
        for close in closers:
            close()


def test_collect_step_without_tool_is_not_flagged() -> None:
    """只核对执行步骤：收集/转人工步骤本来就没有工具。"""

    assert missing_step_tools([DEMO_FLOW], ["sys_clock"]) == []


def test_draft_version_with_unknown_tool_does_not_block_assembly() -> None:
    """只核对**已发布**版本：草稿跑不起来不等于装配不上。"""

    published = flow_with_tool("demo.published", "sys_clock")
    draft = flow_with_tool("demo.draft", "model_echo").model_copy(
        update={"status": FlowStatus.DRAFT}
    )
    directory = FlowRegistry([published, draft])

    registry, closers = default_registry()
    try:
        assert {flow.flow_id for flow in directory.published()} == {"demo.published"}
        ensure_step_tools_available(directory.published(), registry)
    finally:
        for close in closers:
            close()


def test_container_refuses_to_boot_when_published_flow_declares_unknown_tool() -> None:
    """装配期闸门接在真实 harness 分支上：起不来，好过看起来跑通了。"""

    settings = Settings(
        store_backend="memory",
        harness_enabled=True,
        # 只要"配齐"即可：构造客户端不发请求，闸门在它之前就抛了。
        model_base_url="http://127.0.0.1:9/v1",
        model_api_key="test-key",
        model_name="test-model",
    )

    with pytest.raises(UnavailableStepTool) as caught:
        build_container(settings, flows=[flow_with_tool("demo.bad", "model_echo")])

    assert "model_echo" in str(caught.value)


def test_container_boots_when_published_flow_tools_are_registered() -> None:
    """对照组：工具注册过就不该拦。避免把闸门写成"永远抛错"。

    配置与上面那条**完全一致**（真 harness 分支），只有流程声明的工具换成已注册的
    那个——两条用例合起来才说明拦截来自对账，而不是来自"开了 harness 就起不来"。
    """

    settings = Settings(
        store_backend="memory",
        harness_enabled=True,
        model_base_url="http://127.0.0.1:9/v1",
        model_api_key="test-key",
        model_name="test-model",
    )
    container = build_container(
        settings, flows=[flow_with_tool("demo.good", "sys_clock")]
    )
    try:
        assert container.flows.resolve("demo.good").flow_id == "demo.good"
        # 前提校验：这条路真的进了真实 harness 分支，否则"没被拦"毫无意义。
        assert isinstance(container.executor, HarnessExecutor)
    finally:
        container.close()


# ---------- §3 真执行器下工具真的被调用 ----------


def test_demo_execute_step_calls_the_registered_tool() -> None:
    """自检流程在真 harness 下要跑出一次真实工具调用，而不是模型自述。

    这里不用 `ScriptedExecutor`：那个分支不跑模型循环，任何工具名都能"通过"，
    正是它让 `model_echo` 这个笔误活了下来。
    """

    step = execute_step(DEMO_FLOW)
    registry, closers = default_registry()
    client = ScriptedModelClient(
        [
            ModelResponse(tool_calls=[tool_call(step.tool or "", call_id="c1")]),
            ModelResponse(text=SUCCESS),
        ]
    )
    executor = HarnessExecutor(client, registry=registry)
    request = ExecutionRequest(
        task_id=uuid.uuid4(),
        flow_id=DEMO_FLOW.flow_id,
        flow_version=DEMO_FLOW.version,
        step_id=step.step_id,
        tool=step.tool or "",
        instruction=step.instruction,
        arguments={"subject": "链路自检"},
    )

    try:
        result = executor.execute(request)
    finally:
        for close in closers:
            close()

    assert result.outcome is ExecutionOutcome.SUCCEEDED
    # 一步一授权：模型看到的工具清单就是本步骤声明的那个，多一个都不行。
    assert client.calls[0]["tools"] == [step.tool]
    assert [record["name"] for record in result.output["tool_calls"]] == [step.tool]
    evidence = result.output["verification"]["tool_evidence"]
    assert evidence and evidence[0]["tool"] == step.tool
    # 依据必须来自工具返回值本身（sys_clock 的产出形如 utc=... local=... unix=...）。
    assert "utc=" in evidence[0]["content"]

    tool_messages = [
        message for message in client.calls[1]["messages"] if message.role is MessageRole.TOOL
    ]
    assert tool_messages and "utc=" in tool_messages[0].content


def test_demo_flow_completes_through_the_real_executor() -> None:
    """整条链路（收集 → 确认 → 执行 → 完成）在真执行器下走到 COMPLETED。

    单测执行器只证明"这一步能跑"；这条用例证明流程装配、凭证签发、结果落库都不
    依赖脚本化执行器的宽容。
    """

    step = execute_step(DEMO_FLOW)
    registry, closers = default_registry()
    client = ScriptedModelClient(
        [
            ModelResponse(tool_calls=[tool_call(step.tool or "", call_id="c1")]),
            ModelResponse(text=SUCCESS),
        ]
    )
    container = build_container(
        Settings(store_backend="memory", enable_demo_flow=False),
        flows=[DEMO_FLOW],
        executor=HarnessExecutor(client, registry=registry),
    )
    subject = "session-e2e"
    try:
        task = container.tasks.create_task(
            TaskCreateRequest(
                conversation_id="conversation-e2e",
                input_text="跑个自检",
                flow_id=DEMO_FLOW.flow_id,
                slots={"subject": "链路自检"},
            ),
            subject,
        )
        assert task.status is TaskStatus.WAITING_FOR_CONFIRMATION
        assert task.current_step == step.step_id

        task = container.tasks.confirm(
            task.task_id, TaskConfirmRequest(step_id=step.step_id), subject
        )

        assert task.status is TaskStatus.COMPLETED
        assert [rec["name"] for rec in task.result["tool_calls"]] == [step.tool]
        assert task.result["verification"]["tool_evidence"]
    finally:
        container.close()
        for close in closers:
            close()
