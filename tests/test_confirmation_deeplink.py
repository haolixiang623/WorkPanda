"""卡片深链确认（Issue #65 / 提案 N5）：红线、负例与凭证契约。

这份用例守的是**授权语义的边界**，不是接口形状：

- 红线（`test_im_text_reply_never_issues_confirmation`）：IM 文本回复（"确认"
  "yes""同意"）**不得签发任何确认凭证**。实现这一点的不是某段判断，而是
  "worker 主体在结构上没有落点"——能签发凭证的只有 `/api/tasks/{id}/confirm`，
  而它只认任务所属的会话。用例把"端点是 404"和"凭证确实没被签发"一起钉住。
- 负例：一次性、TTL 到期、跨任务使用、转发他人会话，四种都拒绝并留痕，
  且**拒绝不消耗卡片**（除了真正被用掉的那张）。
- 契约：确认凭证的绑定字段与现状一致（`task + flow_version + step + 参数摘要 +
  user_ref + TTL`），深链落地**不新增**第二条签发路径。
"""

from __future__ import annotations

import threading
from datetime import timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from tests.support import TEST_FLOWS
from workpanda.api.security import SESSION_HEADER
from workpanda.api.staff import STAFF_TOKEN_HEADER
from workpanda.api.staff import router as staff_router
from workpanda.application.deeplink_confirm import (
    CONFIRM_PAGE_PATH,
    TASK_PARAM,
    TOKEN_PARAM,
)
from workpanda.bootstrap import Container, build_container
from workpanda.config import Settings
from workpanda.domain.confirmation import Confirmation, parameter_digest
from workpanda.domain.deeplink import (
    MAX_TTL_SECONDS,
    ConfirmationLink,
    ConfirmationLinkAttempt,
    ConfirmationLinkOutcome,
    ConfirmationLinkRejected,
    ConfirmationLinkRejection,
    token_fingerprint,
)
from workpanda.domain.identity import Principal, PrincipalKind
from workpanda.domain.models import EventType, TaskCreateRequest, TaskStatus, utc_now
from workpanda.domain.staff import (
    STAFF_ROLE,
    StaffAction,
    StaffGrant,
    StaffMember,
)
from workpanda.infrastructure.adapters import ScriptedExecutor
from workpanda.infrastructure.deeplink_store import (
    InMemoryConfirmationLinkStore,
    SqliteConfirmationLinkStore,
)
from workpanda.main import create_app

STAFF_SECRET = "staff-secret-0001-for-tests"
STAFF_HEADER = {STAFF_TOKEN_HEADER: STAFF_SECRET}

FLOW_ID = "test.confirm"
WAITING_STEP = "submit"


# ---------- 夹具 ----------


def link_settings(**overrides) -> Settings:
    base = {
        "store_backend": "memory",
        "enable_demo_flow": False,
        "staff_tokens": f"staff01:{STAFF_ROLE}:{STAFF_SECRET}",
    }
    base.update(overrides)
    return Settings(**base)


def make_container(settings: Settings | None = None) -> Container:
    return build_container(
        settings or link_settings(), flows=TEST_FLOWS, executor=ScriptedExecutor()
    )


def make_client(container: Container, session: str | None = None) -> TestClient:
    client = TestClient(create_app(container=container))
    if session is not None:
        client.headers.update({SESSION_HEADER: session})
    return client


def provision(
    container: Container,
    subject: str = "staff01",
    actions: frozenset[StaffAction] = frozenset(StaffAction),
) -> StaffMember:
    return container.staff_store.save_member(
        StaffMember(
            subject=subject,
            display_name="张三",
            grants=(StaffGrant(flow_id=FLOW_ID, actions=actions),),
        )
    )


def waiting_task(container: Container, subject: str) -> UUID:
    """造一张"正在等人确认"的任务：只有这个状态才谈得上发卡。"""

    task = container.tasks.create_task(
        TaskCreateRequest(
            conversation_id="conversation-1",
            input_text="我要办理测试事项",
            flow_id=FLOW_ID,
            slots={"subject": "测试主题", "contact": "12345"},
        ),
        subject,
    )
    assert task.status is TaskStatus.WAITING_FOR_CONFIRMATION
    assert task.current_step == WAITING_STEP
    return task.task_id


