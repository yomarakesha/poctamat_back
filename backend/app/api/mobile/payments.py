import uuid

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_session
from app.core.deps import require_client
from app.core.errors import AppError, ErrorCode
from app.core.idempotency import require_idempotency_key
from app.core.types import utc_isoformat
from app.modules.booking.models import Booking
from app.modules.identity.models import Client
from app.modules.payments import service
from app.modules.payments.models import Payment, PaymentStatus

router = APIRouter(tags=["payments"])


class PaymentStarted(BaseModel):
    payment_id: uuid.UUID
    redirect_url: str
    amount_minor: int
    currency: str


class PaymentOut(BaseModel):
    id: uuid.UUID
    booking_id: uuid.UUID
    status: PaymentStatus
    amount_minor: int
    currency: str
    settled_at: str | None
    failure_reason: str | None


@router.post("/bookings/{booking_id}/payment", response_model=PaymentStarted,
             status_code=201, dependencies=[Depends(require_idempotency_key)])
async def start_payment(
    booking_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    client: Client = Depends(require_client),
) -> PaymentStarted:
    booking = await session.get(Booking, booking_id)
    if booking is None or booking.client_id != client.id:
        raise AppError(ErrorCode.NOT_FOUND, "Booking not found.", 404)

    payment, redirect_url = await service.start_payment(session, booking, client.id)
    return PaymentStarted(
        payment_id=payment.id, redirect_url=redirect_url,
        amount_minor=payment.amount_minor, currency=payment.currency,
    )


@router.get("/payments/{payment_id}", response_model=PaymentOut)
async def get_payment(
    payment_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    client: Client = Depends(require_client),
) -> PaymentOut:
    payment = await session.get(Payment, payment_id)
    if payment is None or payment.client_id != client.id:
        raise AppError(ErrorCode.NOT_FOUND, "Payment not found.", 404)
    return PaymentOut(
        id=payment.id, booking_id=payment.booking_id, status=payment.status,
        amount_minor=payment.amount_minor, currency=payment.currency,
        settled_at=utc_isoformat(payment.settled_at) if payment.settled_at else None,
        failure_reason=payment.failure_reason,
    )
