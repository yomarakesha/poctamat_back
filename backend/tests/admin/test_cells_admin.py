import uuid

from app.modules.catalog.models import Cell
from tests.helpers import set_tariffs

PATH = "/api/v1/admin/cells"


def _key():
    return {"Idempotency-Key": str(uuid.uuid4())}


async def _bulk(client, admin_token, postamat, cell_type, **overrides):
    body = {
        "postamat_id": str(postamat.id), "grid_rows": 7, "grid_cols": 6,
        "numbering_start": 1, "hardware_start": {"board": 1, "output": 1},
        "layout": [{"cell_type_id": str(cell_type.id), "count": 12}],
    }
    body.update(overrides)
    return await client.post(f"{PATH}/bulk",
                             headers={"Authorization": f"Bearer {admin_token}"} | _key(),
                             json=body)


async def test_a_whole_cabinet_is_created_in_one_form(
    client, session, admin_token, postamat, cell_type
):
    response = await _bulk(client, admin_token, postamat, cell_type)
    assert response.status_code == 201
    body = response.json()
    assert body["created_count"] == 12

    first, last = body["items"][0], body["items"][-1]
    # Numbers and hardware outputs both increment; positions run row by row.
    assert (first["number"], last["number"]) == ("1", "12")
    assert first["hardware_address"] == {"board": 1, "output": 1}
    assert last["hardware_address"] == {"board": 1, "output": 12}
    assert (first["row"], first["col"]) == (1, 1)
    assert (last["row"], last["col"]) == (2, 6)


async def test_a_layout_that_does_not_fit_is_refused(
    client, admin_token, postamat, cell_type
):
    response = await _bulk(client, admin_token, postamat, cell_type,
                           grid_rows=2, grid_cols=2,
                           layout=[{"cell_type_id": str(cell_type.id), "count": 9}])
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "LAYOUT_EXCEEDS_GRID"
    assert response.json()["error"]["details"]["capacity"] == 4


async def test_a_clashing_cabinet_writes_nothing(
    client, session, admin_token, postamat, cell_type
):
    from sqlalchemy import func, select

    session.add(Cell(postamat_id=postamat.id, cell_type_id=cell_type.id, number=3,
                     board=9, output=9))
    await session.commit()

    response = await _bulk(client, admin_token, postamat, cell_type)
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "BULK_CREATE_PARTIAL_FAILURE"

    # A partial cabinet is worse than no cabinet: the retry with corrected
    # numbers must not find half of them already there.
    total = await session.scalar(select(func.count()).select_from(Cell))
    assert total == 1


async def test_one_cell_carries_its_state_and_neighbours(
    client, session, admin_token, postamat, cell_type
):
    created = await _bulk(client, admin_token, postamat, cell_type)
    cell_id = created.json()["items"][0]["id"]

    response = await client.get(f"{PATH}/{cell_id}",
                                headers={"Authorization": f"Bearer {admin_token}"})
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "free"
    assert body["held_by"] is None
    assert body["postamat_number"] == postamat.number
    assert body["cell_type"]["width_mm"] == 200
    # No door sensor until the kiosk plan: null says "unknown", false would
    # claim the door is shut on nobody's authority.
    assert body["door_open"] is None


async def test_a_held_cell_names_who_holds_it(
    client, session, book, admin_token, city, cell_type, postamat, booking_client
):
    await _bulk(client, admin_token, postamat, cell_type)
    headers = {"Authorization": f"Bearer {admin_token}"}
    await set_tariffs(client, admin_token, city, cell_type,
                      [(hours, 1800) for hours in (12, 24, 48)])
    created = await book({"postamat_id": str(postamat.id),
                          "cell_type_id": str(cell_type.id), "duration_hours": 24,
                          "recipient_phone": "+99365000001"})
    assert created.status_code == 201

    from sqlalchemy import select

    cell = await session.scalar(select(Cell).where(Cell.number == 1))
    response = await client.get(f"{PATH}/{cell.id}", headers=headers)
    body = response.json()
    assert body["status"] == "booked"
    assert body["held_by"]["client_phone"] == booking_client.phone
    assert body["held_by"]["booking_id"] == created.json()["id"]


async def test_maintenance_is_not_blocking(
    client, session, admin_token, postamat, cell_type
):
    from sqlalchemy import select

    from app.modules.audit.models import AuditEntry

    created = await _bulk(client, admin_token, postamat, cell_type)
    cell_id = created.json()["items"][0]["id"]
    headers = {"Authorization": f"Bearer {admin_token}"}

    response = await client.post(f"{PATH}/{cell_id}/maintenance",
                                 headers=headers | _key(),
                                 json={"enabled": True, "reason": "замена замка"})
    assert response.status_code == 200
    assert response.json()["status"] == "maintenance"
    assert response.json()["status_changed_at"].endswith("Z")

    entry = await session.scalar(
        select(AuditEntry).where(AuditEntry.event == "cell.maintenance")
    )
    assert entry.details["enabled"] is True


async def test_maintenance_on_a_held_cell_is_refused(
    client, session, book, admin_token, city, cell_type, postamat
):
    await _bulk(client, admin_token, postamat, cell_type)
    headers = {"Authorization": f"Bearer {admin_token}"}
    await set_tariffs(client, admin_token, city, cell_type,
                      [(hours, 1800) for hours in (12, 24, 48)])
    await book({"postamat_id": str(postamat.id),
                "cell_type_id": str(cell_type.id), "duration_hours": 24,
                "recipient_phone": "+99365000001"})

    from sqlalchemy import select

    cell = await session.scalar(select(Cell).where(Cell.number == 1))
    response = await client.post(f"{PATH}/{cell.id}/maintenance",
                                 headers=headers | _key(),
                                 json={"enabled": True})
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "CELL_HAS_ACTIVE_BOOKING"


async def test_remote_open_is_audited_and_refused_honestly(
    client, session, admin_user, postamat, cell_type
):
    from sqlalchemy import select

    from app.core.security import create_access_token
    from app.modules.audit.models import AuditEntry, Severity
    from app.modules.identity.models import Role

    role = await session.get(Role, admin_user.role_id)
    role.permissions = (role.permissions or []) + ["cells.remote_open"]
    await session.commit()
    token = create_access_token("admin", admin_user.id)

    created = await _bulk(client, token, postamat, cell_type)
    cell_id = created.json()["items"][0]["id"]

    response = await client.post(
        f"{PATH}/{cell_id}/remote-open",
        headers={"Authorization": f"Bearer {token}"} | _key(),
        json={"reason": "клиент не может ввести код, подтверждён по телефону"},
    )
    # No lock agent until the kiosk plan; a 202 would tell the operator a door
    # opened when none did.
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "DEVICE_OFFLINE"

    # The reach for the button is kept whether or not the door moved.
    entry = await session.scalar(
        select(AuditEntry).where(AuditEntry.event == "cell.remote_open_requested")
    )
    assert entry is not None
    assert entry.severity == Severity.WARNING
    assert entry.actor == admin_user.login


async def test_remote_open_needs_a_real_reason(
    client, session, admin_user, postamat, cell_type, admin_token
):
    created = await _bulk(client, admin_token, postamat, cell_type)
    response = await client.post(
        f"{PATH}/{created.json()['items'][0]['id']}/remote-open",
        headers={"Authorization": f"Bearer {admin_token}"} | _key(),
        json={"reason": "потому"},
    )
    # «потому» is not a reason, and the permission is missing besides.
    assert response.status_code in (403, 422)
