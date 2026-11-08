"""管理侧 SOP 配置服务：把"运维在浏览器里改流程"收拢成一组受控操作。

边界（对应 Issue #51 的高风险评级）：

- 只通过 `FlowStore` 写版本与发布指针，不碰库表；版本不可变，发布状态永远由
  指针表达（见 #49）。因此管理侧**不能**覆盖一个已存在的版本。
- 发布走 `FlowReleaseService`：有回放用例时未通过就拦下，指针不动（见 #50）。
  回滚不一样——它恢复的是一个已经发布过的状态，不是新版本准入，若也卡在回放
  门禁上，运维在事故里反而无法快速退回；因此回滚只校验目标版本确实存在。
- 每次改动发布指针后调用 `FlowRegistry.reload()`（原地重载）：否则库里的指针
  已经移了，进程内目录还停在旧版本，群众侧"看起来发布成功"却没生效。
- 不读、不返回任何凭据。主体由 API 层鉴权后作为 `actor` 传入，写入指针行
  （`published_by` / `disabled_by`），只用于审计归属。本轮不做双人复核，署名
  是这条链路唯一的责任锚点，因此三个写操作都要求署名，预演例外（它不写状态）。

刻意不做的事：自由文本 DSL / YAML 编辑、多级审批、意图规则的运行时装配
（意图样例随版本内容提交、发布时由 #52 的门禁裁决，规则本身仍由装配期代码给出）、
多实例间的发布同步（属 #53，见部署文档）。
"""

from __future__ import annotations

from pydantic import BaseModel

from workpanda.application.release import FlowReleaseOutcome, FlowReleaseService
from workpanda.domain.flow import (
    FlowDefinition,
    FlowFieldError,
    FlowRegistry,
    FlowReleaseMissing,
    FlowStatus,
    FlowStore,
    FlowVersionExists,
    flow_field_errors,
)
from workpanda.domain.replay import (
    ReplayCase,
    ReplayCaseExists,
    ReplayCaseStore,
    ReplayReport,
)


class AdminDraftInvalid(ValueError):
    """草稿不符合后端 schema。带着字段级定位，前端可标到具体输入框。"""

    def __init__(self, errors: list[FlowFieldError]) -> None:
        super().__init__("流程草稿校验未通过")
        self.errors = errors


class AdminFlowConflict(RuntimeError):
    """同版本已存在，或用例已存在。版本不可变，绝不覆盖。"""


class AdminFlowNotFound(LookupError):
    """指定的流程版本或用例不存在。"""


class FlowSummary(BaseModel):
    flow_id: str
    version: str
    title: str
    published: bool
    case_count: int
    # 指针行上的署名，只在这一行确实是指针时才有意义（`published=True`）。
    # 停用的流程把停用人挂在它最后指向的那一版上，避免在整张列表里重复同一个人名。
    published_by: str | None = None
    disabled_by: str | None = None
    updated_at: str | None = None


class FlowVersionDetail(BaseModel):
    flow: FlowDefinition
    published: bool
    cases: list[ReplayCase]


class PublishOutcome(BaseModel):
    flow_id: str
    version: str
    # False 只出现在预演（apply=False）：界面必须能区分"预演通过"与"已发布"。
    published: bool
    replay_ran: bool
    report: dict | None = None
    message: str = ""
    # 预演没有移动指针，因此没有署名人——留空而不是回填调用者，避免界面把
    # "谁看过能不能发"显示成"谁发布了"。
    published_by: str | None = None


def serialize_report(report: ReplayReport) -> dict:
    """把回放报告连同**总判定**一起序列化。

    `ReplayReport.verdict` / `publishable` 是 property，不进 `model_dump()`；
    管理侧界面恰恰要显示"总判定"，少了它前端只能自己从逐条结果重算，那等于
    把门禁口径复制一份到浏览器里，迟早会对不上。这里在服务端补齐。
    """

    payload = report.model_dump(mode="json")
    payload["verdict"] = report.verdict.value
    payload["publishable"] = report.publishable
    return payload


