import uuid
from collections import defaultdict

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.db import get_session
from app.core.deps import require_permission
from app.core.errors import AppError, ErrorCode
from app.modules.audit.models import Source
from app.modules.audit.service import record
from app.modules.catalog.models import Tariff
from app.modules.identity.models import AdminUser

router = APIRouter(prefix="/admin/tariffs", tags=["admin-tariffs"])


class TariffEntry(BaseModel):
    city_id: uuid.UUID
    cell_type_id: uuid.UUID
    duration_hours: int
    amount_minor: int = Field(ge=0)
    currency: str = "TMT"

    @field_validator("duration_hours")
    @classmethod
    def _known_duration(cls, value: int) -> int:
        # The durations are a product constant, not free input: a price on a
        # duration nothing can be booked for is unreachable money.
        durations = get_settings().rental_durations
        if value not in durations:
            raise ValueError(f"duration_hours must be one of {list(durations)}")
        return value


class TariffMatrix(BaseModel):
    entries: list[TariffEntry]


class TariffList(BaseModel):
    items: list[TariffEntry]


@router.get("", response_model=TariffList)
async def list_tariffs(
    session: AsyncSession = Depends(get_session),
    _: AdminUser = Depends(require_permission("tariffs.read")),
) -> TariffList:
    rows = await session.scalars(
        select(Tariff).order_by(Tariff.city_id, Tariff.cell_type_id,
                                Tariff.duration_hours)
    )
    return TariffList(
        items=[TariffEntry.model_validate(row, from_attributes=True) for row in rows]
    )


@router.put("", response_model=TariffList)
async def replace_tariffs(
    payload: TariffMatrix,
    session: AsyncSession = Depends(get_session),
    admin: AdminUser = Depends(require_permission("tariffs.write")),
) -> TariffList:
    required = set(get_settings().rental_durations)
    seen: dict[tuple[uuid.UUID, uuid.UUID], set[int]] = defaultdict(set)
    for entry in payload.entries:
        seen[(entry.city_id, entry.cell_type_id)].add(entry.duration_hours)

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

    await session.execute(delete(Tariff))
    session.add_all([Tariff(**entry.model_dump()) for entry in payload.entries])
    await record(session, event="tariffs.replaced", source=Source.ADMIN,
                 message=f"Tariff matrix replaced with {len(payload.entries)} entries",
                 actor=admin.login)
    await session.commit()
    return TariffList(items=payload.entries)
