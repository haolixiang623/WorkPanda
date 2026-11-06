"""会话隔离与限流：跨用户不可读写他人任务。"""

import uuid

import pytest
from fastapi.testclient import TestClient

from tests.support import CONFIRM_FLOW
from workpanda.api.security import SESSION_HEADER
from workpanda.bootstrap import Container, build_container
from workpanda.config import Settings
from workpanda.domain.identity import InvalidSession, SessionSigner
from workpanda.infrastructure.adapters import ScriptedExecutor
from workpanda.main import create_app


@pytest.fixture
def client(container: Container) -> TestClient:
    test_client = TestClient(create_app(container=container))
    test_client.headers.update({SESSION_HEADER: container.signer.issue()})
    return test_client


def create_task(client: TestClient) -> dict:
    response = client.post(
        "/api/tasks",
        json={
            "conversation_id": "conversation-1",
            "input_text": "测试",
            "flow_id": "test.confirm",
            "slots": {"subject": "主题", "contact": "123"},
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def test_other_session_cannot_read_task(client: TestClient, container: Container) -> None:
    body = create_task(client)
    other = TestClient(create_app(container=container))
    other.headers.update({SESSION_HEADER: container.signer.issue()})

    response = other.get(f"/api/tasks/{body['task_id']}")
    assert response.status_code == 404
    assert response.json()["code"] == "task_not_found"


def test_other_session_cannot_confirm_task(client: TestClient, container: Container) -> None:
    body = create_task(client)
    other = TestClient(create_app(container=container))
    other.headers.update({SESSION_HEADER: container.signer.issue()})

    response = other.post(
        f"/api/tasks/{body['task_id']}/confirm",
        json={"step_id": "submit", "accepted": True},
    )
    assert response.status_code == 404


def test_other_session_cannot_cancel_task(client: TestClient, container: Container) -> None:
    body = create_task(client)
    other = TestClient(create_app(container=container))
    other.headers.update({SESSION_HEADER: container.signer.issue()})

    assert other.post(f"/api/tasks/{body['task_id']}/cancel").status_code == 404


def test_request_without_session_gets_one(client: TestClient) -> None:
    anonymous = TestClient(create_app(container=client.app.state.container))
    response = anonymous.post(
        "/api/tasks",
        json={"conversation_id": "conversation-1", "input_text": "测试", "flow_id": "test.confirm"},
    )

    assert response.status_code == 201
    assert response.headers.get(SESSION_HEADER)


def test_tampered_session_is_rejected(client: TestClient) -> None:
    tampered = TestClient(create_app(container=client.app.state.container))
    tampered.headers.update({SESSION_HEADER: "forged.token.value"})

    response = tampered.get(f"/api/tasks/{uuid.uuid4()}")
    assert response.status_code == 401
    assert response.json()["code"] == "session_invalid"


def test_rate_limit_returns_429() -> None:
    limited = build_container(
        Settings(
            store_backend="memory",
            enable_demo_flow=False,
            rate_limit_per_minute=1,
            rate_limit_burst=1,
        ),
        flows=[CONFIRM_FLOW],
        executor=ScriptedExecutor(),
    )
    client = TestClient(create_app(container=limited))
    client.headers.update({SESSION_HEADER: limited.signer.issue()})

    first = client.get(f"/api/tasks/{uuid.uuid4()}")
    second = client.get(f"/api/tasks/{uuid.uuid4()}")

    assert first.status_code == 404
    assert second.status_code == 429
    assert second.json()["code"] == "rate_limited"
    assert int(second.headers["Retry-After"]) >= 1


def test_session_signature_cannot_be_forged() -> None:
    signer = SessionSigner("a-very-long-test-secret-value")
    token = signer.issue()
    session_id, issued_at, _ = token.split(".")

    with pytest.raises(InvalidSession):
        signer.verify(f"{session_id}.{issued_at}.forged")

    assert signer.verify(token).subject == session_id


def test_expired_session_is_rejected() -> None:
    signer = SessionSigner("a-very-long-test-secret-value", ttl_seconds=-1)

    with pytest.raises(InvalidSession):
        signer.verify(signer.issue())
