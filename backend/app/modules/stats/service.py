"""Read-only aggregates for the operator's dashboard.

Computed live rather than materialised. A fleet of a few cabinets and a few
thousand bookings does not need a rollup table, and a stale tile is worse than a
slow one — the point of these numbers is that somebody acts on them today.

Every helper here is one query. Six screens' worth of tiles must not become
sixty round trips.
"""

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import utcnow
from app.modules.booking.models import Booking, BookingStatus
from app.modules.catalog.models import Cell, Device, DeviceStatus, Postamat
from app.modules.payments.models import Payment, PaymentStatus

# Peak hours are read by a person in Ashgabat, so they are bucketed in that
# person's clock. The country keeps one offset all year, which is why a fixed
# offset is honest here and a timezone database would add nothing.
ASHGABAT = timezone(timedelta(hours=5))
ASHGABAT_NAME = "Asia/Ashgabat"

PERIODS = {"7d": 7, "30d": 30, "90d": 90}

# Cells these bookings are sitting in are not for sale, and the tiles count them
# apart: `booked` is paid-for and empty, `occupied` has a parcel behind the door.
BOOKED_STATUSES = (BookingStatus.PENDING_PAYMENT, BookingStatus.AWAITING_DEPOSIT)
OCCUPIED_STATUSES = (
    BookingStatus.AWAITING_PICKUP, BookingStatus.EXPIRED, BookingStatus.GRACE,
    BookingStatus.OVERDUE, BookingStatus.TO_REMOVE,
)


@dataclass
class Window:
    start: datetime
    end: datetime


def window(period: str, since: datetime | None = None,
           until: datetime | None = None) -> Window:
    end = until or utcnow()
    if period == "custom" and since is not None:
        return Window(start=since, end=end)
    return Window(start=end - timedelta(days=PERIODS.get(period, 30)), end=end)


def _scoped(stmt: Select, postamat_id: uuid.UUID | None) -> Select:
    return stmt.where(Booking.postamat_id == postamat_id) if postamat_id else stmt


async def overview(session: AsyncSession) -> dict:
    postamats = await session.scalar(select(func.count()).select_from(Postamat)) or 0
    cells = await session.scalar(select(func.count()).select_from(Cell)) or 0
    blocked = await session.scalar(
        select(func.count()).select_from(Cell).where(Cell.is_blocked.is_(True))
    ) or 0
    maintenance = await session.scalar(
        select(func.count()).select_from(Cell).where(Cell.is_maintenance.is_(True))
    ) or 0
    booked = await session.scalar(
        select(func.count()).select_from(Booking)
        .where(Booking.status.in_(BOOKED_STATUSES))
    ) or 0
    occupied = await session.scalar(
        select(func.count()).select_from(Booking)
        .where(Booking.status.in_(OCCUPIED_STATUSES))
    ) or 0
    return {
        "postamat_count": postamats, "cell_count": cells,
        "occupied_count": occupied, "booked_count": booked,
        "blocked_count": blocked, "maintenance_count": maintenance,
    }


async def attention(session: AsyncSession) -> dict:
    overdue = await session.scalar(
        select(func.count()).select_from(Booking)
        .where(Booking.status == BookingStatus.OVERDUE)
    ) or 0
    to_remove = await session.scalar(
        select(func.count()).select_from(Booking)
        .where(Booking.status == BookingStatus.TO_REMOVE)
    ) or 0
    offline = await session.scalar(
        select(func.count()).select_from(Device)
        .where(Device.status != DeviceStatus.ONLINE)
    ) or 0
    blocked_cells = await session.scalar(
        select(func.count()).select_from(Cell).where(Cell.is_blocked.is_(True))
    ) or 0
    # Failed commands live in the lock agent, which is Plan 3. Zero is the true
    # answer today; the field exists because the panel draws the tile either way.
    return {
        "overdue_bookings": overdue, "to_remove_bookings": to_remove,
        "offline_devices": offline, "failed_commands": 0,
        "blocked_cells": blocked_cells,
    }


async def bookings_over_time(
    session: AsyncSession, frame: Window, postamat_id: uuid.UUID | None = None
) -> list[dict]:
    day = func.date(Booking.created_at)
    stmt = _scoped(
        select(day, func.count()).where(
            Booking.created_at >= frame.start, Booking.created_at <= frame.end
        ),
        postamat_id,
    ).group_by(day).order_by(day)
    rows = await session.execute(stmt)
    return [{"date": str(date), "count": count} for date, count in rows]


