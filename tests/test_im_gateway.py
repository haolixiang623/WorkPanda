"""企业微信 IM 网关：验签、幂等、卡片内容边界与通知 fail-open。

这些用例守的是**安全与可靠性属性**，不是接口形状：

- 验签失败一律拒绝，且**不解析请求体**（未验签的输入不许进入解析器与留痕表）；
- 重投同一 message id 只处理一次（内存与 sqlite 后端各一条）；
- 同一 nonce 第二次请求被拒（明文模式签名不覆盖 body 的补强）；
- 卡片不含服务对象字段原文（构造入口只取标量）；
- 通知失败不影响任务推进（集成：投递器永远失败，任务照常到终态）；
- 网关不持有任何写业务状态的能力（构造参数 + 源码双层审计）。
"""

from __future__ import annotations

import ast
import inspect
import time
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tests.support import TEST_FLOWS
from workpanda.api.im import CHANNEL
from workpanda.application.im_gateway import ImInboundService, TaskEventNotifier
from workpanda.bootstrap import Container, build_container
from workpanda.config import Settings
from workpanda.domain.im_gateway import (
    ImInboundMessage,
    ImInboundOutcome,
    ImMessageKind,
    ImSignatureInvalid,
    ReplayGuard,
    TaskCard,
    build_task_card,
    plaintext_signature,
    verify_callback_signature,
)
from workpanda.domain.models import EventType, Task, TaskStatus
from workpanda.domain.staff import (
    ImIdentity,
    ImIdentityRejected,
    ImRejectionReason,
    StaffAction,
    StaffGrant,
    StaffMember,
)
from workpanda.infrastructure.adapters import KeywordIntentRouter, ScriptedExecutor
from workpanda.infrastructure.im_message_store import (
    InMemoryImMessageStore,
    SqliteImMessageStore,
)
from workpanda.infrastructure.wecom_gateway import (
    MAX_CALLBACK_BYTES,
    NotificationFailed,
    NotificationQueue,
    NotifyingEventPublisher,
    WeComAccessTokenProvider,
    WeComCardSender,
    parse_callback_xml,
    validate_im_gateway,
)
from workpanda.main import create_app

TOKEN = "wecom-callback-token-0123456789"
DEEP_LINK = "https://service.example.cn"
IM_USER = "zhangsan"
SUBJECT = "staff01"

SRC_ROOT = Path(__file__).resolve().parents[1] / "src" / "workpanda"


# ---------- 通用构造 ----------


def im_settings(**overrides) -> Settings:
    base = {
        "store_backend": "memory",
        "enable_demo_flow": False,
        "im_gateway_enabled": True,
        "wecom_callback_token": TOKEN,
    }
    base.update(overrides)
    return Settings(**base)


def make_container(settings: Settings | None = None) -> Container:
    return build_container(
        settings or im_settings(),
        flows=TEST_FLOWS,
        executor=ScriptedExecutor(),
        router=KeywordIntentRouter(),
    )


def make_client(container: Container) -> TestClient:
    return TestClient(create_app(container=container))


def provision(container: Container, *, enabled: bool = True) -> StaffMember:
    container.staff_store.save_member(
        StaffMember(
            subject=SUBJECT,
            display_name="张三",
            enabled=enabled,
            grants=(StaffGrant(flow_id="test.confirm", actions=frozenset(StaffAction)),),
        )
    )
    container.staff_store.save_identity(
        ImIdentity(channel=CHANNEL, im_user_id=IM_USER, principal_subject=SUBJECT)
    )
    return container.staff_store.get_member(SUBJECT)


def signed_params(nonce: str = "n1", *, at: float | None = None) -> dict[str, str]:
    timestamp = str(int(at if at is not None else time.time()))
    return {
        "signature": plaintext_signature(TOKEN, timestamp, nonce),
        "timestamp": timestamp,
        "nonce": nonce,
    }


def text_body(
    message_id: str = "1001", *, user: str = IM_USER, content: str = "你好"
) -> bytes:
    return (
        "<xml>"
        "<ToUserName><![CDATA[wwcorp]]></ToUserName>"
        f"<FromUserName><![CDATA[{user}]]></FromUserName>"
        f"<CreateTime>{int(time.time())}</CreateTime>"
        "<MsgType><![CDATA[text]]></MsgType>"
        f"<Content><![CDATA[{content}]]></Content>"
        f"<MsgId>{message_id}</MsgId>"
        "</xml>"
    ).encode()


