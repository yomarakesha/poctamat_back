import uuid

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_session, utcnow
from app.core.deps import require_permission
from app.core.errors import AppError, ErrorCode
from app.core.idempotency import require_idempotency_key
from app.core.pagination import PageMeta, PageParams, page_params, paginate_page
from app.core.types import utc_isoformat
from app.modules.audit.models import Source
from app.modules.audit.service import record
from app.modules.booking.models import Booking
from app.modules.booking.views import FINISHED_STATUSES
from app.modules.catalog.models import Cell, CellType
from app.modules.identity.models import AdminUser

router = APIRouter(prefix="/admin/cell-types", tags=["admin-cell-types"])


class CellTypeOut(BaseModel):
    id: uuid.UUID
    code: str
    # One name on the wire, three in storage. The panel edits in one language at
    # a time and the app reads whichever the phone asks for.
    name: str
    name_tk: str
    name_ru: str
    name_en: str
    width_mm: int
    height_mm: int
    depth_mm: int
    is_active: bool
    cell_count: int
    blocked_at: str | None


class CellTypePage(BaseModel):
    items: list[CellTypeOut]
    pagination: PageMeta


class CellTypeWrite(BaseModel):
    code: str | None = Field(default=None, max_length=32)
    name: str | None = Field(default=None, max_length=100)
    # Optional per-language names, beyond the contract. Sending only `name`
    # fills all three, which is what a panel with one input does.
    name_tk: str | None = Field(default=None, max_length=50)
    name_ru: str | None = Field(default=None, max_length=50)
    name_en: str | None = Field(default=None, max_length=50)
    # Millimetres, with a ceiling of five metres: no drawer in a postamat is
    # larger, and an unbounded integer reaches the database as a number it
    # cannot store, which answers 500 instead of «так не бывает».
    width_mm: int | None = Field(default=None, ge=1, le=5000)
    height_mm: int | None = Field(default=None, ge=1, le=5000)
    depth_mm: int | None = Field(default=None, ge=1, le=5000)


def _out(row: CellType, cell_count: int) -> CellTypeOut:
    return CellTypeOut(
        id=row.id, code=row.code, name=row.name_ru,
        name_tk=row.name_tk, name_ru=row.name_ru, name_en=row.name_en,
        width_mm=row.width_mm, height_mm=row.height_mm, depth_mm=row.depth_mm,
        # `is_blocked` is the fact; `is_active` is how the contract spells it.
        is_active=not row.is_blocked, cell_count=cell_count,
        blocked_at=utc_isoformat(row.blocked_at) if row.blocked_at else None,
    )


async def _cell_counts(
    session: AsyncSession, type_ids: list[uuid.UUID]
) -> dict[uuid.UUID, int]:
    if not type_ids:
        return {}
    rows = await session.execute(
        select(Cell.cell_type_id, func.count())
        .where(Cell.cell_type_id.in_(type_ids))
        .group_by(Cell.cell_type_id)
    )
    return {type_id: count for type_id, count in rows}


async def _type_or_404(session: AsyncSession, cell_type_id: uuid.UUID) -> CellType:
    row = await session.get(CellType, cell_type_id)
    if row is None:
        raise AppError(ErrorCode.NOT_FOUND, "No such cell type.", 404)
    return row


async def _one(session: AsyncSession, row: CellType) -> CellTypeOut:
    counts = await _cell_counts(session, [row.id])
    return _out(row, counts.get(row.id, 0))


@router.get("", response_model=CellTypePage)
async def list_cell_types(
    include_blocked: bool = Query(default=False),
    params: PageParams = Depends(page_params),
    session: AsyncSession = Depends(get_session),
    _: AdminUser = Depends(require_permission("cells.read")),
) -> CellTypePage:
    stmt = select(CellType).order_by(CellType.width_mm)
    if not include_blocked:
        stmt = stmt.where(CellType.is_blocked.is_(False))
    rows, meta = await paginate_page(session, stmt, params)
    counts = await _cell_counts(session, [row.id for row in rows])
    return CellTypePage(
        items=[_out(row, counts.get(row.id, 0)) for row in rows], pagination=meta
    )


@router.post("", response_model=CellTypeOut, status_code=201,
             dependencies=[Depends(require_idempotency_key)])
