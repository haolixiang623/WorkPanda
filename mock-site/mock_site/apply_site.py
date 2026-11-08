"""办理站：三个 tab、多附件上传、提交与回执回读。

它在另一个端口上，所以 `/services → /apply` 是真的跨 origin，不是同一份 document
里的路由跳转。这一点必须保真——P2 的域名白名单与「每任务一个 browser context」
正是靠这个差异才有意义。

跨站进来的唯一入口是账号站签发的一次性票据；票据换成本站 session 之后，后续三个
tab 全靠服务端 session 保持，不依赖前端暂存，也不依赖 tab 之间共享内存。
"""

from __future__ import annotations

import asyncio

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from mock_site import pages
from mock_site.config import MockConfig
from mock_site.multipart import parse_multipart
from mock_site.shared import (
    APPLY_COOKIE,
    attach_session,
    cookie_value,
    first_value,
    form_body,
    install_error_handlers,
    json_body,
    require_loopback,
)
from mock_site.state import MockDomainError, MockState, service_by_id

SITE = "apply"
UPLOAD_OVERHEAD_BYTES = 64 * 1024


def create_apply_app(state: MockState, config: MockConfig | None = None) -> FastAPI:
    startup = config or state.config
    app = FastAPI(
        title="WorkPanda 模拟站 · 办理站",
        version="0.1.0",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    install_error_handlers(
        app,
        back_href=f"{startup.account_origin}/services",
        back_label="回到事项列表",
    )

    @app.middleware("http")
    async def _site_header(request: Request, call_next):  # type: ignore[no-untyped-def]
        response = await call_next(request)
        response.headers["X-Mock-Site"] = SITE
        return response

    @app.get("/healthz")
    async def healthz() -> dict[str, object]:
        return {"status": "ok", "site": SITE, "origin": startup.apply_origin}

    def _session(request: Request):
        return state.apply_session(cookie_value(request, APPLY_COOKIE))

    def _render_materials(
        sid: str, errors: list[str], *, status_code: int = 200
    ) -> HTMLResponse:
        session = state.apply_session(sid)
        service = service_by_id(session.service_id)
        html = pages.apply_materials_page(
            session=session,
            service=service,
            config=state.config,
            count=len(session.draft.attachments),
            errors=errors,
        )
        return HTMLResponse(html, status_code=status_code)

    def _render_basic(sid: str, errors: dict[str, str], *, status_code: int = 200) -> HTMLResponse:
        session = state.apply_session(sid)
        service = service_by_id(session.service_id)
        html = pages.apply_basic_page(session=session, service=service, errors=errors)
        return HTMLResponse(html, status_code=status_code)

    @app.get("/apply")
    async def apply_entry(request: Request, ticket: str | None = None) -> Response:
        if ticket:
            session = state.redeem_handoff(ticket)
            return attach_session(
                RedirectResponse("/apply/tab1", status_code=303), APPLY_COOKIE, session.sid
            )
        state.apply_session(cookie_value(request, APPLY_COOKIE))
        return RedirectResponse("/apply/tab1", status_code=303)

    @app.get("/apply/tab1", response_class=HTMLResponse)
    async def tab_basic(request: Request) -> HTMLResponse:
        session = _session(request)
        return _render_basic(session.sid, {})

    @app.post("/apply/tab1")
    async def tab_basic_submit(request: Request) -> Response:
        session = _session(request)
        form = await form_body(request)
        errors = state.save_basic(
            session.sid,
            domicile_type=first_value(form, "domicile_type"),
            situations=form.get("situations", []),
            phone=first_value(form, "phone"),
            address=first_value(form, "address"),
        )
        if errors:
            by_field = {item["field"]: item["message"] for item in errors}
            return _render_basic(session.sid, by_field, status_code=400)
        return RedirectResponse("/apply/tab2", status_code=303)

    @app.get("/apply/tab2", response_class=HTMLResponse)
    async def tab_materials(request: Request) -> HTMLResponse:
        session = _session(request)
        return _render_materials(session.sid, [])

    @app.post("/apply/tab2/upload")
    async def tab_materials_upload(request: Request) -> Response:
        session = _session(request)
        content_type = request.headers.get("content-type", "")
        if not content_type.lower().startswith("multipart/form-data"):
            raise MockDomainError(
                415, "multipart_required", "这个表单只接受 multipart/form-data"
            )
        ceiling = (
            state.config.max_upload_bytes * state.config.max_attachments + UPLOAD_OVERHEAD_BYTES
        )
        declared = request.headers.get("content-length", "")
        if declared.isdigit() and int(declared) > ceiling:
            raise MockDomainError(
                413,
                "request_too_large",
                f"整包请求超过上限（{ceiling} 字节）",
            )
        body = await request.body()
        if len(body) > ceiling:
            raise MockDomainError(413, "request_too_large", f"整包请求超过上限（{ceiling} 字节）")
        parts = parse_multipart(content_type, body)
        uploads = [part for part in parts if part.filename is not None and part.filename]
        if not uploads:
            # 浏览器对「没选文件」的 file input 会发一个 filename="" 的 part，这里当没选。
            return _render_materials(
                session.sid, ["没有收到文件：请至少选择一个文件"], status_code=400
            )
        errors: list[str] = []
        status = 0
        for part in uploads:
            try:
                state.add_attachment(
                    session.sid,
                    filename=part.filename,
                    content_type=part.content_type,
                    content=part.content,
                )
            except MockDomainError as exc:
                errors.append(f"{part.filename}：{exc.message}")
                status = status or exc.status
                if exc.code == "attachment_limit_reached":
                    break
        if errors:
            return _render_materials(session.sid, errors, status_code=status or 400)
        return RedirectResponse("/apply/tab2", status_code=303)

    @app.post("/apply/tab2/delete")
    async def tab_materials_delete(request: Request) -> Response:
        session = _session(request)
        form = await form_body(request)
        state.delete_attachment(session.sid, first_value(form, "file_id"))
        return RedirectResponse("/apply/tab2", status_code=303)

    @app.get("/apply/tab3", response_class=HTMLResponse)
    async def tab_confirm(request: Request) -> HTMLResponse:
        session = _session(request)
        service = service_by_id(session.service_id)
        html = pages.apply_confirm_page(
            session=session,
            service=service,
            blockers=state.submit_blockers(session.sid),
        )
        return HTMLResponse(html)

    @app.post("/apply/submit")
    async def submit(request: Request) -> Response:
        session = _session(request)
        # 先判能不能交，再谈「慢」和「不返回」：阻塞项不能靠等超时变成成功。
        state.require_submittable(session.sid)
        mode = state.config.submit_mode
        if mode == "slow":
            await asyncio.sleep(state.config.submit_delay_seconds)
        if mode == "no_response":
            # 挂住再 504：调用方在读超时之后什么都拿不到，这才是「结果未知」。
            # 服务端在此模式下**不落回执**，重试不会造成重复受理。
            await asyncio.sleep(state.config.submit_hang_seconds)
            raise MockDomainError(
                504, "submit_no_response", "服务端没有在超时前返回，结果未知"
            )
        receipt = state.submit(session.sid)
        if mode == "error_but_accepted":
            raise MockDomainError(
                500,
                "submit_error_after_accept",
                "服务端返回 500，但这次申请可能已经受理；请用回执回读确认，不要重复提交",
            )
        return RedirectResponse(f"/receipt/{receipt.no}", status_code=303)

    @app.get("/receipt/{no}")
    async def receipt(no: str, request: Request) -> Response:
        record = state.receipt(no)
        if "application/json" in request.headers.get("accept", "").lower():
            return JSONResponse(record.as_dict())
        return HTMLResponse(
            pages.receipt_page(receipt=record, service_name=service_by_id(record.service_id).name)
        )

    @app.get("/receipt/{no}/status")
    async def receipt_status(no: str) -> JSONResponse:
        return JSONResponse(state.receipt(no).as_dict())

    # ---------------------------------------------------------------- 仅测试用

    @app.get("/__mock__/state")
    async def mock_state(request: Request) -> JSONResponse:
        require_loopback(request)
        snapshot = state.snapshot()
        snapshot["draft"] = _draft_summary(request)
        return JSONResponse(snapshot)

    @app.get("/__mock__/receipts")
    async def mock_receipts(request: Request) -> JSONResponse:
        """回执列表。用途只有一个：证明「响应说 500，但服务端其实已经受理了」。"""

        require_loopback(request)
        session = _session(request)
        return JSONResponse(
            {"receipts": [item.as_dict() for item in state.receipts_for(session.sid)]}
        )

    @app.post("/__mock__/config")
    async def mock_config(request: Request) -> JSONResponse:
        require_loopback(request)
        return JSONResponse(state.patch_switches(await json_body(request)))

    @app.post("/__mock__/reset")
    async def mock_reset(request: Request) -> JSONResponse:
        require_loopback(request)
        state.reset()
        return JSONResponse({"reset": True})

    def _draft_summary(request: Request) -> dict[str, object] | None:
        sid = cookie_value(request, APPLY_COOKIE)
        if not sid:
            return None
        try:
            session = state.apply_session(sid)
        except MockDomainError:
            return None
        draft = session.draft
        return {
            "service_id": session.service_id,
            "username": session.username,
            "domicile_type": draft.domicile_type,
            "situations": list(draft.situations),
            "phone": draft.phone,
            "address": draft.address,
            "attachments": [item.filename for item in draft.attachments],
        }

    return app