def confirmation_issued(container: Container, task_id: UUID) -> bool:
    """任务上是否存在（含已消费的）确认凭证。红线的唯一判据。"""

    return container.confirmations.latest(task_id, WAITING_STEP) is not None


def issue_card(client: TestClient, task_id: UUID) -> dict:
    response = client.post(f"/api/staff/tasks/{task_id}/confirmation-links", headers=STAFF_HEADER)
    assert response.status_code == 201, response.text
    return response.json()


def attempt_reasons(container: Container, task_id: UUID) -> list[str]:
    return [
        attempt.reason
        for attempt in container.confirmation_links.attempts(task_id=task_id)
        if attempt.outcome is ConfirmationLinkOutcome.REJECTED
    ]


# ---------- 红线 ----------


def test_im_text_reply_never_issues_confirmation() -> None:
    """IM 文本回复不构成任何授权：不签发凭证、不推进任务、不留成功事件。

    这条用例是"结构"证据，不是"忘了写某个 if"的证据：能签发确认凭证的路径只有
    `/api/tasks/{id}/confirm`，它只认任务所属的会话；工作人员主体无论把
    "确认""yes""同意"发到哪个端点上，都拿不到凭证。
    """

    container = make_container()
    provision(container)
    session = container.signer.issue()
    citizen = make_client(container, session)
    staff = make_client(container)
    task_id = waiting_task(container, container.signer.verify(session).subject)

    # ① 端点上不存在"工作人员确认"这条路：常见猜法全部 404/405。
    for path in (
        f"/api/staff/tasks/{task_id}/confirm",
        f"/api/staff/tasks/{task_id}/confirmation",
        "/api/staff/confirmations",
        f"/api/staff/tasks/{task_id}/answers",
    ):
        response = staff.post(path, headers=STAFF_HEADER, json={"step_id": WAITING_STEP})
        assert response.status_code in {404, 405}, path

    # ② 把 IM 文本原样塞进群众侧的确认接口，用工作人员凭据 → 401（通道互不认账）。
    for text in ("确认", "yes", "同意", "OK"):
        response = staff.post(
            f"/api/tasks/{task_id}/confirm",
            headers={SESSION_HEADER: STAFF_SECRET},
            json={"step_id": WAITING_STEP, "accepted": True, "text": text},
        )
        assert response.status_code == 401, text

    # ③ 工作人员唯一能给出去的是一张"导航卡"：发卡本身也不得签发凭证。
    ticket = issue_card(staff, task_id)
    assert ticket["step_id"] == WAITING_STEP

    # ④ 红线判据：没有任何凭证被签发，任务没有被推进，也没有确认成功事件。
    assert confirmation_issued(container, task_id) is False
    assert container.tasks.get_task(task_id, container.signer.verify(session).subject).status is (
        TaskStatus.WAITING_FOR_CONFIRMATION
    )
    events = container.tasks.events(task_id, container.signer.verify(session).subject)
    decided = {
        EventType.CONFIRMATION_ACCEPTED.value,
        EventType.CONFIRMATION_DECLINED.value,
    }
    assert [event.type.value for event in events if event.type.value in decided] == []
    assert EventType.CONFIRMATION_REQUIRED.value in [event.type.value for event in events]

    # ⑤ 工作人员拿不到卡：确认通道只认任务所属的会话，带着 staff 凭据去点，
    #    服务端看到的是一个新建的匿名会话，与任务归属对不上。
    response = staff.post(
        f"/api/tasks/{task_id}/confirmation-link",
        headers=STAFF_HEADER,
        json={"token": ticket["token"]},
    )
    assert response.status_code == 409
    assert ConfirmationLinkRejection.SESSION_NOT_OWNER.value in attempt_reasons(container, task_id)
    assert confirmation_issued(container, task_id) is False

    # ⑥ 只有本人在群众会话里确认，才产生凭证并推进任务。
    response = citizen.post(
        f"/api/tasks/{task_id}/confirm", json={"step_id": WAITING_STEP, "accepted": True}
    )
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "completed"
    assert confirmation_issued(container, task_id) is True


