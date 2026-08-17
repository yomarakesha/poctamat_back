import uuid
from collections.abc import Sequence

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_session
from app.core.deps import require_permission
from app.core.errors import AppError, ErrorCode
from app.core.pagination import PageParams, page_params, paginate_page
from app.modules.audit.models import Source
from app.modules.audit.service import record
from app.modules.booking import service
from app.modules.booking.models import Booking, BookingStatus
from app.modules.booking.schemas import BookingOut, BookingPage, booking_out
from app.modules.catalog.service import cell_numbers
from app.modules.identity.models import AdminUser

router = APIRouter(prefix="/admin/bookings", tags=["admin-bookings"])


class AdminCancelRequest(BaseModel):
    reason: str = Field(min_length=1, max_length=500)


async def _out(session: AsyncSession, booking: Booking) -> BookingOut:
    numbers = await cell_numbers(session, [booking.cell_id])
    return booking_out(booking, numbers.get(booking.cell_id, 0))


async def _out_many(
    session: AsyncSession, bookings: Sequence[Booking]
) -> list[BookingOut]:
    # One query for every door number on the page, not one per booking.
    numbers = await cell_numbers(session, [row.cell_id for row in bookings])
    return [booking_out(row, numbers.get(row.cell_id, 0)) for row in bookings]


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
        items=await _out_many(session, rows), pagination=meta
    )


@router.get("/{booking_id}", response_model=BookingOut)
async def get_booking(
    booking_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    _: AdminUser = Depends(require_permission("bookings.read")),
) -> BookingOut:
    return await _out(session, await _load(session, booking_id))


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
    return await _out(session, booking)
