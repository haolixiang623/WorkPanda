"""发布门禁：把"回放通过"与"样例能过"变成发布的前置条件。

两道门，顺序固定，都是先裁决再移动指针：

1. **识别样例**（`application/intent_eval.py`）：该版本必须带够正例与近似误例，且
   评测里不得出现误路由、漏路由不得超过上限。样例不足是作者态问题，报在回放之前，
   省得"回放过了但发不出去"还要再跑一遍才看懂；
2. **回放**：该版本有回放用例时，未通过不得发布。

关于"没有基线就放行"：回放这条是**有意的**——不假装覆盖没写用例的流程，但报告里
会明确标出 `replay_ran=false`。识别样例不同：`intent_sample_gate` 开启时样例是硬性
材料，没有样例不能发布，因为"缺样例"本身就是要拦的那件事。

发布指针的移动仍然由 `FlowStore` 负责，这里只做裁决：先判定，再移动。两件事分成
两个存储操作是刻意的——门禁失败时不该出现"指针移了、回放没过"的中间态。
"""

from __future__ import annotations

from dataclasses import dataclass

from workpanda.application.intent_eval import IntentReleaseGate
from workpanda.application.replay import FlowReplay
from workpanda.domain.flow import FlowDefinition, FlowReleaseMissing, FlowStore
from workpanda.domain.intent_eval import IntentEvalReport
from workpanda.domain.replay import ReplayCaseStore, ReplayReport


class FlowReleaseBlocked(RuntimeError):
    """回放未通过，发布被拦下。异常带着报告，调用方不必再跑一遍回放。"""

    def __init__(self, message: str, report: ReplayReport) -> None:
        super().__init__(message)
        self.report = report


@dataclass(frozen=True, slots=True)
class FlowReleaseOutcome:
    flow_id: str
    flow_version: str
    published: bool
    # 是否存在可用的回放依据。False 有两种原因：没写用例，或门禁被关掉。
    replay_ran: bool
    report: ReplayReport | None = None
    # 识别样例的评测报告。`intent_gate` 未启用时为 None。
    intent_report: IntentEvalReport | None = None


class FlowReleaseService:
    def __init__(
        self,
        *,
        flows: FlowStore,
        cases: ReplayCaseStore,
        replay: FlowReplay,
        gate_enabled: bool = True,
        intent_gate: IntentReleaseGate | None = None,
    ) -> None:
        self.flows = flows
        self.cases = cases
        self.replay = replay
        self.gate_enabled = gate_enabled
        # None 表示该部署没有启用识别样例门禁（`intent_sample_gate=false`，或测试中
        # 直接注入流程定义、根本不存在"发布"这件事）。装配层默认一定给。
        self.intent_gate = intent_gate

    def release(
        self, flow_id: str, version: str, *, apply: bool = True, actor: str
    ) -> FlowReleaseOutcome:
        """裁决（并在 `apply` 时执行）一次发布。

        `apply=False` 是"只看能不能发"的预演：运维可以先跑一遍看报告，确认无误再
        真的移动指针。预演不写任何状态——否则"预演一次"就成了发布会。
        `actor` 由调用方从已鉴权主体取得，只在真正移动指针时落到库里；本服务
        不接受请求体自报的署名（见 `api/admin.py`）。
        """

        # 版本必须真的存在：`FlowStore.publish` 只保证指针不指向空洞，不会告诉我们
        # 这个版本的步骤长什么样，而回放必须要它。
        flow = self._version(flow_id, version)
        intent_report = (
            self.intent_gate.require(flow, self._published_definitions())
            if self.intent_gate is not None
            else None
        )
        cases = self.cases.list(flow_id, version)

        if not self.gate_enabled or not cases:
            if apply:
                self.flows.publish(flow_id, version, actor=actor)
            return FlowReleaseOutcome(
                flow_id=flow_id,
                flow_version=version,
                published=apply,
                replay_ran=False,
                intent_report=intent_report,
            )

        report = self.replay.run_cases(flow, cases)
        if not report.publishable:
            raise FlowReleaseBlocked(_blocked_message(flow_id, version, report), report)

        if apply:
            self.flows.publish(flow_id, version, actor=actor)
        return FlowReleaseOutcome(
            flow_id=flow_id,
            flow_version=version,
            published=apply,
            replay_ran=True,
            report=report,
            intent_report=intent_report,
        )

    def check(self, flow_id: str, version: str) -> ReplayReport:
        """只跑回放、不改状态。运维手工核对时用。"""

        flow = self._version(flow_id, version)
        return self.replay.run_cases(flow, self.cases.list(flow_id, version))

    def check_intents(self, flow_id: str, version: str) -> IntentEvalReport | None:
        """只跑识别评测、不改状态。门禁未启用时返回 None。"""

        if self.intent_gate is None:
            return None
        flow = self._version(flow_id, version)
        return self.intent_gate.evaluate(flow, self._published_definitions())

    def _version(self, flow_id: str, version: str) -> FlowDefinition:
        for flow in self.flows.list_versions(flow_id):
            if flow.version == version:
                return flow
        raise FlowReleaseMissing(f"flow {flow_id!r} version {version!r} is not registered")

    def _published_definitions(self) -> list[FlowDefinition]:
        """当前发布指针指向的版本。评测用它当"其余流程"的口径。"""

        pointers = self.flows.published_versions()
        return [
            flow
            for flow in self.flows.list_versions()
            if pointers.get(flow.flow_id) == flow.version
        ]


def _blocked_message(flow_id: str, version: str, report: ReplayReport) -> str:
    blocking = report.blocking
    details = "；".join(
        f"{result.case_id}={result.verdict.value}"
        f"（{','.join(result.codes) or '无'}）"
        for result in blocking
    )
    if not blocking:
        # 全部用例都被中断：没有阻挡项，但也没有任何通过的用例。
        details = "没有可用的回放结论（用例全部被中断或未产生结果）"
    return (
        f"回放未通过，已拦下 {flow_id}@{version} 的发布：{details}。"
        f"整体判定 {report.verdict.value}"
    )
