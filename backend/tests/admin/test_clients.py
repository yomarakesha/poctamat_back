import uuid

import pytest

from app.modules.catalog.models import Cell
from app.modules.identity.models import Client


@pytest.fixture
async def staff_token(session):
    """An operator who may read and write clients."""
    from app.core.security import create_access_token, hash_secret
    from app.modules.identity.models import AdminUser, Role

    role = Role(code="support", name="Поддержка",
                permissions=["clients.read", "clients.write"])
    session.add(role)
    await session.flush()
    admin = AdminUser(login="support_test", full_name="Поддержкин П.П.",
                      password_hash=hash_secret("secret123"), role_id=role.id)
    session.add(admin)
    await session.commit()
    await session.refresh(admin)
    return create_access_token("admin", admin.id)


def _key():
    return {"Idempotency-Key": str(uuid.uuid4())}


async def _ready(client, session, admin_token, city, cell_type, postamat):
    session.add(Cell(postamat_id=postamat.id, cell_type_id=cell_type.id, number=7,
                     board=1, output=7))
    await session.commit()
    await client.put("/api/v1/admin/tariffs",
                     headers={"Authorization": f"Bearer {admin_token}"},
                     json={"entries": [
                         {"city_id": str(city.id), "cell_type_id": str(cell_type.id),
                          "duration_hours": hours, "amount_minor": 1800}
                         for hours in (12, 24, 48)
                     ]})


async def test_the_table_shows_a_clients_live_bookings(
    client, session, book, staff_token, admin_token, city, cell_type, postamat,
    booking_client,
):
    await _ready(client, session, admin_token, city, cell_type, postamat)
    created = await book({"postamat_id": str(postamat.id),
                          "cell_type_id": str(cell_type.id), "duration_hours": 24,
                          "recipient_phone": "+99365000001"})
    assert created.status_code == 201

    listed = await client.get("/api/v1/admin/clients",
                              headers={"Authorization": f"Bearer {staff_token}"})
    assert listed.status_code == 200
    row = next(item for item in listed.json()["items"]
               if item["id"] == str(booking_client.id))
    assert row["full_name"] == "Отправителев Мырат"
    assert row["status"] == "active"
    # Rendered together as «10001[007]» in the table.
    assert row["active_bookings"] == [{
        "booking_id": created.json()["id"],
        "postamat_number": postamat.number,
        "cell_number": "7",
    }]


async def test_the_table_can_be_searched_and_filtered(
    client, session, staff_token, booking_client
):
    session.add(Client(phone="+99361000042", last_name="Гулыев",
                       first_name="Сердар", is_blocked=True))
    await session.commit()
    headers = {"Authorization": f"Bearer {staff_token}"}

    by_name = await client.get("/api/v1/admin/clients?query=Гулы", headers=headers)
    assert [item["phone"] for item in by_name.json()["items"]] == ["+99361000042"]

    by_phone = await client.get("/api/v1/admin/clients?phone=1000001", headers=headers)
    assert [item["id"] for item in by_phone.json()["items"]] == [str(booking_client.id)]

    blocked = await client.get("/api/v1/admin/clients?status=blocked", headers=headers)
    assert [item["phone"] for item in blocked.json()["items"]] == ["+99361000042"]


async def test_one_client_carries_the_detail_fields(
    client, staff_token, booking_client
):
    response = await client.get(f"/api/v1/admin/clients/{booking_client.id}",
                                headers={"Authorization": f"Bearer {staff_token}"})
    assert response.status_code == 200
    body = response.json()
    assert body["language"] == "tk"
    assert body["blocked_reason"] is None
    assert body["blocked_at"] is None
    assert body["total_bookings"] == 0


async def test_an_unknown_client_is_404(client, staff_token):
    response = await client.get(f"/api/v1/admin/clients/{uuid.uuid4()}",
                                headers={"Authorization": f"Bearer {staff_token}"})
    assert response.status_code == 404


async def test_blocking_records_who_and_why(
    client, session, staff_token, booking_client
):
    from sqlalchemy import select

    from app.modules.audit.models import AuditEntry

    response = await client.post(
        f"/api/v1/admin/clients/{booking_client.id}/block",
        headers={"Authorization": f"Bearer {staff_token}"} | _key(),
        json={"reason": "мошенничество"},
    )
    assert response.status_code == 200
    assert response.json()["status"] == "blocked"
    assert response.json()["blocked_reason"] == "мошенничество"
    assert response.json()["blocked_at"].endswith("Z")

    entry = await session.scalar(
        select(AuditEntry).where(AuditEntry.event == "client.blocked")
    )
    assert entry.actor == "support_test"
    assert entry.details["reason"] == "мошенничество"


async def test_a_block_without_a_reason_is_refused(
    client, staff_token, booking_client
):
    response = await client.post(
        f"/api/v1/admin/clients/{booking_client.id}/block",
        headers={"Authorization": f"Bearer {staff_token}"} | _key(), json={},
    )
    # A block nobody can review later is not a decision, it is an accident.
    assert response.status_code == 422


async def test_a_blocked_clients_live_session_dies_at_the_next_refresh(
    client, session, staff_token, booking_client
):
    from app.modules.identity import service

    _, refresh = await service.issue_token_pair(session, "client", booking_client.id)
    await session.commit()

    await client.post(f"/api/v1/admin/clients/{booking_client.id}/block",
                      headers={"Authorization": f"Bearer {staff_token}"} | _key(),
                      json={"reason": "мошенничество"})

    # The access token still has minutes to live, but it cannot be extended: the
    # refresh path checks the subject, not only the token.
    response = await client.post("/api/v1/auth/refresh",
                                 json={"refresh_token": refresh})
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "CLIENT_BLOCKED"


async def test_unblocking_clears_the_reason(client, staff_token, booking_client):
    headers = {"Authorization": f"Bearer {staff_token}"}
    await client.post(f"/api/v1/admin/clients/{booking_client.id}/block",
                      headers=headers | _key(), json={"reason": "ошибка"})

    response = await client.post(
        f"/api/v1/admin/clients/{booking_client.id}/unblock",
        headers=headers | _key(),
    )
    assert response.status_code == 200
    assert response.json()["status"] == "active"
    assert response.json()["blocked_reason"] is None
    assert response.json()["blocked_at"] is None


async def test_reading_clients_needs_the_permission(client, admin_token):
    response = await client.get("/api/v1/admin/clients",
                                headers={"Authorization": f"Bearer {admin_token}"})
    assert response.status_code == 403
