"""IM 网关（N4）的领域口径：入站消息、幂等去重、出站卡片与投递留痕。

网关是**通道**，不是执行者，也不是权限来源（D2）。这个模块只描述"通道能看见
什么、能送出什么"，刻意不出现任何任务状态写入口：

- 入站只有一条固定命令（查状态），其余文本一律不猜、不回；
- 出站卡片只带**状态与一个深链**，不带服务对象数据正文（槽位、结果、会话主体
  都不在卡片模型里）；
- 送达失败只留痕，不改变任何任务状态——**送达 ≠ 已读 ≠ 已办**。

幂等靠两条主键：`(channel, message_id)` 保证 IM 重投只处理一次；
`(task_id, status, step_id)` 保证同一状态不重复发卡（同一步重复进入待确认除外）。
两条都是"第一次写入成功才算我处理"，交给存储层原子判定，不靠进程内缓存。
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Protocol
from uuid import UUID

from pydantic import BaseModel, Field

from workpanda.domain.models import TaskStatus, utc_now

# 第一个渠道固定为企业微信（D4 问题 2 已决）。常量而不是枚举：接第二个渠道时
# 只是多一个值，不需要改这张表的结构。
WECOM_CHANNEL = "wecom"

# 值得发卡的状态。刻意不含 RUNNING / CREATED / WAITING_FOR_USER：
# 过程状态变化频繁，逐条推送等于把 IM 变成日志窗口，工作人员会把它静音——
# 真需要看的时候反而看不到。
NOTIFY_STATUSES: frozenset[TaskStatus] = frozenset(
    {
        TaskStatus.WAITING_FOR_CONFIRMATION,
        TaskStatus.HANDOFF_REQUIRED,
        TaskStatus.OUTCOME_UNCONFIRMED,
        TaskStatus.COMPLETED,
        TaskStatus.FAILED,
    }
)

STATUS_LABELS: dict[str, str] = {
    TaskStatus.WAITING_FOR_CONFIRMATION.value: "待人确认",
    TaskStatus.HANDOFF_REQUIRED.value: "需转人工",
    TaskStatus.OUTCOME_UNCONFIRMED.value: "结果待确认",
    TaskStatus.COMPLETED.value: "已办结",
    TaskStatus.FAILED.value: "已失败",
}

# 卡片按钮的落地页面。与深链落地路径同样由常量给出，改一处不会让卡片与前端分叉。
STAFF_TASK_PAGE_PATH = "/tasks"


class ImSignatureRejected(Exception):
    """验签不通过。**任何未验签请求都不得进入业务层**，所以它是个异常而不是返回值：
    调用方漏处理会直接 500，而不是静默地"当作没收到"。"""


class ImPayloadRejected(Exception):
    """请求体不是可解析的企业微信回调（过大、非 XML、缺字段、解密后主体不符）。"""


class InboundOutcome(StrEnum):
    """一次入站处理的结局。每一种都对应一个不同的后续动作，不做合并。"""

    # 已经回过一张卡片
    REPLIED = "replied"
    # 认不出的指令：不猜、不回话（回话等于把网关变成一个可探测的入口）
    IGNORED = "ignored"
    # 同一条 message id 第二次到达：IM 重投，什么都不做
    DUPLICATE = "duplicate"
    # 验签失败
    SIGNATURE_REJECTED = "signature_rejected"
    # 请求体不可解析
    MALFORMED = "malformed"
    # 不是已映射的工作人员（未映射 / 停用 / 主体不存在）：拒绝且不回话
    IDENTITY_REJECTED = "identity_rejected"
    # 是工作人员，但这条流程上没有任何授权；任务不存在也归到这里
    FORBIDDEN = "forbidden"
    # 该回卡片，但出站通道没配置或发送失败
    REPLY_FAILED = "reply_failed"


class InboundMessage(BaseModel):
    """归一化之后的入站消息。渠道差异在 adapter 里就被抹平，业务层只看见这个。"""

    channel: str = Field(min_length=1, max_length=32)
    message_id: str = Field(min_length=1, max_length=256)
    im_user_id: str = Field(min_length=1, max_length=128)
    msg_type: str = Field(default="text", max_length=32)
    text: str = Field(default="", max_length=4000)
    # IM 侧给的发送时间（若提供）。只用于排障，不参与任何判定：
    # 服务端时钟与 IM 平台时钟不可比，拿它判"是否过期"会误伤正常重投。
    sent_at: datetime | None = None
    received_at: datetime = Field(default_factory=utc_now)


class InboundResult(BaseModel):
    """处理结果。**不回给 IM 平台**（对方只认 200/空 body），只用于留痕与自测。"""

    outcome: InboundOutcome
    message_id: str = ""
    channel: str = ""
    im_user_id: str = ""
    subject: str = ""
    task_id: UUID | None = None
    detail: str = ""


class TaskCard(BaseModel):
    """发给工作人员的卡片。

    字段表就是隐私边界：只有任务号、状态标签与深链。**没有**槽位、结果、
    会话主体，也没有失败原因原文（`failure_message` 可能含外部系统报文）。
    想加字段时先回答"这个字段会不会把服务对象数据带到 IM 里"。
    """

    task_id: UUID
    status: str = Field(min_length=1, max_length=32)
    title: str = Field(min_length=1, max_length=128)
    description: str = Field(default="", max_length=512)
    url: str = Field(min_length=1, max_length=1024)

    def text(self) -> str:
        """卡片全部可读内容的拼接。负向回归用它断言"没有多余内容"。
        与发送实现无关：换渠道时这条断言仍然成立。"""

        return f"{self.title}\n{self.description}\n{self.url}"


class CardOutcome(StrEnum):
    SENT = "sent"
    FAILED = "failed"
    # 出站通道没配置、或没有收件人。与 FAILED 分开：前者不是故障，是没启用。
    SKIPPED = "skipped"


class CardDelivery(BaseModel):
    """一次投递的留痕。回答"这张卡发出去没有、发给了谁、为什么没发"。"""

    at: datetime = Field(default_factory=utc_now)
    task_id: UUID | None = None
    status: str = ""
    recipient: str = ""
    outcome: CardOutcome
    reason: str = ""
    # 失败摘要。只放错误类型与状态码，不放响应正文：对方报文可能回显请求内容。
    detail: str = Field(default="", max_length=200)


class ImGatewayStore(Protocol):
    """入站去重、发卡去重与投递留痕。

    三件事同库，理由与 `StaffStore` 相同：一次"认领消息 + 解析身份 + 回卡片"
    如果跨三个后端，排障时三边对不上的现场无法还原。
    """

    def claim_message(self, channel: str, message_id: str) -> bool:
        """认领一条入站消息；**返回 True 表示这是第一次见到它**。

        实现必须原子：并发的两个重投请求里只能有一个拿到 True。
        """

        ...

    def has_message(self, channel: str, message_id: str) -> bool:
        """这条消息是否被认领过（只读，用于排障与回归断言）。"""

        ...

    def claim_notification(self, task_id: UUID, status: str, step_id: str) -> bool:
        """认领一次任务发卡；同一 (任务, 状态, 步骤) 只发一次。"""

        ...

    def record_delivery(self, delivery: CardDelivery) -> None: ...

    def list_deliveries(
        self, *, task_id: UUID | None = None, limit: int = 50
    ) -> list[CardDelivery]: ...


class CardSender(Protocol):
    """出站发送端口。实现失败必须抛错——"发失败了"不能表现成"发成功了"。"""

    def send(self, im_user_id: str, card: TaskCard) -> None: ...
