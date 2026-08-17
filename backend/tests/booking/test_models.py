import uuid

import pytest
from sqlalchemy.exc import IntegrityError

from app.modules.booking.models import Booking, BookingStatus, CELL_HELD_STATUSES


def _booking(cell_id: uuid.UUID, status: BookingStatus) -> Booking:
    return Booking(
        client_id=uuid.uuid4(), postamat_id=uuid.uuid4(), cell_id=cell_id,
        cell_type_id=uuid.uuid4(), duration_hours=24, amount_minor=1800,
        currency="TMT", status=status, recipient_phone="+99362123456",
        recipient_name="Получатель",
    )


def test_held_statuses_are_the_ones_that_occupy_a_cell():
    assert BookingStatus.PENDING_PAYMENT in CELL_HELD_STATUSES
    assert BookingStatus.AWAITING_PICKUP in CELL_HELD_STATUSES
    assert BookingStatus.CANCELLED not in CELL_HELD_STATUSES
    assert BookingStatus.COMPLETED not in CELL_HELD_STATUSES


async def test_one_active_booking_per_cell_is_enforced_by_the_database(session):
    cell_id = uuid.uuid4()
    session.add(_booking(cell_id, BookingStatus.PAID))
    await session.flush()

    session.add(_booking(cell_id, BookingStatus.PENDING_PAYMENT))
    with pytest.raises(IntegrityError):
        await session.flush()
    await session.rollback()


async def test_a_finished_booking_frees_the_cell_for_a_new_one(session):
    cell_id = uuid.uuid4()
    session.add(_booking(cell_id, BookingStatus.COMPLETED))
    await session.flush()

    session.add(_booking(cell_id, BookingStatus.PENDING_PAYMENT))
    await session.flush()  # must not raise
