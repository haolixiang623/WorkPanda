"""跨主机共享状态：租约、取消信号、预算在 Redis 后端上的语义。

这里用两个独立的客户端（两条连接、两套对象）模拟两台主机：同一份数据、不同的
进程内存。要验证的都是"跨实例"才成立的性质——同一 (task, step) 只有一个执行者、
取消能送达另一台机器、预算不因为换了机器就重新计。

后端是进程内的假 Redis（`tests/fake_redis.py`），只覆盖共享状态真正发出的命令；
CI 里没有 Redis 也不影响这些用例的执行。
"""

from __future__ import annotations

import json
import time
import uuid
from typing import Any

import pytest

from tests.fake_redis import FakeRedisServer
from workpanda.application.ports import ExecutionOutcome, ExecutionRequest
from workpanda.bootstrap import (
    build_budget_ledger,
    build_cancel_registry,
    build_lease_store,
    build_shared_state,
)
from workpanda.config import Settings
from workpanda.harness.budget import Budget, BudgetExhausted
from workpanda.harness.cancel import SIGNAL_UNAVAILABLE_REASON, CancelRegistry, CancelSignalError
from workpanda.harness.clients import ScriptedModelClient, tool_call
from workpanda.harness.executor import HarnessExecutor
from workpanda.harness.models import ModelResponse, ToolSpec
from workpanda.harness.shared_state import SharedStateError, budget_key
from workpanda.harness.tools import ToolContext, ToolRegistry
from workpanda.infrastructure.redis_client import RedisClient, RedisTarget
from workpanda.infrastructure.redis_shared_state import (
    RedisBudgetLedger,
    RedisCancelSignalStore,
    RedisSharedState,
    RedisStepLeaseStore,
)

SUCCESS = json.dumps(
    {"outcome": "succeeded", "summary": "已回显", "evidence": "echo:hi"},
    ensure_ascii=False,
)


@pytest.fixture
def redis_server():
    server = FakeRedisServer()
    try:
        yield server
    finally:
        server.stop()


@pytest.fixture
def host_a(redis_server: FakeRedisServer):
    state = RedisSharedState(RedisClient(redis_server.url), prefix="wp-test")
    try:
        yield state
    finally:
        state.close()


@pytest.fixture
def host_b(redis_server: FakeRedisServer):
    state = RedisSharedState(RedisClient(redis_server.url), prefix="wp-test")
    try:
        yield state
    finally:
        state.close()


class SubmitTool:
    """非幂等提交：漏拦截就是一次真实的重复动作。"""

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
        return f"echo:{arguments['value']}"


def request_for() -> ExecutionRequest:
    return ExecutionRequest(
        task_id=uuid.uuid4(),
        flow_id="test.flow",
        flow_version="1",
        step_id="submit",
        tool="test_submit",
        instruction="提交",
        arguments={"value": "hi"},
    )


def successful_client() -> ScriptedModelClient:
    return ScriptedModelClient(
        [
            ModelResponse(tool_calls=[tool_call("test_submit", call_id="c1", value="hi")]),
            ModelResponse(text=SUCCESS),
        ]
    )


# ---------- 原语 ----------


def test_acquire_is_exclusive_across_hosts(
    host_a: RedisSharedState, host_b: RedisSharedState
) -> None:
    assert host_a.try_acquire("k", "writer-a", 30) is True
    assert host_b.try_acquire("k", "writer-b", 30) is False, "另一台主机不能同时持有"
    # 非持有者释放必须无效：否则一台机器能把另一台机器的保护抹掉。
    assert host_b.release("k", "writer-b") is False
    assert host_a.try_acquire("k", "writer-c", 30) is False

    assert host_a.release("k", "writer-a") is True
    assert host_b.try_acquire("k", "writer-b", 30) is True


def test_expired_lease_can_be_reclaimed(host_a: RedisSharedState, host_b: RedisSharedState) -> None:
    assert host_a.try_acquire("k", "dead-host", 0.05) is True
    time.sleep(0.15)
    assert host_b.try_acquire("k", "live-host", 30) is True, "TTL 是崩溃后的逃生口"


