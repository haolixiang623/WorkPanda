"""取消对所有执行器生效：至少"开始前"这一道不能漏。

背景：取消信号原本只有 `HarnessExecutor` 自己的循环在读。脚手架执行器不读它，
于是用户点了取消，正在排队的那一步照样从头跑完——包括那笔提交。
`CancellableExecutor` 统一了"执行开始前"这道边界，本组用例把它钉住。
"""

from __future__ import annotations

import json
import uuid

import pytest

from workpanda.application.ports import (
    ExecutionOutcome,
    ExecutionRequest,
    ExecutionResult,
)
from workpanda.bootstrap import build_container
from workpanda.config import Settings
from workpanda.harness.cancel import CancelRegistry
from workpanda.harness.cancel_executor import CancellableExecutor
from workpanda.infrastructure.adapters import ScriptedExecutor


def make_request(task_id: uuid.UUID | None = None) -> ExecutionRequest:
    return ExecutionRequest(
        task_id=task_id or uuid.uuid4(),
        flow_id="test.direct",
        flow_version="1",
        step_id="query",
        tool="test.query",
        instruction="执行该步骤并如实报告结果。",
    )


class CountingExecutor:
    """记录被调用次数的内层执行器。"""

    def __init__(self, result: ExecutionResult | None = None) -> None:
        self.calls = 0
        self._result = result or ExecutionResult(
            outcome=ExecutionOutcome.SUCCEEDED, output={"ok": True}
        )

    def execute(self, request: ExecutionRequest) -> ExecutionResult:
        self.calls += 1
        return self._result


class ExplodingExecutor:
    def execute(self, request: ExecutionRequest) -> ExecutionResult:
        raise RuntimeError("inner exploded")


def test_delegates_and_releases_the_registry_entry() -> None:
    registry = CancelRegistry()
    inner = CountingExecutor()
    executor = CancellableExecutor(inner, registry)

    result = executor.execute(make_request())

    assert result.outcome is ExecutionOutcome.SUCCEEDED
    assert inner.calls == 1
    # 登记表不随任务数增长：一次执行结束就该把自己那条清掉。
    assert len(registry) == 0


def test_cancel_before_start_skips_the_inner_executor() -> None:
    """暂存的取消（比执行先到）必须在这一步开始时立刻生效，且一步都不跑。"""

    registry = CancelRegistry()
    inner = CountingExecutor()
    executor = CancellableExecutor(inner, registry)
    task_id = uuid.uuid4()
    registry.cancel_task(task_id, "user_cancelled")

    result = executor.execute(make_request(task_id))

    assert inner.calls == 0
    assert result.outcome is ExecutionOutcome.UNCONFIRMED
    assert result.error_code == "cancelled"
    assert "user_cancelled" in (result.message or "")
    # 形状要和 harness 自己拒绝时一致：事件与审计都读这两个字段。
    assert result.output["stop_reason"] == "cancelled"
    assert result.output["model_calls"] == 0
    assert result.output["tool_calls"] == []
    assert result.output["verification"]["reason"] == "cancelled"
    assert result.output["duration_ms"] >= 0
    json.dumps(result.output)
    assert len(registry) == 0


def test_stored_cancel_is_consumed_only_once() -> None:
    """暂存的取消只拦住它该拦的那一次：后续新执行不该被一条旧不可见信号挡住。"""

    registry = CancelRegistry()
    inner = CountingExecutor()
    executor = CancellableExecutor(inner, registry)
    task_id = uuid.uuid4()
    registry.cancel_task(task_id, "user_cancelled")

    first = executor.execute(make_request(task_id))
    second = executor.execute(make_request(task_id))

    assert first.error_code == "cancelled"
    assert second.outcome is ExecutionOutcome.SUCCEEDED
    assert inner.calls == 1


def test_cancel_during_execution_does_not_rewrite_the_result() -> None:
    """内层读得到令牌就自己收尾；包装器不改判——已经发生的事不能替它编结论。"""

    registry = CancelRegistry()
    observed: list[bool] = []

    class CancellingInner:
        def execute(self, request: ExecutionRequest) -> ExecutionResult:
            token = registry.begin(request.task_id, request.step_id)
            registry.cancel_task(request.task_id, "user_cancelled")
            observed.append(token.cancelled)
            return ExecutionResult(
                outcome=ExecutionOutcome.UNCONFIRMED,
                error_code="cancelled",
                message="执行期间被取消",
                output={"stop_reason": "cancelled"},
            )

    executor = CancellableExecutor(CancellingInner(), registry)

    result = executor.execute(make_request())

    assert observed == [True]
    assert result.message == "执行期间被取消"
    assert result.output == {"stop_reason": "cancelled"}
    assert len(registry) == 0


def test_inner_failure_still_releases_the_registry_entry() -> None:
    registry = CancelRegistry()
    executor = CancellableExecutor(ExplodingExecutor(), registry)

    with pytest.raises(RuntimeError):
        executor.execute(make_request())

    assert len(registry) == 0


def test_inner_is_exposed_for_resource_cleanup() -> None:
    inner = CountingExecutor()
    assert CancellableExecutor(inner, CancelRegistry()).inner is inner


def test_default_container_wraps_the_scripted_executor() -> None:
    """默认装配（脚手架执行器）也应参与取消，而不是只有 harness 分支参与。"""

    container = build_container(
        Settings(
            store_backend="memory",
            enable_demo_flow=False,
            # 本机 .env 里可能配了模型凭据；显式关掉才能走到脚手架分支。
            harness_enabled=False,
            model_base_url=None,
            model_api_key=None,
        )
    )
    try:
        assert isinstance(container.executor, CancellableExecutor)
        assert isinstance(container.executor.inner, ScriptedExecutor)
    finally:
        container.close()


def test_stale_octop_env_does_not_resurrect_a_second_executor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Octop 运行时已移除：老环境变量不得再换掉执行器。

    部署脚本或历史 `.env` 里可能仍留着 `WORKPANDA_OCTOP_*`。配置类忽略未知键，
    本用例把这一点钉住——否则那些残留配置会变成一条看不见的第二执行路径。
    """

    monkeypatch.setenv("WORKPANDA_OCTOP_ENABLED", "true")
    monkeypatch.setenv("WORKPANDA_OCTOP_API_KEY", "sk-test-not-a-real-key")

    assert not [name for name in Settings.model_fields if name.startswith("octop")]

    container = build_container(
        Settings(
            store_backend="memory",
            enable_demo_flow=False,
            harness_enabled=False,
            model_base_url=None,
            model_api_key=None,
        )
    )
    try:
        assert isinstance(container.executor, CancellableExecutor)
        assert isinstance(container.executor.inner, ScriptedExecutor)
    finally:
        container.close()
