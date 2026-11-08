"""Issue #53：容量基线指标与部署形态一致性门禁。

重点不是「端点能返回 200」，而是几条不该发生的事：不可测的指标不能返回 0 冒充
正常、多实例不能配着进程内状态后端启动、指标里不能出现凭据或群众原话、以及
新增门禁不能把 Redis 探活的 fail-closed 语义放宽。
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from tests.fake_redis import FakeRedisServer
from tests.support import TEST_FLOWS
from workpanda.api.security import SESSION_HEADER
from workpanda.bootstrap import build_container
from workpanda.capacity import (
    DeploymentConfigError,
    MetricValue,
    collect_capacity,
    deployment_view,
    validate_deployment_topology,
)
from workpanda.config import Settings
from workpanda.domain.models import TaskCreateRequest
from workpanda.harness.shared_state import SharedStateError
from workpanda.harness.trace import TraceRecord
from workpanda.infrastructure.adapters import ScriptedExecutor
from workpanda.infrastructure.event_broker import EventBroker
from workpanda.main import create_app
from workpanda.server import build_uvicorn_config

ADMIN_TOKEN = "ops-admin-token-for-tests-0001"
VIEWER_TOKEN = "ops-viewer-token-for-tests-0002"
OPS_TOKEN_HEADER = "X-WorkPanda-Ops-Token"


@pytest.fixture
def redis_server() -> Iterator[FakeRedisServer]:
    server = FakeRedisServer()
    try:
        yield server
    finally:
        server.stop()


def make_settings(**overrides: object) -> Settings:
    """容量测试用到的值全部由显式参数决定，不读本机 .env。

    测试进程在 conftest 加载时已屏蔽 `.env`（#72），本函数的职责是把用到的
    key 枚举清楚，而不是再单独传 `_env_file=None`。详见 conftest 的注释。
    """

    base = {
        "store_backend": "memory",
        "enable_demo_flow": False,
        "ops_tokens": f"boss:admin:{ADMIN_TOKEN},duty:viewer:{VIEWER_TOKEN}",
    }
    base.update(overrides)
    return Settings(**base)


def make_client(settings: Settings, tmp_path: Path | None = None) -> tuple[TestClient, object]:
    if settings.store_backend == "sqlite":
        assert tmp_path is not None
        settings = settings.model_copy(update={"sqlite_path": tmp_path / "capacity.db"})
    container = build_container(settings, flows=TEST_FLOWS, executor=ScriptedExecutor())
    client = TestClient(create_app(container=container))
    client.headers.update({SESSION_HEADER: container.signer.issue()})
    return client, container


# ---------------------------------------------------------------------------
# 1. 部署形态一致性门禁
# ---------------------------------------------------------------------------


def test_single_instance_default_is_unchanged() -> None:
    """默认单实例 + 进程内状态：门禁不得改变现有部署行为。"""

    settings = make_settings()
    validate_deployment_topology(settings)
    assert settings.expected_instances == 1
    assert settings.shared_backend == "memory"
    assert deployment_view(settings)["requires_shared_state"] is False


@pytest.mark.parametrize("backend", ["memory", "sqlite", "redis"])
def test_single_instance_accepts_any_shared_backend(backend: str) -> None:
    """单实例下没有第二个执行者竞争，先单独切 Redis 也允许（升级顺序第 2 步）。"""

    validate_deployment_topology(make_settings(harness_shared_backend=backend))


def test_multi_instance_with_memory_shared_backend_is_rejected() -> None:
    """Issue #53 最核心的「不应发生」：多实例却各自持有进程内状态。"""

    settings = make_settings(expected_instances=2)
    with pytest.raises(DeploymentConfigError) as caught:
        validate_deployment_topology(settings)
    message = str(caught.value)
    assert "redis" in message
    assert "租约" in message
    assert "memory" in message


def test_multi_instance_with_sqlite_shared_backend_is_rejected() -> None:
    """sqlite 共享只覆盖同机多进程，不能当作多实例共享后端。"""

    settings = make_settings(
        expected_instances=2,
        harness_shared_backend="sqlite",
        store_backend="sqlite",
        harness_lease_enabled=True,
    )
    with pytest.raises(DeploymentConfigError, match="redis"):
        validate_deployment_topology(settings)


