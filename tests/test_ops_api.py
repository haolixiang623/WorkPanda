"""运维只读接口：默认关闭，凭据错误返回 401，且不改写业务状态。"""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from tests.support import TEST_FLOWS, harness_flow
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


def _seed_traces(
    container, count: int, *, stop_reason: str = "completed", prefix: str = "trace"
) -> None:
    for index in range(count):
        container.traces.save(
            TraceRecord(
                trace_id=f"{prefix}-{index:02d}",
                task_id=uuid.uuid4(),
                flow_id="test.flow",
                flow_version="1",
                step_id="run",
                outcome="succeeded" if stop_reason == "completed" else "unconfirmed",
                stop_reason=stop_reason,
                model_calls=1,
                tool_calls=1,
                successful_tool_calls=1,
                tokens=10,
                duration_ms=5,
                created_at=datetime(2026, 11, 7, 4, 0, tzinfo=UTC) + timedelta(minutes=index),
            )
        )


def test_ops_traces_page_with_the_cursor_header() -> None:
    """响应体仍是数组（控制台不会被打断），翻页靠 `X-Next-Cursor` 头。"""

    client, container = make_client(ops_token=OPS_TOKEN)
    _seed_traces(container, 5)
    headers = {"X-WorkPanda-Ops-Token": OPS_TOKEN}

    first = client.get("/api/ops/traces", params={"limit": 2}, headers=headers)
    assert first.status_code == 200
    assert [item["trace_id"] for item in first.json()] == ["trace-04", "trace-03"]
    cursor = first.headers.get("X-Next-Cursor")
    assert cursor

    second = client.get(
        "/api/ops/traces", params={"limit": 2, "cursor": cursor}, headers=headers
    )
    assert [item["trace_id"] for item in second.json()] == ["trace-02", "trace-01"]

    last = client.get(
        "/api/ops/traces",
        params={"limit": 2, "cursor": second.headers["X-Next-Cursor"]},
        headers=headers,
    )
    assert [item["trace_id"] for item in last.json()] == ["trace-00"]
    # 到头了就没有游标：否则调用方会一直为"空页"多跑一轮。
    assert "X-Next-Cursor" not in last.headers


def test_ops_traces_reject_an_invalid_cursor() -> None:
    client, container = make_client(ops_token=OPS_TOKEN)
    _seed_traces(container, 2)

    response = client.get(
        "/api/ops/traces",
        params={"cursor": "definitely-not-a-cursor"},
        headers={"X-WorkPanda-Ops-Token": OPS_TOKEN},
    )

    # 不能忽略游标直接返回第一页：那会让调用方以为自己在翻页，实际在打转。
    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "invalid_cursor"


@pytest.mark.parametrize(
    ("params", "code"),
    [
        ({"since": "2026-11-07T00:00:00"}, "naive_time_window"),
        ({"until": "2026-11-07T00:00:00"}, "naive_time_window"),
        (
            {"since": "2026-11-07T00:00:00+08:00", "until": "2026-11-07T00:00:00+08:00"},
            "empty_time_window",
        ),
        (
            {"since": "2026-11-08T00:00:00+08:00", "until": "2026-11-07T00:00:00+08:00"},
            "empty_time_window",
        ),
    ],
)
def test_ops_traces_reject_ambiguous_time_windows(params: dict, code: str) -> None:
    """不带时区会被当成 UTC，对东八区等于凭空错开 8 小时——空结果会被误读成没事。"""

    client, container = make_client(ops_token=OPS_TOKEN)
    _seed_traces(container, 2)
    headers = {"X-WorkPanda-Ops-Token": OPS_TOKEN}

    assert client.get("/api/ops/traces", params=params, headers=headers).status_code == 400
    summary = client.get("/api/ops/traces/summary", params=params, headers=headers)
    assert summary.status_code == 400
    assert summary.json()["detail"]["code"] == code


def test_ops_traces_accepts_timezone_aware_window() -> None:
    client, container = make_client(ops_token=OPS_TOKEN)
    _seed_traces(container, 3)
    headers = {"X-WorkPanda-Ops-Token": OPS_TOKEN}

    response = client.get(
        "/api/ops/traces",
        params={
            "since": "2026-11-07T12:01:00+08:00",  # = 04:01Z
            "until": "2026-11-07T12:03:00+08:00",  # = 04:03Z，不含
        },
        headers=headers,
    )

    assert [item["trace_id"] for item in response.json()] == ["trace-02", "trace-01"]


def test_ops_trace_summary_scopes_to_the_same_filters() -> None:
    """「这段时间的未确认占比」是运维真问题，不能只给全局数字。"""

    client, container = make_client(ops_token=OPS_TOKEN)
    _seed_traces(container, 3)
    _seed_traces(container, 1, stop_reason="deadline", prefix="slow")
    headers = {"X-WorkPanda-Ops-Token": OPS_TOKEN}

    everything = client.get("/api/ops/traces/summary", headers=headers).json()
    scoped = client.get(
        "/api/ops/traces/summary", params={"stop_reason": "deadline"}, headers=headers
    ).json()

    assert everything["total"] == 4
    assert everything["unconfirmed_rate"] == 0.25
    assert scoped["total"] == 1
    assert scoped["unconfirmed_rate"] == 1.0


def test_ops_tool_runtime_reports_absence_without_harness() -> None:
    """没有 harness 就没有工具线程，接口要说"没有"，而不是编一排 0 出来。"""

    client, _ = make_client(ops_token=OPS_TOKEN)

    response = client.get(
        "/api/ops/tool-runtime", headers={"X-WorkPanda-Ops-Token": OPS_TOKEN}
    )

    assert response.status_code == 200
    assert response.json() == {"available": False}


def test_ops_tool_runtime_reports_the_ledger_when_harness_is_in_use() -> None:
    container = build_container(
        Settings(
            store_backend="memory",
            enable_demo_flow=False,
            ops_token=OPS_TOKEN,
            # 本机 .env 可能配了真实模型凭据；只要分支走 harness 即可，不发真实调用。
            harness_enabled=True,
            model_base_url="http://127.0.0.1:9/v1",
            model_api_key="sk-test-not-a-real-key",
        ),
        # 真 harness 分支只核对"声明的工具是否注册"，所以这里给一个真工具名的流程。
        flows=[harness_flow()],
    )
    client = TestClient(create_app(container=container))
    client.headers.update({SESSION_HEADER: container.signer.issue()})
    try:
        response = client.get(
            "/api/ops/tool-runtime", headers={"X-WorkPanda-Ops-Token": OPS_TOKEN}
        )

        assert response.status_code == 200
        body = response.json()
        assert body["available"] is True
        assert body["live"] == 0
        assert body["stuck"] == 0
        assert body["max_stuck"] == 16
    finally:
        container.close()
