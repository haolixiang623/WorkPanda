"""WorkPanda FastAPI 应用入口。

客户端只访问本服务；不直连模型 Runtime，也不持有模型或工具凭据。
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from workpanda.api.admin import router as admin_router
from workpanda.api.admin_identities import router as admin_identities_router
from workpanda.api.errors import register_error_handlers
from workpanda.api.im import router as im_router
from workpanda.api.ops import router as ops_router
from workpanda.api.routes import router
from workpanda.api.staff import router as staff_router
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

    # 跨域白名单为空时完全不装中间件：没有预检、也不会回带任何 CORS 头。
    # 校验在启动时做，通配符配错直接失败（见 Settings.cors_origin_list）。
    origins = resolved_settings.cors_origin_list
    if origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=list(origins),
            allow_credentials=True,
            allow_methods=["GET", "POST", "PUT", "DELETE"],
            allow_headers=[
                "Content-Type",
                "X-WorkPanda-Session",
                "X-WorkPanda-Ops-Token",
                "X-WorkPanda-Admin-Token",
                "X-WorkPanda-Staff-Token",
            ],
        )

    @app.get("/healthz", tags=["system"])
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    register_error_handlers(app)
    app.include_router(router)
    app.include_router(ops_router)
    app.include_router(admin_router)
    app.include_router(admin_identities_router)
    app.include_router(staff_router)
    app.include_router(im_router)
    return app


app = create_app()
