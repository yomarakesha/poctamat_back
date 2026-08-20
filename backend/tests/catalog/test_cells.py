import uuid

PATH = "/api/v1/admin/cells"


def _key():
    return {"Idempotency-Key": str(uuid.uuid4())}


async def _create(client, admin_token, postamat, cell_type, **overrides):
    body = {
        "postamat_id": str(postamat.id), "cell_type_id": str(cell_type.id),
        # A door label is a string on the wire; this cabinet's labels are digits.
        "number": "1", "hardware_address": {"board": 1, "output": 1},
    }
    body.update(overrides)
    return await client.post(
        PATH, headers={"Authorization": f"Bearer {admin_token}"} | _key(), json=body
    )


async def test_a_cell_answers_the_contract_shape(
    client, admin_token, postamat, cell_type
):
    response = await _create(client, admin_token, postamat, cell_type,
                             number="7", row=2, col=3,
                             hardware_address={"board": 1, "output": 7})
    assert response.status_code == 201
    body = response.json()
    assert body["number"] == "7"
    assert body["hardware_address"] == {"board": 1, "output": 7}
    assert (body["row"], body["col"]) == (2, 3)
    assert body["cell_type_id"] == str(cell_type.id)


async def test_duplicate_cell_number_within_postamat_is_rejected(
    client, admin_token, postamat, cell_type
):
    first = await _create(client, admin_token, postamat, cell_type)
    assert first.status_code == 201

    duplicate = await _create(client, admin_token, postamat, cell_type,
                              hardware_address={"board": 1, "output": 2})
    assert duplicate.status_code == 409
    assert duplicate.json()["error"]["code"] == "CELL_NUMBER_DUPLICATE"


async def test_two_cells_cannot_share_one_board_output(
    client, admin_token, postamat, cell_type
):
    await _create(client, admin_token, postamat, cell_type)

    clash = await _create(client, admin_token, postamat, cell_type, number="2")
    # One output drives one lock. Two cells claiming it means a courier opens
    # the wrong door.
    assert clash.status_code == 409
    assert clash.json()["error"]["code"] == "HARDWARE_ADDRESS_DUPLICATE"


async def test_a_door_label_that_is_not_a_number_is_refused(
    client, admin_token, postamat, cell_type
):
    response = await _create(client, admin_token, postamat, cell_type,
                             number="ТП-4")
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"


async def test_the_same_number_in_another_postamat_is_fine(
    client, session, admin_token, postamat, cell_type, city
):
    from app.modules.catalog.models import Postamat

    other = Postamat(number="10002", name="ТП #2", city_id=city.id, address="ул. Мира")
    session.add(other)
    await session.commit()

    for target in (postamat, other):
        response = await _create(client, admin_token, target, cell_type)
        assert response.status_code == 201


async def test_block_and_unblock_a_cell(client, admin_token, postamat, cell_type):
    headers = {"Authorization": f"Bearer {admin_token}"}
    created = await _create(client, admin_token, postamat, cell_type, number="7",
                            hardware_address={"board": 1, "output": 7})
    cell_id = created.json()["id"]

    blocked = await client.post(f"{PATH}/{cell_id}/block", headers=headers | _key(),
                                json={"reason": "Замок заедает"})
    assert blocked.status_code == 200
    assert blocked.json()["status"] == "blocked"
    assert blocked.json()["status_reason"] == "Замок заедает"

    unblocked = await client.post(f"{PATH}/{cell_id}/unblock",
                                  headers=headers | _key(),
                                  json={"parcel_fate": "cell_was_empty"})
    assert unblocked.json()["status"] == "free"
    assert unblocked.json()["status_reason"] is None


async def test_unblocking_must_say_what_happened_to_the_parcel(
    client, session, admin_token, postamat, cell_type
):
    from sqlalchemy import select

    from app.modules.audit.models import AuditEntry

    headers = {"Authorization": f"Bearer {admin_token}"}
    created = await _create(client, admin_token, postamat, cell_type)
    cell_id = created.json()["id"]
    await client.post(f"{PATH}/{cell_id}/block", headers=headers | _key(),
                      json={"reason": "Замок заедает"})

    silent = await client.post(f"{PATH}/{cell_id}/unblock", headers=headers | _key(),
                               json={})
    # A blocked cell may still have a parcel in it, and where it went is the
    # point of the call rather than a footnote to it.
    assert silent.status_code == 422

    await client.post(f"{PATH}/{cell_id}/unblock", headers=headers | _key(),
                      json={"parcel_fate": "moved_to_counter",
                            "note": "Клиент не пришёл"})
    entry = await session.scalar(
        select(AuditEntry).where(AuditEntry.event == "cell.unblocked")
    )
    assert entry.details["parcel_fate"] == "moved_to_counter"


