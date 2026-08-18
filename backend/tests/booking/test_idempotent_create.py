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
            "duration_hours": 24, "recipient_phone": "+99365000001"}


async def test_booking_without_an_idempotency_key_is_refused(
    client, session, client_token, admin_token, city, cell_type, postamat
):
    await _ready(client, session, admin_token, city, cell_type, postamat)
    response = await client.post("/api/v1/bookings", json=_body(postamat, cell_type),
                                 headers={"Authorization": f"Bearer {client_token}"})
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "IDEMPOTENCY_KEY_REQUIRED"


async def test_a_repeated_request_returns_the_first_booking_and_one_cell(
    client, session, client_token, admin_token, city, cell_type, postamat
):
    # A double tap on the booking button costs a real cell for ten minutes, so
    # the second request has to replay the first rather than allocate again.
    await _ready(client, session, admin_token, city, cell_type, postamat)
    headers = {"Authorization": f"Bearer {client_token}", "Idempotency-Key": "tap-1"}

    first = await client.post("/api/v1/bookings", json=_body(postamat, cell_type),
                              headers=headers)
    second = await client.post("/api/v1/bookings", json=_body(postamat, cell_type),
                               headers=headers)

    assert first.status_code == 201
    assert second.status_code == 201
    assert second.json()["id"] == first.json()["id"]
    assert second.json()["deposit_code"] == first.json()["deposit_code"]

    availability = await client.get(f"/api/v1/postamats/{postamat.id}/availability")
    assert availability.json()["items"][0]["free"] == 1


async def test_the_same_key_from_another_client_is_a_separate_booking(
    client, session, client_token, admin_token, city, cell_type, postamat
):
    # Keys are client-chosen and clients share an address behind NAT. Ownership
    # comes from the token, so two people picking "1" get their own bookings
    # instead of one reading the other's cell number and PINs.
    from app.core.security import create_access_token
    from app.modules.identity.models import Client

    await _ready(client, session, admin_token, city, cell_type, postamat)
    other = Client(phone="+99361000077", last_name="Второй", first_name="Второй")
    session.add(other)
    await session.commit()
    await session.refresh(other)

    first = await client.post(
        "/api/v1/bookings", json=_body(postamat, cell_type),
        headers={"Authorization": f"Bearer {client_token}", "Idempotency-Key": "1"},
    )
    second = await client.post(
        "/api/v1/bookings", json=_body(postamat, cell_type),
        headers={"Authorization": f"Bearer {create_access_token('client', other.id)}",
                 "Idempotency-Key": "1"},
    )

    assert first.status_code == 201
    assert second.status_code == 201
    assert second.json()["id"] != first.json()["id"]
    assert second.json()["cell_number"] != first.json()["cell_number"]


async def test_the_same_key_with_a_changed_body_is_a_conflict(
    client, session, client_token, admin_token, city, cell_type, postamat
):
    await _ready(client, session, admin_token, city, cell_type, postamat)
    headers = {"Authorization": f"Bearer {client_token}", "Idempotency-Key": "tap-2"}
    await client.post("/api/v1/bookings", json=_body(postamat, cell_type),
                      headers=headers)

    changed = _body(postamat, cell_type) | {"duration_hours": 48}
    response = await client.post("/api/v1/bookings", json=changed, headers=headers)
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "IDEMPOTENCY_KEY_REUSED"
