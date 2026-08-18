async def test_duplicate_cell_number_within_postamat_is_rejected(
    client, admin_token, postamat, cell_type
):
    headers = {"Authorization": f"Bearer {admin_token}"}
    body = {"postamat_id": str(postamat.id), "cell_type_id": str(cell_type.id),
            "cells": [{"number": 1, "board": 1, "output": 1}]}

    first = await client.post("/api/v1/admin/cells", headers=headers, json=body)
    assert first.status_code == 201

    duplicate = await client.post("/api/v1/admin/cells", headers=headers, json=body)
    assert duplicate.status_code == 409
    assert duplicate.json()["error"]["code"] == "CELL_NUMBER_DUPLICATE"


async def test_bulk_create_accepts_a_full_cabinet(
    client, admin_token, postamat, cell_type
):
    headers = {"Authorization": f"Bearer {admin_token}"}
    cells = [{"number": n, "board": 1 if n <= 21 else 2,
              "output": n if n <= 21 else n - 21} for n in range(1, 44)]
    response = await client.post("/api/v1/admin/cells", headers=headers, json={
        "postamat_id": str(postamat.id), "cell_type_id": str(cell_type.id),
        "cells": cells,
    })
    assert response.status_code == 201
    assert len(response.json()["items"]) == 43


async def test_the_same_number_in_another_postamat_is_fine(
    client, session, admin_token, postamat, cell_type, city
):
    from app.modules.catalog.models import Postamat

    other = Postamat(number="10002", name="ТП #2", city_id=city.id, address="ул. Мира")
    session.add(other)
    await session.commit()

    headers = {"Authorization": f"Bearer {admin_token}"}
    for target in (postamat, other):
        response = await client.post("/api/v1/admin/cells", headers=headers, json={
            "postamat_id": str(target.id), "cell_type_id": str(cell_type.id),
            "cells": [{"number": 1, "board": 1, "output": 1}],
        })
        assert response.status_code == 201


async def test_a_duplicate_inside_one_request_is_rejected_whole(
    client, session, admin_user, admin_token, postamat, cell_type
):
    # The batch is one unit: if the operator lists cell 3 twice, nothing lands,
    # so a retry with the corrected list cannot leave half a cabinet behind.
    headers = {"Authorization": f"Bearer {admin_token}"}
    postamat_id = postamat.id
    response = await client.post("/api/v1/admin/cells", headers=headers, json={
        "postamat_id": str(postamat_id), "cell_type_id": str(cell_type.id),
        "cells": [{"number": 3, "board": 1, "output": 3},
                  {"number": 3, "board": 1, "output": 4}],
    })
    assert response.status_code == 409

    # The rollback the router just performed expires everything in this session,
    # and the suite shares one session across requests where production gives
    # each request its own. Reloading here is harness bookkeeping, not product
    # behaviour: a real second request would open a clean session.
    await session.refresh(admin_user)
    listed = await client.get(f"/api/v1/admin/cells?postamat_id={postamat_id}",
                              headers=headers)
    assert listed.json()["items"] == []


async def test_block_and_unblock_a_cell(client, admin_token, postamat, cell_type):
    headers = {"Authorization": f"Bearer {admin_token}"}
    created = await client.post("/api/v1/admin/cells", headers=headers, json={
        "postamat_id": str(postamat.id), "cell_type_id": str(cell_type.id),
        "cells": [{"number": 7, "board": 1, "output": 7}],
    })
    cell_id = created.json()["items"][0]["id"]

    blocked = await client.post(f"/api/v1/admin/cells/{cell_id}/block",
                                headers=headers, json={"reason": "lock jammed"})
    assert blocked.json()["is_blocked"] is True
    assert blocked.json()["blocked_reason"] == "lock jammed"

    unblocked = await client.post(f"/api/v1/admin/cells/{cell_id}/unblock",
                                  headers=headers)
    assert unblocked.json()["is_blocked"] is False
    assert unblocked.json()["blocked_reason"] is None


async def test_patch_moves_a_cell_to_another_board_output(
    client, admin_token, postamat, cell_type
):
    headers = {"Authorization": f"Bearer {admin_token}"}
    created = await client.post("/api/v1/admin/cells", headers=headers, json={
        "postamat_id": str(postamat.id), "cell_type_id": str(cell_type.id),
        "cells": [{"number": 7, "board": 1, "output": 7}],
    })
    cell_id = created.json()["items"][0]["id"]

    response = await client.patch(f"/api/v1/admin/cells/{cell_id}", headers=headers,
                                  json={"board": 2, "output": 3})
    assert response.status_code == 200
    assert (response.json()["board"], response.json()["output"]) == (2, 3)
    # The door label is untouched by a rewiring.
    assert response.json()["number"] == 7


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
    response = await client.get(f"/api/v1/admin/cells?postamat_id={postamat.id}",
                                headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "FORBIDDEN"
