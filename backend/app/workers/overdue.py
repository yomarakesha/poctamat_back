"""Walk parcels through the overdue stages.

Ticked automatically by the in-process scheduler in `app.main` every
`WORKER_INTERVAL_SECONDS`. Run it by hand only to check it against the live
database out of band:

    ./.venv/Scripts/python.exe -m app.workers.overdue

Nothing here charges for being late — the product deliberately has no storage
fees. The stages exist so staff know when a cell may be emptied, and so the
recipient hears about it before that happens.
"""

import uuid
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.db import session_scope, utcnow
from app.modules.booking import service as booking_service
from app.modules.booking.models import Booking, BookingStatus
from app.modules.catalog.models import Postamat
from app.modules.catalog.service import cell_numbers, is_open_at, next_opening_after
from app.modules.notify.models import (
    Notification,
    NotificationChannel,
    NotificationKind,
    NotificationSettings,
)
from app.modules.notify.service import deliver_pending, notify

WATCHED = (
    BookingStatus.AWAITING_PICKUP,
    BookingStatus.EXPIRED,
    BookingStatus.GRACE,
    BookingStatus.OVERDUE,
)


async def _postamats(
    session: AsyncSession, ids: set[uuid.UUID]
) -> dict[uuid.UUID, Postamat]:
    if not ids:
        return {}
    rows = await session.scalars(select(Postamat).where(Postamat.id.in_(list(ids))))
    return {row.id: row for row in rows}


async def _push_expiring_enabled(
    session: AsyncSession, ids: set[uuid.UUID]
) -> dict[uuid.UUID, bool]:
    """A client who never opened the settings screen has no row, and the
    column defaults to on — so a missing row reads the same as one that says
    yes, rather than silently opting a client out of a reminder they never
    declined.
    """
    if not ids:
        return {}
    rows = await session.scalars(
        select(NotificationSettings).where(NotificationSettings.client_id.in_(list(ids)))
    )
    return {row.client_id: row.push_expiring_soon for row in rows}


def _reachable(postamat: Postamat | None, moment: datetime) -> datetime:
    """Push a deadline to the next opening if it lands behind a locked door.

    A parcel whose storage ends at 21:00 at a site that closed at 20:00 would
    otherwise burn its whole grace period overnight, and the recipient would
    find it overdue before they could physically reach it.
    """
    if postamat is None or is_open_at(postamat, moment):
        return moment
    opening = next_opening_after(postamat, moment)
    return moment if opening is None else opening


async def run_escalation(
    session: AsyncSession, now: datetime | None = None
) -> dict[str, int]:
    settings = get_settings()
    moment = now or utcnow()
    counts = {"reminded": 0, "expired": 0, "grace": 0, "overdue": 0, "to_remove": 0}
    pending_pushes: list[Notification] = []

    live = list(await session.scalars(
        select(Booking).where(
            Booking.status.in_(WATCHED), Booking.expires_at.is_not(None)
        )
    ))
    if not live:
        return counts

    postamats = await _postamats(session, {row.postamat_id for row in live})
    numbers = await cell_numbers(session, [row.cell_id for row in live])
    push_expiring = await _push_expiring_enabled(
        session, {row.client_id for row in live if row.client_id}
    )

    for booking in live:
        expires_at = booking_service.as_utc(booking.expires_at)
        postamat = postamats.get(booking.postamat_id)
        cell_number = numbers.get(booking.cell_id, 0)
        language = "ru"

        if booking.status == BookingStatus.AWAITING_PICKUP:
            if moment >= expires_at:
                await booking_service.expire(session, booking)
                await notify(
                    session, client_id=booking.client_id,
                    phone=booking.recipient_phone,
                    kind=NotificationKind.BOOKING_EXPIRED, language=language,
                    channel=NotificationChannel.IN_APP, booking_id=booking.id,
                    cell_number=cell_number,
                )
                counts["expired"] += 1
            elif (
                booking.reminded_at is None
                and moment >= expires_at - timedelta(
                    hours=settings.reminder_hours_before
                )
            ):
                # Stamped so a worker running every few minutes sends this once
                # rather than every few minutes.
                booking.reminded_at = moment
                await notify(
                    session, client_id=booking.client_id,
                    phone=booking.recipient_phone,
                    kind=NotificationKind.BOOKING_EXPIRING, language=language,
                    channel=NotificationChannel.IN_APP, booking_id=booking.id,
                    cell_number=cell_number,
                    hours=settings.reminder_hours_before,
                )
                if booking.client_id and push_expiring.get(booking.client_id, True):
                    # Recorded now, sent after the commit below: see
                    # `deliver_pending`.
                    pending_pushes.append(await notify(
                        session, client_id=booking.client_id, phone=None,
                        kind=NotificationKind.BOOKING_EXPIRING, language=language,
                        channel=NotificationChannel.PUSH, booking_id=booking.id,
                        cell_number=cell_number,
                        hours=settings.reminder_hours_before, deliver=False,
                    ))
                counts["reminded"] += 1

        elif booking.status == BookingStatus.EXPIRED:
            grace_ends = _reachable(
                postamat, expires_at + timedelta(hours=settings.grace_hours)
            )
            if moment >= grace_ends:
                await booking_service.to_grace(session, booking)
                counts["grace"] += 1

        elif booking.status == BookingStatus.GRACE:
            overdue_at = _reachable(
                postamat, expires_at + timedelta(hours=settings.grace_hours * 2)
            )
            if moment >= overdue_at:
                await booking_service.to_overdue(session, booking)
                await notify(
                    session, client_id=booking.client_id,
                    phone=booking.recipient_phone,
                    kind=NotificationKind.BOOKING_OVERDUE, language=language,
                    channel=NotificationChannel.IN_APP, booking_id=booking.id,
                    cell_number=cell_number,
                )
                counts["overdue"] += 1

        elif booking.status == BookingStatus.OVERDUE:
            # `to_remove` is a status rather than a derived flag because the work
            # queue has to tell "staff have been told to pull this" apart from
            # "merely late", and staff act on the first only.
            remove_after = booking.remove_after
            if remove_after is not None and moment >= booking_service.as_utc(
                remove_after
            ):
                await booking_service.to_remove(session, booking)
                counts["to_remove"] += 1

    await session.commit()

    # The reminders the loop recorded go out only now, with the write lock
    # released and `reminded_at` durable: a push sent before that commit is
    # one the customer gets twice if the commit fails.
    if pending_pushes:
        await deliver_pending(session, pending_pushes)
        await session.commit()
    return counts


async def run_once() -> dict[str, int]:
    """Entry point for a scheduler or a manual run: opens its own session."""
    async with session_scope() as session:
        return await run_escalation(session)


if __name__ == "__main__":
    import asyncio

    print(asyncio.run(run_once()))
