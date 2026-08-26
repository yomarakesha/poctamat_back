from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_session
from app.core.errors import AppError, ErrorCode
from app.modules.notify import service
from app.modules.notify.sms import get_sms_provider

router = APIRouter(prefix="/webhooks", tags=["webhooks"])


@router.post("/sms/{provider}")
async def sms_delivery_callback(
    provider: str,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> dict[str, str]:
    """Unauthenticated by necessity, trusted only through its signature."""
    handler = get_sms_provider()
    if provider != handler.name:
        raise AppError(ErrorCode.NOT_FOUND, "Unknown SMS provider.", 404)

    body = await request.body()
    try:
        event = handler.parse_delivery_report(dict(request.headers), body)
    except (ValueError, KeyError):
        # Deliberately terse: someone probing this endpoint learns that the
        # request was rejected and nothing about which part failed.
        raise AppError(ErrorCode.CALLBACK_SIGNATURE_INVALID,
                       "Invalid webhook signature.", 400) from None

    await service.record_delivery(session, event)
    await session.commit()
    return {"status": "ok"}
