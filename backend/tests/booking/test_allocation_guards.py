import uuid

import pytest

from app.core.errors import AppError, ErrorCode
from app.modules.booking import service as service_module
from app.modules.booking.models import Booking, BookingStatus, Depositor
from app.modules.booking.service import create_booking
from app.modules.catalog.models import Cell


async def _one_cell(session, postamat, cell_type) -> Cell:
    cell = Cell(postamat_id=postamat.id, cell_type_id=cell_type.id, number=1,
                board=1, output=1)
    session.add(cell)
    await session.commit()
    return cell


def _args(postamat, cell_type) -> dict:
    return dict(
        client_id=uuid.uuid4(), postamat_id=postamat.id, cell_type_id=cell_type.id,
        duration_hours=24, amount_minor=1800, currency="TMT",
        recipient_phone="+99365000001", recipient_name=None,
        depositor=Depositor.OWNER, courier_phone=None,
    )


async def test_a_cell_the_index_refuses_is_reported_as_sold_out(
    session, monkeypatch, postamat, cell_type
):
    # The database is the last line of defence: if the in-application check ever
    # misses a held cell, the partial unique index refuses the insert. That must
    # reach the caller as "sold out", not as a 500.
    cell = await _one_cell(session, postamat, cell_type)
    session.add(Booking(
        client_id=uuid.uuid4(), postamat_id=postamat.id, cell_id=cell.id,
        cell_type_id=cell_type.id, duration_hours=24, amount_minor=1800,
        status=BookingStatus.AWAITING_PICKUP, recipient_phone="+99362123456",
    ))
    await session.commit()

    async def blind(*args, **kwargs):
        return set()

    monkeypatch.setattr(service_module, "_held_cell_ids", blind)

    with pytest.raises(AppError) as caught:
        await create_booking(session, **_args(postamat, cell_type))
    assert caught.value.code == ErrorCode.NO_FREE_CELLS
    assert caught.value.status_code == 409


async def test_pending_work_in_the_session_is_refused_rather_than_committed(
    session, postamat, cell_type
):
    # create_booking ends the caller's transaction to take the write lock. That
    # is safe for reads and destructive for writes, so unflushed work is a
    # programming error and has to say so instead of being committed silently.
    await _one_cell(session, postamat, cell_type)
    session.add(Cell(postamat_id=postamat.id, cell_type_id=cell_type.id, number=99,
                     board=9, output=9))

    with pytest.raises(RuntimeError, match="pending"):
        await create_booking(session, **_args(postamat, cell_type))
