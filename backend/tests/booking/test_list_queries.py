import pytest
from sqlalchemy import event

from app.modules.catalog.models import Cell


@pytest.fixture
def count_cell_queries(test_engine):
    """Count the statements that read the cells table."""
    seen: list[str] = []

    def before_execute(conn, cursor, statement, parameters, context, executemany):
        if "FROM cells" in statement:
            seen.append(statement)

    event.listen(test_engine.sync_engine, "before_cursor_execute", before_execute)
    yield seen
    event.remove(test_engine.sync_engine, "before_cursor_execute", before_execute)


async def _five_bookings(client, session, book, admin_token, city, cell_type, postamat):
    session.add_all([
        Cell(postamat_id=postamat.id, cell_type_id=cell_type.id, number=n,
             board=1, output=n)
        for n in range(1, 6)
    ])
    await session.commit()
    await client.put("/api/v1/admin/tariffs",
                     headers={"Authorization": f"Bearer {admin_token}"},
                     json={"entries": [
                         {"city_id": str(city.id), "cell_type_id": str(cell_type.id),
                          "duration_hours": hours, "amount_minor": 1800}
                         for hours in (12, 24, 48)
                     ]})
    body = {"postamat_id": str(postamat.id), "cell_type_id": str(cell_type.id),
            "duration_hours": 24, "recipient_phone": "+99365000001"}
    for _ in range(5):
        assert (await book(body)).status_code == 201


async def test_the_client_list_reads_cells_once_not_once_per_booking(
    client, session, book, client_token, admin_token, city, cell_type, postamat,
    count_cell_queries,
):
    await _five_bookings(client, session, book, admin_token, city, cell_type, postamat)

    count_cell_queries.clear()
    listed = await client.get("/api/v1/bookings",
                              headers={"Authorization": f"Bearer {client_token}"})

    assert len(listed.json()["items"]) == 5
    assert {item["cell_number"] for item in listed.json()["items"]} == {
        "1", "2", "3", "4", "5"
    }
    # One query for the page of cells. A per-row lookup is what turns a list of
    # twenty bookings into twenty round trips.
    assert len(count_cell_queries) == 1


async def test_the_admin_list_reads_cells_once_too(
    client, session, book, admin_token, city, cell_type, postamat, count_cell_queries
):
    await _five_bookings(client, session, book, admin_token, city, cell_type, postamat)

    count_cell_queries.clear()
    listed = await client.get("/api/v1/admin/bookings",
                              headers={"Authorization": f"Bearer {admin_token}"})

    assert len(listed.json()["items"]) == 5
    assert len(count_cell_queries) == 1