async def create_cell_type(
    payload: CellTypeWrite,
    session: AsyncSession = Depends(get_session),
    actor: AdminUser = Depends(require_permission("cells.write")),
) -> CellTypeOut:
    missing = [field for field in ("code", "width_mm", "height_mm", "depth_mm")
               if getattr(payload, field) is None]
    if missing or not (payload.name or payload.name_ru):
        raise AppError(ErrorCode.VALIDATION_ERROR,
                       "A cell type needs a code, a name and its dimensions.", 422,
                       details={"missing": missing or ["name"]})
    if await session.scalar(select(CellType).where(CellType.code == payload.code)):
        raise AppError(ErrorCode.CONFLICT, "That code is taken.", 409,
                       details={"code": payload.code})

    name = payload.name or payload.name_ru
    row = CellType(
        code=payload.code,
        name_tk=payload.name_tk or name, name_ru=payload.name_ru or name,
        name_en=payload.name_en or name,
        width_mm=payload.width_mm, height_mm=payload.height_mm,
        depth_mm=payload.depth_mm,
    )
    session.add(row)
    await session.flush()
    await record(
        session, event="cell_type.created", source=Source.ADMIN, actor=actor.login,
        message=f"Добавлен размер ячейки {row.code}.",
        details={"cell_type_id": str(row.id)},
    )
    await session.commit()
    return await _one(session, row)


@router.patch("/{cell_type_id}", response_model=CellTypeOut)
async def update_cell_type(
    cell_type_id: uuid.UUID,
    payload: CellTypeWrite,
    session: AsyncSession = Depends(get_session),
    actor: AdminUser = Depends(require_permission("cells.write")),
) -> CellTypeOut:
    row = await _type_or_404(session, cell_type_id)
    changes = payload.model_dump(exclude_unset=True, exclude_none=True)
    if "name" in changes:
        # One input on the panel edits every language that was not sent
        # explicitly, rather than leaving two of them stale.
        name = changes.pop("name")
        for field in ("name_tk", "name_ru", "name_en"):
            changes.setdefault(field, name)
    for field, value in changes.items():
        setattr(row, field, value)

    await record(
        session, event="cell_type.updated", source=Source.ADMIN, actor=actor.login,
        message=f"Изменён размер ячейки {row.code}.",
        details={"cell_type_id": str(row.id), "fields": sorted(changes)},
    )
    await session.commit()
    return await _one(session, row)


@router.post("/{cell_type_id}/block", response_model=CellTypeOut,
             dependencies=[Depends(require_idempotency_key)])
async def block_cell_type(
    cell_type_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    actor: AdminUser = Depends(require_permission("cells.write")),
) -> CellTypeOut:
    row = await _type_or_404(session, cell_type_id)
    live = await session.scalar(
        select(func.count()).select_from(Booking).where(
            Booking.cell_type_id == row.id,
            Booking.status.not_in(tuple(FINISHED_STATUSES)),
        )
    ) or 0
    if live:
        # Blocking stops new bookings of this size. Doing it while parcels of
        # this size are still in cells would take the type out from under them.
        raise AppError(ErrorCode.CELL_TYPE_IN_USE,
                       "Bookings of this size are still live.", 409,
                       details={"active_bookings": live})

    # Blocked, not deleted: existing cells keep their type and past prices stay
    # readable.
    row.is_blocked = True
    row.blocked_at = utcnow()
    await record(
        session, event="cell_type.blocked", source=Source.ADMIN, actor=actor.login,
        message=f"Размер ячейки {row.code} заблокирован.",
        details={"cell_type_id": str(row.id)},
    )
    await session.commit()
    return await _one(session, row)


@router.post("/{cell_type_id}/unblock", response_model=CellTypeOut,
             dependencies=[Depends(require_idempotency_key)])
async def unblock_cell_type(
    cell_type_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    actor: AdminUser = Depends(require_permission("cells.write")),
) -> CellTypeOut:
    # Not in the contract, and unavoidable: a size blocked by mistake would
    # otherwise need a database edit to sell again.
    row = await _type_or_404(session, cell_type_id)
    row.is_blocked = False
    row.blocked_at = None
    await record(
        session, event="cell_type.unblocked", source=Source.ADMIN, actor=actor.login,
        message=f"Размер ячейки {row.code} разблокирован.",
        details={"cell_type_id": str(row.id)},
    )
    await session.commit()
    return await _one(session, row)
