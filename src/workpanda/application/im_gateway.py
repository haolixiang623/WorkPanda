"""IM 网关的应用层：幂等入站处理与任务事件 → 卡片。

这一层刻意只做三件事，且**没有任何能写任务状态的东西**：

1. `ImInboundService.handle`：判重 → 身份解析 → 留痕。它不 import `TaskService`，
   也不持有 `TaskStore`；构造参数里没有可写业务状态的依赖，因此"网关顺手改一下
   任务"在代码层面做不到（`docs/IM-GATEWAY.md` 第 2 节，回归里有一条源码审计）。
2. `TaskEventNotifier.handle`：任务事件 → 卡片 → 发给有授权的工作人员。
   它是**读**任务与事件、**发**消息，不推进流程。
3. 失败一律"返回结果 + 记录"，不向调用方抛业务异常：通知失败不能把任务推进
   带下水（fail-open on notification）。

"送达 ≠ 已读 ≠ 已办"：这里只回答"卡片发出去了没有"，不产生任何 IM 侧状态。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Protocol, runtime_checkable
from uuid import UUID

from workpanda.application.staff_access import ImIdentityResolver
from workpanda.domain.flow import FlowNotFound, FlowRegistry
from workpanda.domain.im_gateway import (
    ImInboundMessage,
    ImInboundOutcome,
    ImInboundRecord,
    NotificationCounters,
    TaskCard,
    build_task_card,
    card_event_type,
)
from workpanda.domain.models import Task
from workpanda.domain.repository import TaskNotFoundError, TaskStore
from workpanda.domain.staff import ImIdentityRejected, StaffStore

logger = logging.getLogger(__name__)


@runtime_checkable
class ImMessageStore(Protocol):
    """入站消息的幂等登记。`claim` 必须原子：并发的两条同 id 消息只能有一条赢。"""

    def claim(self, record: ImInboundRecord) -> bool:
        """登记一条消息；已存在返回 False（调用方据此判定重投）。"""
        ...

    def complete(self, record: ImInboundRecord) -> None:
        """回填处理结论。已存在的记录才更新，不新建。"""
        ...

    def get(self, channel: str, message_id: str) -> ImInboundRecord | None: ...

    def count(self) -> int: ...


@runtime_checkable
class CardSender(Protocol):
    """发一张卡片。失败必须抛错，由调用方决定怎么兜（这里不吞异常）。"""

    def send(self, im_user_id: str, card: TaskCard) -> None: ...


class ImInboundService:
    """入站 webhook 的业务部分：判重、身份解析、留痕。

    明确不做的事：不创建任务、不回答问题、不确认、不取消。IM 里不做业务操作
    （决策 D3），所以这条链路的终点是"这条消息属于谁"。
    """

    def __init__(
        self,
        *,
        resolver: ImIdentityResolver,
        messages: ImMessageStore,
        channel: str = "wecom",
    ) -> None:
        self._resolver = resolver
        self._messages = messages
        self._channel = channel

    @property
    def channel(self) -> str:
        return self._channel

    def handle(self, message: ImInboundMessage) -> ImInboundRecord:
        """处理一条入站消息。**永不抛出"业务失败"**：webhook 需要稳定应答。

        顺序是刻意的：先判重，再解析身份。反过来的话，重投的未映射消息会重复写
        拒绝记录，把真实拒绝淹在噪声里。
        """

        record = ImInboundRecord(
            channel=message.channel,
            message_id=message.message_id,
            im_user_id=message.im_user_id,
            kind=message.kind,
        )
        if not self._messages.claim(record):
            existing = self._messages.get(message.channel, message.message_id)
            duplicate = record.model_copy(update={"outcome": ImInboundOutcome.DUPLICATE})
            if existing is not None:
                # 保留首次处理的主体与结论，重投不应改写历史。
                duplicate = duplicate.model_copy(
                    update={"subject": existing.subject, "reason": existing.reason}
                )
            return duplicate

        try:
            principal = self._resolver.resolve(message.channel, message.im_user_id)
        except ImIdentityRejected as rejected:
            record = record.model_copy(
                update={"outcome": ImInboundOutcome.REJECTED, "reason": rejected.reason.value}
            )
            self._messages.complete(record)
            return record
        except Exception:
            # 存储故障等"我们自己的问题"：如实留痕为拒绝，但不把异常抛回渠道，
            # 否则 IM 平台会持续重投，把一次存储抖动放大成重投风暴。
            logger.warning("im inbound identity resolution failed", exc_info=False)
            failed = record.model_copy(
                update={"outcome": ImInboundOutcome.REJECTED, "reason": "im_resolution_failed"}
            )
            self._messages.complete(failed)
            return failed

        record = record.model_copy(
            update={"outcome": ImInboundOutcome.ACCEPTED, "subject": principal.subject}
        )
        self._messages.complete(record)
        return record

    def get(self, channel: str, message_id: str) -> ImInboundRecord | None:
        return self._messages.get(channel, message_id)


@dataclass(frozen=True)
class CardDelivery:
    """一次卡片投递的结果。`error` 只留异常类型名。

    异常消息里可能带 URL（含 webhook key）、内网地址或对方返回体，落进日志就等于
    二次泄漏；排查时知道"哪一类错"足够定位是配置、网络还是对方拒绝。
    """

    im_user_id: str
    subject: str
    ok: bool
    error: str = ""


class TaskEventNotifier:
    """任务事件 → 卡片。只读任务与事件，不推进流程、不改状态。"""

    def __init__(
        self,
        *,
        store: TaskStore,
        staff: StaffStore,
        sender: CardSender,
        flows: FlowRegistry | None = None,
        channel: str = "wecom",
        deep_link_base: str = "",
        counters: NotificationCounters | None = None,
    ) -> None:
        self._store = store
        self._staff = staff
        self._sender = sender
        self._flows = flows
        self._channel = channel
        self._deep_link_base = (deep_link_base or "").strip()
        # 与 `NotificationQueue` 共用同一个计数器对象：队列记丢件，这里记投递结果，
        # 值班看的是一个快照。
        self._counters = counters if counters is not None else NotificationCounters()

    @property
    def counters(self) -> NotificationCounters:
        return self._counters

    @property
    def enabled(self) -> bool:
        """没有深链基址就等于没法发卡片。

        不做"相对链接凑合用"：IM 客户端不会把相对链接补成对的地址，发出去就是一条
        点不开的卡片，而发信方以为通知成功了。
        """

        return bool(self._deep_link_base)

    def handle(self, task_id: UUID, sequence: int) -> list[CardDelivery]:
        """处理一条刚落地的事件。返回每个收件人的投递结果（可能为空）。"""

        if not self.enabled:
            return []
        event = self._event_at(task_id, sequence)
        if event is None or not card_event_type(event.type):
            return []
        try:
            task = self._store.get(task_id)
        except TaskNotFoundError:
            return []

        card = build_task_card(
            task_id=task.task_id,
            flow_id=task.flow_id,
            flow_title=self._flow_title(task),
            # 用事件自带的状态快照，而不是"现在库里的状态"：一条迟到的通知
            # 若报成最新状态，就会把"待确认"说成"已完成"。
            status=event.status,
            deep_link_base=self._deep_link_base,
        )
        recipients = self._recipients(task.flow_id)
        return [self._deliver(im_user_id, subject, card) for im_user_id, subject in recipients]

    # ---------- 内部 ----------

    def _event_at(self, task_id: UUID, sequence: int):
        """取指定序号的事件。按序号翻页取一条，避免把整条事件流读进内存。"""

        events = self._store.events(task_id, max(0, sequence - 1))
        for event in events:
            if event.sequence == sequence:
                return event
        return None

    def _flow_title(self, task: Task) -> str:
        if self._flows is None:
            return task.flow_id
        try:
            return self._flows.require(task.flow_id, task.flow_version).title
        except FlowNotFound:
            # 版本已下架但历史任务还在：用 flow_id 兜底，不因为拿不到标题就不通知。
            return task.flow_id

    def _recipients(self, flow_id: str) -> list[tuple[str, str]]:
        """该流程上有授权、且配了 IM 映射的工作人员。

        只发给"对这条流程有任一动作授权"的人：没有授权的人收到卡片也多办不了事，
        却多知道了一条业务在发生。
        """

        recipients: list[tuple[str, str]] = []
        seen: set[str] = set()
        for identity in self._staff.list_identities(self._channel):
            if not identity.enabled or identity.principal_subject in seen:
                continue
            member = self._staff.get_member(identity.principal_subject)
            if member is None or not member.enabled:
                continue
            if not member.actions_on(flow_id):
                continue
            seen.add(member.subject)
            recipients.append((identity.im_user_id, member.subject))
        return recipients

    def _deliver(self, im_user_id: str, subject: str, card: TaskCard) -> CardDelivery:
        try:
            self._sender.send(im_user_id, card)
        except Exception as exc:  # noqa: BLE001 - 投递失败的类型不属于应用层协议
            logger.warning(
                "im card delivery failed: channel=%s subject=%s error=%s",
                self._channel,
                subject,
                type(exc).__name__,
            )
            self._counters.incr("failed")
            return CardDelivery(im_user_id, subject, ok=False, error=type(exc).__name__)
        self._counters.incr("delivered")
        return CardDelivery(im_user_id, subject, ok=True)
