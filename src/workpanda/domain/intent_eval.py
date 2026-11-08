"""识别评测：把"样例能过"变成可判定的指标，交给发布门禁与 CI。

三条硬规则（对应 Issue #52 的验收标准）：

1. **误路由必须为 0**：任何"期望 A / 实际 B"直接不通过，没有比例可言——一旦发生，
   群众的话已经被塞进另一个事项的执行参数里了；
2. **漏路由有上限**：正例没被识别到任何流程即为漏。上限默认 0，因为规则是确定性的，
   一条正例漏掉就是规则缺陷；给比例留口子只会让覆盖率静默衰减（指标上只表现为
   "澄清率上升"，看不出是缺样例）；
3. **没有结论不算通过**：超时与崩溃由 `application/intent_eval.py` 判成 `unconfirmed`，
   与"通过"严格分开。

评测对象是"发布后会生效的口径"：同一流程只能有一个版本出现在语料里（否则两版规则
互相抢话，先发的那版样例必然被判误路由）。这一点由调用方保证；本模块发现重复直接报错，
因为那是装配错误，不是评测结论。

报告只含 `sample_id`、类别与流程标识，**不含样例原文**：报告会进 CI 日志与运维记录，
而样例文本哪怕合成，也应当按"群众可能这样说"的证据来对待。
"""

from __future__ import annotations

import time
from collections.abc import Sequence
from enum import StrEnum

from pydantic import BaseModel, Field

from workpanda.application.ports import IntentRouter
from workpanda.domain.flow import FlowDefinition
from workpanda.domain.intent_sample import (
    MIN_NEAR_MISS_SAMPLES,
    MIN_POSITIVE_SAMPLES,
    IntentSample,
    IntentSampleCategory,
    SampleCoverage,
    sample_coverage,
)

# 漏路由率上限的默认值：一条都不许漏。
DEFAULT_MAX_MISS_RATE = 0.0
# 一次评测的时限。评测是纯函数、正常在毫秒级完成；超时说明实现卡死。
DEFAULT_EVAL_TIMEOUT_SECONDS = 5.0
# 混淆矩阵里表示"没有匹配到任何流程"的行列标签。
UNMATCHED_LABEL = "(未匹配)"


class IntentEvalStatus(StrEnum):
    PASS = "pass"
    FAIL = "fail"
    # 超时或崩溃：没有结论。既不通过，也不能当"没发现问题"。
    UNCONFIRMED = "unconfirmed"


class IntentEvalVerdict(StrEnum):
    PASS = "pass"
    # 正例没有识别到任何流程。
    MISSED = "missed"
    # 识别到了别的流程，或不该被路由的样例被路由了。
    MISROUTED = "misrouted"


class IntentEvalFindingCode(StrEnum):
    NO_PUBLISHED_FLOWS = "no_published_flows"
    SAMPLE_COUNT_SHORT = "sample_count_short"
    POSITIVE_MISSED = "positive_missed"
    POSITIVE_MISROUTED = "positive_misrouted"
    SAMPLE_ROUTED = "sample_routed"
    EVAL_TIMEOUT = "eval_timeout"
    EVAL_CRASHED = "eval_crashed"


class IntentEvalFinding(BaseModel):
    code: IntentEvalFindingCode
    detail: str
    # "发现"不等于"阻断"：漏路由只有超过上限才挡发布，其余问题一律挡。
    blocking: bool = True


class IntentSampleResult(BaseModel):
    """一条样例的判定。刻意不含原文，只到 `sample_id` 为止。"""

    flow_id: str
    flow_version: str
    sample_id: str
    category: IntentSampleCategory
    expected_flow_id: str | None = None
    expected_flow_version: str | None = None
    actual_flow_id: str | None = None
    actual_flow_version: str | None = None
    verdict: IntentEvalVerdict

    @property
    def expected_label(self) -> str:
        return flow_label(self.expected_flow_id, self.expected_flow_version)

    @property
    def actual_label(self) -> str:
        return flow_label(self.actual_flow_id, self.actual_flow_version)


class ConfusionCell(BaseModel):
    """混淆矩阵的一格。用列表而不是字典：JSON 里字典的键会被转成字符串。"""

    expected: str
    actual: str
    count: int = Field(ge=0)


