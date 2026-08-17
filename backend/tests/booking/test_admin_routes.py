from app.modules.booking.models import BookingStatus
from app.modules.catalog.models import Cell


async def _booked(client, session, client_token, admin_token, city, cell_type, postamat):
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
    created = await client.post(
        "/api/v1/bookings",
        json={"postamat_id": str(postamat.id), "cell_type_id": str(cell_type.id),
              "duration_hours": 24, "recipient_phone": "+99365000001"},
        headers={"Authorization": f"Bearer {client_token}"},
    )
    return created.json()


async def test_admin_sees_every_booking_and_can_filter_by_status(
    client, session, client_token, admin_token, city, cell_type, postamat
):
    booking = await _booked(client, session, client_token, admin_token, city,
                            cell_type, postamat)
    headers = {"Authorization": f"Bearer {admin_token}"}

    listed = await client.get("/api/v1/admin/bookings", headers=headers)
    assert [item["id"] for item in listed.json()["items"]] == [booking["id"]]

    filtered = await client.get("/api/v1/admin/bookings?status=completed",
                                headers=headers)
    assert filtered.json()["items"] == []


async def test_admin_detail_carries_the_timeline_but_no_plaintext_codes(
    client, session, client_token, admin_token, city, cell_type, postamat
):
    booking = await _booked(client, session, client_token, admin_token, city,
                            cell_type, postamat)
    response = await client.get(f"/api/v1/admin/bookings/{booking['id']}",
                                headers={"Authorization": f"Bearer {admin_token}"})
    assert response.status_code == 200
    assert "codes" not in response.json()
    assert [event["status"] for event in response.json()["timeline"]] == ["pending_payment"]


async def test_admin_force_cancel_records_who_did_it(
    client, session, client_token, admin_token, admin_user, city, cell_type, postamat
):
    from sqlalchemy import select

    from app.modules.audit.models import AuditEntry

    booking = await _booked(client, session, client_token, admin_token, city,
                            cell_type, postamat)
    response = await client.post(f"/api/v1/admin/bookings/{booking['id']}/cancel",
                                 headers={"Authorization": f"Bearer {admin_token}"},
                                 json={"reason": "жалоба клиента"})
    assert response.status_code == 200
    assert response.json()["status"] == BookingStatus.CANCELLED
    assert response.json()["timeline"][-1]["message"] == "Отменено"

    entry = await session.scalar(
        select(AuditEntry).where(AuditEntry.event == "booking.cancelled")
    )
    assert entry is not None
    assert entry.actor == admin_user.login
    assert entry.message == "жалоба клиента"


async def test_bookings_need_the_read_permission(client, session, postamat):
    from app.core.security import create_access_token
    from app.modules.identity.models import AdminUser, Role

    role = Role(code="viewer", name="Viewer", permissions=["postamats.read"])
    session.add(role)
    await session.flush()
    admin = AdminUser(login="viewer_test", full_name="Смотров С.С.",
                      password_hash="x", role_id=role.id)
    session.add(admin)
    await session.commit()
    await session.refresh(admin)

    response = await client.get(
        "/api/v1/admin/bookings",
        headers={"Authorization": f"Bearer {create_access_token('admin', admin.id)}"},
    )
    assert response.status_code == 403
