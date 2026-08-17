from app.modules.booking.models import BookingStatus
from app.modules.catalog.models import Cell


async def _cells(session, postamat, cell_type, count):
    rows = [
        Cell(postamat_id=postamat.id, cell_type_id=cell_type.id, number=n,
             board=1, output=n)
        for n in range(1, count + 1)
    ]
    session.add_all(rows)
    await session.commit()
    return rows


async def _tariffs(client, admin_token, city, cell_type):
    await client.put("/api/v1/admin/tariffs",
                     headers={"Authorization": f"Bearer {admin_token}"},
                     json={"entries": [
                         {"city_id": str(city.id), "cell_type_id": str(cell_type.id),
                          "duration_hours": hours, "amount_minor": amount}
                         for hours, amount in ((12, 1200), (24, 1800), (48, 2600))
                     ]})


def _body(postamat, cell_type):
    return {
        "postamat_id": str(postamat.id), "cell_type_id": str(cell_type.id),
        "duration_hours": 24, "recipient_phone": "+99365000001",
        "recipient_name": "Получатель", "depositor": "owner",
    }


async def test_booking_holds_a_cell_and_returns_three_codes(
    client, session, client_token, admin_token, city, cell_type, postamat
):
    await _cells(session, postamat, cell_type, 2)
    await _tariffs(client, admin_token, city, cell_type)

    response = await client.post("/api/v1/bookings", json=_body(postamat, cell_type),
                                 headers={"Authorization": f"Bearer {client_token}"})
    assert response.status_code == 201
    body = response.json()
    assert body["status"] == BookingStatus.PENDING_PAYMENT
    assert body["cell_number"] == 1
    assert body["amount_minor"] == 1800
    assert set(body["codes"]) == {"deposit", "courier", "pickup"}
    assert all(len(code) == 5 and code.isdigit() for code in body["codes"].values())
    assert body["hold_expires_at"].endswith("Z")


async def test_the_second_booking_takes_the_next_cell(
    client, session, client_token, admin_token, city, cell_type, postamat
):
    await _cells(session, postamat, cell_type, 2)
    await _tariffs(client, admin_token, city, cell_type)
    headers = {"Authorization": f"Bearer {client_token}"}

    first = await client.post("/api/v1/bookings", json=_body(postamat, cell_type),
                              headers=headers)
    second = await client.post("/api/v1/bookings", json=_body(postamat, cell_type),
                               headers=headers)
    assert [first.json()["cell_number"], second.json()["cell_number"]] == [1, 2]


async def test_a_sold_out_size_is_409(
    client, session, client_token, admin_token, city, cell_type, postamat
):
    await _cells(session, postamat, cell_type, 1)
    await _tariffs(client, admin_token, city, cell_type)
    headers = {"Authorization": f"Bearer {client_token}"}

    await client.post("/api/v1/bookings", json=_body(postamat, cell_type), headers=headers)
    sold_out = await client.post("/api/v1/bookings", json=_body(postamat, cell_type),
                                 headers=headers)
    assert sold_out.status_code == 409
    assert sold_out.json()["error"]["code"] == "SIZE_SOLD_OUT"


async def test_an_unpriced_size_cannot_be_booked(
    client, session, client_token, cell_type, postamat
):
    await _cells(session, postamat, cell_type, 1)
    response = await client.post("/api/v1/bookings", json=_body(postamat, cell_type),
                                 headers={"Authorization": f"Bearer {client_token}"})
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "TARIFF_INCOMPLETE"


async def test_a_blocked_postamat_cannot_be_booked(
    client, session, client_token, admin_token, city, cell_type, postamat
):
    await _cells(session, postamat, cell_type, 1)
    await _tariffs(client, admin_token, city, cell_type)
    await client.post(f"/api/v1/admin/postamats/{postamat.id}/block",
                      headers={"Authorization": f"Bearer {admin_token}"},
                      json={"reason": "vandalised"})

    response = await client.post("/api/v1/bookings", json=_body(postamat, cell_type),
                                 headers={"Authorization": f"Bearer {client_token}"})
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "POSTAMAT_BLOCKED"


async def test_the_timeline_starts_with_the_booking_itself(
    client, session, client_token, admin_token, city, cell_type, postamat
):
    await _cells(session, postamat, cell_type, 1)
    await _tariffs(client, admin_token, city, cell_type)
    created = await client.post("/api/v1/bookings", json=_body(postamat, cell_type),
                                headers={"Authorization": f"Bearer {client_token}"})

    detail = await client.get(f"/api/v1/bookings/{created.json()['id']}",
                              headers={"Authorization": f"Bearer {client_token}"})
    assert [event["status"] for event in detail.json()["timeline"]] == ["pending_payment"]
    assert "codes" not in detail.json()
