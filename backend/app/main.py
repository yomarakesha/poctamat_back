import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import APIRouter, FastAPI

from app.api.admin import audit as admin_audit
from app.api.admin import auth as admin_auth
from app.api.admin import bookings as admin_bookings
from app.api.admin import cell_types as admin_cell_types
from app.api.admin import cells as admin_cells
from app.api.admin import clients as admin_clients
from app.api.admin import custody as admin_custody
from app.api.admin import devices as admin_devices
from app.api.admin import postamats as admin_postamats
from app.api.admin import realtime as admin_realtime
from app.api.admin import stats as admin_stats
from app.api.admin import tariffs as admin_tariffs
from app.api.admin import users as admin_users
from app.api.mobile import auth as mobile_auth
from app.api.mobile import bookings as mobile_bookings
from app.api.mobile import payments as mobile_payments
from app.api.mobile import profile as mobile_profile
from app.api.public import catalog as public_catalog
from app.api.public import media as public_media
from app.api.public import postamats as public_postamats
from app.api.webhooks import payments as payment_webhooks
from app.api.webhooks import sms as sms_webhooks
from app.core.config import get_settings
from app.core.context import RequestContextMiddleware
from app.core.errors import install_error_handlers
from app.core.idempotency import IdempotencyMiddleware
from app.modules.notify.push import close_push_provider
from app.workers import holds, overdue

API_PREFIX = "/api/v1"

logger = logging.getLogger("app.workers")

health_router = APIRouter()


@health_router.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


async def _run_workers_forever(interval_seconds: int) -> None:
    """Tick the hold-release and overdue-escalation workers in-process.

    Runs immediately on startup — so a process that was down for a while
    catches up right away rather than waiting a full interval — then on the
    configured cadence. Each worker gets its own try/except: a persistent
    failure in one must not starve the other of every tick forever, which is
    what sharing one try block would do.

    Single-process only, like `app.core.kvstore`: two processes each running
    this loop would race to release the same hold and could each send the
    same reminder to the same customer. This deployment runs one uvicorn
    worker, which is what makes that safe.
    """
    while True:
        try:
            released = await holds.run_once()
            if released:
                logger.info("workers: released %d expired hold(s)", released)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - a bad tick must not kill the loop
            logger.exception("holds worker tick failed")

        try:
            counts = await overdue.run_once()
            if any(counts.values()):
                logger.info("workers: escalation=%s", counts)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - a bad tick must not kill the loop
            logger.exception("overdue worker tick failed")

        await asyncio.sleep(interval_seconds)


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    task = asyncio.create_task(
        _run_workers_forever(get_settings().worker_interval_seconds)
    )
    try:
        yield
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        # The push provider keeps one HTTP client alive across sends; it has
        # to be closed on this loop, not left for the garbage collector to
        # find after the loop is gone.
        await close_push_provider()


def create_app() -> FastAPI:
    app = FastAPI(title="Postamat API", version="1.0.0", lifespan=lifespan)
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
    app.include_router(mobile_bookings.router, prefix=API_PREFIX)
    app.include_router(mobile_payments.router, prefix=API_PREFIX)
    app.include_router(mobile_profile.router, prefix=API_PREFIX)
    app.include_router(admin_auth.router, prefix=API_PREFIX)
    app.include_router(admin_postamats.router, prefix=API_PREFIX)
    app.include_router(admin_cells.router, prefix=API_PREFIX)
    app.include_router(admin_tariffs.router, prefix=API_PREFIX)
    app.include_router(admin_bookings.router, prefix=API_PREFIX)
    app.include_router(admin_custody.router, prefix=API_PREFIX)
    app.include_router(admin_stats.router, prefix=API_PREFIX)
    app.include_router(admin_users.router, prefix=API_PREFIX)
    app.include_router(admin_clients.router, prefix=API_PREFIX)
    app.include_router(admin_cell_types.router, prefix=API_PREFIX)
    app.include_router(admin_devices.router, prefix=API_PREFIX)
    app.include_router(admin_audit.router, prefix=API_PREFIX)
    app.include_router(admin_realtime.router, prefix=API_PREFIX)
    app.include_router(public_catalog.router, prefix=API_PREFIX)
    app.include_router(public_media.router, prefix=API_PREFIX)
    app.include_router(public_postamats.router, prefix=API_PREFIX)
    app.include_router(payment_webhooks.router, prefix=API_PREFIX)
    app.include_router(sms_webhooks.router, prefix=API_PREFIX)
    return app


app = create_app()
