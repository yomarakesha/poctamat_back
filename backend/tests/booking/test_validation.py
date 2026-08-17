from app.modules.catalog.models import Cell


async def _ready(client, session, admin_token, city, cell_type, postamat):
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


def _body(postamat, cell_type, **overrides):
    body = {"postamat_id": str(postamat.id), "cell_type_id": str(cell_type.id),
            "duration_hours": 24, "recipient_phone": "+99365000001"}
    body.update(overrides)
    return body


async def test_a_duration_outside_the_product_constant_is_rejected(
    client, session, book, admin_token, city, cell_type, postamat
):
    # 36 hours is not a thing a customer can buy, and a tariff for it cannot
    # exist. Saying so at validation names the field instead of blaming pricing.
    await _ready(client, session, admin_token, city, cell_type, postamat)
    response = await book(_body(postamat, cell_type, duration_hours=36))
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_FAILED"
    fields = [item["field"] for item in response.json()["error"]["details"]["fields"]]
    assert "duration_hours" in fields


async def test_a_courier_booking_without_a_courier_phone_is_rejected(
    client, session, book, admin_token, city, cell_type, postamat
):
    # The courier's PIN is delivered by SMS. Accepting the booking without a
    # number means issuing a code that can never reach the person it is for.
    await _ready(client, session, admin_token, city, cell_type, postamat)
    response = await book(_body(postamat, cell_type, depositor="courier"))
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_FAILED"


async def test_a_courier_phone_without_a_courier_is_rejected(
    client, session, book, admin_token, city, cell_type, postamat
):
    await _ready(client, session, admin_token, city, cell_type, postamat)
    response = await book(
        _body(postamat, cell_type, depositor="owner", courier_phone="+99366000002")
    )
    assert response.status_code == 422


async def test_a_courier_booking_with_a_phone_is_accepted(
    client, session, book, admin_token, city, cell_type, postamat
):
    await _ready(client, session, admin_token, city, cell_type, postamat)
    response = await book(
        _body(postamat, cell_type, depositor="courier", courier_phone="+99366000002")
    )
    assert response.status_code == 201