def test_multi_instance_without_lease_is_rejected() -> None:
    """租约没开时，同一 (task, step) 会被两个实例各执行一次。"""

    settings = make_settings(
        expected_instances=2,
        harness_shared_backend="redis",
        store_backend="sqlite",
        harness_lease_enabled=False,
    )
    with pytest.raises(DeploymentConfigError, match="租约"):
        validate_deployment_topology(settings)


def test_multi_instance_with_memory_store_is_rejected() -> None:
    """任务与确认凭证不能只留在各自进程内。"""

    settings = make_settings(
        expected_instances=2,
        harness_shared_backend="redis",
        store_backend="memory",
        harness_lease_enabled=True,
    )
    with pytest.raises(DeploymentConfigError, match="任务存储为 memory"):
        validate_deployment_topology(settings)


def test_multi_instance_fully_configured_passes() -> None:
    settings = make_settings(
        expected_instances=3,
        harness_shared_backend="redis",
        store_backend="sqlite",
        harness_lease_enabled=True,
    )
    validate_deployment_topology(settings)
    assert deployment_view(settings)["requires_shared_state"] is True


@pytest.mark.parametrize("instances", [0, -1])
def test_invalid_instance_count_is_rejected(instances: int) -> None:
    with pytest.raises(DeploymentConfigError, match="必须 >= 1"):
        validate_deployment_topology(make_settings(expected_instances=instances))


def test_build_container_enforces_topology_before_opening_resources() -> None:
    """门禁挂在装配入口：坏配置连容器都建不起来，不会先占住存储。"""

    with pytest.raises(DeploymentConfigError):
        build_container(make_settings(expected_instances=2), flows=TEST_FLOWS)


def test_server_startup_chain_enforces_topology() -> None:
    """`python -m workpanda` 这条链也要拦住，不只 lifespan 那条。"""

    with pytest.raises(DeploymentConfigError):
        build_uvicorn_config(make_settings(expected_instances=2))


def test_gate_passes_but_unreachable_redis_still_fails_closed(tmp_path: Path) -> None:
    """门禁放行不代表放宽 Redis 探活：连不上仍必须启动失败。

    这是验收标准「Redis 后端不可用时启动失败（现有 fail-closed 语义不得放松）」。
    断言抛的是 SharedStateError（探活），而不是被门禁吞掉或改成警告。
    """

    settings = make_settings(
        expected_instances=2,
        harness_shared_backend="redis",
        harness_redis_url="redis://127.0.0.1:1/0",
        store_backend="sqlite",
        sqlite_path=tmp_path / "unreachable.db",
        harness_lease_enabled=True,
    )
    validate_deployment_topology(settings)  # 形态本身是自洽的
    with pytest.raises(SharedStateError):
        build_container(settings, flows=TEST_FLOWS)


def test_multi_instance_with_live_redis_can_start(
    redis_server: FakeRedisServer, tmp_path: Path
) -> None:
    """完整多实例配置 + 可用 Redis：能真正装配出容器。"""

    settings = make_settings(
        expected_instances=2,
        harness_shared_backend="redis",
        harness_redis_url=redis_server.url,
        store_backend="sqlite",
        sqlite_path=tmp_path / "multi.db",
        harness_lease_enabled=True,
    )
    container = build_container(settings, flows=TEST_FLOWS)
    try:
        assert deployment_view(container.settings)["requires_shared_state"] is True
    finally:
        container.close()


# ---------------------------------------------------------------------------
# 2. 指标契约：不可测绝不返回 0
# ---------------------------------------------------------------------------


def test_metric_value_forbids_zero_for_unavailable() -> None:
    with pytest.raises(ValidationError, match="不得携带数值"):
        MetricValue(
            status="not_applicable",
            available=False,
            value=0,
            unit="ms",
            reason="没有来源",
            source="test",
        )
    with pytest.raises(ValidationError, match="必须给出实测数值"):
        MetricValue(status="ok", available=True, value=None, unit="ms", source="test")
    with pytest.raises(ValidationError, match="必须说明原因"):
        MetricValue(status="error", available=False, value=None, unit="ms", source="test")


