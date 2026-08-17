import uuid

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy import inspect, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_session
from app.core.deps import require_permission
from app.core.errors import AppError, ErrorCode
from app.core.pagination import PageMeta, PageParams, page_params, paginate_page
from app.core.types import utc_isoformat
from app.modules.booking.models import Booking
from app.modules.catalog.service import cell_numbers
from app.modules.custody import service
from app.modules.custody.models import CustodyRecord, CustodyStatus
from app.modules.identity.models import AdminUser
from app.modules.notify.models import NotificationChannel, NotificationKind
from app.modules.notify.service import notify

router = APIRouter(prefix="/admin/custody", tags=["admin-custody"])


class RemovalRequest(BaseModel):
    booking_id: uuid.UUID
    # The description is the whole evidentiary value of the act: without it the
    # record says a parcel existed and nothing about what it was.
    description: str = Field(min_length=1, max_length=500)


class HandoverRequest(BaseModel):
    to_whom: str = Field(pattern="^(recipient|sender)$")
    note: str | None = Field(default=None, max_length=500)


class DisposeRequest(BaseModel):
    reason: str = Field(min_length=1, max_length=500)


class HandoverOut(BaseModel):
    to_whom: str
    by_admin: str
    note: str | None
    at: str


class CustodyOut(BaseModel):
    id: uuid.UUID
    booking_id: uuid.UUID
    postamat_id: uuid.UUID
    cell_number: int
    status: CustodyStatus
    description: str
    removed_by: str
    removed_at: str
    closed_by: str | None
    closing_reason: str | None
    handovers: list[HandoverOut]


class CustodyPage(BaseModel):
    items: list[CustodyOut]
    pagination: PageMeta


def _out(record: CustodyRecord) -> CustodyOut:
    return CustodyOut(
        id=record.id, booking_id=record.booking_id, postamat_id=record.postamat_id,
        cell_number=record.cell_number, status=record.status,
        description=record.description, removed_by=record.removed_by,
        removed_at=utc_isoformat(record.removed_at), closed_by=record.closed_by,
        closing_reason=record.closing_reason,
        handovers=[
            HandoverOut(to_whom=row.to_whom, by_admin=row.by_admin, note=row.note,
                        at=utc_isoformat(row.created_at))
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


@router.get("", response_model=CustodyPage)
async def list_custody(
    status: CustodyStatus | None = Query(default=None),
    postamat_id: uuid.UUID | None = Query(default=None),
    params: PageParams = Depends(page_params),
    session: AsyncSession = Depends(get_session),
    _: AdminUser = Depends(require_permission("custody.read")),
) -> CustodyPage:
    stmt = select(CustodyRecord).order_by(CustodyRecord.removed_at.desc())
    if status is not None:
        stmt = stmt.where(CustodyRecord.status == status)
    if postamat_id is not None:
        stmt = stmt.where(CustodyRecord.postamat_id == postamat_id)
    rows, meta = await paginate_page(session, stmt, params)
    return CustodyPage(items=[_out(row) for row in rows], pagination=meta)


@router.get("/{record_id}", response_model=CustodyOut)
async def get_custody(
    record_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    _: AdminUser = Depends(require_permission("custody.read")),
) -> CustodyOut:
    return _out(await _load(session, record_id))


@router.post("", response_model=CustodyOut, status_code=201)
async def file_removal(
    payload: RemovalRequest,
    session: AsyncSession = Depends(get_session),
    admin: AdminUser = Depends(require_permission("custody.write")),
) -> CustodyOut:
    booking = await session.get(Booking, payload.booking_id)
    if booking is None:
        raise AppError(ErrorCode.NOT_FOUND, "Booking not found.", 404)

    numbers = await cell_numbers(session, [booking.cell_id])
    record = await service.file_removal(
        session, booking, numbers.get(booking.cell_id, 0), admin.login,
        payload.description,
    )
    await notify(
        session, client_id=booking.client_id, phone=None,
        kind=NotificationKind.PARCEL_REMOVED, language="ru",
        channel=NotificationChannel.IN_APP, booking_id=booking.id,
    )
    await session.commit()
    return _out(await _with_handovers(session, record))


@router.post("/{record_id}/handover", response_model=CustodyOut)
async def hand_over(
    record_id: uuid.UUID,
    payload: HandoverRequest,
    session: AsyncSession = Depends(get_session),
    admin: AdminUser = Depends(require_permission("custody.write")),
) -> CustodyOut:
    record = await _load(session, record_id)
    booking = await session.get(Booking, record.booking_id)
    await service.hand_over(session, record, booking, admin.login,
                            payload.to_whom, payload.note)
    await session.commit()
    return _out(await _with_handovers(session, record))


@router.post("/{record_id}/dispose", response_model=CustodyOut)
async def dispose(
    record_id: uuid.UUID,
    payload: DisposeRequest,
    session: AsyncSession = Depends(get_session),
    admin: AdminUser = Depends(require_permission("custody.write")),
) -> CustodyOut:
    record = await _load(session, record_id)
    booking = await session.get(Booking, record.booking_id)
    await service.dispose(session, record, booking, admin.login, payload.reason)
    await session.commit()
    return _out(await _with_handovers(session, record))