async def test_patch_moves_a_cell_to_another_board_output(
    client, admin_token, postamat, cell_type
):
    headers = {"Authorization": f"Bearer {admin_token}"}
    created = await _create(client, admin_token, postamat, cell_type, number="7",
                            hardware_address={"board": 1, "output": 7})
    cell_id = created.json()["id"]

    response = await client.patch(f"{PATH}/{cell_id}", headers=headers,
                                  json={"hardware_address": {"board": 2, "output": 3}})
    assert response.status_code == 200
    assert response.json()["hardware_address"] == {"board": 2, "output": 3}
    # The door label is untouched by a rewiring.
    assert response.json()["number"] == "7"


async def test_the_monitor_lists_cells_across_postamats(
    client, session, admin_token, postamat, cell_type, city
):
    from app.modules.catalog.models import Postamat

    other = Postamat(number="10002", name="ТП #2", city_id=city.id, address="ул. Мира")
    session.add(other)
    await session.commit()
    await _create(client, admin_token, postamat, cell_type)
    await _create(client, admin_token, other, cell_type)

    headers = {"Authorization": f"Bearer {admin_token}"}
    everything = await client.get(PATH, headers=headers)
    assert everything.status_code == 200
    assert len(everything.json()["items"]) == 2
    assert everything.json()["pagination"]["has_more"] is False

    one = await client.get(f"{PATH}?postamat_id={postamat.id}", headers=headers)
    assert [item["postamat_number"] for item in one.json()["items"]] == [
        postamat.number
    ]


async def test_the_monitor_filters_by_status(
    client, session, admin_token, postamat, cell_type
):
    headers = {"Authorization": f"Bearer {admin_token}"}
    await _create(client, admin_token, postamat, cell_type, number="1",
                  hardware_address={"board": 1, "output": 1})
    second = await _create(client, admin_token, postamat, cell_type, number="2",
                           hardware_address={"board": 1, "output": 2})
    await client.post(f"{PATH}/{second.json()['id']}/block", headers=headers | _key(),
                      json={"reason": "Замок заедает"})

    blocked = await client.get(f"{PATH}?status=blocked", headers=headers)
    assert [item["number"] for item in blocked.json()["items"]] == ["2"]

    free = await client.get(f"{PATH}?status=free,booked", headers=headers)
    assert [item["number"] for item in free.json()["items"]] == ["1"]


async def test_the_monitor_page_is_a_handful_of_queries(
    client, session, admin_token, postamat, cell_type, count_queries
):
    from app.modules.catalog.models import Cell

    session.add_all([
        Cell(postamat_id=postamat.id, cell_type_id=cell_type.id, number=n,
             board=1, output=n)
        for n in range(1, 11)
    ])
    await session.commit()

    count_queries.clear()
    response = await client.get(PATH,
                                headers={"Authorization": f"Bearer {admin_token}"})
    assert len(response.json()["items"]) == 10
    # Postamats, types, bookings, phones — batched for the page. A query per row
    # is how a live view becomes a load test.
    assert len(count_queries) <= 8


async def test_listing_cells_needs_the_read_permission(client, session, postamat):
    from app.core.security import create_access_token
    from app.modules.identity.models import AdminUser, Role

    role = Role(code="courier", name="Courier", permissions=["postamats.read"])
    session.add(role)
    await session.flush()
    admin = AdminUser(login="courier_test", full_name="Курьеров К.К.",
                      password_hash="x", role_id=role.id)
    session.add(admin)
    await session.commit()
    await session.refresh(admin)

    token = create_access_token("admin", admin.id)
    response = await client.get(f"{PATH}?postamat_id={postamat.id}",
                                headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "FORBIDDEN"
