"""两个站点共用的小工具：cookie 名、回环守卫、JSON 体与错误渲染。

放在一个文件里是为了让「哪些行为属于模拟站基础设施」一眼可见。这里没有任何业务
判断——判断都在 `state.py`，HTML 都在 `pages.py`。
"""

from __future__ import annotations

import json
from urllib.parse import parse_qs

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response

from mock_site import pages
from mock_site.config import LOOPBACK_HOSTS
from mock_site.state import MockDomainError

LOGIN_COOKIE = "mock_session"
APPLY_COOKIE = "mock_apply_session"


def require_loopback(request: Request) -> None:
    """`/__mock__/*` 只接受回环调用。模拟站不对外暴露，这条也顺带挡住误配的反向代理。"""

    client = request.client
    host = (client.host if client is not None else "") or ""
    if host not in LOOPBACK_HOSTS:
        raise MockDomainError(403, "loopback_only", "这个端点只接受来自回环地址的调用")


async def json_body(request: Request) -> dict[str, object]:
    raw = await request.body()
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise MockDomainError(400, "invalid_json", "请求体不是合法 JSON") from exc
    if not isinstance(parsed, dict):
        raise MockDomainError(400, "invalid_json", "请求体必须是 JSON 对象")
    return parsed


async def form_body(request: Request) -> dict[str, list[str]]:
    """读 `application/x-www-form-urlencoded`，保留同名多值。

    刻意不用 Starlette 的 `request.form()`：它一进函数就断言 `python-multipart` 存在，
    哪怕这次请求只是 urlencoded。那会把「模拟站不依赖额外解析器」这条边界悄悄破掉，
    本地装了依赖看不出来，CI 没装就是 500。multipart 走 `mock_site/multipart.py`。
    """

    raw = await request.body()
    if not raw:
        return {}
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise MockDomainError(400, "invalid_form", "表单请求体不是合法 UTF-8") from exc
    return {key: list(values) for key, values in parse_qs(text, keep_blank_values=True).items()}


def first_value(form: dict[str, list[str]], name: str) -> str:
    """取单值字段；缺字段与空字段都当空串，交给 `state` 去判该报什么错。"""

    values = form.get(name) or []
    return values[0] if values else ""


def cookie_value(request: Request, name: str) -> str | None:
    value = request.cookies.get(name)
    return value or None


def attach_session(response: Response, name: str, sid: str) -> Response:
    response.set_cookie(
        name,
        sid,
        httponly=True,
        samesite="lax",
        path="/",
    )
    return response


def install_error_handlers(app: FastAPI, *, back_href: str, back_label: str) -> None:
    @app.exception_handler(MockDomainError)
    async def _handle_mock_error(request: Request, exc: MockDomainError) -> Response:
        # 机器接口（`/__mock__/*`、`/status`）永远给 JSON：调用方是按字段解析的，
        # 给它一张 HTML 错误页等于让它去猜。
        json_only = request.url.path.startswith("/__mock__") or request.url.path.endswith(
            "/status"
        )
        if json_only or "application/json" in request.headers.get("accept", "").lower():
            return JSONResponse(
                {"error_code": exc.code, "message": exc.message, "detail": exc.detail},
                status_code=exc.status,
            )
        html = pages.error_page(
            status=exc.status,
            code=exc.code,
            message=exc.message,
            detail=exc.detail,
            back_href=back_href,
            back_label=back_label,
        )
        return HTMLResponse(html, status_code=exc.status)
