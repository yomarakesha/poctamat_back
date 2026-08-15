from fastapi import APIRouter, FastAPI

from app.core.context import RequestContextMiddleware
from app.core.errors import install_error_handlers

API_PREFIX = "/api/v1"

health_router = APIRouter()


@health_router.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


def create_app() -> FastAPI:
    app = FastAPI(title="Postamat API", version="1.0.0")
    install_error_handlers(app)
    # RequestContextMiddleware must stay the OUTERMOST middleware: Starlette
    # applies middleware in reverse registration order, so whatever is
    # registered here first ends up outermost. Any later middleware (e.g. the
    # idempotency middleware) must call app.add_middleware(...) BEFORE this
    # line so that request.state.trace_id is already set by the time it runs.
    app.add_middleware(RequestContextMiddleware)
    app.include_router(health_router, prefix=API_PREFIX)
    return app


app = create_app()
