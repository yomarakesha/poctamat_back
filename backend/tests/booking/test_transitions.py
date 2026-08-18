import uuid
from datetime import datetime, time, timezone

import pytest

from app.core.errors import AppError, ErrorCode
from app.modules.booking.models import Booking, BookingStatus, CELL_HELD_STATUSES
from app.modules.booking.service import (
    cancel,
    mark_collected,
    mark_deposited,
    mark_paid,
)
from app.modules.catalog.models import PostamatSchedule


def _booking(**overrides) -> Booking:
    base = dict(
        client_id=uuid.uuid4(), postamat_id=uuid.uuid4(), cell_id=uuid.uuid4(),
        cell_type_id=uuid.uuid4(), duration_hours=12, amount_minor=1200,
        status=BookingStatus.PENDING_PAYMENT, recipient_phone="+99362123456",
    )
    base.update(overrides)
    return Booking(**base)


async def test_payment_moves_straight_to_awaiting_deposit(session):
    booking = _booking()
    session.add(booking)
    await session.flush()

    await mark_paid(session, booking)
    assert booking.status == BookingStatus.AWAITING_DEPOSIT
    assert booking.paid_at is not None
    # The hold is over: what protects the cell now is the booking itself.
    assert booking.hold_expires_at is None
    assert [event.status for event in booking.events][-1] == BookingStatus.AWAITING_DEPOSIT


async def test_paying_twice_is_refused(session):
    booking = _booking()
    session.add(booking)
    await session.flush()
    await mark_paid(session, booking)

    with pytest.raises(AppError) as caught:
        await mark_paid(session, booking)
    assert caught.value.code == ErrorCode.BOOKING_INVALID_STATE


async def test_deposit_starts_the_storage_clock_at_the_next_opening(session, postamat):
    postamat.schedule = [
        PostamatSchedule(weekday=day, opens_at=time(8, 0), closes_at=time(20, 0))
        for day in range(7)
    ]
    booking = _booking(status=BookingStatus.AWAITING_DEPOSIT, duration_hours=12)
    session.add(booking)
    await session.flush()

    deposited = datetime(2026, 8, 17, 19, 0, tzinfo=timezone.utc)
    _, pickup_code = await mark_deposited(session, booking, postamat, moment=deposited)

    assert booking.status == BookingStatus.AWAITING_PICKUP
    assert booking.deposited_at == deposited
    # The pickup code exists from this moment and not before: it opens a door
    # with a parcel behind it.
    assert len(pickup_code) == 5
    assert booking.pickup_code_sent_at == deposited
    assert booking.expires_at == datetime(2026, 8, 18, 8, 0, tzinfo=timezone.utc)


async def test_collection_completes_and_frees_the_cell(session):
    booking = _booking(status=BookingStatus.AWAITING_PICKUP)
    session.add(booking)
    await session.flush()

    await mark_collected(session, booking)
    assert booking.status == BookingStatus.COMPLETED
    assert booking.collected_at is not None
    assert booking.status not in CELL_HELD_STATUSES


async def test_cancelling_records_the_reason_and_frees_the_cell(session):
    booking = _booking()
    session.add(booking)
    await session.flush()

    await cancel(session, booking, reason="Клиент отменил", actor="client")
    assert booking.status == BookingStatus.CANCELLED
    assert booking.cancelled_reason == "Клиент отменил"
    assert booking.status not in CELL_HELD_STATUSES


async def test_a_deposited_parcel_cannot_be_cancelled(session):
    booking = _booking(status=BookingStatus.AWAITING_PICKUP)
    session.add(booking)
    await session.flush()

    with pytest.raises(AppError) as caught:
        await cancel(session, booking, reason="передумал")
    assert caught.value.status_code == 409