def test_metrics_endpoint_requires_admin(tmp_path: Path) -> None:
    settings = make_settings()
    client, container = make_client(settings, tmp_path)
    try:
        assert client.get("/api/ops/metrics").status_code == 401
        assert (
            client.get(
                "/api/ops/metrics", headers={OPS_TOKEN_HEADER: VIEWER_TOKEN}
            ).status_code
            == 403
        )
        assert (
            client.get(
                "/api/ops/metrics", headers={OPS_TOKEN_HEADER: ADMIN_TOKEN}
            ).status_code
            == 200
        )
    finally:
        container.close()


def test_metrics_hidden_when_ops_disabled(tmp_path: Path) -> None:
    client, container = make_client(make_settings(ops_tokens="", ops_token=None), tmp_path)
    try:
        assert client.get("/api/ops/metrics").status_code == 404
    finally:
        container.close()


def test_unavailable_metrics_never_report_zero(tmp_path: Path) -> None:
    """缺参/歧义验收：采不到的指标必须 value=null + 原因，不能是 0。"""

    client, container = make_client(make_settings(), tmp_path)
    try:
        body = client.get(
            "/api/ops/metrics", headers={OPS_TOKEN_HEADER: ADMIN_TOKEN}
        ).json()
        metrics = body["metrics"]

        assert body["degraded"] is False  # not_applicable 不是故障
        assert metrics["sse_connections"]["value"] == 0  # 真实的 0：确实没有连接
        assert metrics["sse_connections"]["status"] == "ok"

        for name in ("database_file_bytes", "concurrent_tool_threads"):
            assert metrics[name]["status"] == "not_applicable"
            assert metrics[name]["value"] is None
            assert metrics[name]["available"] is False
            assert metrics[name]["reason"]

        for name in ("sqlite_write_lock_wait_ms", "task_create_p95_ms"):
            assert metrics[name]["status"] == "not_instrumented"
            assert metrics[name]["value"] is None
            assert "埋点" in metrics[name]["reason"]

        assert set(body["instrumentation_gaps"]) == {
            "sqlite_write_lock_wait_ms",
            "task_create_p95_ms",
        }
        assert set(body["not_applicable"]) == {
            "database_file_bytes",
            "concurrent_tool_threads",
        }
        assert body["deployment"]["requires_shared_state"] is False
    finally:
        container.close()


def test_collection_error_is_reported_not_swallowed_into_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """采集抛异常 -> error + degraded，且不带异常 message（可能含敏感值）。"""

    settings = make_settings()
    if settings.store_backend == "sqlite":
        settings = settings.model_copy(update={"sqlite_path": tmp_path / "capacity.db"})
    container = build_container(settings, flows=TEST_FLOWS, executor=ScriptedExecutor())
    client = TestClient(create_app(container=container))
    try:

        def broken() -> int:
            raise RuntimeError(f"连接串 redis://secret-password@db.internal 与路径 {tmp_path}")

        monkeypatch.setattr(container.broker, "total_subscriber_count", broken)
        body = client.get(
            "/api/ops/metrics", headers={OPS_TOKEN_HEADER: ADMIN_TOKEN}
        ).json()
        item = body["metrics"]["sse_connections"]
        assert item["status"] == "error"
        assert item["value"] is None
        assert item["reason"] == "采集失败（RuntimeError），已按不可用上报"
        assert "secret-password" not in str(body)
        assert str(tmp_path) not in str(body)
        assert body["degraded"] is True
        # 单项故障不影响其它指标继续采集
        assert body["metrics"]["trace_resident"]["status"] == "ok"
    finally:
        container.close()


