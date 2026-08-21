import hashlib
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import jwt
from fastapi import Request
from fastapi.responses import JSONResponse
from sqlalchemy import String, UniqueConstraint, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column
from starlette.middleware.base import BaseHTTPMiddleware

from app.core.db import Base, Timestamped, UUIDPrimaryKey, get_session, session_scope
from app.core.errors import AppError, ErrorCode
from app.core.security import decode_token


def _authenticated(request: Request) -> None:
    """Refuse a caller whose bearer token is missing, expired or forged.

    Only the token is checked here — whether that principal may do this is the
    route's own business, and answering that question twice in two places is
    how the two answers drift apart.
    """
    header = request.headers.get("Authorization", "")
    if not header.startswith("Bearer "):
        raise AppError(ErrorCode.TOKEN_INVALID, "Missing bearer token.", 401)
    try:
        decode_token(header.removeprefix("Bearer "))
    except jwt.ExpiredSignatureError:
        raise AppError(ErrorCode.TOKEN_EXPIRED, "Access token expired.", 401) from None
    except jwt.PyJWTError:
        raise AppError(ErrorCode.TOKEN_INVALID, "Access token invalid.", 401) from None


def require_idempotency_key(request: Request) -> str:
    """Refuse a write that cannot be retried safely.

    Booking allocates a physical cell and holds it for ten minutes. A retry
    after a dropped connection, or a double tap on the button, would otherwise
    take a second cell that nobody is coming to fill. The key is what lets the
    middleware answer the repeat with the first response instead.

    A caller the server does not recognise is turned away first. FastAPI
    resolves this dependency before the route's own auth, so without this check
    an unauthenticated request would be told which header it is missing —
    answering 400 where the contract documents 401, and telling a stranger
    about the endpoint's requirements before asking who they are.
    """
    _authenticated(request)
    key = request.headers.get("Idempotency-Key")
    if not key:
        raise AppError(
            ErrorCode.IDEMPOTENCY_KEY_MISSING,
            "This endpoint requires an Idempotency-Key header.", 400,
            details={"header": "Idempotency-Key"},
        )
    return key


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


def _subject_from_token(request: Request) -> str | None:
    """Read the caller's identity out of the bearer token, if there is one.

    The token is decoded here rather than taken from `request.state`, because
    middleware runs before FastAPI resolves dependencies: whatever `require_client`
    sets is set long after this has had to decide who owns the key. An invalid or
    expired token yields None and the request falls back to its address — the
    route's own auth dependency is what rejects it a moment later, and this
    function must not decide that question twice.
    """
    header = request.headers.get("Authorization", "")
    if not header.startswith("Bearer "):
        return None
    try:
        claims = decode_token(header.removeprefix("Bearer "))
    except jwt.PyJWTError:
        return None
    subject_id = claims.get("sub")
    if not subject_id:
        return None
    return f"{claims.get('typ', 'subject')}:{subject_id}"


def _owner(request: Request) -> str:
    """Identify who owns this idempotency key.

    Keys are client-chosen, so without an owner two clients that pick the same
    key on the same path would read each other's memoized response — including
    bodies carrying cell numbers and access PINs. The authenticated subject is
    the strong identity and is preferred; the address is a fallback for the
    unauthenticated surface, and a poor one, since a whole building behind NAT
    shares it. The namespaces are prefixed so they can never collide.
    """
    subject_id = getattr(request.state, "subject_id", None)
    if subject_id:
        return f"subject:{subject_id}"
    from_token = _subject_from_token(request)
    if from_token:
        return from_token
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
