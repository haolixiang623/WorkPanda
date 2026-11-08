"""账号站：登录（密码 + 图形验证码）、扫码登录、事项列表、跨站跳转。

站点的职责到「把用户交给另一个 origin」为止。它不碰办理页的任何字段，也不认识
回执——跨站之后的会话是办理站自己的，这条边界是 P2 要验证的东西，靶子自己不能糊。
"""

from __future__ import annotations

from urllib.parse import quote

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from mock_site import pages
from mock_site.config import MockConfig
from mock_site.graphics import as_data_uri, qr_payload, render_captcha_svg, render_qr_svg
from mock_site.shared import (
    LOGIN_COOKIE,
    attach_session,
    cookie_value,
    install_error_handlers,
    json_body,
    require_loopback,
)
from mock_site.state import SERVICES, MockDomainError, MockState

SITE = "account"


def create_account_app(state: MockState, config: MockConfig | None = None) -> FastAPI:
    """建账号站。`config` 只用来定 origin，其余开关都读 `state.config`（可运行期改）。"""

    startup = config or state.config
    app = FastAPI(
        title="WorkPanda 模拟站 · 账号站",
        version="0.1.0",
        # 关掉默认文档页：它从 CDN 拉 swagger-ui，那就不是「不联网」了。
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    install_error_handlers(app, back_href="/login", back_label="返回登录")

    @app.middleware("http")
    async def _site_header(request: Request, call_next):  # type: ignore[no-untyped-def]
        response = await call_next(request)
        response.headers["X-Mock-Site"] = SITE
        return response

    @app.get("/healthz")
    async def healthz() -> dict[str, object]:
        return {"status": "ok", "site": SITE, "origin": startup.account_origin}

    @app.get("/", include_in_schema=False)
    async def root() -> RedirectResponse:
        return RedirectResponse("/login", status_code=303)

    @app.get("/login", response_class=HTMLResponse)
    async def login_form() -> HTMLResponse:
        captcha_id, code = state.issue_captcha()
        return HTMLResponse(
            pages.login_page(
                captcha_id=captcha_id,
                captcha_svg=as_data_uri(render_captcha_svg(code)),
                max_failures=state.config.captcha_max_failures,
            )
        )

    @app.post("/login")
    async def login_submit(request: Request) -> Response:
        form = await request.form()
        username = str(form.get("username", "")).strip()
        try:
            session = state.login(
                username=username,
                password=str(form.get("password", "")),
                captcha_id=str(form.get("captcha_id", "")),
                captcha_code=str(form.get("captcha_code", "")),
            )
        except MockDomainError as exc:
            captcha_id, code = state.issue_captcha()
            html = pages.login_page(
                captcha_id=captcha_id,
                captcha_svg=as_data_uri(render_captcha_svg(code)),
                username=username,
                error=exc.message,
                error_code=exc.code,
                locked=state.is_locked(username),
                max_failures=state.config.captcha_max_failures,
            )
            return HTMLResponse(html, status_code=exc.status)
        return attach_session(
            RedirectResponse("/services", status_code=303), LOGIN_COOKIE, session.sid
        )

    @app.get("/login/qr", response_class=HTMLResponse)
    async def qr_form() -> HTMLResponse:
        ticket = state.issue_qr_ticket()
        payload = qr_payload(startup.account_origin, ticket.ticket)
        return HTMLResponse(
            pages.qr_login_page(
                ticket=ticket.ticket,
                qr_svg=as_data_uri(render_qr_svg(payload)),
                ttl_seconds=state.config.qr_ttl_seconds,
            )
        )

    @app.get("/login/qr/{ticket}/status")
    async def qr_status(ticket: str) -> JSONResponse:
        status = state.qr_status(ticket)
        payload: dict[str, object] = {"ticket": ticket, "status": status}
        if status == "confirmed":
            payload["session_url"] = f"/login/qr/{quote(ticket)}/session"
        return JSONResponse(payload)

    @app.get("/login/qr/{ticket}/session")
    async def qr_session(ticket: str) -> Response:
        session = state.open_qr_session(ticket)
        return attach_session(
            RedirectResponse("/services", status_code=303), LOGIN_COOKIE, session.sid
        )

    @app.get("/services", response_class=HTMLResponse)
    async def services(request: Request) -> HTMLResponse:
        session = state.login_session(cookie_value(request, LOGIN_COOKIE))
        return HTMLResponse(pages.services_page(username=session.username, services=SERVICES))

    @app.get("/handoff/{service_id}")
    async def handoff(service_id: str, request: Request) -> Response:
        session = state.login_session(cookie_value(request, LOGIN_COOKIE))
        ticket = state.issue_handoff(sid=session.sid, service_id=service_id)
        target = f"{state.config.apply_origin}/apply?ticket={quote(ticket.ticket)}"
        return RedirectResponse(target, status_code=303)

    # ---------------------------------------------------------------- 仅测试用

    @app.get("/__mock__/state")
    async def mock_state(request: Request) -> JSONResponse:
        require_loopback(request)
        return JSONResponse(state.snapshot())

    @app.post("/__mock__/config")
    async def mock_config(request: Request) -> JSONResponse:
        require_loopback(request)
        return JSONResponse(state.patch_switches(await json_body(request)))

    @app.post("/__mock__/reset")
    async def mock_reset(request: Request) -> JSONResponse:
        require_loopback(request)
        state.reset()
        return JSONResponse({"reset": True})

    @app.get("/__mock__/captcha/{captcha_id}")
    async def mock_captcha(captcha_id: str, request: Request) -> JSONResponse:
        require_loopback(request)
        return JSONResponse({"captcha_id": captcha_id, "code": state.peek_captcha(captcha_id)})

    @app.post("/__mock__/qr/{ticket}/confirm")
    async def mock_qr_confirm(ticket: str, request: Request) -> JSONResponse:
        require_loopback(request)
        body = await json_body(request)
        record = state.confirm_qr(ticket, username=str(body.get("username", "")))
        return JSONResponse(
            {"ticket": record.ticket, "status": "confirmed", "username": record.username}
        )

    return app
