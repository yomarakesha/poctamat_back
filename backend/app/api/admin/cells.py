import uuid

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_session
from app.core.deps import require_permission
from app.core.errors import AppError, ErrorCode
from app.modules.audit.models import Source
from app.modules.audit.service import record
from app.modules.catalog.models import Cell
from app.modules.identity.models import AdminUser

router = APIRouter(prefix="/admin/cells", tags=["admin-cells"])


class CellSpec(BaseModel):
    number: int = Field(ge=1)
    board: int = Field(ge=0)
    output: int = Field(ge=0)
    row: int | None = None
    col: int | None = None


class BulkCreate(BaseModel):
    postamat_id: uuid.UUID
    cell_type_id: uuid.UUID
    cells: list[CellSpec] = Field(min_length=1)


class CellPatch(BaseModel):
    cell_type_id: uuid.UUID | None = None
    board: int | None = Field(default=None, ge=0)
    output: int | None = Field(default=None, ge=0)
    row: int | None = None
    col: int | None = None
    is_maintenance: bool | None = None


class BlockRequest(BaseModel):
    reason: str = Field(min_length=1, max_length=500)


class CellOut(BaseModel):
    id: uuid.UUID
    postamat_id: uuid.UUID
    cell_type_id: uuid.UUID
    number: int
    row: int | None
    col: int | None
    board: int
    output: int
    is_blocked: bool
    is_maintenance: bool
    blocked_reason: str | None


class CellList(BaseModel):
    items: list[CellOut]


def _out(cell: Cell) -> CellOut:
    return CellOut.model_validate(cell, from_attributes=True)


async def _load(session: AsyncSession, cell_id: uuid.UUID) -> Cell:
    cell = await session.get(Cell, cell_id)
    if cell is None:
        raise AppError(ErrorCode.NOT_FOUND, "Cell not found.", 404)
    return cell


@router.post("", response_model=CellList, status_code=201)
async def create_cells(
    payload: BulkCreate,
    session: AsyncSession = Depends(get_session),
    admin: AdminUser = Depends(require_permission("cells.write")),
) -> CellList:
    rows = [
        Cell(postamat_id=payload.postamat_id, cell_type_id=payload.cell_type_id,
             **spec.model_dump())
        for spec in payload.cells
    ]
    session.add_all(rows)
    try:
        # Flushed before the audit entry is written, because the audit write
        # flushes too and the constraint would otherwise fire from inside it,
        # outside this handler.
        await session.flush()
        await record(session, event="cells.created", source=Source.ADMIN,
                     message=f"{len(rows)} cells added", actor=admin.login,
                     postamat_id=payload.postamat_id,
                     details={"numbers": [spec.number for spec in payload.cells]})
        await session.commit()
    except IntegrityError:
        # The batch is one unit: a rollback here is what keeps a retry with the
        # corrected list from finding half a cabinet already in place.
        await session.rollback()
        raise AppError(ErrorCode.CELL_NUMBER_TAKEN,
                       "A cell with this number already exists in the postamat.",
                       409) from None

    return CellList(items=[_out(row) for row in rows])


@router.get("", response_model=CellList)
async def list_cells(
    postamat_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    _: AdminUser = Depends(require_permission("cells.read")),
) -> CellList:
    rows = await session.scalars(
        select(Cell).where(Cell.postamat_id == postamat_id).order_by(Cell.number)
    )
    return CellList(items=[_out(row) for row in rows])


@router.patch("/{cell_id}", response_model=CellOut)
async def update_cell(
    cell_id: uuid.UUID,
    payload: CellPatch,
    session: AsyncSession = Depends(get_session),
    admin: AdminUser = Depends(require_permission("cells.write")),
) -> CellOut:
    cell = await _load(session, cell_id)
    changes = payload.model_dump(exclude_unset=True)
    for field, value in changes.items():
        setattr(cell, field, value)
    # `number` is absent on purpose: it is the label physically on the door, so
    # changing it in the database alone would make the two disagree.
    await record(session, event="cell.updated", source=Source.ADMIN,
                 message=f"Cell {cell.number} updated", actor=admin.login,
                 postamat_id=cell.postamat_id, cell_id=cell.id,
                 details={"fields": sorted(changes)})
    await session.commit()
    return _out(cell)


@router.post("/{cell_id}/block", response_model=CellOut)
async def block_cell(
    cell_id: uuid.UUID,
    payload: BlockRequest,
    session: AsyncSession = Depends(get_session),
    admin: AdminUser = Depends(require_permission("cells.write")),
) -> CellOut:
    cell = await _load(session, cell_id)
    cell.is_blocked = True
    cell.blocked_reason = payload.reason
    await record(session, event="cell.blocked", source=Source.ADMIN,
                 message=payload.reason, actor=admin.login,
                 postamat_id=cell.postamat_id, cell_id=cell.id)
    await session.commit()
    return _out(cell)


@router.post("/{cell_id}/unblock", response_model=CellOut)
async def unblock_cell(
    cell_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    admin: AdminUser = Depends(require_permission("cells.write")),
) -> CellOut:
    cell = await _load(session, cell_id)
    cell.is_blocked = False
    # Cleared with the block: a stale reason on an open cell reads as a warning
    # that no longer applies.
    cell.blocked_reason = None
    await record(session, event="cell.unblocked", source=Source.ADMIN,
                 message=f"Cell {cell.number} returned to service",
                 actor=admin.login, postamat_id=cell.postamat_id, cell_id=cell.id)
    await session.commit()
    return _out(cell)
