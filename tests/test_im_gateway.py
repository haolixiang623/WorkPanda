"""企业微信网关（Issue #64 / 提案 N4）的边界回归。

这份用例守的是**网关的四条边界**，不是接口形状：

1. **验签前置**：验签失败的请求不进入业务层——不写去重表、不解析身份、不发卡片；
2. **幂等**：同一条 message id 只处理一次（含并发重投，memory 与 sqlite 各一条）；
3. **出站不反噬**：通知失败只留痕，任务照常推进到终态；
4. **卡片无正文**：只有任务号、状态与深链，槽位原文一个字都不出现。

外加一条结构断言：网关只拿只读任务视图，**没有**改动任务状态的入口。
"""

from __future__ import annotations

import ast
import inspect
import threading
from collections.abc import Callable, Iterator
from pathlib import Path
from threading import Lock
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from tests.support import TEST_FLOWS
from tests.wecom_support import (
    CALLBACK_TOKEN,
    CORP_ID,
    ENCODING_AES_KEY,
    callback_params,
    encrypt_message,
    message_body,
    sign,
)
from workpanda.api.im import router as im_router
from workpanda.application.im_gateway import ImGatewayService, parse_task_command
from workpanda.bootstrap import Container, build_container
from workpanda.config import Settings
from workpanda.domain.im_gateway import STATUS_LABELS, CardOutcome, InboundOutcome, TaskCard
from workpanda.domain.models import TaskConfirmRequest, TaskCreateRequest, TaskStatus
from workpanda.domain.staff import (
    STAFF_ROLE,
    ImIdentity,
    StaffAction,
    StaffGrant,
    StaffMember,
)
from workpanda.infrastructure.adapters import ScriptedExecutor
from workpanda.infrastructure.im_gateway_store import (
    InMemoryImGatewayStore,
    SqliteImGatewayStore,
)
from workpanda.infrastructure.wecom import WeComConfigError
from workpanda.main import create_app

STAFF_SECRET = "staff-secret-0001-for-tests"
FLOW_ID = "test.confirm"
WAITING_STEP = "submit"
CONSOLE_BASE = "https://wpa.example.com"
IM_USER = "zhangsan"


# ---------- 夹具 ----------


class RecordingSender:
    """记录发出去的每一张卡。"""

    def __init__(self) -> None:
        self.sent: list[tuple[str, TaskCard]] = []
        self._lock = Lock()

    def send(self, im_user_id: str, card: TaskCard) -> None:
        with self._lock:
            self.sent.append((im_user_id, card))

    @property
    def cards(self) -> list[TaskCard]:
        with self._lock:
            return [card for _, card in self.sent]

    def clear(self) -> None:
        with self._lock:
            self.sent.clear()


class FailingSender:
    """永远发失败。用来证明"通知失败不影响任务推进"。"""

    def __init__(self) -> None:
        self.attempts = 0

    def send(self, im_user_id: str, card: TaskCard) -> None:
        self.attempts += 1
        raise RuntimeError("wecom is down")


def im_settings(**overrides) -> Settings:
    base = {
        "store_backend": "memory",
        "enable_demo_flow": False,
        "staff_tokens": f"staff01:{STAFF_ROLE}:{STAFF_SECRET}",
        "wecom_callback_token": CALLBACK_TOKEN,
        "wecom_encoding_aes_key": ENCODING_AES_KEY,
        "wecom_corp_id": CORP_ID,
        "wecom_corp_secret": "corp-secret-for-tests",
        "wecom_agent_id": 1000002,
        "staff_console_base_url": CONSOLE_BASE,
    }
    base.update(overrides)
    return Settings(**base)


def make_container(sender=None, **overrides) -> Container:
    return build_container(
        im_settings(**overrides),
        flows=TEST_FLOWS,
        executor=ScriptedExecutor(),
        im_sender=sender,
    )


@pytest.fixture
def build() -> Iterator[Callable[..., Container]]:
    created: list[Container] = []

    def _build(sender=None, **overrides) -> Container:
        container = make_container(sender, **overrides)
        created.append(container)
        return container

    yield _build
    for container in created:
        container.close()


def make_client(container: Container) -> TestClient:
    return TestClient(create_app(container=container))


