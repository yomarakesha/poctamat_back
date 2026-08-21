import uuid
from typing import Literal

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy import inspect, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_session
from app.core.deps import require_permission
from app.core.errors import AppError, ErrorCode
from app.core.idempotency import require_idempotency_key
from app.core.pagination import CursorMeta, CursorParams, cursor_params, paginate_cursor
from app.core.types import utc_isoformat
from app.modules.booking.models import Booking
from app.modules.catalog.service import cell_numbers
from app.modules.custody import service
from app.modules.custody.models import CustodyRecord, CustodyStatus
from app.modules.identity.models import AdminUser
from app.modules.notify.models import NotificationChannel, NotificationKind
from app.modules.notify.service import notify

router = APIRouter(prefix="/admin/custody", tags=["admin-custody"])

# The contract's filter also names `pending_removal`, which is a booking waiting
# for an act rather than an act — no custody record can carry it, so it matches
# nothing here. That queue is `to_remove_bookings` on /admin/stats/attention.
PENDING_REMOVAL = "pending_removal"


class RemovalRequest(BaseModel):
    booking_id: uuid.UUID
    # Why the cell was opened. Separate from the description, which is what was
    # found inside — a dispute is usually about one or the other. Emptiness is
    # checked in the handler so it answers REASON_REQUIRED, the code the
    # contract names, rather than a generic validation error.
    reason: str = Field(max_length=500)
    description: str = Field(min_length=1, max_length=500)


class HandoverRequest(BaseModel):
    to: Literal["recipient", "sender", "other"]
    document_ref: str | None = Field(default=None, max_length=128)
    note: str | None = Field(default=None, max_length=500)


class DisposeRequest(BaseModel):
    outcome: Literal["returned_to_sender", "disposed"]
    reason: str = Field(max_length=500)


class HandoverOut(BaseModel):
    at: str
    to: str
    by: str
    document_ref: str | None
    note: str | None


class CustodyOut(BaseModel):
    id: uuid.UUID
    booking_id: uuid.UUID
    postamat_id: uuid.UUID
    # A door label rather than a quantity, and the contract types it as a string.
    cell_number: str
    status: CustodyStatus
    reason: str
    description: str
    removed_by: str
    removed_at: str
    disposal_due_at: str | None
    closed_by: str | None
    closing_reason: str | None
    handovers: list[HandoverOut]


class CustodyPage(BaseModel):
    items: list[CustodyOut]
    pagination: CursorMeta


def _out(record: CustodyRecord) -> CustodyOut:
    return CustodyOut(
        id=record.id, booking_id=record.booking_id, postamat_id=record.postamat_id,
        cell_number=str(record.cell_number), status=record.status,
        reason=record.reason, description=record.description,
        removed_by=record.removed_by, removed_at=utc_isoformat(record.removed_at),
        disposal_due_at=(utc_isoformat(record.disposal_due_at)
                         if record.disposal_due_at else None),
        closed_by=record.closed_by, closing_reason=record.closing_reason,
        handovers=[
            HandoverOut(at=utc_isoformat(row.created_at), to=row.to_whom,
                        by=row.by_admin, document_ref=row.document_ref, note=row.note)
            for row in record.handovers
        ],
    )


async def _with_handovers(
    session: AsyncSession, record: CustodyRecord
) -> CustodyRecord:
    """Make sure the handovers are in memory before anything serialises them.

    `lazy="selectin"` applies when a row is queried, not when it was just
    written, so a freshly created record hands back an unloaded collection —
    and touching one inside async code raises MissingGreenlet rather than
    loading it.
    """
    state = inspect(record)
    if state.persistent and "handovers" in state.unloaded:
        await session.refresh(record, ["handovers"])
    return record


async def _load(session: AsyncSession, record_id: uuid.UUID) -> CustodyRecord:
    record = await session.get(CustodyRecord, record_id)
    if record is None:
        raise AppError(ErrorCode.NOT_FOUND, "Custody record not found.", 404)
    return record


