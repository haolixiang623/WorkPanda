"""管理侧意图样例录入（Issue #62）的端到端与权威口径回归。

这一组守的是「界面建得出发不出去」这条老问题，拆成三件必须同时成立的事：

1. 表单提交的 `intent_samples` 必须真的落到版本里。曾经 `FlowDraftRequest` 没声明
   这个字段，pydantic 静默丢掉额外键，界面填了也白填，发布必吃 409；
2. 只用界面提交体建出的流程，能一路走完「建草稿 → 回放 → 发布成功」；
3. 本地条数只是提示。前端阈值与服务端阈值不一致（或被绕过）时，服务端 409
   仍是权威，发布指针不动。

另有两条前端行为回归：本地预检在样例不足时禁用保存并写明「缺哪一类、缺几条」，
且这份逻辑必须是编译产物里真正跑着的那一份，而不是源码里的死代码。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from workpanda.bootstrap import build_container
from workpanda.config import Settings
from workpanda.domain.intent import IntentRule, RuleIntentRouter
from workpanda.main import create_app

REPO_ROOT = Path(__file__).resolve().parents[1]
UI_DIR = REPO_ROOT / "admin-ui"

ADMIN_TOKEN = "admin-secret-0123456789"
ADMIN_HEADER = {"X-WorkPanda-Admin-Token": ADMIN_TOKEN}

# 与 admin-ui/src/main.ts 的 readDraft() 输出同形：未填写的可选字段是 undefined，
# JSON.stringify 会丢掉这些键，所以这里也只写界面真正会发出的键。
UI_COLLECT_STEP = {
    "step_id": "collect",
    "kind": "collect",
    "title": "收集信息",
    "required_fields": ["subject"],
    "prompt": "请描述需要报修的内容。",
    "requires_confirmation": False,
}
UI_EXECUTE_STEP = {
    "step_id": "submit",
    "kind": "execute",
    "title": "提交",
    "required_fields": [],
    "tool": "replay_submit",
    "requires_confirmation": True,
}

# 关键词是「报修」：正例都含它，近似误例 / 超范围例都不含，评测才能过。
UI_SAMPLES = [
    {"sample_id": "pos_1", "category": "positive", "text": "我要报修"},
    {"sample_id": "pos_2", "category": "positive", "text": "报修申请"},
    {"sample_id": "pos_3", "category": "positive", "text": "帮我报修一下"},
    {"sample_id": "near_1", "category": "near_miss", "text": "我要取消订单"},
    {"sample_id": "near_2", "category": "near_miss", "text": "查一下天气预报"},
    {"sample_id": "oos_1", "category": "out_of_scope", "text": "今天天气不错"},
]


def ui_draft(samples: list[dict] | None = None, **overrides: object) -> dict:
    """管理页「保存草稿」发出的请求体，字段与界面 readDraft() 一一对应。"""

    payload: dict = {
        "flow_id": "ui.repair",
        "version": "1",
        "title": "界面建出的报修流程",
        "steps": [dict(UI_COLLECT_STEP), dict(UI_EXECUTE_STEP)],
        "intent_samples": UI_SAMPLES if samples is None else samples,
    }
    payload.update(overrides)
    return payload


def ui_container(**settings_overrides: object) -> tuple[object, TestClient]:
    """带确定性路由器的容器：正例命中本流程这一版，其余样例不被路由。"""

    settings = Settings(
        _env_file=None,
        store_backend="memory",
        enable_demo_flow=False,
        admin_tokens=f"boss:admin:{ADMIN_TOKEN}",
        **settings_overrides,
    )
    router = RuleIntentRouter(
        rules=(
            IntentRule(
                intent_id="ui.repair.submit",
                flow_id="ui.repair",
                flow_version="1",
                keywords=("报修",),
            ),
        )
    )
    container = build_container(settings, router=router)
    return container, TestClient(create_app(container=container))


# ---------- 端到端：只用界面能建出并发布一个流程 ----------


def test_ui_created_flow_can_publish() -> None:
    """界面提交体建草稿（含样例）→ 回放 → 发布成功，且样例确实随版本落库。"""

    container, client = ui_container()
    try:
        created = client.post("/api/admin/flows", headers=ADMIN_HEADER, json=ui_draft())
        assert created.status_code == 201, created.text
        body = created.json()
        # 根因回归：服务端草稿请求体必须接住样例，不能静默丢弃。
        assert len(body["intent_samples"]) == len(UI_SAMPLES), body
        assert [item["sample_id"] for item in body["intent_samples"]] == [
            item["sample_id"] for item in UI_SAMPLES
        ]

        detail = client.get(
            "/api/admin/flows/ui.repair/versions/1", headers=ADMIN_HEADER
        )
        assert detail.status_code == 200, detail.text
        assert len(detail.json()["flow"]["intent_samples"]) == len(UI_SAMPLES)

        case = client.post(
            "/api/admin/flows/ui.repair/versions/1/cases",
            headers=ADMIN_HEADER,
            json={
                "case_id": "happy",
                "slots": {"subject": "自检"},
                "expect": {"steps": ["collect", "submit"], "tools": ["replay_submit"]},
            },
        )
        assert case.status_code == 201, case.text

        replay = client.post(
            "/api/admin/flows/ui.repair/versions/1/replay", headers=ADMIN_HEADER
        )
        assert replay.status_code == 200, replay.text
        assert replay.json()["publishable"] is True, replay.text

        published = client.post(
            "/api/admin/flows/ui.repair/publish",
            headers=ADMIN_HEADER,
            json={"version": "1", "apply": True},
        )
        assert published.status_code == 200, published.text
        outcome = published.json()
        assert outcome["published"] is True
        assert outcome["replay_ran"] is True
        assert container.flow_store is not None
        assert container.flow_store.published_versions() == {"ui.repair": "1"}
    finally:
        container.close()


def test_draft_request_pins_intent_sample_shape_errors() -> None:
    """重复 sample_id / 重复话术必须由服务端给出带 loc 的字段级错误。"""

    container, client = ui_container()
    try:
        duplicated = [
            {"sample_id": "pos_1", "category": "positive", "text": "我要报修"},
            {"sample_id": "pos_1", "category": "positive", "text": "另一句报修"},
        ]
        response = client.post(
            "/api/admin/flows",
            headers=ADMIN_HEADER,
            json=ui_draft(samples=duplicated),
        )
        assert response.status_code == 422, response.text
        detail = response.json()["detail"]
        assert detail["code"] == "admin_draft_invalid"
        assert ["intent_samples", 1, "sample_id"] in [
            field["loc"] for field in detail["fields"]
        ]
        assert client.get("/api/admin/flows", headers=ADMIN_HEADER).json() == []
    finally:
        container.close()


# ---------- 服务端仍是权威：前端阈值漂移也不能当作放行 ----------


def test_server_gate_is_authoritative_when_local_thresholds_drift() -> None:
    """前端 3 正例 / 2 近似误例认为「够了」，服务端阈值更高时仍必须 409 拦下。

    这是刻意的负向回归：界面本地预检只管「要不要先别提交」，一旦两边数字漂移，
    判定权归 `IntentReleaseGate`，浏览器不能因为自己算过就把发布放行。
    """

    container, client = ui_container(
        intent_eval_min_positive=5,
        intent_eval_min_near_miss=4,
    )
    try:
        created = client.post("/api/admin/flows", headers=ADMIN_HEADER, json=ui_draft())
        # 草稿本身允许先建流程、后补样例：服务端不在草稿阶段裁决条数。
        assert created.status_code == 201, created.text
        assert len(created.json()["intent_samples"]) == len(UI_SAMPLES)

        blocked = client.post(
            "/api/admin/flows/ui.repair/publish",
            headers=ADMIN_HEADER,
            json={"version": "1", "apply": True},
        )
        assert blocked.status_code == 409, blocked.text
        detail = blocked.json()["detail"]
        assert detail["code"] == "admin_intent_gate_blocked"
        assert "正例不足" in detail["detail"]
        coverage = {
            row["category"]: (row["have"], row["need"])
            for row in detail["report"]["coverage"]
        }
        assert coverage["positive"] == (3, 5)
        assert coverage["near_miss"] == (2, 4)
        assert detail["report"]["status"] == "fail"
        assert container.flow_store is not None
        assert container.flow_store.published_versions() == {}
    finally:
        container.close()


# ---------- 前端本地预检：按钮禁用与「缺哪一类、缺几条」 ----------

_NODE_PROBE = """
import { readFileSync } from "node:fs";
const source = readFileSync(process.env.WP62_MODULE, "utf8");
const mod = await import(
  "data:text/javascript;base64," + Buffer.from(source).toString("base64")
);
const { checkSamples, sampleGateDecision, LOCAL_MIN_POSITIVE, LOCAL_MIN_NEAR_MISS } = mod;
const blank = (category) => ({ sample_id: "", category, text: "" });
const full = [
  { sample_id: "p1", category: "positive", text: "我要报修" },
  { sample_id: "p2", category: "positive", text: "报修申请" },
  { sample_id: "p3", category: "positive", text: "帮我报修一下" },
  { sample_id: "n1", category: "near_miss", text: "我要取消订单" },
  { sample_id: "n2", category: "near_miss", text: "查一下天气预报" },
];
const scenarios = {
  empty: [blank("positive"), blank("positive"), blank("positive"),
          blank("near_miss"), blank("near_miss"), blank("out_of_scope")],
  full,
  duplicate: [...full, { sample_id: "p1", category: "positive", text: "再报修一次" }],
  partial: [{ sample_id: "p1", category: "positive", text: "" }, ...full.slice(1)],
};
const out = { constants: { positive: LOCAL_MIN_POSITIVE, nearMiss: LOCAL_MIN_NEAR_MISS } };
for (const [name, rows] of Object.entries(scenarios)) {
  const check = checkSamples(rows);
  const decision = sampleGateDecision(check);
  out[name] = {
    ready: check.ready,
    counts: check.counts,
    shortfalls: check.shortfalls,
    problems: check.problems,
    disabled: decision.disabled,
    className: decision.className,
    html: decision.html,
  };
}
console.log(JSON.stringify(out));
"""


def _run_node_probe() -> dict:
    node = shutil.which("node")
    if node is None:
        pytest.skip("本机没有 node，跳过前端预检的行为回归")
    completed = subprocess.run(
        [node, "--input-type=module", "-e", _NODE_PROBE],
        env={**os.environ, "WP62_MODULE": str(UI_DIR / "dist" / "sample_check.js")},
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    return json.loads(completed.stdout)


def test_local_sample_gate_disables_save_until_counts_met() -> None:
    """样例不足时按钮禁用并写明缺哪一类、缺几条；够了才放开。"""

    result = _run_node_probe()
    assert result["constants"] == {"positive": 3, "nearMiss": 2}

    empty = result["empty"]
    assert empty["ready"] is False
    assert empty["disabled"] is True
    assert empty["className"] == "warn"
    assert any("正例不足" in item and "还缺 3 条" in item for item in empty["shortfalls"])
    assert any("近似误例不足" in item and "还缺 2 条" in item for item in empty["shortfalls"])
    assert "已禁用" in empty["html"]
    # 全空的占位行只报缺口，不额外报「不能为空」，避免提示被形状错误淹没。
    assert empty["problems"] == []

    ready = result["full"]
    assert ready["ready"] is True
    assert ready["disabled"] is False
    assert ready["className"] == "ok"
    assert ready["counts"] == {"positive": 3, "near_miss": 2, "out_of_scope": 0}

    # 形状错误也要挡住提交，且定位到具体行，供 data-loc 标红。
    assert result["duplicate"]["problems"] == [
        {
            "loc": "intent_samples.5.sample_id",
            "message": "sample_id「p1」重复。",
        }
    ]
    assert result["duplicate"]["disabled"] is True
    assert result["partial"]["problems"][0]["loc"] == "intent_samples.0.text"


def test_compiled_bundle_wires_the_sample_gate() -> None:
    """防「源码改了、产物没编译」：交付的 bundle 必须真的接上预检与禁用逻辑。"""

    main_js = (UI_DIR / "dist" / "main.js").read_text(encoding="utf-8")
    page = (UI_DIR / "index.html").read_text(encoding="utf-8")
    assert 'from "./sample_check.js"' in main_js
    assert "checkSamples(payload.intent_samples)" in main_js
    assert 'el("save-draft").disabled = decision.disabled' in main_js
    assert "intent_samples:" in main_js
    assert 'id="samples"' in page
    assert 'id="sample-status"' in page
    assert 'id="add-sample-positive"' in page
    assert 'id="add-sample-near-miss"' in page
    assert 'id="add-sample-out-of-scope"' in page
    assert "409" in page  # 页面写明服务端 409 才是权威
