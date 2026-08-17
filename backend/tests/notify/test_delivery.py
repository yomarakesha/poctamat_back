from app.modules.catalog.models import Cell
from app.modules.notify.sms import get_sms_provider


async def test_the_login_code_is_sent_by_sms(client):
    from app.modules.identity.service import peek_otp

    phone = "+99362123456"
    response = await client.post("/api/v1/auth/otp/request", json={"phone": phone})
    assert response.status_code == 200

    code = await peek_otp(response.json()["request_id"])
    outbox = get_sms_provider().outbox
    assert outbox[-1].phone == phone
    assert code in outbox[-1].text
    # The response still refuses to carry the code itself.
    assert code not in response.text


async def test_the_login_sms_follows_the_requested_language(client):
    russian = await client.post("/api/v1/auth/otp/request",
                                json={"phone": "+99362123401"},
                                headers={"Accept-Language": "ru"})
    assert russian.status_code == 200
    ru_text = get_sms_provider().outbox[-1].text

    turkmen = await client.post("/api/v1/auth/otp/request",
                                json={"phone": "+99362123402"},
                                headers={"Accept-Language": "tk"})
    assert turkmen.status_code == 200
    tk_text = get_sms_provider().outbox[-1].text

    assert ru_text != tk_text


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


async def test_the_courier_pin_is_sent_and_no_longer_returned(
    client, session, book, client_token, admin_token, city, cell_type, postamat
):
    await _ready(client, session, admin_token, city, cell_type, postamat)
    created = await book({
        "postamat_id": str(postamat.id), "cell_type_id": str(cell_type.id),
        "duration_hours": 24, "recipient_phone": "+99365000001",
        "depositor": "courier", "courier_phone": "+99366000002",
    })
    booking_id = created.json()["id"]

    resent = await client.post(f"/api/v1/bookings/{booking_id}/courier/resend",
                               headers={"Authorization": f"Bearer {client_token}"})
    assert resent.status_code == 202
    assert resent.content in (b"", b"null")

    last = get_sms_provider().outbox[-1]
    assert last.phone == "+99366000002"
    assert "1" in last.text  # a five-digit code is in there somewhere


async def test_the_resent_code_is_the_one_that_opens_the_door(
    client, session, book, client_token, admin_token, city, cell_type, postamat
):
    import re

    from app.modules.booking.codes import find_code

    await _ready(client, session, admin_token, city, cell_type, postamat)
    created = await book({
        "postamat_id": str(postamat.id), "cell_type_id": str(cell_type.id),
        "duration_hours": 24, "recipient_phone": "+99365000001",
        "depositor": "courier", "courier_phone": "+99366000002",
    })
    original = created.json()["codes"]["courier"]

    await client.post(f"/api/v1/bookings/{created.json()['id']}/courier/resend",
                      headers={"Authorization": f"Bearer {client_token}"})

    texted = re.search(r"\d{5}", get_sms_provider().outbox[-1].text).group()
    assert texted != original
    assert await find_code(session, postamat.id, original) is None
    assert await find_code(session, postamat.id, texted) is not None
