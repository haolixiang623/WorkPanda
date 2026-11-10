"""群众侧最小对话页（Issue #77）的回归集。

这一层要守的不是"页面好不好看"，而是四类会真出事的东西：

1. **状态只有一个来源**。页面显示的"办完了"必须来自服务端 `ui_state`。只要
   `state_view` 里出现时钟、定时器、动画帧，就说明有人开始按"等待结束"推断
   结果了——那是最危险的一类错误，所以在源码层面直接禁止这些入口。
2. **认不出的状态不许落到"已完成"**。服务端先上线新状态时，页面必须保守成
   "状态待确认"，而不是默认成功。
3. **「我已完成」不是确认凭证**。它只能走 `/answers`；请求体里出现
   `step_id`/`accepted` 就等于在群众页上开了一条签发捷径（D3 红线）。
4. **页面不持有凭据、不读 URL**。构建产物随仓库交付，任何一处把令牌或业务字段
   写进 URL、持久化存储或调试输出，都会跟着产物一起发出去。

页面本身以手工走查为主（见 `web/README.md`）；这里能自动化的是纯投影函数
（Node 直接加载编译产物 `dist/state_view.js`）、静态托管的开合、以及视图接口
是否真的把页面要的东西给全了。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from workpanda.bootstrap import build_container
from workpanda.config import Settings
from workpanda.domain.flow import (
    FlowDefinition,
    FlowStatus,
    StepDefinition,
    StepKind,
    UserActionSpec,
)
from workpanda.domain.models import Task, TaskAnswerRequest, TaskCreateRequest, TaskView
from workpanda.infrastructure.adapters import ScriptedExecutor
from workpanda.infrastructure.demo_flows import demo_flows
from workpanda.main import create_app

REPO_ROOT = Path(__file__).resolve().parents[1]
WEB_DIR = REPO_ROOT / "web"

SUBJECT = "session-web"

# 与 `domain/presentation.py` 的 UiState 一一对应。多一个少一个都要先改那张表。
SERVER_STATES = (
    "idle",
    "received",
    "processing",
    "needs_clarification",
    "awaiting_input",
    "awaiting_action",
    "awaiting_confirmation",
    "completed",
    "failed",
    "outcome_unconfirmed",
    "handoff",
    "cancelled",
)

# 带上字段标签的流程：页面上的"报修事项"来自作者，不是页面猜的。
LABELLED_FLOW = FlowDefinition(
    flow_id="web.repair",
    version="1",
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


def build_container_web() -> tuple[object, TestClient]:
    settings = Settings(store_backend="memory", enable_demo_flow=False)
    container = build_container(
        settings,
        flows=[LABELLED_FLOW],
        executor=ScriptedExecutor(),
    )
    return container, TestClient(create_app(container=container))


def create_labelled_task(container: object) -> Task:
    return container.tasks.create_task(  # type: ignore[attr-defined]
        TaskCreateRequest(
            conversation_id="conversation-web",
            input_text="我要报修",
            flow_id="web.repair",
            flow_version="1",
        ),
        SUBJECT,
    )


# ---------- 静态托管：默认只挂回环 ----------


def test_page_is_served_by_default() -> None:
    """默认配置（回环绑定）下，打开服务根就是群众页。"""

    client = TestClient(create_app(Settings(store_backend="memory", enable_demo_flow=False)))
    root = client.get("/", follow_redirects=False)
    assert root.status_code == 307
    assert root.headers["location"] == "/web/"

    page = client.get("/web/")
    assert page.status_code == 200
    assert "办事助手" in page.text
    assert '<script type="module" src="./dist/main.js">' in page.text

    bundle = client.get("/web/dist/main.js")
    assert bundle.status_code == 200
    assert "javascript" in bundle.headers["content-type"]


@pytest.mark.parametrize(
    ("overrides", "served"),
    [
        ({"bind_host": "0.0.0.0"}, False),
        ({"bind_host": "0.0.0.0", "web_ui_expose_public": True}, True),
        ({"web_ui_enabled": False}, False),
    ],
    ids=["对外绑定默认不挂", "显式开公网", "总开关关闭"],
)
def test_page_mount_follows_loopback_rule(overrides: dict, served: bool) -> None:
    """页面会给任何访问者签发会话，所以对外可达必须是一次显式决定。"""

    settings = Settings(store_backend="memory", enable_demo_flow=False, **overrides)
    client = TestClient(create_app(settings))
    response = client.get("/web/", follow_redirects=False)
    assert (response.status_code == 200) is served
    assert (client.get("/", follow_redirects=False).status_code == 307) is served


def test_api_404_keeps_json_shape() -> None:
    """挂载静态目录不能把 `/api` 下的拼写错误变成纯文本 404。"""

    client = TestClient(create_app(Settings(store_backend="memory", enable_demo_flow=False)))
    response = client.get("/api/no-such-endpoint")
    assert response.status_code == 404
    assert response.headers["content-type"].startswith("application/json")


def test_explicit_web_dir_without_index_fails_at_startup() -> None:
    """配了目录却没有 index.html = 部署写错了，启动就该失败。"""

    with tempfile.TemporaryDirectory() as empty:
        with pytest.raises(ValueError, match="index.html"):
            create_app(
                Settings(
                    store_backend="memory",
                    enable_demo_flow=False,
                    web_ui_dir=Path(empty),
                )
            )


# ---------- 视图接口：页面要的东西一次给全 ----------


def test_task_view_carries_step_copy_labels_and_slots() -> None:
    """字段中文名、步骤说明、确认文案都由服务端给，页面不自己编。"""

    container, _ = build_container_web()
    try:
        task = create_labelled_task(container)
        view = TaskView.from_task(task, container.flows)

        assert view.ui_state == "awaiting_input"
        assert view.pending_fields == ["subject", "contact"]
        assert view.current_step_title == "收集报修信息"
        assert view.current_step_prompt == "请说一下要报修什么，再留一个联系电话。"
        # 标签覆盖这一版声明过的**全部**字段，不只是待填的那几个。
        assert view.field_labels == {"subject": "报修事项", "contact": "联系电话"}
        assert view.ui_title == "需要您补充信息"
        assert view.ui_hint

        answered = container.tasks.answer(
            task.task_id,
            TaskAnswerRequest(values={"subject": "厨房水管漏水", "contact": "13800000000"}),
            SUBJECT,
        )
        confirm = TaskView.from_task(answered, container.flows)
        # 确认页此刻没有待填字段，但摘要照样要能读出人话。
        assert confirm.ui_state == "awaiting_confirmation"
        assert confirm.pending_fields == []
        assert confirm.field_labels == {"subject": "报修事项", "contact": "联系电话"}
        assert confirm.slots == {"subject": "厨房水管漏水", "contact": "13800000000"}
        assert confirm.current_step_title == "提交报修工单"
        assert confirm.confirm_message == "提交后维修人员会在三个工作日内联系您。"
    finally:
        container.close()


def test_task_view_survives_flow_version_missing_from_registry() -> None:
    """历史任务指向库里没有的版本时，少显示几句作者文案，而不是整条接口 500。"""

    container, _ = build_container_web()
    try:
        orphan = Task(
            conversation_id="conversation-web",
            flow_id="web.repair",
            flow_version="0",
            current_step="collect",
            pending_fields=["subject"],
        )
        view = TaskView.from_task(orphan, container.flows)
        assert view.current_step_title is None
        assert view.current_step_prompt is None
        assert view.field_labels == {}
        # 状态翻译与它无关，仍然要给出来。
        assert view.ui_state == "received"
    finally:
        container.close()


def test_refresh_needs_the_same_session() -> None:
    """刷新恢复的前提：按 task_id 查任务要带同一个会话，换会话就当不存在。"""

    container, client = build_container_web()
    try:
        created = client.post(
            "/api/tasks",
            json={
                "conversation_id": "conversation-web",
                "input_text": "我要报修",
                "flow_id": "web.repair",
                "flow_version": "1",
            },
        )
        assert created.status_code == 201, created.text
        session = created.headers["X-WorkPanda-Session"]
        task_id = created.json()["task_id"]

        restored = client.get(f"/api/tasks/{task_id}", headers={"X-WorkPanda-Session": session})
        assert restored.status_code == 200, restored.text
        assert restored.json()["task_id"] == task_id
        assert restored.json()["current_step_title"] == "收集报修信息"

        stranger = client.get(f"/api/tasks/{task_id}", headers={"X-WorkPanda-Session": "another"})
        assert stranger.status_code in {401, 404}
    finally:
        container.close()


def test_walkthrough_create_answer_confirm_complete() -> None:
    """页面的正常路径：创建 → 澄清（一次一个字段）→ 确认 → 执行 → 完成。

    这条用例走的就是 `web/src/main.ts` 发出的那串请求。手工走查照此复现即可
    （见 `web/README.md`），自动化这边守住"每一步的状态确实是服务端说的那个"。
    """

    container, client = build_container_web()
    try:
        created = client.post(
            "/api/tasks",
            json={
                "conversation_id": "conversation-web",
                "input_text": "我要报修",
                "flow_id": "web.repair",
                "flow_version": "1",
            },
        )
        assert created.status_code == 201, created.text
        headers = {"X-WorkPanda-Session": created.headers["X-WorkPanda-Session"]}
        task_id = created.json()["task_id"]

        first = created.json()
        assert first["ui_state"] == "awaiting_input"
        assert first["pending_fields"] == ["subject", "contact"]

        answered = client.post(
            f"/api/tasks/{task_id}/answers",
            json={"values": {"subject": "厨房水管漏水"}},
            headers=headers,
        )
        assert answered.status_code == 200, answered.text
        # 一次只问一个：补上一个字段之后，服务端才把下一个列出来。
        assert answered.json()["pending_fields"] == ["contact"]
        assert answered.json()["ui_state"] == "awaiting_input"

        filled = client.post(
            f"/api/tasks/{task_id}/answers",
            json={"values": {"contact": "13800000000"}},
            headers=headers,
        )
        assert filled.status_code == 200, filled.text
        confirmation = filled.json()
        assert confirmation["ui_state"] == "awaiting_confirmation"
        assert confirmation["pending_fields"] == []
        assert confirmation["slots"] == {"subject": "厨房水管漏水", "contact": "13800000000"}

        done = client.post(
            f"/api/tasks/{task_id}/confirm",
            json={"step_id": "submit", "accepted": True},
            headers=headers,
        )
        assert done.status_code == 200, done.text
        assert done.json()["ui_state"] == "completed"
        assert done.json()["result"] == {"tool": "web.submit", "step_id": "submit"}
    finally:
        container.close()


def test_walkthrough_i_finished_over_http() -> None:
    """「需要本人操作」全链路：暂停 → 我已完成 → 复检 → 完成。

    走的是 `web/src/main.ts` 真实发出的那串请求。这里盯三件页面拿不到第二份答案
    的事：三要素是否由服务端给全、「我已完成」是否用 `/answers` 而不是 `/confirm`、
    复检结果是否才是判定依据。
    """

    flow = FlowDefinition(
        flow_id="web.verify",
        version="1",
        title="需要本人核验",
        status=FlowStatus.PUBLISHED,
        steps=(
            StepDefinition(
                step_id="collect",
                kind=StepKind.COLLECT,
                title="收集信息",
                required_fields=("subject",),
                field_labels={"subject": "办理事项"},
            ),
            StepDefinition(
                step_id="login",
                kind=StepKind.USER_ACTION,
                title="在登录页输入图形验证码",
                user_action=UserActionSpec(
                    what="在登录页输入图片里的字符",
                    where="社保网上办事大厅",
                    expires_in_seconds=120,
                    check_step="verify",
                ),
            ),
            StepDefinition(
                step_id="verify",
                kind=StepKind.EXECUTE,
                title="复检登录状态",
                tool="web.verify",
                answer_label="核验结果",
            ),
        ),
    )
    container = build_container(
        Settings(store_backend="memory", enable_demo_flow=False),
        flows=[flow],
        executor=ScriptedExecutor(),
    )
    client = TestClient(create_app(container=container))
    try:
        created = client.post(
            "/api/tasks",
            json={
                "conversation_id": "conversation-web",
                "input_text": "我要核验",
                "flow_id": "web.verify",
                "flow_version": "1",
                "slots": {"subject": "灵活就业补贴"},
            },
        )
        assert created.status_code == 201, created.text
        headers = {"X-WorkPanda-Session": created.headers["X-WorkPanda-Session"]}
        task_id = created.json()["task_id"]
        first = created.json()

        assert first["ui_state"] == "awaiting_action"
        assert first["i_finished_allowed"] is True
        guide = first["user_action"]
        assert guide["what"] == "在登录页输入图片里的字符"
        assert guide["where"] == "社保网上办事大厅"
        assert 0 < guide["remaining_seconds"] <= 120
        assert guide["awaiting_check"] is False

        # 与页面完全一致：只有 values，没有 step_id/accepted。
        done = client.post(
            f"/api/tasks/{task_id}/answers",
            json={"values": {"i_finished": "done"}},
            headers=headers,
        )
        assert done.status_code == 200, done.text
        final = done.json()
        assert final["ui_state"] == "completed"
        # 复检那一步真的跑过：结果来自它，不是"用户说完成了"。
        assert final["completed_steps"] == ["collect", "login", "verify"]
    finally:
        container.close()


def test_i_finished_cannot_be_sent_as_a_confirmation() -> None:
    """把 `i_finished` 发到 `/confirm` 会被拒：两条通道的语义不能互相顶替。"""

    container, client = build_container_web()
    try:
        created = client.post(
            "/api/tasks",
            json={
                "conversation_id": "conversation-web",
                "input_text": "我要报修",
                "flow_id": "web.repair",
                "flow_version": "1",
                "slots": {"subject": "厨房水管漏水", "contact": "13800000000"},
            },
        )
        headers = {"X-WorkPanda-Session": created.headers["X-WorkPanda-Session"]}
        task_id = created.json()["task_id"]
        assert created.json()["ui_state"] == "awaiting_confirmation"

        # 确认必须点名步骤；拿信号值冒充步骤号，服务端不认。
        spoof = client.post(
            f"/api/tasks/{task_id}/confirm",
            json={"step_id": "i_finished", "accepted": True},
            headers=headers,
        )
        assert spoof.status_code == 409, spoof.text
        assert client.get(f"/api/tasks/{task_id}", headers=headers).json()["ui_state"] == (
            "awaiting_confirmation"
        )
    finally:
        container.close()


def test_demo_flow_labels_every_field_it_asks_for() -> None:
    """凡是让群众填的字段，都得有一个能读的名字。

    群众侧只按 `field_labels` 显示，缺标签就回退成内部字段名。机制早就有了，
    但 `demo.generic` 一直没接线——真实浏览器里那一屏印的就是 `subject`。
    """

    for flow in demo_flows():
        for step in flow.steps:
            for name in step.required_fields:
                assert step.field_labels.get(name), (
                    f"{flow.flow_id}/{step.step_id}/{name} 缺 field_labels"
                )


def test_page_declares_an_icon_so_no_favicon_404() -> None:
    """不给浏览器留一个必然 404 的 `/favicon.ico`。

    走查时控制台里唯一一条错误就是它；页面在 `/web` 挂载下，根路径的 favicon
    永远打不到，声明一个内联空图标比补一个静态文件更省事。
    """

    html = (WEB_DIR / "index.html").read_text(encoding="utf-8")
    assert re.search(r'<link rel="icon" href="data:', html), html[:200]


# ---------- 纯投影函数（编译产物） ----------

_NODE_PROBE = """
import { readFileSync } from "node:fs";
const source = readFileSync(process.env.WP77_MODULE, "utf8");
const mod = await import(
  "data:text/javascript;base64," + Buffer.from(source).toString("base64")
);