def test_service_refuses_non_citizen_principal() -> None:
    """兜底分支：确认通道永远给出 citizen 主体，服务层仍不假设这一点。

    `get_principal` 只认会话令牌，所以 HTTP 层到不了这个分支；但"深链只能由
    群众会话落地"是授权语义，不能只靠路由层的依赖注入来保证。
    """

    container = make_container()
    provision(container)
    session = container.signer.issue()
    subject = container.signer.verify(session).subject
    task_id = waiting_task(container, subject)
    ticket = issue_card(make_client(container), task_id)

    with pytest.raises(ConfirmationLinkRejected) as excinfo:
        container.confirmation_links.redeem(
            task_id,
            ticket["token"],
            Principal(subject=subject, kind=PrincipalKind.STAFF),
        )
    assert excinfo.value.reason is ConfirmationLinkRejection.PRINCIPAL_NOT_CITIZEN
    assert attempt_reasons(container, task_id) == [
        ConfirmationLinkRejection.PRINCIPAL_NOT_CITIZEN.value
    ]
    # 被拒不影响卡片本身：真正的任务主人仍然可以用它。
    assert (
        container.confirmation_links.redeem(
            task_id, ticket["token"], Principal(subject=subject, kind=PrincipalKind.CITIZEN)
        ).step_id
        == WAITING_STEP
    )


def test_staff_surface_has_no_task_state_mutating_route() -> None:
    """工作人员接口面就是这四个；发卡与看留痕之外，没有任何改动任务状态的入口。

    这是白名单断言：新增一个 staff 端点会直接让这条用例失败，逼着"再加一个入口"
    变成一次显式决定——深链确认之所以安全，靠的正是"没有别的入口"。
    """

    surface = sorted(
        (route.path, method) for route in staff_router.routes for method in route.methods
    )
    assert surface == sorted(
        [
            ("/api/staff/me", "GET"),
            ("/api/staff/flows", "GET"),
            ("/api/staff/tasks/{task_id}/confirmation-links", "POST"),
            ("/api/staff/tasks/{task_id}/confirmation-links/attempts", "GET"),
        ]
    )


# ---------- 落地：不签发凭证 ----------


def test_landing_returns_no_credential_and_leaves_task_untouched() -> None:
    container = make_container()
    provision(container)
    session = container.signer.issue()
    subject = container.signer.verify(session).subject
    citizen = make_client(container, session)
    task_id = waiting_task(container, subject)

    ticket = issue_card(make_client(container), task_id)
    response = citizen.post(
        f"/api/tasks/{task_id}/confirmation-link", json={"token": ticket["token"]}
    )
    assert response.status_code == 200, response.text
    landing = response.json()

    # 响应体的形状就是红线的形状证据：字段集合被钉死，多一个 token 就多一条签发路径。
    assert set(landing) == {"task_id", "step_id", "flow_version", "confirm_path"}
    assert landing["task_id"] == str(task_id)
    assert landing["step_id"] == WAITING_STEP
    assert landing["confirm_path"] == f"/api/tasks/{task_id}/confirm"
    assert not any("token" in key for key in landing)

    assert confirmation_issued(container, task_id) is False
    assert container.tasks.get_task(task_id, subject).status is TaskStatus.WAITING_FOR_CONFIRMATION


