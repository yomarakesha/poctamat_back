import uuid
from datetime import datetime, time, timezone

from app.modules.catalog.models import Cell, PostamatSchedule
from app.modules.catalog.service import cell_ids_by_type, free_cell_ids, storage_expiry


async def _cells(session, postamat, cell_type, count: int) -> list[Cell]:
    rows = [
        Cell(postamat_id=postamat.id, cell_type_id=cell_type.id, number=n,
             board=1, output=n)
        for n in range(1, count + 1)
    ]
    session.add_all(rows)
    await session.commit()
    return rows


async def test_pools_are_grouped_by_type_and_ordered_by_door_number(
    session, postamat, cell_type
):
    await _cells(session, postamat, cell_type, 3)
    pools = await cell_ids_by_type(session, postamat.id)
    assert list(pools) == [cell_type.id]
    assert len(pools[cell_type.id]) == 3


async def test_blocked_and_maintenance_cells_are_not_in_the_pool(
    session, postamat, cell_type
):
    rows = await _cells(session, postamat, cell_type, 3)
    rows[0].is_blocked = True
    rows[1].is_maintenance = True
    await session.commit()

    pools = await cell_ids_by_type(session, postamat.id)
    assert pools[cell_type.id] == [rows[2].id]


async def test_free_cells_exclude_the_ones_already_taken(session, postamat, cell_type):
    rows = await _cells(session, postamat, cell_type, 3)
    free = await free_cell_ids(session, postamat.id, cell_type.id, taken={rows[0].id})
    assert free == [rows[1].id, rows[2].id]


async def test_an_unknown_type_has_an_empty_pool(session, postamat, cell_type):
    await _cells(session, postamat, cell_type, 2)
    assert await free_cell_ids(session, postamat.id, uuid.uuid4(), taken=set()) == []


def test_storage_expiry_is_pushed_past_the_next_opening(postamat):
    # Closes at 20:00; 12 hours bought at 19:00 would otherwise expire at 07:00,
    # an hour before the doors open and the recipient could physically arrive.
    postamat.schedule = [
        PostamatSchedule(weekday=day, opens_at=time(8, 0), closes_at=time(20, 0))
        for day in range(7)
    ]
    deposited = datetime(2026, 8, 17, 19, 0, tzinfo=timezone.utc)
    assert storage_expiry(postamat, deposited, 12) == datetime(
        2026, 8, 18, 8, 0, tzinfo=timezone.utc
    )


def test_storage_expiry_is_left_alone_when_the_site_is_open_at_that_hour(postamat):
    postamat.schedule = [
        PostamatSchedule(weekday=day, opens_at=time(8, 0), closes_at=time(20, 0))
        for day in range(7)
    ]
    deposited = datetime(2026, 8, 17, 9, 0, tzinfo=timezone.utc)
    assert storage_expiry(postamat, deposited, 4) == datetime(
        2026, 8, 17, 13, 0, tzinfo=timezone.utc
    )


def test_storage_expiry_of_a_round_the_clock_site_is_exactly_the_duration(postamat):
    postamat.round_the_clock = True
    deposited = datetime(2026, 8, 17, 19, 0, tzinfo=timezone.utc)
    assert storage_expiry(postamat, deposited, 12) == datetime(
        2026, 8, 18, 7, 0, tzinfo=timezone.utc
    )


def test_storage_expiry_of_a_postamat_without_a_schedule_is_the_plain_duration(postamat):
    # Nothing to push it to: a machine that never opens has no next opening, and
    # inventing one would be worse than leaving the plain clock alone.
    deposited = datetime(2026, 8, 17, 19, 0, tzinfo=timezone.utc)
    assert storage_expiry(postamat, deposited, 12) == datetime(
        2026, 8, 18, 7, 0, tzinfo=timezone.utc
    )
