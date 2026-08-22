"""Release cells held by bookings that were never paid for.

Ticked automatically by the in-process scheduler in `app.main` every
`WORKER_INTERVAL_SECONDS`. Run it by hand only to check it against the live
database out of band:

    ./.venv/Scripts/python.exe -m app.workers.holds
"""

import uuid
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import session_scope, utcnow
from app.modules.booking import service
from app.modules.booking.models import Booking, BookingStatus

RELEASE_REASON = "Бронь не оплачена вовремя"


async def release_expired_holds(
    session: AsyncSession, now: datetime | None = None
) -> list[uuid.UUID]:
    """Cancel unpaid bookings whose reservation has run out.

    Only `pending_payment` is in scope. A paid booking holds its cell because it
    was paid for, not because of a timer, and releasing one would hand a
    stranger a cell somebody is on their way to fill.
    """
    moment = now or utcnow()
    stale = await session.scalars(
        select(Booking).where(
            Booking.status == BookingStatus.PENDING_PAYMENT,
            Booking.hold_expires_at.is_not(None),
            Booking.hold_expires_at <= moment,
        )
    )
    released: list[uuid.UUID] = []
    for booking in stale:
        await service.cancel(session, booking, reason=RELEASE_REASON, actor="system")
        released.append(booking.id)
    if released:
        await session.commit()
    return released


async def run_once() -> int:
    """Entry point for a scheduler or a manual run: opens its own session."""
    async with session_scope() as session:
        return len(await release_expired_holds(session))


if __name__ == "__main__":
    import asyncio

    print(f"released {asyncio.run(run_once())} expired holds")