class IntentEvalReport(BaseModel):
    """一次评测的完整结论。`publishable` 是门禁与 CI 的唯一判据。"""

    status: IntentEvalStatus
    # 参与评测的流程版本，`flow_id@version`。
    flows: tuple[str, ...] = ()
    results: tuple[IntentSampleResult, ...] = ()
    coverage: tuple[SampleCoverage, ...] = ()
    matrix: tuple[ConfusionCell, ...] = ()
    findings: tuple[IntentEvalFinding, ...] = ()
    max_miss_rate: float = DEFAULT_MAX_MISS_RATE
    duration_ms: int = Field(default=0, ge=0)

    @property
    def positive_results(self) -> tuple[IntentSampleResult, ...]:
        return tuple(
            result
            for result in self.results
            if result.category is IntentSampleCategory.POSITIVE
        )

    @property
    def missed(self) -> tuple[IntentSampleResult, ...]:
        return tuple(
            result for result in self.results if result.verdict is IntentEvalVerdict.MISSED
        )

    @property
    def misrouted(self) -> tuple[IntentSampleResult, ...]:
        return tuple(
            result for result in self.results if result.verdict is IntentEvalVerdict.MISROUTED
        )

    @property
    def positive_total(self) -> int:
        return len(self.positive_results)

    @property
    def misroute_count(self) -> int:
        return len(self.misrouted)

    @property
    def miss_rate(self) -> float:
        """漏路由率。没有正例时按最高缺口（1.0）计，绝不按 0 计。

        "没有正例"是缺样例，不是完美覆盖；把它算成 0 会让最糟的情形看起来最好。
        """

        if not self.positive_total:
            return 1.0
        return len(self.missed) / self.positive_total

    @property
    def blocking(self) -> tuple[IntentEvalFinding, ...]:
        return tuple(finding for finding in self.findings if finding.blocking)

    @property
    def publishable(self) -> bool:
        """只有 `pass` 且没有阻断项才可发布。超时/崩溃永远不是 pass。"""

        return self.status is IntentEvalStatus.PASS and not self.blocking


def flow_label(flow_id: str | None, flow_version: str | None) -> str:
    if flow_id is None:
        return UNMATCHED_LABEL
    return f"{flow_id}@{flow_version}" if flow_version else flow_id


def evaluate_corpus(
    flows: Sequence[FlowDefinition],
    router: IntentRouter,
    *,
    min_positive: int = MIN_POSITIVE_SAMPLES,
    min_near_miss: int = MIN_NEAR_MISS_SAMPLES,
    max_miss_rate: float = DEFAULT_MAX_MISS_RATE,
) -> IntentEvalReport:
    """跑一遍语料。纯函数：输入相同则输出相同（`duration_ms` 除外）。

    `flows` 必须是"发布后会生效"的那一组版本，同一 `flow_id` 只能出现一次；重复直接
    抛 `ValueError`——那是装配错了，不该被折算成一条评测结论。
    """

    started = time.monotonic()
    duplicates = _duplicate_flow_ids(flows)
    if duplicates:
        raise ValueError(
            "评测语料里同一流程出现多个版本：" + "、".join(duplicates)
            + "；发布口径只能有一个版本，请由调用方先定版本"
        )

    ordered = sorted(flows, key=lambda flow: (flow.flow_id, flow.version))
    if not ordered:
        return IntentEvalReport(
            status=IntentEvalStatus.FAIL,
            findings=(
                IntentEvalFinding(
                    code=IntentEvalFindingCode.NO_PUBLISHED_FLOWS,
                    detail=(
                        "没有任何已发布流程参与评测：识别能力等于零，"
                        "这不是通过。请先发布一个带样例的流程。"
                    ),
                ),
            ),
            max_miss_rate=max_miss_rate,
            duration_ms=_elapsed_ms(started),
        )

    results: list[IntentSampleResult] = []
    coverage: list[SampleCoverage] = []
    findings: list[IntentEvalFinding] = []

    for flow in ordered:
        flow_coverage = sample_coverage(
            flow.intent_samples,
            flow_id=flow.flow_id,
            flow_version=flow.version,
            min_positive=min_positive,
            min_near_miss=min_near_miss,
        )
        coverage.extend(flow_coverage)
        for row in flow_coverage:
            if row.short:
                findings.append(
                    IntentEvalFinding(
                        code=IntentEvalFindingCode.SAMPLE_COUNT_SHORT,
                        detail=(
                            f"{flow_label(flow.flow_id, flow.version)} 的{row.label}不足："
                            f"{row.have}/{row.need}"
                        ),
                    )
                )
        for sample in sorted(flow.intent_samples, key=lambda item: item.sample_id):
            result = _judge(flow, sample, router)
            results.append(result)
            finding = _finding_for(flow, result)
            if finding is not None:
                findings.append(finding)

    # 漏路由是阈值判定，必须在拿到整体比例之后才能定"挡不挡"。
    missed = [result for result in results if result.verdict is IntentEvalVerdict.MISSED]
    if missed:
        positive_total = sum(
            1 for result in results if result.category is IntentSampleCategory.POSITIVE
        )
        rate = len(missed) / positive_total if positive_total else 1.0
        findings.append(
            IntentEvalFinding(
                code=IntentEvalFindingCode.POSITIVE_MISSED,
                detail=(
                    f"漏路由 {len(missed)}/{positive_total}"
                    f"（{rate:.1%}，上限 {max_miss_rate:.1%}）："
                    + "、".join(result.sample_id for result in missed)
                ),
                blocking=rate > max_miss_rate,
            )
        )

    return IntentEvalReport(
        status=_status_for(findings),
        flows=tuple(flow_label(flow.flow_id, flow.version) for flow in ordered),
        results=tuple(results),
        coverage=tuple(coverage),
        matrix=_matrix(results),
        findings=tuple(findings),
        max_miss_rate=max_miss_rate,
        duration_ms=_elapsed_ms(started),
    )