class AdminFlowService:
    def __init__(
        self,
        *,
        flows: FlowStore,
        cases: ReplayCaseStore,
        release: FlowReleaseService,
        registry: FlowRegistry,
    ) -> None:
        self.flows = flows
        self.cases = cases
        self.release = release
        self.registry = registry

    # ---------- 读 ----------

    def overview(self) -> list[FlowSummary]:
        releases = {release.flow_id: release for release in self.flows.releases()}
        summaries: list[FlowSummary] = []
        for flow in self.flows.list_versions():
            release = releases.get(flow.flow_id)
            published = (
                release is not None
                and release.published
                and release.version == flow.version
            )
            summaries.append(
                FlowSummary(
                    flow_id=flow.flow_id,
                    version=flow.version,
                    title=flow.title,
                    published=published,
                    case_count=len(self.cases.list(flow.flow_id, flow.version)),
                    published_by=(
                        release.published_by if release is not None and published else None
                    ),
                    # 停用标记只挂在停用前最后指向的那一版上：挂到每一版等于在列表里
                    # 重复同一条事实，反而看不出当时停的是哪一版。
                    disabled_by=(
                        release.disabled_by
                        if release is not None
                        and not release.published
                        and release.version == flow.version
                        else None
                    ),
                    updated_at=(
                        release.updated_at if release is not None and published else None
                    ),
                )
            )
        return summaries

    def detail(self, flow_id: str, version: str) -> FlowVersionDetail:
        flow = self._require_version(flow_id, version)
        pointers = self.flows.published_versions()
        return FlowVersionDetail(
            flow=flow,
            published=pointers.get(flow_id) == version,
            cases=list(self.cases.list(flow_id, version)),
        )

    # ---------- 草稿与用例 ----------

    def create_draft(self, draft: FlowDefinition) -> FlowDefinition:
        errors = flow_field_errors(draft)
        if errors:
            raise AdminDraftInvalid(errors)
        # status 只是作者态标记，运行语义由指针决定；强制草稿，避免请求体自带
        # "published" 让界面误以为这一步已经发布。
        flow = draft.model_copy(update={"status": FlowStatus.DRAFT})
        try:
            self.flows.add_version(flow)
        except FlowVersionExists as exc:
            raise AdminFlowConflict(str(exc)) from exc
        self.registry.reload(self.flows)
        return flow

    def add_case(self, flow_id: str, version: str, draft: ReplayCase) -> ReplayCase:
        self._require_version(flow_id, version)
        # flow_id / version 取自路径，不接受请求体自报：用例绑错版本会被回放判
        # `invalid`，而那种错误在界面上看起来像"流程坏了"，很难查。
        case = draft.model_copy(update={"flow_id": flow_id, "flow_version": version})
        try:
            self.cases.add(case)
        except ReplayCaseExists as exc:
            raise AdminFlowConflict(str(exc)) from exc
        return case

    def delete_case(self, flow_id: str, version: str, case_id: str) -> None:
        self._require_version(flow_id, version)
        if not self.cases.delete(flow_id, version, case_id):
            raise AdminFlowNotFound(f"回放用例 {case_id!r} 不存在")

    def replay(self, flow_id: str, version: str) -> ReplayReport:
        self._require_version(flow_id, version)
        try:
            return self.release.check(flow_id, version)
        except FlowReleaseMissing as exc:
            raise AdminFlowNotFound(str(exc)) from exc

    # ---------- 发布指针 ----------

    def publish(
        self, flow_id: str, version: str, *, actor: str, apply: bool = True
    ) -> PublishOutcome:
        self._require_version(flow_id, version)
        # `IntentEvalBlocked` 与 `FlowReleaseBlocked` 故意都不在这里吞掉：门禁拦下时
        # 指针没动，报告要原样交给界面，运维才能看到是"样例不够/误路由"还是
        # "某条回放用例以什么判定挡住"。由 API 层分别映射成 409 的两个错误码。
        try:
            outcome: FlowReleaseOutcome = self.release.release(
                flow_id, version, actor=actor, apply=apply
            )
        except FlowReleaseMissing as exc:
            raise AdminFlowNotFound(str(exc)) from exc
        if apply:
            self.registry.reload(self.flows)
        return PublishOutcome(
            flow_id=outcome.flow_id,
            version=version,
            published=outcome.published,
            replay_ran=outcome.replay_ran,
            report=(
                serialize_report(outcome.report) if outcome.report is not None else None
            ),
            message=(
                "已发布，群众侧下一次创建任务即使用该版本"
                if apply
                else "预演通过，尚未发布（发布指针未移动）"
            ),
            published_by=outcome.published_by,
        )

    def disable(self, flow_id: str, *, actor: str) -> None:
        self.flows.disable(flow_id, actor=actor)
        self.registry.reload(self.flows)

    def rollback(self, flow_id: str, version: str, *, actor: str) -> None:
        """回滚也是一次"把指针移到某个版本"，因此签名与发布同源（`published_by`）。"""

        self._require_version(flow_id, version)
        try:
            self.flows.publish(flow_id, version, actor=actor)
        except FlowReleaseMissing as exc:
            raise AdminFlowNotFound(str(exc)) from exc
        self.registry.reload(self.flows)

    def _require_version(self, flow_id: str, version: str) -> FlowDefinition:
        for flow in self.flows.list_versions(flow_id):
            if flow.version == version:
                return flow
        raise AdminFlowNotFound(f"流程 {flow_id!r} 版本 {version!r} 不存在")
