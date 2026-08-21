import uuid

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field, model_serializer
from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_session, utcnow
from app.core.deps import require_permission
from app.core.errors import AppError, ErrorCode
from app.core.idempotency import require_idempotency_key
from app.core.pagination import (
    CursorMeta,
    CursorParams,
    cursor_params,
    paginate_cursor,
)
from app.core.types import utc_isoformat
from app.modules.audit.models import Severity, Source
from app.modules.audit.service import record
from app.modules.booking.models import Booking, BookingStatus
from app.modules.booking.views import FINISHED_STATUSES
from app.modules.catalog.models import Cell, CellType, Postamat
from app.modules.identity.models import AdminUser, Client

router = APIRouter(prefix="/admin/cells", tags=["admin-cells"])


class BlockRequest(BaseModel):
    reason: str = Field(min_length=1, max_length=500)


class UnblockRequest(BaseModel):
    # Mandatory, because a blocked cell may still have a parcel in it and the
    # answer to «посылку забрали?» is the only record of where it went.
    parcel_fate: str = Field(
        pattern="^(collected|moved_to_counter|cell_was_empty|returned_to_sender)$"
    )
    note: str | None = Field(default=None, max_length=500)


class HardwareAddress(BaseModel):
    # Bounded well above any real board, and far below what would reach the
    # database as a number it cannot store: an unbounded integer from a form
    # field is a 500 waiting to happen.
    board: int = Field(ge=0, le=255)
    output: int = Field(ge=0, le=255)


class LayoutEntry(BaseModel):
    cell_type_id: uuid.UUID
    count: int = Field(ge=1, le=400)


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


class CellWrite(BaseModel):
    """One cell, as the panel's form sends it.

    `number` is a string on the wire because a door label is not arithmetic;
    this cabinet's labels happen to be digits, and one that is not is refused
    rather than silently renumbered.
    """

    postamat_id: uuid.UUID | None = None
    number: str | None = Field(default=None, max_length=16)
    row: int | None = Field(default=None, ge=1)
    col: int | None = Field(default=None, ge=1)
    row_span: int | None = Field(default=None, ge=1)
    col_span: int | None = Field(default=None, ge=1)
    hardware_address: HardwareAddress | None = None
    cell_type_id: uuid.UUID | None = None


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

    @model_serializer(mode="wrap")
    def _drop_unset_moment(self, handler):
        """Leave `status_changed_at` out rather than sending it as null.

        The contract types it as a Timestamp and does not make it nullable, so
        `null` fails the panel's generated validation. A cell whose status has
        never been touched simply has no such moment.
        """
        data = handler(self)
        if data.get("status_changed_at") is None:
            data.pop("status_changed_at", None)
        return data


class CellPage(BaseModel):
    items: list[CellWithState]
    pagination: CursorMeta


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


async def _load(session: AsyncSession, cell_id: uuid.UUID) -> Cell:
    cell = await session.get(Cell, cell_id)
    if cell is None:
        raise AppError(ErrorCode.NOT_FOUND, "Cell not found.", 404)
    return cell


def _door_number(number: str) -> int:
    if not number.isdigit():
        raise AppError(
            ErrorCode.VALIDATION_ERROR,
            "Door numbers are digits in this cabinet.", 422,
            details={"number": number},
        )
    return int(number)


def _uuids(values: list[str]) -> list[uuid.UUID]:
    """Read a comma-separated id filter without letting a typo become a 500."""
    parsed = []
    for value in values:
        try:
            parsed.append(uuid.UUID(value.strip()))
        except ValueError:
            raise AppError(ErrorCode.VALIDATION_ERROR, "Malformed identifier.", 422,
                           details={"cell_type_id": value}) from None
    return parsed


def _apply_write(cell: Cell, payload: CellWrite) -> list[str]:
    changed: list[str] = []
    for field in ("postamat_id", "cell_type_id", "row", "col",
                  "row_span", "col_span"):
        value = getattr(payload, field)
        if value is not None:
            setattr(cell, field, value)
            changed.append(field)
    if payload.number is not None:
        cell.number = _door_number(payload.number)
        changed.append("number")
    if payload.hardware_address is not None:
        cell.board = payload.hardware_address.board
        cell.output = payload.hardware_address.output
        changed.append("hardware_address")
    return changed


async def _hardware_taken(
    session: AsyncSession, cell: Cell, board: int, output: int
) -> bool:
    clash = await session.scalar(
        select(Cell.id).where(
            Cell.postamat_id == cell.postamat_id,
            Cell.board == board, Cell.output == output, Cell.id != cell.id,
        )
    )
    return clash is not None


@router.post("", response_model=ContractCell, status_code=201,
             dependencies=[Depends(require_idempotency_key)])
