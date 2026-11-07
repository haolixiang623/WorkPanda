"""HTTP 接口行为：状态码、错误码和群众可见的安全边界。"""

import uuid

import pytest
from fastapi.testclient import TestClient

from workpanda.api.security import SESSION_HEADER
from workpanda.bootstrap import Container
from workpanda.main import create_app


@pytest.fixture
def client(container: Container) -> TestClient:
    test_client = TestClient(create_app(container=container))
    test_client.headers.update({SESSION_HEADER: container.signer.issue()})
    return test_client


def create_task(client: TestClient, **overrides) -> dict:
    payload = {
        "conversation_id": "conversation-1",
        "input_text": "我要办理测试事项",
        "flow_id": "test.confirm",
    }
    payload.update(overrides)
    response = client.post("/api/tasks", json=payload)
    assert response.status_code == 201, response.text
    return response.json()


def test_health_check(client: TestClient) -> None:
    assert client.get("/healthz").json() == {"status": "ok"}


def test_create_task_returns_waiting_state(client: TestClient) -> None:
    body = create_task(client)
    assert body["status"] == "waiting_for_user"
    assert body["pending_fields"] == ["subject", "contact"]
    # 群众可见状态由服务端给出，前端不再自己从 status 推断。
    assert body["ui_state"] == "awaiting_input"


def test_full_confirmation_flow(client: TestClient) -> None:
    body = create_task(client, slots={"subject": "测试主题", "contact": "12345"})
    assert body["status"] == "waiting_for_confirmation"

    response = client.post(
        f"/api/tasks/{body['task_id']}/confirm",
        json={"step_id": "submit", "accepted": True},
    )
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "completed"
    assert response.json()["ui_state"] == "completed"


def test_unknown_task_returns_404(client: TestClient) -> None:
    response = client.get(f"/api/tasks/{uuid.uuid4()}")
    assert response.status_code == 404
    assert response.json()["code"] == "task_not_found"


def test_unknown_field_is_rejected(client: TestClient) -> None:
    body = create_task(client)
    response = client.post(
        f"/api/tasks/{body['task_id']}/answers", json={"values": {"unknown": "x"}}
    )
    assert response.status_code == 422
    assert response.json()["code"] == "request_rejected"


def test_unresolved_intent_requires_clarification(client: TestClient) -> None:
    response = client.post(
        "/api/tasks", json={"conversation_id": "conversation-1", "input_text": "随便说点什么"}
    )
    assert response.status_code == 422
    assert response.json()["code"] == "clarification_required"
    # 识别失败也要给前端一个稳定状态键，而不是让它自己拼一个。
    assert response.json()["ui_state"] == "needs_clarification"


def test_unknown_flow_is_rejected(client: TestClient) -> None:
    response = client.post(
        "/api/tasks",
        json={
            "conversation_id": "conversation-1",
            "input_text": "测试",
            "flow_id": "does.not.exist",
        },
    )
    assert response.status_code == 422
    assert response.json()["code"] == "request_rejected"


def test_cancelled_task_conflicts(client: TestClient) -> None:
    body = create_task(client)
    assert client.post(f"/api/tasks/{body['task_id']}/cancel").status_code == 200

    response = client.post(f"/api/tasks/{body['task_id']}/cancel")
    assert response.status_code == 409
    assert response.json()["code"] == "task_state_conflict"
