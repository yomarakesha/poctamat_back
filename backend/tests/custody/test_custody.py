import uuid
from datetime import timedelta

from sqlalchemy import select

from app.core.db import utcnow
from app.modules.booking.models import Booking, BookingStatus
from app.modules.catalog.models import Cell
from app.modules.custody.models import CustodyRecord, CustodyStatus


def _key():
    return {"Idempotency-Key": str(uuid.uuid4())}


async def _overdue_parcel(session, postamat, cell_type, number=1):
    cell = Cell(postamat_id=postamat.id, cell_type_id=cell_type.id, number=number,
                board=1, output=number)
    session.add(cell)
    await session.flush()
    booking = Booking(
        client_id=uuid.uuid4(), postamat_id=postamat.id, cell_id=cell.id,
        cell_type_id=cell_type.id, duration_hours=24, amount_minor=1800,
        status=BookingStatus.TO_REMOVE, recipient_phone="+99365000001",
        expires_at=utcnow() - timedelta(hours=30),
        remove_after=utcnow() - timedelta(hours=1),
    )
    session.add(booking)
    await session.commit()
    return booking, cell


async def _file_act(client, admin_token, booking, **overrides):
    body = {"booking_id": str(booking.id), "reason": "Срок вышел, ячейка нужна",
            "description": "Коробка 30×20, скотч, без повреждений"} | overrides
    return await client.post(
        "/api/v1/admin/custody",
        headers={"Authorization": f"Bearer {admin_token}"} | _key(), json=body,
    )


async def _due_now(session):
    """Move the disposal date into the past, the way thirty days would."""
    record = await session.scalar(select(CustodyRecord))
    record.disposal_due_at = utcnow() - timedelta(minutes=1)
    await session.commit()
    return record


async def test_filing_an_act_frees_the_cell(
    client, session, admin_token, cell_type, postamat
):
    booking, _ = await _overdue_parcel(session, postamat, cell_type)

    response = await _file_act(client, admin_token, booking)
    assert response.status_code == 201
    body = response.json()
    assert body["status"] == CustodyStatus.AT_COUNTER
    # The contract types the cell number as a string: it is a label on a door,
    # not a quantity.
    assert body["cell_number"] == "1"
    assert body["reason"] == "Срок вышел, ячейка нужна"
    assert body["removed_at"].endswith("Z")
    assert body["disposal_due_at"] is not None
    assert booking.status == BookingStatus.REMOVED

    availability = await client.get(f"/api/v1/postamats/{postamat.id}/availability")
    assert availability.json()["items"][0]["free"] == 1


async def test_an_act_without_a_description_is_refused(
    client, session, admin_token, cell_type, postamat
):
    booking, _ = await _overdue_parcel(session, postamat, cell_type)
    response = await _file_act(client, admin_token, booking, description="")
    assert response.status_code == 422
    assert booking.status == BookingStatus.TO_REMOVE


async def test_an_act_without_a_reason_is_refused(
    client, session, admin_token, cell_type, postamat
):
    booking, _ = await _overdue_parcel(session, postamat, cell_type)
    response = await _file_act(client, admin_token, booking, reason="")
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "REASON_REQUIRED"
    assert booking.status == BookingStatus.TO_REMOVE


async def test_a_parcel_that_is_not_flagged_for_removal_cannot_be_taken_out(
    client, session, admin_token, cell_type, postamat
):
    booking, _ = await _overdue_parcel(session, postamat, cell_type)
    booking.status = BookingStatus.AWAITING_PICKUP
    await session.commit()

    response = await _file_act(client, admin_token, booking, description="рано")
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "BOOKING_NOT_EXPIRED"


