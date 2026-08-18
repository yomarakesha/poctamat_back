from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_session
from app.core.errors import AppError, ErrorCode
from app.modules.payments import service
from app.modules.payments.models import BANK_CODES
from app.modules.payments.provider import get_payment_provider

router = APIRouter(prefix="/webhooks", tags=["webhooks"])


@router.post("/payments/{provider}")
async def payment_callback(
    provider: str,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> dict[str, str]:
    """Unauthenticated by necessity, trusted only through its signature."""
    handler = get_payment_provider()
    # The contract types this parameter as a bank code, and the acquirer that
    # eventually ships will call the path named after it. `mock` stays callable
    # meanwhile, which is why both are accepted.
    if provider != handler.name and provider not in BANK_CODES:
        raise AppError(ErrorCode.NOT_FOUND, "Unknown payment provider.", 404)

    body = await request.body()
    try:
        event = handler.parse_webhook(dict(request.headers), body)
    except (ValueError, KeyError):
        # Deliberately terse: someone probing this endpoint learns that the
        # request was rejected and nothing about which part failed.
        raise AppError(ErrorCode.CALLBACK_SIGNATURE_INVALID,
                       "Invalid webhook signature.", 400) from None

    payment = await service.settle(session, event)
    return {"status": payment.status}