def post_callback(
    client: TestClient, body: bytes, params: dict[str, str]
):
    return client.post(
        "/api/im/wecom/callback",
        params=params,
        content=body,
        headers={"Content-Type": "text/xml"},
    )


def new_task(container: Container, **overrides) -> Task:
    payload = {"conversation_id": "c1", "flow_id": "test.confirm", "flow_version": "1"}
    payload.update(overrides)
    return container.store.create(Task(**payload))


def make_notifier(container: Container, sender) -> TaskEventNotifier:
    return TaskEventNotifier(
        store=container.store,
        staff=container.staff_store,
        sender=sender,
        flows=container.flows,
        deep_link_base=DEEP_LINK,
    )


class _RecordingSender:
    def __init__(self) -> None:
        self.cards: list[TaskCard] = []
        self.recipients: list[str] = []

    def send(self, im_user_id: str, card: TaskCard) -> None:
        self.recipients.append(im_user_id)
        self.cards.append(card)


class _BrokenSender:
    def send(self, im_user_id: str, card: TaskCard) -> None:
        raise NotificationFailed("wecom_errcode_40001")


class _AlwaysUnmapped:
    """解析永远失败的替身：用于不依赖工作人员目录的存储层用例。"""

    def resolve(self, channel: str, im_user_id: str):
        raise ImIdentityRejected(ImRejectionReason.UNMAPPED, channel, im_user_id)


