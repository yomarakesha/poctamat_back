import secrets
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.db import utcnow
from app.core.errors import AppError, ErrorCode
from app.core.kvstore import get_kvstore
from app.core.security import create_access_token, hash_token
from app.modules.identity.models import Client, RefreshToken


def _otp_key(phone: str) -> str:
    return f"otp:{phone}"


def _as_utc(value: datetime) -> datetime:
    # SQLite hands back naive datetimes even for DateTime(timezone=True) columns,
    # so normalise before comparing against utcnow().
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


async def issue_otp(phone: str) -> int:
    settings = get_settings()
    code = "".join(secrets.choice("0123456789") for _ in range(settings.otp_length))
    await get_kvstore().put(
        _otp_key(phone), {"code": code, "attempts": "0"}, settings.otp_ttl_seconds
    )
    return settings.otp_ttl_seconds


async def peek_otp(phone: str) -> str | None:
    """Read the live code. For tests only — no route may expose this."""
    stored = await get_kvstore().get(_otp_key(phone))
    return stored["code"] if stored else None


async def verify_otp(phone: str, code: str) -> None:
    settings = get_settings()
    store = get_kvstore()
    stored = await store.get(_otp_key(phone))
    if not stored:
        raise AppError(ErrorCode.OTP_EXPIRED, "No active code for this number.", 400)

    attempts = int(stored["attempts"]) + 1
    if attempts >= settings.otp_max_attempts:
        await store.delete(_otp_key(phone))
        raise AppError(ErrorCode.OTP_TOO_MANY_ATTEMPTS, "Too many attempts.", 429)

    if not secrets.compare_digest(stored["code"], code):
        await store.set_field(_otp_key(phone), "attempts", str(attempts))
        raise AppError(
            ErrorCode.OTP_INVALID, "Wrong code.", 400,
            details={"attempts_left": settings.otp_max_attempts - attempts},
        )

    await store.delete(_otp_key(phone))


async def get_or_create_client(session: AsyncSession, phone: str) -> tuple[Client, bool]:
    existing = await session.scalar(select(Client).where(Client.phone == phone))
    if existing:
        if existing.is_blocked:
            raise AppError(ErrorCode.CLIENT_BLOCKED, "Client is blocked.", 403)
        return existing, False

    created = Client(phone=phone)
    session.add(created)
    await session.flush()
    return created, True


async def issue_token_pair(
    session: AsyncSession, subject_type: str, subject_id: uuid.UUID, extra: dict | None = None
) -> tuple[str, str]:
    settings = get_settings()
    access = create_access_token(subject_type, subject_id, extra)
    refresh = secrets.token_urlsafe(48)
    session.add(
        RefreshToken(
            token_hash=hash_token(refresh),
            subject_type=subject_type,
            subject_id=subject_id,
            expires_at=utcnow() + timedelta(days=settings.refresh_token_ttl_days),
        )
    )
    await session.flush()
    return access, refresh


async def _load_usable_refresh_token(session: AsyncSession, token: str) -> RefreshToken:
    stored = await session.scalar(
        select(RefreshToken).where(RefreshToken.token_hash == hash_token(token))
    )
    # Unknown, already rotated and expired all answer the same way: distinguishing
    # them would tell a caller something about tokens it does not hold.
    if (
        stored is None
        or stored.revoked_at is not None
        or _as_utc(stored.expires_at) <= utcnow()
    ):
        raise AppError(ErrorCode.TOKEN_INVALID, "Refresh token is not usable.", 401)
    return stored


async def rotate_refresh_token(session: AsyncSession, token: str) -> tuple[str, str]:
    """Revoke the presented token and issue a fresh pair in its place.

    Rotation is what makes the stored hash worth keeping: a stolen token is
    single-use, and its replay lands on a revoked row instead of a live session.
    """
    stored = await _load_usable_refresh_token(session, token)
    stored.revoked_at = utcnow()
    await session.flush()
    return await issue_token_pair(session, stored.subject_type, stored.subject_id)


async def revoke_refresh_token(session: AsyncSession, token: str) -> None:
    stored = await _load_usable_refresh_token(session, token)
    stored.revoked_at = utcnow()
    await session.flush()
