"""运维只读接口的过滤与键集翻页。

除审计归档（只有一种实现）外，每个用例都会在**内存与 SQLite 两个后端**上各跑
一遍：筛选与分页是"两处实现一件事"的典型场景，只测一边等于没测——另一边漂移了
没人知道，而运维看到的数字会随部署的存储后端变化。用例里断言的 id 序列是固定的，
因此两个后端必须给出逐条一致的结果。
"""

import sqlite3
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tests.support import TEST_FLOWS
from workpanda.api.security import SESSION_HEADER
from workpanda.bootstrap import Container, build_container
from workpanda.config import Settings
from workpanda.domain.models import EventType, Task, TaskEvent, TaskStatus
from workpanda.domain.paging import EventQuery, TaskCursor, TaskQuery, decode_token, event_matches
from workpanda.infrastructure.adapters import ScriptedExecutor
from workpanda.infrastructure.sqlite_store import SqliteStore
from workpanda.main import create_app

OPS_TOKEN = "ops-token-for-tests"
HEADERS = {"X-WorkPanda-Ops-Token": OPS_TOKEN}
BASE = datetime(2026, 11, 7, 4, 0, tzinfo=UTC)


@pytest.fixture(params=["memory", "sqlite"])
def container(request: pytest.FixtureRequest, tmp_path: Path) -> Iterator[Container]:
    """两个后端各来一遍。SQLite 走真实文件，不靠 mock 假装持久化。"""

    built = build_container(
        Settings(
            store_backend=request.param,
            sqlite_path=tmp_path / "ops.db",
            enable_demo_flow=False,
            ops_token=OPS_TOKEN,
        ),
        flows=TEST_FLOWS,
        executor=ScriptedExecutor(),
    )
    try:
        yield built
    finally:
        built.close()


@pytest.fixture
def client(container: Container) -> TestClient:
    built = TestClient(create_app(container=container))
    built.headers.update({SESSION_HEADER: container.signer.issue()})
    return built


def seed_task(
    container: Container,
    *,
    index: int,
    minutes: int | None = None,
    status: TaskStatus = TaskStatus.CREATED,
    flow_id: str = "test.direct",
) -> Task:
    """按可控的时间点直接落一条任务，避免依赖"跑测试时的 now"。"""

    created = BASE + timedelta(minutes=index if minutes is None else minutes)
    return container.store.create(
        Task(
            conversation_id=f"conversation-{index}",
            flow_id=flow_id,
            flow_version="1",
            status=status,
            slots={"subject": "主题"},
            created_at=created,
            updated_at=created,
        )
    )


def page_through(client: TestClient, path: str, *, limit: int, **params: object) -> list[str]:
    """按 `X-Next-Cursor` 翻到底，返回所有条目的 task_id/trace_id。

    翻不动就直接失败：本函数的存在意义就是"必须能翻完"。
    """

    seen: list[str] = []
    cursor: str | None = None
    for _ in range(50):
        query = {**params, "limit": limit}
        if cursor is not None:
            query["cursor"] = cursor
        response = client.get(path, params=query, headers=HEADERS)
        assert response.status_code == 200, response.text
        seen.extend(item["task_id"] for item in response.json())
        cursor = response.headers.get("X-Next-Cursor")
        if not cursor:
            return seen
    raise AssertionError("翻页没有终点：游标可能原地打转")


# ---------- 任务 ----------


def test_task_paging_covers_every_record_in_order(client: TestClient, container: Container) -> None:
    ids = [str(seed_task(container, index=index).task_id) for index in range(7)]

    seen = page_through(client, "/api/ops/tasks", limit=3)

    # 新的在前；翻页结果与一次性取全逐条一致，不重不漏。
    assert seen == list(reversed(ids))
    assert len(set(seen)) == len(seen)


def test_task_cursor_is_absent_on_an_exactly_full_last_page(
    client: TestClient, container: Container
) -> None:
    """刚好整页时不给游标：靠 `len(items) == limit` 猜"还有"必然多跑一轮空页。"""

    for index in range(6):
        seed_task(container, index=index)

    first = client.get("/api/ops/tasks", params={"limit": 3}, headers=HEADERS)
    second = client.get(
        "/api/ops/tasks",
        params={"limit": 3, "cursor": first.headers["X-Next-Cursor"]},
        headers=HEADERS,
    )

    assert len(first.json()) == 3
    assert len(second.json()) == 3
    assert "X-Next-Cursor" not in second.headers


