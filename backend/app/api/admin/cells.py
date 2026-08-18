import uuid

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_session, utcnow
from app.core.deps import require_permission
from app.core.errors import AppError, ErrorCode
from app.core.idempotency import require_idempotency_key
from app.core.types import utc_isoformat
from app.modules.audit.models import Severity, Source
from app.modules.audit.service import record
from app.modules.booking.models import Booking, BookingStatus
from app.modules.booking.views import FINISHED_STATUSES
from app.modules.catalog.models import Cell, CellType, Postamat
from app.modules.identity.models import AdminUser, Client

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


class HardwareAddress(BaseModel):
    board: int = Field(ge=0)
    output: int = Field(ge=0)


class LayoutEntry(BaseModel):
    cell_type_id: uuid.UUID
    count: int = Field(ge=1)


class GridBulkCreate(BaseModel):
    """A whole cabinet in one form, the way the contract describes it.

    Nobody fills in 42 forms by hand. Positions run row by row across the grid
    and hardware addresses increment from `hardware_start`, which is exactly how
    the boards are wired.
    """

    postamat_id: uuid.UUID
    grid_rows: int = Field(ge=1, le=20)
    grid_cols: int = Field(ge=1, le=20)
    numbering_start: int = Field(default=1, ge=1)
    hardware_start: HardwareAddress = Field(
        default_factory=lambda: HardwareAddress(board=1, output=1)
    )
    layout: list[LayoutEntry] = Field(min_length=1)


class ContractCell(BaseModel):
    id: uuid.UUID
    postamat_id: uuid.UUID
    # A string on the wire: a door label is not arithmetic.
    number: str
    row: int | None
    col: int | None
    row_span: int | None
    col_span: int | None
    hardware_address: HardwareAddress | None
    cell_type_id: uuid.UUID


class BulkCreated(BaseModel):
    created_count: int
    items: list[ContractCell]


class HeldBy(BaseModel):
    booking_id: uuid.UUID
    client_id: uuid.UUID
    client_phone: str
    expires_at: str | None


class CellTypeBrief(BaseModel):
    id: uuid.UUID
    code: str
    name: str
    width_mm: int
    height_mm: int
    depth_mm: int
    is_active: bool


class CellWithState(ContractCell):
    status: str
    cell_type: CellTypeBrief | None
    postamat_number: str
    postamat_address: str
    held_by: HeldBy | None
    status_reason: str | None
    status_changed_at: str | None
    # Null until a door sensor reports, which is Plan 3. Absent would be a lie of
    # a different kind: the panel would draw «закрыта» for a door nobody asked.
    door_open: bool | None


class MaintenanceRequest(BaseModel):
    enabled: bool
    reason: str | None = Field(default=None, max_length=500)


class RemoteOpenRequest(BaseModel):
    # Ten characters minimum, as the contract states: «потому что» is not a
    # reason, and this is the most dangerous button in the panel.
    reason: str = Field(min_length=10, max_length=500)


