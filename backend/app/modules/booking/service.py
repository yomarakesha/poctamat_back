import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import inspect, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.db import utcnow, write_transaction
from app.core.errors import AppError, ErrorCode
from app.modules.booking.codes import issue_code
from app.modules.booking.models import (
    Booking,
    BookingEvent,
    BookingStatus,
    CELL_HELD_STATUSES,
    CodePurpose,
    Depositor,
)
from app.modules.catalog.service import free_cell_ids, storage_expiry

# Written as data rather than as if-branches so the whole machine can be read at
# once and tested exhaustively. Payment moves pending_payment -> paid (Plan 2b);
# the kiosk moves awaiting_deposit -> awaiting_pickup -> completed (Plan 3).
ALLOWED_TRANSITIONS: dict[BookingStatus, frozenset[BookingStatus]] = {
    # Payment goes straight to awaiting_deposit. There is no `paid` state: the
    # front-end contract does not define one, and a generated client throws on a
    # status it has never heard of. Payment is a timeline step instead.
    BookingStatus.PENDING_PAYMENT: frozenset({
        BookingStatus.AWAITING_DEPOSIT,
        BookingStatus.CANCELLED,
        BookingStatus.PAYMENT_FAILED,
    }),
    BookingStatus.AWAITING_DEPOSIT: frozenset(
        {BookingStatus.AWAITING_PICKUP, BookingStatus.CANCELLED}
    ),
    # No cancellation from here on: the parcel is inside, and freeing the cell
    # would hand someone else a door with a stranger's parcel behind it.
    BookingStatus.AWAITING_PICKUP: frozenset(
        {BookingStatus.COMPLETED, BookingStatus.EXPIRED}
    ),
    # Collection stays reachable from every overdue stage: nothing is charged
    # for being late, so the recipient can still take their parcel right up
    # until staff physically remove it.
    BookingStatus.EXPIRED: frozenset({BookingStatus.COMPLETED, BookingStatus.GRACE}),
    BookingStatus.GRACE: frozenset({BookingStatus.COMPLETED, BookingStatus.OVERDUE}),
    BookingStatus.OVERDUE: frozenset(
        {BookingStatus.COMPLETED, BookingStatus.TO_REMOVE}
    ),
    # Staff have been told to go and pull it; the queue tells this apart from
    # `overdue`, which is merely late.
    BookingStatus.TO_REMOVE: frozenset(
        {BookingStatus.COMPLETED, BookingStatus.REMOVED}
    ),
    BookingStatus.REMOVED: frozenset({BookingStatus.CLOSED}),
    BookingStatus.CLOSED: frozenset(),
    BookingStatus.COMPLETED: frozenset(),
    BookingStatus.CANCELLED: frozenset(),
    BookingStatus.PAYMENT_FAILED: frozenset(),
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


def as_utc(value: datetime) -> datetime:
    """Give a stored datetime its timezone back.

    SQLite returns naive values even for DateTime(timezone=True) columns, and
    comparing one against an aware `utcnow()` raises. Public because the overdue
    worker reads the same columns.
    """
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


async def record_event(
    session: AsyncSession, booking: Booking, status: BookingStatus,
    message: str, details: dict | None = None, step: str | None = None,
) -> BookingEvent:
    state = inspect(booking)
    # Appending to an unloaded collection makes SQLAlchemy load it first, and a
    # lazy load inside async code raises MissingGreenlet instead of loading. The
    # collection is loaded explicitly here so the response also sees the event
    # that is being written, not a timeline one entry out of date.
    if state.persistent and "events" in state.unloaded:
        await session.refresh(booking, ["events"])
    event = BookingEvent(seq=len(booking.events), status=status, message=message,
                         details=details, step=step)
    booking.events.append(event)
    return event


async def _ensure_codes(session: AsyncSession, booking: Booking) -> None:
    """Load the grants before touching them.

    Same trap as `events`: appending to an unloaded collection makes SQLAlchemy
    load it first, and a lazy load inside async code raises MissingGreenlet
    instead of loading.
    """
    state = inspect(booking)
    if state.persistent and "codes" in state.unloaded:
        await session.refresh(booking, ["codes"])


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
) -> tuple[Booking, str]:
    """Take one free cell of a size and hold it.

    The allocation runs inside a transaction that took the write lock before
    reading anything, so a second caller cannot see the same cell as free:
    SQLite admits one writer at a time and the second one waits here rather
    than racing. The partial unique index is the backstop if this is ever wrong.
    """
    settings = get_settings()
    # Ending the caller's transaction below is safe for reads and destructive for
    # writes, so unflushed work is a programming error rather than something to
    # commit on the caller's behalf.
    if session.new or session.dirty or session.deleted:
        raise RuntimeError(
            "create_booking needs a session with no pending changes; "
            "commit or roll back before allocating a cell."
        )
    # The caller's own lookups have already opened a deferred transaction, and
    # BEGIN IMMEDIATE cannot upgrade one that is already running. Those lookups
    # were reads, so ending their transaction here costs nothing.
    if session.in_transaction():
        await session.commit()

    booking: Booking | None = None
    deposit_code = ""
    try:
        async with write_transaction(session):
            taken = await _held_cell_ids(session, postamat_id)
            free = await free_cell_ids(session, postamat_id, cell_type_id, taken)
            # Sold out is an answer, not a failed write: the transaction is
            # closed normally and the refusal raised outside it. Rolling back
            # instead would expire every object in the session for a request
            # that wrote nothing.
            if free:
                booking = Booking(
                    client_id=client_id, postamat_id=postamat_id, cell_id=free[0],
                    cell_type_id=cell_type_id, duration_hours=duration_hours,
                    amount_minor=amount_minor, currency=currency,
                    status=BookingStatus.PENDING_PAYMENT, depositor=depositor,
                    courier_phone=courier_phone, recipient_phone=recipient_phone,
                    recipient_name=recipient_name,
                    hold_expires_at=(
                        utcnow() + timedelta(minutes=settings.hold_minutes)
                    ),
                )
                deposit_code = issue_code(booking, CodePurpose.DEPOSIT)
                session.add(booking)
                await record_event(session, booking, BookingStatus.PENDING_PAYMENT,
                                   "Забронировано", step="booked")
                await session.flush()
    except IntegrityError:
        # uq_active_booking_per_cell refused the row, so the cell was held by a
        # booking this transaction could not see. That is the database catching
        # what application code missed, and the customer is owed the same answer
        # as an ordinary sold-out: their size is gone, not "something broke".
        booking = None

    if booking is None:
        raise AppError(
            ErrorCode.NO_FREE_CELLS,
            "No free cell of this size at this postamat.", 409,
            details={"cell_type_id": str(cell_type_id)},
        )
    return booking, deposit_code