def _wait_until(predicate, timeout: float = 5.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return
        time.sleep(0.05)
    raise AssertionError("condition not met in time")


# ---------- 未开启时不存在 ----------


def test_gateway_is_absent_when_disabled() -> None:
    container = make_container(im_settings(im_gateway_enabled=False))
    client = make_client(container)
    assert container.im_gateway is None
    assert client.get("/api/im/wecom/callback").status_code == 404
    assert client.post("/api/im/wecom/callback").status_code == 404


def test_enabling_without_token_fails_at_startup() -> None:
    with pytest.raises(ValueError, match="wecom_callback_token"):
        build_container(im_settings(wecom_callback_token=""))


def test_aes_key_configured_fails_at_startup() -> None:
    with pytest.raises(ValueError, match="encoding_aes_key"):
        build_container(im_settings(wecom_encoding_aes_key="some-43-char-key"))


def test_partial_outbound_config_fails_at_startup() -> None:
    with pytest.raises(ValueError, match="出站卡片配置不完整"):
        build_container(im_settings(wecom_corp_id="ww123"))


def test_validate_is_noop_when_disabled() -> None:
    validate_im_gateway(Settings(store_backend="memory", im_gateway_enabled=False))


# ---------- URL 验证 ----------


def test_url_verification_returns_echostr() -> None:
    client = make_client(make_container())
    timestamp = str(int(time.time()))
    echo = "echo-42"
    response = client.get(
        "/api/im/wecom/callback",
        params={
            "msg_signature": plaintext_signature(TOKEN, timestamp, "n1", echo),
            "timestamp": timestamp,
            "nonce": "n1",
            "echostr": echo,
        },
    )
    assert response.status_code == 200
    assert response.text == echo


def test_url_verification_rejects_bad_signature() -> None:
    client = make_client(make_container())
    response = client.get(
        "/api/im/wecom/callback",
        params={
            "msg_signature": "deadbeef",
            "timestamp": str(int(time.time())),
            "nonce": "n1",
            "echostr": "echo-42",
        },
    )
    assert response.status_code == 403


# ---------- 验签是硬门禁 ----------


def test_bad_signature_is_rejected_without_parsing_the_body() -> None:
    container = make_container()
    provision(container)
    client = make_client(container)

    response = post_callback(
        client,
        text_body("2001"),
        {"signature": "0" * 40, "timestamp": str(int(time.time())), "nonce": "n9"},
    )

    assert response.status_code == 403
    # 关键断言：连一条入站记录都没有——未验签的 body 从未进入业务层。
    assert container.im_messages is not None
    assert container.im_messages.count() == 0
    assert container.staff_store.list_access() == []


@pytest.mark.parametrize(
    "params",
    [
        {"signature": "x", "timestamp": "abc", "nonce": "n1"},
        {"signature": "x", "timestamp": str(int(time.time()))},
    ],
)
def test_missing_or_malformed_signature_params_are_rejected(params: dict) -> None:
    client = make_client(make_container())
    assert post_callback(client, text_body("3001"), params).status_code == 403


def test_stale_timestamp_is_rejected() -> None:
    client = make_client(make_container())
    response = post_callback(
        client, text_body("4001"), signed_params("n-old", at=time.time() - 3600)
    )
    assert response.status_code == 403


def test_replayed_nonce_is_rejected() -> None:
    container = make_container()
    provision(container)
    client = make_client(container)
    params = signed_params("same-nonce")

    first = post_callback(client, text_body("5001"), params)
    second = post_callback(client, text_body("5002"), params)

    assert first.status_code == 200
    assert second.status_code == 403


def test_dtd_payload_is_rejected_without_recording_anything() -> None:
    container = make_container()
    client = make_client(container)
    body = (
        b'<?xml version="1.0"?><!DOCTYPE xml [<!ENTITY a "boom">]>'
        b"<xml><FromUserName>&a;</FromUserName><MsgType>text</MsgType>"
        b"<MsgId>6001</MsgId></xml>"
    )
    response = post_callback(client, body, signed_params("n-dtd"))

    assert response.status_code == 403
    assert container.im_messages is not None
    assert container.im_messages.count() == 0


def test_non_xml_payload_is_rejected() -> None:
    client = make_client(make_container())
    assert post_callback(client, b"not xml at all", signed_params("n-raw")).status_code == 403


def test_oversized_payload_is_rejected() -> None:
    client = make_client(make_container())
    body = b"<xml>" + b"a" * (MAX_CALLBACK_BYTES + 1) + b"</xml>"
    assert post_callback(client, body, signed_params("n-big")).status_code == 403


# ---------- 幂等 ----------


def test_duplicate_message_id_is_processed_only_once() -> None:
    container = make_container()
    provision(container)
    client = make_client(container)
    body = text_body("7001")

    for index in range(2):
        response = post_callback(client, body, signed_params(f"nonce-{index}"))
        assert response.status_code == 200

    assert container.im_messages is not None
    assert container.im_messages.count() == 1
    record = container.im_messages.get(CHANNEL, "7001")
    assert record is not None
    assert record.outcome is ImInboundOutcome.ACCEPTED
    # 身份解析只跑了一次：重投没有重复写授权留痕。
    granted = [
        entry for entry in container.staff_store.list_access() if entry.outcome.value == "granted"
    ]
    assert len(granted) == 1


def test_duplicate_detection_is_atomic_in_sqlite_backend(workdir: Path) -> None:
    store = SqliteImMessageStore(workdir / "im.db")
    try:
        message = ImInboundMessage(
            channel=CHANNEL, message_id="8001", im_user_id=IM_USER, kind=ImMessageKind.TEXT
        )
        service = ImInboundService(resolver=_AlwaysUnmapped(), messages=store)

        first = service.handle(message)
        second = service.handle(message)

        assert first.outcome is ImInboundOutcome.REJECTED
        assert second.outcome is ImInboundOutcome.DUPLICATE
        assert store.count() == 1
    finally:
        store.close()


def test_duplicate_is_visible_across_store_instances(workdir: Path) -> None:
    """两个连接指向同一个库：判重由数据库裁决，不依赖进程内状态。"""

    path = workdir / "im-shared.db"
    first_store = SqliteImMessageStore(path)
    second_store = SqliteImMessageStore(path)
    try:
        message = ImInboundMessage(
            channel=CHANNEL, message_id="8100", im_user_id=IM_USER, kind=ImMessageKind.TEXT
        )
        service_a = ImInboundService(resolver=_AlwaysUnmapped(), messages=first_store)
        service_b = ImInboundService(resolver=_AlwaysUnmapped(), messages=second_store)

        assert service_a.handle(message).outcome is ImInboundOutcome.REJECTED
        assert service_b.handle(message).outcome is ImInboundOutcome.DUPLICATE
    finally:
        first_store.close()
        second_store.close()


def test_in_memory_store_returns_copies() -> None:
    store = InMemoryImMessageStore()
    record = ImInboundMessage(
        channel=CHANNEL, message_id="8200", im_user_id=IM_USER, kind=ImMessageKind.TEXT
    )
    from workpanda.domain.im_gateway import ImInboundRecord

    assert store.claim(ImInboundRecord(**record.model_dump())) is True
    fetched = store.get(CHANNEL, "8200")
    assert fetched is not None
    fetched.subject = "tampered"
    assert store.get(CHANNEL, "8200").subject == ""


# ---------- 未映射用户 ----------


def test_unmapped_user_is_recorded_but_answered_success() -> None:
    container = make_container()
    client = make_client(container)

    response = post_callback(
        client, text_body("9001", user="outsider"), signed_params("n-outsider")
    )

    # 回 200 是有意的：403 会让企业微信持续重投，把一次配置问题放大成重投风暴。
    assert response.status_code == 200
    assert container.im_messages is not None
    record = container.im_messages.get(CHANNEL, "9001")
    assert record is not None
    assert record.outcome is ImInboundOutcome.REJECTED
    assert record.reason == "im_identity_unmapped"
    # 拒绝记录里能查到 IM 标识，事后能回答"是谁被拦了"。
    rejected = [
        entry for entry in container.staff_store.list_access() if entry.outcome.value == "rejected"
    ]
    assert [entry.im_user_id for entry in rejected] == ["outsider"]


def test_disabled_identity_reason_is_recorded() -> None:
    container = make_container()
    provision(container)
    identity = container.staff_store.resolve_identity(CHANNEL, IM_USER)
    assert identity is not None
    container.staff_store.save_identity(identity.model_copy(update={"enabled": False}))

    assert container.im_gateway is not None
    record = container.im_gateway.handle(
        ImInboundMessage(
            channel=CHANNEL, message_id="9100", im_user_id=IM_USER, kind=ImMessageKind.TEXT
        )
    )
    assert record.outcome is ImInboundOutcome.REJECTED
    assert record.reason == "im_identity_disabled"


def test_event_message_without_msg_id_gets_a_synthetic_id() -> None:
    body = (
        b"<xml>"
        b"<FromUserName><![CDATA[zhangsan]]></FromUserName>"
        b"<CreateTime>1700000000</CreateTime>"
        b"<MsgType><![CDATA[event]]></MsgType>"
        b"<Event><![CDATA[subscribe]]></Event>"
        b"</xml>"
    )
    message = parse_callback_xml(body, channel=CHANNEL)
    assert message.kind is ImMessageKind.EVENT
    assert message.message_id == "zhangsan:1700000000:subscribe"


# ---------- 卡片内容边界 ----------


def test_card_contains_only_status_and_deep_link() -> None:
    task_id = uuid.uuid4()
    card = build_task_card(
        task_id=task_id,
        flow_id="test.confirm",
        flow_title="需要确认的测试流程",
        status=TaskStatus.WAITING_FOR_CONFIRMATION,
        deep_link_base=DEEP_LINK,
    )
    assert set(card.model_dump()) == {"task_id", "flow_id", "title", "description", "url"}
    assert card.url == f"{DEEP_LINK}/staff/tasks/{task_id}"
    assert "等待您确认" in card.title


def test_card_builder_takes_scalars_not_a_task() -> None:
    """构造入口不接收 `Task`：槽位没有进入卡片的通路（本模块的核心约束）。"""

    parameters = inspect.signature(build_task_card).parameters
    assert set(parameters) == {"task_id", "flow_id", "flow_title", "status", "deep_link_base"}
    # 每个入参都必须是标量：一旦有人把 `task: Task` 加进来，这条断言就失败。
    assert all(parameter.annotation is not Task for parameter in parameters.values())


def test_card_never_carries_slot_values() -> None:
    """端到端：任务带着敏感槽位，卡片里不允许出现这些取值。"""

    container = make_container()
    provision(container)
    sender = _RecordingSender()
    notifier = make_notifier(container, sender)
    marker = "13800001111"
    task = new_task(container, slots={"contact": marker, "subject": "张三的身份证"})
    event = container.store.append_event(
        task.task_id, EventType.CONFIRMATION_REQUIRED, TaskStatus.WAITING_FOR_CONFIRMATION
    )

    deliveries = notifier.handle(task.task_id, event.sequence)

    assert [delivery.ok for delivery in deliveries] == [True]
    rendered = sender.cards[0].render()
    assert marker not in rendered
    assert "张三的身份证" not in rendered
    assert "slots" not in rendered


@pytest.mark.parametrize(
    "base",
    [
        "",
        "service.example.cn",
        "https://user:pass@service.example.cn",
        "https://service.example.cn/portal",
    ],
)
def test_deep_link_base_rejects_bad_values(base: str) -> None:
    with pytest.raises(ValueError):
        build_task_card(
            task_id=uuid.uuid4(),
            flow_id="test.confirm",
            flow_title="x",
            status=TaskStatus.COMPLETED,
            deep_link_base=base,
        )


def test_only_selected_events_produce_cards() -> None:
    container = make_container()
    provision(container)
    sender = _RecordingSender()
    notifier = make_notifier(container, sender)
    task = new_task(container)
    silent = container.store.append_event(task.task_id, EventType.STEP_STARTED, TaskStatus.RUNNING)

    assert notifier.handle(task.task_id, silent.sequence) == []
    assert sender.cards == []


def test_cards_go_only_to_staff_granted_on_the_flow() -> None:
    container = make_container()
    provision(container)
    # 另一个人：配了 IM 映射，但对这条流程没有任何授权。
    container.staff_store.save_member(StaffMember(subject="staff02", display_name="李四"))
    container.staff_store.save_identity(
        ImIdentity(channel=CHANNEL, im_user_id="lisi", principal_subject="staff02")
    )
    sender = _RecordingSender()
    notifier = make_notifier(container, sender)
    task = new_task(container)
    event = container.store.append_event(task.task_id, EventType.COMPLETED, TaskStatus.COMPLETED)

    deliveries = notifier.handle(task.task_id, event.sequence)

    assert [delivery.subject for delivery in deliveries] == [SUBJECT]
    assert sender.recipients == [IM_USER]


def test_cards_are_skipped_when_deep_link_is_not_configured() -> None:
    container = make_container()
    provision(container)
    sender = _RecordingSender()
    notifier = TaskEventNotifier(
        store=container.store, staff=container.staff_store, sender=sender, deep_link_base=""
    )
    task = new_task(container)
    event = container.store.append_event(task.task_id, EventType.COMPLETED, TaskStatus.COMPLETED)

    assert notifier.enabled is False
    assert notifier.handle(task.task_id, event.sequence) == []
    assert sender.cards == []


# ---------- 通知失败不影响任务推进 ----------


def test_delivery_failure_is_reported_but_not_raised() -> None:
    container = make_container()
    provision(container)
    notifier = make_notifier(container, _BrokenSender())
    task = new_task(container)
    event = container.store.append_event(task.task_id, EventType.COMPLETED, TaskStatus.COMPLETED)

    deliveries = notifier.handle(task.task_id, event.sequence)

    assert [delivery.ok for delivery in deliveries] == [False]
    # 只留异常类型名：异常消息里可能带对方返回体或含 token 的 URL。
    assert deliveries[0].error == "NotificationFailed"


def test_notification_failure_does_not_block_task_completion(monkeypatch) -> None:
    """集成：投递器永远失败，任务照常走完。"""

    import workpanda.bootstrap as bootstrap_module

    monkeypatch.setattr(
        bootstrap_module, "build_transport", lambda settings: (_FakeTransport(), [])
    )
    monkeypatch.setattr(
        bootstrap_module, "build_card_sender", lambda settings, transport: _BrokenSender()
    )

    container = build_container(
        im_settings(
            wecom_corp_id="ww123",
            wecom_agent_id="1000002",
            wecom_app_secret="secret-0123456789",
            notification_deep_link_base=DEEP_LINK,
        ),
        flows=TEST_FLOWS,
        executor=ScriptedExecutor(),
        router=KeywordIntentRouter(),
    )
    try:
        provision(container)
        client = make_client(container)
        headers = {"X-WorkPanda-Session": container.signer.issue()}

        created = client.post(
            "/api/tasks",
            json={
                "conversation_id": "c1",
                "input_text": "我要办理测试事项",
                "flow_id": "test.confirm",
                "slots": {"subject": "x", "contact": "y"},
            },
            headers=headers,
        )
        assert created.status_code == 201, created.text
        assert created.json()["status"] == "waiting_for_confirmation"

        confirmed = client.post(
            f"/api/tasks/{created.json()['task_id']}/confirm",
            json={"step_id": "submit", "accepted": True},
            headers=headers,
        )
        assert confirmed.status_code == 200, confirmed.text
        assert confirmed.json()["status"] == "completed"

        # 投递失败被计数，但任务已经是 completed —— 通知没有反过来影响流程。
        assert container.notification_counters is not None
        _wait_until(
            lambda: container.notification_counters.snapshot()["failed"] >= 1
        )
        assert container.notifications is not None
        assert container.notifications.stats.snapshot()["failed"] >= 1
    finally:
        container.close()


def test_queue_drops_instead_of_blocking_when_full() -> None:
    queue = NotificationQueue(_NeverReturns(), max_pending=2)
    try:
        accepted = sum(
            1 for index in range(20) if queue.submit(uuid.uuid4(), index)
        )
        # 工作线程会取走一个，队列里最多再排 2 个；其余全部丢弃且不阻塞。
        assert accepted <= 3
        assert queue.stats.snapshot()["dropped"] >= 1
    finally:
        queue.close(timeout=0.2)


def test_notifying_publisher_swallows_notification_errors() -> None:
    class _ExplodingQueue:
        def submit(self, task_id, sequence):
            raise RuntimeError("queue is on fire")

    class _RecordingBroker:
        def __init__(self) -> None:
            self.published: list[tuple] = []

        def publish(self, task_id, sequence) -> None:
            self.published.append((task_id, sequence))

    broker = _RecordingBroker()
    NotifyingEventPublisher(broker, _ExplodingQueue()).publish(uuid.uuid4(), 7)

    assert len(broker.published) == 1


def test_publisher_pushes_events_before_enqueueing_cards() -> None:
    order: list[str] = []

    class _Broker:
        def publish(self, task_id, sequence) -> None:
            order.append("broker")

    class _Queue:
        def submit(self, task_id, sequence) -> bool:
            order.append("queue")
            return True

    NotifyingEventPublisher(_Broker(), _Queue()).publish(uuid.uuid4(), 1)
    assert order == ["broker", "queue"]


# ---------- 出站适配器 ----------


class _FakeTransport:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []

    def get(self, url: str) -> dict:
        self.calls.append(("GET", url))
        return {"errcode": 0, "access_token": "token-abc", "expires_in": 7200}

    def post(self, url: str, payload: dict) -> dict:
        self.calls.append(("POST", payload))
        return {"errcode": 0, "errmsg": "ok"}


def _card(status: TaskStatus = TaskStatus.HANDOFF_REQUIRED) -> TaskCard:
    return build_task_card(
        task_id=uuid.uuid4(),
        flow_id="test.confirm",
        flow_title="需要确认的测试流程",
        status=status,
        deep_link_base=DEEP_LINK,
    )


def test_card_sender_posts_textcard_once() -> None:
    transport = _FakeTransport()
    sender = WeComCardSender(
        transport=transport,
        tokens=WeComAccessTokenProvider(
            transport=transport, corp_id="ww123", app_secret="secret"
        ),
        agent_id="1000002",
    )

    card = _card()
    sender.send(IM_USER, card)

    posts = [call for call in transport.calls if call[0] == "POST"]
    assert len(posts) == 1
    payload = posts[0][1]
    assert payload["touser"] == IM_USER
    assert payload["textcard"]["url"] == card.url
    assert payload["agentid"] == 1000002


def test_access_token_is_cached_between_sends() -> None:
    transport = _FakeTransport()
    tokens = WeComAccessTokenProvider(
        transport=transport, corp_id="ww123", app_secret="secret"
    )
    assert tokens.get() == "token-abc"
    assert tokens.get() == "token-abc"
    assert [call[0] for call in transport.calls] == ["GET"]


class _StubResponse:
    def __init__(self, status_code: int, payload: object) -> None:
        self.status_code = status_code
        self._payload = payload

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


@pytest.mark.parametrize(
    ("status_code", "payload", "expected"),
    [
        (200, {"errcode": 40001, "errmsg": "invalid credential"}, "wecom_errcode_40001"),
        (500, {"errcode": 0}, "http_status_500"),
        (200, ValueError("not json"), "response_not_json"),
    ],
)
def test_transport_decodes_failures_into_stable_codes(
    status_code: int, payload: object, expected: str
) -> None:
    """对外只留稳定错误码：对方返回体、内网地址、token 片段都不外传。"""

    from workpanda.infrastructure.wecom_gateway import HttpxTransport

    with pytest.raises(NotificationFailed, match=expected):
        HttpxTransport._decode(_StubResponse(status_code, payload))


def test_token_request_failure_is_wrapped() -> None:
    class _FailingTransport(_FakeTransport):
        def get(self, url: str) -> dict:
            raise NotificationFailed("http_status_500")

    tokens = WeComAccessTokenProvider(
        transport=_FailingTransport(), corp_id="ww123", app_secret="secret"
    )
    with pytest.raises(NotificationFailed):
        tokens.get()


# ---------- 能力边界：网关不能写业务状态 ----------


def test_inbound_service_has_no_business_write_dependency() -> None:
    parameters = set(inspect.signature(ImInboundService.__init__).parameters)
    assert parameters == {"self", "resolver", "messages", "channel"}
    assert not {"store", "tasks", "confirmations"} & parameters


def test_gateway_source_cannot_touch_business_writes() -> None:
    """源码审计：网关的模块不许导入或调用写任务状态的入口。

    用 AST 而不是字符串匹配：docstring 里解释"为什么不写状态"不该被误判，
    而真正的 import 与调用能精确取到。
    """

    forbidden_modules = {
        "workpanda.application.task_service",
        "workpanda.application.runner",
    }
    forbidden_call_names = {
        "commit",
        "create_task",
        "answer",
        "confirm",
        "cancel",
        "issue_confirmation",
        "append_event",
    }
    forbidden_names = {"TaskService", "FlowRunner", "ConfirmationStore"}

    for relative in (
        "application/im_gateway.py",
        "api/im.py",
        "infrastructure/wecom_gateway.py",
    ):
        tree = ast.parse((SRC_ROOT / relative).read_text(encoding="utf-8"))
        imported: set[str] = set()
        called: set[str] = set()
        referenced: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                imported.add(node.module or "")
            elif isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.Call):
                func = node.func
                if isinstance(func, ast.Name):
                    called.add(func.id)
                elif isinstance(func, ast.Attribute):
                    called.add(func.attr)
            elif isinstance(node, ast.Name):
                referenced.add(node.id)
            elif isinstance(node, ast.Attribute):
                referenced.add(node.attr)

        assert not imported & forbidden_modules, relative
        assert not called & forbidden_call_names, relative
        assert not referenced & forbidden_names, relative

    # 通知器确实要读任务与事件，但只允许读：把 `self._store` 上的调用逐个列出来，
    # 出现 `commit` / `append_event` 之类的写入口就直接失败。
    app_tree = ast.parse(
        (SRC_ROOT / "application/im_gateway.py").read_text(encoding="utf-8")
    )
    store_calls = {
        node.func.attr
        for node in ast.walk(app_tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Attribute)
        and node.func.value.attr == "_store"
    }
    assert store_calls <= {"events", "get"}, store_calls


def test_replay_guard_rejects_reuse_within_capacity() -> None:
    guard = ReplayGuard(capacity=8)
    assert guard.accept("a") is True
    assert guard.accept("a") is False
    assert guard.accept("b") is True
    assert guard.accept("") is False


def test_verify_requires_configured_token() -> None:
    with pytest.raises(ImSignatureInvalid):
        verify_callback_signature(token="", signature="x", timestamp="1", nonce="n")


class _NeverReturns:
    """永不返回的处理器：用来把队列灌满。"""

    def __call__(self, task_id, sequence) -> None:
        time.sleep(5)
