"""SOP 发布前回放：用例、观测与断言。

要解决的问题：流程改完只能上线后才知道对不对。这里把"发布前跑一遍"变成可重复的
判定——同一版本 + 同一用例，必须给出同一个结论。

三条设计约束（对应验收标准里的"不应发生"）：

1. **回放不产生副作用**：判定由观测数据给出，观测来自隔离执行器（见
   `application/replay.py`），本模块不接触任何执行器；
2. **没有证据不算通过**：只有"观测与期望一致"才是 `pass`。超时、崩溃、用例缺字段
   各有独立结论，绝不被折叠成 `pass`；
3. **结论可重复**：报告里不含任务 ID、时间戳、原始参数值，只含步骤序列、工具名与
   参数摘要——同一版本 + 同一用例跑两次，观测部分逐字节相同。

关于"判定 pass"与"办成了"的区别：回放里带副作用的工具一律按未确认记账，所以一条
提交类流程的 `pass` 只表示"步骤与工具符合预期"，不表示真的提交成功。这一点必须在
运维口径里说清，否则发布门禁会变成"看起来都绿了"的错觉。
"""

from __future__ import annotations

import hashlib
import json
from enum import StrEnum
from typing import Protocol

from pydantic import BaseModel, Field, field_validator

from workpanda.domain.flow import FlowDefinition, StepKind
from workpanda.domain.models import TaskStatus

# 允许写进期望的终态。刻意只开放这四种：`cancelled` 是人的决定、`waiting_*` 是中间态，
# 拿它们当发布依据等于把"没跑完"写成"预期结果"。
REPLAY_TERMINALS: frozenset[TaskStatus] = frozenset(
    {
        TaskStatus.COMPLETED,
        TaskStatus.FAILED,
        TaskStatus.OUTCOME_UNCONFIRMED,
        TaskStatus.HANDOFF_REQUIRED,
    }
)


class ReplayVerdict(StrEnum):
    """单条用例的判定。

    `invalid` 与 `fail` 必须分开：用例自己写错了（缺字段、引用了别的版本）不能算流程
    回归，否则每次改用例都会看起来像流程坏了；但也不能算通过——没有基线就没有依据。
    """

    PASS = "pass"
    FAIL = "fail"
    INVALID = "invalid"
    # 回放超时或崩溃：没有结论。既不是通过，也不能当"没发现问题"。
    UNCONFIRMED = "unconfirmed"
    # 被中断（运维主动停止 / 门禁取消）。不进发布依据。
    ABORTED = "aborted"


class ReplayFindingCode(StrEnum):
    CASE_FLOW_MISMATCH = "case_flow_mismatch"
    CASE_UNKNOWN_FIELD = "case_unknown_field"
    CASE_MISSING_FIELDS = "case_missing_fields"
    # 用例走到了"需要用户本人操作"的节点。回放**不能**替用户做这件事（也不该
    # 假装做了），所以这是一个"回放测不到这里"的结论，不是流程回归。
    #
    # 单独一个码而不是并进 `CASE_MISSING_FIELDS`：后者会让人去改用例补字段，
    # 而这一屏根本没有字段可补——照着报错去找字段，只会找上一个不存在的入口。
    CASE_USER_ACTION_REQUIRED = "case_user_action_required"
    UNAUTHORIZED_TOOL = "unauthorized_tool"
    STEP_MISSING = "step_missing"
    STEP_UNEXPECTED = "step_unexpected"
    STEP_ORDER_MISMATCH = "step_order_mismatch"
    TOOL_SET_MISMATCH = "tool_set_mismatch"
    TERMINAL_MISMATCH = "terminal_mismatch"
    REPLAY_TIMEOUT = "replay_timeout"
    REPLAY_CRASHED = "replay_crashed"
    REPLAY_ABORTED = "replay_aborted"


class StepOutcome(StrEnum):
    """逐 step 结论。只回答"这一步走完没有"，不回答"办成没有"。"""

    COMPLETED = "completed"
    PAUSED = "paused"
    INCOMPLETE = "incomplete"


