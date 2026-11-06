"""Octop-Harness 适配器：结果三态映射不得夸大成功。"""

import uuid
from pathlib import Path

import pytest

from workpanda.application.ports import ExecutionOutcome, ExecutionRequest
from workpanda.infrastructure.octop_runtime import (
    HarnessUnavailable,
    OctopHarnessExecutor,
    OctopHarnessGateway,
    default_prompt,
)


class FakeGateway:
    def __init__(self, behavior: str = "ok") -> None:
        self.behavior = behavior
        self.calls: list[tuple[str, str, float]] = []

    def run(self, *, prompt: str, thread_id: str, timeout_seconds: float) -> dict:
        self.calls.append((prompt, thread_id, timeout_seconds))
        if self.behavior == "timeout":
            raise TimeoutError("runtime timed out")
        if self.behavior == "unavailable":
            raise HarnessUnavailable("octop-harness 未安装")
        if self.behavior == "crash":
            raise RuntimeError("boom")
        return {"answer": "ok"}

    def close(self) -> None:
        return None


def make_request() -> ExecutionRequest:
    return ExecutionRequest(
        task_id=uuid.uuid4(),
        flow_id="test.confirm",
        flow_version="1",
        step_id="submit",
        tool="test.submit",
        arguments={"subject": "主题", "contact": "123"},
    )


def test_successful_call_maps_to_succeeded() -> None:
    gateway = FakeGateway()
    result = OctopHarnessExecutor(gateway, timeout_seconds=5).execute(make_request())

    assert result.outcome is ExecutionOutcome.SUCCEEDED
    assert result.output["runtime"] == {"answer": "ok"}
    assert gateway.calls[0][2] == 5


def test_timeout_maps_to_unconfirmed_not_failed() -> None:
    result = OctopHarnessExecutor(FakeGateway("timeout")).execute(make_request())

    assert result.outcome is ExecutionOutcome.UNCONFIRMED
    assert result.error_code == "runtime_timeout"


def test_unexpected_exception_maps_to_unconfirmed() -> None:
    result = OctopHarnessExecutor(FakeGateway("crash")).execute(make_request())

    assert result.outcome is ExecutionOutcome.UNCONFIRMED
    assert result.error_code == "runtime_error"


def test_missing_dependency_maps_to_failed() -> None:
    result = OctopHarnessExecutor(FakeGateway("unavailable")).execute(make_request())

    assert result.outcome is ExecutionOutcome.FAILED
    assert result.error_code == "runtime_unavailable"


def test_prompt_scopes_to_single_step() -> None:
    prompt = default_prompt(make_request())

    assert "test.submit" in prompt
    assert "submit" in prompt
    assert "不要自行改变流程" in prompt


# ---------- 与真实 SDK 的构造契约（未安装 SDK 时跳过） ----------


def test_missing_api_key_is_reported_before_building_agent() -> None:
    gateway = OctopHarnessGateway(agent_name="test", model="gpt-4o-mini", api_key=None)
    try:
        with pytest.raises(HarnessUnavailable):
            gateway._ensure_agent()
    finally:
        gateway.close()


def test_real_sdk_builds_harness_agent() -> None:
    pytest.importorskip("octop_harness")
    workspace = Path(".pytest-tmp") / f"octop-{uuid.uuid4().hex}"
    gateway = OctopHarnessGateway(
        agent_name="workpanda-test",
        model="gpt-4o-mini",
        provider="openai",
        # 只验证配置装配，不发起真实模型调用。
        api_key="sk-test-not-a-real-key",
        workspace=str(workspace),
    )
    try:
        agent = gateway._ensure_agent()
        assert hasattr(agent, "call")
        assert workspace.exists()
    finally:
        gateway.close()
