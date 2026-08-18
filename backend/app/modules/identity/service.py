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
from app.modules.identity.models import AdminUser, Client, PushToken, RefreshToken
from app.modules.notify.models import NotificationChannel, NotificationKind
from app.modules.notify.service import notify


def _otp_key(request_id: str) -> str:
    return f"otp:req:{request_id}"


def _resend_key(phone: str) -> str:
    return f"otp:phone:{phone}"


def _as_utc(value: datetime) -> datetime:
    # SQLite hands back naive datetimes even for DateTime(timezone=True) columns,
    # so normalise before comparing against utcnow().
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


async def issue_otp(
    session: AsyncSession, phone: str, language: str | None = None
) -> tuple[str, datetime, int]:
    """Send a login code and return `(request_id, expires_at, resend_after)`.

    The code is filed under a request id rather than under the phone, because
    verification quotes that id back: two devices asking for a code on the same
    number then hold two independent attempts counters instead of overwriting
    one another's.
    """
    settings = get_settings()
    store = get_kvstore()

    blocked = await session.scalar(
        select(Client).where(Client.phone == phone, Client.is_blocked.is_(True))
    )
    if blocked is not None:
        raise AppError(ErrorCode.CLIENT_BLOCKED, "Client is blocked.", 403)

    live = await store.get(_resend_key(phone))
    if live:
        raise AppError(
            ErrorCode.OTP_REQUEST_TOO_SOON, "Wait before requesting a new code.", 429,
            details={"resend_after": settings.otp_resend_seconds},
        )

    request_id = str(uuid.uuid4())
    code = "".join(secrets.choice("0123456789") for _ in range(settings.otp_length))
    await store.put(
        _otp_key(request_id), {"code": code, "attempts": "0", "phone": phone},
        settings.otp_ttl_seconds,
    )
    await store.put(
        _resend_key(phone), {"request_id": request_id}, settings.otp_resend_seconds
    )
    # The client may not exist yet — this is also the registration path — so the
    # language comes from the request rather than from a stored profile.
    await notify(
        session, client_id=None, phone=phone, kind=NotificationKind.OTP,
        language=language or settings.default_language,
        channel=NotificationChannel.SMS, code=code,
    )
    await session.commit()
    return (
        request_id,
        utcnow() + timedelta(seconds=settings.otp_ttl_seconds),
        settings.otp_resend_seconds,
    )


async def peek_otp(request_id: str) -> str | None:
    """Read the live code. For tests and the smoke script — no route exposes it."""
    stored = await get_kvstore().get(_otp_key(request_id))
    return stored["code"] if stored else None


async def verify_otp(request_id: str, code: str) -> str:
    """Check a code and return the phone it was issued for."""
    settings = get_settings()
    store = get_kvstore()
    stored = await store.get(_otp_key(request_id))
    if not stored:
        # One code for both "never existed" and "expired": a caller guessing
        # request ids learns nothing from the difference.
        raise AppError(ErrorCode.OTP_NOT_FOUND, "No active code for this request.", 401)

    attempts = int(stored["attempts"]) + 1
    if attempts >= settings.otp_max_attempts:
        await store.delete(_otp_key(request_id))
        raise AppError(ErrorCode.OTP_ATTEMPTS_EXCEEDED, "Too many attempts.", 429)

    if not secrets.compare_digest(stored["code"], code):
        await store.set_field(_otp_key(request_id), "attempts", str(attempts))
        raise AppError(
            ErrorCode.OTP_INVALID, "Wrong code.", 401,
            details={"attempts_left": settings.otp_max_attempts - attempts},
        )

    await store.delete(_otp_key(request_id))
    return stored["phone"]


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


async def _load_usable_refresh_token(
    session: AsyncSession, token: str, subject_type: str
) -> RefreshToken:
    stored = await session.scalar(
        select(RefreshToken).where(RefreshToken.token_hash == hash_token(token))
    )
    # Unknown, already rotated, expired and belonging to the other kind of subject
    # all answer the same way: distinguishing them would tell a caller something
    # about tokens it does not hold.
    if (
        stored is None
        or stored.subject_type != subject_type
        or stored.revoked_at is not None
        or _as_utc(stored.expires_at) <= utcnow()
    ):
        raise AppError(
            ErrorCode.REFRESH_TOKEN_INVALID, "Refresh token is not usable.", 401
        )
    return stored


