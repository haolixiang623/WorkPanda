"""企业微信回调接口 `/api/im/wecom/callback`。

这组接口是企业微信自建应用的回调地址，形状由对方决定（`GET` 做 URL 验证、
`POST` 推消息），所以它不像 `/api/ops`、`/api/staff` 那样带 `X-WorkPanda-*` 凭据头：
**回调签名本身就是凭据**。相应地，未开启 IM 网关时整个前缀返回 404（与 staff、
admin 一致），而不是 401——401 会让人以为"带上某种头就能进"。

顺序是这条链路唯一需要记住的事：**先验签，再碰请求体**。反过来的话，匿名输入就会
进入 XML 解析器、日志与判重表。因此本文件的 `POST` 分支在验签失败时直接返回 403，
既不解析 body，也不记录 body。

接口只做"验签 → 归一化 → 交给 `ImInboundService`"，不判断身份、不碰任务。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request, status
from fastapi.responses import PlainTextResponse

from workpanda.api.dependencies import get_container
from workpanda.bootstrap import Container
from workpanda.domain.im_gateway import ImSignatureInvalid, verify_callback_signature
from workpanda.infrastructure.wecom_gateway import (
    MAX_CALLBACK_BYTES,
    WeComCallbackError,
    parse_callback_xml,
)

router = APIRouter(prefix="/api/im", tags=["im"])

CHANNEL = "wecom"


def _not_found() -> PlainTextResponse:
    return PlainTextResponse(status_code=status.HTTP_404_NOT_FOUND, content="not found")


def _forbidden() -> PlainTextResponse:
    """统一的拒绝应答。**不回原因**：区分"签名错"与"时间窗超限"是在给攻击者反馈。"""

    return PlainTextResponse(status_code=status.HTTP_403_FORBIDDEN, content="forbidden")


def _accepted() -> PlainTextResponse:
    """企业微信要求成功返回 200 且内容包含 success（或为空），否则会持续重投。"""

    return PlainTextResponse(content="success")


@router.get(
    "/wecom/callback",
    response_class=PlainTextResponse,
    summary="验证企业微信回调 URL",
    description="企业微信配置回调地址时会先发一次 GET，本接口按约定校验签名并回显 echostr。",
    response_description="校验通过回显 echostr；失败返回 403，未开启返回 404。",
)
async def verify_callback(
    request: Request,
    container: Container = Depends(get_container),
) -> PlainTextResponse:
    if container.im_gateway is None:
        return _not_found()

    params = request.query_params
    echo = params.get("echostr") or ""
    try:
        verify_callback_signature(
            token=container.settings.wecom_callback_token,
            signature=params.get("msg_signature"),
            timestamp=params.get("timestamp"),
            nonce=params.get("nonce"),
            echo=echo,
            max_skew_seconds=container.settings.wecom_callback_max_skew_seconds,
        )
    except ImSignatureInvalid:
        return _forbidden()
    return PlainTextResponse(content=echo)


@router.post(
    "/wecom/callback",
    response_class=PlainTextResponse,
    summary="接收企业微信回调消息",
    description="验签通过后归一化入站消息、按 message id 判重、解析 IM 身份并留痕。",
    response_description="接收成功返回 success；验签或报文不合法返回 403。",
)
async def receive_callback(
    request: Request,
    container: Container = Depends(get_container),
) -> PlainTextResponse:
    if container.im_gateway is None:
        return _not_found()

    settings = container.settings
    params = request.query_params
    nonce = params.get("nonce") or ""

    try:
        verify_callback_signature(
            token=settings.wecom_callback_token,
            signature=params.get("signature"),
            timestamp=params.get("timestamp"),
            nonce=nonce,
            max_skew_seconds=settings.wecom_callback_max_skew_seconds,
        )
    except ImSignatureInvalid:
        return _forbidden()

    # 同一个 nonce 只接受一次：明文模式的签名不覆盖消息体，截获一对参数后替换 body
    # 重发是可行的，而重复 nonce 是这类重放的必要特征。
    if not container.callback_replay.accept(nonce):
        return _forbidden()

    declared = request.headers.get("content-length")
    if declared is not None and declared.isdigit() and int(declared) > MAX_CALLBACK_BYTES:
        return _forbidden()

    body = await request.body()
    try:
        message = parse_callback_xml(body, channel=CHANNEL)
    except WeComCallbackError:
        return _forbidden()

    # 到这里才进入业务：判重、身份解析、留痕。业务结论不改变 HTTP 应答——
    # 未映射用户也算"已接收"，否则企业微信会一直重投，把一次配置问题放大成重投风暴。
    container.im_gateway.handle(message)
    return _accepted()