const base = {
  task_id: "t1",
  conversation_id: "c1",
  flow_id: "web.repair",
  flow_version: "1",
  flow_title: "报修",
  status: "waiting_for_user",
  current_step: "collect",
  current_step_title: "收集报修信息",
  current_step_prompt: "请说一下要报修什么。",
  confirm_message: "提交后维修人员会联系您。",
  completed_steps: ["collect"],
  pending_fields: ["subject"],
  field_labels: { subject: "报修事项", contact: "联系电话" },
  sensitive_fields: ["contact"],
  slots: { subject: "厨房水管漏水", contact: "13800000000" },
  result: { receipt_no: "R-1", _internal: "x", detail: { nested: 1 } },
  failure_code: null,
  failure_message: null,
  user_action: null,
  i_finished_allowed: false,
  ui_state: "awaiting_input",
  ui_title: "服务端标题",
  ui_hint: "服务端提示",
  updated_at: "2026-01-01T00:00:00Z",
};

const view = (overrides) => ({ ...base, ...overrides });

const shape = (screen) => ({
  state: screen.state,
  title: screen.title,
  hint: screen.hint,
  field: screen.field === null ? null : screen.field.name,
  fieldLabel: screen.field === null ? null : screen.field.label,
  fieldValue: screen.field === null ? null : screen.field.value,
  summary: screen.summary.map((row) => row.label + "=" + row.value),
  impact: screen.impact,
  guide: screen.guide,
  actions: screen.actions.map((action) => action.kind),
  blocked: screen.blockedReason !== null,
  terminal: screen.terminal,
  hasPanel: mod.panelHasContent(screen),
});

