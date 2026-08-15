import uuid

from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware

from app.core.config import get_settings


class RequestContextMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        settings = get_settings()
        request.state.trace_id = request.headers.get("X-Trace-Id") or str(uuid.uuid4())

        header = request.headers.get("Accept-Language", "")
        primary = header.split(",")[0].split("-")[0].strip().lower()
        request.state.language = (
            primary if primary in settings.supported_languages else settings.default_language
        )

        response = await call_next(request)
        response.headers["X-Trace-Id"] = request.state.trace_id
        return response


def get_language(request: Request) -> str:
    return getattr(request.state, "language", get_settings().default_language)