def _contract_cell(cell: Cell) -> ContractCell:
    return ContractCell(
        id=cell.id, postamat_id=cell.postamat_id, number=str(cell.number),
        row=cell.row, col=cell.col, row_span=cell.row_span, col_span=cell.col_span,
        hardware_address=HardwareAddress(board=cell.board, output=cell.output),
        cell_type_id=cell.cell_type_id,
    )


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
        raise AppError(ErrorCode.CELL_NUMBER_DUPLICATE,
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


async def _live_booking(session: AsyncSession, cell: Cell) -> Booking | None:
    return await session.scalar(
        select(Booking).where(
            Booking.cell_id == cell.id,
            Booking.status.not_in(tuple(FINISHED_STATUSES)),
        )
    )


def _status(cell: Cell, booking: Booking | None) -> str:
    # Order matters: an operator's decision outranks whatever a booking says,
    # because that is what blocking is for.
    if cell.is_blocked:
        return "blocked"
    if cell.is_maintenance:
        return "maintenance"
    if booking is None:
        return "free"
    return "occupied" if booking.status == BookingStatus.AWAITING_PICKUP else "booked"


async def _with_state(session: AsyncSession, cell: Cell) -> CellWithState:
    booking = await _live_booking(session, cell)
    postamat = await session.get(Postamat, cell.postamat_id)
    cell_type = await session.get(CellType, cell.cell_type_id)

    held_by = None
    if booking is not None:
        client = await session.get(Client, booking.client_id)
        held_by = HeldBy(
            booking_id=booking.id, client_id=booking.client_id,
            client_phone=client.phone if client else "",
            expires_at=(
                utc_isoformat(booking.expires_at) if booking.expires_at
                else utc_isoformat(booking.hold_expires_at)
                if booking.hold_expires_at else None
            ),
        )

    return CellWithState(
        **_contract_cell(cell).model_dump(),
        status=_status(cell, booking),
        cell_type=(
            CellTypeBrief(
                id=cell_type.id, code=cell_type.code, name=cell_type.name_ru,
                width_mm=cell_type.width_mm, height_mm=cell_type.height_mm,
                depth_mm=cell_type.depth_mm, is_active=not cell_type.is_blocked,
            ) if cell_type else None
        ),
        postamat_number=postamat.number if postamat else "",
        postamat_address=postamat.address if postamat else "",
        held_by=held_by,
        status_reason=cell.blocked_reason,
        status_changed_at=(
            utc_isoformat(cell.status_changed_at) if cell.status_changed_at else None
        ),
        # No door sensor until Plan 3. Null says "unknown"; false would claim the
        # door is shut on nobody's authority.
        door_open=None,
    )


@router.post("/bulk", response_model=BulkCreated, status_code=201,
             dependencies=[Depends(require_idempotency_key)])
async def bulk_create_cells(
    payload: GridBulkCreate,
    session: AsyncSession = Depends(get_session),
    admin: AdminUser = Depends(require_permission("cells.write")),
) -> BulkCreated:
    total = sum(entry.count for entry in payload.layout)
    capacity = payload.grid_rows * payload.grid_cols
    if total > capacity:
        raise AppError(
            ErrorCode.LAYOUT_EXCEEDS_GRID,
            "The layout does not fit the grid.", 422,
            details={"cells": total, "capacity": capacity},
        )

    postamat = await session.get(Postamat, payload.postamat_id)
    if postamat is None:
        raise AppError(ErrorCode.NOT_FOUND, "Postamat not found.", 404)

    rows: list[Cell] = []
    number = payload.numbering_start
    board, output = payload.hardware_start.board, payload.hardware_start.output
    index = 0
    for entry in payload.layout:
        if await session.get(CellType, entry.cell_type_id) is None:
            raise AppError(ErrorCode.NOT_FOUND, "Cell type not found.", 404,
                           details={"cell_type_id": str(entry.cell_type_id)})
        for _ in range(entry.count):
            rows.append(Cell(
                postamat_id=postamat.id, cell_type_id=entry.cell_type_id,
                number=number,
                # Filled row by row across the grid, which is how the doors are
                # counted by somebody standing in front of the cabinet.
                row=index // payload.grid_cols + 1,
                col=index % payload.grid_cols + 1,
                board=board, output=output,
            ))
            number += 1
            output += 1
            index += 1

    session.add_all(rows)
    try:
        await session.flush()
        await record(session, event="cells.bulk_created", source=Source.ADMIN,
                     message=f"{len(rows)} ячеек добавлено.", actor=admin.login,
                     postamat_id=postamat.id,
                     details={"count": len(rows),
                              "grid": [payload.grid_rows, payload.grid_cols]})
        await session.commit()
    except IntegrityError:
        # Either the whole cabinet or none of it: a partial cabinet is worse
        # than no cabinet, and a retry with corrected numbers must not find half
        # of them already taken.
        await session.rollback()
        raise AppError(
            ErrorCode.BULK_CREATE_PARTIAL_FAILURE,
            "Some numbers or hardware addresses are taken; nothing was written.",
            409, details={"numbers": [row.number for row in rows]},
        ) from None

    return BulkCreated(created_count=len(rows),
                       items=[_contract_cell(row) for row in rows])


@router.get("/{cell_id}", response_model=CellWithState)
async def get_cell(
    cell_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    _: AdminUser = Depends(require_permission("cells.read")),
) -> CellWithState:
    return await _with_state(session, await _load(session, cell_id))


@router.post("/{cell_id}/maintenance", response_model=CellWithState,
             dependencies=[Depends(require_idempotency_key)])
async def set_maintenance(
    cell_id: uuid.UUID,
    payload: MaintenanceRequest,
    session: AsyncSession = Depends(get_session),
    admin: AdminUser = Depends(require_permission("cells.write")),
) -> CellWithState:
    cell = await _load(session, cell_id)
    if payload.enabled and await _live_booking(session, cell) is not None:
        # Planned work on a cell with somebody's parcel in it is not planned
        # work, it is a surprise for the owner.
        raise AppError(ErrorCode.CELL_HAS_ACTIVE_BOOKING,
                       "This cell is holding a booking.", 409)

    cell.is_maintenance = payload.enabled
    cell.blocked_reason = payload.reason if payload.enabled else None
    cell.status_changed_at = utcnow()
    # Maintenance is planned work and blocking is a security decision. The audit
    # log keeps them apart because the monitor filters on the difference.
    await record(session, event="cell.maintenance", source=Source.ADMIN,
                 message=payload.reason or "Обслуживание", actor=admin.login,
                 postamat_id=cell.postamat_id, cell_id=cell.id,
                 details={"enabled": payload.enabled})
    await session.commit()
    return await _with_state(session, cell)


@router.post("/{cell_id}/remote-open", status_code=202,
             dependencies=[Depends(require_idempotency_key)])
async def remote_open(
    cell_id: uuid.UUID,
    payload: RemoteOpenRequest,
    session: AsyncSession = Depends(get_session),
    admin: AdminUser = Depends(require_permission("cells.remote_open")),
) -> dict:
    cell = await _load(session, cell_id)
    # Recorded before the refusal, not after: that somebody reached for this
    # button is worth keeping whether or not the door moved.
    await record(
        session, event="cell.remote_open_requested", source=Source.ADMIN,
        severity=Severity.WARNING, message=payload.reason, actor=admin.login,
        postamat_id=cell.postamat_id, cell_id=cell.id,
        details={"cell_number": cell.number},
    )
    await session.commit()

    # Honest refusal until Plan 3: the lock agent this would command does not
    # exist yet, and a 202 would tell the operator a door opened when none did.
    raise AppError(
        ErrorCode.DEVICE_OFFLINE,
        "No lock agent is connected to this postamat yet.", 503,
        details={"postamat_id": str(cell.postamat_id)},
    )