async def create_cell(
    payload: CellWrite,
    session: AsyncSession = Depends(get_session),
    admin: AdminUser = Depends(require_permission("cells.write")),
) -> ContractCell:
    """One cell. A whole cabinet goes through `/bulk`, which fills a grid."""
    missing = [
        field for field in ("postamat_id", "cell_type_id", "number",
                            "hardware_address")
        if getattr(payload, field) is None
    ]
    if missing:
        raise AppError(ErrorCode.VALIDATION_ERROR,
                       "A cell needs a postamat, a type, a number and an address.",
                       422, details={"missing": missing})

    cell = Cell(postamat_id=payload.postamat_id, cell_type_id=payload.cell_type_id,
                number=_door_number(payload.number), board=0, output=0)
    _apply_write(cell, payload)
    if await _hardware_taken(session, cell, cell.board, cell.output):
        raise AppError(ErrorCode.HARDWARE_ADDRESS_DUPLICATE,
                       "Another cell already drives that board output.", 409,
                       details={"board": cell.board, "output": cell.output})

    session.add(cell)
    try:
        # Flushed before the audit entry is written, because the audit write
        # flushes too and the constraint would otherwise fire from inside it,
        # outside this handler.
        await session.flush()
        await record(session, event="cell.created", source=Source.ADMIN,
                     message=f"Ячейка {cell.number} добавлена.", actor=admin.login,
                     postamat_id=cell.postamat_id, cell_id=cell.id,
                     details={"number": cell.number})
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise AppError(ErrorCode.CELL_NUMBER_DUPLICATE,
                       "A cell with this number already exists in the postamat.",
                       409, details={"number": payload.number}) from None
    return _contract_cell(cell)


@router.get("", response_model=CellPage)
async def list_cells(
    query: str | None = Query(default=None, alias="q", max_length=200),
    postamat_id: uuid.UUID | None = Query(default=None),
    status: str | None = Query(default=None,
                               description="Comma-separated CellStatus values."),
    cell_type_id: str | None = Query(default=None,
                                     description="Comma-separated list."),
    city_id: uuid.UUID | None = Query(default=None),
    params: CursorParams = Depends(cursor_params),
    session: AsyncSession = Depends(get_session),
    _: AdminUser = Depends(require_permission("cells.read")),
) -> CellPage:
    """Every cell in the fleet, with what is holding it — the monitor's table."""
    stmt = select(Cell)
    if postamat_id:
        stmt = stmt.where(Cell.postamat_id == postamat_id)
    if city_id:
        stmt = stmt.where(Cell.postamat_id.in_(
            select(Postamat.id).where(Postamat.city_id == city_id)
        ))
    if types := [part for part in (cell_type_id or "").split(",") if part.strip()]:
        stmt = stmt.where(Cell.cell_type_id.in_(_uuids(types)))
    if query:
        stmt = stmt.where(Cell.postamat_id.in_(
            select(Postamat.id).where(or_(
                Postamat.number.ilike(f"%{query}%"),
                Postamat.name.ilike(f"%{query}%"),
                Postamat.address.ilike(f"%{query}%"),
            ))
        ))

    rows, meta = await paginate_cursor(session, stmt, params, Cell.created_at)
    items = await _with_state_page(session, list(rows))
    # Status is computed from a booking rather than stored, so it is filtered
    # after the page is built. The alternative is duplicating the state machine
    # into SQL, where it would drift from the one in `_status`.
    if wanted := [part for part in (status or "").split(",") if part.strip()]:
        items = [item for item in items if item.status in wanted]
    return CellPage(items=items, pagination=meta)


@router.patch("/{cell_id}", response_model=ContractCell)
async def update_cell(
    cell_id: uuid.UUID,
    payload: CellWrite,
    session: AsyncSession = Depends(get_session),
    admin: AdminUser = Depends(require_permission("cells.write")),
) -> ContractCell:
    cell = await _load(session, cell_id)
    changed = _apply_write(cell, payload)
    if payload.hardware_address is not None and await _hardware_taken(
        session, cell, cell.board, cell.output
    ):
        raise AppError(ErrorCode.HARDWARE_ADDRESS_DUPLICATE,
                       "Another cell already drives that board output.", 409,
                       details={"board": cell.board, "output": cell.output})

    await record(session, event="cell.updated", source=Source.ADMIN,
                 message=f"Ячейка {cell.number} изменена.", actor=admin.login,
                 postamat_id=cell.postamat_id, cell_id=cell.id,
                 details={"fields": sorted(changed)})
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise AppError(ErrorCode.CELL_NUMBER_DUPLICATE,
                       "A cell with this number already exists in the postamat.",
                       409) from None
    return _contract_cell(cell)


@router.post("/{cell_id}/block", response_model=CellWithState,
             dependencies=[Depends(require_idempotency_key)])