async def _move(
    session: AsyncSession, booking: Booking, target: BookingStatus, message: str,
    details: dict | None = None, step: str | None = None,
) -> Booking:
    assert_transition(booking.status, target)
    booking.status = target
    await record_event(session, booking, target, message, details, step=step)
    return booking


async def mark_paid(session: AsyncSession, booking: Booking) -> Booking:
    """Settle the booking and put it straight into awaiting_deposit.

    `paid` is a state for the ledger, not somewhere a booking waits: the
    customer's next act is to deposit, so both transitions run together and the
    timeline shows each of them.
    """
    booking.paid_at = utcnow()
    # The hold protected an unpaid cell. Payment replaces it: from here the
    # booking itself holds the cell, and the hold worker must leave it alone.
    booking.hold_expires_at = None
    # Payment is a checklist step, not a state to sit in: the contract's status
    # set goes straight from pending_payment to awaiting_deposit, so the step is
    # recorded and the status moves once.
    await record_event(session, booking, BookingStatus.PENDING_PAYMENT, "Оплачено",
                       step="paid")
    return await _move(session, booking, BookingStatus.AWAITING_DEPOSIT,
                       "Ожидает отправителя")


async def mark_deposited(
    session: AsyncSession, booking: Booking, postamat, moment: datetime | None = None,
) -> tuple[Booking, str]:
    """Close the door on a parcel and issue the code that opens it again.

    The pickup code exists from this moment and not before: issuing it at
    booking time would put a live door code in somebody's hands for hours while
    the cell was still empty. The caller sends it — that is where the recipient's
    phone and language are known — and the plaintext is returned exactly once.
    """
    at = moment or utcnow()
    booking.deposited_at = at
    booking.expires_at = storage_expiry(postamat, at, booking.duration_hours)
    await _ensure_codes(session, booking)
    pickup_code = issue_code(booking, CodePurpose.PICKUP)
    booking.pickup_code_sent_at = at
    await _move(session, booking, BookingStatus.AWAITING_PICKUP,
                "Посылка в ячейке", step="parcel_deposited")
    await record_event(session, booking, BookingStatus.AWAITING_PICKUP,
                       "Код получения отправлен", step="pickup_code_sent")
    return booking, pickup_code


