"""审计归档：只追加、按大小滚动、坏行不致失读，并且不因留痕失败影响业务结论。"""

from __future__ import annotations

import json
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from tests.support import TEST_FLOWS
from workpanda.api.security import SESSION_HEADER
from workpanda.application.ports import ExecutionOutcome, ExecutionRequest
from workpanda.bootstrap import build_container
from workpanda.config import Settings
from workpanda.harness.audit import (
    AuditEntry,
    AuditKind,
    JsonlAuditJournal,
    NullAuditJournal,
)
from workpanda.harness.clients import ScriptedModelClient, tool_call
from workpanda.harness.executor import HarnessExecutor
from workpanda.harness.models import ModelResponse, ToolSpec
from workpanda.harness.tools import ToolContext, ToolRegistry
from workpanda.infrastructure.adapters import ScriptedExecutor
from workpanda.main import create_app

SUCCESS = json.dumps(
    {"outcome": "succeeded", "summary": "已提交", "evidence": "submitted:x"},
    ensure_ascii=False,
)

OPS_TOKEN = "ops-token-for-tests"


class SubmitTool:
    """非幂等提交工具：调用一次就是一笔真实的外部动作。"""

    spec = ToolSpec(
        name="test_submit",
        description="提交单据",
        parameters={
            "type": "object",
            "properties": {"token": {"type": "string"}},
            "required": ["token"],
            "additionalProperties": False,
        },
        idempotent=False,
        sensitive_fields=["token"],
    )

    def __init__(self) -> None:
        self.invocations = 0

    def invoke(self, arguments: dict[str, Any], context: ToolContext) -> str:
        self.invocations += 1
        return f"submitted:{arguments['token']}"


class SlowSubmitTool(SubmitTool):
    """必然超时的提交工具：制造"结果未知"。"""

    spec = ToolSpec(
        name="test_submit",
        description="提交单据（慢）",
        parameters={
            "type": "object",
            "properties": {"token": {"type": "string"}},
            "required": ["token"],
            "additionalProperties": False,
        },
        idempotent=False,
        timeout_seconds=0.05,
    )

    def invoke(self, arguments: dict[str, Any], context: ToolContext) -> str:
        time.sleep(0.3)
        return "submitted"


def entry(kind: AuditKind = AuditKind.EXECUTION, **payload: Any) -> AuditEntry:
    return AuditEntry(
        kind=kind,
        trace_id=f"trace-{uuid.uuid4().hex[:8]}",
        task_id=uuid.uuid4(),
        flow_id="test.flow",
        flow_version="1",
        step_id="submit",
        payload=payload,
    )


def journal(path: Path, **kwargs: Any) -> JsonlAuditJournal:
    return JsonlAuditJournal(path, fsync=False, **kwargs)


# ---------- 归档本身 ----------


def test_appends_one_json_line_per_entry_and_reads_newest_first(tmp_path: Path) -> None:
    path = tmp_path / "audit.jsonl"
    store = journal(path)
    first = entry(outcome="succeeded")
    second = entry(outcome="unconfirmed")
    store.append(first)
    store.append(second)

    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0])["payload"]["outcome"] == "succeeded"

    records = store.read()
    assert [record.trace_id for record in records] == [second.trace_id, first.trace_id]


def test_read_filters_by_task_and_respects_limit(tmp_path: Path) -> None:
    path = tmp_path / "audit.jsonl"
    store = journal(path)
    target = entry(outcome="succeeded")
    for _ in range(3):
        store.append(entry(outcome="failed"))
    store.append(target)

    assert [one.trace_id for one in store.read(task_id=target.task_id)] == [target.trace_id]
    assert len(store.read(limit=2)) == 2
    assert store.read(limit=0) == []


def test_read_skips_malformed_lines_instead_of_failing(tmp_path: Path) -> None:
    path = tmp_path / "audit.jsonl"
    store = journal(path)
    store.append(entry(outcome="succeeded"))
    # 断电或磁盘写满时完全可能出现半行：此时必须还能读出其余记录。
    with path.open("a", encoding="utf-8") as handle:
        handle.write('{"kind": "execution", "trace_id"\n')
        handle.write("\n")

    records = store.read()

    assert len(records) == 1
    assert records[0].payload["outcome"] == "succeeded"