def provision(
    container: Container,
    *,
    subject: str = "staff01",
    flow_id: str = FLOW_ID,
    actions: frozenset[StaffAction] = frozenset(StaffAction),
    im_user_id: str = IM_USER,
) -> StaffMember:
    member = container.staff_store.save_member(
        StaffMember(
            subject=subject,
            display_name="张三",
            grants=(StaffGrant(flow_id=flow_id, actions=actions),),
        )
    )
    # 走维护入口而不是直接写库：目标主体必须已存在这条规则在服务里。
    container.staff_directory.save_identity(
        ImIdentity(channel="wecom", im_user_id=im_user_id, principal_subject=subject)
    )
    return member


def create_task(
    container: Container,
    *,
    flow_id: str = FLOW_ID,
    slots: dict[str, str] | None = None,
) -> tuple[UUID, str]:
    """造一张任务，返回 (task_id, 会话主体)。

    等发卡线程把创建期的通知处理完再返回：调用方从"任务已存在、通知已送达"
    这个状态开始断言，不会把创建期那张卡算进来。
    """

    session = container.signer.issue()
    subject = container.signer.verify(session).subject
    task = container.tasks.create_task(
        TaskCreateRequest(
            conversation_id="conversation-1",
            input_text="我要办理测试事项",
            flow_id=flow_id,
            slots=slots if slots is not None else {"subject": "测试主题", "contact": "12345"},
        ),
        subject,
    )
    if container.im_cards is not None:
        container.im_cards.flush()
    return task.task_id, subject


def settle(container: Container, sender: RecordingSender) -> None:
    """排空队列并清空记录：接下来的动作才是我要观察的那一次。"""

    if container.im_cards is not None:
        container.im_cards.flush()
    sender.clear()


def post_callback(
    client: TestClient,
    body: bytes,
    *,
    params: dict[str, str] | None = None,
    signature: str | None = None,
):
    merged = params if params is not None else callback_params(body, signature=signature)
    return client.post("/api/im/wecom/callback", params=merged, content=body)


# ---------- 1. 验签前置 ----------


def test_signature_failure_never_reaches_business_layer(build) -> None:
    """坏签名 → 403，且**任何**业务痕迹都没有：不去重、不解析身份、不发卡。"""

    sender = RecordingSender()
    container = build(sender)
    provision(container)
    client = make_client(container)

    calls: list[str] = []
    original = container.im_gateway.resolver.resolve

    def counting(channel: str, im_user_id: str, **kwargs):
        calls.append(im_user_id)
        return original(channel, im_user_id, **kwargs)

    container.im_gateway.resolver.resolve = counting  # type: ignore[method-assign]

    body = message_body(content="状态 00000000-0000-0000-0000-000000000000", msg_id="9001")
    response = post_callback(client, body, signature="0" * 40)

    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "im_signature_invalid"
    assert calls == []  # 身份解析一次都没被调用
    assert container.im_store.has_message("wecom", "9001") is False  # 去重表没被写
    assert sender.cards == []
    assert container.im_store.list_deliveries() == []


def test_plaintext_callback_is_rejected(build) -> None:
    """明文模式的签名不覆盖消息体，收下它等于假装验签，所以直接拒绝。"""

    container = build(RecordingSender())
    provision(container)
    client = make_client(container)

    body = b"<xml><ToUserName><![CDATA[ww]]></ToUserName><MsgId>1</MsgId></xml>"
    response = post_callback(client, body)
    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "im_payload_invalid"
    assert container.im_store.list_deliveries() == []


def test_url_verification_requires_signature(build) -> None:
    container = build(RecordingSender())
    client = make_client(container)
    echo = encrypt_message("echo-12345")

    ok = client.get(
        "/api/im/wecom/callback",
        params={
            "msg_signature": sign(encrypt=echo),
            "timestamp": "1700000000",
            "nonce": "nonce1",
            "echostr": echo,
        },
    )
    assert ok.status_code == 200
    assert ok.text == "echo-12345"

    bad = client.get(
        "/api/im/wecom/callback",
        params={
            "msg_signature": "0" * 40,
            "timestamp": "1700000000",
            "nonce": "nonce1",
            "echostr": echo,
        },
    )
    assert bad.status_code == 403


# ---------- 2. 幂等 ----------


