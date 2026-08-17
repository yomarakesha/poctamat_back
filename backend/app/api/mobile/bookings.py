import uuid

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_session
from app.core.deps import require_client
from app.core.errors import AppError, ErrorCode
from app.core.pagination import PageParams, page_params, paginate_page
from app.core.types import utc_isoformat
from app.modules.booking import service
from app.modules.booking.codes import reissue_code
from app.modules.booking.models import (
    Booking,
    BookingStatus,
    CELL_HELD_STATUSES,
    CodePurpose,
    Depositor,
)
from app.modules.booking.schemas import (
    BookingCreate,
    BookingCreated,
    BookingOut,
    BookingPage,
    CancelRequest,
    CourierCodeOut,
)
from app.modules.catalog.models import Cell, Postamat, PostamatStatus, Tariff
from app.modules.identity.models import Client

router = APIRouter(prefix="/bookings", tags=["bookings"])


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
        "expires_at": (
            utc_isoformat(booking.expires_at) if booking.expires_at else None
        ),
        "created_at": utc_isoformat(booking.created_at),
        "timeline": [
            {"status": event.status, "message": event.message,
             "at": utc_isoformat(event.created_at)}
            for event in booking.events
        ],
    }


async def _own_booking(
    session: AsyncSession, client: Client, booking_id: uuid.UUID
) -> Booking:
    booking = await session.get(Booking, booking_id)
    # Someone else's booking answers 404 rather than 403: whether a booking id
    # exists is not something a stranger gets to learn.
    if booking is None or booking.client_id != client.id:
        raise AppError(ErrorCode.NOT_FOUND, "Booking not found.", 404)
    return booking


@router.post("", response_model=BookingCreated, status_code=201)
async def create_booking(
    payload: BookingCreate,
    session: AsyncSession = Depends(get_session),
    client: Client = Depends(require_client),
) -> BookingCreated:
    postamat = await session.get(Postamat, payload.postamat_id)
    if postamat is None:
        raise AppError(ErrorCode.NOT_FOUND, "Postamat not found.", 404)
    if postamat.status == PostamatStatus.BLOCKED:
        raise AppError(ErrorCode.POSTAMAT_BLOCKED, "This postamat is out of service.",
                       409)

    tariff = await session.scalar(
        select(Tariff).where(
            Tariff.city_id == postamat.city_id,
            Tariff.cell_type_id == payload.cell_type_id,
            Tariff.duration_hours == payload.duration_hours,
        )
    )
    if tariff is None:
        # Refusing rather than inventing a price: a booking whose amount was
        # guessed is a payment nobody agreed to.
        raise AppError(ErrorCode.TARIFF_INCOMPLETE,
                       "This size and duration are not priced at this postamat.", 422)

    booking, codes = await service.create_booking(
        session, client_id=client.id, postamat_id=postamat.id,
        cell_type_id=payload.cell_type_id, duration_hours=payload.duration_hours,
        amount_minor=tariff.amount_minor, currency=tariff.currency,
        recipient_phone=payload.recipient_phone, recipient_name=payload.recipient_name,
        depositor=payload.depositor, courier_phone=payload.courier_phone,
    )
    return BookingCreated(**await _out(session, booking), codes=codes)


@router.get("", response_model=BookingPage)
async def list_bookings(
    active: bool = Query(default=False),
    params: PageParams = Depends(page_params),
    session: AsyncSession = Depends(get_session),
    client: Client = Depends(require_client),
) -> BookingPage:
    stmt = (
        select(Booking).where(Booking.client_id == client.id)
        .order_by(Booking.created_at.desc())
    )
    if active:
        stmt = stmt.where(Booking.status.in_(tuple(CELL_HELD_STATUSES)))
    rows, meta = await paginate_page(session, stmt, params)
    return BookingPage(
        items=[BookingOut(**await _out(session, row)) for row in rows], pagination=meta
    )


@router.get("/{booking_id}", response_model=BookingOut)
async def get_booking(
    booking_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    client: Client = Depends(require_client),
) -> BookingOut:
    booking = await _own_booking(session, client, booking_id)
    return BookingOut(**await _out(session, booking))


@router.post("/{booking_id}/cancel", response_model=BookingOut)
async def cancel_booking(
    booking_id: uuid.UUID,
    payload: CancelRequest,
    session: AsyncSession = Depends(get_session),
    client: Client = Depends(require_client),
) -> BookingOut:
    booking = await _own_booking(session, client, booking_id)
    await service.cancel(session, booking, reason=payload.reason, actor="client")
    await session.commit()
    return BookingOut(**await _out(session, booking))


@router.post("/{booking_id}/courier/resend", response_model=CourierCodeOut)
async def resend_courier_code(
    booking_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    client: Client = Depends(require_client),
) -> CourierCodeOut:
    booking = await _own_booking(session, client, booking_id)
    if booking.depositor != Depositor.COURIER or not booking.courier_phone:
        # Nothing to resend, and issuing a courier PIN for a booking nobody is
        # couriering would put a second live code in the sender's own hands.
        raise AppError(ErrorCode.BOOKING_INVALID_STATE,
                       "This booking has no courier.", 409)
    if booking.status not in {BookingStatus.PENDING_PAYMENT, BookingStatus.PAID,
                              BookingStatus.AWAITING_DEPOSIT}:
        raise AppError(ErrorCode.BOOKING_INVALID_STATE,
                       "The parcel has already been deposited.", 409)

    code = reissue_code(booking, CodePurpose.COURIER)
    await service.record_event(session, booking, booking.status,
                               "PIN курьера отправлен повторно")
    await session.commit()
    # Delivering it by SMS is the notify module's job, which arrives with the
    # provider in Plan 2b. Returning it here keeps the flow testable meanwhile.
    return CourierCodeOut(courier_code=code)