def test_rotates_when_size_limit_is_reached_and_keeps_bounded_backups(tmp_path: Path) -> None:
    path = tmp_path / "audit.jsonl"
    store = journal(path, max_bytes=300, backups=2)
    for _ in range(12):
        store.append(entry(outcome="succeeded", note="x" * 60))

    assert path.exists()
    assert store.backup_path(1).exists()
    # 超过保留份数的旧文件必须被清掉，否则归档会无限占盘。
    assert not store.backup_path(3).exists()
    for index in (1, 2):
        for line in store.backup_path(index).read_text(encoding="utf-8").splitlines():
            assert json.loads(line)["kind"] == "execution"


def test_backups_zero_keeps_only_the_current_file(tmp_path: Path) -> None:
    path = tmp_path / "audit.jsonl"
    store = journal(path, max_bytes=300, backups=0)
    for _ in range(8):
        store.append(entry(outcome="succeeded", note="x" * 60))

    assert not store.backup_path(1).exists()
    assert len(store.read(limit=100)) >= 1


def test_concurrent_appends_never_tear_a_line(tmp_path: Path) -> None:
    path = tmp_path / "audit.jsonl"
    store = journal(path)

    def write(index: int) -> None:
        store.append(entry(outcome="succeeded", worker=index))

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(write, range(40)))

    lines = [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert len(lines) == 40
    assert {json.loads(line)["payload"]["worker"] for line in lines} == set(range(40))


def test_null_journal_is_a_safe_no_op() -> None:
    quiet = NullAuditJournal()

    quiet.append(entry(outcome="succeeded"))

    assert quiet.read() == []


# ---------- 接进执行器 ----------


def run_step(path: Path, tool: Any, *, token: str = "token-live-9f3c") -> Any:
    registry = ToolRegistry()
    registry.register(tool)
    client = ScriptedModelClient(
        [
            ModelResponse(tool_calls=[tool_call(tool.spec.name, call_id="c1", token=token)]),
            ModelResponse(text=SUCCESS),
        ]
    )
    executor = HarnessExecutor(
        client,
        registry=registry,
        deadline_seconds=30,
        audit=journal(path),
    )
    request = ExecutionRequest(
        task_id=uuid.uuid4(),
        flow_id="test.flow",
        flow_version="1",
        step_id="submit",
        tool=tool.spec.name,
        instruction="提交单据",
        arguments={"token": token},
    )
    return executor.execute(request), request


def test_execution_and_effect_are_archived(tmp_path: Path) -> None:
    path = tmp_path / "audit.jsonl"

    result, request = run_step(path, SubmitTool())
    records = journal(path).read()

    assert result.outcome is ExecutionOutcome.SUCCEEDED
    kinds = [record.kind for record in records]
    assert kinds == [AuditKind.EFFECT, AuditKind.EXECUTION]
    effect = records[0]
    assert effect.payload["tool"] == "test_submit"
    assert effect.payload["status"] == "applied"
    execution = records[1]
    assert execution.payload["outcome"] == "succeeded"
    assert execution.payload["effects_applied"] == 1
    assert execution.payload["effects_unknown"] == 0
    # 只有一笔确定的外部动作，且都已成功，不需要人工核对。
    assert execution.payload["requires_manual_check"] is False
    assert execution.task_id == request.task_id
    assert execution.trace_id == result.output["trace_id"]


def test_skipped_calls_are_counted_but_never_archived_as_effects(tmp_path: Path) -> None:
    """预算掐断的第二条提交没发生过：归档里只能有一笔真实副作用。"""

    path = tmp_path / "audit.jsonl"
    tool = SubmitTool()
    registry = ToolRegistry()
    registry.register(tool)
    client = ScriptedModelClient(
        [
            ModelResponse(
                tool_calls=[
                    tool_call(tool.spec.name, call_id="c1", token="t1"),
                    tool_call(tool.spec.name, call_id="c2", token="t2"),
                ]
            )
        ]
    )
    executor = HarnessExecutor(
        client, registry=registry, deadline_seconds=30, audit=journal(path), max_tool_calls=1
    )
    request = ExecutionRequest(
        task_id=uuid.uuid4(),
        flow_id="test.flow",
        flow_version="1",
        step_id="submit",
        tool=tool.spec.name,
        instruction="提交单据",
        arguments={"token": "t1"},
    )

    executor.execute(request)
    records = journal(path).read()
    execution = next(record for record in records if record.kind is AuditKind.EXECUTION)

    assert len([record for record in records if record.kind is AuditKind.EFFECT]) == 1
    assert execution.payload["tool_calls"] == 1
    assert execution.payload["skipped_tool_calls"] == 1
    assert execution.payload["effects_applied"] == 1


def test_sensitive_arguments_never_reach_the_archive(tmp_path: Path) -> None:
    path = tmp_path / "audit.jsonl"

    run_step(path, SubmitTool(), token="token-live-9f3c")

    text = path.read_text(encoding="utf-8")
    assert "token-live-9f3c" not in text
    # 但仍要能回答"两次调用是不是同一个动作"。
    assert len(journal(path).read()[0].payload["fingerprint"]) == 16


def test_timed_out_submission_is_archived_as_unknown(tmp_path: Path) -> None:
    path = tmp_path / "audit.jsonl"

    result, _ = run_step(path, SlowSubmitTool())
    records = journal(path).read()

    assert result.outcome is ExecutionOutcome.UNCONFIRMED
    effect = next(record for record in records if record.kind is AuditKind.EFFECT)
    assert effect.payload["status"] == "unknown"
    execution = next(record for record in records if record.kind is AuditKind.EXECUTION)
    # 结果未知就必须标成"待人工核对"，这是归档存在的首要理由。
    assert execution.payload["requires_manual_check"] is True
    assert execution.payload["effects_unknown"] == 1


def test_journal_failure_does_not_change_the_execution_result(tmp_path: Path) -> None:
    class BrokenJournal:
        def append(self, entry: AuditEntry) -> None:
            raise OSError("disk full")

        def read(self, *, task_id: uuid.UUID | None = None, limit: int = 50) -> list[AuditEntry]:
            return []

    registry = ToolRegistry()
    tool = SubmitTool()
    registry.register(tool)
    client = ScriptedModelClient(
        [
            ModelResponse(tool_calls=[tool_call("test_submit", call_id="c1", token="t")]),
            ModelResponse(text=SUCCESS),
        ]
    )
    executor = HarnessExecutor(
        client, registry=registry, deadline_seconds=30, audit=BrokenJournal()
    )
    request = ExecutionRequest(
        task_id=uuid.uuid4(),
        flow_id="test.flow",
        flow_version="1",
        step_id="submit",
        tool="test_submit",
        instruction="提交单据",
        arguments={"token": "t"},
    )

    result = executor.execute(request)

    # 留痕写不进去是运维问题，不能让已经办成的事变成失败。
    assert result.outcome is ExecutionOutcome.SUCCEEDED
    assert tool.invocations == 1


# ---------- 运维接口 ----------


def make_client(audit_path: Path | None) -> tuple[TestClient, Any]:
    container = build_container(
        Settings(
            store_backend="memory",
            enable_demo_flow=False,
            ops_token=OPS_TOKEN,
            harness_audit_path=audit_path,
        ),
        flows=TEST_FLOWS,
        executor=ScriptedExecutor(),
    )
    client = TestClient(create_app(container=container))
    client.headers.update({SESSION_HEADER: container.signer.issue()})
    return client, container


def test_ops_audit_endpoint_lists_archived_entries(tmp_path: Path) -> None:
    client, container = make_client(tmp_path / "audit.jsonl")
    archived = entry(AuditKind.EFFECT, tool="test_submit", status="applied")
    container.audit.append(archived)

    response = client.get("/api/ops/audit", headers={"x-workpanda-ops-token": OPS_TOKEN})

    assert response.status_code == 200
    body = response.json()
    assert [item["trace_id"] for item in body] == [archived.trace_id]
    assert body[0]["payload"]["tool"] == "test_submit"


def test_ops_audit_endpoint_is_empty_when_archive_is_not_configured() -> None:
    client, _ = make_client(None)

    response = client.get("/api/ops/audit", headers={"x-workpanda-ops-token": OPS_TOKEN})

    # 未配置归档时返回空列表而不是 404：接口存在性不随配置变化。
    assert response.status_code == 200
    assert response.json() == []
