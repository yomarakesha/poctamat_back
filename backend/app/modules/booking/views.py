"""Client-facing serialisation of a booking.

The database keeps its own names — `depositor`, `amount_minor`, `kind` — and the
front-end contract keeps its. This module is the only place the two meet, so a
rename on the wire never turns into a migration, and three generated clients
never see a field the contract has not defined.
"""

import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import utcnow
from app.core.types import Money, utc_isoformat
from app.modules.booking.models import Booking, BookingStatus
from app.modules.booking.schemas import BookingDetail, BookingListItem, PaymentBrief
from app.modules.catalog.models import Cell, CellType, Postamat
from app.modules.payments.models import Payment

# Where a booking stops being something the app can act on. Everything else is
# «активные» on screen 488:8413 — including the overdue stages, because a parcel
# behind a door the recipient can still open is not history.
FINISHED_STATUSES = frozenset({
    BookingStatus.COMPLETED,
    BookingStatus.CANCELLED,
    BookingStatus.PAYMENT_FAILED,
    BookingStatus.REMOVED,
    BookingStatus.CLOSED,
})


@dataclass
class BookingContext:
    """Everything a page of bookings needs that does not live on the booking."""

    cells: dict[uuid.UUID, Cell] = field(default_factory=dict)
    postamats: dict[uuid.UUID, Postamat] = field(default_factory=dict)
    cell_types: dict[uuid.UUID, CellType] = field(default_factory=dict)
    payments: dict[uuid.UUID, Payment] = field(default_factory=dict)


async def load_context(
    session: AsyncSession, bookings: Sequence[Booking], *, with_payments: bool = False
) -> BookingContext:
    """One query per related table for the whole page, never one per booking."""
    if not bookings:
        return BookingContext()

    cells = {
        row.id: row
        for row in await session.scalars(
            select(Cell).where(Cell.id.in_({b.cell_id for b in bookings}))
        )
    }
    postamats = {
        row.id: row
        for row in await session.scalars(
            select(Postamat).where(Postamat.id.in_({b.postamat_id for b in bookings}))
        )
    }
    cell_types = {
        row.id: row
        for row in await session.scalars(
            select(CellType).where(CellType.id.in_({b.cell_type_id for b in bookings}))
        )
    }

    payments: dict[uuid.UUID, Payment] = {}
    if with_payments:
        # Newest last, so the dict keeps the latest attempt per booking — the one
        # the result screen is polling.
        rows = await session.scalars(
            select(Payment)
            .where(Payment.booking_id.in_({b.id for b in bookings}))
            .order_by(Payment.created_at)
        )
        for row in rows:
            payments[row.booking_id] = row

    return BookingContext(cells=cells, postamats=postamats, cell_types=cell_types,
                          payments=payments)


def _cell_type_name(cell_type: CellType | None, language: str) -> str:
    if cell_type is None:
        return ""
    return getattr(cell_type, f"name_{language}", None) or cell_type.name_ru


def qr_payload(booking: Booking) -> str:
    """What the app draws in the booking QR.

    Deliberately not a code: a QR is shown on a screen in a public place and
    photographed by anyone standing behind. It names the booking, and the door
    still opens on a PIN.
    """
    return f"PB1:{booking.id}"


def _payment_brief(payment: Payment | None) -> PaymentBrief | None:
    if payment is None:
        return None
    return PaymentBrief(
        id=payment.id, booking_id=payment.booking_id, status=payment.status,
        amount=Money(amount_minor=payment.amount_minor, currency=payment.currency),
        bank_code=payment.provider,
        redirect_url=None,
        expires_at=None,
        failure_code=payment.failure_reason,
        created_at=utc_isoformat(payment.created_at),
    )


def list_item(
    booking: Booking, context: BookingContext, language: str
) -> BookingListItem:
    cell = context.cells.get(booking.cell_id)
    postamat = context.postamats.get(booking.postamat_id)
    return BookingListItem(
        id=booking.id, status=booking.status, postamat_id=booking.postamat_id,
        postamat_name=postamat.name if postamat else "",
        # A string on the wire though it is an integer in the database: door
        # labels are not arithmetic, and a cabinet numbered "A-3" costs nothing
        # to support this way.
        cell_number=str(cell.number) if cell else "",
        cell_type_name=_cell_type_name(context.cell_types.get(booking.cell_type_id),
                                       language),
        created_at=utc_isoformat(booking.created_at),
        expires_at=utc_isoformat(booking.expires_at) if booking.expires_at else None,
    )


def detail(
    booking: Booking,
    context: BookingContext,
    language: str,
    *,
    deposit_code: str | None = None,
) -> BookingDetail:
    """The full booking. `deposit_code` is filled only where it is issued.

    Every other read answers null there, because the server keeps a keyed hash
    and genuinely cannot produce the digits again.
    """
    postamat = context.postamats.get(booking.postamat_id)
    return BookingDetail(
        **list_item(booking, context, language).model_dump(),
        postamat_address=postamat.address if postamat else "",
        cell_id=booking.cell_id,
        duration_hours=booking.duration_hours,
        price=Money(amount_minor=booking.amount_minor, currency=booking.currency),
        recipient_phone=booking.recipient_phone,
        recipient_name=booking.recipient_name,
        deposited_by=booking.depositor,
        courier_phone=booking.courier_phone,
        hold_expires_at=(
            utc_isoformat(booking.hold_expires_at) if booking.hold_expires_at else None
        ),
        deposit_code=deposit_code,
        pickup_code_sent_at=(
            utc_isoformat(booking.pickup_code_sent_at)
            if booking.pickup_code_sent_at else None
        ),
        qr_payload=qr_payload(booking),
        payment=_payment_brief(context.payments.get(booking.id)),
        cached_at=utc_isoformat(utcnow()),
    )
