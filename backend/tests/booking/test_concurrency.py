import asyncio
import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.core.errors import AppError
from app.modules.booking import service as service_module
from app.modules.booking.models import Booking, CELL_HELD_STATUSES, Depositor
from app.modules.booking.service import create_booking
from app.modules.catalog.models import Cell, CellType, City, Postamat


async def test_only_one_of_ten_parallel_bookings_takes_the_last_cell(
    file_engine, monkeypatch
):
    maker = async_sessionmaker(file_engine, expire_on_commit=False)

    # Widen the window between reading the pool and inserting the booking. On a
    # fast machine the ten attempts otherwise finish one after another and the
    # test passes without ever exercising a race — including with the write lock
    # removed, which is the regression it exists to catch.
    real_free_cell_ids = service_module.free_cell_ids

    async def slow_free_cell_ids(*args, **kwargs):
        free = await real_free_cell_ids(*args, **kwargs)
        await asyncio.sleep(0.05)
        return free

    monkeypatch.setattr(service_module, "free_cell_ids", slow_free_cell_ids)

    async with maker() as setup:
        city = City(code="ashgabat", name_tk="Aşgabat", name_ru="Ашхабад",
                    name_en="Ashgabat")
        cell_type = CellType(code="small", name_tk="Kiçi", name_ru="Маленький",
                             name_en="Small", width_cm=20, height_cm=20, depth_cm=40)
        setup.add_all([city, cell_type])
        await setup.flush()
        postamat = Postamat(number="10099", name="ТП #99", city_id=city.id,
                            address="ул. Ататюрк")
        setup.add(postamat)
        await setup.flush()
        setup.add(Cell(postamat_id=postamat.id, cell_type_id=cell_type.id,
                       number=1, board=1, output=1))
        await setup.commit()
        postamat_id, cell_type_id = postamat.id, cell_type.id

    async def attempt() -> str:
        async with maker() as session:
            try:
                await create_booking(
                    session, client_id=uuid.uuid4(), postamat_id=postamat_id,
                    cell_type_id=cell_type_id, duration_hours=24,
                    amount_minor=1800, currency="TMT",
                    recipient_phone="+99365000001", recipient_name=None,
                    depositor=Depositor.OWNER, courier_phone=None,
                )
                return "won"
            except AppError as error:
                return error.code

    results = await asyncio.gather(*(attempt() for _ in range(10)))

    assert results.count("won") == 1
    assert set(results) == {"won", "NO_FREE_CELLS"}

    async with maker() as check:
        held = await check.scalar(
            select(func.count()).select_from(Booking).where(
                Booking.status.in_(tuple(CELL_HELD_STATUSES))
            )
        )
        assert held == 1