def _reason(value: str) -> str:
    if not value.strip():
        raise AppError(ErrorCode.REASON_REQUIRED,
                       "A custody act needs a stated reason.", 422,
                       details={"field": "reason"})
    return value


def _statuses(raw: str | None) -> list[CustodyStatus] | None:
    """Read the contract's comma-separated status filter.

    An unknown value is refused rather than ignored: a panel asking for a status
    this server does not have is a panel showing the operator a list that
    silently means something else.
    """
    if raw is None:
        return None
    wanted: list[CustodyStatus] = []
    for part in (piece.strip() for piece in raw.split(",")):
        if not part or part == PENDING_REMOVAL:
            continue
        try:
            wanted.append(CustodyStatus(part))
        except ValueError:
            raise AppError(ErrorCode.VALIDATION_ERROR,
                           "Unknown custody status.", 422,
                           details={"status": part}) from None
    return wanted


@router.get("", response_model=CustodyPage)
async def list_custody(
    status: str | None = Query(default=None),
    postamat_id: uuid.UUID | None = Query(default=None),
    params: CursorParams = Depends(cursor_params),
    session: AsyncSession = Depends(get_session),
    _: AdminUser = Depends(require_permission("custody.read")),
) -> CustodyPage:
    wanted = _statuses(status)
    stmt = select(CustodyRecord)
    if wanted is not None:
        stmt = stmt.where(CustodyRecord.status.in_(wanted))
    if postamat_id is not None:
        stmt = stmt.where(CustodyRecord.postamat_id == postamat_id)
    rows, meta = await paginate_cursor(session, stmt, params,
                                       CustodyRecord.removed_at)
    return CustodyPage(items=[_out(row) for row in rows], pagination=meta)


@router.get("/{record_id}", response_model=CustodyOut)
async def get_custody(
    record_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    _: AdminUser = Depends(require_permission("custody.read")),
) -> CustodyOut:
    return _out(await _load(session, record_id))


@router.post("", response_model=CustodyOut, status_code=201,
             dependencies=[Depends(require_idempotency_key)])
async def file_removal(
    payload: RemovalRequest,
    session: AsyncSession = Depends(get_session),
    admin: AdminUser = Depends(require_permission("custody.write")),
) -> CustodyOut:
    _reason(payload.reason)
    booking = await session.get(Booking, payload.booking_id)
    if booking is None:
        raise AppError(ErrorCode.NOT_FOUND, "Booking not found.", 404)

    numbers = await cell_numbers(session, [booking.cell_id])
    record = await service.file_removal(
        session, booking, numbers.get(booking.cell_id, 0), admin.login,
        payload.reason, payload.description,
    )
    await notify(
        session, client_id=booking.client_id, phone=None,
        kind=NotificationKind.PARCEL_REMOVED, language="ru",
        channel=NotificationChannel.IN_APP, booking_id=booking.id,
    )
    await session.commit()
    return _out(await _with_handovers(session, record))


@router.post("/{record_id}/handover", response_model=CustodyOut,
             dependencies=[Depends(require_idempotency_key)])
async def hand_over(
    record_id: uuid.UUID,
    payload: HandoverRequest,
    session: AsyncSession = Depends(get_session),
    admin: AdminUser = Depends(require_permission("custody.write")),
) -> CustodyOut:
    record = await _load(session, record_id)
    booking = await session.get(Booking, record.booking_id)
    await service.hand_over(session, record, booking, admin.login, payload.to,
                            payload.document_ref, payload.note)
    await session.commit()
    return _out(await _with_handovers(session, record))


@router.post("/{record_id}/dispose", response_model=CustodyOut,
             dependencies=[Depends(require_idempotency_key)])
async def dispose(
    record_id: uuid.UUID,
    payload: DisposeRequest,
    session: AsyncSession = Depends(get_session),
    admin: AdminUser = Depends(require_permission("custody.write")),
) -> CustodyOut:
    record = await _load(session, record_id)
    booking = await session.get(Booking, record.booking_id)
    await service.dispose(session, record, booking, admin.login, payload.outcome,
                          _reason(payload.reason))
    await session.commit()
    return _out(await _with_handovers(session, record))
