async def test_incomplete_matrix_is_rejected(client, admin_token, city, cell_type):
    headers = {"Authorization": f"Bearer {admin_token}"}
    response = await client.put("/api/v1/admin/tariffs", headers=headers, json={
        "entries": [
            {"city_id": str(city.id), "cell_type_id": str(cell_type.id),
             "duration_hours": 12, "amount_minor": 1200},
        ]
    })
    assert response.status_code == 422
    body = response.json()["error"]
    assert body["code"] == "TARIFF_INCOMPLETE"
    assert body["details"]["missing"] == [24, 48]


async def test_complete_matrix_replaces_previous(client, admin_token, city, cell_type):
    headers = {"Authorization": f"Bearer {admin_token}"}
    entries = [
        {"city_id": str(city.id), "cell_type_id": str(cell_type.id),
         "duration_hours": hours, "amount_minor": amount}
        for hours, amount in ((12, 1200), (24, 1800), (48, 2600))
    ]
    first = await client.put("/api/v1/admin/tariffs", headers=headers,
                             json={"entries": entries})
    assert first.status_code == 200

    entries[0]["amount_minor"] = 1400
    second = await client.put("/api/v1/admin/tariffs", headers=headers,
                              json={"entries": entries})
    assert second.status_code == 200

    listed = await client.get("/api/v1/admin/tariffs", headers=headers)
    prices = {item["duration_hours"]: item["amount_minor"]
              for item in listed.json()["items"]}
    assert prices == {12: 1400, 24: 1800, 48: 2600}


async def test_a_rejected_matrix_leaves_the_previous_one_standing(
    client, admin_token, city, cell_type
):
    headers = {"Authorization": f"Bearer {admin_token}"}
    entries = [
        {"city_id": str(city.id), "cell_type_id": str(cell_type.id),
         "duration_hours": hours, "amount_minor": amount}
        for hours, amount in ((12, 1200), (24, 1800), (48, 2600))
    ]
    await client.put("/api/v1/admin/tariffs", headers=headers,
                     json={"entries": entries})

    rejected = await client.put("/api/v1/admin/tariffs", headers=headers, json={
        "entries": [entries[0]]
    })
    assert rejected.status_code == 422

    listed = await client.get("/api/v1/admin/tariffs", headers=headers)
    assert len(listed.json()["items"]) == 3


async def test_an_empty_matrix_clears_every_price(client, admin_token, city, cell_type):
    # Nothing priced is a legitimate state — a region that has not opened yet —
    # and it is not the same as an incomplete one.
    headers = {"Authorization": f"Bearer {admin_token}"}
    entries = [
        {"city_id": str(city.id), "cell_type_id": str(cell_type.id),
         "duration_hours": hours, "amount_minor": 1000}
        for hours in (12, 24, 48)
    ]
    await client.put("/api/v1/admin/tariffs", headers=headers,
                     json={"entries": entries})

    cleared = await client.put("/api/v1/admin/tariffs", headers=headers,
                               json={"entries": []})
    assert cleared.status_code == 200
    listed = await client.get("/api/v1/admin/tariffs", headers=headers)
    assert listed.json()["items"] == []


async def test_an_unknown_duration_is_rejected(client, admin_token, city, cell_type):
    headers = {"Authorization": f"Bearer {admin_token}"}
    entries = [
        {"city_id": str(city.id), "cell_type_id": str(cell_type.id),
         "duration_hours": hours, "amount_minor": 1000}
        for hours in (12, 24, 48, 72)
    ]
    response = await client.put("/api/v1/admin/tariffs", headers=headers,
                                json={"entries": entries})
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_FAILED"


async def test_a_negative_price_is_rejected(client, admin_token, city, cell_type):
    headers = {"Authorization": f"Bearer {admin_token}"}
    response = await client.put("/api/v1/admin/tariffs", headers=headers, json={
        "entries": [{"city_id": str(city.id), "cell_type_id": str(cell_type.id),
                     "duration_hours": 12, "amount_minor": -100}]
    })
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_FAILED"
