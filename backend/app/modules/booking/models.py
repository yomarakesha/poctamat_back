import uuid
from datetime import datetime
from enum import StrEnum

from sqlalchemy import JSON, DateTime, ForeignKey, Index, Integer, String, func, text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.db import Base, Timestamped, UUIDPrimaryKey, utcnow


class BookingStatus(StrEnum):
    PENDING_PAYMENT = "pending_payment"
    AWAITING_DEPOSIT = "awaiting_deposit"
    AWAITING_PICKUP = "awaiting_pickup"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    # The overdue branch. Nothing is charged at any stage; the stages exist so
    # staff know when a cell may be emptied and the recipient is warned first.
    EXPIRED = "expired"
    GRACE = "grace"
    OVERDUE = "overdue"
    TO_REMOVE = "to_remove"
    REMOVED = "removed"
    CLOSED = "closed"
    # A hold that died without a successful payment, kept apart from a plain
    # cancellation because the app shows a different screen for each.
    PAYMENT_FAILED = "payment_failed"


# The statuses during which the cell belongs to this booking and to no other.
# The partial unique index below is built from exactly this set, so changing it
# without rebuilding the index in a migration would let a cell be sold while
# somebody's parcel is still inside it.
CELL_HELD_STATUSES = frozenset({
    BookingStatus.PENDING_PAYMENT,
    BookingStatus.AWAITING_DEPOSIT,
    BookingStatus.AWAITING_PICKUP,
    BookingStatus.EXPIRED,
    BookingStatus.GRACE,
    BookingStatus.OVERDUE,
    BookingStatus.TO_REMOVE,
})

_HELD_SQL = ", ".join(f"'{status.value}'" for status in sorted(CELL_HELD_STATUSES))


class Depositor(StrEnum):
    OWNER = "owner"
    COURIER = "courier"


class CodePurpose(StrEnum):
    # Two grants, not three. A courier gets the deposit code — that is what the
    # deposit code is for — sent to `courier_phone` instead of the sender's.
    DEPOSIT = "deposit"
    PICKUP = "pickup"


class Booking(UUIDPrimaryKey, Timestamped, Base):
    __tablename__ = "bookings"
    __table_args__ = (
        Index(
            "uq_active_booking_per_cell", "cell_id", unique=True,
            sqlite_where=text(f"status IN ({_HELD_SQL})"),
            postgresql_where=text(f"status IN ({_HELD_SQL})"),
        ),
        Index("ix_bookings_client_created", "client_id", "created_at"),
    )

    client_id: Mapped[uuid.UUID] = mapped_column(index=True)
    postamat_id: Mapped[uuid.UUID] = mapped_column(index=True)
    cell_id: Mapped[uuid.UUID] = mapped_column(index=True)
    cell_type_id: Mapped[uuid.UUID] = mapped_column(index=True)
    duration_hours: Mapped[int] = mapped_column(Integer)
    # Copied from the tariff, never referenced: prices change and history has to
    # stay true to what the customer agreed to pay.
    amount_minor: Mapped[int] = mapped_column(Integer)
    currency: Mapped[str] = mapped_column(String(3), default="TMT")
    status: Mapped[BookingStatus] = mapped_column(
        String(24), index=True, default=BookingStatus.PENDING_PAYMENT
    )
    depositor: Mapped[Depositor] = mapped_column(String(16), default=Depositor.OWNER)
    courier_phone: Mapped[str | None] = mapped_column(String(16))
    recipient_phone: Mapped[str] = mapped_column(String(16), index=True)
    recipient_name: Mapped[str | None] = mapped_column(String(200))
    # The short reservation before payment. Distinct from expires_at, which is
    # the storage clock and only starts once the parcel is in the cell.
    hold_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    deposited_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    collected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancelled_reason: Mapped[str | None] = mapped_column(String(500))
    # When staff may take the parcel out. Stored rather than computed so the
    # work queue is a plain query, and so retuning the interval never
    # retroactively moves parcels that are already overdue.
    remove_after: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Stamped when the "your storage ends soon" message goes out, so a worker
    # running every five minutes does not send it every five minutes.
    reminded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # When the pickup code last went out by SMS. Null until the parcel is inside:
    # there is nothing to collect before that, and the app greys the step out.
    pickup_code_sent_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    # Counted rather than derived from the timeline: the hold may be extended a
    # fixed number of times, and reading that off events would break the moment
    # an unrelated event learns to write the same step.
    hold_extensions: Mapped[int] = mapped_column(Integer, default=0)

    events: Mapped[list["BookingEvent"]] = relationship(
        back_populates="booking", lazy="selectin", cascade="all, delete-orphan",
        order_by="BookingEvent.seq",
    )
    codes: Mapped[list["AccessCode"]] = relationship(
        back_populates="booking", lazy="selectin", cascade="all, delete-orphan",
    )


class BookingEvent(UUIDPrimaryKey, Timestamped, Base):
    __tablename__ = "booking_events"

    booking_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("bookings.id"), index=True
    )
    # Position in this booking's timeline, and the only thing it is ordered by.
    # Timestamps cannot do the job: SQLite's CURRENT_TIMESTAMP has second
    # resolution and hands every row of one statement the same value, so two
    # transitions written together — payment writes two — would come back in
    # arbitrary order and show the customer a history that never happened.
    seq: Mapped[int] = mapped_column(Integer, default=0)
    # The checklist step this event is, in the front-end contract's vocabulary:
    # booked, paid, parcel_deposited, pickup_code_sent, collected, expired,
    # cancelled. Null for transitions the checklist does not draw.
    step: Mapped[str | None] = mapped_column(String(24))
    # The timeline the app renders. One row per transition, so the screen never
    # has to reconstruct history from a single status column.
    status: Mapped[BookingStatus] = mapped_column(String(24))
    message: Mapped[str] = mapped_column(String(200))
    details: Mapped[dict | None] = mapped_column(JSON)

    booking: Mapped[Booking] = relationship(back_populates="events")


class AccessCode(UUIDPrimaryKey, Timestamped, Base):
    __tablename__ = "access_codes"

    booking_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("bookings.id"), index=True
    )
    purpose: Mapped[CodePurpose] = mapped_column(String(16))
    # Only the digest. The plaintext is shown once, when it is issued.
    code_hash: Mapped[str] = mapped_column(String(64), index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    booking: Mapped[Booking] = relationship(back_populates="codes")
