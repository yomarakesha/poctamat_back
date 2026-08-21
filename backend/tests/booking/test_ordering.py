import uuid

from app.modules.booking.models import Booking, BookingStatus
from app.modules.booking.service import mark_paid
from tests.helpers import set_tariffs


def _booking() -> Booking:
    return Booking(
        client_id=uuid.uuid4(), postamat_id=uuid.uuid4(), cell_id=uuid.uuid4(),
        cell_type_id=uuid.uuid4(), duration_hours=24, amount_minor=1800,
        status=BookingStatus.PENDING_PAYMENT, recipient_phone="+99362123456",
    )


async def test_the_timeline_keeps_the_order_transitions_happened_in(session):
    # mark_paid writes two entries inside one transaction. SQLite's CURRENT_TIMESTAMP
    # has second resolution and is the same value for every row in a statement, so
    # ordering the timeline by a timestamp lets "Ожидает отправителя" appear before
    # "Оплачено" — a screen showing the customer a history that never happened.
    booking = _booking()
    session.add(booking)
    await session.flush()
    from app.modules.booking.service import record_event

    await record_event(session, booking, BookingStatus.PENDING_PAYMENT, "Забронировано")
    await mark_paid(session, booking)
    await session.commit()

    reloaded = await session.get(Booking, booking.id)
    assert [event.seq for event in reloaded.events] == [0, 1, 2]
    assert [event.message for event in reloaded.events] == [
        "Забронировано", "Оплачено", "Ожидает отправителя",
    ]


async def test_the_timeline_survives_a_reload_in_the_same_second(session):
    booking = _booking()
    session.add(booking)
    await session.flush()
    from app.modules.booking.service import record_event

    for index in range(5):
        await record_event(session, booking, BookingStatus.PENDING_PAYMENT,
                           f"шаг {index}")
    await session.commit()

    # Read the rows back the way the relationship does, but through a fresh
    # query, so the assertion is about what the database returns and not about
    # the order the objects happen to sit in memory.
    from sqlalchemy import select

    from app.modules.booking.models import BookingEvent

    rows = await session.scalars(
        select(BookingEvent)
        .where(BookingEvent.booking_id == booking.id)
        .order_by(BookingEvent.seq)
    )
    assert [row.message for row in rows] == [f"шаг {index}" for index in range(5)]


async def test_bookings_created_in_the_same_second_still_list_newest_first(
    client, session, client_token, admin_token, city, cell_type, postamat
):
    from app.modules.catalog.models import Cell

    session.add_all([
        Cell(postamat_id=postamat.id, cell_type_id=cell_type.id, number=n,
             board=1, output=n)
        for n in (1, 2, 3)
    ])
    await session.commit()
    await set_tariffs(client, admin_token, city, cell_type,
                      [(hours, 1800) for hours in (12, 24, 48)])

    headers = {"Authorization": f"Bearer {client_token}"}
    body = {"postamat_id": str(postamat.id), "cell_type_id": str(cell_type.id),
            "duration_hours": 24, "recipient_phone": "+99365000001"}
    created = []
    for index in range(3):
        response = await client.post(
            "/api/v1/bookings", json=body,
            headers=headers | {"Idempotency-Key": f"order-{index}"},
        )
        assert response.status_code == 201
        created.append(response.json()["id"])

    listed = await client.get("/api/v1/bookings", headers=headers)
    assert [item["id"] for item in listed.json()["items"]] == list(reversed(created))
