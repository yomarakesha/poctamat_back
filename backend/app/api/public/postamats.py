import uuid

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_session, utcnow
from app.core.errors import AppError, ErrorCode
from app.core.pagination import PageParams, page_params, paginate_page
from app.modules.catalog.models import Postamat, PostamatStatus
from app.modules.catalog.schemas import (
    PostamatPublicOut,
    PostamatPublicPage,
    postamat_public_out,
)

router = APIRouter(tags=["postamats"])


# Blocked machines are invisible to clients rather than shown as unavailable:
# there is nothing a client can do with one, and listing it only invites a
# journey to a locked door. Maintenance stays visible, because it ends.
def _visible():
    return select(Postamat).where(Postamat.status != PostamatStatus.BLOCKED)


@router.get("/postamats", response_model=PostamatPublicPage)
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


@router.get("/postamats/{postamat_id}", response_model=PostamatPublicOut)
async def get_postamat(
    postamat_id: uuid.UUID, session: AsyncSession = Depends(get_session)
) -> PostamatPublicOut:
    postamat = await session.scalar(_visible().where(Postamat.id == postamat_id))
    if postamat is None:
        raise AppError(ErrorCode.NOT_FOUND, "Postamat not found.", 404)
    return postamat_public_out(postamat, utcnow())