async def test_handing_the_parcel_over_closes_the_booking(
    client, session, admin_token, cell_type, postamat
):
    booking, _ = await _overdue_parcel(session, postamat, cell_type)
    headers = {"Authorization": f"Bearer {admin_token}"}
    act = await _file_act(client, admin_token, booking)

    handed = await client.post(
        f"/api/v1/admin/custody/{act.json()['id']}/handover",
        headers=headers | _key(),
        json={"to": "recipient", "document_ref": "AŞ 1234567",
              "note": "паспорт проверен"},
    )
    assert handed.status_code == 200
    body = handed.json()
    assert body["status"] == CustodyStatus.HANDED_OVER
    assert body["handovers"][0]["to"] == "recipient"
    assert body["handovers"][0]["document_ref"] == "AŞ 1234567"
    assert body["handovers"][0]["at"].endswith("Z")
    assert booking.status == BookingStatus.CLOSED


async def test_a_parcel_can_go_to_somebody_else_with_a_note(
    client, session, admin_token, cell_type, postamat
):
    booking, _ = await _overdue_parcel(session, postamat, cell_type)
    act = await _file_act(client, admin_token, booking)

    handed = await client.post(
        f"/api/v1/admin/custody/{act.json()['id']}/handover",
        headers={"Authorization": f"Bearer {admin_token}"} | _key(),
        json={"to": "other", "note": "по доверенности, брат получателя"},
    )
    assert handed.status_code == 200
    assert handed.json()["handovers"][0]["to"] == "other"


async def test_a_parcel_cannot_be_handed_over_twice(
    client, session, admin_token, cell_type, postamat
):
    booking, _ = await _overdue_parcel(session, postamat, cell_type)
    headers = {"Authorization": f"Bearer {admin_token}"}
    act = await _file_act(client, admin_token, booking)
    body = {"to": "sender", "note": None}
    await client.post(f"/api/v1/admin/custody/{act.json()['id']}/handover",
                      headers=headers | _key(), json=body)

    again = await client.post(f"/api/v1/admin/custody/{act.json()['id']}/handover",
                              headers=headers | _key(), json=body)
    assert again.status_code == 409
    assert again.json()["error"]["code"] == "CUSTODY_ALREADY_CLOSED"


async def test_a_parcel_cannot_be_disposed_of_before_its_time(
    client, session, admin_token, cell_type, postamat
):
    booking, _ = await _overdue_parcel(session, postamat, cell_type)
    act = await _file_act(client, admin_token, booking)

    early = await client.post(
        f"/api/v1/admin/custody/{act.json()['id']}/dispose",
        headers={"Authorization": f"Bearer {admin_token}"} | _key(),
        json={"outcome": "disposed", "reason": "не хочу ждать"},
    )
    assert early.status_code == 422
    assert early.json()["error"]["code"] == "CUSTODY_NOT_DUE"
    assert booking.status == BookingStatus.REMOVED


async def test_disposal_records_who_and_why(
    client, session, admin_token, admin_user, cell_type, postamat
):
    booking, _ = await _overdue_parcel(session, postamat, cell_type)
    act = await _file_act(client, admin_token, booking)
    record = await _due_now(session)

    disposed = await client.post(
        f"/api/v1/admin/custody/{act.json()['id']}/dispose",
        headers={"Authorization": f"Bearer {admin_token}"} | _key(),
        json={"outcome": "disposed", "reason": "30 дней не забрали"},
    )
    assert disposed.status_code == 200
    assert disposed.json()["status"] == CustodyStatus.DISPOSED

    await session.refresh(record)
    assert record.closed_by == admin_user.login
    assert record.closing_reason == "30 дней не забрали"


async def test_a_parcel_can_be_returned_to_its_sender(
    client, session, admin_token, cell_type, postamat
):
    booking, _ = await _overdue_parcel(session, postamat, cell_type)
    act = await _file_act(client, admin_token, booking)
    await _due_now(session)

    returned = await client.post(
        f"/api/v1/admin/custody/{act.json()['id']}/dispose",
        headers={"Authorization": f"Bearer {admin_token}"} | _key(),
        json={"outcome": "returned_to_sender", "reason": "отправитель забрал"},
    )
    assert returned.status_code == 200
    assert returned.json()["status"] == CustodyStatus.RETURNED_TO_SENDER
    assert booking.status == BookingStatus.CLOSED