def test_duplicate_message_id_is_processed_once(build) -> None:
    sender = RecordingSender()
    container = build(sender)
    provision(container)
    client = make_client(container)
    task_id, _ = create_task(container)
    settle(container, sender)

    body = message_body(content=f"状态 {task_id}", msg_id="1000001")
    first = post_callback(client, body)
    second = post_callback(client, body)

    # 两次回调企业微信都收到 200（否则它会继续重投），但只发一张卡。
    assert first.status_code == 200
    assert second.status_code == 200
    container.im_cards.flush()
    assert len(sender.cards) == 1


def test_duplicate_message_id_is_processed_once_under_concurrency(build) -> None:
    """重投可能并发到达：认领必须是原子的，只有一方能拿到"第一次"。"""

    sender = RecordingSender()
    container = build(sender)
    provision(container)
    task_id, _ = create_task(container)
    settle(container, sender)
    body = message_body(content=f"状态 {task_id}", msg_id="1000002")
    params = callback_params(body)

    results: list[InboundOutcome] = []
    lock = Lock()

    def worker() -> None:
        outcome = container.im_gateway.handle(params, body).outcome
        with lock:
            results.append(outcome)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert results.count(InboundOutcome.REPLIED) == 1
    assert results.count(InboundOutcome.DUPLICATE) == 7
    assert len(sender.cards) == 1


def test_dedupe_survives_restart_with_sqlite(workdir) -> None:
    """重启后同一条 message id 仍然算处理过：去重表必须落盘，不能只在进程内。"""

    path = workdir / "workpanda.db"
    body = message_body(content="状态 00000000-0000-0000-0000-000000000000", msg_id="1000003")

    first = make_container(RecordingSender(), store_backend="sqlite", sqlite_path=str(path))
    try:
        provision(first)
        first.im_gateway.handle(callback_params(body), body)
        assert first.im_store.has_message("wecom", "1000003") is True
    finally:
        first.close()

    second = make_container(RecordingSender(), store_backend="sqlite", sqlite_path=str(path))
    try:
        provision(second)
        result = second.im_gateway.handle(callback_params(body), body)
        assert result.outcome is InboundOutcome.DUPLICATE
    finally:
        second.close()

    store = SqliteImGatewayStore(path)
    try:
        assert store.has_message("wecom", "1000003") is True
    finally:
        store.close()


# ---------- 3. 身份与授权 ----------


def test_unmapped_user_is_rejected_and_gets_no_reply(build) -> None:
    sender = RecordingSender()
    container = build(sender)
    provision(container, im_user_id="lisi")  # 入站的是 zhangsan，没映射
    client = make_client(container)
    task_id, _ = create_task(container)
    settle(container, sender)

    body = message_body(content=f"状态 {task_id}", from_user=IM_USER, msg_id="1000004")
    response = post_callback(client, body)

    assert response.status_code == 200  # 平台侧收到"已处理"，不要它继续重投
    container.im_cards.flush()
    assert sender.cards == []
    # 留痕在解析器里：拒绝记录带 IM 标识，能回答"是谁被拦了"。
    rejected = container.staff_store.list_access(rejected_only=True)
    assert rejected and rejected[0].im_user_id == IM_USER


def test_staff_without_grant_gets_no_card(build) -> None:
    sender = RecordingSender()
    container = build(sender)
    provision(container, flow_id="test.direct")  # 授权在别的流程上
    client = make_client(container)
    task_id, _ = create_task(container)
    settle(container, sender)

    post_callback(client, message_body(content=f"状态 {task_id}", msg_id="1000005"))
    container.im_cards.flush()

    assert sender.cards == []
    # 没有发出去的卡片，就不该有 SENT 留痕（创建期的 no_recipient 不算发送）。
    assert not [
        item for item in container.im_store.list_deliveries() if item.outcome is CardOutcome.SENT
    ]


def test_unknown_command_is_ignored_without_reply(build) -> None:
    sender = RecordingSender()
    container = build(sender)
    provision(container)
    client = make_client(container)
    task_id, _ = create_task(container)
    settle(container, sender)

    for text, msg_id in (
        (f"帮我确认一下 {task_id}", "1000006"),
        (f"状态 {task_id} 顺便办了吧", "1000007"),
        ("状态", "1000008"),
    ):
        post_callback(client, message_body(content=text, msg_id=msg_id))

    container.im_cards.flush()
    assert sender.cards == []


