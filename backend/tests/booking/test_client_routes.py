from app.modules.booking.models import BookingStatus
from app.modules.catalog.models import Cell


async def _ready(client, session, admin_token, city, cell_type, postamat, cells=2):
    session.add_all([
        Cell(postamat_id=postamat.id, cell_type_id=cell_type.id, number=n,
             board=1, output=n)
        for n in range(1, cells + 1)
    ])
    await session.commit()
    await client.put("/api/v1/admin/tariffs",
                     headers={"Authorization": f"Bearer {admin_token}"},
                     json={"entries": [
                         {"city_id": str(city.id), "cell_type_id": str(cell_type.id),
                          "duration_hours": hours, "amount_minor": 1800}
                         for hours in (12, 24, 48)
                     ]})


def _body(postamat, cell_type):
    return {"postamat_id": str(postamat.id), "cell_type_id": str(cell_type.id),
            "duration_hours": 24, "recipient_phone": "+99365000001",
            "deposited_by": "courier", "courier_phone": "+99366000002"}


async def test_the_list_shows_the_clients_own_bookings_newest_first(
    client, session, book, client_token, admin_token, city, cell_type, postamat
):
    await _ready(client, session, admin_token, city, cell_type, postamat)
    first = await book(_body(postamat, cell_type))
    second = await book(_body(postamat, cell_type))

    listed = await client.get("/api/v1/bookings",
                              headers={"Authorization": f"Bearer {client_token}"})
    assert listed.status_code == 200
    assert [item["id"] for item in listed.json()["items"]] == [
        second.json()["id"], first.json()["id"]
    ]
    assert listed.json()["pagination"]["has_more"] is False


async def test_a_list_item_carries_what_the_card_draws(
    client, session, book, client_token, admin_token, city, cell_type, postamat
):
    await _ready(client, session, admin_token, city, cell_type, postamat)
    await book(_body(postamat, cell_type))

    listed = await client.get("/api/v1/bookings",
                              headers={"Authorization": f"Bearer {client_token}"})
    item = listed.json()["items"][0]
    assert item["postamat_name"] == postamat.name
    # A string, because a door label is not arithmetic.
    assert item["cell_number"] == "1"
    # Turkmen is the default language, and the name is served in it unless the
    # request asks otherwise.
    assert item["cell_type_name"] == cell_type.name_tk
    assert item["created_at"].endswith("Z")


async def test_another_clients_booking_is_invisible(
    client, session, book, admin_token, city, cell_type, postamat
):
    from app.core.security import create_access_token
    from app.modules.identity.models import Client

    await _ready(client, session, admin_token, city, cell_type, postamat)
    created = await book(_body(postamat, cell_type))

    stranger = Client(phone="+99361000009", last_name="Чужой", first_name="Чужой")
    session.add(stranger)
    await session.commit()
    await session.refresh(stranger)
    token = create_access_token("client", stranger.id)

    response = await client.get(f"/api/v1/bookings/{created.json()['id']}",
                                headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 404

    listed = await client.get("/api/v1/bookings",
                              headers={"Authorization": f"Bearer {token}"})
    assert listed.json()["items"] == []


async def test_cancelling_frees_the_cell_for_the_next_booking(
    client, session, book, cancel, client_token, admin_token, city, cell_type, postamat
):
    await _ready(client, session, admin_token, city, cell_type, postamat, cells=1)
    created = await book(_body(postamat, cell_type))

    sold_out = await book(_body(postamat, cell_type))
    assert sold_out.status_code == 409
    assert sold_out.json()["error"]["code"] == "NO_FREE_CELLS"

    cancelled = await cancel(created.json()["id"])
    assert cancelled.status_code == 200
    assert cancelled.json()["status"] == BookingStatus.CANCELLED

    again = await book(_body(postamat, cell_type))
    assert again.status_code == 201


async def test_cancelling_twice_is_refused_with_its_own_code(
    client, session, book, cancel, admin_token, city, cell_type, postamat
):
    await _ready(client, session, admin_token, city, cell_type, postamat)
    created = await book(_body(postamat, cell_type))
    await cancel(created.json()["id"])

    again = await cancel(created.json()["id"])
    assert again.status_code == 409
    assert again.json()["error"]["code"] == "BOOKING_ALREADY_CANCELLED"


async def test_scope_splits_active_from_history(
    client, session, book, cancel, client_token, admin_token, city, cell_type, postamat
):
    await _ready(client, session, admin_token, city, cell_type, postamat, cells=2)
    headers = {"Authorization": f"Bearer {client_token}"}
    live = await book(_body(postamat, cell_type))
    done = await book(_body(postamat, cell_type))
    await cancel(done.json()["id"])

    active = await client.get("/api/v1/bookings?scope=active", headers=headers)
    assert [item["id"] for item in active.json()["items"]] == [live.json()["id"]]

    history = await client.get("/api/v1/bookings?scope=history", headers=headers)
    assert [item["id"] for item in history.json()["items"]] == [done.json()["id"]]


async def test_the_timeline_returns_every_step_in_order(
    client, session, book, client_token, admin_token, city, cell_type, postamat
):
    await _ready(client, session, admin_token, city, cell_type, postamat)
    created = await book(_body(postamat, cell_type))

    response = await client.get(
        f"/api/v1/bookings/{created.json()['id']}/timeline",
        headers={"Authorization": f"Bearer {client_token}"},
    )
    assert response.status_code == 200
    items = response.json()["items"]
    assert [item["step"] for item in items] == [
        "booked", "paid", "parcel_deposited", "pickup_code_sent", "collected",
        "expired", "cancelled",
    ]
    # Only the first has happened; the rest are drawn greyed out in this order.
    assert items[0]["occurred_at"] is not None
    assert all(item["occurred_at"] is None for item in items[1:])