def test_lease_is_scoped_to_task_and_step(host_a: RedisSharedState) -> None:
    store = RedisStepLeaseStore(host_a)
    task = uuid.uuid4()

    assert store.acquire(task, "submit", "a", 30) is True
    assert store.acquire(task, "submit", "b", 30) is False
    assert store.acquire(task, "collect", "b", 30) is True
    assert store.acquire(uuid.uuid4(), "submit", "c", 30) is True


# ---------- 取消信号 ----------


def test_cancel_reaches_the_other_host(host_a: RedisSharedState, host_b: RedisSharedState) -> None:
    executor_side = CancelRegistry(shared=RedisCancelSignalStore(host_a), poll_interval=0.02)
    http_side = CancelRegistry(shared=RedisCancelSignalStore(host_b), poll_interval=0.02)
    task_id = uuid.uuid4()

    token = executor_side.begin(task_id, "submit")
    assert token.cancelled is False
    assert http_side.cancel_task(task_id, "user_cancelled") == 0
    assert token.cancelled is True, "下一次边界检查必须能读到另一台主机写的信号"
    assert token.reason == "user_cancelled"
    executor_side.end(task_id, "submit")


def test_cancel_written_before_begin_is_seen(
    host_a: RedisSharedState, host_b: RedisSharedState
) -> None:
    http_side = CancelRegistry(shared=RedisCancelSignalStore(host_a), poll_interval=0.02)
    executor_side = CancelRegistry(shared=RedisCancelSignalStore(host_b), poll_interval=0.02)
    task_id = uuid.uuid4()

    http_side.cancel_task(task_id, "user_cancelled")
    token = executor_side.begin(task_id, "submit")
    assert token.cancelled is True, "执行开始前的取消不能被无声丢掉"
    assert token.reason == "user_cancelled"


def test_expired_signal_does_not_block_a_later_run(
    host_a: RedisSharedState, host_b: RedisSharedState
) -> None:
    writer = RedisCancelSignalStore(host_a)
    reader = RedisCancelSignalStore(host_b)
    task_id = uuid.uuid4()

    writer.signal(task_id, "user_cancelled", ttl_seconds=0.05)
    assert reader.reason_for(task_id) == "user_cancelled"
    time.sleep(0.15)
    assert reader.reason_for(task_id) is None


def test_signal_write_failure_is_reported(redis_server: FakeRedisServer) -> None:
    registry = CancelRegistry(shared=RedisCancelSignalStore(_state_for(redis_server)))
    redis_server.stop()

    with pytest.raises(CancelSignalError):
        registry.cancel_task(uuid.uuid4(), "user_cancelled")


def test_unreadable_signal_fails_closed(redis_server: FakeRedisServer) -> None:
    registry = CancelRegistry(
        shared=RedisCancelSignalStore(_state_for(redis_server)), poll_interval=0.02
    )
    token = registry.begin(uuid.uuid4(), "submit")
    redis_server.stop()

    assert token.cancelled is True
    assert token.reason == SIGNAL_UNAVAILABLE_REASON


# ---------- 预算账本 ----------


def _ledger(state: RedisSharedState) -> RedisBudgetLedger:
    return RedisBudgetLedger(state, ttl_seconds=300)


def test_budget_is_not_counted_twice_across_hosts(
    host_a: RedisSharedState, host_b: RedisSharedState
) -> None:
    task_id = uuid.uuid4()
    first = Budget(
        max_steps=4, max_tool_calls=4, ledger=_ledger(host_a), task_id=task_id, step_id="s"
    )
    second = Budget(
        max_steps=4, max_tool_calls=4, ledger=_ledger(host_b), task_id=task_id, step_id="s"
    )

    first.consume_step()
    second.consume_step()
    first.consume_tool_call()

    assert first.steps == 1
    assert second.steps == 2, "换一台机器续跑不能把步数重置"
    # 另一台已经用掉一次调用：账本里是 1，本机这份 Budget 只是账本的镜像，
    # 下一次扣减必须接着累计（2），而不是从 1 重新开始。
    assert _ledger_total(host_b, task_id, "tool_calls") == 1
    second.consume_tool_call()
    assert second.tool_calls == 2
    assert first.tool_calls == 1


