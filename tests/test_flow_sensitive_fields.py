"""群众侧对话所需的两项元数据：事项名与字段敏感标记（B1）。

对话式界面有两句话必须由流程作者提供、页面不能自己编：

1. **开场要说"您要办的是哪件事"**。`current_step_title` 是步骤名（"收集报修信息"），
   不是事项名（"报修"）。页面唯一的替代品是复述群众原话，而 `Task` 根本不保存
   `input_text`——那句话在任何地方都取不回来，所以事项名只能由版本下发。
2. **回显要不要打码**。页面没有依据判断哪个字段敏感，只能二选一：全部明文（放大
   暴露面）或全部不显示（群众看不懂在填什么）。标记归作者，页面只负责取。

刻意**没有**做的两件事，一并写在这里，免得日后被当成遗漏：

- 不建敏感词表、不做"`required_fields` 里有敏感字段却没标记就拒绝发布"。哪几个词算
  敏感是业务口径，不是代码能替运营定的；硬门禁会把"作者没想清楚"变成"发不出去"，
  而真正该拦的是下面那条**标错了**——它看起来生效、实际没生效。
- 不改 `input_text` 是否落库。本组只做投影，不碰写入通道。
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from workpanda.bootstrap import build_container
from workpanda.config import Settings
from workpanda.domain.flow import (
    FlowDefinition,
    FlowStatus,
    StepDefinition,
    StepKind,
    flow_field_errors,
)
from workpanda.domain.models import Task, TaskCreateRequest, TaskView
from workpanda.infrastructure.adapters import ScriptedExecutor
from workpanda.main import create_app

ADMIN_TOKEN = "admin-secret-0123456789"
ADMIN_HEADER = {"X-WorkPanda-Admin-Token": ADMIN_TOKEN}
SESSION = "session-sensitive"
CONVERSATION = "conversation-sensitive"


def repair_flow(*, version: str = "1", marks: tuple[str, ...] = ("contact",)) -> FlowDefinition:
    return FlowDefinition(
        flow_id="web.repair",
        version=version,
        title="报修",
        status=FlowStatus.PUBLISHED,
        steps=(
            StepDefinition(
                step_id="collect",
                kind=StepKind.COLLECT,
                title="收集报修信息",
                required_fields=("subject", "contact"),
                prompt="请说一下要报修什么，再留一个联系电话。",
                field_labels={"subject": "报修事项", "contact": "联系电话"},
                sensitive_fields=marks,
            ),
            StepDefinition(
                step_id="submit",
                kind=StepKind.EXECUTE,
                title="提交报修工单",
                tool="web.submit",
                requires_confirmation=True,
                confirm_message="提交后维修人员会在三个工作日内联系您。",
            ),
        ),
    )


def build_web(flows: list[FlowDefinition]) -> tuple[object, TestClient]:
    container = build_container(
        Settings(store_backend="memory", enable_demo_flow=False),
        flows=flows,
        executor=ScriptedExecutor(),
    )
    return container, TestClient(create_app(container=container))


def start(client: TestClient, flow: FlowDefinition) -> tuple[dict, dict[str, str]]:
    created = client.post(
        "/api/tasks",
        json={
            "conversation_id": CONVERSATION,
            "input_text": "我要报修，厨房水管漏水，电话 13800000000",
            "flow_id": flow.flow_id,
            "flow_version": flow.version,
        },
    )
    assert created.status_code == 201, created.text
    # 会话凭据只从响应头取，不写进请求体——页面也是这么拿的。
    return created.json(), {"X-WorkPanda-Session": created.headers["X-WorkPanda-Session"]}


# ---------- 正常路径：两项都真的下发了 ----------


def test_view_carries_flow_title_and_sensitive_fields() -> None:
    """`GET /api/tasks/{id}` 里能读到事项名与要打码的字段。

    走 HTTP 而不只是 `TaskView.from_task`：这里验的是**线上格式**，序列化时漏掉
    一个字段的话，页面拿到的就是 `undefined`，而单测看不出这件事。
    """

    flow = repair_flow()
    container, client = build_web([flow])
    try:
        created, headers = start(client, flow)
        fetched = client.get(f"/api/tasks/{created['task_id']}", headers=headers)
        assert fetched.status_code == 200, fetched.text
        body = fetched.json()
        assert body["flow_title"] == "报修"
        assert body["sensitive_fields"] == ["contact"]
        # 事项名不能让页面从步骤名里猜。
        assert body["current_step_title"] == "收集报修信息"
    finally:
        container.close()


def test_sensitive_marks_are_merged_across_steps_in_declaration_order() -> None:
    """两个步骤各标自己的字段：合并、去重，顺序按声明先后。

    用列表而不是集合，是为了让"页面按服务端给的顺序打码"这件事可断言；集合会
    把这个顺序变成实现细节。
    """

    flow = FlowDefinition(
        flow_id="web.multi",
        version="1",
        title="多步收集",
        steps=(
            StepDefinition(
                step_id="first",
                kind=StepKind.COLLECT,
                title="先要证件号",
                required_fields=("holder", "id_card"),
                sensitive_fields=("id_card",),
            ),
            StepDefinition(
                step_id="second",
                kind=StepKind.COLLECT,
                title="再要联系方式",
                required_fields=("contact", "id_card"),
                sensitive_fields=("contact", "id_card"),
            ),
        ),
    )
    task = Task(
        conversation_id=CONVERSATION,
        flow_id="web.multi",
        flow_version="1",
        current_step="first",
        pending_fields=["holder"],
    )

    class _Registry:
        def find(self, flow_id: str, version: str) -> FlowDefinition:
            return flow

    view = TaskView.from_task(task, _Registry())  # type: ignore[arg-type]
    assert view.sensitive_fields == ["id_card", "contact"]
    # 标签那一侧早就是这个语义：合并、先声明者优先。两者必须一致，
    # 否则页面会在"有标签但不知是否敏感"的中间态上自己猜。
    assert view.sensitive_fields == ["id_card", "contact"]


# ---------- 缺参/歧义：没有版本就什么都不给，而不是 500 ----------


def test_missing_flow_version_yields_empty_markers_without_500() -> None:
    """历史任务指向库里没有的版本：两项为空，其余照常。"""

    container, _ = build_web([])
    try:
        orphan = Task(
            conversation_id=CONVERSATION,
            flow_id="web.repair",
            flow_version="0",
            current_step="collect",
            pending_fields=["subject"],
        )
        view = TaskView.from_task(orphan, container.flows)  # type: ignore[attr-defined]
        assert view.flow_title is None
        assert view.sensitive_fields == []
        # 状态翻译与它无关，仍然要给出来——这类任务照样要能看、能取消。
        assert view.ui_state == "received"
    finally:
        container.close()


def test_declaring_nothing_is_allowed_and_means_plain_text() -> None:
    """不声明就是明文回显。这是**刻意**的默认值，不是漏掉的校验。

    平台不能替作者断定某个字段敏感，也不能因为作者没声明就让群众看不懂在填什么。
    这条用例存在的意义是：日后如果有人想把"漏声明"改成硬门禁，会先看到这里
    写明了为什么没这么做。
    """

    flow = repair_flow(marks=())
    assert flow_field_errors(flow) == []
    container, client = build_web([flow])
    try:
        created, _ = start(client, flow)
        assert created["sensitive_fields"] == []
        assert created["flow_title"] == "报修"
    finally:
        container.close()


# ---------- 不应发生：标错了字段，保存时就拒 ----------


def test_typo_in_sensitive_fields_is_rejected_with_field_location() -> None:
    """标错字段比不标更危险：作者以为已经打码，于是不会再看一眼明文回显。

    所以错误必须落在写流程的人面前，并定位到具体那个名字。
    """

    flow = repair_flow(marks=("contact", "phone"))  # 本步骤没有 phone
    errors = flow_field_errors(flow)
    assert [(error.loc, error.message) for error in errors] == [
        (
            ("steps", 0, "sensitive_fields", "phone"),
            "step 'collect' marks 'phone' as sensitive, "
            "which is not one of its required_fields",
        )
    ]


def test_admin_draft_api_rejects_the_typo_so_it_never_reaches_the_store() -> None:
    """同一条规则在保存入口上生效：管理侧拿到 422 与字段位置，库里什么都不留。"""

    container = build_container(
        Settings(
            store_backend="memory",
            enable_demo_flow=False,
            admin_tokens=f"boss:admin:{ADMIN_TOKEN}",
            intent_sample_gate=False,
        )
    )
    client = TestClient(create_app(container=container))
    try:
        response = client.post(
            "/api/admin/flows",
            headers=ADMIN_HEADER,
            json={
                "flow_id": "admin.sensitive",
                "version": "1",
                "title": "管理侧标错字段",
                "steps": [
                    {
                        "step_id": "collect",
                        "kind": "collect",
                        "title": "收集",
                        "required_fields": ["subject"],
                        "sensitive_fields": ["contact"],
                    },
                    {
                        "step_id": "submit",
                        "kind": "execute",
                        "title": "提交",
                        "tool": "replay_submit",
                    },
                ],
            },
        )
        assert response.status_code == 422, response.text
        detail = response.json()["detail"]
        assert detail["code"] == "admin_draft_invalid"
        assert ["steps", 0, "sensitive_fields", "contact"] in [
            field["loc"] for field in detail["fields"]
        ], detail
        assert client.get("/api/admin/flows", headers=ADMIN_HEADER).json() == []
    finally:
        container.close()


# ---------- 不应发生：视图里没有原始自然语言 ----------


def test_view_never_carries_raw_input_text() -> None:
    """守住 §7 最小化：`input_text` 那句原话在任何响应里都不出现。

    B1 加了事项名，页面于是不必再复述群众原话；这条用例钉住"原始输入不进视图"，
    免得日后有人为了让开场气泡"更自然"而把它加回来。
    """

    flow = repair_flow()
    container, client = build_web([flow])
    try:
        created, _ = start(client, flow)
        assert "input_text" not in created
        assert "厨房水管漏水，电话" not in str(created)
        view = TaskView.from_task(
            container.tasks.create_task(  # type: ignore[attr-defined]
                TaskCreateRequest(
                    conversation_id=CONVERSATION,
                    input_text="我要报修，厨房水管漏水，电话 13800000000",
                    flow_id="web.repair",
                    flow_version="1",
                ),
                SESSION,
            ),
            container.flows,  # type: ignore[attr-defined]
        )
        assert "input_text" not in view.model_dump()
    finally:
        container.close()