def status_value(status: TaskStatus | None) -> str | None:
    """终态的文本值。`None` 保持 `None`（"没有终态"与"终态叫 None"不是一回事）。

    刻意统一走这个函数比较而不是 `is`：`model_copy(update=...)` 不跑校验，传进来的
    可能是裸字符串。用同一把尺子量两边，"completed" 与 `TaskStatus.COMPLETED` 才
    不会被判成不一致。
    """

    return None if status is None else str(status)


def digest_arguments(arguments: dict[str, str]) -> str:
    """参数摘要。回放报告里只留它，不留原文。

    对 (参数名, 取值) 排序后取 sha256：同一组参数恒得同一个摘要，因此"同一版本 +
    同一用例可重复"这件事可以在报告层面被逐字节验证；同时运维手上的报告不含
    群众填写的原始内容。
    """

    payload = json.dumps(
        sorted((str(key), str(value)) for key, value in arguments.items()),
        ensure_ascii=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class ReplayExpectation(BaseModel):
    """对一次回放的期望。三个维度独立：`None` 表示"这一维不约束"。

    为什么用 `None` 而不是空容器表示"不约束"：`steps=()` 与 `tools=()` 是有意义的
    断言——"一步都不该走"和"一次工具都不该调"。若两者都表示不约束，就再也没有
    办法表达"这条流程不允许调用任何工具"这条最常见的负向期望。
    """

    steps: tuple[str, ...] | None = None
    tools: tuple[str, ...] | None = None
    terminal: TaskStatus | None = None

    @field_validator("terminal")
    @classmethod
    def _terminal_must_be_final(cls, value: TaskStatus | None) -> TaskStatus | None:
        if value is not None and value not in REPLAY_TERMINALS:
            allowed = "、".join(sorted(status.value for status in REPLAY_TERMINALS))
            raise ValueError(f"回放期望的终态只能是 {allowed}，收到 {value.value!r}")
        return value


class ReplayCase(BaseModel):
    """一条回放用例。是数据，不是代码：与流程版本一起存库。

    `case_id` 只在 `(flow_id, version)` 内唯一：同一个业务场景在不同版本上是两条
    用例，各自的期望可以不同（改流程往往就是要改期望）。
    """

    case_id: str = Field(min_length=1, max_length=128)
    flow_id: str = Field(min_length=1, max_length=128)
    flow_version: str = Field(min_length=1, max_length=64)
    # 用例输入：只放字段名与取值，等价于"群众会这样填"。
    slots: dict[str, str] = Field(default_factory=dict)
    expect: ReplayExpectation = Field(default_factory=ReplayExpectation)
    note: str | None = Field(default=None, max_length=500)


class ReplayToolCall(BaseModel):
    """回放观测到的一次工具调用（名 + 参数摘要）。

    `replay` 恒为 True：回放里没有任何真实调用。留这个字段是为了让下游（报告、
    门禁、将来的比对工具）不必靠"我在回放里"这个上下文去猜。
    """

    step_id: str
    name: str
    # 参数名。取值不进报告，只有摘要。
    fields: tuple[str, ...] = ()
    argument_digest: str
    replay: bool = True
    # 有副作用 ⇒ 回放按未确认记账（外部系统可能已被调用，但这次没调）。
    side_effecting: bool = True


class ReplayStepResult(BaseModel):
    step_id: str
    kind: str
    outcome: StepOutcome


class ReplayFinding(BaseModel):
    code: ReplayFindingCode
    detail: str


class ReplayCaseResult(BaseModel):
    """一条用例的完整结论：判定 + 依据 + 逐 step 观测。"""

    case_id: str
    verdict: ReplayVerdict
    findings: tuple[ReplayFinding, ...] = ()
    steps: tuple[ReplayStepResult, ...] = ()
    tools: tuple[ReplayToolCall, ...] = ()
    terminal: str | None = None
    duration_ms: int = Field(default=0, ge=0)

    @property
    def codes(self) -> tuple[str, ...]:
        return tuple(finding.code.value for finding in self.findings)


# 报告总判定的严重度排序：大的赢。门禁只看 `publishable`，这里只是为了让人一眼看懂
# "这批用例整体到哪一步了"。
_VERDICT_SEVERITY: dict[ReplayVerdict, int] = {
    ReplayVerdict.PASS: 0,
    ReplayVerdict.ABORTED: 1,
    ReplayVerdict.INVALID: 2,
    ReplayVerdict.UNCONFIRMED: 3,
    ReplayVerdict.FAIL: 4,
}

# 这些结论会挡住发布。`aborted` 不在其中——被中断的用例不进发布依据，它既不放行
# 也不阻挡，最终是否放行由"还有没有通过的用例"决定。
_BLOCKING_VERDICTS: frozenset[ReplayVerdict] = frozenset(
    {ReplayVerdict.FAIL, ReplayVerdict.INVALID, ReplayVerdict.UNCONFIRMED}
)


class ReplayReport(BaseModel):
    """一个版本上全部用例的回放结果。门禁的唯一输入。"""

    flow_id: str
    flow_version: str
    results: tuple[ReplayCaseResult, ...] = ()

    @property
    def verdict(self) -> ReplayVerdict:
        if not self.results:
            # 一条用例都没跑，没有"整体通过"这回事。门禁把它当"无依据"处理。
            return ReplayVerdict.ABORTED
        return max(
            (result.verdict for result in self.results),
            key=lambda verdict: _VERDICT_SEVERITY[verdict],
        )

    @property
    def blocking(self) -> tuple[ReplayCaseResult, ...]:
        return tuple(
            result for result in self.results if result.verdict in _BLOCKING_VERDICTS
        )

    @property
    def passed(self) -> tuple[ReplayCaseResult, ...]:
        return tuple(result for result in self.results if result.verdict is ReplayVerdict.PASS)

    @property
    def publishable(self) -> bool:
        """能否发布。两个条件缺一不可：有通过的用例，且没有任何阻挡项。

        "有通过"这一条是为了堵住"全部中断"这种情形：那时 `blocking` 是空的，
        可如果把"没有阻挡"直接当放行，一次什么都没跑的回放就能放行版本。
        """

        return bool(self.passed) and not self.blocking


def validate_case(case: ReplayCase, flow: FlowDefinition) -> list[ReplayFinding]:
    """用例自身的有效性检查。返回非空即判 `invalid`，不进流程断言。

    放在回放之前做，是为了把"用例写错了"与"流程改坏了"分开报。混在一起的后果是
    运维会去查流程，而实际上该改的是用例。
    """

    findings: list[ReplayFinding] = []
    if case.flow_id != flow.flow_id or case.flow_version != flow.version:
        findings.append(
            ReplayFinding(
                code=ReplayFindingCode.CASE_FLOW_MISMATCH,
                detail=(
                    f"用例绑定的是 {case.flow_id}@{case.flow_version}，"
                    f"实际回放的是 {flow.flow_id}@{flow.version}"
                ),
            )
        )
        # 绑错版本时后面的字段检查没有意义：字段是那个版本的字段。
        return findings

    declared = declared_fields(flow)
    unknown = sorted(set(case.slots) - declared)
    if unknown:
        findings.append(
            ReplayFinding(
                code=ReplayFindingCode.CASE_UNKNOWN_FIELD,
                detail=f"用例给了流程未声明的字段：{', '.join(unknown)}",
            )
        )
    return findings


def declared_fields(flow: FlowDefinition) -> set[str]:
    """流程声明过的全部字段名。与 `application/task_service.py` 的口径一致。"""

    return {field for step in flow.steps for field in step.required_fields}


def declared_tools(flow: FlowDefinition) -> tuple[str, ...]:
    """流程可能用到的工具集合（按执行步骤声明，保留首次出现顺序）。"""

    seen: list[str] = []
    for step in flow.steps:
        if step.kind is StepKind.EXECUTE and step.tool and step.tool not in seen:
            seen.append(step.tool)
    return tuple(seen)


def evaluate(
    flow: FlowDefinition,
    case: ReplayCase,
    *,
    steps: tuple[ReplayStepResult, ...],
    tools: tuple[ReplayToolCall, ...],
    terminal: TaskStatus | None,
) -> list[ReplayFinding]:
    """断言观测与期望是否一致。

    纯函数：输入相同则输出相同。因此"回放结果随时间漂移"这件事只能在观测层发生，
    不会藏在这里。
    """

    findings: list[ReplayFinding] = []
    observed_steps = tuple(step.step_id for step in steps)
    observed_tools = tuple(call.name for call in tools)
    expect = case.expect

    if expect.steps is not None:
        missing = [step_id for step_id in expect.steps if step_id not in observed_steps]
        extra = [step_id for step_id in observed_steps if step_id not in expect.steps]
        if missing:
            findings.append(
                ReplayFinding(
                    code=ReplayFindingCode.STEP_MISSING,
                    detail=f"期望经过但没走过的步骤：{', '.join(missing)}",
                )
            )
        if extra:
            findings.append(
                ReplayFinding(
                    code=ReplayFindingCode.STEP_UNEXPECTED,
                    detail=f"多走了不在期望里的步骤：{', '.join(extra)}",
                )
            )
        if not missing and not extra and observed_steps != tuple(expect.steps):
            # 少调/多调由上面两条报；到这里只剩"集合一样但顺序或次数不对"。
            findings.append(
                ReplayFinding(
                    code=ReplayFindingCode.STEP_ORDER_MISMATCH,
                    detail=(
                        f"步骤集合相同但顺序不一致：期望 {'→'.join(expect.steps)}，"
                        f"实际 {'→'.join(observed_steps)}"
                    ),
                )
            )

    if expect.tools is not None:
        missing_tools = [name for name in expect.tools if name not in observed_tools]
        extra_tools = [name for name in observed_tools if name not in expect.tools]
        if missing_tools or extra_tools:
            parts = []
            if missing_tools:
                parts.append(f"期望调用但没有：{', '.join(missing_tools)}")
            if extra_tools:
                parts.append(f"不该出现却出现：{', '.join(extra_tools)}")
            findings.append(
                ReplayFinding(
                    code=ReplayFindingCode.TOOL_SET_MISMATCH,
                    detail="；".join(parts),
                )
            )

    # 未授权工具与"期望"无关：无论用例怎么期望，流程都不该越过自己声明的工具边界。
    seen_unauthorized: set[str] = set()
    for call in tools:
        step = flow.step(call.step_id)
        if step is None or step.tool != call.name:
            label = f"{call.step_id}:{call.name}"
            if label in seen_unauthorized:
                continue
            seen_unauthorized.add(label)
            expected_tool = step.tool if step is not None else "（无此步骤）"
            findings.append(
                ReplayFinding(
                    code=ReplayFindingCode.UNAUTHORIZED_TOOL,
                    detail=(
                        f"步骤 {call.step_id} 调用了未授权的工具 {call.name}"
                        f"（该步骤声明的工具：{expected_tool or '无'}）"
                    ),
                )
            )

    if expect.terminal is not None and status_value(terminal) != status_value(expect.terminal):
        findings.append(
            ReplayFinding(
                code=ReplayFindingCode.TERMINAL_MISMATCH,
                detail=(
                    f"期望终态 {status_value(expect.terminal)}，"
                    f"实际 {status_value(terminal) or '未知'}"
                ),
            )
        )
    return findings


class ReplayCaseExists(ValueError):
    """同一 `(flow_id, version, case_id)` 已存在。"""


class ReplayCaseStore(Protocol):
    """回放用例的持久化端口。

    用例跟着流程版本走：删版本等于删它的用例，发布指针与用例因此永远对得上。
    """

    def add(self, case: ReplayCase) -> None:
        """新增用例。已存在则抛 `ReplayCaseExists`，不做静默覆盖。"""

    def delete(self, flow_id: str, version: str, case_id: str) -> bool:
        """删除用例，返回是否真的删掉了。"""

    def list(self, flow_id: str, version: str) -> list[ReplayCase]:
        """按 `case_id` 排序返回该版本的全部用例。"""