def test_shared_budget_limit_covers_both_hosts(
    host_a: RedisSharedState, host_b: RedisSharedState
) -> None:
    task_id = uuid.uuid4()
    first = Budget(
        max_steps=1, max_tool_calls=2, ledger=_ledger(host_a), task_id=task_id, step_id="s"
    )
    second = Budget(
        max_steps=1, max_tool_calls=2, ledger=_ledger(host_b), task_id=task_id, step_id="s"
    )

    first.reserve_tool_calls(2)
    with pytest.raises(BudgetExhausted) as caught:
        second.reserve_tool_calls(1)
    assert caught.value.reason == "max_tool_calls"
    # 失败的那次预约必须回滚，不能把额度白白吃掉。
    assert _ledger_total(host_a, task_id, "tool_calls") == 2


def _ledger_total(state: RedisSharedState, task_id: uuid.UUID, counter: str) -> int:
    """无副作用地读回真实用量：HINCRBY 0 不改变值，返回的仍是累计量。"""

    return state.add(budget_key(state.prefix, task_id, "s"), counter, 0, 300)


def test_budget_ledger_failure_is_refused(redis_server: FakeRedisServer) -> None:
    ledger = _ledger(_state_for(redis_server))
    budget = Budget(max_steps=2, ledger=ledger, task_id=uuid.uuid4(), step_id="s")
    redis_server.stop()

    with pytest.raises(BudgetExhausted) as caught:
        budget.consume_step()
    assert caught.value.reason == "budget_store_unavailable"


def test_zero_limit_still_refuses_instead_of_becoming_unlimited(
    host_a: RedisSharedState,
) -> None:
    """`limit=0` 必须照旧拒绝，不能被账本读成"不限"。

    进程内语义里步数与调用次数的 0 是"一次都不许"（`_steps >= 0` 立刻拒绝），
    只有 token 的 0 才是"不限"。账本若把两者混为一谈，配错一个 0 就会把上限
    整条抹掉——那是 fail-open，比拒绝危险得多。
    """

    task_id = uuid.uuid4()
    budget = Budget(
        max_steps=0, max_tool_calls=0, ledger=_ledger(host_a), task_id=task_id, step_id="s"
    )

    with pytest.raises(BudgetExhausted) as caught:
        budget.consume_step()
    assert caught.value.reason == "max_steps"
    with pytest.raises(BudgetExhausted) as caught:
        budget.consume_tool_call()
    assert caught.value.reason == "max_tool_calls"
    # 被拒的那次必须回滚，不能留下用量把后续真额度也算歪。
    assert _ledger_total(host_a, task_id, "steps") == 0
    assert _ledger_total(host_a, task_id, "tool_calls") == 0


def test_zero_token_limit_is_unlimited_through_the_ledger(host_a: RedisSharedState) -> None:
    """`max_tokens=0` 是"不限"：接上账本后这个语义不能变。"""

    task_id = uuid.uuid4()
    budget = Budget(max_tokens=0, ledger=_ledger(host_a), task_id=task_id, step_id="s")

    budget.consume_tokens(10_000)
    budget.consume_tokens(10_000)
    assert budget.tokens == 20_000
    assert _ledger_total(host_a, task_id, "tokens") == 20_000


def test_budget_without_ledger_is_unchanged() -> None:
    """默认路径必须与之前完全一致：没有账本时仍按进程内计数。"""

    budget = Budget(max_steps=2, initial_steps=1)
    budget.consume_step()
    with pytest.raises(BudgetExhausted) as caught:
        budget.consume_step()
    assert caught.value.reason == "max_steps"
    assert budget.snapshot()["shared_ledger"] is False


# ---------- 接进执行器 ----------