def test_confirmation_credential_binding_contract_unchanged() -> None:
    """凭证的绑定字段与现状一致，且参数摘要仍参与绑定。"""

    assert {field for field in Confirmation.model_fields} == {
        "token",
        "task_id",
        "step_id",
        "flow_version",
        "parameter_digest",
        "user_ref",
        "issued_at",
        "expires_at",
        "consumed_at",
    }

    container = make_container()
    provision(container)
    session = container.signer.issue()
    subject = container.signer.verify(session).subject
    citizen = make_client(container, session)
    task_id = waiting_task(container, subject)

    issue_card(make_client(container), task_id)
    citizen.post(f"/api/tasks/{task_id}/confirmation-link", json={"token": "unused"})
    citizen.post(f"/api/tasks/{task_id}/confirm", json={"step_id": WAITING_STEP, "accepted": True})

    issued = container.confirmations.latest(task_id, WAITING_STEP)
    assert issued is not None
    assert issued.task_id == task_id
    assert issued.step_id == WAITING_STEP
    assert issued.flow_version == "1"
    assert issued.user_ref == subject
    assert issued.parameter_digest == parameter_digest(
        FLOW_ID, "1", WAITING_STEP, {"subject": "测试主题", "contact": "12345"}
    )
    assert issued.expires_at > issued.issued_at
    # 服务端签发即消费：凭证不出现在任何响应里（群众侧确认接口只回任务视图）。
    assert issued.consumed_at is not None


# ---------- 负例：一次性 / 过期 / 跨任务 / 转发 ----------


def test_deep_link_is_single_use() -> None:
    container = make_container()
    provision(container)
    session = container.signer.issue()
    subject = container.signer.verify(session).subject
    citizen = make_client(container, session)
    task_id = waiting_task(container, subject)
    ticket = issue_card(make_client(container), task_id)

    path = f"/api/tasks/{task_id}/confirmation-link"
    assert citizen.post(path, json={"token": ticket["token"]}).status_code == 200
    second = citizen.post(path, json={"token": ticket["token"]})
    assert second.status_code == 409
    assert second.json()["code"] == "confirmation_link_invalid"
    assert ConfirmationLinkRejection.ALREADY_USED.value in attempt_reasons(container, task_id)


def test_deep_link_expiry_is_rejected() -> None:
    container = make_container()
    provision(container)
    session = container.signer.issue()
    subject = container.signer.verify(session).subject
    citizen = make_client(container, session)
    task_id = waiting_task(container, subject)
    ticket = issue_card(make_client(container), task_id)

    # 把已存下的卡改成"已过期"：TTL 判定落在 domain 里，不额外注入时钟。
    link = container.confirmation_links.links.get(ticket["token"])
    assert link is not None
    container.confirmation_links.links.issue(
        link.model_copy(update={"expires_at": utc_now() - timedelta(seconds=1)})
    )

    response = citizen.post(
        f"/api/tasks/{task_id}/confirmation-link", json={"token": ticket["token"]}
    )
    assert response.status_code == 409
    assert ConfirmationLinkRejection.EXPIRED.value in attempt_reasons(container, task_id)


def test_deep_link_cross_task_use_is_rejected_and_keeps_card_usable() -> None:
    container = make_container()
    provision(container)
    session = container.signer.issue()
    subject = container.signer.verify(session).subject
    citizen = make_client(container, session)
    task_a = waiting_task(container, subject)
    task_b = waiting_task(container, subject)
    ticket = issue_card(make_client(container), task_a)

    response = citizen.post(
        f"/api/tasks/{task_b}/confirmation-link", json={"token": ticket["token"]}
    )
    assert response.status_code == 409
    assert ConfirmationLinkRejection.TASK_MISMATCH.value in attempt_reasons(container, task_a)

    # 被拒的只是这一次使用：卡片属于 A，去 A 上仍然是好的（拒绝不该消耗卡片）。
    assert (
        citizen.post(
            f"/api/tasks/{task_a}/confirmation-link", json={"token": ticket["token"]}
        ).status_code
        == 200
    )


def test_forwarded_deep_link_cannot_be_used_by_another_session() -> None:
    container = make_container()
    provision(container)
    owner_session = container.signer.issue()
    owner = container.signer.verify(owner_session).subject
    other = container.signer.verify(container.signer.issue()).subject
    assert owner != other

    task_id = waiting_task(container, owner)
    ticket = issue_card(make_client(container), task_id)

    forwarded = make_client(container, container.signer.issue()).post(
        f"/api/tasks/{task_id}/confirmation-link", json={"token": ticket["token"]}
    )
    assert forwarded.status_code == 409
    assert ConfirmationLinkRejection.SESSION_NOT_OWNER.value in attempt_reasons(container, task_id)

    # 转发没有收益：卡还在，主人仍然能用。
    assert (
        make_client(container, owner_session)
        .post(f"/api/tasks/{task_id}/confirmation-link", json={"token": ticket["token"]})
        .status_code
        == 200
    )


