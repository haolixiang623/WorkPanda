"""运维只读接口：默认关闭，凭据错误返回 401，且不改写业务状态。"""

import pytest
from fastapi.testclient import TestClient

from tests.support import TEST_FLOWS
from workpanda.api.security import SESSION_HEADER
from workpanda.bootstrap import build_container
from workpanda.config import Settings
from workpanda.domain.models import TaskCreateRequest
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


@pytest.mark.parametrize("path", ["/api/ops/tasks", "/api/ops/tasks/{id}"])
def test_ops_paths_are_exposed_under_prefix(path: str) -> None:
    client, _ = make_client(ops_token=None)
    url = path.replace("{id}", "00000000-0000-0000-0000-000000000000")
    assert client.get(url).status_code == 404
