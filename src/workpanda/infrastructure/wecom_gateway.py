"""企业微信 adapter：回调报文归一化、卡片投递、以及 fail-open 的投递队列。

放在 `infrastructure` 层是刻意的（决策 D2）：网关是**通道**，不是执行者，也不是
权限来源。这个文件的能力范围仅限——解析对方发来的报文、调企微接口发消息、把
投递失败记下来。它没有 `TaskStore`，没有 `TaskService`，也不能签发确认凭证。

两条与本项目其它模块保持一致的取舍：

- **不重试**。发消息不是幂等操作，重试的代价是工作人员收到两张一样的卡；对方明确
  返回失败时重试没有意义，网络中断时重试则可能让同一件事打扰两遍。失败就记日志，
  下一次状态变化自然会再推一张。
- **异常消息不外泄**。对方返回体、含 `access_token` 的 URL 都可能出现在异常里，
  所以对外只留异常类型名或数字 errcode（与 `capacity.py` 的口径一致）。

企业微信「安全模式」（EncodingAESKey + AES-CBC 解密）本轮**未实现**：那需要引入
加密依赖。为了不让人误以为支持，配置了 `wecom_encoding_aes_key` 会在启动时直接失败，
而不是静默降级成"验过签但解不开"。
"""

from __future__ import annotations

import logging
import queue
import threading
import time
import xml.etree.ElementTree as ElementTree
from collections.abc import Callable
from typing import Protocol, runtime_checkable
from uuid import UUID

import httpx

from workpanda.config import Settings
from workpanda.domain.im_gateway import (
    ImInboundMessage,
    ImMessageKind,
    NotificationCounters,
    TaskCard,
)

logger = logging.getLogger(__name__)

WECOM_API_BASE = "https://qyapi.weixin.qq.com"
# 单条回调报文的体积上限。企微的消息报文通常在几 KB 以内，超过这个量级只可能是
# 攻击或配置错误，而 XML 解析是最不该拿去赌的地方。
MAX_CALLBACK_BYTES = 64 * 1024
# 实体/DTD 声明直接拒绝：本轮只需要 8 个固定标签，用不到任何 DTD 特性，
# 而"拒绝一切 DTD"是防实体膨胀最简单也最彻底的做法。
_FORBIDDEN_MARKERS = ("<!DOCTYPE", "<!ENTITY")


class WeComCallbackError(ValueError):
    """回调报文不合法（格式、体积、字段缺失）。调用方应记录并拒绝，不重试。"""


class NotificationFailed(RuntimeError):
    """卡片投递失败。只携带稳定的错误码，不带对方返回体。"""


def validate_im_gateway(settings: Settings) -> None:
    """启动期校验 IM 网关配置。写错宁可起不来，也不静默降级。

    静默降级的两种典型形态都很坏：「验签用空 token」等于开放匿名入口；「配了深链
    却没配应用凭据」等于每次状态变化都在内部报错、工作人员永远收不到卡片。
    """

    if not settings.im_gateway_enabled:
        return
    if not settings.wecom_callback_token.strip():
        raise ValueError(
            "im_gateway_enabled=true 时必须配置 wecom_callback_token（企业微信回调 Token）"
        )
    if settings.wecom_encoding_aes_key.strip():
        raise ValueError(
            "本轮只支持企业微信「明文模式」回调；配置了 wecom_encoding_aes_key（安全模式）"
            "会在解不开报文时静默失败，因此启动即拒绝。请改用明文模式或等待后续 issue。"
        )

    outbound = {
        "wecom_corp_id": settings.wecom_corp_id,
        "wecom_agent_id": settings.wecom_agent_id,
        "wecom_app_secret": settings.wecom_app_secret,
        "notification_deep_link_base": settings.notification_deep_link_base,
    }
    filled = {name for name, value in outbound.items() if str(value).strip()}
    if filled and len(filled) != len(outbound):
        missing = sorted(set(outbound) - filled)
        raise ValueError(
            "出站卡片配置不完整，缺少：" + ", ".join(missing) + "；要么四项齐备，要么全部留空"
        )
    if settings.notification_deep_link_base.strip():
        parts = settings.notification_deep_link_base.strip()
        if not parts.startswith(("http://", "https://")):
            raise ValueError("notification_deep_link_base 必须是 http(s) 绝对地址")


