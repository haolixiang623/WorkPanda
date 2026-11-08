"""发布门禁：把"回放通过"变成发布的前置条件。

规则只有一条：**该版本有回放用例时，未通过不得发布**。没有用例的版本照旧可以发布
——这条门禁不假装覆盖没有基线的流程；它只是让"写了用例就真的会被拦"成立。反过来说，
"用一条永远不会失败的用例换放行"也是不可能的：用例无效（`invalid`）与回放无结论
（`unconfirmed`）同样会挡住发布。

发布指针的移动仍然由 `FlowStore` 负责，这里只做裁决：先判定，再移动。两件事分成
两个存储操作是刻意的——门禁失败时不该出现"指针移了、回放没过"的中间态。
"""

from __future__ import annotations

from dataclasses import dataclass

from workpanda.application.replay import FlowReplay
from workpanda.domain.flow import FlowDefinition, FlowReleaseMissing, FlowStore
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


class FlowReleaseService:
    def __init__(
        self,
        *,
        flows: FlowStore,
        cases: ReplayCaseStore,
        replay: FlowReplay,
        gate_enabled: bool = True,
    ) -> None:
        self.flows = flows
        self.cases = cases
        self.replay = replay
        self.gate_enabled = gate_enabled

    def release(
        self, flow_id: str, version: str, *, apply: bool = True
    ) -> FlowReleaseOutcome:
        """裁决（并在 `apply` 时执行）一次发布。

        `apply=False` 是"只看能不能发"的预演：运维可以先跑一遍看报告，确认无误再
        真的移动指针。预演不写任何状态——否则"预演一次"就成了发布会。
        """

        # 版本必须真的存在：`FlowStore.publish` 只保证指针不指向空洞，不会告诉我们
        # 这个版本的步骤长什么样，而回放必须要它。
        flow = self._version(flow_id, version)
        cases = self.cases.list(flow_id, version)

        if not self.gate_enabled or not cases:
            if apply:
                self.flows.publish(flow_id, version)
            return FlowReleaseOutcome(
                flow_id=flow_id,
                flow_version=version,
                published=apply,
                replay_ran=False,
            )

        report = self.replay.run_cases(flow, cases)
        if not report.publishable:
            raise FlowReleaseBlocked(_blocked_message(flow_id, version, report), report)

        if apply:
            self.flows.publish(flow_id, version)
        return FlowReleaseOutcome(
            flow_id=flow_id,
            flow_version=version,
            published=apply,
            replay_ran=True,
            report=report,
        )

    def check(self, flow_id: str, version: str) -> ReplayReport:
        """只跑回放、不改状态。运维手工核对时用。"""

        flow = self._version(flow_id, version)
        return self.replay.run_cases(flow, self.cases.list(flow_id, version))

    def _version(self, flow_id: str, version: str) -> FlowDefinition:
        for flow in self.flows.list_versions(flow_id):
            if flow.version == version:
                return flow
        raise FlowReleaseMissing(f"flow {flow_id!r} version {version!r} is not registered")


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
