from datetime import datetime, time, timezone

from app.modules.catalog.models import Postamat, PostamatSchedule
from app.modules.catalog.service import is_open_at, next_opening_after


def build_postamat(days: range = range(7)) -> Postamat:
    postamat = Postamat(number="10042", name="ТП #4", address="ул. Ататюрк",
                        city_id=None, round_the_clock=False)
    postamat.schedule = [
        PostamatSchedule(weekday=day, opens_at=time(8, 0), closes_at=time(20, 0))
        for day in days
    ]
    return postamat


def test_closed_outside_working_hours():
    postamat = build_postamat()
    assert is_open_at(postamat, datetime(2026, 8, 14, 12, 0, tzinfo=timezone.utc)) is True
    assert is_open_at(postamat, datetime(2026, 8, 14, 21, 0, tzinfo=timezone.utc)) is False


def test_the_boundary_minutes_belong_to_the_open_window_at_the_start_only():
    # A window is half-open: the opening minute is open, the closing minute is
    # already shut. Otherwise a client arriving at 20:00 sharp is told to come
    # in and finds the door locked.
    postamat = build_postamat()
    assert is_open_at(postamat, datetime(2026, 8, 14, 8, 0, tzinfo=timezone.utc)) is True
    assert is_open_at(postamat, datetime(2026, 8, 14, 7, 59, tzinfo=timezone.utc)) is False
    assert is_open_at(postamat, datetime(2026, 8, 14, 19, 59, tzinfo=timezone.utc)) is True
    assert is_open_at(postamat, datetime(2026, 8, 14, 20, 0, tzinfo=timezone.utc)) is False


def test_a_day_without_a_slot_is_closed_all_day():
    # Monday to Friday only; 2026-08-15 is a Saturday.
    postamat = build_postamat(days=range(5))
    assert is_open_at(postamat, datetime(2026, 8, 15, 12, 0, tzinfo=timezone.utc)) is False


def test_round_the_clock_is_always_open():
    postamat = build_postamat()
    postamat.round_the_clock = True
    assert is_open_at(postamat, datetime(2026, 8, 14, 3, 0, tzinfo=timezone.utc)) is True


def test_next_opening_skips_to_the_morning():
    postamat = build_postamat()
    moment = datetime(2026, 8, 14, 21, 0, tzinfo=timezone.utc)
    assert next_opening_after(postamat, moment) == datetime(
        2026, 8, 15, 8, 0, tzinfo=timezone.utc
    )


def test_next_opening_skips_the_closed_days():
    # Friday 21:00 with a Monday-to-Friday schedule: the next opening is Monday.
    postamat = build_postamat(days=range(5))
    moment = datetime(2026, 8, 14, 21, 0, tzinfo=timezone.utc)
    assert next_opening_after(postamat, moment) == datetime(
        2026, 8, 17, 8, 0, tzinfo=timezone.utc
    )


def test_next_opening_is_now_when_round_the_clock():
    postamat = build_postamat()
    postamat.round_the_clock = True
    moment = datetime(2026, 8, 14, 3, 0, tzinfo=timezone.utc)
    assert next_opening_after(postamat, moment) == moment


async def test_create_and_block_postamat(client, session, admin_token, city):
    headers = {"Authorization": f"Bearer {admin_token}"}
    created = await client.post("/api/v1/admin/postamats", headers=headers | _key(), json={
        "number": "10042", "name": "ТП #4", "city_id": str(city.id),
        "address": "ул. Ататюрк, 31", "round_the_clock": False,
    })
    assert created.status_code == 201
    postamat_id = created.json()["id"]
    assert created.json()["created_at"].endswith("Z")

    blocked = await client.post(f"/api/v1/admin/postamats/{postamat_id}/block",
                                headers=headers | _key(), json={"reason": "vandalised"})
    assert blocked.json()["status"] == "blocked"

    unblocked = await client.post(f"/api/v1/admin/postamats/{postamat_id}/unblock",
                                  headers=headers | _key())
    assert unblocked.json()["status"] == "active"