def parse_callback_xml(body: bytes, *, channel: str = "wecom") -> ImInboundMessage:
    """把企业微信明文报文归一化成 `ImInboundMessage`。

    只取会用到的字段，其余一律丢弃：`ToUserName`、`AgentID` 之类对处理没有帮助，
    留下它们只会变成"以后有人拿它做判断"的隐患。
    """

    if not body:
        raise WeComCallbackError("empty callback body")
    if len(body) > MAX_CALLBACK_BYTES:
        raise WeComCallbackError("callback body too large")

    text = body.decode("utf-8", errors="replace")
    # 先做标记检查再解析：不把带 DTD 的报文交给解析器，就无法被实体膨胀拖死。
    upper = text.upper()
    if any(marker in upper for marker in _FORBIDDEN_MARKERS):
        raise WeComCallbackError("callback body contains a DTD/entity declaration")

    try:
        # 已拒绝 DTD 且报文限长，这里的解析不接触实体声明。
        root = ElementTree.fromstring(text)
    except ElementTree.ParseError as exc:
        raise WeComCallbackError("callback body is not valid XML") from exc

    def field(name: str) -> str:
        node = root.find(name)
        return (node.text or "").strip() if node is not None else ""

    from_user = field("FromUserName")
    if not from_user:
        raise WeComCallbackError("callback body has no FromUserName")

    msg_type = field("MsgType").lower()
    create_time = field("CreateTime")
    if msg_type == "text":
        kind = ImMessageKind.TEXT
        content = field("Content")
    elif msg_type == "event":
        kind = ImMessageKind.EVENT
        content = ""
    elif msg_type:
        kind = ImMessageKind.UNSUPPORTED
        content = ""
    else:
        raise WeComCallbackError("callback body has no MsgType")

    message_id = field("MsgId")
    if not message_id:
        # 事件推送没有 MsgId。用「发件人 + 时间 + 事件名」合成一个稳定标识：
        # 没有标识就没法判重，重投会被当成两条新消息。
        event_name = field("Event") or "unknown"
        message_id = f"{from_user}:{create_time or '0'}:{event_name}"

    return ImInboundMessage(
        channel=channel,
        message_id=message_id,
        im_user_id=from_user,
        kind=kind,
        text=content[:4000],
        msg_type=msg_type[:32],
    )


@runtime_checkable
class HttpTransport(Protocol):
    """最小 HTTP 面：只为让测试注入假传输，不引入通用 HTTP 抽象。"""

    def get(self, url: str) -> dict: ...

    def post(self, url: str, payload: dict) -> dict: ...


class HttpxTransport:
    """真实传输。失败统一转成 `NotificationFailed`，不把底层异常泄漏给上层。"""

    def __init__(self, *, timeout_seconds: float = 3.0, trust_env: bool = True) -> None:
        self._client = httpx.Client(timeout=timeout_seconds, trust_env=trust_env)

    def close(self) -> None:
        self._client.close()

    def get(self, url: str) -> dict:
        return self._decode(self._client.get(url))

    def post(self, url: str, payload: dict) -> dict:
        return self._decode(self._client.post(url, json=payload))

    @staticmethod
    def _decode(response: httpx.Response) -> dict:
        if response.status_code >= 400:
            # 只留状态码：响应体里可能有对方的 trace id、内网地址或 token 片段。
            raise NotificationFailed(f"http_status_{response.status_code}")
        try:
            data = response.json()
        except ValueError as exc:
            raise NotificationFailed("response_not_json") from exc
        if not isinstance(data, dict):
            raise NotificationFailed("response_not_object")
        errcode = data.get("errcode", 0)
        if errcode not in (0, "0"):
            raise NotificationFailed(f"wecom_errcode_{errcode}")
        return data


