"""IM 网关的领域契约：验签、入站消息归一化、卡片内容边界。

三条不可越过的边界（`docs/IM-GATEWAY.md` 逐条解释了代价）：

1. **验签在业务之前**。`verify_callback_signature` 只做一件事：给出"这条请求是不是
   真的来自渠道"。没通过验签的请求体**不允许被解析**——先解析再验签，等于把所有
   XML 解析器、正则、日志都暴露在匿名输入下。
2. **入站按 message id 幂等**。IM 平台会重投（网络抖动、我们的超时），重投必须只
   处理一次；判重必须在身份解析与任何后续动作之前。
3. **卡片只带状态与一个深链**。`build_task_card` 的入参是标量（task_id / flow_id /
   flow_title / status），**根本不接收 `Task`**——这样"顺手把槽位塞进卡片"不是靠
   自觉避免，而是没有那条数据通路。

本模块不 import 任何应用服务或存储：它只描述契约，判断留给上层。
"""

from __future__ import annotations

import hashlib
import hmac
import time
from collections import deque
from enum import StrEnum
from threading import Lock
from urllib.parse import urlsplit
from uuid import UUID

from pydantic import BaseModel, Field

from workpanda.domain.models import EventType, TaskStatus, utc_now
from workpanda.domain.presentation import copy_for_status

# 验签时允许的时间偏差。IM 平台发的是"此刻"，超出窗口的请求一律拒绝：
# 明文模式下签名不含消息体，时间窗是抗重放的第一道闸。
DEFAULT_CALLBACK_MAX_SKEW_SECONDS = 300


class ImSignatureInvalid(Exception):
    """回调验签失败。**调用方不得继续解析请求体，也不得记录其内容。**"""


def plaintext_signature(token: str, *parts: str) -> str:
    """企业微信「明文模式」的签名算法：把 token 与请求参数一起排序后取 SHA1。

    参数顺序不固定（`token/timestamp/nonce/echostr` 谁先谁后取决于实现），所以先
    排序再拼接——这既是企业微信的做法，也避免了"换个参数顺序就验不过"的偶发故障。
    """

    joined = "".join(sorted([token, *parts]))
    return hashlib.sha1(joined.encode("utf-8")).hexdigest()


def verify_callback_signature(
    *,
    token: str,
    signature: str | None,
    timestamp: str | None,
    nonce: str | None,
    echo: str | None = None,
    now: float | None = None,
    max_skew_seconds: int = DEFAULT_CALLBACK_MAX_SKEW_SECONDS,
) -> None:
    """校验回调签名。不通过抛 `ImSignatureInvalid`，**不返回布尔值**。

    返回布尔值会被写成 `if not verify(...): pass`——那种写法在代码审查里看起来
    像"处理了"，实际上是放行。抛错则必须显式捕获，错误处理不会静默漏掉。

    `echo` 只在 URL 验证（GET）时有值：企业微信把 `echostr` 也算进签名。
    """

    if not token:
        # 没配 token 时绝不能"跳过验签"：那等于给所有未配置的部署开一个匿名入口。
        raise ImSignatureInvalid("callback token is not configured")
    if not signature or not timestamp or not nonce:
        raise ImSignatureInvalid("missing signature parameters")
    if echo is not None and not echo:
        raise ImSignatureInvalid("empty echostr")

    try:
        issued_at = int(timestamp)
    except ValueError as exc:
        raise ImSignatureInvalid("timestamp is not an integer") from exc

    current = time.time() if now is None else now
    if abs(current - issued_at) > max_skew_seconds:
        # 时间窗超限：可能是重放，也可能是对端时钟坏了。两种都拒绝并如实记录，
        # 因为"放宽窗口"和"放过一条重放"是同一件事。
        raise ImSignatureInvalid("timestamp outside the replay window")

    expected = plaintext_signature(token, timestamp, nonce, *([echo] if echo else []))
    if not hmac.compare_digest(expected, signature.strip().lower()):
        raise ImSignatureInvalid("signature mismatch")


class ReplayGuard:
    """同一 `nonce` 只接受一次的进程内记忆。

    企业微信每次推送都用新的 nonce，所以"拒绝重复 nonce"对正常流量是无感的；它挡住的
    是明文模式下的体替换——签名不覆盖消息体，攻击者若截获一对 (signature, nonce)，
    可以换掉 body 重发。有了这道闸，同一 nonce 的第二次使用直接失败。

    只记 `capacity` 条：这是防重放的辅助闸，不是审计日志，满了丢最旧的即可。
    """

    def __init__(self, capacity: int = 2048) -> None:
        self._capacity = max(1, capacity)
        self._seen: deque[str] = deque()
        self._index: set[str] = set()
        self._lock = Lock()

    def accept(self, nonce: str) -> bool:
        key = nonce.strip()
        if not key:
            return False
        with self._lock:
            if key in self._index:
                return False
            self._seen.append(key)
            self._index.add(key)
            while len(self._seen) > self._capacity:
                self._index.discard(self._seen.popleft())
            return True


class ImMessageKind(StrEnum):
    """归一化后的消息类型。只区分我们真的会处理的那几类。"""

    TEXT = "text"
    # 事件（进入会话、菜单点击等）：本轮只需归一化，不做业务动作。
    EVENT = "event"
    UNSUPPORTED = "unsupported"


