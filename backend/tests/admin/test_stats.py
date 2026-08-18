import uuid
from datetime import timedelta

import pytest
from sqlalchemy import event

from app.core.db import utcnow
from app.modules.booking.models import Booking, BookingStatus
from app.modules.catalog.models import Cell, Device, DeviceStatus
from app.modules.payments.models import Payment, PaymentStatus

PATH = "/api/v1/admin/stats"


async def _booking(session, postamat, cell_type, status, cell_id=None, **extra):
    row = Booking(
        client_id=uuid.uuid4(), postamat_id=postamat.id,
        cell_id=cell_id or uuid.uuid4(), cell_type_id=cell_type.id,
        duration_hours=24, amount_minor=1800, status=status,
        recipient_phone="+99365000001", **extra,
    )
    session.add(row)
    await session.commit()
    return row


@pytest.fixture
def count_queries(test_engine):
    seen: list[str] = []

    def before(conn, cursor, statement, parameters, context, executemany):
        seen.append(statement)

    event.listen(test_engine.sync_engine, "before_cursor_execute", before)
    yield seen
    event.remove(test_engine.sync_engine, "before_cursor_execute", before)


async def test_the_queue_counts_what_waits_on_a_person(
    client, session, admin_token, postamat, cell_type
):
    await _booking(session, postamat, cell_type, BookingStatus.OVERDUE)
    await _booking(session, postamat, cell_type, BookingStatus.TO_REMOVE)
    session.add_all([
        Cell(postamat_id=postamat.id, cell_type_id=cell_type.id, number=1,
             board=1, output=1, is_blocked=True),
        Device(postamat_id=postamat.id, status=DeviceStatus.OFFLINE),
    ])
    await session.commit()

    response = await client.get(f"{PATH}/attention",
                                headers={"Authorization": f"Bearer {admin_token}"})
    assert response.status_code == 200
    assert response.json() == {
        "overdue_bookings": 1, "to_remove_bookings": 1, "offline_devices": 1,
        # No lock agent yet, so nothing can have failed. The field is there
        # because the panel draws the tile either way.
        "failed_commands": 0, "blocked_cells": 1,
    }


async def test_a_parcel_that_is_merely_expired_is_not_in_the_queue(
    client, session, admin_token, postamat, cell_type
):
    # Expired and grace are the recipient's problem to solve; the queue is what
    # staff have to act on.
    await _booking(session, postamat, cell_type, BookingStatus.EXPIRED)
    await _booking(session, postamat, cell_type, BookingStatus.GRACE)

    response = await client.get(f"{PATH}/attention",
                                headers={"Authorization": f"Bearer {admin_token}"})
    assert response.json()["overdue_bookings"] == 0


async def test_the_overview_counts_booked_apart_from_occupied(
    client, session, admin_token, postamat, cell_type
):
    session.add_all([
        Cell(postamat_id=postamat.id, cell_type_id=cell_type.id, number=n,
             board=1, output=n)
        for n in (1, 2, 3)
    ])
    await session.commit()
    await _booking(session, postamat, cell_type, BookingStatus.AWAITING_DEPOSIT)
    await _booking(session, postamat, cell_type, BookingStatus.AWAITING_PICKUP)

    response = await client.get(f"{PATH}/overview",
                                headers={"Authorization": f"Bearer {admin_token}"})
    body = response.json()
    assert body["postamat_count"] == 1
    assert body["cell_count"] == 3
    # Paid-for and empty is a different fact from a parcel behind the door.
    assert body["booked_count"] == 1
    assert body["occupied_count"] == 1


async def test_the_histogram_counts_bookings_per_day(
    client, session, admin_token, postamat, cell_type
):
    await _booking(session, postamat, cell_type, BookingStatus.COMPLETED)
    await _booking(session, postamat, cell_type, BookingStatus.COMPLETED)

    response = await client.get(f"{PATH}/bookings?period=7d",
                                headers={"Authorization": f"Bearer {admin_token}"})
    assert response.status_code == 200
    points = response.json()["points"]
    assert len(points) == 1
    assert points[0]["count"] == 2


async def test_revenue_counts_settled_money_only(
    client, session, admin_token, postamat, cell_type
):
    booking = await _booking(session, postamat, cell_type,
                             BookingStatus.AWAITING_PICKUP)
    session.add_all([
        Payment(booking_id=booking.id, client_id=booking.client_id, provider="mock",
                bank_code="halk", amount_minor=1800, status=PaymentStatus.SUCCEEDED,
                settled_at=utcnow()),
        # Pending is not money: counting it would make the chart disagree with
        # the bank.
        Payment(booking_id=booking.id, client_id=booking.client_id, provider="mock",
                bank_code="halk", amount_minor=9900, status=PaymentStatus.PENDING),
    ])
    await session.commit()

    response = await client.get(f"{PATH}/revenue?period=30d",
                                headers={"Authorization": f"Bearer {admin_token}"})
    body = response.json()
    assert body["total"] == {"amount_minor": 1800, "currency": "TMT"}
    assert body["points"][0]["transaction_count"] == 1


