import uuid
from datetime import timedelta

from sqlalchemy import select
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
from app.modules.payments.models import BANK_CODES, Payment, PaymentStatus
from app.modules.payments.provider import WebhookEvent, get_payment_provider


LIVE_STATUSES = (PaymentStatus.PENDING, PaymentStatus.AUTHORIZED)


async def start_payment(
    session: AsyncSession,
    booking: Booking,
    client_id: uuid.UUID,
    *,
    bank_code: str,
    return_url: str | None = None,
) -> Payment:
    """Open a session at the acquirer and hand back where to send the customer.

    No card data passes through here, ever: the customer types it on the bank's
    own page, which is what keeps this server out of PCI DSS scope.
    """
    settings = get_settings()
    if bank_code not in BANK_CODES:
        raise AppError(ErrorCode.BANK_NOT_SUPPORTED, "Unknown bank.", 422,
                       details={"bank_code": bank_code})
    # An empty allow-list means no acquirer has shipped and the mock answers for
    # every bank. Once one does, the others grey out on the payment screen.
    if settings.supported_banks and bank_code not in settings.supported_banks:
        raise AppError(ErrorCode.BANK_NOT_SUPPORTED,
                       "This bank is not available yet.", 422,
                       details={"bank_code": bank_code})

    if booking.status == BookingStatus.CANCELLED:
        raise AppError(ErrorCode.BOOKING_ALREADY_CANCELLED,
                       "This booking is cancelled.", 409)
    if booking.paid_at is not None:
        raise AppError(ErrorCode.BOOKING_ALREADY_PAID, "This booking is paid.", 409)
    if booking.status != BookingStatus.PENDING_PAYMENT:
        raise AppError(ErrorCode.BOOKING_HOLD_EXPIRED,
                       "This booking is no longer awaiting payment.", 409,
                       details={"status": booking.status.value})
    if booking.hold_expires_at is not None and (
        booking_service.as_utc(booking.hold_expires_at) <= utcnow()
    ):
        raise AppError(ErrorCode.BOOKING_HOLD_EXPIRED, "The hold has run out.", 409)

    live = await session.scalar(
        select(Payment).where(
            Payment.booking_id == booking.id,
            Payment.status.in_(tuple(LIVE_STATUSES)),
        )
    )
    if live is not None:
        # A second session against one booking is two ways to take the same
        # money. The app polls the one it already has instead.
        raise AppError(ErrorCode.PAYMENT_ALREADY_EXISTS,
                       "A payment for this booking is already open.", 409,
                       details={"payment_id": str(live.id)})

    provider = get_payment_provider()
    payment = Payment(
        booking_id=booking.id, client_id=client_id, provider=provider.name,
        bank_code=bank_code, amount_minor=booking.amount_minor,
        currency=booking.currency,
        expires_at=utcnow() + timedelta(minutes=settings.payment_session_minutes),
    )
    session.add(payment)
    await session.flush()

    started = await provider.start(
        payment.id, payment.amount_minor, payment.currency,
        return_url or settings.payment_return_url,
    )
    payment.provider_payment_id = started.provider_payment_id
    payment.redirect_url = started.redirect_url
    await session.commit()
    return payment


async def cancel_payment(session: AsyncSession, payment: Payment) -> Payment:
    """The customer backed out of the bank's page on purpose."""
    if payment.status != PaymentStatus.PENDING:
        # Once the bank has the money reserved, walking away is between the
        # customer and the bank — this server cannot undo it, and there are no
        # refunds anywhere in this system.
        # str(), not .value: SQLAlchemy hands back the stored string for this
        # column, and a plain string has no .value to read.
        raise AppError(ErrorCode.PAYMENT_NOT_CANCELLABLE,
                       "This payment can no longer be cancelled.", 409,
                       details={"status": str(payment.status)})
    payment.status = PaymentStatus.CANCELLED
    payment.settled_at = utcnow()
    await session.commit()
    return payment


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
    if payment.status not in LIVE_STATUSES:
        return payment

    if event.amount_minor is not None and event.amount_minor != payment.amount_minor:
        # The acquirer says a different sum than the one this booking was priced
        # at. Settling anyway would either short the customer or credit them a
        # cell they did not pay for.
        raise AppError(
            ErrorCode.AMOUNT_MISMATCH, "Callback amount does not match.", 400,
            details={"expected": payment.amount_minor, "received": event.amount_minor},
        )

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
