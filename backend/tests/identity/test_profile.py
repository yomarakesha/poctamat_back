import uuid

import pytest


@pytest.fixture
async def blank_client(session):
    """A client who has signed in and not yet filled the registration form."""
    from app.modules.identity.models import Client

    row = Client(phone="+99361000123")
    session.add(row)
    await session.commit()
    await session.refresh(row)
    return row


@pytest.fixture
def blank_token(blank_client):
    from app.core.security import create_access_token

    return create_access_token("client", blank_client.id)


async def test_a_fresh_client_has_an_incomplete_profile(
    client, blank_token, blank_client
):
    response = await client.get("/api/v1/me",
                                headers={"Authorization": f"Bearer {blank_token}"})
    assert response.status_code == 200
    body = response.json()
    assert body["phone"] == blank_client.phone
    assert body["profile_complete"] is False
    assert body["status"] == "active"


async def test_filling_both_names_completes_the_profile(client, client_token):
    headers = {"Authorization": f"Bearer {client_token}"}
    response = await client.patch("/api/v1/me", headers=headers, json={
        "last_name": "Аннаев", "first_name": "Мырат", "middle_name": "Аннаевич",
        "language": "ru",
    })
    assert response.status_code == 200
    assert response.json()["profile_complete"] is True
    assert response.json()["language"] == "ru"


async def test_a_patronymic_alone_does_not_complete_it(client, blank_token):
    headers = {"Authorization": f"Bearer {blank_token}"}
    response = await client.patch("/api/v1/me", headers=headers,
                                  json={"middle_name": "Аннаевич"})
    assert response.json()["profile_complete"] is False


async def test_choosing_a_city_that_has_not_opened_is_refused(
    client, session, client_token, city
):
    city.is_active = False
    await session.commit()

    response = await client.patch("/api/v1/me",
                                  headers={"Authorization": f"Bearer {client_token}"},
                                  json={"city_id": str(city.id)})
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "CITY_NOT_AVAILABLE"


async def test_an_unknown_city_is_refused_the_same_way(client, client_token):
    import uuid

    response = await client.patch("/api/v1/me",
                                  headers={"Authorization": f"Bearer {client_token}"},
                                  json={"city_id": str(uuid.uuid4())})
    assert response.status_code == 422


async def test_the_profile_needs_a_client_token(client, admin_token):
    assert (await client.get("/api/v1/me")).status_code == 401
    # An admin token is not a client token: the profile belongs to the person,
    # not to the operator.
    response = await client.get("/api/v1/me",
                                headers={"Authorization": f"Bearer {admin_token}"})
    assert response.status_code == 401


async def test_booking_is_refused_until_the_profile_is_complete(
    client, session, blank_token, admin_token, city, cell_type, postamat
):
    from app.modules.catalog.models import Cell

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

    response = await client.post(
        "/api/v1/bookings",
        json={"postamat_id": str(postamat.id), "cell_type_id": str(cell_type.id),
              "duration_hours": 24, "recipient_phone": "+99365000001"},
        headers={"Authorization": f"Bearer {blank_token}",
                 "Idempotency-Key": str(uuid.uuid4())},
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "PROFILE_INCOMPLETE"
