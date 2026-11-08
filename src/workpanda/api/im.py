"""IM 网关回调入口 `/api/im/*`（Issue #64）。

这个前缀**不认任何 `X-WorkPanda-*` 凭据**：调用方是企业微信服务器，它只会带
`msg_signature` / `timestamp` / `nonce` 三个查询参数。验签就是这里的身份认证，
所以这里没有、也不该有"没带凭据怎么办"的判断——那就是验签本身。

三条响应约定：

- **200 + 空 body**：已处理（或明确不处理）。企业微信只认这个，任何非 200
  都会被重投；
- **403**：验签失败。**在进入业务层之前**就返回，不写去重表、不解析身份；
- **400**：请求体不可解析（非 XML、超大、未加密、解密后 receive_id 不符）。

未配置企业微信回调参数时整个前缀返回 404，与 `/api/staff/*` 同一个立场：
没配置就是没有这个入口，而不是"存在但每次都报错"。
"""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import PlainTextResponse

from workpanda.api.dependencies import get_container
from workpanda.application.im_gateway import ImGatewayService
from workpanda.bootstrap import Container
from workpanda.domain.im_gateway import ImPayloadRejected, ImSignatureRejected
from workpanda.infrastructure.wecom_crypto import MAX_CALLBACK_BYTES

router = APIRouter(prefix="/api/im", tags=["im"])


def _gateway(container: Container) -> ImGatewayService:
    if container.im_gateway is None:
        # 与未配置 staff 凭据时一致：不暴露接口存在性。
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not found")
    return container.im_gateway


def _declared_too_large(content_length: str | None, limit: int = MAX_CALLBACK_BYTES) -> bool:
    """按 `Content-Length` 先挡一次超大请求。

    adapter 里也检查实际长度，但那是**把整个 body 读进内存之后**才知道的：
    一个声明 1 GB 的请求会在验签之前就把进程的内存吃掉。这里只做廉价判断，
    读不出来（缺失/非法）就交给下游按实际字节数判。
    """

    if not content_length:
        return False
    try:
        return int(content_length) > limit
    except ValueError:
        return False


@router.get("/wecom/callback", response_class=PlainTextResponse)
async def verify_wecom_callback(
    msg_signature: str = Query(default=""),
    timestamp: str = Query(default=""),
    nonce: str = Query(default=""),
    echostr: str = Query(default=""),
    container: Container = Depends(get_container),
) -> PlainTextResponse:
    """URL 验证：验签后原样回 `echostr`。

    明文回显一个**没验签**的参数会成为一个反射型注入点，所以这里先验签再解密。
    """

    gateway = _gateway(container)
    params = {
        "msg_signature": msg_signature,
        "timestamp": timestamp,
        "nonce": nonce,
        "echostr": echostr,
    }
    try:
        echo = await asyncio.to_thread(gateway.verify_url, params)
    except ImSignatureRejected as exc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"code": "im_signature_invalid", "detail": "回调验签失败。"},
        ) from exc
    except ImPayloadRejected as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "im_payload_invalid", "detail": str(exc)},
        ) from exc
    return PlainTextResponse(echo)


@router.post("/wecom/callback", response_class=PlainTextResponse)
async def receive_wecom_callback(
    request: Request,
    msg_signature: str = Query(default=""),
    timestamp: str = Query(default=""),
    nonce: str = Query(default=""),
    container: Container = Depends(get_container),
) -> PlainTextResponse:
    """接收消息回调。

    `handle` 放到工作线程：它可能真的去发一张卡片，那是阻塞 IO。留在事件循环上
    会让一次 IM 抖动卡住所有在线群众的 SSE 连接。
    """

    gateway = _gateway(container)
    if _declared_too_large(request.headers.get("content-length")):
        # 在 `await request.body()` 之前拒绝：否则声明 1 GB 的请求会先把内存吃掉。
        # 直接写 413：FastAPI 的 HTTP_413_REQUEST_ENTITY_TOO_LARGE 已弃用，
        # 而新常量在旧版本里没有，写数字在两个版本上都成立。
        raise HTTPException(
            status_code=413,
            detail={"code": "im_payload_too_large", "detail": "回调内容超出大小上限。"},
        )
    body = await request.body()
    params = {"msg_signature": msg_signature, "timestamp": timestamp, "nonce": nonce}
    try:
        await asyncio.to_thread(gateway.handle, params, body)
    except ImSignatureRejected as exc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"code": "im_signature_invalid", "detail": "回调验签失败。"},
        ) from exc
    except ImPayloadRejected as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "im_payload_invalid", "detail": str(exc)},
        ) from exc
    # 空 body：企业微信把它理解为"不需要回消息"，也不会因此重投。
    return PlainTextResponse("")
