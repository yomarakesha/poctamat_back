import uuid

import pytest

from app.core.errors import AppError
from app.modules.booking.models import Booking, BookingStatus, CELL_HELD_STATUSES
from app.modules.booking.service import (
    close_custody,
    expire,
    mark_collected,
    mark_removed,
    to_grace,
    to_overdue,
)


def _booking(status=BookingStatus.AWAITING_PICKUP) -> Booking:
    return Booking(
        client_id=uuid.uuid4(), postamat_id=uuid.uuid4(), cell_id=uuid.uuid4(),
        cell_type_id=uuid.uuid4(), duration_hours=24, amount_minor=1800,
        status=status, recipient_phone="+99362123456",
    )


def test_an_overdue_parcel_still_holds_its_cell():
    for status in (BookingStatus.EXPIRED, BookingStatus.GRACE, BookingStatus.OVERDUE):
        assert status in CELL_HELD_STATUSES
    # Removal is what frees it, and only once an act exists.
    assert BookingStatus.REMOVED not in CELL_HELD_STATUSES
    assert BookingStatus.CLOSED not in CELL_HELD_STATUSES


async def test_the_escalation_walks_one_stage_at_a_time(session):
    booking = _booking()
    session.add(booking)
    await session.flush()

    await expire(session, booking)
    assert booking.status == BookingStatus.EXPIRED
    await to_grace(session, booking)
    assert booking.status == BookingStatus.GRACE
    await to_overdue(session, booking)
    assert booking.status == BookingStatus.OVERDUE
    assert booking.remove_after is not None
    await mark_removed(session, booking)
    assert booking.status == BookingStatus.REMOVED
    await close_custody(session, booking)
    assert booking.status == BookingStatus.CLOSED

    assert [event.seq for event in booking.events] == list(range(5))


async def test_a_collected_parcel_cannot_go_overdue(session):
    booking = _booking(status=BookingStatus.COMPLETED)
    session.add(booking)
    await session.flush()
    with pytest.raises(AppError):
        await expire(session, booking)


async def test_pickup_still_works_at_every_overdue_stage(session):
    for status in (BookingStatus.EXPIRED, BookingStatus.GRACE, BookingStatus.OVERDUE):
        booking = _booking(status=status)
        session.add(booking)
        await session.flush()
        # Collection stays free at every stage — the product charges nothing for
        # being late, so the door has to keep opening.
        await mark_collected(session, booking)
        assert booking.status == BookingStatus.COMPLETED


async def test_a_cell_cannot_be_resold_while_a_parcel_is_still_in_it(session):
    import pytest as _pytest
    from sqlalchemy.exc import IntegrityError

    cell_id = uuid.uuid4()
    overdue = _booking(status=BookingStatus.OVERDUE)
    overdue.cell_id = cell_id
    session.add(overdue)
    await session.flush()

    second = _booking(status=BookingStatus.PENDING_PAYMENT)
    second.cell_id = cell_id
    session.add(second)
    with _pytest.raises(IntegrityError):
        await session.flush()
    await session.rollback()