def _executor(state: RedisSharedState, tool: SubmitTool, **kwargs: Any) -> HarnessExecutor:
    registry = ToolRegistry()
    registry.register(tool)
    return HarnessExecutor(
        successful_client(),
        registry=registry,
        deadline_seconds=30,
        leases=RedisStepLeaseStore(state),
        budget_ledger=_ledger(state),
        **kwargs,
    )


def test_second_host_is_refused_for_the_same_step(
    host_a: RedisSharedState, host_b: RedisSharedState
) -> None:
    request = request_for()
    holder = RedisStepLeaseStore(host_a)
    assert holder.acquire(request.task_id, request.step_id, "host-a", 60) is True

    tool = SubmitTool()
    executor = _executor(host_b, tool)
    result = executor.execute(request)

    assert result.outcome is ExecutionOutcome.UNCONFIRMED
    assert result.error_code == "checkpoint_busy"
    assert tool.invocations == 0, "被拒的一侧绝不能再产生副作用"
    assert holder.acquire(request.task_id, request.step_id, "host-c", 60) is False


def test_successful_step_releases_the_lease_for_other_hosts(
    host_a: RedisSharedState, host_b: RedisSharedState
) -> None:
    request = request_for()
    tool = SubmitTool()
    result = _executor(host_a, tool).execute(request)

    assert result.outcome is ExecutionOutcome.SUCCEEDED
    assert tool.invocations == 1
    # 释放之后另一台主机必须能接手：跨主机也要"用完即还"。
    assert (
        RedisStepLeaseStore(host_b).acquire(request.task_id, request.step_id, "host-b", 60) is True
    )


# ---------- 装配 ----------


def test_bootstrap_wires_all_three_to_redis(redis_server: FakeRedisServer) -> None:
    settings = Settings(
        store_backend="memory",
        enable_demo_flow=False,
        harness_shared_backend="redis",
        harness_redis_url=redis_server.url,
        harness_lease_enabled=True,
    )
    shared, shared_closers = build_shared_state(settings)
    registry, registry_closers = build_cancel_registry(settings, shared)
    leases, lease_closers = build_lease_store(settings, shared)
    ledger, ledger_closers = build_budget_ledger(settings, shared)
    try:
        assert isinstance(shared, RedisSharedState)
        assert isinstance(leases, RedisStepLeaseStore)
        assert isinstance(ledger, RedisBudgetLedger)

        # 取消登记表确实写到了 Redis：另开一条连接（默认前缀，与装配一致）读回同一任务。
        task_id = uuid.uuid4()
        registry.cancel_task(task_id, "user_cancelled")
        reader = RedisCancelSignalStore(RedisSharedState(RedisClient(redis_server.url)))
        assert reader.reason_for(task_id) == "user_cancelled"

        # 预算账本同样落在 Redis：另一条连接读到同一份用量。
        scope = uuid.uuid4()
        ledger.reserve(scope, "s", "tool_calls", 1, 10)
        other = RedisBudgetLedger(RedisSharedState(RedisClient(redis_server.url)), ttl_seconds=300)
        assert other.reserve(scope, "s", "tool_calls", 1, 10) == 2
    finally:
        for closer in lease_closers + ledger_closers + registry_closers + shared_closers:
            closer()


def test_missing_redis_url_fails_loudly() -> None:
    with pytest.raises(ValueError, match="harness_redis_url"):
        build_shared_state(Settings(harness_shared_backend="redis"))


def test_unreachable_redis_fails_at_startup() -> None:
    """配置成共享却连不上时，宁可起不来，也不要"以为自己是唯一执行者"。"""

    settings = Settings(harness_shared_backend="redis", harness_redis_url="redis://127.0.0.1:1/0")
    with pytest.raises(SharedStateError):
        build_shared_state(settings)


def test_tls_url_is_rejected() -> None:
    with pytest.raises(SharedStateError, match="TLS"):
        RedisTarget.parse("rediss://127.0.0.1:6379/0")


def _state_for(server: FakeRedisServer) -> RedisSharedState:
    """取一个独立连接：`stop()` 之后仍需持有对象来验证失败路径。"""

    return RedisSharedState(RedisClient(server.url), prefix="wp-test")
