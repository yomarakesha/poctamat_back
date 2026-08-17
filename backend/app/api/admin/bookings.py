import uuid

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_session
from app.core.deps import require_permission
from app.core.errors import AppError, ErrorCode
from app.core.pagination import PageParams, page_params, paginate_page
from app.core.types import utc_isoformat
from app.modules.audit.models import Source
from app.modules.audit.service import record
from app.modules.booking import service
from app.modules.booking.models import Booking, BookingStatus
from app.modules.booking.schemas import BookingOut, BookingPage
from app.modules.catalog.models import Cell
from app.modules.identity.models import AdminUser

router = APIRouter(prefix="/admin/bookings", tags=["admin-bookings"])


class AdminCancelRequest(BaseModel):
    reason: str = Field(min_length=1, max_length=500)


async def _out(session: AsyncSession, booking: Booking) -> dict:
    cell = await session.get(Cell, booking.cell_id)
    return {
        "id": booking.id, "status": booking.status,
        "postamat_id": booking.postamat_id,
        "cell_number": cell.number if cell else 0,
        "cell_type_id": booking.cell_type_id,
        "duration_hours": booking.duration_hours,
        "amount_minor": booking.amount_minor, "currency": booking.currency,
        "recipient_phone": booking.recipient_phone,
        "recipient_name": booking.recipient_name, "depositor": booking.depositor,
        "hold_expires_at": (
            utc_isoformat(booking.hold_expires_at) if booking.hold_expires_at else None
        ),
        "expires_at": utc_isoformat(booking.expires_at) if booking.expires_at else None,
        "created_at": utc_isoformat(booking.created_at),
        "timeline": [
            {"status": event.status, "message": event.message,
             "at": utc_isoformat(event.created_at)}
            for event in booking.events
        ],
    }


async def _load(session: AsyncSession, booking_id: uuid.UUID) -> Booking:
    booking = await session.get(Booking, booking_id)
    if booking is None:
        raise AppError(ErrorCode.NOT_FOUND, "Booking not found.", 404)
    return booking


@router.get("", response_model=BookingPage)
async def list_bookings(
    status: BookingStatus | None = Query(default=None),
    postamat_id: uuid.UUID | None = Query(default=None),
    client_id: uuid.UUID | None = Query(default=None),
    params: PageParams = Depends(page_params),
    session: AsyncSession = Depends(get_session),
    _: AdminUser = Depends(require_permission("bookings.read")),
) -> BookingPage:
    stmt = select(Booking).order_by(Booking.created_at.desc())
    if status is not None:
        stmt = stmt.where(Booking.status == status)
    if postamat_id is not None:
        stmt = stmt.where(Booking.postamat_id == postamat_id)
    if client_id is not None:
        stmt = stmt.where(Booking.client_id == client_id)
    rows, meta = await paginate_page(session, stmt, params)
    return BookingPage(
        items=[BookingOut(**await _out(session, row)) for row in rows], pagination=meta
    )


@router.get("/{booking_id}", response_model=BookingOut)
async def get_booking(
    booking_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    _: AdminUser = Depends(require_permission("bookings.read")),
) -> BookingOut:
    return BookingOut(**await _out(session, await _load(session, booking_id)))


@router.post("/{booking_id}/cancel", response_model=BookingOut)
async def cancel_booking(
    booking_id: uuid.UUID,
    payload: AdminCancelRequest,
    session: AsyncSession = Depends(get_session),
    admin: AdminUser = Depends(require_permission("bookings.write")),
) -> BookingOut:
    booking = await _load(session, booking_id)
    await service.cancel(session, booking, reason=payload.reason, actor=admin.login)
    # An operator cancelling somebody else's booking is exactly what the audit
    # log is for: it frees a cell and it has a name attached.
    await record(session, event="booking.cancelled", source=Source.ADMIN,
                 message=payload.reason, actor=admin.login,
                 postamat_id=booking.postamat_id, cell_id=booking.cell_id,
                 details={"booking_id": str(booking.id)})
    await session.commit()
    return BookingOut(**await _out(session, booking))
