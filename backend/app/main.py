from fastapi import APIRouter, FastAPI

from app.api.mobile import auth as mobile_auth
from app.core.context import RequestContextMiddleware
from app.core.errors import install_error_handlers
from app.core.idempotency import IdempotencyMiddleware

API_PREFIX = "/api/v1"

health_router = APIRouter()


@health_router.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


def create_app() -> FastAPI:
    app = FastAPI(title="Postamat API", version="1.0.0")
    install_error_handlers(app)
    # RequestContextMiddleware must stay the OUTERMOST middleware, so that
    # request.state.trace_id is already set by the time anything inner runs.
    # add_middleware inserts at the front of the list and the stack is then
    # built in reverse, so the middleware registered LAST ends up outermost.
    # Any middleware added later must therefore go ABOVE this line, not below.
    app.add_middleware(IdempotencyMiddleware)
    app.add_middleware(RequestContextMiddleware)
    app.include_router(health_router, prefix=API_PREFIX)
    app.include_router(mobile_auth.router, prefix=API_PREFIX)
    return app


app = create_app()