def test_unknown_token_is_rejected_and_traced() -> None:
    container = make_container()
    provision(container)
    session = container.signer.issue()
    subject = container.signer.verify(session).subject
    task_id = waiting_task(container, subject)

    response = make_client(container, session).post(
        f"/api/tasks/{task_id}/confirmation-link", json={"token": "not-a-real-link-token"}
    )
    assert response.status_code == 409

    # 认不出的令牌归不到任务上，所以它落在"不带 task_id"的那一档：
    # 留痕必须先记下"有人拿了个不存在的卡在试"，而不是静默丢弃。
    attempts = container.confirmation_links.attempts()
    assert [(item.outcome.value, item.reason) for item in attempts] == [
        (ConfirmationLinkOutcome.REJECTED.value, ConfirmationLinkRejection.UNKNOWN.value)
    ]
    assert attempts[0].task_id is None
    assert attempts[0].token_fingerprint == token_fingerprint("not-a-real-link-token")
    assert attempt_reasons(container, task_id) == []


def test_card_cannot_be_landed_after_task_advanced() -> None:
    """任务已确认推进（或换版）后，旧卡指向的确认点不存在了。"""

    container = make_container()
    provision(container)
    session = container.signer.issue()
    subject = container.signer.verify(session).subject
    citizen = make_client(container, session)
    task_id = waiting_task(container, subject)
    ticket = issue_card(make_client(container), task_id)

    citizen.post(f"/api/tasks/{task_id}/confirm", json={"step_id": WAITING_STEP, "accepted": True})
    response = citizen.post(
        f"/api/tasks/{task_id}/confirmation-link", json={"token": ticket["token"]}
    )
    assert response.status_code == 409
    assert ConfirmationLinkRejection.TASK_NOT_CONFIRMABLE.value in attempt_reasons(
        container, task_id
    )


# ---------- 发卡侧的授权与前置状态 ----------


def test_issue_requires_handle_action() -> None:
    container = make_container()
    provision(container, actions=frozenset({StaffAction.INTAKE, StaffAction.REVIEW}))
    session = container.signer.issue()
    task_id = waiting_task(container, container.signer.verify(session).subject)

    response = make_client(container).post(
        f"/api/staff/tasks/{task_id}/confirmation-links", headers=STAFF_HEADER
    )
    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "staff_forbidden"
    assert container.confirmation_links.attempts(task_id=task_id) == []


def test_issue_requires_staff_token() -> None:
    container = make_container()
    provision(container)
    session = container.signer.issue()
    task_id = waiting_task(container, container.signer.verify(session).subject)

    assert (
        make_client(container).post(f"/api/staff/tasks/{task_id}/confirmation-links").status_code
        == 401
    )


def test_issue_rejects_task_not_waiting_for_confirmation() -> None:
    container = make_container()
    provision(container)
    session = container.signer.issue()
    subject = container.signer.verify(session).subject
    pending = container.tasks.create_task(
        TaskCreateRequest(
            conversation_id="conversation-1",
            input_text="我要办理测试事项",
            flow_id=FLOW_ID,
        ),
        subject,
    )
    assert pending.status is TaskStatus.WAITING_FOR_USER

    response = make_client(container).post(
        f"/api/staff/tasks/{pending.task_id}/confirmation-links", headers=STAFF_HEADER
    )
    assert response.status_code == 409
    assert container.confirmations.latest(pending.task_id, WAITING_STEP) is None


def test_issue_unknown_task_returns_404() -> None:
    container = make_container()
    provision(container)
    response = make_client(container).post(
        f"/api/staff/tasks/{uuid4()}/confirmation-links", headers=STAFF_HEADER
    )
    assert response.status_code == 404
    assert response.json()["code"] == "task_not_found"


# ---------- 留痕 ----------