const out = { states: {}, requests: {}, notices: {} };

for (const state of mod.UI_STATES) {
  out.states[state] = shape(
    mod.screenOf(view({ ui_state: state, ui_title: "标题:" + state, ui_hint: "提示:" + state })),
  );
}

// 「在等填表但没有待填字段」不再被页面擅自解释成"请去做件事"：服务端说在等
// 填表，页面就显示填表（没有可填的字段时只留取消）。这条曾经是派生逻辑的入口。
out.states["awaiting_input_empty_pending"] = shape(
  mod.screenOf(
    view({
      ui_state: "awaiting_input",
      pending_fields: [],
      ui_title: "标题:ui",
      ui_hint: "提示:ui",
    }),
  ),
);

// 真实的「需要用户本人操作」：三要素与剩余时间都来自服务端。
out.states["awaiting_action_fresh"] = shape(
  mod.screenOf(
    view({
      ui_state: "awaiting_action",
      status: "waiting_for_action",
      pending_fields: [],
      user_action: {
        what: "在登录页输入图片里的字符",
        where: "社保网上办事大厅",
        remaining_seconds: 150,
        awaiting_check: false,
      },
      i_finished_allowed: true,
    }),
  ),
);
out.states["awaiting_action_expired"] = shape(
  mod.screenOf(
    view({
      ui_state: "awaiting_action",
      status: "waiting_for_action",
      pending_fields: [],
      user_action: {
        what: "在登录页输入图片里的字符",
        where: "社保网上办事大厅",
        remaining_seconds: 0,
        awaiting_check: false,
      },
      i_finished_allowed: true,
    }),
  ),
);
out.states["awaiting_action_checking"] = shape(
  mod.screenOf(
    view({
      ui_state: "awaiting_action",
      status: "waiting_for_action",
      pending_fields: [],
      user_action: {
        what: "在登录页输入图片里的字符",
        where: "社保网上办事大厅",
        remaining_seconds: 120,
        awaiting_check: true,
      },
      i_finished_allowed: true,
    }),
  ),
);
out.states["awaiting_action_without_guide"] = shape(
  mod.screenOf(
    view({
      ui_state: "awaiting_action",
      status: "waiting_for_action",
      pending_fields: [],
      user_action: null,
    }),
  ),
);
out.states["awaiting_input_without_prompt"] = shape(
  mod.screenOf(
    view({
      ui_state: "awaiting_input",
      current_step_prompt: null,
      ui_hint: "提示:缺省",
    }),
  ),
);
out.states["unknown_state"] = shape(mod.screenOf(view({ ui_state: "brand_new_state" })));
out.states["no_task"] = shape(mod.screenOf(null));