async def mark_collected(
    session: AsyncSession, booking: Booking, moment: datetime | None = None
) -> Booking:
    booking.collected_at = moment or utcnow()
    return await _move(session, booking, BookingStatus.COMPLETED, "Получено",
                       step="collected")


async def expire(session: AsyncSession, booking: Booking) -> Booking:
    return await _move(session, booking, BookingStatus.EXPIRED, "Срок хранения истёк",
                       step="expired")


async def extend_hold(session: AsyncSession, booking: Booking) -> Booking:
    """Push the payment hold out once more.

    The case is a client still on the bank's 3-D Secure page when the ten
    minutes run out. The cell stays theirs, but not indefinitely: past the limit
    the hold dies and the cell goes back on the market.
    """
    settings = get_settings()
    if booking.status != BookingStatus.PENDING_PAYMENT:
        raise AppError(ErrorCode.BOOKING_ALREADY_PAID,
                       "This booking is no longer waiting for payment.", 409)
    if booking.hold_expires_at is None or as_utc(booking.hold_expires_at) <= utcnow():
        raise AppError(ErrorCode.BOOKING_HOLD_EXPIRED, "The hold has already run out.",
                       409)
    if booking.hold_extensions >= settings.hold_extensions_max:
        raise AppError(
            ErrorCode.HOLD_EXTENSION_LIMIT_EXCEEDED,
            "This hold cannot be extended again.", 409,
            details={"limit": settings.hold_extensions_max},
        )

    booking.hold_extensions += 1
    booking.hold_expires_at = as_utc(booking.hold_expires_at) + timedelta(
        minutes=settings.hold_minutes
    )
    await record_event(session, booking, booking.status, "Бронь продлена",
                       details={"extension": booking.hold_extensions})
    return booking


async def to_grace(session: AsyncSession, booking: Booking) -> Booking:
    return await _move(session, booking, BookingStatus.GRACE, "Льготный период")


async def payment_failed(
    session: AsyncSession, booking: Booking, reason: str | None = None
) -> Booking:
    """The hold died without a successful payment.

    Kept apart from a plain cancellation because the app draws a different
    screen for each: one is the customer's decision, the other is the bank's.
    """
    booking.cancelled_reason = reason or "Оплата не прошла"
    return await _move(session, booking, BookingStatus.PAYMENT_FAILED,
                       "Оплата не прошла", details={"reason": reason})


async def to_remove(session: AsyncSession, booking: Booking) -> Booking:
    return await _move(session, booking, BookingStatus.TO_REMOVE, "К изъятию")


async def to_overdue(session: AsyncSession, booking: Booking) -> Booking:
    booking.remove_after = utcnow() + timedelta(
        hours=get_settings().removal_after_hours
    )
    return await _move(session, booking, BookingStatus.OVERDUE, "Просрочено")


async def mark_removed(session: AsyncSession, booking: Booking) -> Booking:
    # This is what frees the cell, and custody calls it only after an act
    # exists: a cell released without a record is a parcel nobody can trace.
    return await _move(session, booking, BookingStatus.REMOVED, "Посылка изъята")


async def close_custody(session: AsyncSession, booking: Booking) -> Booking:
    return await _move(session, booking, BookingStatus.CLOSED, "Дело закрыто")


async def cancel(
    session: AsyncSession, booking: Booking, reason: str, actor: str | None = None
) -> Booking:
    booking.cancelled_reason = reason
    return await _move(session, booking, BookingStatus.CANCELLED, "Отменено",
                       details={"reason": reason, "actor": actor}, step="cancelled")
