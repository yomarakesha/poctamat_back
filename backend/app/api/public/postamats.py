import uuid
from collections import defaultdict

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import get_language
from app.core.db import get_session, utcnow
from app.core.errors import AppError, ErrorCode
from app.core.pagination import PageParams, page_params, paginate_page
from app.core.ratelimit import rate_limit
from app.modules.booking.models import Booking, CELL_HELD_STATUSES
from app.modules.catalog.models import CellType, Postamat, PostamatStatus, Tariff
from app.modules.catalog.schemas import (
    AvailabilityItem,
    AvailabilityOut,
    PostamatPublicOut,
    PostamatPublicPage,
    PriceOut,
    localized,
    postamat_public_out,
)
from app.modules.catalog.service import cell_ids_by_type

router = APIRouter(tags=["postamats"])


# Blocked machines are invisible to clients rather than shown as unavailable:
# there is nothing a client can do with one, and listing it only invites a
# journey to a locked door. Maintenance stays visible, because it ends.
def _visible():
    return select(Postamat).where(Postamat.status != PostamatStatus.BLOCKED)


# The postamat reads share the catalogue's bucket: they are the same
# unauthenticated surface, hit by the same screen.
@router.get(
    "/postamats", response_model=PostamatPublicPage,
    dependencies=[Depends(rate_limit("public", limit=120, window_seconds=60))],
)
async def list_postamats(
    city_id: uuid.UUID | None = Query(default=None),
    params: PageParams = Depends(page_params),
    session: AsyncSession = Depends(get_session),
) -> PostamatPublicPage:
    stmt = _visible().order_by(Postamat.number)
    if city_id is not None:
        stmt = stmt.where(Postamat.city_id == city_id)
    rows, meta = await paginate_page(session, stmt, params)
    moment = utcnow()
    return PostamatPublicPage(
        items=[postamat_public_out(row, moment) for row in rows], pagination=meta
    )


@router.get(
    "/postamats/{postamat_id}", response_model=PostamatPublicOut,
    dependencies=[Depends(rate_limit("public", limit=120, window_seconds=60))],
)
async def get_postamat(
    postamat_id: uuid.UUID, session: AsyncSession = Depends(get_session)
) -> PostamatPublicOut:
    postamat = await session.scalar(_visible().where(Postamat.id == postamat_id))
    if postamat is None:
        raise AppError(ErrorCode.NOT_FOUND, "Postamat not found.", 404)
    return postamat_public_out(postamat, utcnow())


@router.get(
    "/postamats/{postamat_id}/availability", response_model=AvailabilityOut,
    dependencies=[Depends(rate_limit("public", limit=120, window_seconds=60))],
)
async def postamat_availability(
    postamat_id: uuid.UUID,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> AvailabilityOut:
    """Free cells and prices per size — the booking screen in one request."""
    postamat = await session.scalar(_visible().where(Postamat.id == postamat_id))
    if postamat is None:
        raise AppError(ErrorCode.NOT_FOUND, "Postamat not found.", 404)

    language = get_language(request)
    pools = await cell_ids_by_type(session, postamat.id)
    # Occupancy is derived, never stored: two sources of truth for it is the
    # surest way to a cell that is double-sold or cannot be opened.
    held = set(await session.scalars(
        select(Booking.cell_id).where(
            Booking.postamat_id == postamat.id,
            Booking.status.in_(tuple(CELL_HELD_STATUSES)),
        )
    ))
    types = {
        row.id: row
        for row in await session.scalars(
            select(CellType).where(CellType.id.in_(list(pools) or [uuid.uuid4()]))
        )
    }
    prices: dict[uuid.UUID, list[PriceOut]] = defaultdict(list)
    for tariff in await session.scalars(
        select(Tariff).where(Tariff.city_id == postamat.city_id)
    ):
        prices[tariff.cell_type_id].append(PriceOut(
            duration_hours=tariff.duration_hours,
            amount_minor=tariff.amount_minor, currency=tariff.currency,
        ))

    items = []
    for cell_type_id, cell_ids in pools.items():
        cell_type = types.get(cell_type_id)
        if cell_type is None or cell_type.is_blocked:
            continue
        items.append(AvailabilityItem(
            cell_type_id=cell_type_id, code=cell_type.code,
            name=localized(cell_type, language), width_cm=cell_type.width_cm,
            height_cm=cell_type.height_cm, depth_cm=cell_type.depth_cm,
            free=sum(1 for cell_id in cell_ids if cell_id not in held),
            prices=sorted(prices[cell_type_id], key=lambda price: price.duration_hours),
        ))
    items.sort(key=lambda item: (item.width_cm, item.code))
    return AvailabilityOut(items=items)
