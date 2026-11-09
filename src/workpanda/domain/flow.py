"""服务端固定业务流程定义。

流程定义只存在于服务端，不下发到浏览器，也不允许模型生成或改写步骤。
本模块只描述结构与校验，不包含任何具体业务事项。

版本与发布：版本内容不可变，发布状态由**指针**表达（见 `FlowStore`）。同一条
记录既当"已发布"又当"草稿"必然漂移，所以库里只存版本，指针只有一个。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from pydantic import BaseModel, Field

from workpanda.domain.intent import IntentRule
from workpanda.domain.intent_sample import IntentSample


class FlowStatus(StrEnum):
    DRAFT = "draft"
    PUBLISHED = "published"
    DISABLED = "disabled"


class StepKind(StrEnum):
    COLLECT = "collect"
    EXECUTE = "execute"
    HANDOFF = "handoff"


class StepDefinition(BaseModel):
    step_id: str = Field(min_length=1, max_length=128)
    kind: StepKind
    title: str = Field(min_length=1, max_length=200)
    required_fields: tuple[str, ...] = ()
    prompt: str | None = Field(default=None, max_length=1000)
    # 执行步骤的明确指令；由流程定义给出，模型不得自行改写。
    instruction: str | None = Field(default=None, max_length=1000)
    tool: str | None = Field(default=None, max_length=128)
    # 需要群众确认的执行节点：暂停、签发一次性凭证、执行前二次校验。
    requires_confirmation: bool = False
    confirm_message: str | None = Field(default=None, max_length=1000)
    fail_to_handoff: bool = True
    # 群众侧展示用的字段中文名（可选）。字段名是内部标识（`subject`、`contact`），
    # 直接印在页面上等于让群众读代码；但页面也不能自己编一个说法（"主题"还是
    # "办理事项"只有流程作者知道）。所以标签归作者，页面只负责取。
    # 留空时页面退回显示字段名本身——诚实但不好看，好过猜错。
    field_labels: dict[str, str] = Field(default_factory=dict)
    # 群众可见正文的行名。只有执行步骤会产出正文（模型契约里的 `answer`），所以
    # 这个标签也只对执行步骤有意义；声明了它，办结页才会显示这一段。
    #
    # 为什么标签归作者而不是平台：平台不知道这段正文在业务上叫什么（"办理结果"还是
    # "所需材料"），猜一个就会把话说到别的事项上去。留空表示"本步骤没有面向群众的
    # 正文"，此时执行器的平台元数据也不会被顶上来充数。
    answer_label: str | None = Field(default=None, max_length=100)


class FlowDefinition(BaseModel):
    flow_id: str = Field(min_length=1, max_length=128)
    version: str = Field(min_length=1, max_length=64)
    title: str = Field(min_length=1, max_length=200)
    status: FlowStatus = FlowStatus.PUBLISHED
    steps: tuple[StepDefinition, ...] = Field(min_length=1)
    # 意图样例：发布前的必备材料（见 `domain/intent_sample.py`）。刻意放在版本内容里，
    # 而不是单独一张可改的表——"发布时校验的样例"必须就是"这一版生效的样例"。
    # 草稿允许为空，条数要求由发布门禁（`application/intent_eval.py`）裁决。
    intent_samples: tuple[IntentSample, ...] = ()
    # 意图规则（D2=B）：识别规则是**流程版本的一部分**，不是装配期代码里的常量。
    #
    # 放在这里的原因是"发布"这件事必须一次生效：规则若留在代码里，运维发布一个
    # 新版本只移动了指针，识别口径还是旧的——群众说的话仍被路由到上一版，甚至
    # 路由到一个已不存在的版本（#73 的复现就是这样：发布后群众侧直接
    # `FlowNotRunnable`）。
    #
    # 规则只能指向**自己所属的版本**，由 `flow_field_errors()` 强制。
    intent_rules: tuple[IntentRule, ...] = ()

    def step(self, step_id: str) -> StepDefinition | None:
        for step in self.steps:
            if step.step_id == step_id:
                return step
        return None

    def next_step(self, step_id: str | None) -> StepDefinition | None:
        if step_id is None:
            return self.steps[0]
        for index, step in enumerate(self.steps):
            if step.step_id == step_id:
                return self.steps[index + 1] if index + 1 < len(self.steps) else None
        return None


class FlowNotFound(LookupError):
    """流程或版本不存在。"""


class FlowNotRunnable(ValueError):
    """流程存在但未发布。"""


class FlowVersionExists(ValueError):
    """同一 (flow_id, version) 已存在。版本不可变，不允许覆盖。"""


class FlowReleaseMissing(LookupError):
    """发布指针指向的版本不存在。"""


# 署名长度上限。主体名（`OpsPrincipal.subject`）本身已被限制在 64 字符内，这个上限
# 是针对命令行与脚本入口的：宁可写不进去，也不要让一条被粘贴进来的整段日志冒充人名。
MAX_ACTOR_LENGTH = 128


def require_actor(actor: str) -> str:
    """校验并规范化署名，返回去掉首尾空白的值。

    空串与纯空白一律拒绝：库里留下一个空署名，事后与"没有署名"无法区分，却会让
    读者以为"这件事有人负责"。发布、回滚、停用都是不可撤销的责任锚点，这里不做
    静默默认值——默认值等于把"忘了传"变成一条查不出来的历史。
    """

    cleaned = (actor or "").strip()
    if not cleaned:
        raise ValueError("actor must not be blank; 发布/回滚/停用必须记录署名")
    if len(cleaned) > MAX_ACTOR_LENGTH:
        raise ValueError(f"actor must be at most {MAX_ACTOR_LENGTH} characters")
    return cleaned


@dataclass(frozen=True, slots=True)
class FlowRelease:
    """一条发布指针行的只读视图，带署名。

    `version` 是指针最后一次指向的版本。停用后仍保留它：删行会同时删掉
    "谁停用的"这条线索，而停用恰恰是最需要追责的动作。判断"现在到底跑不跑"
    只能看 `published`，不能看 `version` 是否存在。
    """

    flow_id: str
    version: str
    published: bool
    published_by: str | None
    # 指针最后一次移动（发布/回滚）的时间。停用**不**改它：停用不移动指针，
    # 若覆盖成停用时刻，`published_by` 与这个时间就指向两件不同的事。
    updated_at: str
    disabled_by: str | None


class FlowStore(Protocol):
    """流程定义的持久化端口。

    只表达两件事：**有哪些版本**、**哪个版本是当前发布指针**。发布状态不写进
    版本内容——否则"发布"要改两条记录，改一半就是两种真相。

    移动指针与停用都必须带署名：本轮不做双人复核，署名是这条链路唯一的责任锚点。
    """

    def add_version(self, flow: FlowDefinition) -> None:
        """新增一个版本。已存在则抛 `FlowVersionExists`，绝不覆盖。"""

    def publish(self, flow_id: str, version: str, *, actor: str) -> None:
        """把发布指针移向指定版本，并记下是谁做的。版本不存在则抛 `FlowReleaseMissing`。

        `actor` 是**仅关键字必填**参数：漏传是类型错误，不会被静默补成空串。
        """

    def disable(self, flow_id: str, *, actor: str) -> None:
        """停用：在指针行上打标记，历史版本与发布署名都保留。

        不做物理删除——删掉那一行会连"谁发布的、谁停用的"一起抹掉。
        """

    def published_versions(self) -> dict[str, str]:
        """`flow_id -> 已发布版本`。没有指针（含已停用）的流程不出现在结果里。"""

    def releases(self) -> list[FlowRelease]:
        """每一条指针行（含已停用的），按 `flow_id` 排序。供署名台账只读查询。"""

    def list_versions(self, flow_id: str | None = None) -> list[FlowDefinition]:
        """按 `(flow_id, version)` 排序返回版本，不做发布状态过滤。"""


class FlowRegistry:
    """进程内流程目录。启动时由 `FlowStore` 装载，运行期只读。

    发布状态在装载时**派生**：指针指向的版本是 `PUBLISHED`，其余是 `DRAFT`。
    因此版本内容里的 `status` 字段是作者态标记，不参与运行语义。
    """

    def __init__(self, flows: list[FlowDefinition] | None = None) -> None:
        self._flows: dict[tuple[str, str], FlowDefinition] = {}
        # 目录的代数：每次装载/替换 +1。识别路由器靠它判断"我手里的规则过期了没有"，
        # 这样"发布后重建路由器"就不需要任何一个调用方记得去做（#73）。
        self._generation = 0
        for flow in flows or []:
            self.register(flow)

    @classmethod
    def from_store(cls, store: FlowStore) -> FlowRegistry:
        pointers = store.published_versions()
        derived = [
            flow.model_copy(
                update={
                    "status": (
                        FlowStatus.PUBLISHED
                        if pointers.get(flow.flow_id) == flow.version
                        else FlowStatus.DRAFT
                    )
                }
            )
            for flow in store.list_versions()
        ]
        return cls(derived)

    def register(self, flow: FlowDefinition) -> None:
        validate_flow(flow)
        self._flows[(flow.flow_id, flow.version)] = flow
        self._generation += 1

    def reload(self, store: FlowStore) -> None:
        """从版本库重新装载，**原地替换**字典，保持本对象身份不变。

        管理侧发布 / 回滚 / 停用改的是库里的指针，而 `TaskService`、`FlowRunner`
        与路由器都持有本对象的引用。若改为"新建一个 registry 塞回容器"，这些
        已建好的服务仍盯着旧对象，群众侧就会继续跑旧版本——看起来"发布成功"
        却没生效。整表一次赋值保证并发读只会看到旧表或新表，不会看到半张表。

        历史版本全部保留（不是只留指针那一条）：在途任务用 `require(flow_id,
        旧版本)` 继续推进，回滚不能把它们打断。
        """

        self._flows = type(self).from_store(store)._flows
        self._generation += 1

    @property
    def generation(self) -> int:
        """目录的代数。变了就说明"当前有哪些版本、哪一版已发布"变了。

        给"必须跟着目录走"的派生对象用：识别路由器靠它判断缓存是否过期，
        少一个"记得在发布后重建"的人工步骤，也就少一处会忘掉的地方。
        """

        return self._generation

    def versions(self) -> list[FlowDefinition]:
        """全部版本（含未发布），按 `(flow_id, version)` 排序。

        这是"目录里有什么"，不是"现在跑什么"。运行时识别口径取
        `published()`——草稿的规则不该生效。
        """

        return [flow for _, flow in sorted(self._flows.items())]

    def published(self) -> list[FlowDefinition]:
        """当前发布指针指向的版本，按 `(flow_id, version)` 排序。

        识别规则只从这些版本收集：一条草稿版本的规则若参与路由，群众的话会被
        路由到一个 `resolve()` 必然拒绝的版本上——识别"成功"却建不了任务，
        看起来像群众说错了话（#73）。
        """

        return [
            flow
            for _, flow in sorted(self._flows.items())
            if flow.status is FlowStatus.PUBLISHED
        ]

    def resolve(self, flow_id: str, version: str | None = None) -> FlowDefinition:
        """取**可执行**的流程：新任务只能落在发布指针上。

        指定版本时不做"退回最大版本"的兜底——那会让"请求 v2、拿到 v3"变成静默行为。
        """

        if version is not None:
            try:
                flow = self._flows[(flow_id, version)]
            except KeyError as exc:
                raise FlowNotFound(
                    f"flow {flow_id!r} version {version!r} is not registered"
                ) from exc
            return self._published(flow)
        candidates = [
            flow for (registered_id, _), flow in self._flows.items() if registered_id == flow_id
        ]
        if not candidates:
            raise FlowNotFound(f"flow {flow_id!r} is not registered")
        published = [flow for flow in candidates if flow.status is FlowStatus.PUBLISHED]
        if not published:
            raise FlowNotRunnable(f"flow {flow_id!r} has no published version")
        return max(published, key=lambda flow: flow.version)

    def require(self, flow_id: str, version: str) -> FlowDefinition:
        """取**已存在**的版本，不检查发布状态。

        已建任务必须能继续跑：回滚只移动发布指针，若这里也要求"当前已发布"，
        一次回滚就会让在途任务的回答与确认全部失败。新任务的准入走 `resolve`。
        """

        try:
            return self._flows[(flow_id, version)]
        except KeyError as exc:
            raise FlowNotFound(
                f"flow {flow_id!r} version {version!r} is not registered"
            ) from exc

    def find(self, flow_id: str, version: str) -> FlowDefinition | None:
        """按 `(flow_id, version)` 取版本，不存在返回 None。

        给展示层用：任务可能指向库里已经没有的版本（历史数据、迁移、或运维删过），
        这时页面该少显示几句作者文案，而不是整条任务接口 500——那种任务照样要
        能查看、能取消。
        """

        return self._flows.get((flow_id, version))

    @staticmethod
    def _published(flow: FlowDefinition) -> FlowDefinition:
        if flow.status is not FlowStatus.PUBLISHED:
            raise FlowNotRunnable(f"flow {flow.flow_id!r} is not published")
        return flow


@dataclass(frozen=True, slots=True)
class FlowFieldError:
    """一条可定位到具体字段的校验错误。

    `loc` 用 JSON 路径风格（如 `("steps", 0, "tool")`），管理侧前端据此把错误
    标到对应的输入框，而不是只弹一句"保存失败"。
    """

    loc: tuple[str | int, ...]
    message: str


def flow_field_errors(flow: FlowDefinition) -> list[FlowFieldError]:
    """结构与权限的自洽校验，返回**全部**错误及其字段位置。

    纯函数：管理侧保存草稿时用它在界面上定位，存储层写入时仍由 `validate_flow`
    兜底。两处共用同一套规则，避免"前端能构造出后端不允许的流程"。
    """

    errors: list[FlowFieldError] = []
    seen: set[str] = set()
    for index, step in enumerate(flow.steps):
        if step.step_id in seen:
            errors.append(
                FlowFieldError(
                    loc=("steps", index, "step_id"),
                    message=(
                        f"duplicate step_id {step.step_id!r} in flow {flow.flow_id!r}"
                    ),
                )
            )
        seen.add(step.step_id)
        if step.kind is StepKind.EXECUTE:
            if not step.tool:
                errors.append(
                    FlowFieldError(
                        loc=("steps", index, "tool"),
                        message=f"execute step {step.step_id!r} must declare a tool",
                    )
                )
        else:
            if step.tool:
                errors.append(
                    FlowFieldError(
                        loc=("steps", index, "tool"),
                        message=(
                            f"step {step.step_id!r} may only declare a tool when executing"
                        ),
                    )
                )
            if step.requires_confirmation:
                errors.append(
                    FlowFieldError(
                        loc=("steps", index, "requires_confirmation"),
                        message=(
                            f"step {step.step_id!r} may only require confirmation when executing"
                        ),
                    )
                )
            # 正文只由执行步骤产出。别的步骤声明了标签也不会有人往里写值，
            # 于是"页面上应该有一行"变成永远空白——这正是最难查的一类失效，
            # 所以在保存时就拒掉，而不是等办结后才发现。
            if step.answer_label:
                errors.append(
                    FlowFieldError(
                        loc=("steps", index, "answer_label"),
                        message=(
                            f"step {step.step_id!r} may only declare answer_label when executing"
                        ),
                    )
                )
        errors.extend(_field_label_errors(flow, index, step))

    errors.extend(_intent_sample_errors(flow))
    errors.extend(_intent_rule_errors(flow))
    return errors


def validate_flow(flow: FlowDefinition) -> None:
    """结构与权限的自洽校验。持久化之前和装载之后都要过这一道。

    放在模块级而不是 `FlowRegistry` 的私有方法里：存储层写入一条版本时也必须
    校验，否则"库里有一条跑不起来的流程"会直到第一次执行才暴露。

    只抛第一条错误：存储层只需要"写不进去"这一个结论；字段级清单由
    `flow_field_errors` 提供给管理侧界面。消息文本与逐条规则一一对应，不额外包装。
    """

    errors = flow_field_errors(flow)
    if errors:
        raise ValueError(errors[0].message)


def _field_label_errors(
    flow: FlowDefinition, index: int, step: StepDefinition
) -> list[FlowFieldError]:
    """字段标签只能标注这一步骤声明过的字段，且不能是空串。

    标错字段的后果很隐蔽：页面照样渲染，只是那条标签永远不会出现（字段名本身被
    显示给群众），或者出现一个「有名字但没有输入框」的幽灵标签。两种都不会报错。
    """

    errors: list[FlowFieldError] = []
    for name, label in sorted(step.field_labels.items()):
        if name not in step.required_fields:
            errors.append(
                FlowFieldError(
                    loc=("steps", index, "field_labels", name),
                    message=(
                        f"step {step.step_id!r} labels {name!r}, "
                        "which is not one of its required_fields"
                    ),
                )
            )
        if not label.strip():
            errors.append(
                FlowFieldError(
                    loc=("steps", index, "field_labels", name),
                    message=f"step {step.step_id!r} has a blank label for {name!r}",
                )
            )
    return errors


def _intent_rule_errors(flow: FlowDefinition) -> list[FlowFieldError]:
    """意图规则必须指向**自己所属的那一版**，且标识唯一、关键词非空。

    规则指向别的版本是装配错误里最难查的一种：识别那一侧看起来完全正常（命中了、
    置信度够高），失败发生在后来的"取可执行流程"上，报出来的却像是群众说错了话。
    在这里拦住，错误就落在写流程的人面前（#73）。
    """

    rules = flow.intent_rules
    if not rules:
        return []

    errors: list[FlowFieldError] = []
    seen_ids: set[str] = set()
    for index, rule in enumerate(rules):
        if (rule.flow_id, rule.flow_version) != (flow.flow_id, flow.version):
            errors.append(
                FlowFieldError(
                    loc=("intent_rules", index, "flow_id"),
                    message=(
                        f"intent rule {rule.intent_id!r} targets "
                        f"{rule.flow_id!r}@{rule.flow_version} but is declared by "
                        f"{flow.flow_id!r}@{flow.version}"
                    ),
                )
            )
        if rule.intent_id in seen_ids:
            errors.append(
                FlowFieldError(
                    loc=("intent_rules", index, "intent_id"),
                    message=(
                        f"duplicate intent_id {rule.intent_id!r} in flow "
                        f"{flow.flow_id!r}@{flow.version}"
                    ),
                )
            )
        seen_ids.add(rule.intent_id)
        if not any(keyword.strip() for keyword in rule.keywords):
            # 没有关键词的规则永远不会命中，留着只会让"为什么这句话没被识别"更难查。
            errors.append(
                FlowFieldError(
                    loc=("intent_rules", index, "keywords"),
                    message=f"intent rule {rule.intent_id!r} must declare a keyword",
                )
            )
        errors.extend(_slot_pattern_errors(index, rule))
    return errors


def _slot_pattern_errors(index: int, rule: IntentRule) -> list[FlowFieldError]:
    """槽位正则必须能编译、且不能是空串。

    这两条都只在**抽值那一刻**才炸：`re.search()` 撞上写坏的正则抛 `re.error`，
    而它发生在群众提交任务的那次请求里——对外是一个 500，对群众是"点了没反应"。
    空串更隐蔽：`re.search("")` 恒中，会把整句话的前半段当成字段值写进执行参数。
    校验放在这里，错误就落在写规则的人面前（管理侧 422 带 loc）。
    """

    errors: list[FlowFieldError] = []
    for name, patterns in sorted(rule.slot_patterns.items()):
        for pattern in patterns:
            loc = ("intent_rules", index, "slot_patterns", name)
            if not pattern.strip():
                errors.append(
                    FlowFieldError(
                        loc=loc,
                        message=(
                            f"intent rule {rule.intent_id!r} has a blank pattern "
                            f"for field {name!r}"
                        ),
                    )
                )
                continue
            try:
                re.compile(pattern)
            except re.error as exc:
                errors.append(
                    FlowFieldError(
                        loc=loc,
                        message=(
                            f"intent rule {rule.intent_id!r} has an invalid pattern "
                            f"for field {name!r}: {exc}"
                        ),
                    )
                )
    return errors


def _intent_sample_errors(flow: FlowDefinition) -> list[FlowFieldError]:
    """意图样例自身的结构校验。条数要求不在这里：草稿允许先写流程、后补样例。

    报错只给 `sample_id` 对，不给原文——校验信息会进日志、管理侧界面与 CI 输出，
    样例文本按"群众可能这样说"的证据对待，不该顺手复制到第二处。
    """

    samples = flow.intent_samples
    if not samples:
        return []

    errors: list[FlowFieldError] = []
    seen_ids: set[str] = set()
    for index, sample in enumerate(samples):
        if sample.sample_id in seen_ids:
            errors.append(
                FlowFieldError(
                    loc=("intent_samples", index, "sample_id"),
                    message=(
                        f"duplicate intent sample_id {sample.sample_id!r} in flow "
                        f"{flow.flow_id!r}@{flow.version}"
                    ),
                )
            )
        seen_ids.add(sample.sample_id)

    # 局部导入：`domain/intent.py` 依赖应用层端口，而本模块是几乎所有模块的基础，
    # 不在模块级把这条依赖带进整条导入链。
    from workpanda.domain.intent import normalize_text

    first_seen: dict[str, str] = {}
    for index, sample in enumerate(samples):
        key = normalize_text(sample.text)
        if key in first_seen:
            errors.append(
                FlowFieldError(
                    loc=("intent_samples", index, "text"),
                    message=(
                        f"duplicate intent sample text {first_seen[key]}/{sample.sample_id} "
                        f"in flow {flow.flow_id!r}@{flow.version}"
                    ),
                )
            )
            continue
        first_seen[key] = sample.sample_id
    return errors