def test_command_parser_is_strict() -> None:
    task_id = uuid4()
    assert parse_task_command(f"状态 {task_id}") == task_id
    assert parse_task_command(f"状态：{task_id}") == task_id
    assert parse_task_command(f"STATUS {task_id}") == task_id
    assert parse_task_command(f"确认 {task_id}") is None
    assert parse_task_command(f"状态 {task_id} 请办理") is None
    assert parse_task_command("状态 不是任务号") is None


# ---------- 4. 出站卡片内容 ----------


def test_reply_card_carries_only_status_and_deeplink(build) -> None:
    sender = RecordingSender()
    container = build(sender)
    provision(container)
    client = make_client(container)
    task_id, _ = create_task(container)
    settle(container, sender)

    post_callback(client, message_body(content=f"状态 {task_id}", msg_id="1000009"))
    container.im_cards.flush()

    assert len(sender.cards) == 1
    card = sender.cards[0]
    assert card.task_id == task_id
    assert card.status == TaskStatus.WAITING_FOR_CONFIRMATION.value
    assert card.url == f"{CONSOLE_BASE}/tasks/{task_id}"
    # 结构断言：字段表就是隐私边界，多加一个字段必须是一次显式决定。
    assert set(TaskCard.model_fields) == {"task_id", "status", "title", "description", "url"}


def test_card_never_contains_service_object_data(build) -> None:
    """负向回归：槽位原文、联系人、结果正文一个字都不能出现在卡片里。"""

    sender = RecordingSender()
    container = build(sender)
    provision(container)
    create_task(container, slots={"subject": "张三申请低保", "contact": "13812345678"})

    # 创建任务时发的就是那张"待人确认"卡，直接看它。
    assert len(sender.cards) == 1
    text = sender.cards[0].text()
    assert "13812345678" not in text
    assert "张三" not in text
    assert "测试主题" not in text


def test_card_text_goes_through_redaction(build, monkeypatch) -> None:
    """第二道防线：即使有人往标签里塞了手机号，发出前也会被掩码。"""

    sender = RecordingSender()
    container = build(sender)
    provision(container)
    monkeypatch.setitem(
        STATUS_LABELS, TaskStatus.WAITING_FOR_CONFIRMATION.value, "待人确认 13812345678"
    )
    task_id, _ = create_task(container)

    assert len(sender.cards) == 1
    card = sender.cards[0]
    assert "13812345678" not in card.text()
    assert "138****5678" in card.text()
    assert str(task_id) in card.text()  # 任务号仍在，卡片还能定位到具体任务


# ---------- 5. 通知失败不影响任务推进 ----------


def test_notification_failure_does_not_block_task_progress(build) -> None:
    """发送端全挂：任务照常从"待人确认"走到"已办结"，卡片只留下失败留痕。"""

    sender = FailingSender()
    container = build(sender)
    provision(container)
    task_id, subject = create_task(container)
    assert sender.attempts >= 1  # 创建期的"待人确认"卡已经尝试过一次并失败
    assert (
        container.tasks.get_task(task_id, subject).status is TaskStatus.WAITING_FOR_CONFIRMATION
    )

    confirmed = container.tasks.confirm(
        task_id, TaskConfirmRequest(step_id=WAITING_STEP), subject
    )
    assert confirmed.status is TaskStatus.COMPLETED

    container.im_cards.flush()
    deliveries = container.im_store.list_deliveries()
    assert deliveries and all(item.outcome is CardOutcome.FAILED for item in deliveries)


def test_notification_is_skipped_without_recipients(build) -> None:
    container = build(RecordingSender())
    create_task(container)  # 没有工作人员、没有映射

    reasons = [item.reason for item in container.im_store.list_deliveries()]
    assert reasons == ["no_recipient"]


def test_outbound_disabled_is_skipped_not_failed(build) -> None:
    """只接入站、不发卡片：记 skipped，不当故障。"""

    container = build(None, wecom_corp_secret="", wecom_agent_id=0)
    provision(container)
    client = make_client(container)
    task_id, _ = create_task(container)
    body = message_body(content=f"状态 {task_id}", msg_id="1000010")

    result = container.im_gateway.handle(callback_params(body), body)
    assert result.outcome is InboundOutcome.REPLY_FAILED
    assert result.detail == "outbound_disabled"
    # 平台侧仍然收到 200：这次会被判成重投，但不该让企业微信继续重试。
    assert post_callback(client, body).status_code == 200

    deliveries = container.im_store.list_deliveries()
    assert [item.outcome for item in deliveries] == [CardOutcome.SKIPPED]
    assert deliveries[0].reason == "outbound_disabled"