out.requests.answer = mod.answerRequest({ name: "contact", label: "联系电话", value: "1" });
out.requests.finished = mod.finishedRequest();
out.requests.confirm = mod.confirmRequest(
  view({ ui_state: "awaiting_confirmation", current_step: "submit" }),
);
out.requests.canSubmitEmpty = mod.canSubmit({ name: "a", label: "A", value: "  " });
out.requests.canSubmitFilled = mod.canSubmit({ name: "a", label: "A", value: "x" });
out.requests.collected = mod.collected(view({})).map((row) => row.label + "=" + row.value);
out.requests.resultRows = mod.resultRows(
  view({ ui_state: "completed" }).result,
).map((row) => row.label + "=" + row.value);

out.notices.live = mod.connectionNotice("live");
out.notices.reconnecting = mod.connectionNotice("reconnecting");
out.notices.offline = mod.connectionNotice("offline");

// ---------- 对话记录（B2） ----------

const flat = (messages) =>
  messages.map((message) => [message.kind, message.role, message.text]);

out.transcript = {};
for (const state of mod.UI_STATES) {
  out.transcript[state] = flat(
    mod.transcriptOf(
      view({
        ui_state: state,
        ui_title: "标题:" + state,
        ui_hint: "提示:" + state,
        // 澄清态服务端还没建任务：没有字段、没有槽位。
        ...(state === "needs_clarification"
          ? { pending_fields: [], slots: {}, current_step: null, current_step_title: null }
          : {}),
      }),
    ),
  );
}
out.transcript.unknown_state = flat(mod.transcriptOf(view({ ui_state: "brand_new_state" })));
out.transcript.no_task = flat(mod.transcriptOf(null));
out.transcript.no_flow_title = flat(mod.transcriptOf(view({ flow_title: null })));

// 幂等：同一视图派生两次必须深度相等（证明没有内部计数器）。
const twice = view({ ui_state: "awaiting_confirmation" });
out.transcript.idempotent =
  JSON.stringify(mod.transcriptOf(twice)) === JSON.stringify(mod.transcriptOf(twice));

// id 不许重复——重复的 id 会让 DOM 复用把两条消息合成一条。
const ids = mod.transcriptOf(view({ ui_state: "awaiting_confirmation" })).map((m) => m.id);
out.transcript.ids = ids;
out.transcript.idsUnique = new Set(ids).size === ids.length;
// 调用十次，条数不增长。
let last = 0;
for (let i = 0; i < 10; i += 1) last = mod.transcriptOf(twice).length;
out.transcript.stableLength = last === ids.length;

// 打码：同一份视图，声明与不声明 sensitive_fields 的差别。
out.mask = {
  long: mod.maskValue("320501199001011234"),
  phone: mod.maskValue("13800000000"),
  short: mod.maskValue("abc"),
  empty: mod.maskValue(""),
  declared: mod.displayValue(view({}), "contact"),
  undeclared: mod.displayValue(view({ sensitive_fields: [] }), "contact"),
  plain: mod.displayValue(view({}), "subject"),
  // 敏感值不得以明文出现在**任何**消息里。
  leaks: flat(mod.transcriptOf(view({ ui_state: "awaiting_confirmation" })))
    .flat()
    .filter((cell) => typeof cell === "string" && cell.indexOf("13800000000") >= 0),
};

