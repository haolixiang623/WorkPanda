"""识别规则属于流程版本（D2=B，#73）的回归。

规则若只活在装配代码里，"发布"就只移动了指针：运维看到"发布成功"，群众侧仍在按
旧规则识别，甚至被路由到一个 `resolve()` 必然拒绝的旧版本——#73 的现场就是这样，
报出来是 `FlowNotRunnable`，看起来像群众点错了点。这一组把"发布一次生效"钉成四件事：

1. 运行时路由器跟着发布指针走：发布 v2 之后新规则命中、旧规则失效，**不重启**；
2. 草稿版本的规则不参与路由：否则群众会被路由到一个不能执行的版本；
3. 规则指错版本在写流程时就被拦下，错误定位到 `intent_rules[i]`；
4. 发布门禁与运维脚本同一口径：门禁评"发布之后"的那台路由器，脚本只评已发布的版本。

第 4 条里"门禁自己装路由器"是这轮的根因修正：门禁原先拿的是运行时那台，它还不
认识待发布版本的规则，于是新版本的样例必然全判漏路由，谁也发不上去。
"""

from __future__ import annotations

import importlib.util
from collections.abc import Iterable
from pathlib import Path
from types import ModuleType

import pytest
from fastapi.testclient import TestClient

from workpanda.application import intent_routing
from workpanda.application.intent_eval import IntentReleaseGate
from workpanda.application.intent_routing import (
    RegistryIntentRouter,
    intent_rules_of,
    router_for,
)
from workpanda.bootstrap import Container, build_container
from workpanda.config import Settings
from workpanda.domain.flow import (
    FlowDefinition,
    FlowRegistry,
    FlowStatus,
    StepDefinition,
    StepKind,
    flow_field_errors,
)
from workpanda.domain.intent import IntentRule, RuleIntentRouter
from workpanda.domain.intent_eval import IntentEvalStatus
from workpanda.domain.intent_sample import IntentSample, IntentSampleCategory
from workpanda.infrastructure.memory_store import InMemoryFlowStore
from workpanda.main import create_app

POS = IntentSampleCategory.POSITIVE
NEAR = IntentSampleCategory.NEAR_MISS

ADMIN_TOKEN = "admin-secret-0123456789"
ADMIN_HEADER = {"X-WorkPanda-Admin-Token": ADMIN_TOKEN}

REPAIR = "我要报修"
REFUND = "我要退费"

_STEPS = (
    StepDefinition(
        step_id="collect",
        kind=StepKind.COLLECT,
        title="收集信息",
        required_fields=("subject",),
        prompt="请描述需要办理的事项。",
        requires_confirmation=False,
    ),
    StepDefinition(
        step_id="submit",
        kind=StepKind.EXECUTE,
        title="提交",
        required_fields=(),
        tool="test.submit",
        requires_confirmation=False,
    ),
)


def _flow(
    version: str,
    keyword: str,
    *,
    flow_id: str = "intent.repair",
    positives: tuple[str, ...] | None = None,
    near_miss: tuple[str, ...] = ("我要取消订单", "查一下天气预报"),
    rule_flow_id: str | None = None,
    rule_flow_version: str | None = None,
    rule_keywords: tuple[str, ...] | None = None,
) -> FlowDefinition:
    """一条带样例与规则的流程版本。正例都含 `keyword`，近似误例都不含。

    `rule_*` 三个参数用来故意写错规则，供"写流程时就该拦下"那组用例使用。
    """

    texts = positives or (f"我要{keyword}", f"申请{keyword}", f"帮我{keyword}一下")
    samples = tuple(
        [
            IntentSample(sample_id=f"pos_{i}", category=POS, text=text)
            for i, text in enumerate(texts, 1)
        ]
        + [
            IntentSample(sample_id=f"near_{i}", category=NEAR, text=text)
            for i, text in enumerate(near_miss, 1)
        ]
    )
    return FlowDefinition(
        flow_id=flow_id,
        version=version,
        title="报修申请",
        status=FlowStatus.DRAFT,
        steps=_STEPS,
        intent_samples=samples,
        intent_rules=(
            IntentRule(
                intent_id=f"{flow_id}.v{version}",
                flow_id=rule_flow_id or flow_id,
                flow_version=rule_flow_version or version,
                keywords=rule_keywords if rule_keywords is not None else (keyword,),
            ),
        ),
    )


def _container() -> Container:
    settings = Settings(
        store_backend="memory",
        enable_demo_flow=False,
        admin_tokens=f"boss:admin:{ADMIN_TOKEN}",
    )
    return build_container(settings)