def test_attempts_trail_records_lifecycle_without_raw_token() -> None:
    container = make_container()
    provision(container)
    session = container.signer.issue()
    subject = container.signer.verify(session).subject
    citizen = make_client(container, session)
    task_id = waiting_task(container, subject)
    ticket = issue_card(make_client(container), task_id)
    citizen.post(f"/api/tasks/{task_id}/confirmation-link", json={"token": ticket["token"]})

    response = make_client(container).get(
        f"/api/staff/tasks/{task_id}/confirmation-links/attempts", headers=STAFF_HEADER
    )
    assert response.status_code == 200
    attempts = response.json()
    assert [item["outcome"] for item in attempts] == ["redeemed", "issued"]
    assert attempts[0]["step_id"] == WAITING_STEP
    assert attempts[1]["actor"] == "staff01"
    # 留痕只存指纹：原文绝不落库，也不出现在响应里。
    assert attempts[0]["token_fingerprint"] == token_fingerprint(ticket["token"])
    assert ticket["token"] not in response.text


def test_attempts_requires_some_grant_on_the_flow() -> None:
    container = make_container()
    provision(container, actions=frozenset())
    session = container.signer.issue()
    task_id = waiting_task(container, container.signer.verify(session).subject)

    response = make_client(container).get(
        f"/api/staff/tasks/{task_id}/confirmation-links/attempts", headers=STAFF_HEADER
    )
    assert response.status_code == 403


# ---------- 地址与配置契约 ----------


def test_ticket_url_uses_configured_base() -> None:
    relative = make_container()
    provision(relative)
    session = relative.signer.issue()
    task_id = waiting_task(relative, relative.signer.verify(session).subject)
    url = issue_card(make_client(relative), task_id)["url"]
    assert url.startswith(f"{CONFIRM_PAGE_PATH}?{TASK_PARAM}=")
    assert f"&{TOKEN_PARAM}=" in url

    absolute = make_container(
        link_settings(confirmation_link_base_url="https://wpa.example.gov.cn/")
    )
    provision(absolute)
    session = absolute.signer.issue()
    task_id = waiting_task(absolute, absolute.signer.verify(session).subject)
    assert issue_card(make_client(absolute), task_id)["url"].startswith(
        f"https://wpa.example.gov.cn{CONFIRM_PAGE_PATH}?"
    )


def test_settings_reject_unsafe_link_configuration() -> None:
    # 漏了 scheme：卡片会指向一个打不开的地址，而排障会先从"深链过期了吗"查起。
    with pytest.raises(ValueError):
        link_settings(confirmation_link_base_url="wpa.example.gov.cn").confirmation_link_base
    with pytest.raises(ValueError):
        link_settings(
            confirmation_link_base_url="https://wpa.example.gov.cn/confirm"
        ).confirmation_link_base
    for ttl in (0, -1, MAX_TTL_SECONDS + 1):
        with pytest.raises(ValueError):
            link_settings(confirmation_link_ttl_seconds=ttl)


def test_link_ttl_must_be_short() -> None:
    with pytest.raises(ValueError):
        ConfirmationLink.issue(
            task_id=uuid4(),
            step_id=WAITING_STEP,
            flow_version="1",
            issued_by="staff01",
            ttl_seconds=MAX_TTL_SECONDS + 1,
        )


# ---------- 存储：一次性、并发与持久化 ----------


