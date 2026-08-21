import uuid


def _key():
    return {"Idempotency-Key": str(uuid.uuid4())}


def _items(city, cell_type, prices=((12, 1200), (24, 1800), (48, 2600))):
    return [
        {"city_id": str(city.id), "cell_type_id": str(cell_type.id),
         "duration_hours": hours,
         "price": {"amount_minor": amount, "currency": "TMT"}}
        for hours, amount in prices
    ]


async def test_incomplete_matrix_is_rejected(client, admin_token, city, cell_type):
    headers = {"Authorization": f"Bearer {admin_token}"} | _key()
    response = await client.put("/api/v1/admin/tariffs", headers=headers, json={
        "items": _items(city, cell_type, [(12, 1200)]),
    })
    assert response.status_code == 422
    body = response.json()["error"]
    assert body["code"] == "TARIFF_INCOMPLETE"
    assert body["details"]["missing"] == [24, 48]


async def test_complete_matrix_replaces_previous(client, admin_token, city, cell_type):
    headers = {"Authorization": f"Bearer {admin_token}"}
    first = await client.put("/api/v1/admin/tariffs", headers=headers | _key(),
                             json={"items": _items(city, cell_type)})
    assert first.status_code == 200

    items = _items(city, cell_type, [(12, 1400), (24, 1800), (48, 2600)])
    second = await client.put("/api/v1/admin/tariffs", headers=headers | _key(),
                              json={"items": items})
    assert second.status_code == 200

    listed = await client.get("/api/v1/admin/tariffs", headers=headers)
    prices = {item["duration_hours"]: item["price"]["amount_minor"]
              for item in listed.json()["items"]}
    assert prices == {12: 1400, 24: 1800, 48: 2600}


async def test_a_price_is_a_money_object_with_its_currency(
    client, admin_token, city, cell_type
):
    headers = {"Authorization": f"Bearer {admin_token}"}
    await client.put("/api/v1/admin/tariffs", headers=headers | _key(),
                     json={"items": _items(city, cell_type)})

    item = (await client.get("/api/v1/admin/tariffs", headers=headers)).json()["items"][0]
    assert item["price"] == {"amount_minor": 1200, "currency": "TMT"}
    assert item["updated_at"].endswith("Z")


async def test_the_matrix_can_be_read_for_one_city_or_one_type(
    client, session, admin_token, city, cell_type
):
    from app.modules.catalog.models import CellType, City

    other_city = City(code="arkadag", name_tk="Arkadag", name_ru="Аркадаг",
                      name_en="Arkadag")
    other_type = CellType(code="large", name_tk="Uly", name_ru="Большой",
                          name_en="Large", width_mm=400, height_mm=400, depth_mm=600)
    session.add_all([other_city, other_type])
    await session.commit()

    headers = {"Authorization": f"Bearer {admin_token}"}
    await client.put("/api/v1/admin/tariffs", headers=headers | _key(), json={
        "items": _items(city, cell_type) + _items(other_city, other_type),
    })

    by_city = await client.get(f"/api/v1/admin/tariffs?city_id={city.id}",
                               headers=headers)
    assert {item["city_id"] for item in by_city.json()["items"]} == {str(city.id)}

    by_type = await client.get(
        f"/api/v1/admin/tariffs?cell_type_id={other_type.id}", headers=headers
    )
    assert {item["cell_type_id"] for item in by_type.json()["items"]} == {
        str(other_type.id)
    }


async def test_a_rejected_matrix_leaves_the_previous_one_standing(
    client, admin_token, city, cell_type
):
    headers = {"Authorization": f"Bearer {admin_token}"}
    await client.put("/api/v1/admin/tariffs", headers=headers | _key(),
                     json={"items": _items(city, cell_type)})

    rejected = await client.put("/api/v1/admin/tariffs", headers=headers | _key(),
                                json={"items": _items(city, cell_type, [(12, 1200)])})
    assert rejected.status_code == 422

    listed = await client.get("/api/v1/admin/tariffs", headers=headers)
    assert len(listed.json()["items"]) == 3


async def test_an_empty_matrix_clears_every_price(client, admin_token, city, cell_type):
    # Nothing priced is a legitimate state — a region that has not opened yet —
    # and it is not the same as an incomplete one.
    headers = {"Authorization": f"Bearer {admin_token}"}
    await client.put("/api/v1/admin/tariffs", headers=headers | _key(),
                     json={"items": _items(city, cell_type)})

    cleared = await client.put("/api/v1/admin/tariffs", headers=headers | _key(),
                               json={"items": []})
    assert cleared.status_code == 200
    listed = await client.get("/api/v1/admin/tariffs", headers=headers)
    assert listed.json()["items"] == []


async def test_a_price_for_an_unknown_city_is_refused(
    client, admin_token, city, cell_type
):
    headers = {"Authorization": f"Bearer {admin_token}"} | _key()
    stranger = type("X", (), {"id": uuid.uuid4()})
    response = await client.put("/api/v1/admin/tariffs", headers=headers,
                                json={"items": _items(stranger, cell_type)})
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "CITY_NOT_AVAILABLE"


async def test_an_unknown_duration_is_rejected(client, admin_token, city, cell_type):
    headers = {"Authorization": f"Bearer {admin_token}"} | _key()
    response = await client.put("/api/v1/admin/tariffs", headers=headers, json={
        "items": _items(city, cell_type, [(12, 1000), (24, 1000), (48, 1000),
                                          (72, 1000)]),
    })
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"


async def test_a_negative_price_is_rejected(client, admin_token, city, cell_type):
    headers = {"Authorization": f"Bearer {admin_token}"} | _key()
    response = await client.put("/api/v1/admin/tariffs", headers=headers, json={
        "items": _items(city, cell_type, [(12, -100)]),
    })
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"


async def test_replacing_the_matrix_needs_an_idempotency_key(
    client, admin_token, city, cell_type
):
    response = await client.put("/api/v1/admin/tariffs",
                                headers={"Authorization": f"Bearer {admin_token}"},
                                json={"items": _items(city, cell_type)})
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "IDEMPOTENCY_KEY_MISSING"
