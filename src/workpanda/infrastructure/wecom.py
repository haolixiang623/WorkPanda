"""企业微信网关 adapter：入站验签/解密/归一化，出站卡片发送。

放在 `infrastructure` 是因为它只跟外部协议打交道（D2）：验签、归一化、发送。
**它不知道任务是什么，也没有任何能改动任务状态的手段**——解析出来的
`InboundMessage` 交给应用层，发送的 `TaskCard` 也由应用层组装。

两条与"别猜"有关的实现细节：

- 入站只认**加密模式**回调（理由见 `wecom_crypto`）；
- 出站只用 `textcard`：标题 + 一段文字 + 一个链接。富文本、图片、文件都需要
  更多字段，而每个字段都是一次"把服务对象数据带出去"的机会。
"""

from __future__ import annotations

import hashlib
import time
from collections.abc import Mapping
from datetime import UTC, datetime
from threading import Lock
from typing import Any

import httpx

from workpanda.domain.im_gateway import (
    WECOM_CHANNEL,
    ImPayloadRejected,
    InboundMessage,
    TaskCard,
)
from workpanda.infrastructure.wecom_crypto import (
    MAX_CALLBACK_BYTES,
    aes_key_bytes,
    decrypt_message,
    parse_fields,
    verify_callback_signature,
)

TOKEN_URL = "https://qyapi.weixin.qq.com/cgi-bin/gettoken"
SEND_URL = "https://qyapi.weixin.qq.com/cgi-bin/message/send"
# access_token 失效/过期的错误码。只对这几个重取一次；其它错误重试没有意义，
# 反而会把一个配置错误放大成多次调用。
_TOKEN_ERROR_CODES = frozenset({40001, 40014, 41001, 42001})
# token 提前 60 秒失效，避免"取到时还剩 1 秒"这种边界。
_TOKEN_MARGIN_SECONDS = 60


class WeComSendError(RuntimeError):
    """出站失败。调用方（应用层）会把它记成一条 FAILED 留痕，不影响任务推进。"""


class WeComConfigError(ValueError):
    """企业微信配置不成立（缺项、格式错）。启动期失败，不留到运行期。"""


def validate_wecom_config(
    *, token: str, encoding_aes_key: str, corp_id: str
) -> tuple[str, str, str]:
    """三项要么全配、要么全空。半套配置是最坏的情况：接口看起来开着，实际每条
    回调都会失败，而失败原因看起来像"对方配错了"。"""

    values = [token.strip(), encoding_aes_key.strip(), corp_id.strip()]
    configured = [bool(value) for value in values]
    if any(configured) and not all(configured):
        raise WeComConfigError(
            "企业微信回调配置必须三项齐全：wecom_callback_token / "
            "wecom_encoding_aes_key / wecom_corp_id。"
        )
    if not all(configured):
        return "", "", ""
    # 密钥格式在启动期就验一遍：只有 43 字符的 base64 才能解出 32 字节。
    aes_key_bytes(values[1])
    return values[0], values[1], values[2]


def validate_wecom_outbound(
    *, corp_secret: str, agent_id: int, console_base_url: str
) -> tuple[str, int]:
    """出站配置要么整套给出、要么整套留空。

    三种半套配置都会表现为"卡片永远发不出去"：只给 secret、只给 agentid、
    给了凭据却没给站点根（拼不出深链，投递被记成 `skipped/outbound_disabled`）。
    它们都不是运行期故障，而是配置漏项，所以必须在启动期失败——否则现场看到的是
    "任务在跑、卡片没有"，而日志里只有一条看起来正常的 skipped。
    """

    secret = (corp_secret or "").strip()
    if bool(secret) != (agent_id > 0):
        raise WeComConfigError(
            "企业微信出站配置必须同时给出 wecom_corp_secret 与正整数的 wecom_agent_id；"
            "只配一半等于永远不会发卡片。"
        )
    if not secret:
        return "", 0
    if not (console_base_url or "").strip():
        raise WeComConfigError(
            "配了企业微信出站凭据就必须给 staff_console_base_url："
            "没有站点根就拼不出卡片深链。"
        )
    return secret, agent_id


