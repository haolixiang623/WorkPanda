"""识别样例门禁与评测指标：发布前必须带够样例，误路由一条都不许有。

这组用例守的是一条**不会报错**的失效路径：流程越加越多，路由覆盖度悄悄下降，
线上表现只是"澄清率上升"，看不出是缺样例。所以除了"能过"的路径，更要紧的是钉住
哪几种情形必须被拦下：样例条数不足、误路由、漏路由超限、评测超时/崩溃。

另有三条守住评测本身：报告不含样例原文、结果可重跑一致、同一流程不许出现两个版本。
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from workpanda.application.intent_eval import (
    IntentEvalBlocked,
    IntentEvalRunner,
    IntentReleaseGate,
)
from workpanda.application.release import FlowReleaseService
from workpanda.application.replay import FlowReplay
from workpanda.bootstrap import build_container, seed_demo_flows
from workpanda.config import Settings
from workpanda.domain.flow import (
    FlowDefinition,
    FlowStatus,
    StepDefinition,
    StepKind,
    validate_flow,
)
from workpanda.domain.intent import IntentRule, RuleIntentRouter
from workpanda.domain.intent_eval import (
    UNMATCHED_LABEL,
    IntentEvalFindingCode,
    IntentEvalStatus,
    evaluate_corpus,
)
from workpanda.domain.intent_sample import (
    MIN_NEAR_MISS_SAMPLES,
    MIN_POSITIVE_SAMPLES,
    IntentSample,
    IntentSampleCategory,
)
from workpanda.infrastructure.memory_store import InMemoryFlowStore
from workpanda.infrastructure.replay_cases import InMemoryReplayCaseStore
from workpanda.infrastructure.sqlite_flow_store import SqliteFlowStore
from workpanda.main import create_app

POS = IntentSampleCategory.POSITIVE
NEAR = IntentSampleCategory.NEAR_MISS
OOS = IntentSampleCategory.OUT_OF_SCOPE

ALPHA_POS = ("帮我把手机号改绑一下", "改绑手机号", "我要改绑")
ALPHA_NEAR = ("我要取消订单", "查一下天气预报")
BETA_POS = ("帮我查一下办理进度", "进度到哪了", "我要看进度")
BETA_NEAR = ("我要取消订单", "今天天气怎么样")


def _sample(sample_id: str, category: IntentSampleCategory, text: str) -> IntentSample:
    return IntentSample(sample_id=sample_id, category=category, text=text)


def _flow(
    flow_id: str = "eval.alpha",
    version: str = "1",
    *,
    positives: tuple[str, ...] = ALPHA_POS,
    near_miss: tuple[str, ...] = ALPHA_NEAR,
    out_of_scope: tuple[str, ...] = (),
) -> FlowDefinition:
    samples = tuple(
        [_sample(f"pos_{index}", POS, text) for index, text in enumerate(positives, 1)]
        + [_sample(f"near_{index}", NEAR, text) for index, text in enumerate(near_miss, 1)]
        + [_sample(f"oos_{index}", OOS, text) for index, text in enumerate(out_of_scope, 1)]
    )
    return FlowDefinition(
        flow_id=flow_id,
        version=version,
        title="评测用流程",
        status=FlowStatus.PUBLISHED,
        steps=(
            StepDefinition(
                step_id="collect",
                kind=StepKind.COLLECT,
                title="收集信息",
                required_fields=("subject",),
            ),
        ),
        intent_samples=samples,
    )


def _rule(
    flow_id: str = "eval.alpha",
    version: str = "1",
    keywords: tuple[str, ...] = ("改绑",),
    intent_id: str | None = None,
) -> IntentRule:
    return IntentRule(
        intent_id=intent_id or f"{flow_id}.submit",
        flow_id=flow_id,
        flow_version=version,
        keywords=keywords,
    )


def _gate(*rules: IntentRule, **kwargs: float) -> IntentReleaseGate:
    return IntentReleaseGate(router=RuleIntentRouter(rules=rules), **kwargs)


def _release_service(
    store: InMemoryFlowStore,
    gate: IntentReleaseGate | None,
    *,
    cases: InMemoryReplayCaseStore | None = None,
) -> FlowReleaseService:
    return FlowReleaseService(
        flows=store,
        cases=cases or InMemoryReplayCaseStore(),
        replay=FlowReplay(timeout_seconds=1.0),
        intent_gate=gate,
    )


# ---------- 样例条数：发布前置条件 ----------


def test_insufficient_samples_block_publish() -> None:
    """正例与近似误例都不足时，报错必须说清缺的是哪一类，并且指针不动。"""

    flow = _flow(positives=("我要改绑",), near_miss=("我要取消订单",))
    store = InMemoryFlowStore()
    store.add_version(flow)
    service = _release_service(store, _gate(_rule()))

    with pytest.raises(IntentEvalBlocked) as caught:
        service.release(flow.flow_id, flow.version)

    codes = {finding.code for finding in caught.value.report.blocking}
    assert IntentEvalFindingCode.SAMPLE_COUNT_SHORT in codes
    message = str(caught.value)
    assert "正例" in message
    assert "近似误例" in message
    assert store.published_versions() == {}


def test_positives_without_near_miss_are_rejected() -> None:
    """只有正例、没有近似误例的流程不能发布——这正是"关键词太宽"最常见的来源。"""

    flow = _flow(positives=ALPHA_POS, near_miss=())
    store = InMemoryFlowStore()
    store.add_version(flow)
    service = _release_service(store, _gate(_rule()))

    with pytest.raises(IntentEvalBlocked) as caught:
        service.release(flow.flow_id, flow.version)

    details = "；".join(finding.detail for finding in caught.value.report.blocking)
    assert "近似误例不足" in details
    # 正例够 3 条，不该连带报"正例不足"——报错要指得准，否则运维会去补错的那一类。
    assert "正例不足" not in details
    assert store.published_versions() == {}


def test_full_samples_publish_without_replay_cases() -> None:
    """样例齐全且评测通过即可发布；没有回放用例时如实标出 `replay_ran=false`。"""

    flow = _flow()
    store = InMemoryFlowStore()
    store.add_version(flow)
    service = _release_service(store, _gate(_rule()))

    outcome = service.release(flow.flow_id, flow.version)

    assert outcome.published is True
    assert outcome.replay_ran is False
    assert outcome.intent_report is not None
    assert outcome.intent_report.publishable is True
    assert outcome.intent_report.misroute_count == 0
    assert store.published_versions() == {flow.flow_id: flow.version}


def test_gate_is_skipped_only_when_explicitly_absent() -> None:
    """没有门禁对象时照旧可发——这条只用于"注入定义、不存在发布"的场景。"""

    flow = _flow(positives=("我要改绑",), near_miss=())
    store = InMemoryFlowStore()
    store.add_version(flow)

    outcome = _release_service(store, None).release(flow.flow_id, flow.version)

    assert outcome.published is True
    assert outcome.intent_report is None


# ---------- 误路由：必须为 0 ----------


def test_misroute_to_another_flow_blocks_release() -> None:
    """正例被识别到别的事项：既要拦下，也要在报告里指到具体样例（不含原文）。"""

    beta = _flow(
        flow_id="eval.beta", positives=BETA_POS, near_miss=BETA_NEAR
    )
    # delta 没有自己的规则，它的正例会被 beta 的规则捞走 → 误路由。
    delta = _flow(
        flow_id="eval.delta",
        positives=("帮我查一下办理进度", "进度到哪了", "我要看进度"),
        near_miss=("我要取消订单", "今天天气怎么样"),
    )
    store = InMemoryFlowStore()
    for flow in (beta, delta):
        store.add_version(flow)
    store.publish(beta.flow_id, beta.version)

    service = _release_service(store, _gate(_rule("eval.beta", keywords=("进度",))))

    with pytest.raises(IntentEvalBlocked) as caught:
        service.release(delta.flow_id, delta.version)

    report = caught.value.report
    assert report.misroute_count >= 1
    assert IntentEvalFindingCode.POSITIVE_MISROUTED in {f.code for f in report.findings}
    assert "eval.beta@1" in "；".join(finding.detail for finding in report.blocking)
    # 样例原文绝不进报告。
    assert "帮我查一下办理进度" not in str(caught.value)
    assert store.published_versions() == {beta.flow_id: beta.version}


def test_rule_on_old_version_counts_as_misroute() -> None:
    """规则还指着旧版本时，话会被路由到旧版本 —— 发布会"成功"但新版本根本没生效。"""

    flow = _flow(version="2")
    store = InMemoryFlowStore()
    store.add_version(flow)
    service = _release_service(store, _gate(_rule(version="1")))

    with pytest.raises(IntentEvalBlocked) as caught:
        service.release(flow.flow_id, flow.version)

    report = caught.value.report
    assert report.misroute_count == len(ALPHA_POS)
    assert all(
        result.actual_flow_version == "1" for result in report.misrouted
    )


def test_near_miss_routed_is_a_misroute() -> None:
    """近似误例被路由到本流程，等于关键词太宽；同样一条都不许有。"""

    flow = _flow(near_miss=("我要改绑预约时间", "我要取消订单"))
    store = InMemoryFlowStore()
    store.add_version(flow)
    service = _release_service(store, _gate(_rule()))

    with pytest.raises(IntentEvalBlocked) as caught:
        service.release(flow.flow_id, flow.version)

    codes = {finding.code for finding in caught.value.report.findings}
    assert IntentEvalFindingCode.SAMPLE_ROUTED in codes


def test_out_of_scope_routed_is_a_misroute() -> None:
    """超范围请求被塞进某个事项的执行参数，是最坏的一种"误路由"。"""

    flow = _flow(out_of_scope=("我要改绑缴费卡", "今天天气怎么样"))
    store = InMemoryFlowStore()
    store.add_version(flow)
    service = _release_service(store, _gate(_rule()))

    with pytest.raises(IntentEvalBlocked) as caught:
        service.release(flow.flow_id, flow.version)

    assert IntentEvalFindingCode.SAMPLE_ROUTED in {
        finding.code for finding in caught.value.report.findings
    }


# ---------- 漏路由：阈值 ----------


def test_miss_over_limit_blocks() -> None:
    # 没有规则 ⇒ 三条正例全漏。
    flow = _flow()
    store = InMemoryFlowStore()
    store.add_version(flow)
    service = _release_service(store, _gate())

    with pytest.raises(IntentEvalBlocked) as caught:
        service.release(flow.flow_id, flow.version)

    report = caught.value.report
    assert report.miss_rate == 1.0
    missed = [f for f in report.findings if f.code is IntentEvalFindingCode.POSITIVE_MISSED]
    assert missed and missed[0].blocking is True


def test_miss_within_limit_is_recorded_but_not_blocking() -> None:
    """上限放宽到 100% 时漏路由不该挡发布，但必须留在报告里（不能静默）。"""

    report = IntentEvalRunner().run(
        [_flow()],
        RuleIntentRouter(),
        max_miss_rate=1.0,
    )

    assert report.status is IntentEvalStatus.PASS
    assert report.publishable is True
    assert report.miss_rate == 1.0
    missed = [f for f in report.findings if f.code is IntentEvalFindingCode.POSITIVE_MISSED]
    assert missed and missed[0].blocking is False


def test_no_positive_samples_is_not_a_perfect_score() -> None:
    """没有正例时漏路由率按最高缺口计，绝不按 0 计。"""

    flow = _flow(positives=(), near_miss=ALPHA_NEAR)
    report = evaluate_corpus([flow], RuleIntentRouter(rules=(_rule(),)))

    assert report.positive_total == 0
    assert report.miss_rate == 1.0
    assert report.publishable is False


# ---------- 超时 / 崩溃：不得当作通过 ----------


class _SleepingRouter:
    def route(self, text: str):
        time.sleep(0.2)
        return None


class _BrokenRouter:
    def route(self, text: str):
        raise RuntimeError("规则实现崩了")


def test_timeout_is_unconfirmed_and_blocks() -> None:
    report = IntentEvalRunner(timeout_seconds=0.01).run([_flow()], _SleepingRouter())

    assert report.status is IntentEvalStatus.UNCONFIRMED
    assert report.publishable is False
    assert IntentEvalFindingCode.EVAL_TIMEOUT in {f.code for f in report.findings}

    gate = IntentReleaseGate(
        router=_SleepingRouter(), runner=IntentEvalRunner(timeout_seconds=0.01)
    )
    with pytest.raises(IntentEvalBlocked):
        gate.require(_flow(), [])


def test_crash_is_unconfirmed_and_blocks() -> None:
    report = IntentEvalRunner().run([_flow()], _BrokenRouter())

    assert report.status is IntentEvalStatus.UNCONFIRMED
    assert report.publishable is False
    assert IntentEvalFindingCode.EVAL_CRASHED in {f.code for f in report.findings}
    assert "RuntimeError" in report.findings[0].detail


# ---------- 评测本身：可重复、不泄露、口径唯一 ----------


def test_report_is_deterministic_and_sorted() -> None:
    flows = [
        _flow(flow_id="eval.beta", positives=BETA_POS, near_miss=BETA_NEAR),
        _flow(),
    ]
    router = RuleIntentRouter(
        rules=(_rule(), _rule("eval.beta", keywords=("进度",)))
    )

    first = IntentEvalRunner().run(flows, router)
    second = IntentEvalRunner().run(list(reversed(flows)), router)

    assert first.status is second.status
    assert first.results == second.results
    assert first.matrix == second.matrix
    assert first.findings == second.findings
    assert [flow for flow in first.flows] == ["eval.alpha@1", "eval.beta@1"]


def test_report_never_contains_sample_text() -> None:
    """报告会进 CI 日志与运维记录，样例文本一律不得出现在里面。"""

    marker = "合成样例文本甲需要脱敏"
    flow = _flow(
        positives=(marker, "改绑手机号", "我要改绑"),
        near_miss=("我要取消订单", "查一下天气预报"),
    )
    report = IntentEvalRunner().run([flow], RuleIntentRouter(rules=(_rule(),)))
    dumped = json.dumps(report.model_dump(mode="json"), ensure_ascii=False)

    assert marker not in dumped
    assert "pos_1" in dumped


def test_duplicate_samples_rejected_at_registration() -> None:
    """重复的 `sample_id` 或重复文本（含全角变体）在写入时就要报错，报错只给 id。"""

    duplicated_id = FlowDefinition(
        flow_id="eval.dup",
        version="1",
        title="重复样例",
        steps=(StepDefinition(step_id="collect", kind=StepKind.COLLECT, title="收集"),),
        intent_samples=(
            _sample("pos_1", POS, "我要改绑"),
            _sample("pos_1", POS, "改绑手机号"),
            _sample("near_1", NEAR, "我要取消订单"),
        ),
    )
    with pytest.raises(ValueError) as caught:
        validate_flow(duplicated_id)
    assert "pos_1" in str(caught.value)

    duplicated_text = FlowDefinition(
        flow_id="eval.dup",
        version="1",
        title="重复样例",
        steps=(StepDefinition(step_id="collect", kind=StepKind.COLLECT, title="收集"),),
        intent_samples=(
            _sample("pos_1", POS, "我要改绑手机号ABC"),
            # 大小写不同但归一化后同一句：去重必须与识别用同一把尺子。
            _sample("pos_2", POS, "我要改绑手机号abc"),
            _sample("near_1", NEAR, "我要取消订单"),
        ),
    )
    with pytest.raises(ValueError) as caught:
        validate_flow(duplicated_text)
    assert "pos_1/pos_2" in str(caught.value)
    assert "我要改绑手机号" not in str(caught.value)


def test_two_versions_of_one_flow_are_rejected() -> None:
    """同一流程两个版本进语料是装配错误：两版规则会互相抢话，结论没有意义。"""

    with pytest.raises(ValueError):
        evaluate_corpus([_flow(), _flow(version="2")], RuleIntentRouter(rules=(_rule(),)))


def test_empty_corpus_is_not_a_pass() -> None:
    report = evaluate_corpus([], RuleIntentRouter(rules=(_rule(),)))

    assert report.status is IntentEvalStatus.FAIL
    assert report.publishable is False
    assert IntentEvalFindingCode.NO_PUBLISHED_FLOWS in {f.code for f in report.findings}


def test_samples_survive_sqlite_round_trip(workdir: Path) -> None:
    """样例必须跟版本一起进库：换后端不该丢掉基线，也不该丢掉原文。"""

    flow = _flow()
    store = SqliteFlowStore(workdir / "flows.db")
    try:
        store.add_version(flow)
        store.publish(flow.flow_id, flow.version)
        reloaded = store.list_versions(flow.flow_id)[0]
    finally:
        store.close()

    assert reloaded.intent_samples == flow.intent_samples


# ---------- 与既有流程的联动：新流程不得把老流程的话抢走 ----------


def test_new_flow_that_steals_utterances_is_blocked() -> None:
    """新增流程把既有流程的正例抢走时，发布被拒、老流程指针不动。"""

    alpha = _flow()
    gamma = _flow(
        flow_id="eval.gamma",
        positives=("我要改绑手机号办理业务", "改绑手机号", "帮我改绑"),
        near_miss=("我要取消订单", "今天天气怎么样"),
    )
    store = InMemoryFlowStore()
    store.add_version(alpha)
    store.publish(alpha.flow_id, alpha.version)
    store.add_version(gamma)

    # gamma 的规则同时命中"改绑"与"手机号"，分值压过 alpha 的单词命中。
    gate = _gate(
        _rule(),
        _rule("eval.gamma", keywords=("改绑", "手机号")),
    )
    service = _release_service(store, gate)

    with pytest.raises(IntentEvalBlocked) as caught:
        service.release(gamma.flow_id, gamma.version)

    report = caught.value.report
    assert report.misroute_count >= 1
    assert "eval.alpha" in {result.flow_id for result in report.misrouted}
    assert store.published_versions() == {alpha.flow_id: alpha.version}


def test_bootstrap_wires_gate_and_demo_baseline_passes() -> None:
    """装配层默认接上门禁；骨架自检流程的样例就是 N/M 基线的来源。"""

    container = build_container(Settings(store_backend="memory", enable_demo_flow=True))
    try:
        assert container.release is not None
        assert container.release.intent_gate is not None

        report = container.release.check_intents("demo.generic", "1")

        assert report is not None
        assert report.misroute_count == 0
        assert report.miss_rate == 0.0
        assert report.publishable is True
        positives = [
            row for row in report.coverage if row.category is IntentSampleCategory.POSITIVE
        ]
        assert positives[0].need == MIN_POSITIVE_SAMPLES
        near_miss = [
            row for row in report.coverage if row.category is IntentSampleCategory.NEAR_MISS
        ]
        assert near_miss[0].need == MIN_NEAR_MISS_SAMPLES
        assert UNMATCHED_LABEL in {cell.expected for cell in report.matrix}
    finally:
        container.close()


def test_gate_can_be_switched_off_explicitly() -> None:
    """关闭开关是一个显式决定：门禁对象消失，且报告里不再有识别结论。"""

    container = build_container(
        Settings(store_backend="memory", enable_demo_flow=True, intent_sample_gate=False)
    )
    try:
        assert container.release is not None
        assert container.release.intent_gate is None
        assert container.release.check_intents("demo.generic", "1") is None
    finally:
        container.close()


# ---------- CI 入口 ----------

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "evaluate_intents.py"


def _run_script(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


def test_ci_script_passes_on_baseline() -> None:
    """CI 那条命令必须真的给出一份可读报告，并且以 0 退出。"""

    completed = _run_script()

    assert completed.returncode == 0, completed.stderr
    assert "误路由 0 条" in completed.stdout
    assert "漏路由" in completed.stdout
    assert "混淆矩阵" in completed.stdout


def test_ci_script_fails_when_nothing_is_published() -> None:
    """没有已发布流程时不是"通过"：识别能力等于零，CI 必须红。"""

    completed = _run_script("--no-demo")

    assert completed.returncode == 1
    assert "识别评测未通过" in completed.stderr


# ---------- 运维脚本不能绕过门禁 ----------

REPLAY_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "replay_flow.py"


def _publish_via_script(db: Path, flow_id: str, version: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            str(REPLAY_SCRIPT),
            "--flow",
            flow_id,
            "--version",
            version,
            "--sqlite",
            str(db),
            "--publish",
        ],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


def test_replay_script_publish_is_refused_without_samples(workdir: Path) -> None:
    """运维入口必须走同一道门：否则"用脚本发布"就是绕过门禁的后门。"""

    db = workdir / "flows.db"
    flow = _flow()
    store = SqliteFlowStore(db)
    try:
        store.add_version(flow)
    finally:
        store.close()

    completed = _publish_via_script(db, flow.flow_id, flow.version)

    assert completed.returncode == 1, completed.stdout
    assert "识别样例" in completed.stderr

    check = SqliteFlowStore(db)
    try:
        assert check.published_versions() == {}
    finally:
        check.close()


def test_replay_script_publish_accepts_demo_flow(workdir: Path) -> None:
    """样例齐全的版本可以通过运维入口发布（基线闭环）。"""

    db = workdir / "demo.db"
    store = SqliteFlowStore(db)
    try:
        seed_demo_flows(store, enabled=True)
    finally:
        store.close()

    completed = _publish_via_script(db, "demo.generic", "1")

    assert completed.returncode == 0, completed.stderr
    assert "已发布 demo.generic@1" in completed.stdout


# ---------- 管理侧配置台：门禁拦下要给可自救的 409 ----------


def test_admin_publish_without_samples_returns_actionable_409() -> None:
    """管理侧建出来的流程没有样例时，界面拿到的必须是"缺什么"而不是 500。"""

    token = "admin-secret-0123456789"
    header = {"X-WorkPanda-Admin-Token": token}
    container = build_container(
        Settings(
            store_backend="memory",
            enable_demo_flow=False,
            admin_tokens=f"boss:admin:{token}",
        )
    )
    try:
        client = TestClient(create_app(container=container))
        created = client.post(
            "/api/admin/flows",
            headers=header,
            json={
                "flow_id": "admin.no_samples",
                "version": "1",
                "title": "没有样例的流程",
                "steps": [
                    {
                        "step_id": "collect",
                        "kind": "collect",
                        "title": "收集信息",
                        "required_fields": ["subject"],
                    }
                ],
            },
        )
        assert created.status_code == 201, created.text

        response = client.post(
            "/api/admin/flows/admin.no_samples/publish",
            headers=header,
            json={"version": "1"},
        )

        assert response.status_code == 409, response.text
        detail = response.json()["detail"]
        assert detail["code"] == "admin_intent_gate_blocked"
        assert "正例不足" in detail["detail"]
        assert detail["report"]["status"] == "fail"
        assert container.flow_store is not None
        assert container.flow_store.published_versions() == {}
    finally:
        container.close()
