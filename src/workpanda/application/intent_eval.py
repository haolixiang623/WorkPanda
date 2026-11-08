"""识别样例门禁：给评测加时限，并把它接进发布流程。

为什么要有这一层：

- 评测本身是纯函数（`domain/intent_eval.py`），但"跑评测"这件事可能因为规则实现卡死
  而永远不返回。超时必须成为一个**结论**（`unconfirmed`），不能只是"还在跑"——
  否则 CI 会静默挂住、发布门禁会静默放行；
- 发布门禁需要一个"能拒绝"的对象，而不是散落在各调用方的 `if`。

评测对象是"发布后会生效的口径"：待发布的那个版本替换掉当前发布指针上的同流程版本，
其余流程各取发布指针指向的版本。同一流程放两个版本进语料，两版规则会互相抢话，
先发的那版样例必然被判误路由——那不是流程坏了，是口径错了。
"""

from __future__ import annotations

import threading
from collections.abc import Sequence

from workpanda.application.ports import IntentRouter
from workpanda.domain.flow import FlowDefinition
from workpanda.domain.intent_eval import (
    DEFAULT_EVAL_TIMEOUT_SECONDS,
    DEFAULT_MAX_MISS_RATE,
    IntentEvalFinding,
    IntentEvalFindingCode,
    IntentEvalReport,
    IntentEvalStatus,
    evaluate_corpus,
    flow_label,
)
from workpanda.domain.intent_sample import MIN_NEAR_MISS_SAMPLES, MIN_POSITIVE_SAMPLES


class IntentEvalBlocked(RuntimeError):
    """识别样例门禁拦下了发布。异常带着报告，调用方不必再跑一遍评测。"""

    def __init__(self, message: str, report: IntentEvalReport) -> None:
        super().__init__(message)
        self.report = report


class IntentEvalRunner:
    """在时限内跑评测。超时与崩溃都产出"无结论"报告，绝不产出 `pass`。"""

    def __init__(self, *, timeout_seconds: float = DEFAULT_EVAL_TIMEOUT_SECONDS) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds 必须大于 0")
        self._timeout_seconds = timeout_seconds

    @property
    def timeout_seconds(self) -> float:
        return self._timeout_seconds

    def run(
        self,
        flows: Sequence[FlowDefinition],
        router: IntentRouter,
        *,
        min_positive: int = MIN_POSITIVE_SAMPLES,
        min_near_miss: int = MIN_NEAR_MISS_SAMPLES,
        max_miss_rate: float = DEFAULT_MAX_MISS_RATE,
    ) -> IntentEvalReport:
        labels = tuple(flow_label(flow.flow_id, flow.version) for flow in flows)
        box: dict[str, object] = {}

        def worker() -> None:
            try:
                box["report"] = evaluate_corpus(
                    flows,
                    router,
                    min_positive=min_positive,
                    min_near_miss=min_near_miss,
                    max_miss_rate=max_miss_rate,
                )
            except BaseException as exc:  # noqa: BLE001 - 评测里任何异常都不能变成 pass
                box["crashed"] = f"{type(exc).__name__}: {exc}"

        thread = threading.Thread(target=worker, name="intent-eval", daemon=True)
        thread.start()
        thread.join(self._timeout_seconds)

        if thread.is_alive():
            return _unconfirmed(
                IntentEvalFindingCode.EVAL_TIMEOUT,
                f"评测超过 {self._timeout_seconds:g}s 未结束，没有结论，不判通过。",
                labels,
                max_miss_rate,
            )

        crashed = box.get("crashed")
        report = box.get("report")
        if crashed is not None or not isinstance(report, IntentEvalReport):
            detail = str(crashed) if crashed is not None else "评测没有产出报告"
            return _unconfirmed(
                IntentEvalFindingCode.EVAL_CRASHED,
                f"评测异常结束：{detail}；没有结论，不判通过。",
                labels,
                max_miss_rate,
            )
        return report


class IntentReleaseGate:
    """发布前的识别样例门禁：条数够、误路由为 0、漏路由不超限。"""

    def __init__(
        self,
        *,
        router: IntentRouter,
        runner: IntentEvalRunner | None = None,
        min_positive: int = MIN_POSITIVE_SAMPLES,
        min_near_miss: int = MIN_NEAR_MISS_SAMPLES,
        max_miss_rate: float = DEFAULT_MAX_MISS_RATE,
    ) -> None:
        self.router = router
        self.runner = runner or IntentEvalRunner()
        self.min_positive = min_positive
        self.min_near_miss = min_near_miss
        self.max_miss_rate = max_miss_rate

    def evaluate(
        self, flow: FlowDefinition, published: Sequence[FlowDefinition]
    ) -> IntentEvalReport:
        """按发布后的口径评测，只出结论不抛错。运维预演与 CI 都用这个入口。"""

        corpus = [item for item in published if item.flow_id != flow.flow_id]
        corpus.append(flow)
        return self.runner.run(
            corpus,
            self.router,
            min_positive=self.min_positive,
            min_near_miss=self.min_near_miss,
            max_miss_rate=self.max_miss_rate,
        )

    def require(
        self, flow: FlowDefinition, published: Sequence[FlowDefinition]
    ) -> IntentEvalReport:
        report = self.evaluate(flow, published)
        if not report.publishable:
            raise IntentEvalBlocked(_blocked_message(flow, report), report)
        return report


def _unconfirmed(
    code: IntentEvalFindingCode,
    detail: str,
    labels: Sequence[str],
    max_miss_rate: float,
) -> IntentEvalReport:
    return IntentEvalReport(
        status=IntentEvalStatus.UNCONFIRMED,
        flows=tuple(labels),
        findings=(IntentEvalFinding(code=code, detail=detail),),
        max_miss_rate=max_miss_rate,
    )


def _blocked_message(flow: FlowDefinition, report: IntentEvalReport) -> str:
    details = "；".join(finding.detail for finding in report.blocking)
    if not details:
        # 没有阻断项又不是 pass，只可能是"无结论"。
        details = "没有可用的评测结论（评测超时或异常结束）"
    return (
        f"识别样例门禁拦下 {flow_label(flow.flow_id, flow.version)} 的发布：{details}。"
        f"整体判定 {report.status.value}"
    )


__all__ = [
    "IntentEvalBlocked",
    "IntentEvalRunner",
    "IntentReleaseGate",
]
