"""运维访问控制：角色可见性、凭据轮换、CORS 白名单与对外绑定门禁。

这些用例守的是"谁能看到什么"和"配置写错时会不会静默放行"，不是接口形状：
接口形状已由 `test_ops_api.py` 覆盖。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from tests.support import TEST_FLOWS
from workpanda.api.ops_auth import (
    OpsAccessConfigError,
    OpsUnauthenticated,
    build_ops_policy,
)
from workpanda.api.security import SESSION_HEADER
from workpanda.bootstrap import Container, build_container
from workpanda.config import Settings
from workpanda.domain.models import TaskCreateRequest
from workpanda.infrastructure.adapters import ScriptedExecutor
from workpanda.main import create_app
from workpanda.server import NetworkConfigError, build_uvicorn_config

VIEWER_SECRET = "viewer-secret-0001-for-tests"
ADMIN_SECRET = "admin-secret-0001-for-tests"
OPS_HEADER = "X-WorkPanda-Ops-Token"


def make_container(settings: Settings) -> Container:
    return build_container(settings, flows=TEST_FLOWS, executor=ScriptedExecutor())


def make_client(container: Container) -> TestClient:
    return TestClient(create_app(container=container))


def seed_task(container: Container) -> str:
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


def role_settings() -> Settings:
    return Settings(
        store_backend="memory",
        enable_demo_flow=False,
        ops_tokens=f"viewer01:viewer:{VIEWER_SECRET},admin01:admin:{ADMIN_SECRET}",
    )


def test_viewer_sees_lists_and_summary_only() -> None:
    container = make_container(role_settings())
    client = make_client(container)
    task_id = seed_task(container)
    headers = {OPS_HEADER: VIEWER_SECRET}

    assert client.get("/api/ops/tasks", headers=headers).status_code == 200
    assert client.get("/api/ops/traces/summary", headers=headers).status_code == 200

    detail_paths = [
        f"/api/ops/tasks/{task_id}",
        f"/api/ops/tasks/{task_id}/events",
        "/api/ops/traces",
        "/api/ops/audit",
        "/api/ops/tool-runtime",
    ]
    for path in detail_paths:
        response = client.get(path, headers=headers)
        assert response.status_code == 403, path
        assert response.json()["detail"]["code"] == "ops_forbidden"


def test_admin_sees_detail() -> None:
    container = make_container(role_settings())
    client = make_client(container)
    task_id = seed_task(container)
    headers = {OPS_HEADER: ADMIN_SECRET}

    for path in (
        "/api/ops/tasks",
        f"/api/ops/tasks/{task_id}",
        f"/api/ops/tasks/{task_id}/events",
        "/api/ops/traces",
        "/api/ops/traces/summary",
        "/api/ops/audit",
        "/api/ops/tool-runtime",
    ):
        assert client.get(path, headers=headers).status_code == 200, path


def test_unknown_token_is_rejected_without_leaking_roles() -> None:
    client = make_client(make_container(role_settings()))

    response = client.get("/api/ops/tasks", headers={OPS_HEADER: "admin-secret-0001-for-tesT"})
    assert response.status_code == 401
    assert response.json()["detail"]["code"] == "ops_token_invalid"
    # 401 的响应体不能告诉调用方"这个密钥像 admin"，否则等于把角色表送出去。
    assert "admin" not in response.text


def test_legacy_single_token_still_maps_to_admin() -> None:
    container = make_container(
        Settings(store_backend="memory", enable_demo_flow=False, ops_token=ADMIN_SECRET)
    )
    client = make_client(container)
    task_id = seed_task(container)

    response = client.get(f"/api/ops/tasks/{task_id}", headers={OPS_HEADER: ADMIN_SECRET})
    assert response.status_code == 200


def test_session_token_cannot_reach_ops_endpoints() -> None:
    container = make_container(role_settings())
    client = make_client(container)
    session_token = container.signer.issue()

    # 群众会话令牌放进运维请求头：不是任何运维主体的密钥，必须 401。
    assert client.get("/api/ops/tasks", headers={OPS_HEADER: session_token}).status_code == 401
    # 反过来，运维凭据也不是合法会话，不能拿它读群众侧任务。
    unauthorised = client.get(
        "/api/tasks/00000000-0000-0000-0000-000000000000",
        headers={SESSION_HEADER: ADMIN_SECRET},
    )
    assert unauthorised.status_code == 401


def test_token_rotation_keeps_old_and_new_valid() -> None:
    """轮换窗口内同一主体新旧密钥并存，切换调用方时不会出现空窗。"""

    both = build_ops_policy(
        None, f"ops01:admin:{ADMIN_SECRET},ops01:admin:admin-secret-0002-for-tests"
    )
    assert both.subjects == ("ops01",)
    assert both.authenticate(ADMIN_SECRET).subject == "ops01"
    assert both.authenticate("admin-secret-0002-for-tests").subject == "ops01"

    rotated = build_ops_policy(None, "ops01:admin:admin-secret-0002-for-tests")
    assert rotated.authenticate("admin-secret-0002-for-tests").subject == "ops01"
    # 旧密钥从配置里删掉即失效——撤销单个主体不需要停掉所有人的访问。
    with pytest.raises(OpsUnauthenticated):
        rotated.authenticate(ADMIN_SECRET)


def test_disabled_policy_hides_everything() -> None:
    policy = build_ops_policy(None, "")
    assert policy.enabled is False
    client = make_client(make_container(Settings(store_backend="memory", enable_demo_flow=False)))
    assert client.get("/api/ops/tasks", headers={OPS_HEADER: ADMIN_SECRET}).status_code == 404


@pytest.mark.parametrize(
    "spec",
    [
        f"ops01:{ADMIN_SECRET}",  # 少一段
        f"ops01:root:{ADMIN_SECRET}",  # 角色不存在
        f"bad subject:admin:{ADMIN_SECRET}",  # 主体名非法
        "ops01:admin:short",  # 密钥过短
        f"ops01:admin:{ADMIN_SECRET},ops02:admin:{ADMIN_SECRET}",  # 跨主体共用密钥
        f"ops01:admin:{ADMIN_SECRET},ops01:viewer:viewer-secret-0001-for-tests",  # 同主体两角色
    ],
)
def test_invalid_ops_token_config_fails_fast(spec: str) -> None:
    with pytest.raises(OpsAccessConfigError):
        build_ops_policy(None, spec)


def test_cors_wildcard_is_rejected() -> None:
    with pytest.raises(ValueError):
        Settings(cors_origins="*").cors_origin_list
    with pytest.raises(ValueError):
        Settings(cors_origins="https://*.example.gov.cn").cors_origin_list


def test_cors_origin_with_path_is_rejected() -> None:
    with pytest.raises(ValueError):
        Settings(cors_origins="https://ops.example.gov.cn/admin").cors_origin_list


def test_cors_allows_listed_origin_and_sends_no_header_when_empty() -> None:
    container = make_container(Settings(store_backend="memory", enable_demo_flow=False))
    with_origin = TestClient(create_app(container=container))
    plain = TestClient(create_app(container=container))
    allowlisted = TestClient(
        create_app(
            settings=Settings(
                store_backend="memory",
                enable_demo_flow=False,
                cors_origins="https://ops.example.gov.cn",
            ),
            container=container,
        )
    )

    no_middleware = with_origin.get(
        "/healthz", headers={"Origin": "https://ops.example.gov.cn"}
    )
    assert "access-control-allow-origin" not in no_middleware.headers
    response = allowlisted.get("/healthz", headers={"Origin": "https://ops.example.gov.cn"})
    assert response.headers["access-control-allow-origin"] == "https://ops.example.gov.cn"
    # 白名单之外的来源不会被回带，浏览器侧因此拒收响应。
    blocked = allowlisted.get("/healthz", headers={"Origin": "https://evil.example.com"})
    assert "access-control-allow-origin" not in blocked.headers
    assert (
        plain.options(
            "/api/tasks",
            headers={
                "Origin": "https://ops.example.gov.cn",
                "Access-Control-Request-Method": "POST",
            },
        ).status_code
        == 405
    )


def test_default_network_settings_stay_on_loopback() -> None:
    config = build_uvicorn_config(Settings())
    assert config.host == "127.0.0.1"
    assert config.ssl_certfile is None
    assert config.ssl_keyfile is None


def test_network_bind_requires_explicit_confirmation() -> None:
    with pytest.raises(NetworkConfigError):
        build_uvicorn_config(Settings(bind_host="0.0.0.0"))


def test_network_bind_requires_tls_even_when_confirmed() -> None:
    with pytest.raises(NetworkConfigError):
        build_uvicorn_config(Settings(bind_host="0.0.0.0", allow_network_bind=True))


def test_network_bind_with_tls_is_accepted(workdir) -> None:
    cert = workdir / "cert.pem"
    key = workdir / "key.pem"
    cert.write_text("dummy cert\n")
    key.write_text("dummy key\n")

    config = build_uvicorn_config(
        Settings(
            bind_host="0.0.0.0",
            allow_network_bind=True,
            tls_enabled=True,
            tls_certfile=cert,
            tls_keyfile=key,
        )
    )
    assert config.host == "0.0.0.0"
    assert config.ssl_certfile == str(cert)
    assert config.ssl_keyfile == str(key)


def test_tls_enabled_without_files_fails_fast() -> None:
    with pytest.raises(NetworkConfigError):
        build_uvicorn_config(Settings(tls_enabled=True))
    with pytest.raises(NetworkConfigError):
        build_uvicorn_config(
            Settings(
                tls_enabled=True,
                tls_certfile="/nonexistent/cert.pem",
                tls_keyfile="/nonexistent/key.pem",
            )
        )
