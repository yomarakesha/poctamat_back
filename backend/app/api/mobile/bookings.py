import uuid

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.context import get_language
from app.core.db import get_session
from app.core.deps import require_client
from app.core.errors import AppError, ErrorCode
from app.core.idempotency import require_idempotency_key
from app.core.kvstore import get_kvstore
from app.core.pagination import CursorParams, cursor_params, paginate_cursor
from app.core.types import PhoneNumber, utc_isoformat
from app.modules.booking import service, views
from app.modules.booking.codes import code_expiry, issue_code
from app.modules.booking.models import (
    Booking,
    BookingStatus,
    CodePurpose,
    Depositor,
)
from app.modules.booking.schemas import (
    BookingCreate,
    BookingCursorPage,
    BookingDetail,
    CancelRequest,
    CodeIssued,
    Timeline,
    TimelineStep,
    TransferRequest,
)
from app.modules.catalog.models import Postamat, PostamatStatus, Tariff
from app.modules.identity.models import Client
from app.modules.notify.models import NotificationChannel, NotificationKind
from app.modules.notify.service import notify

router = APIRouter(prefix="/bookings", tags=["bookings"])

# The checklist on screen 262:3977, in the order it is drawn. Every step is
# returned whether or not it happened, so the app greys out the future ones
# instead of inventing an order of its own.
TIMELINE_STEPS = (
    "booked", "paid", "parcel_deposited", "pickup_code_sent", "collected",
    "expired", "cancelled",
)

# Cancelling is the client's own act, and only while the cell is still empty.
# Once a door has closed on a parcel, freeing the cell would hand somebody else
# a door with a stranger's parcel behind it.
CANCELLABLE = frozenset({BookingStatus.PENDING_PAYMENT, BookingStatus.AWAITING_DEPOSIT})


async def _detail(
    session: AsyncSession, request: Request, booking: Booking,
    *, deposit_code: str | None = None,
) -> BookingDetail:
    context = await views.load_context(session, [booking], with_payments=True)
    return views.detail(booking, context, get_language(request),
                        deposit_code=deposit_code)


async def _own_booking(
    session: AsyncSession, client: Client, booking_id: uuid.UUID
) -> Booking:
    booking = await session.get(Booking, booking_id)
    # Someone else's booking answers 404 rather than 403: whether a booking id
    # exists is not something a stranger gets to learn.
    if booking is None or booking.client_id != client.id:
        raise AppError(ErrorCode.NOT_FOUND, "Booking not found.", 404)
    return booking


def _resend_key(booking_id: uuid.UUID, purpose: CodePurpose) -> str:
    return f"code:resend:{booking_id}:{purpose.value}"


async def _guard_resend(booking_id: uuid.UUID, purpose: CodePurpose) -> int:
    """Refuse a second rotation inside the cooling-off window.

    Rotation mints a new code and kills the old one, so an unthrottled button is
    a way to invalidate somebody's live PIN over and over.
    """
    seconds = get_settings().code_resend_seconds
    store = get_kvstore()
    if await store.get(_resend_key(booking_id, purpose)):
        raise AppError(ErrorCode.CODE_RESEND_TOO_SOON, "Wait before asking again.",
                       429, details={"resend_after": seconds})
    await store.put(_resend_key(booking_id, purpose), {"sent": "1"}, seconds)
    return seconds


@router.post(
    "", response_model=BookingDetail, status_code=201,
    dependencies=[Depends(require_idempotency_key)],
)
async def create_booking(
    payload: BookingCreate,
    request: Request,
    session: AsyncSession = Depends(get_session),
    client: Client = Depends(require_client),
) -> BookingDetail:
    if not client.profile_complete:
        # The parcel is handed over against a name at the counter, and the
        # registration screen exists for exactly this moment.
        raise AppError(ErrorCode.PROFILE_INCOMPLETE,
                       "Fill in your name before booking.", 422)

    if payload.recipient_phone == client.phone:
        # Omitting the field is how a sender says "I will collect it myself".
        # Typing their own number is a different act — almost always a mistake —
        # and the contract gives it its own code.
        raise AppError(ErrorCode.RECIPIENT_PHONE_SAME_AS_SENDER,
                       "Leave the recipient empty to collect the parcel yourself.",
                       422)

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
        raise AppError(ErrorCode.DURATION_NOT_SUPPORTED,
                       "This size and duration are not priced at this postamat.", 422,
                       details={"duration_hours": payload.duration_hours})

    booking, deposit_code = await service.create_booking(
        session, client_id=client.id, postamat_id=postamat.id,
        cell_type_id=payload.cell_type_id, duration_hours=payload.duration_hours,
        amount_minor=tariff.amount_minor, currency=tariff.currency,
        # No recipient means the sender collects it themselves, so the grant and
        # every message about it go to their own number.
        recipient_phone=payload.recipient_phone or client.phone,
        recipient_name=payload.recipient_name,
        depositor=payload.deposited_by, courier_phone=payload.courier_phone,
    )
    if booking.courier_phone:
        # The courier holds the deposit code and only that: the person carrying
        # the box still cannot collect it later.
        await _send_code(session, booking, CodePurpose.DEPOSIT, deposit_code,
                         booking.courier_phone, client.language)
    await session.commit()
    return await _detail(session, request, booking, deposit_code=deposit_code)


