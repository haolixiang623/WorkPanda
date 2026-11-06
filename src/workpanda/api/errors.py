"""统一错误映射：对外只暴露稳定错误码，不泄露内部堆栈或敏感细节。"""

from fastapi import FastAPI, Request, status
from fastapi.responses import JSONResponse

from workpanda.application.errors import IntentUnresolved, RequestRejected, TaskConflict
from workpanda.domain.confirmation import ConfirmationError
from workpanda.domain.flow import FlowNotFound, FlowNotRunnable
from workpanda.domain.repository import TaskNotFoundError
from workpanda.domain.state_machine import InvalidTransition

# Starlette 已弃用 HTTP_422_UNPROCESSABLE_ENTITY 常量名，直接使用状态码避免版本差异。
UNPROCESSABLE = 422


def _error(status_code: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(status_code=status_code, content={"code": code, "detail": message})


def register_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(TaskNotFoundError)
    async def _task_not_found(request: Request, exc: TaskNotFoundError) -> JSONResponse:
        return _error(status.HTTP_404_NOT_FOUND, "task_not_found", "任务不存在。")

    @app.exception_handler(TaskConflict)
    async def _conflict(request: Request, exc: TaskConflict) -> JSONResponse:
        return _error(status.HTTP_409_CONFLICT, "task_state_conflict", str(exc))

    @app.exception_handler(IntentUnresolved)
    async def _intent(request: Request, exc: IntentUnresolved) -> JSONResponse:
        return _error(UNPROCESSABLE, "clarification_required", str(exc))

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
