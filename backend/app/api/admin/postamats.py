import uuid

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_session
from app.core.deps import require_permission
from app.core.errors import AppError, ErrorCode
from app.core.pagination import PageParams, page_params, paginate_page
from app.modules.audit.models import Source
from app.modules.audit.service import record
from app.modules.catalog.models import Postamat, PostamatSchedule, PostamatStatus
from app.modules.catalog.schemas import (
    BlockRequest,
    PostamatIn,
    PostamatOut,
    PostamatPage,
    PostamatPatch,
    SchedulePut,
    postamat_out,
)
from app.modules.identity.models import AdminUser

router = APIRouter(prefix="/admin/postamats", tags=["admin-postamats"])


async def _load(session: AsyncSession, postamat_id: uuid.UUID) -> Postamat:
    postamat = await session.get(Postamat, postamat_id)
    if postamat is None:
        raise AppError(ErrorCode.NOT_FOUND, "Postamat not found.", 404)
    return postamat


@router.get("", response_model=PostamatPage)
async def list_postamats(
    city_id: uuid.UUID | None = Query(default=None),
    status: PostamatStatus | None = Query(default=None),
    params: PageParams = Depends(page_params),
    session: AsyncSession = Depends(get_session),
    _: AdminUser = Depends(require_permission("postamats.read")),
) -> PostamatPage:
    stmt = select(Postamat).order_by(Postamat.number)
    if city_id is not None:
        stmt = stmt.where(Postamat.city_id == city_id)
    if status is not None:
        stmt = stmt.where(Postamat.status == status)
    rows, meta = await paginate_page(session, stmt, params)
    return PostamatPage(items=[postamat_out(row) for row in rows], pagination=meta)


@router.get("/{postamat_id}", response_model=PostamatOut)
async def get_postamat(
    postamat_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    _: AdminUser = Depends(require_permission("postamats.read")),
) -> PostamatOut:
    return postamat_out(await _load(session, postamat_id))


@router.post("", response_model=PostamatOut, status_code=201)
async def create_postamat(
    payload: PostamatIn,
    session: AsyncSession = Depends(get_session),
    admin: AdminUser = Depends(require_permission("postamats.write")),
) -> PostamatOut:
    # Checked rather than left to the unique index, so the operator gets the
    # field name back instead of a 500 from an IntegrityError.
    taken = await session.scalar(
        select(Postamat.id).where(Postamat.number == payload.number)
    )
    if taken is not None:
        raise AppError(ErrorCode.VALIDATION_FAILED, "Postamat number already exists.",
                       409, details={"field": "number"})
    postamat = Postamat(**payload.model_dump())
    session.add(postamat)
    await session.flush()
    await record(session, event="postamat.created", source=Source.ADMIN,
                 message=f"Postamat {postamat.number} created", actor=admin.login,
                 postamat_id=postamat.id)
    await session.commit()
    # created_at comes from a server default, so it is unloaded until the row is
    # read back. Refreshing here rather than letting the serializer touch it
    # keeps the load out of async lazy-load territory.
    await session.refresh(postamat)
    return postamat_out(postamat)


@router.patch("/{postamat_id}", response_model=PostamatOut)
async def update_postamat(
    postamat_id: uuid.UUID,
    payload: PostamatPatch,
    session: AsyncSession = Depends(get_session),
    admin: AdminUser = Depends(require_permission("postamats.write")),
) -> PostamatOut:
    postamat = await _load(session, postamat_id)
    changes = payload.model_dump(exclude_unset=True)
    for field, value in changes.items():
        setattr(postamat, field, value)
    # `number` and `status` are deliberately absent from PostamatPatch: the
    # number is what the machine is called on its own label, and a status change
    # has to carry a reason, which is what block/unblock exist for.
    await record(session, event="postamat.updated", source=Source.ADMIN,
                 message=f"Postamat {postamat.number} updated", actor=admin.login,
                 postamat_id=postamat.id, details={"fields": sorted(changes)})
    await session.commit()
    return postamat_out(postamat)


@router.post("/{postamat_id}/block", response_model=PostamatOut)
async def block_postamat(
    postamat_id: uuid.UUID,
    payload: BlockRequest,
    session: AsyncSession = Depends(get_session),
    admin: AdminUser = Depends(require_permission("postamats.write")),
) -> PostamatOut:
    postamat = await _load(session, postamat_id)
    postamat.status = PostamatStatus.BLOCKED
    await record(session, event="postamat.blocked", source=Source.ADMIN,
                 message=payload.reason, actor=admin.login, postamat_id=postamat.id)
    await session.commit()
    return postamat_out(postamat)


@router.post("/{postamat_id}/unblock", response_model=PostamatOut)
async def unblock_postamat(
    postamat_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    admin: AdminUser = Depends(require_permission("postamats.write")),
) -> PostamatOut:
    postamat = await _load(session, postamat_id)
    postamat.status = PostamatStatus.ACTIVE
    await record(session, event="postamat.unblocked", source=Source.ADMIN,
                 message=f"Postamat {postamat.number} returned to service",
                 actor=admin.login, postamat_id=postamat.id)
    await session.commit()
    return postamat_out(postamat)


@router.put("/{postamat_id}/schedule", response_model=PostamatOut)
async def set_schedule(
    postamat_id: uuid.UUID,
    payload: SchedulePut,
    session: AsyncSession = Depends(get_session),
    admin: AdminUser = Depends(require_permission("postamats.write")),
) -> PostamatOut:
    postamat = await _load(session, postamat_id)
    # A whole-week replacement rather than a per-day edit: the operator sees the
    # week as one form, and a partial update would leave a day nobody meant to
    # keep. delete-orphan on the relationship removes the rows that are gone.
    postamat.schedule = [
        PostamatSchedule(weekday=slot.weekday, opens_at=slot.opens_at,
                         closes_at=slot.closes_at)
        for slot in payload.slots
    ]
    await record(session, event="postamat.schedule_set", source=Source.ADMIN,
                 message=f"Schedule set for postamat {postamat.number}",
                 actor=admin.login, postamat_id=postamat.id,
                 details={"days": sorted(slot.weekday for slot in payload.slots)})
    await session.commit()
    return postamat_out(postamat)
