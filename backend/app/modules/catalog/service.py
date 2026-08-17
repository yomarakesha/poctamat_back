import uuid
from collections import defaultdict
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.catalog.models import Cell, Postamat, PostamatSchedule

# Every slot is validated as opens_at < closes_at before it reaches the
# database (see `ScheduleSlotIn`), so nothing here has to reason about a window
# that runs past midnight. A machine that serves through the night is
# `round_the_clock`, which is the only form of that the product has.


def _slot_for(postamat: Postamat, weekday: int) -> PostamatSchedule | None:
    for slot in postamat.schedule:
        if slot.weekday == weekday:
            return slot
    return None


def is_open_at(postamat: Postamat, moment: datetime) -> bool:
    if postamat.round_the_clock:
        return True
    slot = _slot_for(postamat, moment.weekday())
    if slot is None:
        return False
    # Half-open: the opening minute counts as open, the closing minute does not.
    return slot.opens_at <= moment.time() < slot.closes_at


def next_opening_after(postamat: Postamat, moment: datetime) -> datetime | None:
    """The first instant at or after `moment` when the postamat is open.

    Returns None when it never opens again on the weekly cycle — no schedule at
    all — because handing back `moment` would read as "open now" to the caller.
    """
    if postamat.round_the_clock:
        return moment
    # Eight days rather than seven: today's own slot may already have passed, so
    # the same weekday has to be reachable a second time.
    for offset in range(8):
        day = moment + timedelta(days=offset)
        slot = _slot_for(postamat, day.weekday())
        if slot is None:
            continue
        candidate = day.replace(
            hour=slot.opens_at.hour, minute=slot.opens_at.minute,
            second=0, microsecond=0,
        )
        if candidate > moment:
            return candidate
    return None


def _usable_cells(postamat_id: uuid.UUID):
    return (
        select(Cell)
        .where(
            Cell.postamat_id == postamat_id,
            Cell.is_blocked.is_(False),
            Cell.is_maintenance.is_(False),
        )
        .order_by(Cell.number)
    )


async def cell_ids_by_type(
    session: AsyncSession, postamat_id: uuid.UUID
) -> dict[uuid.UUID, list[uuid.UUID]]:
    """Usable cells at one postamat, grouped by type, in door order."""
    pools: dict[uuid.UUID, list[uuid.UUID]] = defaultdict(list)
    for cell in await session.scalars(_usable_cells(postamat_id)):
        pools[cell.cell_type_id].append(cell.id)
    return dict(pools)


async def free_cell_ids(
    session: AsyncSession,
    postamat_id: uuid.UUID,
    cell_type_id: uuid.UUID,
    taken: set[uuid.UUID],
) -> list[uuid.UUID]:
    """Cells of one type that no caller-supplied booking is holding.

    `taken` is passed in rather than joined here because bookings belong to
    another module, and catalog importing them is the coupling the module rule
    forbids. On Postgres this is the function that would become
    `SELECT ... FOR UPDATE SKIP LOCKED`; its callers would not change.
    """
    stmt = _usable_cells(postamat_id).where(Cell.cell_type_id == cell_type_id)
    return [cell.id for cell in await session.scalars(stmt) if cell.id not in taken]


def storage_expiry(
    postamat: Postamat, deposited_at: datetime, duration_hours: int
) -> datetime:
    """When storage ends, never before the recipient could physically arrive.

    A 12-hour rental deposited at 19:00 at a site closing at 20:00 would expire
    at 07:00, an hour before the doors open. The expiry is pushed to the next
    opening instead — the customer paid for reachable storage, not for hours
    behind a locked door. A site with no schedule at all has no next opening to
    push to, so the plain clock stands.
    """
    plain = deposited_at + timedelta(hours=duration_hours)
    if is_open_at(postamat, plain):
        return plain
    opening = next_opening_after(postamat, plain)
    return plain if opening is None else opening
