import uuid
from collections import defaultdict

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, field_validator
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.db import get_session
from app.core.deps import require_permission
from app.core.errors import AppError, ErrorCode
from app.core.idempotency import require_idempotency_key
from app.core.types import Money, utc_isoformat
from app.modules.audit.models import Source
from app.modules.audit.service import record
from app.modules.catalog.models import CellType, City, Tariff
from app.modules.identity.models import AdminUser

router = APIRouter(prefix="/admin/tariffs", tags=["admin-tariffs"])


class TariffWrite(BaseModel):
    city_id: uuid.UUID
    cell_type_id: uuid.UUID
    duration_hours: int
    price: Money

    @field_validator("duration_hours")
    @classmethod
    def _known_duration(cls, value: int) -> int:
        # The durations are a product constant, not free input: a price on a
        # duration nothing can be booked for is unreachable money.
        durations = get_settings().rental_durations
        if value not in durations:
            raise ValueError(f"duration_hours must be one of {list(durations)}")
        return value

    @field_validator("price")
    @classmethod
    def _not_negative(cls, value: Money) -> Money:
        if value.amount_minor < 0:
            raise ValueError("price.amount_minor must not be negative")
        return value


class TariffMatrix(BaseModel):
    items: list[TariffWrite]


class TariffOut(TariffWrite):
    updated_at: str


class TariffList(BaseModel):
    items: list[TariffOut]


def _out(row: Tariff) -> TariffOut:
    return TariffOut(
        city_id=row.city_id, cell_type_id=row.cell_type_id,
        duration_hours=row.duration_hours,
        price=Money(amount_minor=row.amount_minor, currency=row.currency),
        updated_at=utc_isoformat(row.updated_at),
    )


@router.get("", response_model=TariffList)
async def list_tariffs(
    city_id: uuid.UUID | None = Query(default=None),
    cell_type_id: uuid.UUID | None = Query(default=None),
    session: AsyncSession = Depends(get_session),
    _: AdminUser = Depends(require_permission("tariffs.read")),
) -> TariffList:
    stmt = select(Tariff).order_by(
        Tariff.city_id, Tariff.cell_type_id, Tariff.duration_hours
    )
    if city_id is not None:
        stmt = stmt.where(Tariff.city_id == city_id)
    if cell_type_id is not None:
        stmt = stmt.where(Tariff.cell_type_id == cell_type_id)
    rows = await session.scalars(stmt)
    return TariffList(items=[_out(row) for row in rows])


@router.put("", response_model=TariffList,
            dependencies=[Depends(require_idempotency_key)])
async def replace_tariffs(
    payload: TariffMatrix,
    session: AsyncSession = Depends(get_session),
    admin: AdminUser = Depends(require_permission("tariffs.write")),
) -> TariffList:
    required = set(get_settings().rental_durations)
    seen: dict[tuple[uuid.UUID, uuid.UUID], set[int]] = defaultdict(set)
    for item in payload.items:
        seen[(item.city_id, item.cell_type_id)].add(item.duration_hours)

    # Checked before anything is deleted: a matrix that would land half-priced
    # is refused whole, because a missing duration is a cell nobody can book.
    for (city_id, cell_type_id), durations in seen.items():
        missing = sorted(required - durations)
        if missing:
            raise AppError(
                ErrorCode.TARIFF_INCOMPLETE,
                "Every priced cell type must price all durations.", 422,
                details={"city_id": str(city_id), "cell_type_id": str(cell_type_id),
                         "missing": missing},
            )

    await _known_pairs(session, seen)

    await session.execute(delete(Tariff))
    session.add_all([
        Tariff(city_id=item.city_id, cell_type_id=item.cell_type_id,
               duration_hours=item.duration_hours,
               amount_minor=item.price.amount_minor, currency=item.price.currency)
        for item in payload.items
    ])
    await record(session, event="tariffs.replaced", source=Source.ADMIN,
                 message=f"Tariff matrix replaced with {len(payload.items)} entries",
                 actor=admin.login)
    await session.commit()

    rows = await session.scalars(
        select(Tariff).order_by(Tariff.city_id, Tariff.cell_type_id,
                                Tariff.duration_hours)
    )
    return TariffList(items=[_out(row) for row in rows])


async def _known_pairs(
    session: AsyncSession, seen: dict[tuple[uuid.UUID, uuid.UUID], set[int]]
) -> None:
    """Refuse a price hung on a city or a cell type that does not exist.

    The foreign keys would catch it too, but as an integrity error at flush time
    with no code the panel can act on — and by then the previous matrix is
    already deleted.
    """
    cities = {city_id for city_id, _ in seen}
    types = {cell_type_id for _, cell_type_id in seen}
    if cities:
        known = set(await session.scalars(
            select(City.id).where(City.id.in_(cities), City.is_active.is_(True))
        ))
        unknown = sorted(str(value) for value in cities - known)
        if unknown:
            raise AppError(ErrorCode.CITY_NOT_AVAILABLE,
                           "No such city, or the city is not active.", 422,
                           details={"city_id": unknown})
    if types:
        known = set(await session.scalars(
            select(CellType.id).where(CellType.id.in_(types))
        ))
        unknown = sorted(str(value) for value in types - known)
        if unknown:
            raise AppError(ErrorCode.NOT_FOUND, "No such cell type.", 404,
                           details={"cell_type_id": unknown})
