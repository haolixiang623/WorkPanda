"""审计归档的过滤与键集翻页。

归档只有一种落地形式（追加式 JSONL），因此这里不参数化后端；重点是两件麻烦事：

- 条目会被滚动到备份文件里，翻页必须跨文件仍然不重不漏；
- 旧版本写下的行没有 `entry_id`，读取时要补一个**稳定**的值，否则每次读到的
  锚点都不同，翻页会漂移。
"""

import json
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from tests.support import TEST_FLOWS
from workpanda.api.security import SESSION_HEADER
from workpanda.bootstrap import build_container
from workpanda.config import Settings
from workpanda.harness.audit import AuditEntry, AuditKind
from workpanda.infrastructure.adapters import ScriptedExecutor
from workpanda.main import create_app

OPS_TOKEN = "ops-token-for-tests"
HEADERS = {"X-WorkPanda-Ops-Token": OPS_TOKEN}
BASE = datetime(2026, 11, 7, 4, 0, tzinfo=UTC)


def make_client(audit_path: Path | None, **overrides: Any) -> TestClient:
    container = build_container(
        Settings(
            store_backend="memory",
            enable_demo_flow=False,
            ops_token=OPS_TOKEN,
            harness_audit_path=audit_path,
            **overrides,
        ),
        flows=TEST_FLOWS,
        executor=ScriptedExecutor(),
    )
    client = TestClient(create_app(container=container))
    client.headers.update({SESSION_HEADER: container.signer.issue()})
    client.audit_journal = container.audit  # type: ignore[attr-defined]
    return client


def entry(*, minutes: int, kind: AuditKind = AuditKind.EXECUTION, **payload: Any) -> AuditEntry:
    return AuditEntry(
        at=BASE + timedelta(minutes=minutes),
        kind=kind,
        trace_id=f"trace-{uuid.uuid4().hex[:8]}",
        task_id=uuid.uuid4(),
        flow_id="test.flow",
        flow_version="1",
        step_id="submit",
        payload=payload,
    )


def page_through(
    client: TestClient, *, limit: int, key: str = "entry_id", **params: Any
) -> list[str]:
    seen: list[str] = []
    cursor: str | None = None
    for _ in range(50):
        query = {**params, "limit": limit}
        if cursor is not None:
            query["cursor"] = cursor
        response = client.get("/api/ops/audit", params=query, headers=HEADERS)
        assert response.status_code == 200, response.text
        seen.extend(item[key] for item in response.json())
        cursor = response.headers.get("X-Next-Cursor")
        if not cursor:
            return seen
    raise AssertionError("翻页没有终点：游标可能原地打转")


def test_audit_paging_covers_entries_across_rotated_backups(tmp_path: Path) -> None:
    """滚动出去的历史也要能被翻到，否则"三个月前那次提交"就查不了了。"""

    client = make_client(tmp_path / "audit.jsonl", harness_audit_max_bytes=1)
    journal = client.audit_journal  # type: ignore[attr-defined]
    written = [entry(minutes=index, seq=index) for index in range(4)]
    for item in written:  # max_bytes=1 → 每次写入都把上一条推进备份
        journal.append(item)

    assert (tmp_path / "audit.jsonl.3").exists()
    seen = page_through(client, limit=2, key="trace_id")

    # 新的在前，且四条都在：跨文件翻页不重不漏。
    assert seen == [item.trace_id for item in reversed(written)]
    assert len(set(seen)) == 4


def test_audit_filters_combine_with_and(tmp_path: Path) -> None:
    client = make_client(tmp_path / "audit.jsonl")
    journal = client.audit_journal  # type: ignore[attr-defined]
    target = entry(minutes=2, kind=AuditKind.EFFECT, tool="test_submit")
    journal.append(entry(minutes=1, kind=AuditKind.EXECUTION, tool="test_submit"))
    journal.append(target)
    journal.append(entry(minutes=3, kind=AuditKind.EFFECT, tool="other"))
    journal.append(entry(minutes=4, kind=AuditKind.EFFECT, task_id=uuid.uuid4()))

    response = client.get(
        "/api/ops/audit",
        params={"kind": "effect", "task_id": str(target.task_id)},
        headers=HEADERS,
    )

    assert [item["trace_id"] for item in response.json()] == [target.trace_id]


def test_audit_time_window_is_left_closed_right_open(tmp_path: Path) -> None:
    client = make_client(tmp_path / "audit.jsonl")
    journal = client.audit_journal  # type: ignore[attr-defined]
    for minutes in range(3):
        journal.append(entry(minutes=minutes, seq=minutes))

    first = client.get(
        "/api/ops/audit",
        params={
            "since": "2026-11-07T12:00:00+08:00",  # = 04:00Z，含
            "until": "2026-11-07T12:01:00+08:00",  # = 04:01Z，不含
        },
        headers=HEADERS,
    ).json()
    second = client.get(
        "/api/ops/audit",
        params={
            "since": "2026-11-07T12:01:00+08:00",
            "until": "2026-11-07T12:02:00+08:00",
        },
        headers=HEADERS,
    ).json()

    assert [item["payload"]["seq"] for item in first] == [0]
    assert [item["payload"]["seq"] for item in second] == [1]


def test_legacy_entries_without_entry_id_page_stably(tmp_path: Path) -> None:
    """旧版本写下的行没有 `entry_id`，补出来的 id 必须每次都一样。

    补成随机值的话，下一页的锚点每次读都不同：要么漏记录，要么重复记录，
    而两种错法都不报错。
    """

    path = tmp_path / "audit.jsonl"
    lines = []
    for index in range(4):
        lines.append(
            json.dumps(
                {
                    "at": (BASE + timedelta(minutes=index)).isoformat(),
                    "kind": "execution",
                    "trace_id": f"legacy-{index}",
                    "task_id": str(uuid.uuid4()),
                    "flow_id": "test.flow",
                    "flow_version": "1",
                    "step_id": "submit",
                    "payload": {"seq": index},
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    client = make_client(path)

    first_pass = page_through(client, limit=2)
    second_pass = page_through(client, limit=2)

    assert first_pass == second_pass
    assert len(set(first_pass)) == 4
    assert [item for item in first_pass][:1] != [""]


def test_audit_endpoint_rejects_a_bad_cursor(tmp_path: Path) -> None:
    client = make_client(tmp_path / "audit.jsonl")
    journal = client.audit_journal  # type: ignore[attr-defined]
    journal.append(entry(minutes=1))

    response = client.get(
        "/api/ops/audit", params={"cursor": "definitely-not-a-cursor"}, headers=HEADERS
    )

    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "invalid_cursor"


def test_audit_endpoint_rejects_a_naive_window(tmp_path: Path) -> None:
    client = make_client(tmp_path / "audit.jsonl")

    response = client.get(
        "/api/ops/audit", params={"since": "2026-11-07T00:00:00"}, headers=HEADERS
    )

    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "naive_time_window"


def test_audit_entries_expose_a_stable_entry_id_on_every_read(tmp_path: Path) -> None:
    client = make_client(tmp_path / "audit.jsonl")
    journal = client.audit_journal  # type: ignore[attr-defined]
    archived = entry(minutes=1)
    journal.append(archived)

    first = client.get("/api/ops/audit", headers=HEADERS).json()
    second = client.get("/api/ops/audit", headers=HEADERS).json()

    assert first[0]["entry_id"]
    assert first[0]["entry_id"] == second[0]["entry_id"]
