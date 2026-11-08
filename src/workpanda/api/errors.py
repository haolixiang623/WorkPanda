"""统一错误映射：对外只暴露稳定错误码，不泄露内部堆栈或敏感细节。"""

from fastapi import FastAPI, Request, status
from fastapi.responses import JSONResponse

from workpanda.application.errors import (
    AuthenticationRequired,
    CancelNotDelivered,
    IntentUnresolved,
    RateLimited,
    RequestRejected,
    TaskConflict,
)
from workpanda.domain.confirmation import ConfirmationError
from workpanda.domain.deeplink import ConfirmationLinkRejected
from workpanda.domain.flow import FlowNotFound, FlowNotRunnable
from workpanda.domain.presentation import UiState
from workpanda.domain.repository import TaskNotFoundError
from workpanda.domain.state_machine import InvalidTransition

# Starlette 已弃用 HTTP_422_UNPROCESSABLE_ENTITY 常量名，直接使用状态码避免版本差异。
UNPROCESSABLE = 422


def _error(status_code: int, code: str, message: str, **extra: object) -> JSONResponse:
    return JSONResponse(status_code=status_code, content={"code": code, "detail": message, **extra})


def register_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(TaskNotFoundError)
    async def _task_not_found(request: Request, exc: TaskNotFoundError) -> JSONResponse:
        return _error(status.HTTP_404_NOT_FOUND, "task_not_found", "任务不存在。")

    @app.exception_handler(TaskConflict)
    async def _conflict(request: Request, exc: TaskConflict) -> JSONResponse:
        return _error(status.HTTP_409_CONFLICT, "task_state_conflict", str(exc))

    @app.exception_handler(IntentUnresolved)
    async def _intent(request: Request, exc: IntentUnresolved) -> JSONResponse:
        # 带上 UI 状态键，客户端不必自己把"没有任务"翻译成"需要澄清"。
        # 此时没有任何任务被创建，也没有任何工具被调用。
        return _error(
            UNPROCESSABLE,
            "clarification_required",
            str(exc),
            ui_state=UiState.NEEDS_CLARIFICATION.value,
        )

    @app.exception_handler(RequestRejected)
    async def _rejected(request: Request, exc: RequestRejected) -> JSONResponse:
        return _error(UNPROCESSABLE, "request_rejected", str(exc))

    @app.exception_handler(InvalidTransition)
    async def _transition(request: Request, exc: InvalidTransition) -> JSONResponse:
        return _error(status.HTTP_409_CONFLICT, "invalid_transition", str(exc))

    @app.exception_handler(FlowNotFound)
    async def _flow_not_found(request: Request, exc: FlowNotFound) -> JSONResponse:
        return _error(status.HTTP_404_NOT_FOUND, "flow_not_found", str(exc))

    @app.exception_handler(FlowNotRunnable)
    async def _flow_not_runnable(request: Request, exc: FlowNotRunnable) -> JSONResponse:
        return _error(UNPROCESSABLE, "flow_not_published", str(exc))

    @app.exception_handler(ConfirmationError)
    async def _confirmation(request: Request, exc: ConfirmationError) -> JSONResponse:
        return _error(status.HTTP_409_CONFLICT, "confirmation_invalid", str(exc))

    @app.exception_handler(ConfirmationLinkRejected)
    async def _confirmation_link(request: Request, exc: ConfirmationLinkRejected) -> JSONResponse:
        # 失败分档只进留痕，不回给调用方：区分"链接有效但你不是这张任务的会话"
        # 等于告诉持卡人"这张卡是真的，去搞到那个会话"。对外只有一句话。
        return _error(
            status.HTTP_409_CONFLICT,
            "confirmation_link_invalid",
            "确认链接无效或已失效，请重新发起。",
        )

    @app.exception_handler(AuthenticationRequired)
    async def _unauthenticated(request: Request, exc: AuthenticationRequired) -> JSONResponse:
        return _error(status.HTTP_401_UNAUTHORIZED, "session_invalid", str(exc))

    @app.exception_handler(RateLimited)
    async def _rate_limited(request: Request, exc: RateLimited) -> JSONResponse:
        response = _error(status.HTTP_429_TOO_MANY_REQUESTS, "rate_limited", "请求过于频繁。")
        response.headers["Retry-After"] = str(max(1, exc.retry_after_seconds))
        return response

    @app.exception_handler(CancelNotDelivered)
    async def _cancel_not_delivered(
        request: Request, exc: CancelNotDelivered
    ) -> JSONResponse:
        # 5xx 而不是 409：这是可重试的临时故障，且任务状态没有被改动。
        return _error(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "cancel_not_delivered",
            "取消信号未送达，任务状态未改变，请重试。",
        )
