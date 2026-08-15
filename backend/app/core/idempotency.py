import hashlib
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import Request
from fastapi.responses import JSONResponse
from sqlalchemy import String, UniqueConstraint, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column
from starlette.middleware.base import BaseHTTPMiddleware

from app.core.db import Base, Timestamped, UUIDPrimaryKey, get_session, session_scope
from app.core.errors import ErrorCode


class IdempotencyRecord(UUIDPrimaryKey, Timestamped, Base):
    __tablename__ = "idempotency_records"
    __table_args__ = (
        UniqueConstraint("owner", "key", "scope", name="uq_idempotency_owner_key_scope"),
    )

    owner: Mapped[str] = mapped_column(String(255), index=True)
    key: Mapped[str] = mapped_column(String(128), index=True)
    scope: Mapped[str] = mapped_column(String(255))
    request_hash: Mapped[str] = mapped_column(String(64))
    status_code: Mapped[int]
    response_body: Mapped[str] = mapped_column(String)


def _owner(request: Request) -> str:
    """Identify who owns this idempotency key.

    Keys are client-chosen, so without an owner two different clients that pick
    the same key on the same path could read each other's memoized response —
    including response bodies that will carry cell numbers and pickup PINs once
    those endpoints exist. Prefer the authenticated subject; it is checked first
    because it is the strong identity, and this whole IP-based branch is a
    fallback that tightens automatically once Task 12 wires up auth middleware
    and starts setting `request.state.subject_id`. Read it defensively with
    `getattr` since that middleware does not exist yet and must not make this
    raise today. The two namespaces are prefixed so they can never collide.
    """
    subject_id = getattr(request.state, "subject_id", None)
    if subject_id:
        return f"subject:{subject_id}"
    if request.client:
        return f"ip:{request.client.host}"
    return "anonymous"


@asynccontextmanager
async def _resolve_session(request: Request) -> AsyncIterator[AsyncSession]:
    """Open the same session a route handler would get for this request.

    Middleware runs outside FastAPI's dependency-injection system, so it cannot
    use `Depends(get_session)`. In production there is no override and this opens
    a fresh session via `session_scope()`, bound to the real engine. Tests
    override the `get_session` dependency with a session bound to the test
    engine (see `backend/tests/conftest.py`); honoring that override here keeps
    idempotency records on the same database the test schema was created on,
    instead of the production database file.
    """
    override = request.app.dependency_overrides.get(get_session)
    if override is None:
        async with session_scope() as session:
            yield session
        return

    result = override()
    if hasattr(result, "__anext__"):
        agen = result
        try:
            yield await agen.__anext__()
        finally:
            await agen.aclose()
    else:
        yield result


class IdempotencyMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        key = request.headers.get("Idempotency-Key")
        if request.method != "POST" or not key:
            return await call_next(request)

        body = await request.body()
        request_hash = hashlib.sha256(body).hexdigest()
        scope = f"{request.method} {request.url.path}"
        owner = _owner(request)

        async with _resolve_session(request) as session:
            existing = await session.scalar(
                select(IdempotencyRecord).where(
                    IdempotencyRecord.owner == owner,
                    IdempotencyRecord.key == key,
                    IdempotencyRecord.scope == scope,
                )
            )
            if existing and existing.request_hash != request_hash:
                return JSONResponse(
                    status_code=409,
                    content={
                        "error": {
                            "code": ErrorCode.IDEMPOTENCY_KEY_REUSED,
                            "message": "This Idempotency-Key was used with a different body.",
                            "details": None,
                            "trace_id": getattr(request.state, "trace_id", None),
                        }
                    },
                )
            if existing:
                return JSONResponse(
                    status_code=existing.status_code,
                    content=json.loads(existing.response_body),
                )

        response = await call_next(request)
        if response.status_code >= 500:
            return response

        chunks = [chunk async for chunk in response.body_iterator]
        payload = b"".join(chunks)

        async with _resolve_session(request) as session:
            session.add(
                IdempotencyRecord(
                    owner=owner,
                    key=key,
                    scope=scope,
                    request_hash=request_hash,
                    status_code=response.status_code,
                    response_body=payload.decode(),
                )
            )
            await session.commit()

        return JSONResponse(
            status_code=response.status_code, content=json.loads(payload.decode())
        )
