"""运维只读接口：默认关闭，凭据错误返回 401，且不改写业务状态。"""

import uuid

import pytest
from fastapi.testclient import TestClient

from tests.support import TEST_FLOWS
from workpanda.api.security import SESSION_HEADER
from workpanda.bootstrap import build_container
from workpanda.config import Settings
from workpanda.domain.models import TaskCreateRequest
from workpanda.harness.trace import TraceRecord
from workpanda.infrastructure.adapters import ScriptedExecutor
from workpanda.main import create_app

OPS_TOKEN = "ops-token-for-tests"


def make_client(*, ops_token: str | None) -> tuple[TestClient, object]:
    container = build_container(
        Settings(store_backend="memory", enable_demo_flow=False, ops_token=ops_token),
        flows=TEST_FLOWS,
        executor=ScriptedExecutor(),
    )
    client = TestClient(create_app(container=container))
    client.headers.update({SESSION_HEADER: container.signer.issue()})
    return client, container


def seed_task(container) -> str:
    task = container.tasks.create_task(
        TaskCreateRequest(
            conversation_id="conversation-1",
            input_text="测试",
            flow_id="test.confirm",
            slots={"subject": "主题", "contact": "123"},
        ),
        "session-a",
    )
    return str(task.task_id)


def test_ops_api_is_hidden_when_not_configured() -> None:
    client, _ = make_client(ops_token=None)

    response = client.get("/api/ops/tasks")
    assert response.status_code == 404


def test_ops_api_requires_token() -> None:
    client, _ = make_client(ops_token=OPS_TOKEN)

    assert client.get("/api/ops/tasks").status_code == 401
    assert (
        client.get("/api/ops/tasks", headers={"X-WorkPanda-Ops-Token": "wrong"}).status_code
        == 401
    )


def test_ops_api_lists_tasks_across_sessions() -> None:
    client, container = make_client(ops_token=OPS_TOKEN)
    task_id = seed_task(container)

    response = client.get(
        "/api/ops/tasks", headers={"X-WorkPanda-Ops-Token": OPS_TOKEN}
    )
    assert response.status_code == 200
    body = response.json()
    assert [item["task_id"] for item in body] == [task_id]


def test_ops_api_reads_events_of_any_task() -> None:
    client, container = make_client(ops_token=OPS_TOKEN)
    task_id = seed_task(container)

    response = client.get(
        f"/api/ops/tasks/{task_id}/events", headers={"X-WorkPanda-Ops-Token": OPS_TOKEN}
    )
    assert response.status_code == 200
    assert response.json()[0]["type"] == "accepted"


def test_ops_api_cannot_be_reached_with_session_token_only() -> None:
    client, container = make_client(ops_token=OPS_TOKEN)
    seed_task(container)

    # 群众会话即使有效，也不能读运维接口。
    response = client.get("/api/ops/tasks", headers={"X-WorkPanda-Ops-Token": ""})
    assert response.status_code == 401


@pytest.mark.parametrize(
    "path", ["/api/ops/tasks", "/api/ops/tasks/{id}", "/api/ops/traces", "/api/ops/traces/summary"]
)
def test_ops_paths_are_exposed_under_prefix(path: str) -> None:
    client, _ = make_client(ops_token=None)
    url = path.replace("{id}", "00000000-0000-0000-0000-000000000000")
    assert client.get(url).status_code == 404


def test_ops_api_lists_traces_and_reports_summary() -> None:
    client, container = make_client(ops_token=OPS_TOKEN)
    task_id = uuid.uuid4()
    for outcome, error_code in (("succeeded", None), ("unconfirmed", "unverified_success")):
        container.traces.save(
            TraceRecord(
                trace_id=uuid.uuid4().hex,
                task_id=task_id,
                flow_id="test.flow",
                flow_version="1",
                step_id="run",
                outcome=outcome,
                stop_reason="completed",
                error_code=error_code,
                model_calls=2,
                tool_calls=1,
                successful_tool_calls=1,
                tokens=120,
                duration_ms=40,
            )
        )

    headers = {"X-WorkPanda-Ops-Token": OPS_TOKEN}
    traces = client.get("/api/ops/traces", params={"task_id": str(task_id)}, headers=headers)
    summary = client.get("/api/ops/traces/summary", headers=headers)

    assert traces.status_code == 200
    assert len(traces.json()) == 2
    assert summary.status_code == 200
    assert summary.json()["total"] == 2
    assert summary.json()["unconfirmed_rate"] == 0.5
    assert summary.json()["tokens_total"] == 240


def test_ops_traces_require_token() -> None:
    client, _ = make_client(ops_token=OPS_TOKEN)

    assert client.get("/api/ops/traces").status_code == 401
    assert client.get("/api/ops/traces/summary").status_code == 401