class ImInboundMessage(BaseModel):
    """归一化后的入站消息。

    刻意**不带原始报文**：网关不该为"以后可能有用"留下一份对方的完整推送，
    那里面可能含群成员列表、外部联系人标识等与本次处理无关的数据。
    `text` 只在处理期间存在，落痕时只写类型与 message id（见 `ImInboundRecord`）。
    """

    channel: str = Field(min_length=1, max_length=32)
    message_id: str = Field(min_length=1, max_length=128)
    im_user_id: str = Field(min_length=1, max_length=128)
    kind: ImMessageKind = ImMessageKind.TEXT
    text: str = Field(default="", max_length=4000)
    msg_type: str = Field(default="", max_length=32)
    received_at: float | None = None


class ImInboundOutcome(StrEnum):
    ACCEPTED = "accepted"
    DUPLICATE = "duplicate"
    REJECTED = "rejected"


class ImInboundRecord(BaseModel):
    """一次入站处理的留痕。**不含消息正文**，只回答"哪条消息、谁、结论"。"""

    channel: str = Field(min_length=1, max_length=32)
    message_id: str = Field(min_length=1, max_length=128)
    im_user_id: str = Field(min_length=1, max_length=128)
    kind: ImMessageKind = ImMessageKind.TEXT
    outcome: ImInboundOutcome = ImInboundOutcome.ACCEPTED
    subject: str = ""
    # 拒绝原因沿用 `ImRejectionReason` 的取值，便于和身份留痕放在一起排查。
    reason: str = ""
    at: float = Field(default_factory=time.time)


# 会触发卡片的任务事件。挑的是"人需要知道、但不必盯屏幕"的那几个：
# 待确认、转人工、结果待确认、完成、失败、取消。过程事件（步骤开始/完成）
# 一律不推——那是把内部节奏外化成骚扰。
CARD_EVENTS: frozenset[EventType] = frozenset(
    {
        EventType.CONFIRMATION_REQUIRED,
        EventType.HANDOFF_REQUIRED,
        EventType.OUTCOME_UNCONFIRMED,
        EventType.COMPLETED,
        EventType.FAILED,
        EventType.CANCELLED,
    }
)


class NotificationCounters:
    """投递计数。**跨层共享同一个对象**：队列记"丢了几个事件"，通知器记"发出去几张卡"。

    为什么放在 domain 而不是 infrastructure：这两处计数会出现在同一个运维面板上，
    拆成两个互不认识的类型之后，"丢了几张"和"发了几张"就永远对不上账，而"通知体系
    正常"这个结论恰恰要靠这两个数字一起看。
    """

    def __init__(self) -> None:
        self._lock = Lock()
        self.delivered = 0
        self.failed = 0
        self.dropped = 0
        self.handler_errors = 0

    def incr(self, field: str, amount: int = 1) -> None:
        with self._lock:
            setattr(self, field, getattr(self, field) + amount)

    def snapshot(self) -> dict[str, int]:
        with self._lock:
            return {
                "delivered": self.delivered,
                "failed": self.failed,
                "dropped": self.dropped,
                "handler_errors": self.handler_errors,
            }


class TaskCard(BaseModel):
    """对外发送的卡片内容。字段是**闭集**，加字段要改这里的构造入口。

    `title` / `description` / `url` 之外什么都不要：服务对象的姓名、证件号、
    联系方式、办理事项正文一律不进卡片。深链回到 Web 之后，看得到什么由 Web 侧
    的既有鉴权决定——卡片不承担展示业务数据的职责。
    """

    task_id: UUID
    flow_id: str
    title: str
    description: str
    url: str

    def render(self) -> str:
        """把卡片摊平成一段文本。用于回归断言"原文没有泄漏"，也用于纯文本渠道。"""

        return f"{self.title}\n{self.description}\n{self.url}"


def _validated_base(deep_link_base: str) -> str:
    """深链基址必须是绝对 http(s) 地址，且不带凭据、查询串或片段。

    拼出来的地址要发给对方，配置错时宁可这张卡发不出去：把卡片指向
    `http://localhost:8010` 或一个带账号的地址，等于把内部拓扑发到 IM 里。
    """

    value = (deep_link_base or "").strip().rstrip("/")
    if not value:
        raise ValueError("deep_link_base is not configured")
    parts = urlsplit(value)
    if parts.scheme not in {"http", "https"} or not parts.netloc:
        raise ValueError("deep_link_base 必须是 http(s) 绝对地址")
    if parts.username or parts.password:
        raise ValueError("deep_link_base 不能带凭据")
    if parts.path not in {"", "/"} or parts.query or parts.fragment:
        raise ValueError("deep_link_base 只能是源（scheme://host[:port]）")
    return value


def build_task_card(
    *,
    task_id: UUID,
    flow_id: str,
    flow_title: str,
    status: TaskStatus,
    deep_link_base: str,
) -> TaskCard:
    """由**标量**拼卡片，不接收 `Task`。

    这是本文件最关键的签名：只要入参里没有 `Task`，槽位、办理结果、会话主体
    就没有进入卡片的通路。用 `Task` 当入参的实现迟早会有人"顺手"把 summary
    加进去，而那种改动在评审时看起来完全无害。
    """

    copy = copy_for_status(status)
    base = _validated_base(deep_link_base)
    title = f"任务状态更新｜{copy.title}"
    # 流程标题是运营侧配置的公开信息，不是服务对象数据。
    subject_line = (flow_title or flow_id).strip()
    description = f"{subject_line}：{copy.hint}"
    return TaskCard(
        task_id=task_id,
        flow_id=flow_id,
        title=title,
        description=description,
        url=f"{base}/staff/tasks/{task_id}",
    )


def card_event_type(event_type: EventType) -> bool:
    return event_type in CARD_EVENTS


def now_timestamp() -> float:
    """当前时间戳。抽成函数是为了让测试能注入固定时刻。"""

    return utc_now().timestamp()
