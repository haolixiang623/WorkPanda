from fastapi.testclient import TestClient

from workpanda.api.dependencies import get_task_service
from workpanda.infrastructure.memory_store import InMemoryTaskStore
from workpanda.application.task_service import TaskService
from workpanda.main import create_app


def client() -> TestClient:
    app = create_app()
    app.dependency_overrides[get_task_service] = lambda: TaskService(InMemoryTaskStore())
    return TestClient(app)


def test_health_check() -> None:
    response = client().get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_create_and_get_task() -> None:
    test_client = client()
    created = test_client.post(
        "/api/tasks",
        json={
            "conversation_id": "conversation-1",
            "flow_id": "generic",
            "flow_version": "1",
            "input_text": "generic request",
        },
    )
    assert created.status_code == 201
    task_id = created.json()["task_id"]

    fetched = test_client.get(f"/api/tasks/{task_id}")
    assert fetched.status_code == 200
    assert fetched.json()["status"] == "created"


def test_unknown_task_returns_404() -> None:
    response = client().get("/api/tasks/00000000-0000-0000-0000-000000000000")
    assert response.status_code == 404
