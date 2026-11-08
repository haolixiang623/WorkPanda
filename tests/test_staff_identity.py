"""工作人员主体、IM 身份映射与四通道互不认账。

这些用例守的是**权限面**，不是接口形状：

- 四类凭据交叉使用必须全部 401（每类一条），否则最弱的那类凭据就是实际的安全上限；
- 未映射的 IM 用户必须被拒绝，且拒绝记录里能查到 IM 标识（可追溯），
  **不降级为匿名会话**；
- 映射表变更无需重启即生效（解析每次读目录，没有进程内缓存）；
- staff 不得继承 admin 的流程编辑能力。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tests.support import TEST_FLOWS
from workpanda.api.ops_auth import OpsAccessConfigError, build_ops_policy
from workpanda.api.security import SESSION_HEADER
from workpanda.api.staff import STAFF_TOKEN_HEADER
from workpanda.bootstrap import Container, build_container
from workpanda.config import Settings
from workpanda.domain.staff import (
    STAFF_ROLE,
    ImAccessEntry,
    ImAccessOutcome,
    ImIdentity,
    ImIdentityRejected,
    ImRejectionReason,
    StaffAction,
    StaffGrant,
    StaffMember,
)
from workpanda.infrastructure.adapters import ScriptedExecutor
from workpanda.infrastructure.staff_store import InMemoryStaffStore, SqliteStaffStore
from workpanda.main import create_app

OPS_HEADER = "X-WorkPanda-Ops-Token"
ADMIN_HEADER = "X-WorkPanda-Admin-Token"

OPS_SECRET = "ops-viewer-secret-0001-for-test"
ADMIN_SECRET = "admin-secret-0001-for-tests"
VIEWER_SECRET = "admin-viewer-secret-0001"
STAFF_SECRET = "staff-secret-0001-for-tests"

OPS = {OPS_HEADER: OPS_SECRET}
ADMIN = {ADMIN_HEADER: ADMIN_SECRET}
STAFF = {STAFF_TOKEN_HEADER: STAFF_SECRET}


def identity_settings(**overrides) -> Settings:
    base = {
        "store_backend": "memory",
        "enable_demo_flow": False,
        "ops_tokens": f"duty01:viewer:{OPS_SECRET}",
        "admin_tokens": f"mgr01:admin:{ADMIN_SECRET}",
        "staff_tokens": f"staff01:{STAFF_ROLE}:{STAFF_SECRET}",
    }
    base.update(overrides)
    return Settings(**base)


def make_container(settings: Settings | None = None) -> Container:
    return build_container(
        settings or identity_settings(), flows=TEST_FLOWS, executor=ScriptedExecutor()
    )


def make_client(container: Container) -> TestClient:
    return TestClient(create_app(container=container))


def provision_staff(container: Container, subject: str = "staff01") -> StaffMember:
    return container.staff_store.save_member(
        StaffMember(
            subject=subject,
            display_name="张三",
            grants=(StaffGrant(flow_id="test.confirm", actions=frozenset(StaffAction)),),
        )
    )


def map_identity(container: Container, subject: str = "staff01", im_user_id: str = "zhangsan"):
    return container.staff_store.save_identity(
        ImIdentity(channel="wecom", im_user_id=im_user_id, principal_subject=subject)
    )


# ---------- 四类通道凭据互不认账 ----------


def test_citizen_session_token_cannot_reach_other_three_channels() -> None:
    container = make_container()
    provision_staff(container)
    client = make_client(container)
    session_token = container.signer.issue()

    assert client.get("/api/ops/tasks", headers={OPS_HEADER: session_token}).status_code == 401
    assert (
        client.get("/api/admin/flows", headers={ADMIN_HEADER: session_token}).status_code == 401
    )
    assert (
        client.get("/api/staff/me", headers={STAFF_TOKEN_HEADER: session_token}).status_code == 401
    )


def test_ops_token_cannot_reach_other_three_channels() -> None:
    container = make_container()
    provision_staff(container)
    client = make_client(container)

    assert client.get("/api/admin/flows", headers={ADMIN_HEADER: OPS_SECRET}).status_code == 401
    assert client.get("/api/staff/me", headers={STAFF_TOKEN_HEADER: OPS_SECRET}).status_code == 401
    response = client.get(
        "/api/tasks/00000000-0000-0000-0000-000000000000",
        headers={SESSION_HEADER: OPS_SECRET},
    )
    assert response.status_code == 401


def test_admin_token_cannot_reach_other_three_channels() -> None:
    container = make_container()
    provision_staff(container)
    client = make_client(container)

    assert client.get("/api/ops/tasks", headers={OPS_HEADER: ADMIN_SECRET}).status_code == 401
    assert (
        client.get("/api/staff/me", headers={STAFF_TOKEN_HEADER: ADMIN_SECRET}).status_code == 401
    )
    response = client.get(
        "/api/tasks/00000000-0000-0000-0000-000000000000",
        headers={SESSION_HEADER: ADMIN_SECRET},
    )
    assert response.status_code == 401


def test_staff_token_cannot_reach_other_three_channels() -> None:
    container = make_container()
    provision_staff(container)
    client = make_client(container)

    assert client.get("/api/ops/tasks", headers={OPS_HEADER: STAFF_SECRET}).status_code == 401
    assert client.get("/api/admin/flows", headers={ADMIN_HEADER: STAFF_SECRET}).status_code == 401
    response = client.get(
        "/api/tasks/00000000-0000-0000-0000-000000000000",
        headers={SESSION_HEADER: STAFF_SECRET},
    )
    assert response.status_code == 401


def test_staff_cannot_use_admin_flow_write_endpoints() -> None:
    """负向回归：工作人员的凭据不能改流程语义（连 admin 头都进不去）。"""

    container = make_container()
    provision_staff(container)
    client = make_client(container)

    created = client.post(
        "/api/admin/flows",
        headers=STAFF,
        json={"flow_id": "x", "version": "1", "title": "t", "steps": []},
    )
    assert created.status_code == 401
    assert created.json()["detail"]["code"] == "admin_token_invalid"

    published = client.post("/api/admin/flows/test.confirm/publish", headers=STAFF, json={})
    assert published.status_code == 401
    # 没有凭据时同样不是 403，而是 401：staff 身份不是"权限不足的 admin"。
    assert client.post("/api/admin/flows/test.confirm/disable").status_code == 401


def test_staff_endpoints_are_hidden_when_unconfigured() -> None:
    container = make_container(identity_settings(staff_tokens=""))
    provision_staff(container)
    client = make_client(container)

    assert client.get("/api/staff/me", headers=STAFF).status_code == 404
    # 管理侧凭据也不能代用：否则"没配 staff"会静默退化成"用 admin 冒充工作人员"。
    assert client.get("/api/staff/me", headers=ADMIN).status_code == 404


# ---------- 工作人员目录 ----------


def test_staff_me_reflects_directory_grants() -> None:
    container = make_container()
    provision_staff(container)
    client = make_client(container)

    profile = client.get("/api/staff/me", headers=STAFF)
    assert profile.status_code == 200
    body = profile.json()
    assert body["subject"] == "staff01"
    assert body["kind"] == "staff"
    assert "flow:test.confirm:intake" in body["roles"]

    flows = client.get("/api/staff/flows", headers=STAFF).json()
    assert [item["flow_id"] for item in flows] == ["test.confirm"]
    assert set(flows[0]["actions"]) == {"intake", "review", "handle"}


def test_staff_flows_only_lists_granted_flows() -> None:
    container = make_container()
    container.staff_store.save_member(
        StaffMember(
            subject="staff01",
            grants=(StaffGrant(flow_id="test.direct", actions=frozenset({StaffAction.HANDLE})),),
        )
    )
    client = make_client(container)

    flows = client.get("/api/staff/flows", headers=STAFF).json()
    assert [item["flow_id"] for item in flows] == ["test.direct"]
    assert flows[0]["actions"] == ["handle"]


def test_credential_without_directory_entry_is_403_not_401() -> None:
    """凭据是真的、人没开通：401 会让人反复换密钥，真正要改的是目录。"""

    container = make_container()
    client = make_client(container)

    response = client.get("/api/staff/me", headers=STAFF)
    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "staff_not_provisioned"


def test_disabled_staff_member_is_rejected() -> None:
    container = make_container()
    container.staff_store.save_member(StaffMember(subject="staff01", enabled=False))
    client = make_client(container)

    assert client.get("/api/staff/me", headers=STAFF).status_code == 403


def test_wildcard_grant_covers_every_flow() -> None:
    container = make_container()
    container.staff_store.save_member(
        StaffMember(
            subject="staff01",
            grants=(StaffGrant(flow_id="*", actions=frozenset({StaffAction.REVIEW})),),
        )
    )
    client = make_client(container)

    flows = {item["flow_id"] for item in client.get("/api/staff/flows", headers=STAFF).json()}
    assert flows == {"test.confirm", "test.direct"}


def test_deleting_member_cascades_im_mapping() -> None:
    container = make_container()
    provision_staff(container)
    map_identity(container)
    client = make_client(container)

    assert client.delete("/api/admin/identities/staff/staff01", headers=ADMIN).json()["deleted"]

    with pytest.raises(ImIdentityRejected) as rejected:
        container.im_resolver.resolve("wecom", "zhangsan")
    assert rejected.value.reason is ImRejectionReason.UNMAPPED


# ---------- IM 身份映射 ----------


def test_unmapped_im_user_is_rejected_and_traced() -> None:
    container = make_container()
    provision_staff(container)

    with pytest.raises(ImIdentityRejected) as rejected:
        container.im_resolver.resolve("wecom", "outsider-42")
    assert rejected.value.reason is ImRejectionReason.UNMAPPED

    entries = container.staff_store.list_access(rejected_only=True)
    assert entries[0].outcome is ImAccessOutcome.REJECTED
    # 可追溯的关键：拒绝记录里必须能查到 IM 标识，而不只是"有人被拦了"。
    assert entries[0].channel == "wecom"
    assert entries[0].im_user_id == "outsider-42"
    assert entries[0].reason == ImRejectionReason.UNMAPPED.value


def test_resolver_never_degrades_to_anonymous_session() -> None:
    """拒绝就是拒绝：解析失败的调用方拿不到任何 Principal。"""

    container = make_container()
    provision_staff(container)
    client = make_client(container)

    response = client.post(
        "/api/admin/identities/im/resolve",
        headers=ADMIN,
        json={"channel": "wecom", "im_user_id": "outsider-42"},
    )
    assert response.status_code == 403
    assert response.json()["detail"]["code"] == ImRejectionReason.UNMAPPED.value


def test_im_mapping_change_takes_effect_without_restart() -> None:
    """同一容器内新增/停用映射立即生效：解析每次读目录，没有进程内缓存。"""

    container = make_container()
    provision_staff(container)
    client = make_client(container)

    with pytest.raises(ImIdentityRejected):
        container.im_resolver.resolve("wecom", "zhangsan")

    created = client.put(
        "/api/admin/identities/im/wecom/zhangsan",
        headers=ADMIN,
        json={"principal_subject": "staff01"},
    )
    assert created.status_code == 200
    principal = container.im_resolver.resolve("wecom", "zhangsan")
    assert principal.subject == "staff01"
    assert principal.kind.value == "staff"

    client.put(
        "/api/admin/identities/im/wecom/zhangsan",
        headers=ADMIN,
        json={"principal_subject": "staff01", "enabled": False},
    )
    with pytest.raises(ImIdentityRejected) as rejected:
        container.im_resolver.resolve("wecom", "zhangsan")
    assert rejected.value.reason is ImRejectionReason.DISABLED


def test_mapping_to_disabled_member_is_rejected() -> None:
    container = make_container()
    container.staff_store.save_member(StaffMember(subject="staff01", enabled=False))
    map_identity(container)

    with pytest.raises(ImIdentityRejected) as rejected:
        container.im_resolver.resolve("wecom", "zhangsan")
    assert rejected.value.reason is ImRejectionReason.PRINCIPAL_DISABLED


def test_mapping_requires_existing_staff_member() -> None:
    container = make_container()
    client = make_client(container)

    response = client.put(
        "/api/admin/identities/im/wecom/zhangsan",
        headers=ADMIN,
        json={"principal_subject": "nobody"},
    )
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "admin_identity_invalid"


def test_channel_is_normalized_but_im_id_is_case_sensitive() -> None:
    container = make_container()
    provision_staff(container)
    container.staff_store.save_identity(
        ImIdentity(channel="WeCom", im_user_id="ZhangSan", principal_subject="staff01")
    )

    assert container.im_resolver.resolve("wecom", "ZhangSan").subject == "staff01"
    # 大小写不同的 IM 标识是不同的人：企业微信的 userid 就是区分大小写的。
    with pytest.raises(ImIdentityRejected):
        container.im_resolver.resolve("wecom", "zhangsan")


def test_staff_directory_write_requires_admin_role() -> None:
    """viewer 能读（排障要看"这个人为什么登不进来"），不能写（写等于给人开权限）。"""

    container = make_container(
        identity_settings(admin_tokens=f"viewer02:viewer:{VIEWER_SECRET}")
    )
    provision_staff(container)
    client = make_client(container)
    viewer = {ADMIN_HEADER: VIEWER_SECRET}

    assert client.get("/api/admin/identities/staff", headers=viewer).status_code == 200
    denied = client.put(
        "/api/admin/identities/staff/staff09",
        headers=viewer,
        json={"grants": []},
    )
    assert denied.status_code == 403
    assert denied.json()["detail"]["code"] == "admin_forbidden"
    assert client.put("/api/admin/identities/im/wecom/x", headers=viewer, json={
        "principal_subject": "staff01"
    }).status_code == 403


def test_identities_surface_is_hidden_without_admin_credentials() -> None:
    container = make_container(identity_settings(admin_tokens=""))
    provision_staff(container)
    client = make_client(container)

    # 没配置管理凭据时整个管理侧按不存在处理，工作人员/运维凭据也不能代用。
    assert client.get("/api/admin/identities/staff", headers=STAFF).status_code == 404
    assert client.get("/api/admin/identities/staff", headers=OPS).status_code == 404


def test_access_log_is_readable_and_filters_rejections() -> None:
    container = make_container()
    provision_staff(container)
    map_identity(container)
    container.im_resolver.resolve("wecom", "zhangsan")
    with pytest.raises(ImIdentityRejected):
        container.im_resolver.resolve("wecom", "outsider-42")
    client = make_client(container)

    everything = client.get("/api/admin/identities/im/access-log", headers=ADMIN).json()
    assert [entry["outcome"] for entry in everything] == ["rejected", "granted"]
    rejected = client.get(
        "/api/admin/identities/im/access-log?rejected_only=true", headers=ADMIN
    ).json()
    assert len(rejected) == 1
    assert rejected[0]["im_user_id"] == "outsider-42"


# ---------- 配置与持久化 ----------


@pytest.mark.parametrize(
    "spec",
    [
        f"staff01:{STAFF_SECRET}",  # 少一段
        f"staff01:admin:{STAFF_SECRET}",  # staff 凭据不得写成 admin
        f"staff01:{STAFF_ROLE}:short",  # 密钥过短
        f"staff01:{STAFF_ROLE}:{STAFF_SECRET},staff02:{STAFF_ROLE}:{STAFF_SECRET}",  # 共用密钥
    ],
)
def test_invalid_staff_token_config_fails_fast(spec: str) -> None:
    with pytest.raises(OpsAccessConfigError):
        build_ops_policy(None, spec, allowed_roles=(STAFF_ROLE,), field="staff_tokens")
    with pytest.raises(OpsAccessConfigError):
        make_container(identity_settings(staff_tokens=spec))


def test_staff_store_survives_rebuild_on_sqlite(workdir: Path) -> None:
    path = workdir / "workpanda.db"
    settings = identity_settings(store_backend="sqlite", sqlite_path=path)

    first = build_container(settings, flows=TEST_FLOWS, executor=ScriptedExecutor())
    try:
        provision_staff(first)
        map_identity(first)
    finally:
        first.close()

    second = build_container(settings, flows=TEST_FLOWS, executor=ScriptedExecutor())
    try:
        assert second.staff_store.get_member("staff01") is not None
        principal = second.im_resolver.resolve("wecom", "zhangsan")
        assert principal.subject == "staff01"
        assert second.staff_store.list_access()[0].outcome is ImAccessOutcome.GRANTED
    finally:
        second.close()


def test_sqlite_store_roundtrip_and_access_trail(workdir: Path) -> None:
    store = SqliteStaffStore(workdir / "staff.db")
    try:
        store.save_member(StaffMember(subject="staff01"))
        store.save_identity(
            ImIdentity(channel="wecom", im_user_id="zhangsan", principal_subject="staff01")
        )
        store.record_access(
            ImAccessEntry(
                channel="wecom",
                im_user_id="outsider-42",
                outcome=ImAccessOutcome.REJECTED,
                reason=ImRejectionReason.UNMAPPED.value,
            )
        )
        assert store.resolve_identity("wecom", "zhangsan") is not None
        assert store.list_access(rejected_only=True)[0].im_user_id == "outsider-42"
        # 删除主体必须连带清掉指向它的映射，不留"指向已删除的人"的半截状态。
        assert store.delete_member("staff01") is True
        assert store.get_member("staff01") is None
        assert store.resolve_identity("wecom", "zhangsan") is None
    finally:
        store.close()


def test_memory_store_returns_copies() -> None:
    store = InMemoryStaffStore()
    saved = store.save_member(StaffMember(subject="staff01", display_name="张三"))
    saved.display_name = "改到的是调用方那一份"
    assert store.get_member("staff01").display_name == "张三"
