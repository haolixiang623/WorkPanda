"""IM 网关应用服务（Issue #64 / 提案 N4）。

只做四件事，按顺序：**验签 → 幂等去重 → 身份解析 → 归一化后的固定命令**。
四步的顺序不是风格问题：

1. **验签在最前面**，且由 adapter 完成。未验签的请求连"这条消息处理过没有"都
   不该被写进去——否则任何人都能拿任意 message id 把去重表写满；
2. **去重排在身份解析之前**，因为重投是 IM 平台的正常行为，不该每次重投都去
   读一遍工作人员目录并写一条访问留痕；
3. **身份解析失败不回话**。回一句"你不是工作人员"等于告诉外部联系人这里有个
   能干活的机器人，而外部联系人名单不受本系统控制（D4）；
4. **命令是固定的**，只认"查状态"。不做自然语言理解——识别模型在这里没有
   落点：一旦它能"理解"，它就能被诱导去执行别的动作。

出站方向只有一件：把任务事件变成一张卡片发出去，**失败只留痕**。送达不等于
已读，更不等于已办，所以这里既不写任务状态，也不产生任何"回执"。

结构上的保证比约定可靠：本模块注入的是 `TaskReader`（只有 `get`），不是
`TaskService`，也不是 `TaskStore`。它没有 `commit`、`answer`、`confirm`、`cancel`
可调用，因此"网关不会改任务状态"是**没有入口**，而不是"记得别调"。
`tests/test_im_gateway.py::test_gateway_has_no_task_state_write_surface` 钉住这一点。
"""

from __future__ import annotations

import logging
from typing import Protocol
from uuid import UUID

from workpanda.domain.im_gateway import (
    NOTIFY_STATUSES,
    STAFF_TASK_PAGE_PATH,
    STATUS_LABELS,
    WECOM_CHANNEL,
    CardDelivery,
    CardOutcome,
    CardSender,
    ImGatewayStore,
    InboundMessage,
    InboundOutcome,
    InboundResult,
    TaskCard,
)
from workpanda.domain.models import Task
from workpanda.domain.repository import TaskNotFoundError
from workpanda.domain.staff import ImIdentityRejected, StaffStore
from workpanda.harness.redaction import redact_text

logger = logging.getLogger(__name__)

# 固定命令别名。只允许"查看"，不允许"办理"：IM 里没有能推进任务的入口。
_COMMAND_ALIASES = frozenset({"状态", "查", "查询", "status", "query"})


class TaskReader(Protocol):
    """只读的任务视图。**故意只有 `get`**：网关不需要、也不该有写能力。"""

    def get(self, task_id: UUID) -> Task: ...


class ReadOnlyTasks:
    """把任意任务存储收窄成只读视图。

    即使装配时误传了完整的 `TaskStore`，注入到网关里的这个对象也只暴露 `get`：
    `hasattr(reader, "commit")` 为 False。这是"没有入口"的具体形状。
    """

    def __init__(self, store: TaskReader) -> None:
        self._store = store

    def get(self, task_id: UUID) -> Task:
        return self._store.get(task_id)


class InboundAdapter(Protocol):
    """入站协议适配器（渠道差异在 infrastructure 里抹平）。"""

    def verify_url(self, params: dict[str, str]) -> str: ...

    def read(self, params: dict[str, str], body: bytes) -> InboundMessage: ...


def parse_task_command(text: str) -> UUID | None:
    """解析 `状态 <task_id>`（或 `查 <task_id>` / `status <task_id>`）。

    只在**整条消息就是这条命令**时成立：多余的字一律当认不出，不去猜
    "他大概想看哪张任务"。猜错的代价是把 A 的状态回给 B。
    """

    normalized = (text or "").replace("：", " ").replace(":", " ")
    parts = [part for part in normalized.split() if part]
    if len(parts) != 2:
        return None
    if parts[0].lower() not in _COMMAND_ALIASES:
        return None
    try:
        return UUID(parts[1].strip("<>《》.。"))
    except ValueError:
        return None


