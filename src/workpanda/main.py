"""WorkPanda FastAPI 应用入口。"""

from fastapi import FastAPI

from workpanda.api.routes import router


def create_app() -> FastAPI:
    app = FastAPI(title="WorkPanda", version="0.1.0")

    @app.get("/healthz", tags=["system"])
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    app.include_router(router)
    return app


app = create_app()
