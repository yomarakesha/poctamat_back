import uuid

from app.modules.catalog.models import Cell, CellType
from tests.helpers import set_tariffs

PATH = "/api/v1/admin/cell-types"


def _key():
    return {"Idempotency-Key": str(uuid.uuid4())}


async def test_the_list_shows_dimensions_and_cell_counts(
    client, session, admin_token, cell_type, postamat
):
    session.add_all([
        Cell(postamat_id=postamat.id, cell_type_id=cell_type.id, number=n,
             board=1, output=n)
        for n in (1, 2, 3)
    ])
    await session.commit()

    response = await client.get(PATH,
                                headers={"Authorization": f"Bearer {admin_token}"})
    assert response.status_code == 200
    item = response.json()["items"][0]
    # Millimetres, as the contract types them.
    assert (item["width_mm"], item["height_mm"], item["depth_mm"]) == (200, 200, 400)
    assert item["cell_count"] == 3
    assert item["is_active"] is True


async def test_a_size_can_be_added_without_a_release(client, session, admin_token):
    headers = {"Authorization": f"Bearer {admin_token}"} | _key()
    response = await client.post(PATH, headers=headers, json={
        "code": "xl", "name": "Огромный",
        "width_mm": 600, "height_mm": 600, "depth_mm": 800,
    })
    assert response.status_code == 201
    body = response.json()
    assert body["code"] == "xl"
    # One input on the panel fills every language rather than leaving two stale.
    assert body["name_tk"] == body["name_ru"] == body["name_en"] == "Огромный"

    created = await session.get(CellType, uuid.UUID(body["id"]))
    assert created.width_mm == 600


async def test_a_duplicate_code_is_refused(client, admin_token, cell_type):
    response = await client.post(
        PATH, headers={"Authorization": f"Bearer {admin_token}"} | _key(),
        json={"code": cell_type.code, "name": "Копия", "width_mm": 100,
              "height_mm": 100, "depth_mm": 100},
    )
    assert response.status_code == 409


async def test_a_size_without_dimensions_is_refused(client, admin_token):
    response = await client.post(
        PATH, headers={"Authorization": f"Bearer {admin_token}"} | _key(),
        json={"code": "flat", "name": "Плоский"},
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"


async def test_editing_one_language_leaves_the_others(client, admin_token, cell_type):
    response = await client.patch(
        f"{PATH}/{cell_type.id}",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={"name_ru": "Небольшой"},
    )
    assert response.status_code == 200
    assert response.json()["name_ru"] == "Небольшой"
    assert response.json()["name_tk"] == cell_type.name_tk


async def test_blocking_stops_new_bookings_of_that_size(
    client, session, admin_token, cell_type, postamat
):
    headers = {"Authorization": f"Bearer {admin_token}"}
    blocked = await client.post(f"{PATH}/{cell_type.id}/block",
                                headers=headers | _key())
    assert blocked.status_code == 200
    assert blocked.json()["is_active"] is False
    assert blocked.json()["blocked_at"].endswith("Z")

    # Blocked, not deleted: the row is still there and its cells keep their type.
    listed = await client.get(PATH, headers=headers)
    assert listed.json()["items"] == []
    with_blocked = await client.get(f"{PATH}?include_blocked=true", headers=headers)
    assert len(with_blocked.json()["items"]) == 1


async def test_a_size_behind_a_live_booking_cannot_be_blocked(
    client, session, book, admin_token, city, cell_type, postamat
):
    session.add(Cell(postamat_id=postamat.id, cell_type_id=cell_type.id, number=1,
                     board=1, output=1))
    await session.commit()
    headers = {"Authorization": f"Bearer {admin_token}"}
    # Every duration, because a half-priced matrix is refused whole.
    await set_tariffs(client, admin_token, city, cell_type,
                      [(hours, 1800) for hours in (12, 24, 48)])
    created = await book({"postamat_id": str(postamat.id),
                          "cell_type_id": str(cell_type.id), "duration_hours": 24,
                          "recipient_phone": "+99365000001"})
    assert created.status_code == 201, created.text

    response = await client.post(f"{PATH}/{cell_type.id}/block",
                                 headers=headers | _key())
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "CELL_TYPE_IN_USE"
    assert response.json()["error"]["details"]["active_bookings"] == 1


async def test_unblocking_puts_the_size_back_on_sale(client, admin_token, cell_type):
    headers = {"Authorization": f"Bearer {admin_token}"}
    await client.post(f"{PATH}/{cell_type.id}/block", headers=headers | _key())

    response = await client.post(f"{PATH}/{cell_type.id}/unblock",
                                 headers=headers | _key())
    assert response.status_code == 200
    assert response.json()["is_active"] is True
    assert response.json()["blocked_at"] is None