class ImGatewayService:
    def __init__(
        self,
        *,
        adapter: InboundAdapter,
        resolver,
        staff_store: StaffStore,
        tasks: TaskReader,
        store: ImGatewayStore,
        sender: CardSender | None = None,
        console_base_url: str = "",
    ) -> None:
        self.adapter = adapter
        self.resolver = resolver
        self.staff_store = staff_store
        self.tasks = tasks
        self.store = store
        self.sender = sender
        self.console_base_url = console_base_url.rstrip("/")

    # ---------- 入站 ----------

    def verify_url(self, params: dict[str, str]) -> str:
        """URL 验证。无副作用：不写去重表、不解析身份。"""

        return self.adapter.verify_url(params)

    def handle(self, params: dict[str, str], body: bytes) -> InboundResult:
        """处理一次回调。

        验签/解密失败会**原样抛出** `ImSignatureRejected` / `ImPayloadRejected`，
        由 API 层映射成 403/400。不把它们变成返回值，是为了让"忘记处理"表现为
        500 而不是静默放行。
        """

        message = self.adapter.read(params, body)
        if not self.store.claim_message(message.channel, message.message_id):
            # IM 重投：同一条消息只处理一次。**不重复回卡片**——重投是平台行为，
            # 不是用户又发了一遍，回两遍会让工作人员以为有两件事。
            return _result(message, InboundOutcome.DUPLICATE, detail="message already processed")

        try:
            principal = self.resolver.resolve(message.channel, message.im_user_id)
        except ImIdentityRejected as exc:
            # 未映射/停用：拒绝且不回话。留痕已经在解析器里写过（带 IM 标识）。
            return _result(
                message, InboundOutcome.IDENTITY_REJECTED, detail=exc.reason.value
            )

        member = self.staff_store.get_member(principal.subject)
        if member is None or not member.enabled:
            return _result(
                message, InboundOutcome.IDENTITY_REJECTED, detail="staff_not_provisioned"
            )

        task_id = parse_task_command(message.text)
        if task_id is None:
            # 认不出的指令：不猜、不回。回一句"没听懂"会把这个入口变成可探测的。
            return _result(message, InboundOutcome.IGNORED, subject=member.subject)

        task = self._read_task(task_id)
        if task is None or not member.actions_on(task.flow_id):
            # 任务不存在与"没有授权"对外是同一个结果：区分两者等于提供一个
            # "这个任务号存在吗"的探测接口。
            return _result(
                message,
                InboundOutcome.FORBIDDEN,
                subject=member.subject,
                task_id=task_id,
                detail="task_not_found_or_not_granted",
            )

        card = self._card(task)
        sender = self.sender if self._outbound_ready() else None
        if sender is None:
            self._record(task, card, message.im_user_id, CardOutcome.SKIPPED, "outbound_disabled")
            return _result(
                message,
                InboundOutcome.REPLY_FAILED,
                subject=member.subject,
                task_id=task_id,
                detail="outbound_disabled",
            )

        try:
            sender.send(message.im_user_id, card)
        except Exception as exc:  # noqa: BLE001 - 发送失败的原因千奇百怪，都不该冒泡
            # 发不出去不改任何状态：工作人员可以再问一次，任务照常推进。
            self._record(task, card, message.im_user_id, CardOutcome.FAILED, _failure(exc))
            return _result(
                message,
                InboundOutcome.REPLY_FAILED,
                subject=member.subject,
                task_id=task_id,
                detail=_failure(exc),
            )

        self._record(task, card, message.im_user_id, CardOutcome.SENT, "")
        return _result(message, InboundOutcome.REPLIED, subject=member.subject, task_id=task_id)

    # ---------- 出站 ----------

    def notify_task(self, task_id: UUID) -> None:
        """任务事件触发的发卡。**绝不抛错**——它跑在流程之外的后台线程里，
        但即使将来被内联调用，也不能让一次发送失败改变任务推进。"""

        try:
            self._notify_task(task_id)
        except Exception:  # noqa: BLE001 - fail-open：通知是旁路，不能反噬主流程
            logger.warning("im notifier failed for task %s", task_id, exc_info=True)

    def _notify_task(self, task_id: UUID) -> None:
        task = self._read_task(task_id)
        if task is None:
            return
        if task.status not in NOTIFY_STATUSES:
            # 过程状态不进 IM：逐条推送会让工作人员把这个号静音，真正要看的时候
            # 反而看不到。
            return
        step_id = task.current_step or ""
        if not self.store.claim_notification(task.task_id, task.status.value, step_id):
            return
        card = self._card(task)
        recipients = self._recipients(task.flow_id)
        if not recipients:
            self._record(task, card, "", CardOutcome.SKIPPED, "no_recipient")
            return
        sender = self.sender if self._outbound_ready() else None
        if sender is None:
            self._record(task, card, "", CardOutcome.SKIPPED, "outbound_disabled")
            return
        for recipient in recipients:
            try:
                sender.send(recipient, card)
            except Exception as exc:  # noqa: BLE001
                self._record(task, card, recipient, CardOutcome.FAILED, _failure(exc))
                continue
            self._record(task, card, recipient, CardOutcome.SENT, "")

    # ---------- 内部 ----------

    def _read_task(self, task_id: UUID) -> Task | None:
        try:
            return self.tasks.get(task_id)
        except TaskNotFoundError:
            return None

    def _recipients(self, flow_id: str) -> list[str]:
        """该流程上"有任何动作授权"的、已开通且已映射的工作人员。

        取全部动作而不是只取 `handle`：受理与复核的人同样需要知道这笔业务走到
        哪一步了。没授权的人一个都收不到——IM 名单本身不是权限来源。
        """

        recipients: set[str] = set()
        for identity in self.staff_store.list_identities(WECOM_CHANNEL):
            if not identity.enabled:
                continue
            member = self.staff_store.get_member(identity.principal_subject)
            if member is None or not member.enabled:
                continue
            if not member.actions_on(flow_id):
                continue
            recipients.add(identity.im_user_id)
        return sorted(recipients)

    def _outbound_ready(self) -> bool:
        return self.sender is not None and bool(self.console_base_url)

    def _card(self, task: Task) -> TaskCard:
        """卡片内容。**只从任务元数据取字段**，不读 `slots` / `result` / `user_ref`
        ——那三样就是"服务对象数据正文"。标题与描述再过一遍既有脱敏规则，
        防止将来有人往里加一个业务标签时把手机号带出去。
        """

        label = STATUS_LABELS.get(task.status.value, task.status.value)
        return TaskCard(
            task_id=task.task_id,
            status=task.status.value,
            title=redact_text(f"任务状态：{label}"),
            description=redact_text(f"任务号：{task.task_id}"),
            url=f"{self.console_base_url}{STAFF_TASK_PAGE_PATH}/{task.task_id}",
        )

    def _record(
        self,
        task: Task,
        card: TaskCard,
        recipient: str,
        outcome: CardOutcome,
        reason: str,
    ) -> None:
        try:
            self.store.record_delivery(
                CardDelivery(
                    task_id=card.task_id,
                    status=card.status,
                    recipient=recipient,
                    outcome=outcome,
                    reason=reason,
                )
            )
        except Exception:  # noqa: BLE001 - 留痕失败不该把"已发送"变成"发送失败"
            logger.warning("failed to record im delivery for task %s", task.task_id)


def _result(
    message: InboundMessage,
    outcome: InboundOutcome,
    *,
    subject: str = "",
    task_id: UUID | None = None,
    detail: str = "",
) -> InboundResult:
    return InboundResult(
        outcome=outcome,
        message_id=message.message_id,
        channel=message.channel,
        im_user_id=message.im_user_id,
        subject=subject,
        task_id=task_id,
        detail=detail,
    )


def _failure(exc: Exception) -> str:
    """只留错误类型与必要的数值，不留响应正文：对方报文可能回显请求内容。"""

    return type(exc).__name__


__all__ = [
    "ImGatewayService",
    "InboundAdapter",
    "ReadOnlyTasks",
    "TaskReader",
    "parse_task_command",
]
