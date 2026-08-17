import uuid
from datetime import timedelta

from app.core.db import utcnow
from app.modules.booking.models import Booking, BookingStatus


async def _booking(session, postamat, status, remove_after=None):
    row = Booking(
        client_id=uuid.uuid4(), postamat_id=postamat.id, cell_id=uuid.uuid4(),
        cell_type_id=uuid.uuid4(), duration_hours=24, amount_minor=1800,
        status=status, recipient_phone="+99365000001",
        expires_at=utcnow() - timedelta(hours=5), remove_after=remove_after,
    )
    session.add(row)
    await session.commit()
    return row


async def test_the_queue_separates_overdue_from_ready_to_remove(
    client, session, admin_token, postamat
):
    await _booking(session, postamat, BookingStatus.OVERDUE,
                   remove_after=utcnow() + timedelta(hours=10))
    await _booking(session, postamat, BookingStatus.OVERDUE,
                   remove_after=utcnow() - timedelta(hours=1))

    response = await client.get("/api/v1/admin/dashboard/attention",
                                headers={"Authorization": f"Bearer {admin_token}"})
    assert response.status_code == 200
    assert response.json()["counts"]["overdue"] == 2
    assert response.json()["counts"]["to_remove"] == 1
    assert len(response.json()["to_remove"]) == 1


async def test_a_parcel_that_is_merely_expired_is_not_in_the_queue(
    client, session, admin_token, postamat
):
    # Expired and grace are the recipient's problem to solve; the queue is what
    # staff have to act on.
    await _booking(session, postamat, BookingStatus.EXPIRED)
    await _booking(session, postamat, BookingStatus.GRACE)

    response = await client.get("/api/v1/admin/dashboard/attention",
                                headers={"Authorization": f"Bearer {admin_token}"})
    assert response.json()["counts"]["overdue"] == 0


async def test_payments_needing_attention_reach_the_queue(
    client, session, admin_token
):
    from app.modules.audit.models import AuditEntry, Severity, Source

    session.add(AuditEntry(
        event="payment.needs_attention", source=Source.SYSTEM,
        severity=Severity.WARNING, message="Оплата по неактивной брони",
        details={"amount_minor": 1800},
    ))
    await session.commit()

    response = await client.get("/api/v1/admin/dashboard/attention",
                                headers={"Authorization": f"Bearer {admin_token}"})
    assert response.json()["counts"]["payments_needing_attention"] == 1
    assert response.json()["payments_needing_attention"][0]["details"][
        "amount_minor"] == 1800


async def test_a_quiet_morning_is_all_zeroes(client, admin_token):
    response = await client.get("/api/v1/admin/dashboard/attention",
                                headers={"Authorization": f"Bearer {admin_token}"})
    assert response.json()["counts"] == {
        "overdue": 0, "to_remove": 0, "payments_needing_attention": 0,
    }
    assert response.json()["overdue"] == []


async def test_the_queue_needs_the_bookings_read_permission(client, session):
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
        "/api/v1/admin/dashboard/attention",
        headers={"Authorization": f"Bearer {create_access_token('admin', admin.id)}"},
    )
    assert response.status_code == 403