@router.get("", response_model=BookingCursorPage)
async def list_bookings(
    request: Request,
    scope: str = Query(default="active", pattern="^(active|history)$"),
    params: CursorParams = Depends(cursor_params),
    session: AsyncSession = Depends(get_session),
    client: Client = Depends(require_client),
) -> BookingCursorPage:
    stmt = select(Booking).where(Booking.client_id == client.id)
    if scope == "active":
        stmt = stmt.where(Booking.status.not_in(tuple(views.FINISHED_STATUSES)))
    else:
        stmt = stmt.where(Booking.status.in_(tuple(views.FINISHED_STATUSES)))

    rows, meta = await paginate_cursor(session, stmt, params, Booking.created_at)
    context = await views.load_context(session, rows)
    language = get_language(request)
    return BookingCursorPage(
        items=[views.list_item(row, context, language) for row in rows],
        pagination=meta,
    )


@router.get("/{booking_id}", response_model=BookingDetail)
async def get_booking(
    booking_id: uuid.UUID,
    request: Request,
    session: AsyncSession = Depends(get_session),
    client: Client = Depends(require_client),
) -> BookingDetail:
    booking = await _own_booking(session, client, booking_id)
    return await _detail(session, request, booking)


@router.get("/{booking_id}/timeline", response_model=Timeline)
async def get_timeline(
    booking_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    client: Client = Depends(require_client),
) -> Timeline:
    booking = await _own_booking(session, client, booking_id)
    happened: dict[str, tuple[str, str | None]] = {}
    for event in booking.events:
        if event.step and event.step not in happened:
            actor = (event.details or {}).get("actor")
            happened[event.step] = (utc_isoformat(event.created_at), actor)

    return Timeline(items=[
        TimelineStep(
            step=step,
            occurred_at=happened[step][0] if step in happened else None,
            actor=happened[step][1] if step in happened else None,
        )
        for step in TIMELINE_STEPS
    ])


@router.post("/{booking_id}/cancel", response_model=BookingDetail,
             dependencies=[Depends(require_idempotency_key)])
async def cancel_booking(
    booking_id: uuid.UUID,
    request: Request,
    payload: CancelRequest | None = None,
    session: AsyncSession = Depends(get_session),
    client: Client = Depends(require_client),
) -> BookingDetail:
    booking = await _own_booking(session, client, booking_id)
    if booking.status == BookingStatus.CANCELLED:
        raise AppError(ErrorCode.BOOKING_ALREADY_CANCELLED,
                       "This booking is already cancelled.", 409)
    if booking.status not in CANCELLABLE:
        raise AppError(ErrorCode.BOOKING_NOT_CANCELLABLE,
                       "This booking can no longer be cancelled.", 409,
                       details={"status": booking.status.value})

    reason = (payload.reason if payload else None) or "Отменено клиентом"
    await service.cancel(session, booking, reason=reason, actor="client")
    await session.commit()
    return await _detail(session, request, booking)


@router.post("/{booking_id}/extend-hold", response_model=BookingDetail,
             dependencies=[Depends(require_idempotency_key)])
async def extend_hold(
    booking_id: uuid.UUID,
    request: Request,
    session: AsyncSession = Depends(get_session),
    client: Client = Depends(require_client),
) -> BookingDetail:
    booking = await _own_booking(session, client, booking_id)
    await service.extend_hold(session, booking)
    await session.commit()
    return await _detail(session, request, booking)


class RotateDepositRequest(BaseModel):
    # Send to a different courier and remember that number. Omitted reuses the
    # one captured when the booking was made.
    phone: PhoneNumber | None = None


async def _send_code(
    session: AsyncSession, booking: Booking, purpose: CodePurpose, code: str,
    phone: str, language: str,
) -> None:
    context = await views.load_context(session, [booking])
    cell = context.cells.get(booking.cell_id)
    kind = (NotificationKind.DEPOSIT_CODE if purpose == CodePurpose.DEPOSIT
            else NotificationKind.PICKUP_CODE_SENT)
    await notify(
        session, client_id=None, phone=phone, kind=kind, language=language,
        channel=NotificationChannel.SMS, booking_id=booking.id, code=code,
        cell_number=cell.number if cell else 0,
    )


