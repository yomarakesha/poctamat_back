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
    created = await client.post("/api/v1/admin/postamats", headers=headers, json={
        "number": "10042", "name": "ТП #4", "city_id": str(city.id),
        "address": "ул. Ататюрк, 31", "round_the_clock": False,
    })
    assert created.status_code == 201
    postamat_id = created.json()["id"]
    assert created.json()["created_at"].endswith("Z")

    blocked = await client.post(f"/api/v1/admin/postamats/{postamat_id}/block",
                                headers=headers, json={"reason": "vandalised"})
    assert blocked.json()["status"] == "blocked"

    unblocked = await client.post(f"/api/v1/admin/postamats/{postamat_id}/unblock",
                                  headers=headers)
    assert unblocked.json()["status"] == "active"


async def test_duplicate_number_is_409(client, admin_token, city, postamat):
    headers = {"Authorization": f"Bearer {admin_token}"}
    response = await client.post("/api/v1/admin/postamats", headers=headers, json={
        "number": postamat.number, "name": "ТП #2", "city_id": str(city.id),
        "address": "ул. Гарашсызлык, 1",
    })
    assert response.status_code == 409
    assert response.json()["error"]["details"]["field"] == "number"


async def test_schedule_replaces_the_whole_week(client, admin_token, postamat):
    headers = {"Authorization": f"Bearer {admin_token}"}
    week = {"slots": [{"weekday": day, "opens_at": "08:00", "closes_at": "20:00"}
                      for day in range(7)]}
    response = await client.put(f"/api/v1/admin/postamats/{postamat.id}/schedule",
                                headers=headers, json=week)
    assert response.status_code == 200
    assert len(response.json()["schedule"]) == 7

    weekdays_only = {"slots": [{"weekday": day, "opens_at": "09:00",
                                "closes_at": "18:00"} for day in range(5)]}
    replaced = await client.put(f"/api/v1/admin/postamats/{postamat.id}/schedule",
                                headers=headers, json=weekdays_only)
    assert [slot["weekday"] for slot in replaced.json()["schedule"]] == [0, 1, 2, 3, 4]
    assert replaced.json()["schedule"][0]["opens_at"] == "09:00:00"


async def test_a_window_crossing_midnight_is_rejected(client, admin_token, postamat):
    headers = {"Authorization": f"Bearer {admin_token}"}
    response = await client.put(
        f"/api/v1/admin/postamats/{postamat.id}/schedule", headers=headers,
        json={"slots": [{"weekday": 0, "opens_at": "22:00", "closes_at": "06:00"}]},
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_FAILED"


async def test_the_same_weekday_twice_is_rejected(client, admin_token, postamat):
    headers = {"Authorization": f"Bearer {admin_token}"}
    response = await client.put(
        f"/api/v1/admin/postamats/{postamat.id}/schedule", headers=headers,
        json={"slots": [{"weekday": 0, "opens_at": "08:00", "closes_at": "12:00"},
                        {"weekday": 0, "opens_at": "13:00", "closes_at": "20:00"}]},
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
                      headers=headers, json={"reason": "vandalised"})

    hidden = await client.get("/api/v1/postamats")
    assert hidden.json()["items"] == []
    detail = await client.get(f"/api/v1/postamats/{postamat.id}")
    assert detail.status_code == 404


async def test_the_public_detail_reports_opening_state(client, admin_token, postamat):
    headers = {"Authorization": f"Bearer {admin_token}"}
    await client.put(f"/api/v1/admin/postamats/{postamat.id}/schedule", headers=headers,
                     json={"slots": [{"weekday": day, "opens_at": "00:00",
                                      "closes_at": "23:59"} for day in range(7)]})

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