async def test_duplicate_number_is_409(client, admin_token, city, postamat):
    headers = {"Authorization": f"Bearer {admin_token}"}
    response = await client.post("/api/v1/admin/postamats", headers=headers | _key(), json={
        "number": postamat.number, "name": "ТП #2", "city_id": str(city.id),
        "address": "ул. Гарашсызлык, 1",
    })
    assert response.status_code == 409
    assert response.json()["error"]["details"]["field"] == "number"


async def test_schedule_replaces_the_whole_week(client, admin_token, postamat):
    headers = {"Authorization": f"Bearer {admin_token}"}
    # Weekdays run 1..7 on the wire, Monday first, as the contract numbers them.
    week = {"round_the_clock": False,
            "days": [{"weekday": day, "opens_at": "08:00", "closes_at": "20:00"}
                     for day in range(1, 8)]}
    response = await client.put(f"/api/v1/admin/postamats/{postamat.id}/schedule",
                                headers=headers | _key(), json=week)
    assert response.status_code == 200
    assert len(response.json()["days"]) == 7

    weekdays_only = {"round_the_clock": False,
                     "days": [{"weekday": day, "opens_at": "09:00",
                               "closes_at": "18:00"} for day in range(1, 6)]}
    replaced = await client.put(f"/api/v1/admin/postamats/{postamat.id}/schedule",
                                headers=headers | _key(), json=weekdays_only)
    assert [slot["weekday"] for slot in replaced.json()["days"]] == [1, 2, 3, 4, 5]
    assert replaced.json()["days"][0]["opens_at"] == "09:00"


async def test_round_the_clock_keeps_no_days(client, admin_token, postamat):
    headers = {"Authorization": f"Bearer {admin_token}"}
    await client.put(f"/api/v1/admin/postamats/{postamat.id}/schedule",
                     headers=headers | _key(),
                     json={"round_the_clock": False,
                           "days": [{"weekday": 1, "opens_at": "08:00",
                                     "closes_at": "20:00"}]})

    response = await client.put(f"/api/v1/admin/postamats/{postamat.id}/schedule",
                                headers=headers | _key(), json={"round_the_clock": True})
    # Hours kept alongside «круглосуточно» are two answers to one question.
    assert response.json() == {"round_the_clock": True, "days": []}
    detail = await client.get(f"/api/v1/postamats/{postamat.id}")
    assert detail.json()["working_hours"] == "24/7"