@pytest.mark.parametrize("backend", ["memory", "sqlite"])
def test_concurrent_redeem_has_exactly_one_winner(backend: str, workdir: Path) -> None:
    settings = link_settings()
    if backend == "sqlite":
        settings = link_settings(store_backend="sqlite", sqlite_path=workdir / "workpanda.db")
    container = make_container(settings)
    provision(container)
    session = container.signer.issue()
    subject = container.signer.verify(session).subject
    task_id = waiting_task(container, subject)
    ticket = issue_card(make_client(container), task_id)

    principal = Principal(subject=subject, kind=PrincipalKind.CITIZEN)
    outcomes: list[str] = []
    lock = threading.Lock()
    barrier = threading.Barrier(8)

    def redeem() -> None:
        barrier.wait()
        try:
            container.confirmation_links.redeem(task_id, ticket["token"], principal)
            result = "ok"
        except ConfirmationLinkRejected as exc:
            result = exc.reason.value
        with lock:
            outcomes.append(result)

    threads = [threading.Thread(target=redeem) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert outcomes.count("ok") == 1, outcomes
    assert outcomes.count(ConfirmationLinkRejection.ALREADY_USED.value) == 7, outcomes


def test_sqlite_store_survives_rebuild(workdir: Path) -> None:
    path = workdir / "workpanda.db"
    settings = link_settings(store_backend="sqlite", sqlite_path=path)

    first = make_container(settings)
    try:
        provision(first)
        session = first.signer.issue()
        subject = first.signer.verify(session).subject
        task_id = waiting_task(first, subject)
        ticket = issue_card(make_client(first), task_id)
    finally:
        first.close()

    second = make_container(settings)
    try:
        # 客户端的会话在重启后失效，但"这张卡发出去过"这个事实必须还在。
        attempts = second.confirmation_links.attempts(task_id=task_id)
        assert [item.outcome for item in attempts] == [ConfirmationLinkOutcome.ISSUED]
        assert attempts[0].token_fingerprint == token_fingerprint(ticket["token"])

        refreshed = second.store.get(task_id)
        owner = Principal(subject=refreshed.user_ref or "", kind=PrincipalKind.CITIZEN)
        landing = second.confirmation_links.redeem(task_id, ticket["token"], owner)
        assert landing.step_id == WAITING_STEP
    finally:
        second.close()


def test_memory_store_returns_copies_and_orders_attempts_newest_first() -> None:
    store = InMemoryConfirmationLinkStore()
    link = ConfirmationLink.issue(
        task_id=uuid4(),
        step_id=WAITING_STEP,
        flow_version="1",
        issued_by="staff01",
        ttl_seconds=60,
    )
    store.issue(link)

    leaked = store.get(link.token)
    assert leaked is not None
    leaked.consumed_at = utc_now()
    stored = store.get(link.token)
    assert stored is not None
    assert stored.consumed_at is None  # 取出来的必须是副本，改不动库里那份

    for outcome in (
        ConfirmationLinkOutcome.ISSUED,
        ConfirmationLinkOutcome.REJECTED,
        ConfirmationLinkOutcome.REDEEMED,
    ):
        store.record_attempt(_attempt(outcome))
    assert [item.outcome for item in store.list_attempts()] == [
        ConfirmationLinkOutcome.REDEEMED,
        ConfirmationLinkOutcome.REJECTED,
        ConfirmationLinkOutcome.ISSUED,
    ]
    assert store.list_attempts(limit=1) == store.list_attempts()[:1]
    assert store.list_attempts(limit=0) == []


def test_sqlite_store_orders_and_filters_attempts(workdir: Path) -> None:
    store = SqliteConfirmationLinkStore(workdir / "links.db")
    try:
        kept = uuid4()
        store.record_attempt(_attempt(ConfirmationLinkOutcome.ISSUED, task_id=kept))
        store.record_attempt(_attempt(ConfirmationLinkOutcome.REJECTED, task_id=uuid4()))
        store.record_attempt(_attempt(ConfirmationLinkOutcome.REDEEMED, task_id=kept))

        assert [item.outcome for item in store.list_attempts(task_id=kept)] == [
            ConfirmationLinkOutcome.REDEEMED,
            ConfirmationLinkOutcome.ISSUED,
        ]
        assert len(store.list_attempts()) == 3
        assert store.list_attempts(limit=2)[0].outcome is ConfirmationLinkOutcome.REDEEMED
    finally:
        store.close()


def _attempt(
    outcome: ConfirmationLinkOutcome, task_id: UUID | None = None
) -> ConfirmationLinkAttempt:
    return ConfirmationLinkAttempt(
        outcome=outcome,
        reason="",
        task_id=task_id,
        step_id=WAITING_STEP,
        actor="citizen-1",
        actor_kind="citizen",
        token_fingerprint=token_fingerprint("whatever"),
    )
