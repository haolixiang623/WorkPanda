"""Octop-Harness 适配器：模型有回复不等于业务成功。"""

import json
import uuid
from pathlib import Path

import pytest

from workpanda.application.ports import ExecutionOutcome, ExecutionRequest
from workpanda.infrastructure.octop_runtime import (
    HarnessUnavailable,
    OctopHarnessExecutor,
    OctopHarnessGateway,
    default_prompt,
    normalize_result,
    parse_contract,
)


class FakeMessage:
    def __init__(self, kind: str, content: str) -> None:
        self.type = kind
        self.content = content


def gateway_returning(text: str) -> dict:
    return {"messages": [FakeMessage("human", "问"), FakeMessage("ai", text)]}


class FakeGateway:
    def __init__(self, text: str | None = None, failure: str | None = None) -> None:
        self.text = text
        self.failure = failure
        self.calls: list[tuple[str, str, float]] = []

    def run(self, *, prompt: str, thread_id: str, timeout_seconds: float) -> dict:
        self.calls.append((prompt, thread_id, timeout_seconds))
        if self.failure == "timeout":
            raise TimeoutError("runtime timed out")
        if self.failure == "unavailable":
            raise HarnessUnavailable("octop-harness 未安装")
        if self.failure == "crash":
            raise RuntimeError("boom")
        return gateway_returning(self.text or "")

    def close(self) -> None:
        return None


def make_request() -> ExecutionRequest:
    return ExecutionRequest(
        task_id=uuid.uuid4(),
        flow_id="test.confirm",
        flow_version="1",
        step_id="submit",
        tool="test.submit",
        instruction="回显 subject",
        arguments={"subject": "主题", "contact": "123"},
    )


SUCCESS_JSON = json.dumps(
    {"outcome": "succeeded", "summary": "已回显", "evidence": "主题"}, ensure_ascii=False
)


def test_successful_contract_maps_to_succeeded() -> None:
    gateway = FakeGateway(SUCCESS_JSON)
    result = OctopHarnessExecutor(gateway, timeout_seconds=5).execute(make_request())

    assert result.outcome is ExecutionOutcome.SUCCEEDED
    assert result.output["verification"]["evidence"] == "主题"
    assert gateway.calls[0][2] == 5
    json.dumps(result.output)


def test_success_without_evidence_is_not_trusted() -> None:
    text = json.dumps({"outcome": "succeeded", "summary": "已完成", "evidence": ""})
    result = OctopHarnessExecutor(FakeGateway(text)).execute(make_request())

    assert result.outcome is ExecutionOutcome.UNCONFIRMED
    assert result.error_code == "missing_evidence"


def test_plain_refusal_is_unconfirmed_not_success() -> None:
    result = OctopHarnessExecutor(
        FakeGateway("当前运行时没有注册名为 test.submit 的工具，无法执行。")
    ).execute(make_request())

    assert result.outcome is ExecutionOutcome.UNCONFIRMED
    assert result.error_code == "unparsable_output"


def test_gateway_returning_normalized_dict_is_accepted() -> None:
    """网关无论返回原始 messages 还是已归一化文本，都不能被读成空回复。"""

    class NormalizedGateway(FakeGateway):
        def run(self, *, prompt: str, thread_id: str, timeout_seconds: float) -> dict:
            return {"text": SUCCESS_JSON, "message_count": 2}

    result = OctopHarnessExecutor(NormalizedGateway()).execute(make_request())

    assert result.outcome is ExecutionOutcome.SUCCEEDED
    assert result.output["message_count"] == 2


def test_explicit_failure_contract_maps_to_failed() -> None:
    text = json.dumps({"outcome": "failed", "summary": "外部系统拒绝", "evidence": "HTTP 403"})
    result = OctopHarnessExecutor(FakeGateway(text)).execute(make_request())

    assert result.outcome is ExecutionOutcome.FAILED
    assert result.error_code == "failed"


def test_code_fenced_json_is_accepted() -> None:
    text = f"```json\n{SUCCESS_JSON}\n```"
    outcome, detail = parse_contract(text)

    assert outcome is ExecutionOutcome.SUCCEEDED
    assert detail["evidence"] == "主题"


def test_timeout_maps_to_unconfirmed_not_failed() -> None:
    result = OctopHarnessExecutor(FakeGateway(failure="timeout")).execute(make_request())

    assert result.outcome is ExecutionOutcome.UNCONFIRMED
    assert result.error_code == "runtime_timeout"


def test_unexpected_exception_maps_to_unconfirmed() -> None:
    result = OctopHarnessExecutor(FakeGateway(failure="crash")).execute(make_request())

    assert result.outcome is ExecutionOutcome.UNCONFIRMED
    assert result.error_code == "runtime_error"


def test_missing_dependency_maps_to_failed() -> None:
    result = OctopHarnessExecutor(FakeGateway(failure="unavailable")).execute(make_request())

    assert result.outcome is ExecutionOutcome.FAILED
    assert result.error_code == "runtime_unavailable"


def test_normalize_extracts_last_assistant_text() -> None:
    result = normalize_result(
        {
            "messages": [
                FakeMessage("human", "问"),
                FakeMessage("ai", "答"),
                FakeMessage("tool", "x"),
            ]
        }
    )

    assert result == {"text": "答", "message_count": 3}


def test_prompt_scopes_to_single_step_and_requires_contract() -> None:
    prompt = default_prompt(make_request())

    assert "回显 subject" in prompt
    assert "test.submit" in prompt
    assert "outcome" in prompt
    assert "不得填 succeeded" in prompt


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
