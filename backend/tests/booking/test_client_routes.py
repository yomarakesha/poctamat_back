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
            "depositor": "courier", "courier_phone": "+99366000002"}


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
    assert listed.json()["pagination"]["total"] == 2


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
    client, session, book, client_token, admin_token, city, cell_type, postamat
):
    await _ready(client, session, admin_token, city, cell_type, postamat, cells=1)
    headers = {"Authorization": f"Bearer {client_token}"}
    created = await book(_body(postamat, cell_type))

    sold_out = await book(_body(postamat, cell_type))
    assert sold_out.status_code == 409

    cancelled = await client.post(f"/api/v1/bookings/{created.json()['id']}/cancel",
                                  headers=headers, json={"reason": "передумал"})
    assert cancelled.status_code == 200
    assert cancelled.json()["status"] == BookingStatus.CANCELLED

    again = await book(_body(postamat, cell_type))
    assert again.status_code == 201


async def test_only_active_bookings_are_listed_when_asked(
    client, session, book, client_token, admin_token, city, cell_type, postamat
):
    await _ready(client, session, admin_token, city, cell_type, postamat, cells=1)
    headers = {"Authorization": f"Bearer {client_token}"}
    created = await book(_body(postamat, cell_type))
    await client.post(f"/api/v1/bookings/{created.json()['id']}/cancel",
                      headers=headers, json={"reason": "передумал"})

    active = await client.get("/api/v1/bookings?active=true", headers=headers)
    assert active.json()["items"] == []
    everything = await client.get("/api/v1/bookings", headers=headers)
    assert len(everything.json()["items"]) == 1


async def test_resending_the_courier_pin_answers_202_without_the_code(
    client, session, book, client_token, admin_token, city, cell_type, postamat
):
    await _ready(client, session, admin_token, city, cell_type, postamat)
    headers = {"Authorization": f"Bearer {client_token}"}
    created = await book(_body(postamat, cell_type))
    original = created.json()["codes"]["courier"]

    resent = await client.post(
        f"/api/v1/bookings/{created.json()['id']}/courier/resend", headers=headers
    )
    assert resent.status_code == 202
    # The new code exists only on the courier's phone: it must not come back
    # through the sender's screen.
    assert original not in resent.text


async def test_the_old_courier_pin_stops_working_after_a_resend(
    client, session, book, client_token, admin_token, city, cell_type, postamat
):
    import re

    from app.modules.booking.codes import find_code
    from app.modules.notify.sms import get_sms_provider

    await _ready(client, session, admin_token, city, cell_type, postamat)
    headers = {"Authorization": f"Bearer {client_token}"}
    created = await book(_body(postamat, cell_type))
    original = created.json()["codes"]["courier"]

    await client.post(
        f"/api/v1/bookings/{created.json()['id']}/courier/resend", headers=headers
    )
    texted = re.search(r"\d{5}", get_sms_provider().outbox[-1].text).group()

    assert await find_code(session, postamat.id, original) is None
    assert await find_code(session, postamat.id, texted) is not None


async def test_resending_is_refused_when_nobody_is_couriering(
    client, session, book, client_token, admin_token, city, cell_type, postamat
):
    await _ready(client, session, admin_token, city, cell_type, postamat)
    headers = {"Authorization": f"Bearer {client_token}"}
    body = _body(postamat, cell_type) | {"depositor": "owner", "courier_phone": None}
    created = await book(body)

    response = await client.post(
        f"/api/v1/bookings/{created.json()['id']}/courier/resend", headers=headers
    )
    assert response.status_code == 409
