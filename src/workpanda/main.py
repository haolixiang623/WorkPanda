"""WorkPanda FastAPI 应用入口。

客户端只访问本服务；不直连模型 Runtime，也不持有模型或工具凭据。
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles

import workpanda
from workpanda.api.admin import router as admin_router
from workpanda.api.admin_identities import router as admin_identities_router
from workpanda.api.errors import register_error_handlers
from workpanda.api.im import router as im_router
from workpanda.api.ops import router as ops_router
from workpanda.api.routes import router
from workpanda.api.staff import router as staff_router
from workpanda.bootstrap import Container, build_container
from workpanda.config import Settings, get_settings

# 群众侧页面的挂载点。刻意不挂在 `/`：一个吞掉全部未匹配路径的 Mount 会把
# `/api/` 下的拼写错误也变成静态文件的 404（纯文本），调用方拿到的就不再是
# 统一的 JSON 错误体。`/` 只做一次跳转。
WEB_UI_MOUNT = "/web"


def _web_ui_directory(settings: Settings) -> Path | None:
    """解析群众侧静态目录。

    显式配置了却指不到 `index.html` 时直接失败：那说明部署写错了路径，而"安静地
    不挂载"会表现为页面打不开、日志里什么都没有。自动推断的路径（wheel 安装）
    找不到就不挂——静态资源本就该由网关下发。
    """

    if settings.web_ui_dir is not None:
        explicit = settings.web_ui_dir
        if not (explicit / "index.html").is_file():
            raise ValueError(f"web_ui_dir 指向的目录里没有 index.html：{explicit}")
        return explicit
    discovered = Path(workpanda.__file__).resolve().parents[2] / "web"
    return discovered if (discovered / "index.html").is_file() else None


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

    # 群众侧页面：只在绑定回环时挂载（见 `Settings.web_ui_served`）。
    if resolved_settings.web_ui_served:
        directory = _web_ui_directory(resolved_settings)
        if directory is not None:
            app.mount(WEB_UI_MOUNT, StaticFiles(directory=directory, html=True), name="web-ui")

            @app.get("/", include_in_schema=False)
            async def web_ui_root() -> RedirectResponse:
                """打开服务根就是群众页。相对跳转，避免部署在子路径下时跳错主机。"""

                return RedirectResponse(f"{WEB_UI_MOUNT}/", status_code=307)

    return app


app = create_app()
