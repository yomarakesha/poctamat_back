import secrets
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.db import utcnow
from app.core.security import hash_pin
from app.modules.booking.models import (
    AccessCode,
    Booking,
    CELL_HELD_STATUSES,
    CodePurpose,
)


def _digits(length: int) -> str:
    # secrets, not random: these open a physical door.
    return "".join(secrets.choice("0123456789") for _ in range(length))


def code_expiry(booking: Booking, purpose: CodePurpose) -> datetime:
    """When this grant stops opening the door.

    The deposit code has a window of its own: it opens an empty cell, so it need
    not outlive the trip to the postamat. The pickup code lives as long as the
    parcel is collectable, which is past `expires_at` — nothing is charged for
    being late and staff remove the parcel only afterwards.
    """
    settings = get_settings()
    if purpose == CodePurpose.DEPOSIT:
        return utcnow() + timedelta(hours=settings.deposit_code_hours)
    if booking.expires_at is None:
        return utcnow() + timedelta(hours=booking.duration_hours)
    stored = booking.expires_at
    if stored.tzinfo is None:
        stored = stored.replace(tzinfo=timezone.utc)
    return stored + timedelta(hours=settings.removal_after_hours)


def issue_code(booking: Booking, purpose: CodePurpose) -> str:
    """Issue the grant for one purpose, replacing whatever it had before.

    The old digest is overwritten rather than left beside the new one, so a code
    that went to the wrong number stops working the moment a fresh one is sent.
    Codes are issued when they become usable, not all at once: the deposit code
    at booking, the pickup code when the parcel is actually inside.
    """
    code = _digits(get_settings().pin_length)
    expires_at = code_expiry(booking, purpose)
    for existing in booking.codes:
        if existing.purpose == purpose:
            existing.code_hash = hash_pin(code)
            existing.attempts = 0
            existing.used_at = None
            existing.expires_at = expires_at
            return code
    booking.codes.append(
        AccessCode(purpose=purpose, code_hash=hash_pin(code), expires_at=expires_at)
    )
    return code


async def find_code(
    session: AsyncSession, postamat_id: uuid.UUID, plaintext: str
) -> AccessCode | None:
    """Look up an unused code at one postamat by its digest.

    Scoped to the postamat because five digits repeat across a fleet: the same
    PIN is very likely live at another cabinet, and opening the wrong door is
    not a recoverable mistake.
    """
    stmt = (
        select(AccessCode)
        .join(Booking, Booking.id == AccessCode.booking_id)
        .where(
            AccessCode.code_hash == hash_pin(plaintext),
            AccessCode.used_at.is_(None),
            Booking.postamat_id == postamat_id,
            Booking.status.in_(tuple(CELL_HELD_STATUSES)),
        )
    )
    return await session.scalar(stmt)
