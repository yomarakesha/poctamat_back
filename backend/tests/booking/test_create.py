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
        "recipient_name": "Получатель", "deposited_by": "owner",
    }


async def test_booking_holds_a_cell_and_returns_the_deposit_code(
    client, session, book, admin_token, city, cell_type, postamat
):
    await _cells(session, postamat, cell_type, 2)
    await _tariffs(client, admin_token, city, cell_type)

    response = await book(_body(postamat, cell_type))
    assert response.status_code == 201
    body = response.json()
    assert body["status"] == BookingStatus.PENDING_PAYMENT
    assert body["cell_number"] == "1"
    assert body["price"] == {"amount_minor": 1800, "currency": "TMT"}
    # One grant, issued because it is usable now. The pickup code does not exist
    # until there is a parcel to collect.
    assert len(body["deposit_code"]) == 5 and body["deposit_code"].isdigit()
    assert body["pickup_code_sent_at"] is None
    assert body["hold_expires_at"].endswith("Z")


async def test_the_deposit_code_is_returned_once_and_never_again(
    client, session, book, client_token, admin_token, city, cell_type, postamat
):
    await _cells(session, postamat, cell_type, 1)
    await _tariffs(client, admin_token, city, cell_type)
    created = await book(_body(postamat, cell_type))

    detail = await client.get(f"/api/v1/bookings/{created.json()['id']}",
                              headers={"Authorization": f"Bearer {client_token}"})
    # The server keeps a keyed hash and genuinely cannot produce the digits
    # again, which is why rotation mints a new code rather than re-sending one.
    assert detail.json()["deposit_code"] is None
    assert created.json()["deposit_code"] not in detail.text


async def test_the_second_booking_takes_the_next_cell(
    client, session, book, admin_token, city, cell_type, postamat
):
    await _cells(session, postamat, cell_type, 2)
    await _tariffs(client, admin_token, city, cell_type)

    first = await book(_body(postamat, cell_type))
    second = await book(_body(postamat, cell_type))
    assert [first.json()["cell_number"], second.json()["cell_number"]] == ["1", "2"]


async def test_a_sold_out_size_is_409(
    client, session, book, admin_token, city, cell_type, postamat
):
    await _cells(session, postamat, cell_type, 1)
    await _tariffs(client, admin_token, city, cell_type)

    await book(_body(postamat, cell_type))
    sold_out = await book(_body(postamat, cell_type))
    assert sold_out.status_code == 409
    assert sold_out.json()["error"]["code"] == "NO_FREE_CELLS"


async def test_an_unpriced_size_cannot_be_booked(
    client, session, book, cell_type, postamat
):
    await _cells(session, postamat, cell_type, 1)
    response = await book(_body(postamat, cell_type))
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "DURATION_NOT_SUPPORTED"


async def test_a_blocked_postamat_cannot_be_booked(
    client, session, book, admin_token, city, cell_type, postamat
):
    await _cells(session, postamat, cell_type, 1)
    await _tariffs(client, admin_token, city, cell_type)
    await client.post(f"/api/v1/admin/postamats/{postamat.id}/block",
                      headers={"Authorization": f"Bearer {admin_token}"},
                      json={"reason": "vandalised"})

    response = await book(_body(postamat, cell_type))
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "POSTAMAT_BLOCKED"


async def test_the_sender_may_be_their_own_recipient(
    client, session, book, booking_client, admin_token, city, cell_type, postamat
):
    await _cells(session, postamat, cell_type, 2)
    await _tariffs(client, admin_token, city, cell_type)

    # Omitting the field is the «Пропустить» path: the sender collects it.
    skipped = await book(_body(postamat, cell_type) | {"recipient_phone": None})
    assert skipped.status_code == 201
    assert skipped.json()["recipient_phone"] == booking_client.phone

    # Typing their own number is a different act, and almost always a mistake.
    typed = await book(_body(postamat, cell_type)
                       | {"recipient_phone": booking_client.phone})
    assert typed.status_code == 422
    assert typed.json()["error"]["code"] == "RECIPIENT_PHONE_SAME_AS_SENDER"