async def test_a_window_crossing_midnight_is_rejected(client, admin_token, postamat):
    headers = {"Authorization": f"Bearer {admin_token}"}
    response = await client.put(f"/api/v1/admin/postamats/{postamat.id}/schedule", headers=headers | _key(),
        json={"days": [{"weekday": 1, "opens_at": "22:00", "closes_at": "06:00"}]},
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"


async def test_the_same_weekday_twice_is_rejected(client, admin_token, postamat):
    headers = {"Authorization": f"Bearer {admin_token}"}
    response = await client.put(f"/api/v1/admin/postamats/{postamat.id}/schedule", headers=headers | _key(),
        json={"days": [{"weekday": 1, "opens_at": "08:00", "closes_at": "12:00"},
                       {"weekday": 1, "opens_at": "13:00", "closes_at": "20:00"}]},
    )
    assert response.status_code == 422


async def test_patch_edits_only_what_was_sent(client, admin_token, postamat):
    headers = {"Authorization": f"Bearer {admin_token}"}
    response = await client.patch(f"/api/v1/admin/postamats/{postamat.id}",
                                  headers=headers, json={"name": "ТП #1 (новый)"})
    assert response.status_code == 200
    assert response.json()["name"] == "ТП #1 (новый)"
    assert response.json()["address"] == postamat.address


async def test_unknown_postamat_is_404(client, admin_token):
    headers = {"Authorization": f"Bearer {admin_token}"}
    response = await client.get(
        "/api/v1/admin/postamats/00000000-0000-0000-0000-000000000000", headers=headers
    )
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "NOT_FOUND"


async def test_the_public_list_hides_blocked_machines(client, admin_token, postamat):
    headers = {"Authorization": f"Bearer {admin_token}"}
    listed = await client.get("/api/v1/postamats")
    assert [item["number"] for item in listed.json()["items"]] == [postamat.number]

    await client.post(f"/api/v1/admin/postamats/{postamat.id}/block",
                      headers=headers | _key(), json={"reason": "vandalised"})

    hidden = await client.get("/api/v1/postamats")
    assert hidden.json()["items"] == []
    detail = await client.get(f"/api/v1/postamats/{postamat.id}")
    assert detail.status_code == 404


async def test_the_public_detail_reports_opening_state(client, admin_token, postamat):
    headers = {"Authorization": f"Bearer {admin_token}"}
    await client.put(f"/api/v1/admin/postamats/{postamat.id}/schedule", headers=headers | _key(),
                     json={"days": [{"weekday": day, "opens_at": "00:00",
                                    "closes_at": "23:59"} for day in range(1, 8)]})

    detail = await client.get(f"/api/v1/postamats/{postamat.id}")
    assert detail.status_code == 200
    assert detail.json()["is_open_now"] is True
    assert detail.json()["next_opening_at"].endswith("Z")


async def test_a_postamat_without_a_schedule_has_no_next_opening(client, postamat):
    detail = await client.get(f"/api/v1/postamats/{postamat.id}")
    assert detail.json()["is_open_now"] is False
    assert detail.json()["next_opening_at"] is None


async def test_next_opening_of_a_permanently_closed_postamat_is_none():
    # No schedule at all and not round-the-clock: the search has to terminate
    # and say so, rather than loop or hand back the moment it was given, which
    # a caller would read as "open now".
    postamat = build_postamat(days=range(0))
    moment = datetime(2026, 8, 14, 21, 0, tzinfo=timezone.utc)
    assert next_opening_after(postamat, moment) is None


async def test_a_postamat_carries_one_location_object(
    client, session, admin_token, city
):
    headers = {"Authorization": f"Bearer {admin_token}"}
    created = await client.post("/api/v1/admin/postamats", headers=headers | _key(), json={
        "number": "10077", "name": "ТП #7", "city_id": str(city.id),
        "address": "ул. Гарашсызлык, 7",
        # One object rather than two loose floats: that is the shape three
        # generated clients read.
        "location": {"lat": 37.9601, "lon": 58.3261},
        "working_hours": "08:00–20:00", "grid_rows": 6, "grid_cols": 7,
    })
    assert created.status_code == 201
    body = created.json()
    assert body["location"] == {"lat": 37.9601, "lon": 58.3261}
    assert body["working_hours"] == "08:00–20:00"
    assert body["grid_rows"] == 6 and body["grid_cols"] == 7
    assert "latitude" not in body

    public = await client.get(f"/api/v1/postamats/{body['id']}")
    assert public.json()["location"] == {"lat": 37.9601, "lon": 58.3261}


async def test_the_admin_table_counts_cells_and_what_is_in_them(
    client, session, admin_token, postamat, cell_type
):
    from app.modules.booking.models import Booking, BookingStatus
    from app.modules.catalog.models import Cell
    import uuid as _uuid

    session.add_all([
        Cell(postamat_id=postamat.id, cell_type_id=cell_type.id, number=n,
             board=1, output=n)
        for n in (1, 2, 3)
    ])
    session.add(Booking(
        client_id=_uuid.uuid4(), postamat_id=postamat.id, cell_id=_uuid.uuid4(),
        cell_type_id=cell_type.id, duration_hours=24, amount_minor=1800,
        status=BookingStatus.AWAITING_PICKUP, recipient_phone="+99365000001",
    ))
    await session.commit()

    listed = await client.get("/api/v1/admin/postamats",
                              headers={"Authorization": f"Bearer {admin_token}"})
    item = listed.json()["items"][0]
    assert item["total_cell_count"] == 3
    assert item["occupied_cell_count"] == 1


async def test_the_detail_carries_the_device_and_the_block_reason(
    client, session, admin_token, postamat
):
    headers = {"Authorization": f"Bearer {admin_token}"}
    # The IP and MAC are edited on the postamat form even though the device is
    # its own resource.
    await client.patch(f"/api/v1/admin/postamats/{postamat.id}", headers=headers,
                       json={"device": {"ip_address": "10.0.0.9",
                                        "mac_address": "b8:27:eb:00:00:02"}})
    await client.post(f"/api/v1/admin/postamats/{postamat.id}/block",
                      headers=headers | _key(), json={"reason": "Сломан замок"})

    detail = await client.get(f"/api/v1/admin/postamats/{postamat.id}",
                              headers=headers)
    body = detail.json()
    assert body["device"]["ip_address"] == "10.0.0.9"
    assert body["blocked_reason"] == "Сломан замок"
    assert body["updated_at"].endswith("Z")

    unblocked = await client.post(f"/api/v1/admin/postamats/{postamat.id}/unblock",
                                  headers=headers | _key())
    # A stale reason on a working machine reads as a warning that no longer
    # applies.
    assert unblocked.json()["blocked_reason"] is None


async def test_working_hours_are_read_off_the_schedule_when_nobody_typed_them(
    client, admin_token, postamat
):
    headers = {"Authorization": f"Bearer {admin_token}"}
    await client.put(f"/api/v1/admin/postamats/{postamat.id}/schedule",
                     headers=headers | _key(),
                     json={"round_the_clock": False,
                           "days": [{"weekday": day, "opens_at": "09:00",
                                     "closes_at": "18:00"} for day in range(1, 6)]})

    detail = await client.get(f"/api/v1/admin/postamats/{postamat.id}",
                              headers=headers)
    assert detail.json()["working_hours"] == "09:00–18:00"


async def test_a_postamat_without_coordinates_omits_location(
    client, admin_token, city
):
    # The contract types `location` as a GeoPoint and does not make it nullable,
    # so a machine nobody has placed on the map leaves the field out rather than
    # sending null — which the panel's generated client refuses.
    headers = {"Authorization": f"Bearer {admin_token}"}
    created = await client.post("/api/v1/admin/postamats", headers=headers | _key(), json={
        "number": "10099", "name": "ТП без координат", "city_id": str(city.id),
        "address": "ул. Огузхан, 5",
    })
    assert created.status_code == 201
    assert "location" not in created.json()

    listed = await client.get("/api/v1/admin/postamats", headers=headers)
    assert all("location" not in item for item in listed.json()["items"])


async def test_coordinates_are_reported_when_they_exist(client, admin_token, city):
    headers = {"Authorization": f"Bearer {admin_token}"}
    created = await client.post("/api/v1/admin/postamats", headers=headers | _key(), json={
        "number": "10098", "name": "ТП на карте", "city_id": str(city.id),
        "address": "ул. Огузхан, 6", "location": {"lat": 37.95, "lon": 58.38},
    })
    assert created.json()["location"] == {"lat": 37.95, "lon": 58.38}


async def test_the_list_searches_number_name_and_address(
    client, admin_token, city, postamat
):
    # `q`, the contract's one search parameter, over what an operator has in
    # front of them when a customer telephones about «тот, что у почты».
    headers = {"Authorization": f"Bearer {admin_token}"}
    await client.post("/api/v1/admin/postamats", headers=headers | _key(), json={
        "number": "20001", "name": "ТП у почты", "city_id": str(city.id),
        "address": "ул. Почтовая, 1",
    })

    by_name = await client.get("/api/v1/admin/postamats?q=у почты", headers=headers)
    assert [item["number"] for item in by_name.json()["items"]] == ["20001"]

    by_address = await client.get("/api/v1/admin/postamats?q=Почтовая",
                                  headers=headers)
    assert len(by_address.json()["items"]) == 1

    by_number = await client.get(f"/api/v1/admin/postamats?q={postamat.number}",
                                 headers=headers)
    assert [item["id"] for item in by_number.json()["items"]] == [str(postamat.id)]


def _key():
    import uuid as _uuid

    return {"Idempotency-Key": str(_uuid.uuid4())}