async def block_cell(
    cell_id: uuid.UUID,
    payload: BlockRequest,
    session: AsyncSession = Depends(get_session),
    admin: AdminUser = Depends(require_permission("cells.write")),
) -> CellWithState:
    cell = await _load(session, cell_id)
    cell.is_blocked = True
    cell.blocked_reason = payload.reason
    cell.status_changed_at = utcnow()
    await record(session, event="cell.blocked", source=Source.ADMIN,
                 message=payload.reason, actor=admin.login,
                 postamat_id=cell.postamat_id, cell_id=cell.id,
                 details={"reason": payload.reason})
    await session.commit()
    return await _with_state(session, cell)


@router.post("/{cell_id}/unblock", response_model=CellWithState,
             dependencies=[Depends(require_idempotency_key)])
async def unblock_cell(
    cell_id: uuid.UUID,
    payload: UnblockRequest,
    session: AsyncSession = Depends(get_session),
    admin: AdminUser = Depends(require_permission("cells.write")),
) -> CellWithState:
    cell = await _load(session, cell_id)
    cell.is_blocked = False
    # Cleared with the block: a stale reason on an open cell reads as a warning
    # that no longer applies.
    cell.blocked_reason = None
    cell.status_changed_at = utcnow()
    # What happened to the parcel is the point of the call, not a footnote to
    # it: without this line a parcel that was in a blocked cell simply stops
    # being mentioned anywhere.
    await record(session, event="cell.unblocked", source=Source.ADMIN,
                 message=f"Ячейка {cell.number} возвращена в работу.",
                 actor=admin.login, postamat_id=cell.postamat_id, cell_id=cell.id,
                 details={"parcel_fate": payload.parcel_fate,
                          "note": payload.note})
    await session.commit()
    return await _with_state(session, cell)


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
    client = (await session.get(Client, booking.client_id)
              if booking is not None else None)
    return _state(
        cell, booking,
        await session.get(Postamat, cell.postamat_id),
        await session.get(CellType, cell.cell_type_id),
        client.phone if client else "",
    )


def _cell_type_brief(cell_type: CellType | None) -> CellTypeBrief | None:
    if cell_type is None:
        return None
    return CellTypeBrief(
        id=cell_type.id, code=cell_type.code, name=cell_type.name_ru,
        width_mm=cell_type.width_mm, height_mm=cell_type.height_mm,
        depth_mm=cell_type.depth_mm, is_active=not cell_type.is_blocked,
    )


def _held_by(booking: Booking | None, phone: str) -> HeldBy | None:
    if booking is None:
        return None
    return HeldBy(
        booking_id=booking.id, client_id=booking.client_id, client_phone=phone,
        expires_at=(
            utc_isoformat(booking.expires_at) if booking.expires_at
            else utc_isoformat(booking.hold_expires_at)
            if booking.hold_expires_at else None
        ),
    )


def _state(cell: Cell, booking: Booking | None, postamat: Postamat | None,
           cell_type: CellType | None, phone: str) -> CellWithState:
    return CellWithState(
        **_contract_cell(cell).model_dump(),
        status=_status(cell, booking),
        cell_type=_cell_type_brief(cell_type),
        postamat_number=postamat.number if postamat else "",
        postamat_address=postamat.address if postamat else "",
        held_by=_held_by(booking, phone),
        status_reason=cell.blocked_reason,
        status_changed_at=(
            utc_isoformat(cell.status_changed_at) if cell.status_changed_at else None
        ),
        # No door sensor until Plan 3. Null says "unknown"; false would claim the
        # door is shut on nobody's authority.
        door_open=None,
    )


async def _with_state_page(
    session: AsyncSession, cells: list[Cell]
) -> list[CellWithState]:
    """The monitor's rows for a page of cells, in four queries rather than 4n.

    At two hundred cabinets this table is the one screen an operator leaves
    open; a query per row is how a live view becomes a load test.
    """
    if not cells:
        return []
    postamats = {
        row.id: row for row in await session.scalars(
            select(Postamat).where(
                Postamat.id.in_({cell.postamat_id for cell in cells})
            )
        )
    }
    types = {
        row.id: row for row in await session.scalars(
            select(CellType).where(
                CellType.id.in_({cell.cell_type_id for cell in cells})
            )
        )
    }
    bookings = {
        row.cell_id: row for row in await session.scalars(
            select(Booking).where(
                Booking.cell_id.in_([cell.id for cell in cells]),
                Booking.status.not_in(tuple(FINISHED_STATUSES)),
            )
        )
    }
    phones = {
        row_id: phone for row_id, phone in await session.execute(
            select(Client.id, Client.phone).where(
                Client.id.in_({row.client_id for row in bookings.values()})
            )
        )
    } if bookings else {}

    return [
        _state(cell, bookings.get(cell.id), postamats.get(cell.postamat_id),
               types.get(cell.cell_type_id),
               phones.get(getattr(bookings.get(cell.id), "client_id", None), ""))
        for cell in cells
    ]


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