@router.post("/{booking_id}/deposit-code/rotate", response_model=CodeIssued,
             dependencies=[Depends(require_idempotency_key)])
async def rotate_deposit_code(
    booking_id: uuid.UUID,
    payload: RotateDepositRequest | None = None,
    session: AsyncSession = Depends(get_session),
    client: Client = Depends(require_client),
) -> CodeIssued:
    booking = await _own_booking(session, client, booking_id)
    if booking.deposited_at is not None:
        # The parcel is already inside, so there is nothing left for this grant
        # to open.
        raise AppError(ErrorCode.GRANT_ALREADY_USED,
                       "The parcel has already been deposited.", 409)
    if booking.status not in CANCELLABLE:
        raise AppError(ErrorCode.GRANT_EXPIRED, "This booking is no longer open.", 409)

    resend_after = await _guard_resend(booking.id, CodePurpose.DEPOSIT)
    if payload and payload.phone:
        booking.courier_phone = payload.phone
        booking.depositor = Depositor.COURIER

    code = issue_code(booking, CodePurpose.DEPOSIT)
    target = booking.courier_phone or client.phone
    await _send_code(session, booking, CodePurpose.DEPOSIT, code, target,
                     client.language)
    await service.record_event(session, booking, booking.status,
                               "PIN для закладки выпущен заново")
    await session.commit()
    # The digits come back as well as going out by SMS: the sender reads them to
    # the courier when the message does not arrive, which is the case this
    # endpoint exists for.
    return CodeIssued(code=code,
                      expires_at=utc_isoformat(code_expiry(booking,
                                                           CodePurpose.DEPOSIT)),
                      resend_after=resend_after)


@router.post("/{booking_id}/pickup-code/rotate", response_model=CodeIssued,
             dependencies=[Depends(require_idempotency_key)])
async def rotate_pickup_code(
    booking_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    client: Client = Depends(require_client),
) -> CodeIssued:
    booking = await _own_booking(session, client, booking_id)
    if booking.pickup_code_sent_at is None:
        raise AppError(ErrorCode.PICKUP_CODE_NOT_ISSUED_YET,
                       "The parcel is not in the cell yet.", 409)
    if booking.collected_at is not None:
        raise AppError(ErrorCode.GRANT_ALREADY_USED,
                       "The parcel has already been collected.", 409)

    resend_after = await _guard_resend(booking.id, CodePurpose.PICKUP)
    code = issue_code(booking, CodePurpose.PICKUP)
    booking.pickup_code_sent_at = service.utcnow()
    await _send_code(session, booking, CodePurpose.PICKUP, code,
                     booking.recipient_phone, client.language)
    await service.record_event(session, booking, booking.status,
                               "PIN для получения выпущен заново",
                               step="pickup_code_sent")
    await session.commit()
    return CodeIssued(
        code=code,
        expires_at=utc_isoformat(code_expiry(booking, CodePurpose.PICKUP)),
        resend_after=resend_after,
    )


@router.post("/{booking_id}/pickup-code/transfer", response_model=BookingDetail,
             dependencies=[Depends(require_idempotency_key)])
async def transfer_pickup(
    booking_id: uuid.UUID,
    payload: TransferRequest,
    request: Request,
    session: AsyncSession = Depends(get_session),
    client: Client = Depends(require_client),
) -> BookingDetail:
    booking = await _own_booking(session, client, booking_id)
    if booking.collected_at is not None:
        raise AppError(ErrorCode.TRANSFER_NOT_ALLOWED,
                       "The parcel has already been collected.", 409)
    if booking.pickup_code_sent_at is None:
        raise AppError(ErrorCode.PICKUP_CODE_NOT_ISSUED_YET,
                       "The parcel is not in the cell yet.", 409)
    if payload.new_phone == booking.recipient_phone:
        raise AppError(ErrorCode.TRANSFER_PHONE_SAME,
                       "That is already the recipient's number.", 422)

    booking.recipient_phone = payload.new_phone
    booking.recipient_name = payload.new_name
    # People forward codes over messengers anyway. Transferring makes it an
    # explicit act: the old grant stops working the moment the new one is minted.
    code = issue_code(booking, CodePurpose.PICKUP)
    booking.pickup_code_sent_at = service.utcnow()
    await _send_code(session, booking, CodePurpose.PICKUP, code,
                     booking.recipient_phone, client.language)
    await service.record_event(session, booking, booking.status,
                               "Право получения передано",
                               details={"actor": "client"})
    await session.commit()
    return await _detail(session, request, booking)
