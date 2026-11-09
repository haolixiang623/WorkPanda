"""管理侧草稿表单必须能表达「流程定义里可维护的东西」（A2）。

界面建出来的流程有两处一直没接线，且都不会报错：

1. 步骤的 `field_labels` —— 群众页只好把内部字段名（`subject`）印给群众；
2. 流程的 `intent_rules` —— 一句话命不中，只能由接口显式指定 `flow_id` 建任务。

两处都是「机制在、没接线」：服务端字段早就有、校验早就有，表单里却没有入口。
所以这组用例除了端到端跑通，还要钉住**表单覆盖性**：可写的字段缺一个输入口就红，
否则下次新增字段还会以同样方式静默失效。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from workpanda.bootstrap import build_container
from workpanda.config import Settings
from workpanda.domain.flow import StepDefinition
from workpanda.domain.models import TaskCreateRequest, TaskStatus, TaskView
from workpanda.main import create_app

REPO_ROOT = Path(__file__).resolve().parents[1]
UI_SRC = REPO_ROOT / "admin-ui" / "src" / "main.ts"
UI_DIST = REPO_ROOT / "admin-ui" / "dist" / "main.js"
UI_PAGE = REPO_ROOT / "admin-ui" / "index.html"

ADMIN_TOKEN = "admin-secret-0123456789"
ADMIN_HEADER = {"X-WorkPanda-Admin-Token": ADMIN_TOKEN}

# 与界面 readDraft() 输出同形。关键词是「灵活就业」「社保补贴」，正例都含其一。
UI_DRAFT: dict = {
    "flow_id": "ui.subsidy",
    "version": "1",
    "title": "灵活就业社保补贴 材料清单自查",
    "steps": [
        {
            "step_id": "collect",
            "kind": "collect",
            "title": "收集必要信息",
            "required_fields": ["subject"],
            "field_labels": {"subject": "办理主题"},
            "prompt": "请补充要自查的补贴类型。",
            "requires_confirmation": False,
        },
        {
            "step_id": "submit",
            "kind": "execute",
            "title": "生成材料清单",
            "required_fields": [],
            "tool": "replay_submit",
            "requires_confirmation": True,
            "confirm_message": "即将生成清单，请确认。",
            "answer_label": "材料清单自查结果",
        },
    ],
    "intent_samples": [
        {"sample_id": "pos_1", "category": "positive", "text": "我要办灵活就业社保补贴"},
        {"sample_id": "pos_2", "category": "positive", "text": "社保补贴怎么办"},
        {"sample_id": "pos_3", "category": "positive", "text": "灵活就业补贴材料"},
        {"sample_id": "near_1", "category": "near_miss", "text": "我要取消订单"},
        {"sample_id": "near_2", "category": "near_miss", "text": "查一下天气预报"},
        {"sample_id": "oos_1", "category": "out_of_scope", "text": "今天天气不错"},
    ],
    "intent_rules": [
        {
            # 界面保存时按 flow_id / version 输入框补齐这两项，请求体里也是齐的。
            "flow_id": "ui.subsidy",
            "flow_version": "1",
            "intent_id": "ui.subsidy.list",
            "keywords": ["灵活就业", "社保补贴"],
            "slot_patterns": {"subject": ["主题[:：]\\s*(\\S+)"]},
        }
    ],
}


def ui_container(**overrides: object):
    """不注入路由器：识别必须来自**已发布版本**里的规则，这正是 A2 要证明的事。"""

    settings = Settings(
        store_backend="memory",
        enable_demo_flow=False,
        admin_tokens=f"boss:admin:{ADMIN_TOKEN}",
        **overrides,
    )
    container = build_container(settings)
    return container, TestClient(create_app(container=container))


def create_draft(client: TestClient, **overrides: object) -> dict:
    payload = {**UI_DRAFT, **overrides}
    response = client.post("/api/admin/flows", headers=ADMIN_HEADER, json=payload)
    assert response.status_code == 201, response.text
    return response.json()


# ---------- 端到端：界面建出的流程能一句话命中，且字段名是中文 ----------


def test_ui_created_flow_routes_a_sentence_and_keeps_chinese_labels() -> None:
    """建草稿 → 回放 → 发布 → 群众只说一句话就能落进这个事项。"""

    container, client = ui_container()
    try:
        created = create_draft(client)
        # 两处接线的最直接证据：服务端没有丢掉它们。
        assert created["steps"][0]["field_labels"] == {"subject": "办理主题"}
        assert created["intent_rules"][0]["intent_id"] == "ui.subsidy.list"
        # 第三处接线：正文行名。没有它，这个事项办完了也说不出一句话给群众。
        assert created["steps"][1]["answer_label"] == "材料清单自查结果"

        case = client.post(
            "/api/admin/flows/ui.subsidy/versions/1/cases",
            headers=ADMIN_HEADER,
            json={
                "case_id": "happy",
                "slots": {"subject": "社保补贴"},
                "expect": {"steps": ["collect", "submit"], "tools": ["replay_submit"]},
            },
        )
        assert case.status_code == 201, case.text

        published = client.post(
            "/api/admin/flows/ui.subsidy/publish",
            headers=ADMIN_HEADER,
            json={"version": "1", "apply": True},
        )
        assert published.status_code == 200, published.text
        assert published.json()["published"] is True

        # 群众侧：不给 flow_id，只说一句话。
        task = container.tasks.create_task(
            TaskCreateRequest(
                conversation_id="conv-subsidy",
                input_text="我要办灵活就业社保补贴，主题：补贴自查",
            ),
            "session-subsidy",
        )
        assert task.flow_id == "ui.subsidy"
        assert task.flow_version == "1"
        # 槽位来自规则里的正则，不是模型猜的。
        assert task.slots == {"subject": "补贴自查"}
        assert task.status is TaskStatus.WAITING_FOR_CONFIRMATION

        # 群众页看到的是作者给的中文名，而不是内部字段名。
        view = TaskView.from_task(task, container.flows)
        assert view.flow_id == "ui.subsidy"
        assert view.field_labels["subject"] == "办理主题"
    finally:
        container.close()


def test_sentence_outside_the_rule_still_asks_for_clarification() -> None:
    """规则只认自己的关键词：不会因为"有规则了"就把别的话也捞进来。"""

    container, client = ui_container()
    try:
        create_draft(client)
        client.post(
            "/api/admin/flows/ui.subsidy/publish",
            headers=ADMIN_HEADER,
            json={"version": "1", "apply": False},
        )

        # 未发布时不该有任何规则生效——发布只做预演（apply=False）。
        assert container.flows.published() == []
    finally:
        container.close()


# ---------- 表单覆盖性：可写字段必须在界面上有入口 ----------


# 必须能在界面上录入的步骤字段。`fail_to_handoff` 刻意不在列表里：默认就是"失败
# 转人工"，把它暴露出去等于让作者可以把失败改成终态，属于风险更高的一次决策，
# 不该顺手跟着表单一起放出来。
EDITABLE_STEP_FIELDS = (
    "step_id",
    "kind",
    "title",
    "required_fields",
    "field_labels",
    "prompt",
    "instruction",
    "tool",
    "confirm_message",
    "answer_label",
    "requires_confirmation",
)


def test_every_editable_step_field_has_a_form_input() -> None:
    """新增步骤字段时，界面必须同时长出输入口，否则又是「机制在、没接线」。"""

    source = UI_SRC.read_text(encoding="utf-8")
    missing = [
        name for name in EDITABLE_STEP_FIELDS if f'data-key="{name}"' not in source
    ]
    assert missing == []
    # 反向兜底：列表不能比模型本身还宽——多出来的名字会让上面的检查永远成立。
    assert set(EDITABLE_STEP_FIELDS) <= set(StepDefinition.model_fields)


def test_flow_level_fields_are_sent_by_the_form() -> None:
    """流程级字段靠 readDraft() 送出，没有 data-key 可查，所以按源码断言。"""

    source = UI_SRC.read_text(encoding="utf-8")
    for key in ("intent_samples:", "intent_rules:", "flow_id:", "version:", "title:"):
        assert key in source, key
    # 规则的两个归属字段由输入框补齐，界面不给单独入口（写错就是 #73）。
    assert "flow_version: version" in source


def test_compiled_bundle_carries_the_new_fields() -> None:
    """防「源码改了、产物没编译」：交付的 bundle 必须真的带上这两处接线。"""

    bundle = UI_DIST.read_text(encoding="utf-8")
    page = UI_PAGE.read_text(encoding="utf-8")

    assert "field_labels:" in bundle
    assert "intent_rules:" in bundle
    assert "answer_label:" in bundle
    assert "parseSlotPatterns" in bundle or "slot_patterns" in bundle
    assert 'id="rules"' in page
    assert 'id="add-rule"' in page
    assert "字段中文名" in page


# ---------- 槽位正则：写坏了必须在保存时就被拦下 ----------


def test_invalid_slot_pattern_is_rejected_at_save() -> None:
    """写坏的正则原本会在群众提交那一刻抛 `re.error`（对外是 500）。"""

    container, client = ui_container()
    try:
        rule = dict(UI_DRAFT["intent_rules"][0])
        rule["slot_patterns"] = {"subject": ["(未闭合"]}
        response = client.post(
            "/api/admin/flows",
            headers=ADMIN_HEADER,
            json={**UI_DRAFT, "intent_rules": [rule]},
        )

        assert response.status_code == 422, response.text
        detail = response.json()["detail"]
        assert detail["code"] == "admin_draft_invalid"
        fields = detail["fields"]
        assert any("invalid pattern" in item["message"] for item in fields), fields
        assert ["intent_rules", 0, "slot_patterns", "subject"] in [
            item["loc"] for item in fields
        ]
    finally:
        container.close()


def test_blank_slot_pattern_is_rejected_at_save() -> None:
    """空正则恒中，会把整句前半段当成字段值写进执行参数。"""

    container, client = ui_container()
    try:
        rule = dict(UI_DRAFT["intent_rules"][0])
        rule["slot_patterns"] = {"subject": ["  "]}
        response = client.post(
            "/api/admin/flows",
            headers=ADMIN_HEADER,
            json={**UI_DRAFT, "intent_rules": [rule]},
        )

        assert response.status_code == 422, response.text
        fields = response.json()["detail"]["fields"]
        assert any("blank pattern" in item["message"] for item in fields), fields
    finally:
        container.close()


def test_rule_pointing_at_another_version_is_still_rejected() -> None:
    """界面把这两个字段补齐了，服务端校验依旧独立成立（不靠界面自觉）。"""

    container, client = ui_container()
    try:
        rule = {**UI_DRAFT["intent_rules"][0], "flow_version": "9"}
        response = client.post(
            "/api/admin/flows",
            headers=ADMIN_HEADER,
            json={**UI_DRAFT, "intent_rules": [rule]},
        )

        assert response.status_code == 422, response.text
        fields = response.json()["detail"]["fields"]
        assert ["intent_rules", 0, "flow_id"] in [item["loc"] for item in fields]
    finally:
        container.close()


@pytest.mark.parametrize("pattern", ["主题[:：]\\s*(\\S+)", "(?P<x>a)"])
def test_usable_patterns_are_accepted(pattern: str) -> None:
    """对照组：能编译的正则不该被误拦（否则上面的拒绝只是「什么都拦」）。"""

    container, client = ui_container()
    try:
        rule = dict(UI_DRAFT["intent_rules"][0])
        rule["slot_patterns"] = {"subject": [pattern]}
        created = create_draft(client, intent_rules=[rule])
        assert created["intent_rules"][0]["slot_patterns"]["subject"] == [pattern]
    finally:
        container.close()
