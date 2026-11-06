"""WorkPanda FastAPI 应用入口。

客户端只访问本服务；不直连模型 Runtime，也不持有模型或工具凭据。
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from workpanda.api.errors import register_error_handlers
from workpanda.api.routes import router
from workpanda.bootstrap import Container, build_container
from workpanda.config import Settings, get_settings


def create_app(settings: Settings | None = None, container: Container | None = None) -> FastAPI:
    resolved_settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        if container is None:
            app.state.container = build_container(resolved_settings)
        try:
            yield
        finally:
            current: Container | None = getattr(app.state, "container", None)
            if current is not None:
                current.close()
                app.state.container = None

    app = FastAPI(title=resolved_settings.app_name, version="0.1.0", lifespan=lifespan)
    if container is not None:
        app.state.container = container

    @app.get("/healthz", tags=["system"])
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    register_error_handlers(app)
    app.include_router(router)
    return app


app = create_app()