class WeComCallbackAdapter:
    """把一次 HTTP 回调变成 `InboundMessage`。"""

    def __init__(
        self,
        *,
        token: str,
        encoding_aes_key: str,
        corp_id: str,
        max_body_bytes: int = MAX_CALLBACK_BYTES,
    ) -> None:
        self._token = token
        self._encoding_aes_key = encoding_aes_key
        self._corp_id = corp_id
        self._max_body_bytes = max_body_bytes

    def verify_url(self, params: Mapping[str, str]) -> str:
        """URL 验证：验签后把 `echostr` 解出来原样回给企业微信。

        只走这一个入口，不额外开一条"随便回显"的调试路径——回显一个没验签的
        参数，等于给了一个反射型注入点。
        """

        echostr = (params.get("echostr") or "").strip()
        if not echostr:
            raise ImPayloadRejected("缺少 echostr。")
        verify_callback_signature(
            token=self._token,
            signature=params.get("msg_signature", ""),
            timestamp=params.get("timestamp", ""),
            nonce=params.get("nonce", ""),
            encrypt=echostr,
        )
        return decrypt_message(self._encoding_aes_key, echostr, self._corp_id)

    def read(self, params: Mapping[str, str], body: bytes) -> InboundMessage:
        if not body:
            raise ImPayloadRejected("回调内容为空。")
        if len(body) > self._max_body_bytes:
            raise ImPayloadRejected("回调内容超出大小上限。")
        try:
            text = body.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ImPayloadRejected("回调内容不是 UTF-8。") from exc

        envelope = parse_fields(text)
        encrypt = envelope.get("Encrypt", "")
        if not encrypt:
            # 明文回调：签名不覆盖消息体（见 wecom_crypto 的说明），收下它等于
            # 假装验签。这里明确拒绝，并在留痕里给出可操作的原因。
            raise ImPayloadRejected("未加密的回调不受支持：请在管理后台开启加密模式。")
        verify_callback_signature(
            token=self._token,
            signature=params.get("msg_signature", ""),
            timestamp=params.get("timestamp", ""),
            nonce=params.get("nonce", ""),
            encrypt=encrypt,
        )
        inner = decrypt_message(self._encoding_aes_key, encrypt, self._corp_id)
        return self._normalize(inner)

    def _normalize(self, inner_xml: str) -> InboundMessage:
        """内层 XML → 内部事件。字段名按企业微信的约定，缺失即按缺失处理，
        不猜默认值（猜出来的 userid 会指向另一个真实的人）。"""

        fields = parse_fields(inner_xml)
        im_user_id = fields.get("FromUserName", "").strip()
        if not im_user_id:
            raise ImPayloadRejected("回调缺少 FromUserName。")
        msg_type = (fields.get("MsgType") or "unknown").strip().lower()
        message_id = fields.get("MsgId", "").strip()
        if not message_id:
            # 事件回调没有 MsgId。用内层报文的摘要当去重键：重投的报文字节相同，
            # 摘要也就相同，幂等仍然成立；而内容不同的两次事件不会被误判成一次。
            digest = hashlib.sha1(inner_xml.encode("utf-8")).hexdigest()
            message_id = f"sha1:{digest}"
        return InboundMessage(
            channel=WECOM_CHANNEL,
            message_id=message_id[:256],
            im_user_id=im_user_id,
            msg_type=msg_type,
            text=fields.get("Content", "")[:4000],
            sent_at=_parse_timestamp(fields.get("CreateTime", "")),
        )


def _parse_timestamp(raw: str) -> datetime | None:
    try:
        return datetime.fromtimestamp(int(raw), tz=UTC)
    except (TypeError, ValueError):
        # CreateTime 只用于排障。解析不出来不算错误，也不必因此丢掉整条消息。
        return None


