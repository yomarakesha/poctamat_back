import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.db import utcnow
from app.core.errors import AppError, ErrorCode
from app.modules.audit.models import Severity, Source
from app.modules.audit.service import record
from app.modules.booking import service as booking_service
from app.modules.booking.models import Booking, BookingStatus
from app.modules.catalog.service import cell_numbers
from app.modules.notify.models import NotificationChannel, NotificationKind
from app.modules.notify.service import notify
from app.modules.payments.models import Payment, PaymentStatus
from app.modules.payments.provider import WebhookEvent, get_payment_provider


async def start_payment(
    session: AsyncSession, booking: Booking, client_id: uuid.UUID
) -> tuple[Payment, str]:
    if booking.status != BookingStatus.PENDING_PAYMENT:
        raise AppError(ErrorCode.BOOKING_INVALID_STATE,
                       "This booking is not awaiting payment.", 409,
                       details={"status": booking.status.value})

    provider = get_payment_provider()
    payment = Payment(
        booking_id=booking.id, client_id=client_id, provider=provider.name,
        amount_minor=booking.amount_minor, currency=booking.currency,
    )
    session.add(payment)
    await session.flush()

    started = await provider.start(
        payment.id, payment.amount_minor, payment.currency,
        get_settings().payment_return_url,
    )
    payment.provider_payment_id = started.provider_payment_id
    await session.commit()
    return payment, started.redirect_url


async def settle(session: AsyncSession, event: WebhookEvent) -> Payment:
    """Apply a provider callback exactly once.

    Acquirers redeliver: the same event arrives twice or five times, and every
    extra delivery has to be a no-op rather than a second walk through the
    lifecycle.
    """
    payment = await session.get(Payment, event.payment_id)
    # Every status comparison below is `==`, never `is`: SQLAlchemy hands back
    # the stored string for these columns, and an identity check against the
    # enum member is silently false — a webhook that quietly does nothing.
    if payment is None:
        raise AppError(ErrorCode.NOT_FOUND, "Unknown payment.", 404)
    if payment.status != PaymentStatus.PENDING:
        return payment

    booking = await session.get(Booking, payment.booking_id)
    payment.settled_at = utcnow()
    payment.provider_payment_id = (
        event.provider_payment_id or payment.provider_payment_id
    )

    if not event.succeeded:
        payment.status = PaymentStatus.FAILED
        payment.failure_reason = event.reason
        if booking is not None and booking.status == BookingStatus.PENDING_PAYMENT:
            # payment_failed, not cancelled: the bank refused, the customer did
            # not change their mind, and the app shows a different screen.
            await booking_service.payment_failed(session, booking, event.reason)
        await session.commit()
        return payment

    payment.status = PaymentStatus.SUCCEEDED
    if booking is None or booking.status != BookingStatus.PENDING_PAYMENT:
        # Money for a booking that is gone: the hold ran out while the customer
        # was on the bank's page. This system does not refund (Ruling Q1), so
        # the only honest thing is to make it visible and let a person settle
        # it. Failing the webhook instead would make the acquirer retry forever
        # and would still leave the money here.
        await record(
            session, event="payment.needs_attention", source=Source.SYSTEM,
            severity=Severity.WARNING,
            message="Оплата поступила по брони, которая уже не активна.",
            details={"payment_id": str(payment.id),
                     "booking_id": str(payment.booking_id),
                     "amount_minor": payment.amount_minor},
        )
        await session.commit()
        return payment

    await booking_service.mark_paid(session, booking)
    numbers = await cell_numbers(session, [booking.cell_id])
    await notify(
        session, client_id=booking.client_id, phone=None,
        kind=NotificationKind.BOOKING_PAID, language="ru",
        channel=NotificationChannel.IN_APP, booking_id=booking.id,
        cell_number=numbers.get(booking.cell_id, 0),
    )
    await session.commit()
    return payment
