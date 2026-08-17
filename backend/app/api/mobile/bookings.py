import uuid
from collections.abc import Sequence

from fastapi import APIRouter, Depends, Query, Response, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_session
from app.core.deps import require_client
from app.core.errors import AppError, ErrorCode
from app.core.idempotency import require_idempotency_key
from app.core.pagination import PageParams, page_params, paginate_page
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
    booking_out,
)
from app.modules.catalog.models import Postamat, PostamatStatus, Tariff
from app.modules.catalog.service import cell_numbers
from app.modules.notify.models import NotificationChannel, NotificationKind
from app.modules.notify.service import notify
from app.modules.identity.models import Client

router = APIRouter(prefix="/bookings", tags=["bookings"])


async def _out(session: AsyncSession, booking: Booking) -> BookingOut:
    numbers = await cell_numbers(session, [booking.cell_id])
    return booking_out(booking, numbers.get(booking.cell_id, 0))


async def _out_many(
    session: AsyncSession, bookings: Sequence[Booking]
) -> list[BookingOut]:
    # One query for every door number on the page, not one per booking.
    numbers = await cell_numbers(session, [row.cell_id for row in bookings])
    return [booking_out(row, numbers.get(row.cell_id, 0)) for row in bookings]


async def _own_booking(
    session: AsyncSession, client: Client, booking_id: uuid.UUID
) -> Booking:
    booking = await session.get(Booking, booking_id)
    # Someone else's booking answers 404 rather than 403: whether a booking id
    # exists is not something a stranger gets to learn.
    if booking is None or booking.client_id != client.id:
        raise AppError(ErrorCode.NOT_FOUND, "Booking not found.", 404)
    return booking


@router.post(
    "", response_model=BookingCreated, status_code=201,
    dependencies=[Depends(require_idempotency_key)],
)
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
    return BookingCreated(**(await _out(session, booking)).model_dump(), codes=codes)


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
        items=await _out_many(session, rows), pagination=meta
    )


@router.get("/{booking_id}", response_model=BookingOut)
async def get_booking(
    booking_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    client: Client = Depends(require_client),
) -> BookingOut:
    booking = await _own_booking(session, client, booking_id)
    return await _out(session, booking)


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
    return await _out(session, booking)


@router.post("/{booking_id}/courier/resend", status_code=202)
async def resend_courier_code(
    booking_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    client: Client = Depends(require_client),
) -> Response:
    booking = await _own_booking(session, client, booking_id)
    if booking.depositor != Depositor.COURIER or not booking.courier_phone:
        # Nothing to resend, and issuing a courier PIN for a booking nobody is
        # couriering would put a second live code in the sender's own hands.
        raise AppError(ErrorCode.BOOKING_INVALID_STATE,
                       "This booking has no courier.", 409)
    if booking.status not in {BookingStatus.PENDING_PAYMENT,
                              BookingStatus.AWAITING_DEPOSIT}:
        raise AppError(ErrorCode.BOOKING_INVALID_STATE,
                       "The parcel has already been deposited.", 409)

    code = reissue_code(booking, CodePurpose.COURIER)
    numbers = await cell_numbers(session, [booking.cell_id])
    await notify(
        session, client_id=None, phone=booking.courier_phone,
        kind=NotificationKind.COURIER_CODE, language=client.language,
        channel=NotificationChannel.SMS, booking_id=booking.id,
        code=code, cell_number=numbers.get(booking.cell_id, 0),
    )
    await service.record_event(session, booking, booking.status,
                               "PIN курьера отправлен повторно")
    await session.commit()
    # 202 and no body: the code goes to the courier's phone and never back
    # through the sender's screen, which is the whole point of a courier PIN.
    return Response(status_code=status.HTTP_202_ACCEPTED)