def _judge(flow: FlowDefinition, sample: IntentSample, router: IntentRouter) -> IntentSampleResult:
    match = router.route(sample.text)
    expected_id = flow.flow_id if sample.category is IntentSampleCategory.POSITIVE else None
    expected_version = flow.version if expected_id is not None else None
    actual_id = match.flow_id if match is not None else None
    actual_version = match.flow_version if match is not None else None

    if expected_id is None:
        # 不该被路由的样例：路由到任何流程都算误路由。
        verdict = (
            IntentEvalVerdict.PASS if match is None else IntentEvalVerdict.MISROUTED
        )
    elif match is None:
        verdict = IntentEvalVerdict.MISSED
    elif (actual_id, actual_version) == (expected_id, expected_version):
        verdict = IntentEvalVerdict.PASS
    else:
        # 版本不同也算误路由：话被路由到同一流程的旧版本，等于新版本没生效。
        verdict = IntentEvalVerdict.MISROUTED

    return IntentSampleResult(
        flow_id=flow.flow_id,
        flow_version=flow.version,
        sample_id=sample.sample_id,
        category=sample.category,
        expected_flow_id=expected_id,
        expected_flow_version=expected_version,
        actual_flow_id=actual_id,
        actual_flow_version=actual_version,
        verdict=verdict,
    )


def _finding_for(
    flow: FlowDefinition, result: IntentSampleResult
) -> IntentEvalFinding | None:
    if result.verdict is IntentEvalVerdict.MISSED:
        # 漏路由的结论要等整体比例算完才能定，这里不产出。
        return None
    if result.verdict is not IntentEvalVerdict.MISROUTED:
        return None
    if result.category is IntentSampleCategory.POSITIVE:
        return IntentEvalFinding(
            code=IntentEvalFindingCode.POSITIVE_MISROUTED,
            detail=(
                f"正例 {result.sample_id}（{flow_label(flow.flow_id, flow.version)}）"
                f"被识别到 {result.actual_label}"
            ),
        )
    label = "近似误例" if result.category is IntentSampleCategory.NEAR_MISS else "超范围例"
    return IntentEvalFinding(
        code=IntentEvalFindingCode.SAMPLE_ROUTED,
        detail=f"{label} {result.sample_id} 被识别到 {result.actual_label}",
    )


def _matrix(results: Sequence[IntentSampleResult]) -> tuple[ConfusionCell, ...]:
    counts: dict[tuple[str, str], int] = {}
    for result in results:
        key = (result.expected_label, result.actual_label)
        counts[key] = counts.get(key, 0) + 1
    return tuple(
        ConfusionCell(expected=expected, actual=actual, count=count)
        for (expected, actual), count in sorted(counts.items())
    )


def _duplicate_flow_ids(flows: Sequence[FlowDefinition]) -> list[str]:
    seen: set[str] = set()
    duplicates: list[str] = []
    for flow in flows:
        if flow.flow_id in seen and flow.flow_id not in duplicates:
            duplicates.append(flow.flow_id)
        seen.add(flow.flow_id)
    return duplicates


def _status_for(findings: Sequence[IntentEvalFinding]) -> IntentEvalStatus:
    """整体状态。超时/崩溃优先判"无结论"，其余看有没有阻断项。

    只看 `blocking` 而不是"有没有任何一条样例没通过"：漏路由的上限是可配置的，
    上限之内的漏点应当放行但在报告里留痕。
    """

    codes = {finding.code for finding in findings}
    if codes & {IntentEvalFindingCode.EVAL_TIMEOUT, IntentEvalFindingCode.EVAL_CRASHED}:
        return IntentEvalStatus.UNCONFIRMED
    return (
        IntentEvalStatus.FAIL
        if any(finding.blocking for finding in findings)
        else IntentEvalStatus.PASS
    )


def _elapsed_ms(started: float) -> int:
    return max(0, int((time.monotonic() - started) * 1000))
