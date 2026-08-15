from fastapi import APIRouter, Depends, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import get_language
from app.core.db import get_session
from app.modules.catalog.models import CellType, City
from app.modules.catalog.schemas import CellTypeOut, CityOut, localized

router = APIRouter(tags=["catalog"])


# Neither list is paginated: both are small fixed reference tables that a client
# fetches once and caches, so a page envelope would only get in the way.
@router.get("/cities", response_model=list[CityOut])
async def list_cities(
    request: Request, session: AsyncSession = Depends(get_session)
) -> list[CityOut]:
    language = get_language(request)
    rows = await session.scalars(
        select(City).where(City.is_active.is_(True)).order_by(City.code)
    )
    return [
        CityOut(id=row.id, code=row.code, name=localized(row, language)) for row in rows
    ]


@router.get("/cell-types", response_model=list[CellTypeOut])
async def list_cell_types(
    request: Request, session: AsyncSession = Depends(get_session)
) -> list[CellTypeOut]:
    language = get_language(request)
    rows = await session.scalars(
        select(CellType).where(CellType.is_blocked.is_(False)).order_by(CellType.width_cm)
    )
    return [
        CellTypeOut(
            id=row.id, code=row.code, name=localized(row, language),
            width_cm=row.width_cm, height_cm=row.height_cm, depth_cm=row.depth_cm,
        )
        for row in rows
    ]
