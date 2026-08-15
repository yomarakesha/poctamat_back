from fastapi import APIRouter, FastAPI

API_PREFIX = "/api/v1"

health_router = APIRouter()


@health_router.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


def create_app() -> FastAPI:
    app = FastAPI(title="Postamat API", version="1.0.0")
    app.include_router(health_router, prefix=API_PREFIX)
    return app


app = create_app()