# ---------- 1 / 2：发布后生效，草稿不生效 ----------


def test_publishing_a_version_switches_routing_without_restart() -> None:
    """发布 v2 之后：v2 的规则命中、v1 的规则失效，同一个容器、同一个路由器对象。"""

    container = _container()
    try:
        store = container.flow_store
        assert store is not None
        store.add_version(_flow("1", "报修"))
        store.publish("intent.repair", "1", actor="tester")
        container.flows.reload(store)

        router = container.router
        assert isinstance(router, RegistryIntentRouter)
        matched = router.route(REPAIR)
        assert matched is not None and matched.flow_version == "1"
        assert router.route(REFUND) is None

        # 草稿 v2 进目录但不动指针：它的规则**不得**参与路由。
        store.add_version(_flow("2", "退费"))
        container.flows.reload(store)
        assert router.route(REFUND) is None, "草稿版本的规则抢跑了：群众会被路由到不能执行的版本"
        still_old = router.route(REPAIR)
        assert still_old is not None and still_old.flow_version == "1"

        client = TestClient(create_app(container=container))
        published = client.post(
            "/api/admin/flows/intent.repair/publish",
            headers=ADMIN_HEADER,
            json={"version": "2", "apply": True},
        )
        assert published.status_code == 200, published.text

        # 没有重启、没有换对象：靠目录代数触发的惰性重建。
        assert container.router is router
        switched = router.route(REFUND)
        assert switched is not None and switched.flow_version == "2"
        assert router.route(REPAIR) is None, "旧版本的规则还活着：发布没有换掉识别口径"
        assert container.flows.resolve("intent.repair").version == "2"
    finally:
        container.close()


def test_registry_router_rebuilds_lazily_only_when_the_catalogue_changes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """重建由目录代数触发：目录没变就不重建，变了才重建，且只重建一次。"""

    calls: list[tuple[str, ...]] = []
    real_router_for = intent_routing.router_for

    def counting(flows: Iterable[FlowDefinition]) -> RuleIntentRouter:
        calls.append(tuple(flow.version for flow in flows))
        return real_router_for(flows)

    monkeypatch.setattr(intent_routing, "router_for", counting)

    published = _flow("1", "报修").model_copy(update={"status": FlowStatus.PUBLISHED})
    registry = FlowRegistry([published])
    router = RegistryIntentRouter(registry)

    assert router.route(REPAIR) is not None
    assert len(calls) == 1
    assert router.route(REFUND) is None
    assert len(calls) == 1, "目录没变却重建了：识别口径在两次调用之间无故跳变"

    registry.register(_flow("2", "退费").model_copy(update={"status": FlowStatus.DRAFT}))
    assert router.route(REFUND) is None
    assert len(calls) == 2
    # 重建后看到的仍然只有已发布的那一版。
    assert calls[-1] == ("1",)


def test_draft_only_catalogue_routes_nothing() -> None:
    """全是草稿时规则集合为空：识别不了就走澄清，不猜一个流程。"""

    registry = FlowRegistry([_flow("1", "报修")])
    router = RegistryIntentRouter(registry)

    assert registry.published() == []
    assert router.has_rules is False
    assert router.route(REPAIR) is None


def test_rules_are_collected_in_a_stable_order() -> None:
    """收集顺序按 `(flow_id, version)`，与调用方传入顺序无关：评测结果才可复现。"""

    alpha = _flow("1", "报修", flow_id="alpha.repair").model_copy(
        update={"status": FlowStatus.PUBLISHED}
    )
    beta = _flow("1", "退费", flow_id="beta.refund").model_copy(
        update={"status": FlowStatus.PUBLISHED}
    )

    forward = intent_rules_of([alpha, beta])
    backward = intent_rules_of([beta, alpha])

    assert forward == backward
    assert [rule.intent_id for rule in forward] == ["alpha.repair.v1", "beta.refund.v1"]


# ---------- 3：规则指错版本在写流程时就被拦下 ----------


def test_rule_targeting_another_version_is_reported_at_intent_rules_index() -> None:
    """规则指错版本 → 字段级错误，loc 能定位到 `intent_rules[i]`。"""

    misplaced = _flow("2", "退费", rule_flow_version="1")

    errors = flow_field_errors(misplaced)
    assert [error.loc for error in errors] == [("intent_rules", 0, "flow_id")]
    assert "'intent.repair'@1" in errors[0].message
    assert "'intent.repair'@2" in errors[0].message