def test_task_filters_combine_with_and(client: TestClient, container: Container) -> None:
    wanted = [
        seed_task(container, index=0, status=TaskStatus.COMPLETED, flow_id="test.confirm"),
        seed_task(container, index=1, status=TaskStatus.COMPLETED, flow_id="test.confirm"),
    ]
    seed_task(container, index=2, status=TaskStatus.CREATED, flow_id="test.confirm")
    seed_task(container, index=3, status=TaskStatus.COMPLETED, flow_id="test.direct")

    response = client.get(
        "/api/ops/tasks",
        params={"flow_id": "test.confirm", "status": "completed"},
        headers=HEADERS,
    )

    # "与"而不是"或"：只按流水线或只按状态都会多出上面那两条。
    assert [item["task_id"] for item in response.json()] == [
        str(wanted[1].task_id),
        str(wanted[0].task_id),
    ]


def test_task_time_window_is_left_closed_right_open(
    client: TestClient, container: Container
) -> None:
    for index in range(6):
        seed_task(container, index=index)

    earlier = client.get(
        "/api/ops/tasks",
        params={
            "since": "2026-11-07T12:00:00+08:00",  # = 04:00Z，含
            "until": "2026-11-07T12:03:00+08:00",  # = 04:03Z，不含
        },
        headers=HEADERS,
    ).json()
    later = client.get(
        "/api/ops/tasks",
        params={
            "since": "2026-11-07T12:03:00+08:00",
            "until": "2026-11-07T12:06:00+08:00",
        },
        headers=HEADERS,
    ).json()

    # 相邻两窗拼起来正好是全部：午夜那一秒不会被算进两次，也不会漏掉。
    assert [item["conversation_id"] for item in earlier] == [
        "conversation-2",
        "conversation-1",
        "conversation-0",
    ]
    assert [item["conversation_id"] for item in later] == [
        "conversation-5",
        "conversation-4",
        "conversation-3",
    ]


@pytest.mark.parametrize(
    ("params", "code"),
    [
        ({"since": "2026-11-07T00:00:00"}, "naive_time_window"),
        ({"until": "2026-11-07T00:00:00"}, "naive_time_window"),
        (
            {"since": "2026-11-07T00:00:00+08:00", "until": "2026-11-07T00:00:00+08:00"},
            "empty_time_window",
        ),
    ],
)
def test_task_endpoint_rejects_ambiguous_windows(
    client: TestClient, params: dict, code: str
) -> None:
    response = client.get("/api/ops/tasks", params=params, headers=HEADERS)

    assert response.status_code == 400
    assert response.json()["detail"]["code"] == code


def test_task_endpoint_rejects_an_invalid_cursor(client: TestClient) -> None:
    response = client.get(
        "/api/ops/tasks", params={"cursor": "definitely-not-a-cursor"}, headers=HEADERS
    )

    # 不能忽略游标直接返回第一页：那会让调用方以为自己在翻页，实际在打转。
    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "invalid_cursor"


def test_cursor_cannot_be_reused_on_another_endpoint(
    client: TestClient, container: Container
) -> None:
    """把任务的游标拿去查归档必须报错。

    两个端点各自按自己的字段解释同一个字符串，会返回一页"看起来正常"的错误数据——
    这比翻页失效更难发现，所以游标里带 scope。
    """

    for index in range(4):
        seed_task(container, index=index)
    task_cursor = client.get("/api/ops/tasks", params={"limit": 2}, headers=HEADERS).headers[
        "X-Next-Cursor"
    ]

    response = client.get(
        "/api/ops/audit", params={"cursor": task_cursor}, headers=HEADERS
    )

    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "invalid_cursor"


def test_tasks_are_ordered_deterministically_when_timestamps_collide(
    client: TestClient, container: Container
) -> None:
    """同一时刻创建的任务也必须有确定顺序，否则翻页会在原地打转。"""

    ids = [str(seed_task(container, index=index, minutes=5).task_id) for index in range(5)]

    seen = page_through(client, "/api/ops/tasks", limit=2)

    assert seen == sorted(ids, reverse=True)
    assert len(set(seen)) == len(ids)


# ---------- 事件 ----------


def seed_events(container: Container, task_id: uuid.UUID, count: int) -> None:
    for _ in range(count):
        container.store.append_event(task_id, EventType.STEP_STARTED, TaskStatus.RUNNING)


def test_events_page_by_sequence_without_duplicates(
    client: TestClient, container: Container
) -> None:
    task = seed_task(container, index=0)
    seed_events(container, task.task_id, 5)

    seen: list[int] = []
    cursor: str | None = None
    for _ in range(10):
        params: dict[str, object] = {"limit": 2}
        if cursor is not None:
            params["cursor"] = cursor
        response = client.get(
            f"/api/ops/tasks/{task.task_id}/events", params=params, headers=HEADERS
        )
        assert response.status_code == 200, response.text
        seen.extend(item["sequence"] for item in response.json())
        cursor = response.headers.get("X-Next-Cursor")
        if not cursor:
            break

    assert seen == sorted(seen)
    assert len(set(seen)) == len(seen)
    assert seen == list(range(1, len(seen) + 1))