class WeComAccessTokenProvider:
    """企业微信应用 access_token 的取用与缓存。

    提前 60 秒过期，避免"拿到 token 的瞬间正好过期"这种只在高峰期出现的偶发失败。
    取 token 失败按 `NotificationFailed` 抛出，由上层 fail-open 兜住——不缓存失败结果，
    否则一次网络抖动会让后续一小时都不再尝试。
    """

    def __init__(
        self,
        *,
        transport: HttpTransport,
        corp_id: str,
        app_secret: str,
        api_base: str = WECOM_API_BASE,
        now: Callable[[], float] | None = None,
    ) -> None:
        self._transport = transport
        self._corp_id = corp_id
        self._app_secret = app_secret
        self._api_base = api_base.rstrip("/")
        self._token: str | None = None
        self._expires_at = 0.0
        self._lock = threading.Lock()
        self._now = now or time.time

    def get(self) -> str:
        with self._lock:
            if self._token is not None and self._now() < self._expires_at:
                return self._token
            url = (
                f"{self._api_base}/cgi-bin/gettoken"
                f"?corpid={self._corp_id}&corpsecret={self._app_secret}"
            )
            try:
                data = self._transport.get(url)
            except NotificationFailed:
                raise
            except Exception as exc:  # noqa: BLE001 - 传输层异常类型不进入对外契约
                raise NotificationFailed("token_request_failed") from exc
            token = str(data.get("access_token") or "")
            if not token:
                raise NotificationFailed("token_missing")
            expires_in = int(data.get("expires_in") or 7200)
            self._token = token
            self._expires_at = self._now() + max(60, expires_in - 60)
            return token


class WeComCardSender:
    """把 `TaskCard` 发成企微应用消息（textcard）。

    用应用消息而不是群机器人：群机器人只能发到群里，而本轮的收件人是"有授权的
    具体工作人员"，不做群广播（issue 的非目标）。发到个人也顺带保证了收件人范围
    由我们的授权目录决定，而不是由群成员名单决定。
    """

    def __init__(
        self,
        *,
        transport: HttpTransport,
        tokens: WeComAccessTokenProvider,
        agent_id: str,
        api_base: str = WECOM_API_BASE,
    ) -> None:
        self._transport = transport
        self._tokens = tokens
        self._agent_id = agent_id
        self._api_base = api_base.rstrip("/")

    def send(self, im_user_id: str, card: TaskCard) -> None:
        token = self._tokens.get()
        payload = {
            "touser": im_user_id,
            "msgtype": "textcard",
            "agentid": int(self._agent_id) if self._agent_id.isdigit() else self._agent_id,
            "textcard": {
                "title": card.title,
                "description": card.description,
                "url": card.url,
                "btntxt": "查看",
            },
            # 不开重复消息检查：企微的去重是"同一应用同一成员 1800 秒内只发一条"，
            # 会把两次真实的状态变化压成一条，而我们自己的幂等已经在上游做过了。
            "enable_duplicate_check": 0,
        }
        url = f"{self._api_base}/cgi-bin/message/send?access_token={token}"
        try:
            self._transport.post(url, payload)
        except NotificationFailed:
            raise
        except Exception as exc:  # noqa: BLE001 - 同上
            raise NotificationFailed("message_send_failed") from exc


