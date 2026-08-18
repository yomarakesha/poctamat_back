import uuid

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_session
from app.core.deps import require_client
from app.core.errors import AppError, ErrorCode
from app.core.idempotency import require_idempotency_key
from app.core.types import Money, utc_isoformat
from app.modules.booking.models import Booking
from app.modules.booking.schemas import PaymentBrief
from app.modules.identity.models import Client
from app.modules.payments import service
from app.modules.payments.models import Payment

router = APIRouter(tags=["payments"])


class PaymentStart(BaseModel):
    # Typed as a plain string rather than a Literal so an unknown bank answers
    # BANK_NOT_SUPPORTED — the code the payment screen renders — instead of a
    # generic validation failure.
    bank_code: str = Field(max_length=16)
    # Deep link the acquirer redirects back to, e.g. postamat://payment/result.
    return_url: str | None = Field(default=None, max_length=500)


def payment_out(payment: Payment) -> PaymentBrief:
    return PaymentBrief(
        id=payment.id, booking_id=payment.booking_id, status=payment.status,
        amount=Money(amount_minor=payment.amount_minor, currency=payment.currency),
        # The bank the customer picked, not the integration that talked to it:
        # until an acquirer ships, every bank runs through the mock.
        bank_code=payment.bank_code or payment.provider,
        redirect_url=payment.redirect_url,
        expires_at=utc_isoformat(payment.expires_at) if payment.expires_at else None,
        failure_code=payment.failure_reason,
        created_at=utc_isoformat(payment.created_at),
    )


async def _own_payment(
    session: AsyncSession, client: Client, payment_id: uuid.UUID
) -> Payment:
    payment = await session.get(Payment, payment_id)
    if payment is None or payment.client_id != client.id:
        raise AppError(ErrorCode.NOT_FOUND, "Payment not found.", 404)
    return payment


@router.post("/bookings/{booking_id}/payments", response_model=PaymentBrief,
             status_code=201, dependencies=[Depends(require_idempotency_key)])
async def start_payment(
    booking_id: uuid.UUID,
    payload: PaymentStart,
    session: AsyncSession = Depends(get_session),
    client: Client = Depends(require_client),
) -> PaymentBrief:
    booking = await session.get(Booking, booking_id)
    if booking is None or booking.client_id != client.id:
        raise AppError(ErrorCode.NOT_FOUND, "Booking not found.", 404)

    payment = await service.start_payment(
        session, booking, client.id,
        bank_code=payload.bank_code, return_url=payload.return_url,
    )
    return payment_out(payment)


@router.get("/payments/{payment_id}", response_model=PaymentBrief)
async def get_payment(
    payment_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    client: Client = Depends(require_client),
) -> PaymentBrief:
    # The authoritative answer for the result screen: the deep link back from
    # the WebView is a hint, and the app may have been killed while away.
    return payment_out(await _own_payment(session, client, payment_id))


@router.post("/payments/{payment_id}/cancel", response_model=PaymentBrief,
             dependencies=[Depends(require_idempotency_key)])
async def cancel_payment(
    payment_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    client: Client = Depends(require_client),
) -> PaymentBrief:
    payment = await _own_payment(session, client, payment_id)
    return payment_out(await service.cancel_payment(session, payment))