# ---------- 6. 只发"值得发"的状态，且不重复 ----------


def test_notification_is_sent_once_per_status_and_step(build) -> None:
    sender = RecordingSender()
    container = build(sender)
    provision(container)
    task_id, _ = create_task(container)

    assert len(sender.cards) == 1

    container.im_gateway.notify_task(task_id)
    container.im_gateway.notify_task(task_id)
    container.im_cards.flush()
    assert len(sender.cards) == 1


def test_process_events_do_not_produce_cards(build) -> None:
    """一条流程一路跑到完成只发一张卡：过程事件不进 IM。"""

    sender = RecordingSender()
    container = build(sender)
    provision(container, flow_id="test.direct")
    task_id, subject = create_task(container, flow_id="test.direct", slots={})

    assert container.tasks.get_task(task_id, subject).status is TaskStatus.COMPLETED
    assert len(sender.cards) == 1
    assert sender.cards[0].status == TaskStatus.COMPLETED.value


# ---------- 7. 结构保证：网关没有写入口 ----------


def test_gateway_has_no_task_state_write_surface(build) -> None:
    """结构断言：网关拿到的是只读视图，模块里也没有任何写任务的调用。

    用 AST 而不是字符串匹配：文档里提到 `TaskService` 不算"用到了它"，
    而一次真正的 `.commit(` 调用会出现在 `ast.Call` 里、躲不过去。
    """

    container = build(RecordingSender())
    gateway = container.im_gateway
    assert gateway is not None

    reader = gateway.tasks
    for forbidden in ("commit", "save", "append_event", "create", "answer"):
        assert not hasattr(reader, forbidden), forbidden

    source = Path(inspect.getsourcefile(ImGatewayService) or "").read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[-1] for alias in node.names)
    assert not {"TaskService", "TaskStore"} & imported

    called = {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    assert not {"commit", "append_event", "save", "create", "answer", "confirm"} & called


def test_im_surface_is_only_the_callback() -> None:
    """白名单断言：网关只多出这一对回调入口。新增端点必须是一次显式决定。"""

    surface = sorted((route.path, method) for route in im_router.routes for method in route.methods)
    assert surface == sorted(
        [
            ("/api/im/wecom/callback", "GET"),
            ("/api/im/wecom/callback", "POST"),
        ]
    )


# ---------- 8. 未配置就是没有这个入口 ----------


def test_im_prefix_is_absent_when_not_configured(build) -> None:
    container = build(
        RecordingSender(),
        wecom_callback_token="",
        wecom_encoding_aes_key="",
        wecom_corp_id="",
    )
    assert container.im_gateway is None
    client = make_client(container)
    body = message_body(content="状态 x", msg_id="1")
    response = client.post("/api/im/wecom/callback", params=callback_params(body), content=body)
    assert response.status_code == 404


def test_half_configured_callback_fails_at_startup(build) -> None:
    """半套配置会表现为"接口开着但每条回调都失败"，宁可在启动期就拒绝。"""

    with pytest.raises(WeComConfigError):
        build(RecordingSender(), wecom_encoding_aes_key="")


def test_console_base_url_must_be_a_site_root() -> None:
    # 校验发生在装配期（启动期）：写错即启动失败，而不是等到第一张卡片发不出去。
    with pytest.raises(ValueError):
        make_container(RecordingSender(), staff_console_base_url="wpa.example.com")
    with pytest.raises(ValueError):
        make_container(RecordingSender(), staff_console_base_url="https://wpa.example.com/app")


# ---------- 9. 存储后端一致性 ----------


def test_in_memory_and_sqlite_stores_agree(workdir) -> None:
    task_id = uuid4()
    memory = InMemoryImGatewayStore()
    sqlite = SqliteImGatewayStore(workdir / "im.db")
    try:
        for store in (memory, sqlite):
            assert store.claim_message("wecom", "m1") is True
            assert store.claim_message("wecom", "m1") is False
            assert store.has_message("wecom", "m1") is True
            assert store.claim_notification(task_id, "completed", "") is True
            assert store.claim_notification(task_id, "completed", "") is False
            assert store.list_deliveries() == []
    finally:
        sqlite.close()