class NotificationQueue:
    """有界队列 + 单个后台线程：发卡片不阻塞任务线程。

    为什么必须有这条队列：`EventPublisher.publish` 在任务推进的调用栈里，同步发 HTTP
    会让"一次状态变化"的响应时间取决于对方接口的响应时间——对端一慢，群众侧就跟着
    卡住。而卡片是附带通知，不该有这个权力。

    队列满时**丢弃并计数**，不阻塞调用方：宁可少一张卡，也不能让推进流程等排队。
    """

    def __init__(
        self,
        handler,
        *,
        max_pending: int = 64,
        name: str = "im-notify",
        counters: NotificationCounters | None = None,
    ) -> None:
        self._handler = handler
        self._queue: queue.Queue = queue.Queue(maxsize=max(1, max_pending))
        # 与 `TaskEventNotifier` 共用同一个计数器对象，见 domain/im_gateway.py。
        self._stats = counters if counters is not None else NotificationCounters()
        self._closed = threading.Event()
        self._thread = threading.Thread(target=self._run, name=name, daemon=True)
        self._thread.start()

    @property
    def stats(self) -> NotificationCounters:
        return self._stats

    def submit(self, task_id: UUID, sequence: int) -> bool:
        if self._closed.is_set():
            self._stats.incr("dropped")
            return False
        try:
            self._queue.put_nowait((task_id, sequence))
            return True
        except queue.Full:
            self._stats.incr("dropped")
            logger.warning("im notification queue full; dropping event %s", sequence)
            return False

    def close(self, timeout: float = 2.0) -> None:
        if self._closed.is_set():
            return
        self._closed.set()
        try:
            self._queue.put_nowait(None)
        except queue.Full:
            pass
        self._thread.join(timeout=timeout)

    def _run(self) -> None:
        while True:
            try:
                item = self._queue.get(timeout=0.1)
            except queue.Empty:
                if self._closed.is_set():
                    return
                continue
            if item is None:
                return
            task_id, sequence = item
            try:
                self._handler(task_id, sequence)
            except Exception:  # noqa: BLE001 - 后台线程不能因为一次失败就退出
                self._stats.incr("handler_errors")
                logger.warning("im notification handler failed", exc_info=False)


class NotifyingEventPublisher:
    """在事件发布之后投递卡片。**先推送，再排队**，且任何时候都不抛错。

    顺序是有意的：订阅者（SSE）先拿到事件，卡片排队在后面。反过来的话，对方接口
    一次超时就会推迟一次界面更新——把"看得到"排在"通知到"之后，而不是相反。
    """

    def __init__(self, publisher, notifications: NotificationQueue | None = None) -> None:
        self._publisher = publisher
        self._notifications = notifications

    def publish(self, task_id: UUID, sequence: int) -> None:
        self._publisher.publish(task_id, sequence)
        if self._notifications is None:
            return
        try:
            self._notifications.submit(task_id, sequence)
        except Exception:  # noqa: BLE001 - 通知路径永远不能影响任务推进
            logger.warning("im notification submit failed", exc_info=False)


def build_transport(settings: Settings) -> tuple[HttpxTransport, list]:
    transport = HttpxTransport(
        timeout_seconds=settings.notification_timeout_seconds,
        trust_env=settings.wecom_trust_env,
    )
    return transport, [transport.close]


def outbound_configured(settings: Settings) -> bool:
    """四项出站配置是否齐备。启动期已校验过"要么全有、要么全无"。"""

    return all(
        str(value).strip()
        for value in (
            settings.wecom_corp_id,
            settings.wecom_agent_id,
            settings.wecom_app_secret,
            settings.notification_deep_link_base,
        )
    )


def build_card_sender(settings: Settings, transport: HttpTransport) -> WeComCardSender | None:
    """出站发送器。四项配置齐全才建；否则返回 None（调用方不再排队投递）。"""

    if not outbound_configured(settings):
        return None
    tokens = WeComAccessTokenProvider(
        transport=transport,
        corp_id=settings.wecom_corp_id.strip(),
        app_secret=settings.wecom_app_secret.strip(),
        api_base=settings.wecom_api_base,
    )
    return WeComCardSender(
        transport=transport,
        tokens=tokens,
        agent_id=settings.wecom_agent_id.strip(),
        api_base=settings.wecom_api_base,
    )