async def test_utilization_is_reported_per_postamat(
    client, session, admin_token, postamat, cell_type
):
    session.add_all([
        Cell(postamat_id=postamat.id, cell_type_id=cell_type.id, number=n,
             board=1, output=n)
        for n in (1, 2)
    ])
    await session.commit()
    await _booking(session, postamat, cell_type, BookingStatus.COMPLETED)

    response = await client.get(f"{PATH}/utilization?period=7d",
                                headers={"Authorization": f"Bearer {admin_token}"})
    body = response.json()
    assert body["by_postamat"][0]["postamat_id"] == str(postamat.id)
    assert body["by_postamat"][0]["uses_per_cell"] == 0.5


async def test_peak_hours_are_bucketed_in_ashgabat(
    client, session, admin_token, postamat, cell_type
):
    response = await client.get(f"{PATH}/peak-hours",
                                headers={"Authorization": f"Bearer {admin_token}"})
    assert response.status_code == 200
    # A chart drawn in UTC would tell the reader about five o'clock somewhere
    # else.
    assert response.json()["timezone"] == "Asia/Ashgabat"


async def test_recent_events_carry_their_labels(
    client, session, admin_token, postamat
):
    from app.modules.audit.models import AuditEntry, Severity, Source

    session.add(AuditEntry(
        event="payment.needs_attention", source=Source.SYSTEM,
        severity=Severity.WARNING, message="Оплата по неактивной брони",
        postamat_id=postamat.id, details={"amount_minor": 1800},
    ))
    await session.commit()

    response = await client.get(f"{PATH}/recent-events?limit=5",
                                headers={"Authorization": f"Bearer {admin_token}"})
    assert response.status_code == 200
    item = response.json()["items"][0]
    assert item["event_type"] == "payment.needs_attention"
    assert item["severity"] == "warning"
    assert item["postamat_number"] == postamat.number
    assert item["details"]["amount_minor"] == 1800


async def test_recent_bookings_carry_the_client_and_the_cell(
    client, session, book, admin_token, city, cell_type, postamat, booking_client
):
    session.add(Cell(postamat_id=postamat.id, cell_type_id=cell_type.id, number=4,
                     board=1, output=4))
    await session.commit()
    headers = {"Authorization": f"Bearer {admin_token}"}
    await client.put("/api/v1/admin/tariffs", headers=headers, json={"entries": [
        {"city_id": str(city.id), "cell_type_id": str(cell_type.id),
         "duration_hours": hours, "amount_minor": 1800}
        for hours in (12, 24, 48)
    ]})
    created = await book({"postamat_id": str(postamat.id),
                          "cell_type_id": str(cell_type.id), "duration_hours": 24,
                          "recipient_phone": "+99365000001"})
    assert created.status_code == 201

    response = await client.get(f"{PATH}/recent-bookings", headers=headers)
    item = response.json()["items"][0]
    assert item["client_phone"] == booking_client.phone
    assert item["cell_number"] == "4"
    assert item["cell_type_code"] == cell_type.code


async def test_the_dashboard_tiles_are_a_handful_of_queries(
    client, session, admin_token, postamat, cell_type, count_queries
):
    headers = {"Authorization": f"Bearer {admin_token}"}
    count_queries.clear()
    await client.get(f"{PATH}/overview", headers=headers)
    # Six counters, one query each plus the admin's own lookup. A tile that
    # costs a query per row is what turns a dashboard into sixty round trips.
    assert len(count_queries) <= 8


async def test_statistics_need_a_permission(client, session):
    from app.core.security import create_access_token
    from app.modules.identity.models import AdminUser, Role

    role = Role(code="nobody", name="Nobody", permissions=[])
    session.add(role)
    await session.flush()
    admin = AdminUser(login="nobody_test", full_name="Никто Н.Н.",
                      password_hash="x", role_id=role.id)
    session.add(admin)
    await session.commit()
    await session.refresh(admin)

    response = await client.get(
        f"{PATH}/attention",
        headers={"Authorization": f"Bearer {create_access_token('admin', admin.id)}"},
    )
    assert response.status_code == 403
