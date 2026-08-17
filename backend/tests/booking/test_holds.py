import uuid
from datetime import timedelta

from app.core.db import utcnow
from app.modules.booking.models import Booking, BookingStatus
from app.workers.holds import release_expired_holds


def _booking(status, hold_offset_minutes):
    return Booking(
        client_id=uuid.uuid4(), postamat_id=uuid.uuid4(), cell_id=uuid.uuid4(),
        cell_type_id=uuid.uuid4(), duration_hours=24, amount_minor=1800,
        status=status, recipient_phone="+99362123456",
        hold_expires_at=utcnow() + timedelta(minutes=hold_offset_minutes),
    )


async def test_an_expired_unpaid_hold_is_cancelled(session):
    stale = _booking(BookingStatus.PENDING_PAYMENT, -1)
    session.add(stale)
    await session.commit()

    released = await release_expired_holds(session)
    assert released == [stale.id]
    assert stale.status == BookingStatus.CANCELLED
    assert stale.cancelled_reason == "Бронь не оплачена вовремя"


async def test_a_live_hold_is_left_alone(session):
    fresh = _booking(BookingStatus.PENDING_PAYMENT, 5)
    session.add(fresh)
    await session.commit()

    assert await release_expired_holds(session) == []
    assert fresh.status == BookingStatus.PENDING_PAYMENT


async def test_a_paid_booking_is_never_released(session):
    # mark_paid clears hold_expires_at, but a row written before that, or by a
    # future code path, must still be safe: paid bookings are out of scope.
    paid = _booking(BookingStatus.PAID, -60)
    session.add(paid)
    await session.commit()

    assert await release_expired_holds(session) == []
    assert paid.status == BookingStatus.PAID


async def test_the_timeline_records_the_cancellation(session):
    stale = _booking(BookingStatus.PENDING_PAYMENT, -1)
    session.add(stale)
    await session.commit()

    await release_expired_holds(session)
    assert [event.status for event in stale.events][-1] == BookingStatus.CANCELLED


async def test_the_released_cell_can_be_booked_again(
    client, session, client_token, admin_token, city, cell_type, postamat
):
    from app.modules.catalog.models import Cell

    session.add(Cell(postamat_id=postamat.id, cell_type_id=cell_type.id, number=1,
                     board=1, output=1))
    await session.commit()
    await client.put("/api/v1/admin/tariffs",
                     headers={"Authorization": f"Bearer {admin_token}"},
                     json={"entries": [
                         {"city_id": str(city.id), "cell_type_id": str(cell_type.id),
                          "duration_hours": hours, "amount_minor": 1800}
                         for hours in (12, 24, 48)
                     ]})
    headers = {"Authorization": f"Bearer {client_token}"}
    body = {"postamat_id": str(postamat.id), "cell_type_id": str(cell_type.id),
            "duration_hours": 24, "recipient_phone": "+99365000001"}

    first = await client.post("/api/v1/bookings", json=body, headers=headers)
    assert first.status_code == 201

    booking = await session.get(Booking, uuid.UUID(first.json()["id"]))
    booking.hold_expires_at = utcnow() - timedelta(minutes=1)
    await session.commit()

    assert len(await release_expired_holds(session)) == 1

    again = await client.post("/api/v1/bookings", json=body, headers=headers)
    assert again.status_code == 201