class WeComCardSender:
    """把一张卡片发到企业微信应用消息。

    发送需要 `access_token`，而它是"取一次用两小时"的。缓存与刷新放在这里，
    没有放进应用层：token 的过期语义属于渠道，应用层不该知道。
    """

    def __init__(
        self,
        *,
        corp_id: str,
        corp_secret: str,
        agent_id: int,
        timeout_seconds: float = 5.0,
        client: httpx.Client | None = None,
    ) -> None:
        if not corp_id or not corp_secret:
            raise WeComConfigError("发送卡片需要 wecom_corp_id 与 wecom_corp_secret。")
        if agent_id <= 0:
            raise WeComConfigError("发送卡片需要正整数 wecom_agent_id（应用 agentid）。")
        self._corp_id = corp_id
        self._corp_secret = corp_secret
        self._agent_id = agent_id
        self._client = client or httpx.Client(timeout=timeout_seconds)
        self._owns_client = client is None
        self._token: str = ""
        self._token_expires_at: float = 0.0
        self._lock = Lock()

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def send(self, im_user_id: str, card: TaskCard) -> None:
        recipient = (im_user_id or "").strip()
        if not recipient:
            raise WeComSendError("收件人为空。")
        payload: dict[str, Any] = {
            "touser": recipient,
            "msgtype": "textcard",
            "agentid": self._agent_id,
            "textcard": {
                "title": card.title,
                "description": card.description,
                "url": card.url,
                "btntxt": "查看详情",
            },
        }
        self._post(payload, retry_on_token_error=True)

    # ---------- 内部 ----------

    def _post(self, payload: dict[str, Any], *, retry_on_token_error: bool) -> None:
        token = self._access_token()
        try:
            response = self._client.post(SEND_URL, params={"access_token": token}, json=payload)
        except httpx.HTTPError as exc:
            raise WeComSendError(f"发送请求失败：{type(exc).__name__}") from exc
        data = _json_or_raise(response)
        code = int(data.get("errcode", 0) or 0)
        if code in _TOKEN_ERROR_CODES and retry_on_token_error:
            # token 过期或无效：清掉缓存重取一次。只重试一次——第二次还错，
            # 说明问题不在 token，继续重试只是把一次故障放大成一串。
            self._invalidate_token()
            self._post(payload, retry_on_token_error=False)
            return
        if code != 0:
            raise WeComSendError(f"发送被拒绝：errcode={code}")

    def _access_token(self) -> str:
        with self._lock:
            if self._token and time.monotonic() < self._token_expires_at:
                return self._token
            try:
                response = self._client.get(
                    TOKEN_URL, params={"corpid": self._corp_id, "corpsecret": self._corp_secret}
                )
            except httpx.HTTPError as exc:
                raise WeComSendError(f"获取 token 失败：{type(exc).__name__}") from exc
            data = _json_or_raise(response)
            if int(data.get("errcode", 0) or 0) != 0:
                raise WeComSendError(f"获取 token 被拒绝：errcode={data.get('errcode')}")
            token = str(data.get("access_token", "")).strip()
            if not token:
                raise WeComSendError("获取 token 的响应里没有 access_token。")
            expires_in = int(data.get("expires_in", 7200) or 7200)
            self._token = token
            self._token_expires_at = time.monotonic() + max(
                1, expires_in - _TOKEN_MARGIN_SECONDS
            )
            return token

    def _invalidate_token(self) -> None:
        with self._lock:
            self._token = ""
            self._token_expires_at = 0.0


def _json_or_raise(response: httpx.Response) -> dict[str, Any]:
    """企业微信的错误可能以 HTTP 200 + errcode 返回，也可能是 5xx。
    两种都要变成异常，否则"发失败"会被当成"发成功"。"""

    if response.status_code >= 400:
        raise WeComSendError(f"企业微信返回 HTTP {response.status_code}")
    try:
        data = response.json()
    except ValueError as exc:
        raise WeComSendError("企业微信返回的不是 JSON。") from exc
    if not isinstance(data, dict):
        raise WeComSendError("企业微信返回的 JSON 不是对象。")
    return data
