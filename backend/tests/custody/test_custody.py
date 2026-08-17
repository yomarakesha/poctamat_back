import uuid
from datetime import timedelta

from sqlalchemy import select

from app.core.db import utcnow
from app.modules.booking.models import Booking, BookingStatus
from app.modules.catalog.models import Cell
from app.modules.custody.models import CustodyRecord, CustodyStatus


async def _overdue_parcel(session, postamat, cell_type, number=1):
    cell = Cell(postamat_id=postamat.id, cell_type_id=cell_type.id, number=number,
                board=1, output=number)
    session.add(cell)
    await session.flush()
    booking = Booking(
        client_id=uuid.uuid4(), postamat_id=postamat.id, cell_id=cell.id,
        cell_type_id=cell_type.id, duration_hours=24, amount_minor=1800,
        status=BookingStatus.OVERDUE, recipient_phone="+99365000001",
        expires_at=utcnow() - timedelta(hours=30),
        remove_after=utcnow() - timedelta(hours=1),
    )
    session.add(booking)
    await session.commit()
    return booking, cell


async def test_filing_an_act_frees_the_cell(
    client, session, admin_token, cell_type, postamat
):
    booking, _ = await _overdue_parcel(session, postamat, cell_type)
    headers = {"Authorization": f"Bearer {admin_token}"}

    response = await client.post("/api/v1/admin/custody", headers=headers, json={
        "booking_id": str(booking.id),
        "description": "Коробка 30×20, скотч, без повреждений",
    })
    assert response.status_code == 201
    assert response.json()["status"] == CustodyStatus.AT_COUNTER
    assert response.json()["cell_number"] == 1
    assert booking.status == BookingStatus.REMOVED

    availability = await client.get(f"/api/v1/postamats/{postamat.id}/availability")
    assert availability.json()["items"][0]["free"] == 1


async def test_an_act_without_a_description_is_refused(
    client, session, admin_token, cell_type, postamat
):
    booking, _ = await _overdue_parcel(session, postamat, cell_type)
    response = await client.post("/api/v1/admin/custody",
                                 headers={"Authorization": f"Bearer {admin_token}"},
                                 json={"booking_id": str(booking.id), "description": ""})
    assert response.status_code == 422
    assert booking.status == BookingStatus.OVERDUE


async def test_a_parcel_that_is_not_overdue_cannot_be_removed(
    client, session, admin_token, cell_type, postamat
):
    booking, _ = await _overdue_parcel(session, postamat, cell_type)
    booking.status = BookingStatus.AWAITING_PICKUP
    await session.commit()

    response = await client.post("/api/v1/admin/custody",
                                 headers={"Authorization": f"Bearer {admin_token}"},
                                 json={"booking_id": str(booking.id),
                                       "description": "рано"})
    assert response.status_code == 409


async def test_handing_the_parcel_over_closes_the_booking(
    client, session, admin_token, cell_type, postamat
):
    booking, _ = await _overdue_parcel(session, postamat, cell_type)
    headers = {"Authorization": f"Bearer {admin_token}"}
    act = await client.post("/api/v1/admin/custody", headers=headers, json={
        "booking_id": str(booking.id), "description": "Коробка",
    })

    handed = await client.post(f"/api/v1/admin/custody/{act.json()['id']}/handover",
                               headers=headers, json={
                                   "to_whom": "recipient", "note": "паспорт проверен",
                               })
    assert handed.status_code == 200
    assert handed.json()["status"] == CustodyStatus.HANDED_OVER
    assert handed.json()["handovers"][0]["to_whom"] == "recipient"
    assert booking.status == BookingStatus.CLOSED


async def test_a_parcel_cannot_be_handed_over_twice(
    client, session, admin_token, cell_type, postamat
):
    booking, _ = await _overdue_parcel(session, postamat, cell_type)
    headers = {"Authorization": f"Bearer {admin_token}"}
    act = await client.post("/api/v1/admin/custody", headers=headers, json={
        "booking_id": str(booking.id), "description": "Коробка",
    })
    body = {"to_whom": "sender", "note": None}
    await client.post(f"/api/v1/admin/custody/{act.json()['id']}/handover",
                      headers=headers, json=body)

    again = await client.post(f"/api/v1/admin/custody/{act.json()['id']}/handover",
                              headers=headers, json=body)
    assert again.status_code == 409


async def test_disposal_records_who_and_why(
    client, session, admin_token, admin_user, cell_type, postamat
):
    booking, _ = await _overdue_parcel(session, postamat, cell_type)
    headers = {"Authorization": f"Bearer {admin_token}"}
    act = await client.post("/api/v1/admin/custody", headers=headers, json={
        "booking_id": str(booking.id), "description": "Коробка",
    })

    disposed = await client.post(f"/api/v1/admin/custody/{act.json()['id']}/dispose",
                                 headers=headers, json={"reason": "30 дней не забрали"})
    assert disposed.status_code == 200
    assert disposed.json()["status"] == CustodyStatus.DISPOSED

    record = await session.scalar(select(CustodyRecord))
    assert record.closed_by == admin_user.login
    assert record.closing_reason == "30 дней не забрали"


async def test_the_queue_lists_what_is_waiting_at_the_counter(
    client, session, admin_token, cell_type, postamat
):
    booking, _ = await _overdue_parcel(session, postamat, cell_type)
    headers = {"Authorization": f"Bearer {admin_token}"}
    await client.post("/api/v1/admin/custody", headers=headers,
                      json={"booking_id": str(booking.id), "description": "Коробка"})

    listed = await client.get("/api/v1/admin/custody?status=at_counter",
                              headers=headers)
    assert len(listed.json()["items"]) == 1
    assert listed.json()["items"][0]["description"] == "Коробка"


async def test_removal_is_written_to_the_audit_log(
    client, session, admin_token, admin_user, cell_type, postamat
):
    from app.modules.audit.models import AuditEntry, Severity

    booking, _ = await _overdue_parcel(session, postamat, cell_type)
    await client.post("/api/v1/admin/custody",
                      headers={"Authorization": f"Bearer {admin_token}"},
                      json={"booking_id": str(booking.id), "description": "Коробка"})

    entry = await session.scalar(
        select(AuditEntry).where(AuditEntry.event == "custody.removed")
    )
    assert entry is not None
    assert entry.actor == admin_user.login
    assert entry.severity == Severity.WARNING


async def test_custody_needs_its_own_permission(client, session, postamat):
    from app.core.security import create_access_token
    from app.modules.identity.models import AdminUser, Role

    role = Role(code="clerk", name="Clerk", permissions=["bookings.read"])
    session.add(role)
    await session.flush()
    admin = AdminUser(login="clerk_test", full_name="Клеркова К.К.",
                      password_hash="x", role_id=role.id)
    session.add(admin)
    await session.commit()
    await session.refresh(admin)

    response = await client.get(
        "/api/v1/admin/custody",
        headers={"Authorization": f"Bearer {create_access_token('admin', admin.id)}"},
    )
    assert response.status_code == 403