async def _assert_subject_usable(
    session: AsyncSession, subject_type: str, subject_id: uuid.UUID
) -> None:
    """Refuse to extend a session whose owner has since lost access.

    The access token is minutes old but the refresh token lives for days, so
    without this an administrator blocking a client, or deactivating a colleague,
    would not take effect until that refresh token finally expired.
    """
    if subject_type == "client":
        client = await session.get(Client, subject_id)
        if client is None or client.is_blocked:
            raise AppError(ErrorCode.CLIENT_BLOCKED, "Client is blocked.", 403)
    elif subject_type == "admin":
        admin = await session.get(AdminUser, subject_id)
        if admin is None or not admin.is_active:
            raise AppError(ErrorCode.ADMIN_INACTIVE, "Account is inactive.", 403)


async def rotate_refresh_token(
    session: AsyncSession, token: str, subject_type: str
) -> tuple[str, str]:
    """Revoke the presented token and issue a fresh pair in its place.

    Rotation is what makes the stored hash worth keeping: a stolen token is
    single-use, and its replay lands on a revoked row instead of a live session.
    """
    stored = await _load_usable_refresh_token(session, token, subject_type)
    await _assert_subject_usable(session, stored.subject_type, stored.subject_id)
    stored.revoked_at = utcnow()
    await session.flush()
    return await issue_token_pair(session, stored.subject_type, stored.subject_id)


async def revoke_refresh_token(
    session: AsyncSession, token: str, subject_type: str
) -> None:
    # No subject check here on purpose: logging out is giving up access, and a
    # blocked client must still be able to tear its own session down.
    stored = await _load_usable_refresh_token(session, token, subject_type)
    stored.revoked_at = utcnow()
    await session.flush()


async def revoke_client_session(
    session: AsyncSession, client_id: uuid.UUID, refresh_token: str | None
) -> None:
    """Tear down the caller's session.

    The contract's logout carries no body: it revokes "the current session",
    identified by the access token the request already carries. A client that
    still knows its refresh token may name it, and then only that one dies —
    logging out on the phone must not sign the tablet out too. Without one there
    is nothing to single out, so every live token for this client goes.
    """
    stmt = select(RefreshToken).where(
        RefreshToken.subject_type == "client",
        RefreshToken.subject_id == client_id,
        RefreshToken.revoked_at.is_(None),
    )
    if refresh_token is not None:
        stmt = stmt.where(RefreshToken.token_hash == hash_token(refresh_token))

    now = utcnow()
    for stored in await session.scalars(stmt):
        stored.revoked_at = now
    await session.flush()


async def register_push_token(
    session: AsyncSession,
    client_id: uuid.UUID,
    token: str,
    platform: str,
    app_version: str | None,
) -> PushToken:
    """Store a device token, or hand back the row that already holds it.

    The platform reissues these on its own schedule and the app re-registers on
    every launch, so the same value arriving twice is the normal case, not a
    conflict. A previously revoked row comes back to life rather than leaving a
    duplicate behind.
    """
    existing = await session.scalar(
        select(PushToken).where(
            PushToken.client_id == client_id, PushToken.token == token
        )
    )
    if existing is not None:
        existing.revoked_at = None
        existing.platform = platform
        existing.app_version = app_version
        await session.flush()
        return existing

    created = PushToken(
        client_id=client_id, token=token, platform=platform, app_version=app_version
    )
    session.add(created)
    await session.flush()
    return created


async def revoke_push_token(
    session: AsyncSession, client_id: uuid.UUID, push_token_id: uuid.UUID
) -> None:
    row = await session.get(PushToken, push_token_id)
    # Someone else's token is not found rather than forbidden: whether a given
    # id exists is not a fact this caller gets to confirm.
    if row is None or row.client_id != client_id or row.revoked_at is not None:
        raise AppError(ErrorCode.NOT_FOUND, "Push token not found.", 404)
    row.revoked_at = utcnow()
    await session.flush()


async def revoke_push_token_value(
    session: AsyncSession, client_id: uuid.UUID, token: str
) -> None:
    """Revoke by the device token itself, which is what logout knows."""
    row = await session.scalar(
        select(PushToken).where(
            PushToken.client_id == client_id,
            PushToken.token == token,
            PushToken.revoked_at.is_(None),
        )
    )
    # Silence on a miss: logging out is not the place to tell a caller which of
    # its tokens the server still holds.
    if row is not None:
        row.revoked_at = utcnow()
        await session.flush()
