"""管理侧 SOP 配置台（Issue #51）的契约与权限回归。

按 Issue 验收标准组织：

- 正常路径：建草稿 → 发布 → 群众侧能命中该流程；
- 缺参/歧义：必填字段缺失与语义非法都被拒，且错误定位到具体字段；
- 失败：保存/发布失败时明确报错，预演不移动指针，界面不会"看起来已发布"；
- 恢复：回滚后群众侧立即用旧版本，在途任务仍能按自己的版本推进；
- 不应发生：viewer 发布/回滚、前端构造出后端不允许的流程、管理侧凭据串到
  群众侧或运维侧、凭据写进前端构建产物。
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from workpanda.api.security import SESSION_HEADER
from workpanda.bootstrap import Container, build_container
from workpanda.config import Settings
from workpanda.domain.flow import FlowNotRunnable
from workpanda.main import create_app

ADMIN_TOKEN = "admin-secret-0123456789"
VIEWER_TOKEN = "viewer-secret-0123456789"
OPS_TOKEN = "ops-secret-0123456789"
ADMIN_HEADER = {"X-WorkPanda-Admin-Token": ADMIN_TOKEN}
VIEWER_HEADER = {"X-WorkPanda-Admin-Token": VIEWER_TOKEN}

COLLECT = {
    "step_id": "collect",
    "kind": "collect",
    "title": "收集信息",
    "required_fields": ["subject"],
    "prompt": "请补充主题。",
}
EXECUTE = {
    "step_id": "submit",
    "kind": "execute",
    "title": "提交",
    "tool": "replay_submit",
    "requires_confirmation": True,
}


def draft(version: str = "1", **overrides) -> dict:
    payload = {
        "flow_id": "admin.demo",
        "version": version,
        "title": f"管理侧测试流程 {version}",
        "steps": [dict(COLLECT), dict(EXECUTE)],
    }
    payload.update(overrides)
    return payload


@pytest.fixture
def admin_client() -> Iterator[tuple[TestClient, Container]]:
    settings = Settings(
        _env_file=None,
        store_backend="memory",
        enable_demo_flow=False,
        admin_tokens=f"boss:admin:{ADMIN_TOKEN},duty:viewer:{VIEWER_TOKEN}",
        ops_token=OPS_TOKEN,
        # 本文件验的是管理侧"指针语义"（发布 / 预演 / 回滚 / 停用），与识别样例门禁
        # 无关；这里建的流程没有样例、代码里也没有对应规则，门禁会挡在发布之前。
        # 门禁与它的管理侧 409 映射由 `tests/test_intent_eval.py` 单独覆盖。
        intent_sample_gate=False,
    )
    container = build_container(settings)
    client = TestClient(create_app(container=container))
    yield client, container
    container.close()


def create_version(client: TestClient, version: str = "1") -> dict:
    response = client.post("/api/admin/flows", headers=ADMIN_HEADER, json=draft(version))
    assert response.status_code == 201, response.text
    return response.json()


def publish(client: TestClient, version: str = "1", *, apply: bool = True) -> dict:
    response = client.post(
        "/api/admin/flows/admin.demo/publish",
        headers=ADMIN_HEADER,
        json={"version": version, "apply": apply},
    )
    return response


def citizen_create(client: TestClient, container: Container) -> dict:
    """群众侧显式指定 flow_id 建任务，用来验证发布指针是否真的生效。"""

    return client.post(
        "/api/tasks",
        headers={SESSION_HEADER: container.signer.issue()},
        json={
            "conversation_id": "conversation-1",
            "input_text": "我要办理管理侧测试事项",
            "flow_id": "admin.demo",
        },
    )


# ---------- 独立凭据与权限矩阵 ----------


def test_admin_api_is_hidden_when_not_configured() -> None:
    container = build_container(
        Settings(_env_file=None, store_backend="memory", enable_demo_flow=False)
    )
    client = TestClient(create_app(container=container))
    try:
        assert client.get("/api/admin/flows", headers=ADMIN_HEADER).status_code == 404
        assert (
            client.post("/api/admin/flows", headers=ADMIN_HEADER, json=draft()).status_code
            == 404
        )
    finally:
        container.close()


def test_admin_api_requires_its_own_credential(admin_client) -> None:
    client, container = admin_client
    assert client.get("/api/admin/flows").status_code == 401
    assert (
        client.get(
            "/api/admin/flows", headers={"X-WorkPanda-Admin-Token": "wrong"}
        ).status_code
        == 401
    )
    # 运维只读凭据与群众会话都不能当管理侧凭据用。
    assert (
        client.get(
            "/api/admin/flows", headers={"X-WorkPanda-Admin-Token": OPS_TOKEN}
        ).status_code
        == 401
    )
    assert (
        client.get(
            "/api/admin/flows",
            headers={"X-WorkPanda-Admin-Token": container.signer.issue()},
        ).status_code
        == 401
    )
    # 反过来，管理侧凭据也不能读运维接口。
    assert (
        client.get(
            "/api/ops/tasks", headers={"X-WorkPanda-Ops-Token": ADMIN_TOKEN}
        ).status_code
        == 401
    )


def test_viewer_can_read_but_cannot_change_anything(admin_client) -> None:
    client, _ = admin_client
    create_version(client)
    assert client.get("/api/admin/flows", headers=VIEWER_HEADER).status_code == 200
    assert (
        client.get(
            "/api/admin/flows/admin.demo/versions/1", headers=VIEWER_HEADER
        ).status_code
        == 200
    )
    assert (
        client.post(
            "/api/admin/flows/admin.demo/versions/1/replay", headers=VIEWER_HEADER
        ).status_code
        == 200
    )
    write_calls = [
        ("post", "/api/admin/flows", draft("2")),
        ("post", "/api/admin/flows/admin.demo/publish", {"version": "1"}),
        ("post", "/api/admin/flows/admin.demo/disable", None),
        ("post", "/api/admin/flows/admin.demo/rollback", {"version": "1"}),
        (
            "post",
            "/api/admin/flows/admin.demo/versions/1/cases",
            {"case_id": "happy", "expect": {}},
        ),
        ("delete", "/api/admin/flows/admin.demo/versions/1/cases/happy", None),
    ]
    for method, path, body in write_calls:
        kwargs = {"headers": VIEWER_HEADER}
        if body is not None:
            kwargs["json"] = body
        response = getattr(client, method)(path, **kwargs)
        assert response.status_code == 403, (method, path, response.text)
        assert response.json()["detail"]["code"] == "admin_forbidden"


# ---------- 契约：字段形状与语义都受后端 schema 约束 ----------


@pytest.mark.parametrize(
    "payload",
    [
        {"title": "", "steps": [dict(COLLECT), dict(EXECUTE)]},
        {"steps": []},
        {"steps": [{"step_id": "s", "kind": "unknown"}]},
        {"flow_id": ""},
    ],
)
def test_draft_rejects_invalid_field_shapes_with_location(admin_client, payload) -> None:
    client, _ = admin_client
    body = draft()
    body.update(payload)
    response = client.post("/api/admin/flows", headers=ADMIN_HEADER, json=body)
    assert response.status_code == 422, response.text
    # FastAPI 的 422 带 loc；前端据此把错误标到对应输入框。
    assert response.json()["detail"]
    assert any("loc" in item for item in response.json()["detail"])


NO_TOOL_EXECUTE = {"step_id": "bad", "kind": "execute", "title": "执行"}
COLLECT_WITH_TOOL = {"step_id": "bad", "kind": "collect", "title": "收集", "tool": "x"}
COLLECT_WITH_CONFIRM = {
    "step_id": "bad",
    "kind": "collect",
    "title": "收集",
    "requires_confirmation": True,
}


@pytest.mark.parametrize(
    ("steps", "expected_loc"),
    [
        ([dict(COLLECT), NO_TOOL_EXECUTE], ["steps", 1, "tool"]),
        ([COLLECT_WITH_TOOL, dict(EXECUTE)], ["steps", 0, "tool"]),
        ([COLLECT_WITH_CONFIRM, dict(EXECUTE)], ["steps", 0, "requires_confirmation"]),
        ([dict(COLLECT), dict(COLLECT)], ["steps", 1, "step_id"]),
    ],
)
def test_draft_rejects_semantically_illegal_flows_with_field_location(
    admin_client, steps, expected_loc
) -> None:
    client, _ = admin_client
    response = client.post(
        "/api/admin/flows", headers=ADMIN_HEADER, json=draft(steps=steps)
    )
    assert response.status_code == 422, response.text
    detail = response.json()["detail"]
    assert detail["code"] == "admin_draft_invalid"
    assert expected_loc in [field["loc"] for field in detail["fields"]], detail
    # 非法流程绝不能落库。
    assert client.get("/api/admin/flows", headers=ADMIN_HEADER).json() == []


def test_status_is_authoring_only_and_versions_are_immutable(admin_client) -> None:
    client, _ = admin_client
    body = draft()
    body["status"] = "published"  # 客户端试图自带发布态
    created = client.post("/api/admin/flows", headers=ADMIN_HEADER, json=body)
    assert created.status_code == 201, created.text
    assert created.json()["status"] == "draft"
    assert client.get("/api/admin/flows", headers=ADMIN_HEADER).json()[0]["published"] is False

    duplicate = client.post("/api/admin/flows", headers=ADMIN_HEADER, json=draft())
    assert duplicate.status_code == 409
    assert duplicate.json()["detail"]["code"] == "admin_flow_conflict"
    # 版本内容没有被覆盖：仍是第一次保存的那一份。
    assert len(client.get("/api/admin/flows", headers=ADMIN_HEADER).json()) == 1


# ---------- 正常路径：发布后群众侧立即命中 ----------


def test_publish_makes_flow_hittable_by_citizen_side(admin_client) -> None:
    client, container = admin_client
    create_version(client)
    assert citizen_create(client, container).status_code == 422  # 草稿不可执行

    result = publish(client, "1")
    assert result.status_code == 200, result.text
    body = result.json()
    assert body["published"] is True
    assert body["replay_ran"] is False  # 没有用例时门禁不假装检查通过
    assert "已发布" in body["message"]

    overview = client.get("/api/admin/flows", headers=ADMIN_HEADER).json()
    assert overview[0]["published"] is True

    created = citizen_create(client, container)
    assert created.status_code == 201, created.text
    assert created.json()["flow_id"] == "admin.demo"


def test_dry_run_does_not_move_publish_pointer(admin_client) -> None:
    client, container = admin_client
    create_version(client)
    result = publish(client, "1", apply=False)
    assert result.status_code == 200, result.text
    assert result.json()["published"] is False
    assert "尚未发布" in result.json()["message"]
    assert client.get("/api/admin/flows", headers=ADMIN_HEADER).json()[0]["published"] is False
    assert citizen_create(client, container).status_code == 422


def test_publishing_unknown_version_is_a_clear_error(admin_client) -> None:
    client, _ = admin_client
    create_version(client)
    response = publish(client, "99")
    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "admin_flow_not_found"


# ---------- 失败：门禁拦下时指针不动，且给出完整报告 ----------


def test_release_gate_blocks_publish_without_moving_pointer(admin_client) -> None:
    client, container = admin_client
    create_version(client, "1")
    created = client.post(
        "/api/admin/flows/admin.demo/versions/1/cases",
        headers=ADMIN_HEADER,
        json={
            "case_id": "wrong_expectation",
            "slots": {"subject": "自检"},
            # 回放里带副作用的工具恒按未确认记账，要求 completed 必然 fail。
            "expect": {"steps": ["collect", "submit"], "terminal": "completed"},
        },
    )
    assert created.status_code == 201, created.text

    blocked = publish(client, "1")
    assert blocked.status_code == 409, blocked.text
    detail = blocked.json()["detail"]
    assert detail["code"] == "admin_release_blocked"
    assert detail["report"]["verdict"] == "fail"
    assert detail["report"]["results"][0]["steps"]  # 逐 step 观测随报告返回

    assert client.get("/api/admin/flows", headers=ADMIN_HEADER).json()[0]["published"] is False
    assert citizen_create(client, container).status_code == 422


def test_replay_endpoint_returns_per_step_results_and_verdict(admin_client) -> None:
    client, _ = admin_client
    create_version(client)
    client.post(
        "/api/admin/flows/admin.demo/versions/1/cases",
        headers=ADMIN_HEADER,
        json={
            "case_id": "happy",
            "slots": {"subject": "自检"},
            "expect": {"steps": ["collect", "submit"], "tools": ["replay_submit"]},
        },
    )
    report = client.post(
        "/api/admin/flows/admin.demo/versions/1/replay", headers=VIEWER_HEADER
    )
    assert report.status_code == 200, report.text
    body = report.json()
    assert body["verdict"] in {"pass", "fail"}
    assert body["results"][0]["case_id"] == "happy"
    assert body["results"][0]["steps"]
    assert body["results"][0]["tools"][0]["replay"] is True


def test_cases_can_be_added_and_deleted_without_overwriting(admin_client) -> None:
    client, _ = admin_client
    create_version(client)
    case_body = {"case_id": "happy", "slots": {}, "expect": {}}
    path = "/api/admin/flows/admin.demo/versions/1/cases"
    assert client.post(path, headers=ADMIN_HEADER, json=case_body).status_code == 201
    assert client.post(path, headers=ADMIN_HEADER, json=case_body).status_code == 409
    assert (
        client.delete(f"{path}/happy", headers=ADMIN_HEADER).json()["deleted"] is True
    )
    assert client.delete(f"{path}/happy", headers=ADMIN_HEADER).status_code == 404
    assert (
        client.get(
            "/api/admin/flows/admin.demo/versions/1", headers=ADMIN_HEADER
        ).json()["cases"]
        == []
    )


# ---------- 恢复：停用与回滚 ----------


def test_disable_stops_new_citizen_tasks(admin_client) -> None:
    client, container = admin_client
    create_version(client)
    assert publish(client, "1").status_code == 200
    assert citizen_create(client, container).status_code == 201

    disabled = client.post("/api/admin/flows/admin.demo/disable", headers=ADMIN_HEADER)
    assert disabled.status_code == 200, disabled.text
    rejected = citizen_create(client, container)
    assert rejected.status_code == 422
    # 群众侧把 FlowNotRunnable 统一收成 request_rejected（既有语义，不在本 Issue 改）。
    assert rejected.json()["code"] == "request_rejected"
    # 历史版本仍在，回滚随时可用。
    assert len(client.get("/api/admin/flows", headers=ADMIN_HEADER).json()) == 1


def test_rollback_restores_old_version_without_breaking_inflight_tasks(
    admin_client,
) -> None:
    client, container = admin_client
    create_version(client, "1")
    create_version(client, "2")
    assert publish(client, "1").status_code == 200
    assert publish(client, "2").status_code == 200
    assert citizen_create(client, container).status_code == 201  # 在途任务落在 v2

    rolled = client.post(
        "/api/admin/flows/admin.demo/rollback",
        headers=ADMIN_HEADER,
        json={"version": "1"},
    )
    assert rolled.status_code == 200, rolled.text

    overview = client.get("/api/admin/flows", headers=ADMIN_HEADER).json()
    assert [item["published"] for item in overview] == [True, False]
    # 新任务立刻走旧版本。
    assert container.flows.resolve("admin.demo").version == "1"
    # 在途任务用 require(自己的版本) 继续推进：回滚不能把它打断。
    assert container.flows.require("admin.demo", "2").version == "2"

    with pytest.raises(FlowNotRunnable):
        container.flows.resolve("admin.demo", "2")


# ---------- 凭据不泄漏 ----------


def test_admin_credential_never_appears_in_responses(admin_client) -> None:
    client, _ = admin_client
    create_version(client)
    responses = [
        client.get("/api/admin/flows", headers=ADMIN_HEADER),
        client.get("/api/admin/flows/admin.demo/versions/1", headers=ADMIN_HEADER),
        publish(client, "1", apply=False),
    ]
    for response in responses:
        assert ADMIN_TOKEN not in response.text
        assert VIEWER_TOKEN not in response.text
        assert OPS_TOKEN not in response.text


def test_admin_ui_assets_do_not_embed_credentials() -> None:
    """管理侧凭据只能由运维运行时输入，不能写进构建产物或持久化存储。"""

    root = Path(__file__).resolve().parents[1] / "admin-ui"
    assert root.is_dir(), "管理侧前端目录缺失"
    # 只扫描会真正下发到浏览器的交付文件；文档里说明"不使用持久化存储"不算违规。
    files = [
        path
        for path in root.rglob("*")
        if path.is_file() and path.suffix in {".html", ".js", ".json"}
    ]
    assert len(files) >= 3, files
    forbidden = (ADMIN_TOKEN, VIEWER_TOKEN, OPS_TOKEN, "localStorage", "sessionStorage")
    for path in files:
        text = path.read_text(encoding="utf-8")
        for marker in forbidden:
            assert marker not in text, f"{path.name} 含凭据或持久化存储：{marker}"


# ---------- 发布署名（Issue #61）----------


def test_publish_result_and_overview_carry_authenticated_actor(admin_client) -> None:
    """署名取已鉴权主体；接口结果与版本列表都能读到，事后可追责。"""

    client, _ = admin_client
    create_version(client)
    result = publish(client, "1")
    assert result.status_code == 200, result.text
    assert result.json()["published_by"] == "boss"

    overview = client.get("/api/admin/flows", headers=ADMIN_HEADER).json()
    assert overview[0]["published"] is True
    assert overview[0]["published_by"] == "boss"
    assert overview[0]["disabled_by"] is None


def test_forged_published_by_in_request_body_is_ignored(admin_client) -> None:
    """请求体自报 published_by / actor 一律无效：可自报的署名等于没有署名。"""

    client, _ = admin_client
    create_version(client)
    forged = client.post(
        "/api/admin/flows/admin.demo/publish",
        headers=ADMIN_HEADER,
        json={
            "version": "1",
            "apply": True,
            "published_by": "attacker",
            "actor": "attacker",
        },
    )
    assert forged.status_code == 200, forged.text
    assert forged.json()["published_by"] == "boss"
    assert "attacker" not in forged.text

    overview = client.get("/api/admin/flows", headers=ADMIN_HEADER).json()
    assert overview[0]["published_by"] == "boss"


def test_dry_run_reports_no_publisher(admin_client) -> None:
    """预演没移动指针，也就没有「发布人」，不能显示成已发布。"""

    client, _ = admin_client
    create_version(client)
    result = publish(client, "1", apply=False)
    assert result.status_code == 200, result.text
    assert result.json()["published"] is False
    assert result.json()["published_by"] is None
    overview = client.get("/api/admin/flows", headers=ADMIN_HEADER).json()
    assert overview[0]["published_by"] is None


def test_rollback_result_carries_actor_and_ignores_forgery(admin_client) -> None:
    """回滚同样留下 published_by（谁把 v2 换回 v1），且伪造字段无效。"""

    client, _ = admin_client
    create_version(client, "1")
    create_version(client, "2")
    assert publish(client, "2").status_code == 200

    rolled = client.post(
        "/api/admin/flows/admin.demo/rollback",
        headers=ADMIN_HEADER,
        json={"version": "1", "published_by": "attacker", "actor": "attacker"},
    )
    assert rolled.status_code == 200, rolled.text
    assert rolled.json()["published_by"] == "boss"
    assert "attacker" not in rolled.text
    assert client.get("/api/admin/flows", headers=ADMIN_HEADER).json()[0]["published_by"] == "boss"


def test_disable_result_carries_actor_and_ignores_forgery(admin_client) -> None:
    """停用留下 disabled_by；带请求体的伪造字段也不能改署名。"""

    client, _ = admin_client
    create_version(client)
    assert publish(client, "1").status_code == 200

    disabled = client.post(
        "/api/admin/flows/admin.demo/disable",
        headers=ADMIN_HEADER,
        json={"disabled_by": "attacker", "actor": "attacker"},
    )
    assert disabled.status_code == 200, disabled.text
    assert disabled.json()["disabled_by"] == "boss"
    assert "attacker" not in disabled.text

    overview = client.get("/api/admin/flows", headers=ADMIN_HEADER).json()
    assert overview[0]["published"] is False
    assert overview[0]["disabled_by"] == "boss"
    # 上一个发布者仍可查：停用不抹掉发布责任。
    assert overview[0]["published_by"] == "boss"


def test_viewer_cannot_forge_an_actor_to_gain_write(admin_client) -> None:
    """viewer 带伪造署名仍是 403：署名不是权限来源。"""

    client, _ = admin_client
    create_version(client)
    response = client.post(
        "/api/admin/flows/admin.demo/publish",
        headers=VIEWER_HEADER,
        json={"version": "1", "published_by": "boss", "actor": "boss"},
    )
    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "admin_forbidden"