def test_events_filter_by_type(client: TestClient, container: Container) -> None:
    task = seed_task(container, index=0)
    seed_events(container, task.task_id, 2)
    container.store.append_event(task.task_id, EventType.CANCELLED, TaskStatus.CANCELLED)

    response = client.get(
        f"/api/ops/tasks/{task.task_id}/events",
        params={"type": "cancelled"},
        headers=HEADERS,
    )

    assert [item["type"] for item in response.json()] == ["cancelled"]


def test_events_reject_two_paging_sources_at_once(
    client: TestClient, container: Container
) -> None:
    """`after` 与 `cursor` 同时给就 400：两种来源并存，"谁优先"只能靠猜。"""

    task = seed_task(container, index=0)
    seed_events(container, task.task_id, 3)

    response = client.get(
        f"/api/ops/tasks/{task.task_id}/events",
        params={"after": 1, "cursor": "2"},
        headers=HEADERS,
    )

    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "invalid_cursor"


def test_events_reject_a_non_numeric_cursor(client: TestClient, container: Container) -> None:
    task = seed_task(container, index=0)

    response = client.get(
        f"/api/ops/tasks/{task.task_id}/events",
        params={"cursor": "not-a-sequence"},
        headers=HEADERS,
    )

    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "invalid_cursor"


def test_events_of_an_unknown_task_is_404_not_an_empty_page(client: TestClient) -> None:
    """"没这个任务"和"这个任务没有事件"是两件事，混成一个空列表会误导运维。"""

    response = client.get(
        f"/api/ops/tasks/{uuid.uuid4()}/events", headers=HEADERS
    )

    assert response.status_code == 404


def test_event_window_is_left_closed_right_open() -> None:
    event = TaskEvent(
        task_id=uuid.uuid4(),
        type=EventType.STEP_STARTED,
        status=TaskStatus.RUNNING,
        sequence=1,
        occurred_at=BASE,
    )

    assert event_matches(event, EventQuery(task_id=event.task_id, since=BASE))
    assert not event_matches(event, EventQuery(task_id=event.task_id, until=BASE))
    assert not event_matches(
        event, EventQuery(task_id=event.task_id, after_sequence=event.sequence)
    )


# ---------- 时间戳文本（只在 SQLite 上有意义） ----------


def test_legacy_timestamp_text_is_normalized_on_open(workdir: Path) -> None:
    """老库里的行长不一致，打开时改写一次：同一时刻不能排成两个位置。"""

    path = workdir / "legacy.db"
    first = SqliteStore(path)
    task = first.create(
        Task(
            conversation_id="conversation-legacy",
            flow_id="test.direct",
            flow_version="1",
            created_at=BASE,
            updated_at=BASE,
        )
    )
    first.close()

    # 模拟旧版本写下的裸 isoformat：微秒为 0 时不带小数位。
    conn = sqlite3.connect(path)
    conn.execute(
        "UPDATE tasks SET created_at = ?, updated_at = ? WHERE task_id = ?",
        ("2026-11-07T04:00:00+00:00", "2026-11-07T04:00:00+00:00", str(task.task_id)),
    )
    conn.commit()
    conn.close()

    SqliteStore(path).close()  # 打开即规范化

    conn = sqlite3.connect(path)
    try:
        stored = conn.execute(
            "SELECT created_at FROM tasks WHERE task_id = ?", (str(task.task_id),)
        ).fetchone()[0]
    finally:
        conn.close()
    assert stored == "2026-11-07T04:00:00.000000+00:00"


def test_sqlite_paging_is_stable_when_instants_are_equal(workdir: Path) -> None:
    """同一时刻的多条任务靠 task_id 次级键定序，翻页不能重复也不能漏。"""

    store = SqliteStore(workdir / "equal.db")
    try:
        ids = [
            store.create(
                Task(
                    conversation_id=f"conversation-{index}",
                    flow_id="test.direct",
                    flow_version="1",
                    created_at=BASE,
                    updated_at=BASE,
                )
            ).task_id
            for index in range(3)
        ]

        collected: list[uuid.UUID] = []
        cursor = None
        for _ in range(5):
            page = store.query_tasks(TaskQuery(limit=1, cursor=cursor))
            collected.extend(task.task_id for task in page.items)
            if page.next_cursor is None:
                break
            stamp, task_id = decode_token(page.next_cursor, scope="tasks", arity=2)
            cursor = TaskCursor(
                created_at=datetime.fromisoformat(stamp), task_id=uuid.UUID(task_id)
            )
    finally:
        store.close()

    assert sorted(str(item) for item in collected) == sorted(str(item) for item in ids)
    assert len(set(collected)) == len(ids)
