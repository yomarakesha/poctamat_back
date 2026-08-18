import uuid
from contextvars import ContextVar

from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware

from app.core.config import get_settings

# The id that ties an error envelope, a log line and a journal row to one
# request. Kept in a context variable as well as on request.state because the
# audit service is called from services that never see the request.
current_trace_id: ContextVar[str | None] = ContextVar("trace_id", default=None)


class RequestContextMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        settings = get_settings()
        request.state.trace_id = request.headers.get("X-Trace-Id") or str(uuid.uuid4())
        current_trace_id.set(request.state.trace_id)

        header = request.headers.get("Accept-Language", "")
        primary = header.split(",")[0].split("-")[0].strip().lower()
        request.state.language = (
            primary if primary in settings.supported_languages else settings.default_language
        )

        response = await call_next(request)
        response.headers["X-Trace-Id"] = request.state.trace_id
        return response


def get_trace_id() -> str | None:
    """The current request's trace id, or None outside a request (a worker)."""
    return current_trace_id.get()


def get_language(request: Request) -> str:
    return getattr(request.state, "language", get_settings().default_language)
