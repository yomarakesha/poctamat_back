import secrets
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
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


def issue_codes(booking: Booking) -> dict[CodePurpose, str]:
    """Create one code per purpose and return the plaintext to show once."""
    length = get_settings().pin_length
    plaintext: dict[CodePurpose, str] = {}
    for purpose in CodePurpose:
        code = _digits(length)
        plaintext[purpose] = code
        booking.codes.append(AccessCode(purpose=purpose, code_hash=hash_pin(code)))
    return plaintext


def reissue_code(booking: Booking, purpose: CodePurpose) -> str:
    """Replace one code — the courier's PIN, when the SMS never arrived.

    The old digest is overwritten rather than left beside the new one, so a code
    that went to the wrong number stops working the moment it is resent.
    """
    code = _digits(get_settings().pin_length)
    for existing in booking.codes:
        if existing.purpose == purpose:
            existing.code_hash = hash_pin(code)
            existing.attempts = 0
            existing.used_at = None
            return code
    booking.codes.append(AccessCode(purpose=purpose, code_hash=hash_pin(code)))
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