console.log(JSON.stringify(out));
"""


def _run_probe() -> dict:
    node = shutil.which("node")
    if node is None:
        pytest.skip("本机没有 node，跳过群众侧投影函数的回归")
    completed = subprocess.run(
        [node, "--input-type=module", "-e", _NODE_PROBE],
        env={**os.environ, "WP77_MODULE": str(WEB_DIR / "dist" / "state_view.js")},
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    return json.loads(completed.stdout)


def test_every_server_state_maps_to_its_own_screen() -> None:
    """11 个服务端状态各有各的一屏，不许落到同一屏或空屏。"""

    probe = _run_probe()
    expected = {
        "idle": "idle",
        "received": "received",
        "processing": "processing",
        "needs_clarification": "needs_clarification",
        "awaiting_input": "awaiting_input",
        "awaiting_confirmation": "awaiting_confirmation",
        "completed": "completed",
        "failed": "failed",
        "outcome_unconfirmed": "outcome_unconfirmed",
        "handoff": "handoff",
        "cancelled": "cancelled",
    }
    for state, screen in expected.items():
        assert probe["states"][state]["state"] == screen, state
    assert set(probe["states"]) >= set(expected)


def test_server_copy_is_shown_verbatim() -> None:
    """页面不维护自己的一套措辞：服务端说什么就显示什么。"""

    probe = _run_probe()
    for state in SERVER_STATES:
        screen = probe["states"][state]
        assert screen["title"] == "标题:" + state, state
        assert probe["states"][state]["title"] != ""


def test_unknown_state_never_falls_back_to_success() -> None:
    """服务端先上线了新状态：宁可说"待确认"，也不能说"已办完"。"""

    probe = _run_probe()
    assert probe["states"]["unknown_state"]["state"] == "outcome_unconfirmed"
    assert probe["states"]["unknown_state"]["actions"] == []


def test_result_rows_hide_internal_and_nested_values() -> None:
    """办结结果只印标量；`_` 开头的内部键与嵌套结构不上页面。"""

    probe = _run_probe()
    assert probe["requests"]["resultRows"] == ["receipt_no=R-1"]


def test_clarification_asks_one_labelled_field_at_a_time() -> None:
    """一次只问一个字段，字段名用作者给的标签，顺序听服务端的。"""

    probe = _run_probe()
    screen = probe["states"]["awaiting_input"]
    assert screen["field"] == "subject"
    assert screen["fieldValue"] == "厨房水管漏水"  # 已填过的值会回填，不让人重填
    assert screen["actions"] == ["answer", "cancel"]
    assert probe["requests"]["canSubmitEmpty"] is False
    assert probe["requests"]["canSubmitFilled"] is True


def test_clarification_shows_the_step_authors_prompt() -> None:
    """澄清屏要说"这一步补什么"，而不是一句通用的"请补充提示的内容"。

    `ui_hint` 是状态级的（"补齐后会继续办理"），步骤作者的 `prompt` 才回答"补什么"。
    真实浏览器走查里群众看到的正是那句通用文案，标签又是 `subject`——两项加起来
    等于让群众自己猜。
    """

    probe = _run_probe()
    assert probe["states"]["awaiting_input"]["hint"] == "请说一下要报修什么。"
    # 没有作者文案时回退到服务端的状态提示，不自造一句。
    assert probe["states"]["awaiting_input_without_prompt"]["hint"] == "提示:缺省"
    # 「等填表但没有待填字段」不再变成另一屏：它仍按填表屏处理（作者文案照用），
    # 只是没有可提交的字段时只留取消。页面不替服务端改写状态（#80）。
    assert probe["states"]["awaiting_input_empty_pending"]["hint"] == "请说一下要报修什么。"
    assert probe["states"]["awaiting_input_empty_pending"]["state"] == "awaiting_input"


def test_confirmation_shows_action_fields_and_impact() -> None:
    """确认页要能核对"提交什么"，并说明影响；不给默认确认的入口。"""

    probe = _run_probe()
    screen = probe["states"]["awaiting_confirmation"]
    assert screen["summary"] == ["报修事项=厨房水管漏水", "联系电话=13800000000"]
    assert screen["impact"] == "提交后维修人员会联系您。"
    assert screen["actions"] == ["confirm", "edit", "cancel"]
    # 确认凭证只能由 /confirm 签发，且必须显式带上步骤号。
    assert probe["requests"]["confirm"] == {"step_id": "submit", "accepted": True}


def test_user_action_screen_comes_from_the_server_state() -> None:
    """「需要用户本人操作」是服务端状态 `awaiting_action`，不是页面推出来的。

    #77 时服务端还没有这个状态，页面靠"在等填表但没待填字段"派生了一屏。那种
    派生有两个问题：一是页面在替服务端解释状态，二是服务端一旦给出"等填表"
    却没有字段的坏数据，页面会把它显示成"请去做件事"。这里把两件事分开断言。
    """

    probe = _run_probe()

    # 真实状态：三要素、剩余时间、按钮都来自服务端。
    fresh = probe["states"]["awaiting_action_fresh"]
    assert fresh["state"] == "awaiting_action"
    assert set(fresh["guide"]) == {"what", "where", "expires"}
    assert fresh["guide"]["what"] == "在登录页输入图片里的字符"
    assert fresh["guide"]["where"] == "社保网上办事大厅"
    assert fresh["guide"]["expires"] == "请在接下来的 2 分钟内完成"
    assert fresh["actions"] == ["i_finished", "cancel"]
    assert fresh["blocked"] is False
    # 标题也来自服务端（探针里这一屏没覆写 title，用的就是基线值）。
    assert fresh["title"] == "服务端标题"

    # 旧派生入口必须彻底消失：在等填表就是等填表，页面不改写它。
    derived = probe["states"]["awaiting_input_empty_pending"]
    assert derived["state"] == "awaiting_input"
    assert derived["guide"] is None
    assert "i_finished" not in derived["actions"]
    assert set(derived["actions"]) == {"cancel"}


def test_expired_action_offers_no_way_forward() -> None:
    """二维码/验证码过期：给出说明，收掉按钮，不报成功也不静默重试。"""

    probe = _run_probe()
    screen = probe["states"]["awaiting_action_expired"]
    assert screen["state"] == "awaiting_action"
    assert screen["guide"]["expires"] == "已超过有效期"
    # 没有「我已完成」可点：点下去服务端只会转人工，留个能点的按钮等于骗人。
    assert screen["actions"] == ["cancel"]
    assert screen["blocked"] is True
    assert "超过有效期" in screen["guide"]["expires"] or screen["blocked"] is True


def test_checking_state_hides_the_button_again() -> None:
    """已经点过了：显示"正在核实"，不再给重复入口（重复点只会 409）。"""

    probe = _run_probe()
    screen = probe["states"]["awaiting_action_checking"]
    assert screen["actions"] == ["cancel"]
    assert screen["blocked"] is True


def test_action_screen_without_guide_does_not_invent_a_where() -> None:
    """服务端没给指引时，页面不许拿步骤标题拼一个看起来像地址的东西。"""

    probe = _run_probe()
    screen = probe["states"]["awaiting_action_without_guide"]
    assert screen["state"] == "awaiting_action"
    assert screen["guide"]["where"] == "请按办理页面上的提示操作"
    assert screen["guide"]["expires"] == "有效期以办理页面上的提示为准"


def test_i_finished_never_builds_a_confirmation() -> None:
    """「我已完成」的请求体里只有 values：出现 step_id/accepted 就是签发捷径。"""

    probe = _run_probe()
    assert probe["requests"]["answer"] == {"values": {"contact": "1"}}
    assert set(probe["requests"]["confirm"]) == {"step_id", "accepted"}
    # 「我已完成」只带一个信号键：既不是字段值，也不可能被当成确认。
    finished = probe["requests"]["finished"]
    assert set(finished) == {"values"}
    assert finished["values"] == {"i_finished": "done"}
    assert "step_id" not in finished and "accepted" not in finished


def test_outcome_unconfirmed_offers_no_resubmit() -> None:
    """结果未知时不给"重新发起"：那等于引导群众重复提交一笔可能已受理的申请。"""

    probe = _run_probe()
    screen = probe["states"]["outcome_unconfirmed"]
    assert screen["actions"] == []
    assert screen["terminal"] is True


def test_cancel_and_handoff_render_as_non_success() -> None:
    probe = _run_probe()
    assert probe["states"]["cancelled"]["actions"] == ["restart"]
    assert probe["states"]["handoff"]["terminal"] is True
    assert probe["states"]["failed"]["actions"] == ["restart"]


def test_empty_action_panel_is_not_drawn_at_all() -> None:
    """操作区没东西可显示时，整块卡片不画——空卡片在手机上就是一块白板。

    `outcome_unconfirmed` 的 DOM 曾经是
    `<section class="card panel"><div class="actions"></div></section>`：没有动作时
    `actionBar()` 只剩一个空 `div`，渲染成一块白框，看着像"这里本该有内容但没加载
    出来"。`handoff` 同理（两者本来就不该给按钮）。

    判据不按状态名硬编码，而是与 `panel()` 真正会塞进卡片的那几样东西一一对应：
    将来给某个终态加一个按钮或一行摘要，这里自动跟着变。
    """

    probe = _run_probe()
    states = probe["states"]
    assert states["outcome_unconfirmed"]["hasPanel"] is False
    assert states["handoff"]["hasPanel"] is False
    # 落回"结果待确认"的未知状态同样不该有那块框。
    assert states["unknown_state"]["hasPanel"] is False
    # 别的屏都有话要说，不能被这条规则误伤成空白页。
    for state in (
        "idle",
        "needs_clarification",
        "awaiting_input",
        "awaiting_confirmation",
        "completed",
        "failed",
        "cancelled",
    ):
        assert states[state]["hasPanel"] is True, state


def test_completed_screen_leaves_the_result_to_the_transcript() -> None:
    """办结屏不在面板里再印一遍结论——结论只归对话流。

    `transcriptOf` 已经把 `result` 逐行放进了对话流；`screenOf` 的 `completed` 分支
    再返回一份 `summary`，`panel()` 就会把同一段话原样印第二遍。demo 流程的结论只有
    一行，看不出问题；换成真实模型的三段式答案（300+ 字）之后，390px 手机屏上同一
    段话出现两次，群众要读两遍、滚两遍。
    """

    probe = _run_probe()
    assert probe["states"]["completed"]["summary"] == []
    # 结论不能因此消失：必须还在对话流里，而且只出现一次。
    results = [
        text for kind, _, text in probe["transcript"]["completed"] if kind == "result"
    ]
    assert results == ["receipt_no：R-1"], results


def test_disconnection_is_a_banner_not_a_task_state() -> None:
    """断线只该是一条提示：任务状态仍以服务端最后一次答复为准。"""

    probe = _run_probe()
    assert probe["notices"]["live"] is None
    assert "任务可能仍在继续" in probe["notices"]["reconnecting"]["text"]
    assert probe["notices"]["offline"]["level"] == "offline"


# ---------- 对话记录（B2）：由 TaskView 派生，不累积本地数组 ----------


def test_transcript_starts_by_naming_the_service() -> None:
    """开场要说"您要办的是哪件事"。

    页面唯一的替代品是复述群众原话，而 `Task` 根本不保存 `input_text`——那句话
    在任何地方都取不回来。所以事项名只能由服务端下发（B1），缺失时不显示而不是
    印一个 `undefined`。
    """

    probe = _run_probe()
    first = probe["transcript"]["awaiting_input"][0]
    assert first[0] == "opening"
    assert first[2] == "您要办的是：报修。"

    # 版本缺失（`flow_title: null`）时，开场退化成服务端状态提示这一条，
    # 不出现"您要办的是：undefined"。
    without = probe["transcript"]["no_flow_title"]
    assert all("您要办的是" not in text for _, _, text in without)
    assert "undefined" not in " ".join(text for _, _, text in without)


def test_transcript_asks_one_labelled_field_and_echoes_answers() -> None:
    """问答对用作者给的中文名，顺序听服务端的 `slots`。"""

    probe = _run_probe()
    messages = probe["transcript"]["awaiting_input"]
    kinds = [kind for kind, _, _ in messages]
    assert kinds[0] == "opening"
    # 已答的 subject 是一问一答；contact 在 `slots` 里也有值，因此同样成对出现。
    assert kinds.count("field_question") == 3  # subject + contact + 当前待填的 subject
    assert kinds.count("user_answer") == 2
    questions = [text for kind, _, text in messages if kind == "field_question"]
    assert "报修事项" in questions
    assert "联系电话" in questions
    # 当前待填的那个字段用的也是标签，不是字段名。
    assert questions[-1] == "报修事项"


def test_transcript_masks_sensitive_values_everywhere() -> None:
    """敏感字段的明文**不得**出现在任何一条消息里。

    页面不维护字段名黑名单：打码只看 `sensitive_fields`。这条用例连"消息文本里
    有没有残留"一起查，堵的是"某条消息绕过了 `displayValue`"。
    """

    probe = _run_probe()
    assert probe["mask"]["leaks"] == []
    assert probe["mask"]["declared"] != "13800000000"
    assert probe["mask"]["declared"].endswith("0000")
    # 未声明就明文。这是作者显式承担的默认值，不是页面在猜。
    assert probe["mask"]["undeclared"] == "13800000000"
    assert probe["mask"]["plain"] == "厨房水管漏水"
    # 长度 ≥ 12 保 6 头 4 尾；6–11 位只保后 4 位；4 位以内整串掩掉。
    assert probe["mask"]["long"] == "320501********1234"
    assert probe["mask"]["phone"] == "*******0000"
    assert probe["mask"]["short"] == "***"
    assert probe["mask"]["empty"] == ""


def test_transcript_is_idempotent_and_ids_are_stable() -> None:
    """派生幂等、id 不重复、条数不随调用次数增长。

    这三条合起来证明"页面没有自己记着进度"：本地累积的典型症状正是重连后消息
    翻倍、刷新后全丢。
    """

    probe = _run_probe()
    assert probe["transcript"]["idempotent"] is True
    assert probe["transcript"]["idsUnique"] is True
    assert probe["transcript"]["stableLength"] is True


def test_clarification_transcript_asks_no_field_questions() -> None:
    """没听清要办什么时问不出任何字段。

    此时服务端**没有建任务**，`pending_fields` 与 `slots` 都是空的，所以除了开场
    （事项名来自上一句话命中的流程版本）之外只有一条"没听清"的助手消息，一条
    `field_question` 都不该有——问了也没有地方收答案。
    """

    probe = _run_probe()
    messages = probe["transcript"]["needs_clarification"]
    kinds = [kind for kind, _, _ in messages]
    assert kinds == ["opening", "assistant"]
    assert messages[-1][2] == "提示:needs_clarification"
    assert "field_question" not in kinds
    assert "user_answer" not in kinds


def test_terminal_transcripts_say_what_actually_happened() -> None:
    """终态用服务端文案，且不再追加任何"进行中"的尾巴。"""

    probe = _run_probe()

    completed = probe["transcript"]["completed"]
    assert [kind for kind, _, _ in completed][-1] == "status"
    assert "进行中" not in " ".join(text for _, _, text in completed)

    unconfirmed = " ".join(text for _, _, text in probe["transcript"]["outcome_unconfirmed"])
    assert "标题:outcome_unconfirmed" in unconfirmed
    # "结果待确认"时**不能**出现重新提交类措辞。
    assert "重新提交" not in unconfirmed
    assert "请重新" not in unconfirmed

    handoff = " ".join(text for _, _, text in probe["transcript"]["handoff"])
    assert "标题:handoff" in handoff


def test_no_task_transcript_invites_a_description() -> None:
    """没有任务时也是一句"说您要办什么"，而不是空白页。"""

    probe = _run_probe()
    assert [kind for kind, _, _ in probe["transcript"]["no_task"]] == ["opening"]


def test_unknown_state_transcript_never_claims_success() -> None:
    """认不出的状态：派生落到"待确认"，不许有结果行。"""

    probe = _run_probe()
    kinds = [kind for kind, _, _ in probe["transcript"]["unknown_state"]]
    assert "result" not in kinds


# ---------- 源码纪律 ----------

_TIME_APIS = re.compile(
    r"\b(setTimeout|setInterval|requestAnimationFrame|performance\.now|Date\.now|new Date)\b"
)


def test_projection_layer_cannot_see_time() -> None:
    """投影层里不许出现时钟与定时器——"等动画结束就算完成"从这里根本写不出来。"""

    source = (WEB_DIR / "src" / "state_view.ts").read_text(encoding="utf-8")
    # 注释里说明"没有用定时器"不算违规；被扫的是代码本身。
    code = re.sub(r"/\*.*?\*/", "", source, flags=re.S)
    code = re.sub(r"//[^\n]*", "", code)
    assert _TIME_APIS.search(code) is None
    compiled = (WEB_DIR / "dist" / "state_view.js").read_text(encoding="utf-8")
    assert _TIME_APIS.search(compiled) is None


def test_page_never_reads_url_or_persistent_storage() -> None:
    """不把字段写进 URL、不落 localStorage、不打日志。"""

    forbidden = (
        "localStorage",
        "document.cookie",
        "console.",
        "location.",
        "location[",
        "history.",
    )
    files = sorted(WEB_DIR.glob("src/*.ts")) + sorted(WEB_DIR.glob("dist/*.js"))
    files.append(WEB_DIR / "index.html")
    assert len(files) >= 5, files
    for path in files:
        text = path.read_text(encoding="utf-8")
        for marker in forbidden:
            assert marker not in text, f"{path.name} 出现 {marker}"


def test_bundle_contains_no_credentials() -> None:
    """产物随仓库交付：任何令牌样式的串都不许写进去。"""

    markers = (
        "X-WorkPanda-Admin-Token",
        "X-WorkPanda-Ops-Token",
        "X-WorkPanda-Staff-Token",
        "secret",
    )
    for path in sorted(WEB_DIR.glob("dist/*.js")) + [WEB_DIR / "index.html"]:
        text = path.read_text(encoding="utf-8").lower()
        for marker in markers:
            assert marker.lower() not in text, f"{path.name} 含凭据痕迹：{marker}"


def test_page_nodes_exist_for_every_lookup() -> None:
    """页面里 `must("x")` 找的节点必须真的在 HTML 里，否则整页白屏。"""

    main_ts = (WEB_DIR / "src" / "main.ts").read_text(encoding="utf-8")
    page = (WEB_DIR / "index.html").read_text(encoding="utf-8")
    looked_up = set(re.findall(r'must\("([^"]+)"\)', main_ts))
    looked_up |= set(re.findall(r'getElementById\("([^"]+)"\)', main_ts))
    # B3 之后的骨架：消息流、操作面板、常驻输入框三段。
    assert {"stream", "panel", "composer"} <= looked_up, looked_up
    assert "connection" in looked_up
    for node_id in sorted(looked_up):
        # 动态创建的节点（输入框、消息）在渲染时才有，不要求出现在骨架里。
        if node_id in {"field-input", "composer-text", "composer-send"}:
            assert f'id = "{node_id}"' in main_ts
            continue
        assert f'id="{node_id}"' in page, f"# {node_id} 不在 index.html 里"
    # 旧骨架的 `#app` 已拆成两段：留着它只会让人以为页面还是整块重建。
    assert 'id="app"' not in page


def test_stream_is_appended_from_the_server_view_not_accumulated() -> None:
    """消息流只由 `transcriptOf` 派生，页面不持有本地数组。

    这条是 B2/B3 的共同底线：只要出现一个 module 级的消息数组，SSE 提醒与重连
    就会让它翻倍，而刷新就会让它全丢——两种症状都是"页面自己记着进度"。
    """

    main_ts = (WEB_DIR / "src" / "main.ts").read_text(encoding="utf-8")
    assert "transcriptOf(view)" in main_ts
    # 模块级不存消息：`syncStream` 的入参是派生结果，不是一份攒下来的历史。
    assert not re.search(r"^(let|const) \w+(: Message\[\])? = \[\]", main_ts, re.M)
    assert ".push(" not in main_ts.split("function syncStream")[0]
    # 输入框常驻：`#composer` 在骨架里，渲染函数只切换禁用态，不重建它。
    page = (WEB_DIR / "index.html").read_text(encoding="utf-8")
    assert 'id="composer"' in page
    assert "panelEl.replaceChildren" in main_ts
    assert "streamEl.replaceChildren" not in main_ts


def test_compiled_bundle_is_in_sync_with_source() -> None:
    """防"源码改了、产物没编译"：源码导出的符号必须都在产物里。"""

    source = (WEB_DIR / "src" / "state_view.ts").read_text(encoding="utf-8")
    compiled = (WEB_DIR / "dist" / "state_view.js").read_text(encoding="utf-8")
    exported = set(re.findall(r"export function (\w+)", source))
    exported |= set(re.findall(r"export const (\w+)", source))
    assert len(exported) >= 8, exported
    for name in sorted(exported):
        assert name in compiled, f"产物里缺 {name}：源码改了但没重新编译"

    main_ts = (WEB_DIR / "src" / "main.ts").read_text(encoding="utf-8")
    main_js = (WEB_DIR / "dist" / "main.js").read_text(encoding="utf-8")
    assert 'from "./state_view.js"' in main_ts
    assert 'from "./state_view.js"' in main_js
    # 事件只是"去问一次服务端"的提醒：状态赋值只能来自 GET 响应。
    assert "view = await request" in main_js
    # 确认凭证只有一个出口：任何第二处 `/confirm` 都是签发捷径。
    assert main_ts.count("/confirm") == 1
    assert main_js.count("/confirm") == 1
    # `/answers` 恰好两处，且两处都不能构造 `accepted`：
    #   1. 补一个字段（`answerRequest`）；
    #   2. 「我已完成」（`finishedRequest`，只有一个信号键）。
    # 两者走同一条通道正是设计意图——它让"我已完成"在结构上不可能变成确认凭证。
    # 一旦变成三处（=有人加了第三条写入路径），或某处出现 `accepted`，就该红。
    assert main_ts.count("/answers") == 2
    assert main_js.count("/answers") == 2
    # `accepted` 只允许出现在 `state_view.ts` 的 `confirmRequest` 里，走到 `main.ts`
    # 一个字都不能有：那说明有人在自己拼确认请求。扫代码主体，不扫注释——注释里
    # 正是在解释"为什么不能带 accepted"。
    body = re.sub(r"/\*.*?\*/", "", main_ts, flags=re.S)
    body = re.sub(r"//[^\n]*", "", body)
    assert "accepted" not in body
    view_source = (WEB_DIR / "src" / "state_view.ts").read_text(encoding="utf-8")
    view_body = re.sub(r"/\*.*?\*/", "", view_source, flags=re.S)
    view_body = re.sub(r"//[^\n]*", "", view_body)
    # 两处：`ConfirmRequest` 的接口声明与 `confirmRequest` 的实现。多一处就是
    # 有人在自己拼确认请求。
    assert view_body.count("accepted") == 2, "签发请求只能由 confirmRequest 构造"