def test_rule_shape_errors_are_caught_before_the_version_is_stored() -> None:
    """重复 intent_id 与空关键词同样定位到具体字段，且版本库拒绝写入。"""

    duplicated = _flow("1", "报修").model_copy(
        update={
            "intent_rules": (
                IntentRule(
                    intent_id="intent.repair.same",
                    flow_id="intent.repair",
                    flow_version="1",
                    keywords=("报修",),
                ),
                IntentRule(
                    intent_id="intent.repair.same",
                    flow_id="intent.repair",
                    flow_version="1",
                    keywords=("退费",),
                ),
            )
        }
    )
    errors = flow_field_errors(duplicated)
    assert [error.loc for error in errors] == [("intent_rules", 1, "intent_id")]

    # 关键词全空白：永远不会命中，留着只会让"这句话为什么没被识别"更难查。
    empty = _flow("1", "报修", rule_keywords=("", "   "))
    assert [error.loc for error in flow_field_errors(empty)] == [
        ("intent_rules", 0, "keywords")
    ]

    store = InMemoryFlowStore()
    with pytest.raises(ValueError):
        store.add_version(_flow("2", "退费", rule_flow_version="1"))
    assert store.list_versions() == []


def test_admin_draft_endpoint_rejects_misplaced_rule_with_a_loc() -> None:
    """界面/接口建草稿时也走同一套校验：错误带 loc，不必等到发布才发现。"""

    container = _container()
    try:
        client = TestClient(create_app(container=container))
        body = {
            "flow_id": "intent.repair",
            "version": "2",
            "title": "报修申请",
            "steps": [step.model_dump() for step in _STEPS],
            "intent_samples": [
                {"sample_id": "pos_1", "category": "positive", "text": REPAIR},
            ],
            "intent_rules": [
                {
                    "intent_id": "intent.repair.v2",
                    "flow_id": "intent.repair",
                    "flow_version": "1",  # 指错版本
                    "keywords": ["报修"],
                }
            ],
        }

        response = client.post("/api/admin/flows", headers=ADMIN_HEADER, json=body)

        assert response.status_code == 422, response.text
        detail = response.json()["detail"]
        assert detail["code"] == "admin_draft_invalid"
        assert ["intent_rules", 0, "flow_id"] in [f["loc"] for f in detail["fields"]]
        assert client.get("/api/admin/flows", headers=ADMIN_HEADER).json() == []
    finally:
        container.close()


# ---------- 4：门禁与运维脚本同一口径 ----------


def test_gate_evaluates_the_router_that_will_be_live_after_publish() -> None:
    """门禁必须评"发布之后"的那台路由器，而不是"现在线上正在用"的那台。"""

    v1 = _flow("1", "报修").model_copy(update={"status": FlowStatus.PUBLISHED})
    v2 = _flow("2", "退费")

    # #73 之前的装配：把运行时那台（只认得 v1）交给门禁。新版本样例必然全判漏。
    stale = IntentReleaseGate(router=router_for([v1])).evaluate(v2, [v1])
    assert stale.status is IntentEvalStatus.FAIL
    assert len(stale.missed) == 3

    # 默认装配：按待评语料自己装。同一个版本、同一批样例，必须能过。
    fresh = IntentReleaseGate().evaluate(v2, [v1])
    assert fresh.publishable, fresh.findings
    assert not fresh.missed


def test_eval_script_only_uses_rules_of_published_versions() -> None:
    """运维脚本评的是已发布口径：草稿版本的规则不得抢走已发布版本的样例。"""

    module = _load_script()
    store = InMemoryFlowStore()
    store.add_version(_flow("1", "报修"))
    store.publish("intent.repair", "1", actor="tester")
    # 草稿版本用同一个关键词，但挂在另一个 flow_id 上：规则若混进路由器，
    # 已发布版本的"我要报修"会被路由到它，报告里出现误路由。
    store.add_version(
        _flow("1", "报修", flow_id="intent.refund", positives=("我要退款", "申请退款", "退款进度"))
    )

    report = module.run_eval(
        FlowRegistry.from_store(store),
        timeout_seconds=5.0,
        max_miss_rate=0.0,
        min_positive=3,
        min_near_miss=2,
    )

    assert report.flows == ("intent.repair@1",)
    assert report.misroute_count == 0, report.matrix
    assert report.publishable, report.findings


def _load_script() -> ModuleType:
    path = Path(__file__).resolve().parents[1] / "scripts" / "evaluate_intents.py"
    spec = importlib.util.spec_from_file_location("wp_evaluate_intents", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module
