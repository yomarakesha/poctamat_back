from app.modules.booking.models import Booking, BookingStatus
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


async def _price(client, admin_token, city, cell_type):
    await client.put("/api/v1/admin/tariffs",
                     headers={"Authorization": f"Bearer {admin_token}"},
                     json={"entries": [
                         {"city_id": str(city.id), "cell_type_id": str(cell_type.id),
                          "duration_hours": hours, "amount_minor": amount}
                         for hours, amount in ((12, 1200), (24, 1800), (48, 2600))
                     ]})


async def test_availability_counts_free_cells_and_lists_prices(
    client, session, admin_token, city, cell_type, postamat
):
    await _cells(session, postamat, cell_type, 4)
    await _price(client, admin_token, city, cell_type)

    response = await client.get(f"/api/v1/postamats/{postamat.id}/availability")
    assert response.status_code == 200
    item = response.json()["items"][0]
    assert item["code"] == cell_type.code
    assert item["free"] == 4
    assert {price["duration_hours"] for price in item["prices"]} == {12, 24, 48}
    assert [p["amount_minor"] for p in item["prices"] if p["duration_hours"] == 24] == [1800]


async def test_a_held_cell_is_not_free(
    client, session, admin_token, city, cell_type, postamat
):
    rows = await _cells(session, postamat, cell_type, 2)
    await _price(client, admin_token, city, cell_type)
    session.add(Booking(
        client_id=postamat.id, postamat_id=postamat.id, cell_id=rows[0].id,
        cell_type_id=cell_type.id, duration_hours=24, amount_minor=1800,
        status=BookingStatus.AWAITING_PICKUP, recipient_phone="+99362123456",
    ))
    await session.commit()

    response = await client.get(f"/api/v1/postamats/{postamat.id}/availability")
    assert response.json()["items"][0]["free"] == 1


async def test_a_finished_booking_does_not_hold_a_cell(
    client, session, admin_token, city, cell_type, postamat
):
    rows = await _cells(session, postamat, cell_type, 1)
    await _price(client, admin_token, city, cell_type)
    session.add(Booking(
        client_id=postamat.id, postamat_id=postamat.id, cell_id=rows[0].id,
        cell_type_id=cell_type.id, duration_hours=24, amount_minor=1800,
        status=BookingStatus.COMPLETED, recipient_phone="+99362123456",
    ))
    await session.commit()

    response = await client.get(f"/api/v1/postamats/{postamat.id}/availability")
    assert response.json()["items"][0]["free"] == 1


async def test_a_blocked_cell_is_not_offered(
    client, session, admin_token, city, cell_type, postamat
):
    rows = await _cells(session, postamat, cell_type, 2)
    rows[0].is_blocked = True
    await session.commit()
    await _price(client, admin_token, city, cell_type)

    response = await client.get(f"/api/v1/postamats/{postamat.id}/availability")
    assert response.json()["items"][0]["free"] == 1


async def test_availability_of_a_blocked_postamat_is_404(
    client, admin_token, cell_type, postamat
):
    await client.post(f"/api/v1/admin/postamats/{postamat.id}/block",
                      headers={"Authorization": f"Bearer {admin_token}"},
                      json={"reason": "vandalised"})
    response = await client.get(f"/api/v1/postamats/{postamat.id}/availability")
    assert response.status_code == 404