async def test_the_queue_lists_what_is_waiting_at_the_counter(
    client, session, admin_token, cell_type, postamat
):
    booking, _ = await _overdue_parcel(session, postamat, cell_type)
    headers = {"Authorization": f"Bearer {admin_token}"}
    await _file_act(client, admin_token, booking)

    listed = await client.get("/api/v1/admin/custody?status=at_counter",
                              headers=headers)
    body = listed.json()
    assert len(body["items"]) == 1
    assert body["items"][0]["description"].startswith("Коробка")
    assert body["pagination"]["has_more"] is False


async def test_the_queue_takes_several_statuses_at_once(
    client, session, admin_token, cell_type, postamat
):
    first, _ = await _overdue_parcel(session, postamat, cell_type, number=1)
    second, _ = await _overdue_parcel(session, postamat, cell_type, number=2)
    headers = {"Authorization": f"Bearer {admin_token}"}
    kept = await _file_act(client, admin_token, first)
    await _file_act(client, admin_token, second)
    await client.post(f"/api/v1/admin/custody/{kept.json()['id']}/handover",
                      headers=headers | _key(), json={"to": "recipient"})

    both = await client.get(
        "/api/v1/admin/custody?status=at_counter,handed_over", headers=headers
    )
    assert len(both.json()["items"]) == 2

    one = await client.get("/api/v1/admin/custody?status=handed_over",
                           headers=headers)
    assert [item["id"] for item in one.json()["items"]] == [kept.json()["id"]]


async def test_an_unknown_status_filter_is_refused(client, admin_token):
    listed = await client.get("/api/v1/admin/custody?status=lost",
                              headers={"Authorization": f"Bearer {admin_token}"})
    assert listed.status_code == 422
    assert listed.json()["error"]["code"] == "VALIDATION_ERROR"


async def test_the_queue_pages_by_cursor(
    client, session, admin_token, cell_type, postamat
):
    headers = {"Authorization": f"Bearer {admin_token}"}
    for number in (1, 2, 3):
        booking, _ = await _overdue_parcel(session, postamat, cell_type, number)
        await _file_act(client, admin_token, booking)

    first = await client.get("/api/v1/admin/custody?limit=2", headers=headers)
    assert len(first.json()["items"]) == 2
    assert first.json()["pagination"]["has_more"] is True

    cursor = first.json()["pagination"]["next_cursor"]
    rest = await client.get(f"/api/v1/admin/custody?limit=2&cursor={cursor}",
                            headers=headers)
    assert len(rest.json()["items"]) == 1
    assert rest.json()["pagination"]["has_more"] is False
    seen = {item["id"] for item in first.json()["items"] + rest.json()["items"]}
    assert len(seen) == 3


async def test_one_record_reads_back_with_its_handovers(
    client, session, admin_token, cell_type, postamat
):
    booking, _ = await _overdue_parcel(session, postamat, cell_type)
    headers = {"Authorization": f"Bearer {admin_token}"}
    act = await _file_act(client, admin_token, booking)
    await client.post(f"/api/v1/admin/custody/{act.json()['id']}/handover",
                      headers=headers | _key(), json={"to": "recipient"})

    one = await client.get(f"/api/v1/admin/custody/{act.json()['id']}",
                           headers=headers)
    assert one.status_code == 200
    assert len(one.json()["handovers"]) == 1


async def test_removal_is_written_to_the_audit_log(
    client, session, admin_token, admin_user, cell_type, postamat
):
    from app.modules.audit.models import AuditEntry, Severity

    booking, _ = await _overdue_parcel(session, postamat, cell_type)
    await _file_act(client, admin_token, booking)

    entry = await session.scalar(
        select(AuditEntry).where(AuditEntry.event == "custody.removed")
    )
    assert entry is not None
    assert entry.actor == admin_user.login
    assert entry.severity == Severity.WARNING


async def test_filing_an_act_needs_an_idempotency_key(
    client, session, admin_token, cell_type, postamat
):
    booking, _ = await _overdue_parcel(session, postamat, cell_type)
    response = await client.post(
        "/api/v1/admin/custody",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={"booking_id": str(booking.id), "reason": "Срок вышел",
              "description": "Коробка"},
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "IDEMPOTENCY_KEY_MISSING"


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
