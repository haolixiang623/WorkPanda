"""人工处置备注：只追加事实，不改状态，也不出现在群众侧事件流里。

这些用例守的是同一条线：备注不能变成第二条"改状态"的路径，
也不能把运营的内部判断泄露给群众。
"""

import uuid

from fastapi.testclient import TestClient

from tests.support import TEST_FLOWS
from workpanda.api.security import SESSION_HEADER
from workpanda.bootstrap import build_container
from workpanda.config import Settings
from workpanda.domain.models import EventType, TaskCreateRequest, TaskStatus
from workpanda.infrastructure.adapters import ScriptedExecutor
from workpanda.main import create_app

OPS_TOKEN = "ops-token-for-tests"
OPS_HEADERS = {"X-WorkPanda-Ops-Token": OPS_TOKEN}


def make_client(
    *, ops_token: str | None = OPS_TOKEN
) -> tuple[TestClient, object, str]:
    container = build_container(
        Settings(store_backend="memory", enable_demo_flow=False, ops_token=ops_token),
        flows=TEST_FLOWS,
        executor=ScriptedExecutor(),
    )
    client = TestClient(create_app(container=container))
    token = container.signer.issue()
    client.headers.update({SESSION_HEADER: token})
    # 任务要建在这个会话名下，群众侧接口（归属校验）才打得开。
    return client, container, container.signer.verify(token).subject


def seed_task(container, subject: str) -> str:
    task = container.tasks.create_task(
        TaskCreateRequest(
            conversation_id="conversation-1",
            input_text="测试",
            flow_id="test.confirm",
            slots={"subject": "主题", "contact": "123"},
        ),
        subject,
    )
    return str(task.task_id)


def test_note_is_appended_to_the_event_stream() -> None:
    client, container, subject = make_client()
    task_id = seed_task(container, subject)

    response = client.post(
        f"/api/ops/tasks/{task_id}/notes",
        json={
            "operator": "张运维",
            "note": "已电话核实，群众确认继续办理",
            "reference": "工单 2026-1107",
        },
        headers=OPS_HEADERS,
    )

    assert response.status_code == 201
    body = response.json()
    assert body["type"] == EventType.OPERATOR_NOTE
    assert body["data"]["operator"] == "张运维"
    assert body["data"]["reference"] == "工单 2026-1107"

    events = client.get(f"/api/ops/tasks/{task_id}/events", headers=OPS_HEADERS).json()
    assert [event["type"] for event in events][-1] == "operator_note"


def test_note_does_not_touch_task_state_or_slots() -> None:
    """备注是纯追加：状态、更新时间、槽位一个都不许动。

    如果这里走了带整行写回的 `commit`，就会把入口读到的旧快照覆盖回去，
    把期间 `answer` 刚写入的槽位悄悄改掉——这是这条能力最容易踩的坑。
    """

    client, container, subject = make_client()
    task_id = seed_task(container, subject)
    before = container.tasks.ops_task(uuid.UUID(task_id))

    response = client.post(
        f"/api/ops/tasks/{task_id}/notes",
        json={"operator": "李运维", "note": "复核中"},
        headers=OPS_HEADERS,
    )
    assert response.status_code == 201

    after = container.tasks.ops_task(before.task_id)
    assert after.status == before.status
    assert after.updated_at == before.updated_at
    assert after.slots == before.slots
    assert after.current_step == before.current_step


def test_note_is_allowed_on_a_terminal_task() -> None:
    """终态才是这条能力的主场：任务已经转人工/已取消，事实仍需要留痕。"""

    client, container, subject = make_client()
    task_id = seed_task(container, subject)

    cancelled = container.tasks.cancel(uuid.UUID(task_id), subject)
    assert cancelled.status is TaskStatus.CANCELLED

    response = client.post(
        f"/api/ops/tasks/{task_id}/notes",
        json={"operator": "王运维", "note": "群众来电撤销，已在窗口登记"},
        headers=OPS_HEADERS,
    )

    assert response.status_code == 201
    assert response.json()["status"] == TaskStatus.CANCELLED
    # 加完备注，任务仍是取消态：备注没有把终态"改活"，也没有另立结论。
    assert container.tasks.ops_task(uuid.UUID(task_id)).status is TaskStatus.CANCELLED


def test_operator_note_is_hidden_from_the_citizen_stream() -> None:
    """内部判断不能推给群众：备注只在运维侧可见。"""

    client, container, subject = make_client()
    task_id = seed_task(container, subject)
    client.post(
        f"/api/ops/tasks/{task_id}/notes",
        json={"operator": "张运维", "note": "疑似重复申请，注意核查"},
        headers=OPS_HEADERS,
    )

    with client.stream("GET", f"/api/tasks/{task_id}/events") as response:
        assert response.status_code == 200
        seen = []
        for line in response.iter_lines():
            seen.append(line)
            if len(seen) >= 12:
                break

    payload = "\n".join(seen)
    assert "operator_note" not in payload
    assert "疑似重复申请" not in payload
    # 群众仍能看到自己的正常事件，过滤不能误伤整条流。
    assert "accepted" in payload


def test_note_requires_an_operator_name() -> None:
    client, container, subject = make_client()
    task_id = seed_task(container, subject)

    missing = client.post(
        f"/api/ops/tasks/{task_id}/notes", json={"note": "没有署名"}, headers=OPS_HEADERS
    )
    blank = client.post(
        f"/api/ops/tasks/{task_id}/notes",
        json={"operator": "", "note": "署名是空的"},
        headers=OPS_HEADERS,
    )

    assert missing.status_code == 422
    assert blank.status_code == 422


def test_note_rejects_an_oversized_body() -> None:
    """备注会长期留在事件流里，长度必须有上限，否则等于变相塞文件。"""

    client, container, subject = make_client()
    task_id = seed_task(container, subject)

    response = client.post(
        f"/api/ops/tasks/{task_id}/notes",
        json={"operator": "张运维", "note": "x" * 1001},
        headers=OPS_HEADERS,
    )

    assert response.status_code == 422


def test_note_carries_no_status_field() -> None:
    """请求体里的多余字段必须被忽略：这是"这个入口改不了状态"的硬证据。"""

    client, container, subject = make_client()
    task_id = seed_task(container, subject)
    before = container.tasks.ops_task(uuid.UUID(task_id))

    response = client.post(
        f"/api/ops/tasks/{task_id}/notes",
        json={"operator": "张运维", "note": "想顺手改成已完成", "status": "completed"},
        headers=OPS_HEADERS,
    )

    assert response.status_code == 201
    after = container.tasks.ops_task(before.task_id)
    assert after.status == before.status
    assert "status" not in response.json()["data"]


def test_note_requires_the_ops_token() -> None:
    client, container, subject = make_client()
    task_id = seed_task(container, subject)

    response = client.post(
        f"/api/ops/tasks/{task_id}/notes", json={"operator": "张运维", "note": "无凭据"}
    )

    assert response.status_code == 401


def test_note_is_hidden_when_ops_api_is_disabled() -> None:
    client, container, subject = make_client(ops_token=None)
    task_id = seed_task(container, subject)

    response = client.post(
        f"/api/ops/tasks/{task_id}/notes",
        json={"operator": "张运维", "note": "未启用"},
        headers=OPS_HEADERS,
    )

    assert response.status_code == 404


def test_note_on_a_missing_task_is_404() -> None:
    client, _, _ = make_client()

    response = client.post(
        "/api/ops/tasks/00000000-0000-0000-0000-000000000000/notes",
        json={"operator": "张运维", "note": "不存在"},
        headers=OPS_HEADERS,
    )

    assert response.status_code == 404