def test_metric_values_match_real_sources(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """数值与来源对得上，不是随手编的常量。"""

    client, container = make_client(make_settings(), tmp_path)
    try:
        for outcome in ("succeeded", "unconfirmed"):
            container.traces.save(
                TraceRecord(
                    trace_id=uuid.uuid4().hex,
                    task_id=uuid.uuid4(),
                    flow_id="test.flow",
                    flow_version="1",
                    step_id="run",
                    outcome=outcome,
                    stop_reason="completed",
                )
            )
        monkeypatch.setattr(container.broker, "total_subscriber_count", lambda: 7)
        body = client.get(
            "/api/ops/metrics", headers={OPS_TOKEN_HEADER: ADMIN_TOKEN}
        ).json()
        metrics = body["metrics"]
        assert metrics["trace_resident"]["value"] == 2
        assert metrics["trace_unconfirmed_rate"]["value"] == 0.5
        assert metrics["sse_connections"]["value"] == 7
    finally:
        container.close()


def test_sqlite_backend_reports_real_file_size(tmp_path: Path) -> None:
    client, container = make_client(make_settings(store_backend="sqlite"), tmp_path)
    try:
        body = client.get(
            "/api/ops/metrics", headers={OPS_TOKEN_HEADER: ADMIN_TOKEN}
        ).json()
        item = body["metrics"]["database_file_bytes"]
        assert item["status"] == "ok"
        assert item["value"] == float(container.settings.sqlite_path.stat().st_size)
        assert item["value"] > 0
        assert "database_file_bytes" not in body["not_applicable"]
    finally:
        container.close()


def test_harness_enabled_reports_tool_thread_ledger() -> None:
    container = build_container(
        make_settings(
            harness_enabled=True,
            model_base_url="http://127.0.0.1:9/v1",
            model_api_key="not-a-real-key-for-tests",
        ),
        flows=TEST_FLOWS,
    )
    try:
        body = collect_capacity(container).model_dump()
        item = body["metrics"]["concurrent_tool_threads"]
        assert item["status"] == "ok"
        assert item["value"] == 0
    finally:
        container.close()


def test_metrics_never_leak_credentials_or_citizen_text(tmp_path: Path) -> None:
    """验收「不应发生」：指标里出现明文凭据或群众原话。"""

    sentinel_model_key = "sentinel-model-key-should-not-leak"
    settings = make_settings(
        model_api_key=sentinel_model_key,
        session_secret="sentinel-session-secret-should-not-leak",
        harness_redis_url="redis://:sentinel-redis-password@127.0.0.1:6379/0",
    )
    client, container = make_client(settings, tmp_path)
    try:
        citizen_text = "群众原话：身份证号与家庭住址等敏感信息不应出现在指标里"
        container.tasks.create_task(
            TaskCreateRequest(
                conversation_id="conversation-capacity",
                input_text=citizen_text,
                flow_id="test.direct",
            ),
            "session-capacity",
        )
        raw = client.get(
            "/api/ops/metrics", headers={OPS_TOKEN_HEADER: ADMIN_TOKEN}
        ).text
        assert ADMIN_TOKEN not in raw
        assert VIEWER_TOKEN not in raw
        assert sentinel_model_key not in raw
        assert "sentinel-session-secret" not in raw
        assert "sentinel-redis-password" not in raw
        assert "身份证号" not in raw
        assert citizen_text not in raw
    finally:
        container.close()


def test_deployment_view_exposes_only_enums_and_flags() -> None:
    settings = make_settings(
        expected_instances=2,
        harness_shared_backend="redis",
        harness_redis_url="redis://:sentinel-redis-password@127.0.0.1:6379/0",
        store_backend="sqlite",
        harness_lease_enabled=True,
    )
    view = deployment_view(settings)
    assert set(view) == {
        "expected_instances",
        "store_backend",
        "shared_backend",
        "lease_enabled",
        "requires_shared_state",
    }
    assert "sentinel-redis-password" not in str(view)
    assert view["requires_shared_state"] is True


# ---------------------------------------------------------------------------
# 3. SSE 并发连接数的来源
# ---------------------------------------------------------------------------


async def test_total_subscriber_count_sums_all_tasks_and_drops_on_disconnect() -> None:
    broker = EventBroker()
    first, second = uuid.uuid4(), uuid.uuid4()

    assert broker.total_subscriber_count() == 0
    async with broker.subscribe(first):
        assert broker.total_subscriber_count() == 1
        async with broker.subscribe(first), broker.subscribe(second):
            assert broker.total_subscriber_count() == 3
            assert broker.subscriber_count(first) == 2
            assert broker.subscriber_count(second) == 1
        assert broker.total_subscriber_count() == 1
    assert broker.total_subscriber_count() == 0
