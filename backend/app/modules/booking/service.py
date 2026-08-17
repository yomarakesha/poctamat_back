import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.db import utcnow, write_transaction
from app.core.errors import AppError, ErrorCode
from app.modules.booking.codes import issue_codes
from app.modules.booking.models import (
    Booking,
    BookingEvent,
    BookingStatus,
    CELL_HELD_STATUSES,
    CodePurpose,
    Depositor,
)
from app.modules.catalog.service import free_cell_ids

# Written as data rather than as if-branches so the whole machine can be read at
# once and tested exhaustively. Payment moves pending_payment -> paid (Plan 2b);
# the kiosk moves awaiting_deposit -> awaiting_pickup -> completed (Plan 3).
ALLOWED_TRANSITIONS: dict[BookingStatus, frozenset[BookingStatus]] = {
    BookingStatus.PENDING_PAYMENT: frozenset(
        {BookingStatus.PAID, BookingStatus.CANCELLED}
    ),
    BookingStatus.PAID: frozenset(
        {BookingStatus.AWAITING_DEPOSIT, BookingStatus.CANCELLED}
    ),
    BookingStatus.AWAITING_DEPOSIT: frozenset(
        {BookingStatus.AWAITING_PICKUP, BookingStatus.CANCELLED}
    ),
    # No cancellation from here on: the parcel is inside, and freeing the cell
    # would hand someone else a door with a stranger's parcel behind it.
    BookingStatus.AWAITING_PICKUP: frozenset({BookingStatus.COMPLETED}),
    BookingStatus.COMPLETED: frozenset(),
    BookingStatus.CANCELLED: frozenset(),
}


def can_transition(current: BookingStatus, target: BookingStatus) -> bool:
    return target in ALLOWED_TRANSITIONS[current]


def assert_transition(current: BookingStatus, target: BookingStatus) -> None:
    if not can_transition(current, target):
        raise AppError(
            ErrorCode.BOOKING_INVALID_STATE,
            f"A booking in state {current.value} cannot become {target.value}.",
            409, details={"from": current.value, "to": target.value},
        )


def _as_utc(value: datetime) -> datetime:
    # SQLite hands back naive datetimes even for DateTime(timezone=True) columns.
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


async def record_event(
    session: AsyncSession, booking: Booking, status: BookingStatus,
    message: str, details: dict | None = None,
) -> BookingEvent:
    event = BookingEvent(status=status, message=message, details=details)
    booking.events.append(event)
    return event


async def _held_cell_ids(
    session: AsyncSession, postamat_id: uuid.UUID
) -> set[uuid.UUID]:
    rows = await session.scalars(
        select(Booking.cell_id).where(
            Booking.postamat_id == postamat_id,
            Booking.status.in_(tuple(CELL_HELD_STATUSES)),
        )
    )
    return set(rows)


async def create_booking(
    session: AsyncSession,
    *,
    client_id: uuid.UUID,
    postamat_id: uuid.UUID,
    cell_type_id: uuid.UUID,
    duration_hours: int,
    amount_minor: int,
    currency: str,
    recipient_phone: str,
    recipient_name: str | None,
    depositor: Depositor,
    courier_phone: str | None,
) -> tuple[Booking, dict[CodePurpose, str]]:
    """Take one free cell of a size and hold it.

    The allocation runs inside a transaction that took the write lock before
    reading anything, so a second caller cannot see the same cell as free:
    SQLite admits one writer at a time and the second one waits here rather
    than racing. The partial unique index is the backstop if this is ever wrong.
    """
    settings = get_settings()
    # The caller's own lookups have already opened a deferred transaction, and
    # BEGIN IMMEDIATE cannot upgrade one that is already running. Those lookups
    # were reads, so ending their transaction here costs nothing.
    if session.in_transaction():
        await session.commit()

    async with write_transaction(session):
        taken = await _held_cell_ids(session, postamat_id)
        free = await free_cell_ids(session, postamat_id, cell_type_id, taken)
        if not free:
            raise AppError(
                ErrorCode.SIZE_SOLD_OUT,
                "No free cell of this size at this postamat.", 409,
                details={"cell_type_id": str(cell_type_id)},
            )

        booking = Booking(
            client_id=client_id, postamat_id=postamat_id, cell_id=free[0],
            cell_type_id=cell_type_id, duration_hours=duration_hours,
            amount_minor=amount_minor, currency=currency,
            status=BookingStatus.PENDING_PAYMENT, depositor=depositor,
            courier_phone=courier_phone, recipient_phone=recipient_phone,
            recipient_name=recipient_name,
            hold_expires_at=utcnow() + timedelta(minutes=settings.hold_minutes),
        )
        plaintext = issue_codes(booking)
        session.add(booking)
        await record_event(session, booking, BookingStatus.PENDING_PAYMENT,
                           "Забронировано")
        await session.flush()

    return booking, plaintext
