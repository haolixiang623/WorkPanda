"""工具线程台账：超时后仍在跑的线程要被数清，触顶就拒绝再起新的。

背景：工具超时的实现是"放弃等待"，线程无法被强制终止，会跑到自然结束。
只看返回值的话，进程里到底堆了多少卡住的线程没人知道——真实长任务里这是
句柄与内存被悄悄吃光的路径。这一组用例钉住三件事：数得清、触顶拒绝、线程
真的结束后名额能放回来。
"""

from __future__ import annotations

import threading
import time
import uuid
from typing import Any

from workpanda.harness.models import RiskLevel, ToolCall, ToolOutcome, ToolSpec
from workpanda.harness.tools import (
    RunningToolThreads,
    ToolContext,
    ToolGrant,
    ToolRegistry,
)

TASK_ID = uuid.uuid4()


class HangingTool:
    """一直卡着，直到测试放行；用来制造"超时但线程还活着"的状态。"""

    spec = ToolSpec(name="test_hang", description="卡住不返回")

    def __init__(self) -> None:
        self.release = threading.Event()
        self.calls = 0
        self.started = threading.Event()

    def invoke(self, arguments: dict[str, Any], context: ToolContext) -> str:
        self.calls += 1
        self.started.set()
        # 上限只是防止测试自身挂死，不代表工具"该"跑这么久。
        self.release.wait(5.0)
        return "终于回来了"


class QuickTool:
    spec = ToolSpec(name="test_quick", description="立刻返回")

    def __init__(self) -> None:
        self.calls = 0

    def invoke(self, arguments: dict[str, Any], context: ToolContext) -> str:
        self.calls += 1
        return "ok"


def context() -> ToolContext:
    return ToolContext(task_id=TASK_ID, flow_id="test.direct", step_id="query")


def call(name: str) -> ToolCall:
    return ToolCall(call_id=f"{name}-1", name=name, arguments={})


def grant(registry: ToolRegistry, *names: str, timeout: float = 0.1) -> ToolGrant:
    return ToolGrant(
        registry=registry,
        allowed=names,
        call_timeout_seconds=timeout,
        retry_backoff_seconds=0,
    )


def test_finished_calls_leave_no_trace_in_the_ledger() -> None:
    registry = ToolRegistry()
    registry.register(QuickTool())

    record = grant(registry, "test_quick").call(call("test_quick"), context())

    assert record.outcome is ToolOutcome.OK
    assert registry.threads.stats() == {"live": 0, "stuck": 0, "max_stuck": 16}


def test_timed_out_thread_is_counted_as_stuck_until_it_really_exits() -> None:
    tool = HangingTool()
    registry = ToolRegistry()
    registry.register(tool)

    record = grant(registry, "test_hang", timeout=0.05).call(call("test_hang"), context())

    assert record.outcome is ToolOutcome.TIMEOUT
    assert record.error_code == "tool_timeout"
    stats = registry.threads.stats()
    assert stats["stuck"] == 1
    assert stats["live"] == 1

    # 线程自己跑完之后名额要还回来，否则上限会被历史调用永久占死。
    tool.release.set()
    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline and registry.threads.stats()["live"]:
        time.sleep(0.01)
    assert registry.threads.stats()["live"] == 0


def test_capacity_gate_refuses_new_calls_when_stuck_threads_hit_the_cap() -> None:
    hanging = HangingTool()
    quick = QuickTool()
    registry = ToolRegistry(max_stuck_threads=1)
    registry.register(hanging)
    registry.register(quick)
    guard = grant(registry, "test_hang", "test_quick", timeout=0.05)

    first = guard.call(call("test_hang"), context())
    assert first.outcome is ToolOutcome.TIMEOUT

    blocked = guard.call(call("test_quick"), context())

    # 拒绝发生在执行之前：确定没执行，按 BLOCKED 入账，副作用账本不会多一笔。
    assert blocked.outcome is ToolOutcome.BLOCKED
    assert blocked.error_code == "tool_capacity"
    assert "上限" in blocked.content
    assert quick.calls == 0

    hanging.release.set()
    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline and registry.threads.stats()["live"]:
        time.sleep(0.01)

    recovered = guard.call(call("test_quick"), context())
    assert recovered.outcome is ToolOutcome.OK
    assert quick.calls == 1


def test_zero_cap_disables_the_gate() -> None:
    """显式设 0 表示关闭闸门：行为与加入本机制之前一致。"""

    guard = RunningToolThreads(max_stuck=0)
    handles = [guard.acquire("test_hang") for _ in range(3)]
    assert all(handle is not None for handle in handles)
    for handle in handles[:2]:
        guard.abandon(handle)
    assert guard.stats()["stuck"] == 2
    assert guard.acquire("test_hang") is not None


def test_abandoning_an_already_finished_call_is_harmless() -> None:
    """主线程与工作线程会抢跑：线程先结束、主线程后标记放弃，不能凭空多出一个卡住。"""

    guard = RunningToolThreads(max_stuck=1)
    handle = guard.acquire("test_quick")
    assert handle is not None
    guard.release(handle)
    guard.abandon(handle)

    assert guard.stats() == {"live": 0, "stuck": 0, "max_stuck": 1}


class LimitedRiskTool:
    """带调用上限与中风险声明的工具，用来验证拒绝时留痕的字段没有被简化。"""

    spec = ToolSpec(
        name="test_limited",
        description="只允许调用一次",
        risk=RiskLevel.MEDIUM,
        max_calls=1,
    )

    def invoke(self, arguments: dict[str, Any], context: ToolContext) -> str:
        return "ok"


def test_capacity_refusal_keeps_risk_and_spends_the_attempt_reservation() -> None:
    """拒绝也要占掉这次尝试：否则模型可以对着被闸门挡住的工具无限重试。"""

    hanging = HangingTool()
    limited = LimitedRiskTool()
    registry = ToolRegistry(max_stuck_threads=1)
    registry.register(hanging)
    registry.register(limited)
    guard = grant(registry, "test_hang", "test_limited", timeout=0.05)

    guard.call(call("test_hang"), context())
    blocked = guard.call(
        ToolCall(call_id="limited-1", name="test_limited", arguments={}), context()
    )

    assert blocked.outcome is ToolOutcome.BLOCKED
    assert blocked.error_code == "tool_capacity"
    assert blocked.risk is RiskLevel.MEDIUM

    # 预留的这一次尝试已经花掉：再来一次会先撞上调用上限，而不是无限重试。
    again = guard.call(
        ToolCall(call_id="limited-2", name="test_limited", arguments={}), context()
    )
    assert again.error_code == "tool_call_limit"
    hanging.release.set()