async def revenue(
    session: AsyncSession, frame: Window, postamat_id: uuid.UUID | None = None
) -> dict:
    day = func.date(Payment.settled_at)
    stmt = (
        select(day, func.sum(Payment.amount_minor), func.count())
        # Settled only: a pending session is not money, and counting it would
        # make the revenue chart disagree with the bank.
        .where(
            Payment.status == PaymentStatus.SUCCEEDED,
            Payment.settled_at >= frame.start, Payment.settled_at <= frame.end,
        )
        .group_by(day).order_by(day)
    )
    if postamat_id:
        stmt = stmt.join(Booking, Booking.id == Payment.booking_id).where(
            Booking.postamat_id == postamat_id
        )

    points = []
    total = 0
    for date, amount, count in await session.execute(stmt):
        total += amount or 0
        points.append({
            "date": str(date),
            "amount": {"amount_minor": amount or 0, "currency": "TMT"},
            "transaction_count": count,
        })
    return {"total": {"amount_minor": total, "currency": "TMT"}, "points": points}


async def utilization(
    session: AsyncSession, frame: Window, postamat_id: uuid.UUID | None = None
) -> dict:
    """How busy the fleet was, per postamat and overall.

    Utilisation is bookings in the window against cells available to take them.
    Uses per cell is the same figure unnormalised, which is what an operator
    comparing two cabinets actually wants to see.
    """
    cells_stmt = select(Cell.postamat_id, func.count()).group_by(Cell.postamat_id)
    if postamat_id:
        cells_stmt = cells_stmt.where(Cell.postamat_id == postamat_id)
    cells = {row_id: count for row_id, count in await session.execute(cells_stmt)}

    uses_stmt = _scoped(
        select(Booking.postamat_id, func.count()).where(
            Booking.created_at >= frame.start, Booking.created_at <= frame.end
        ),
        postamat_id,
    ).group_by(Booking.postamat_id)
    uses = {row_id: count for row_id, count in await session.execute(uses_stmt)}

    names_stmt = select(Postamat.id, Postamat.name)
    if postamat_id:
        names_stmt = names_stmt.where(Postamat.id == postamat_id)
    names = {row_id: name for row_id, name in await session.execute(names_stmt)}

    days = max((frame.end - frame.start).days, 1)
    by_postamat = []
    for row_id, name in names.items():
        cell_count = cells.get(row_id, 0)
        used = uses.get(row_id, 0)
        per_cell = used / cell_count if cell_count else 0.0
        by_postamat.append({
            "postamat_id": row_id, "name": name,
            # One booking per cell per day is taken as full: the cabinet cannot
            # sell the same drawer twice in a day and be honest about it.
            "utilization_pct": round(min(per_cell / days, 1.0) * 100, 2),
            "uses_per_cell": round(per_cell, 2),
        })

    total_cells = sum(cells.values())
    total_uses = sum(uses.values())
    fleet_per_cell = total_uses / total_cells if total_cells else 0.0
    return {
        "utilization_pct": round(min(fleet_per_cell / days, 1.0) * 100, 2),
        "uses_per_cell": round(fleet_per_cell, 2),
        "by_postamat": by_postamat,
    }


async def peak_hours(
    session: AsyncSession, frame: Window, postamat_id: uuid.UUID | None = None
) -> dict:
    """Counts per weekday and hour, bucketed in Ashgabat's clock.

    Bucketed in Python rather than in SQL: SQLite has no timezone arithmetic,
    and a chart drawn in UTC would tell the reader about five o'clock somewhere
    else.
    """
    stmt = _scoped(
        select(Booking.created_at).where(
            Booking.created_at >= frame.start, Booking.created_at <= frame.end
        ),
        postamat_id,
    )
    counts: dict[tuple[int, int], int] = {}
    for (moment,) in await session.execute(stmt):
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        local = moment.astimezone(ASHGABAT)
        key = (local.isoweekday(), local.hour)
        counts[key] = counts.get(key, 0) + 1

    return {
        "timezone": ASHGABAT_NAME,
        "buckets": [
            {"weekday": weekday, "hour": hour, "count": count}
            for (weekday, hour), count in sorted(counts.items())
        ],
    }
