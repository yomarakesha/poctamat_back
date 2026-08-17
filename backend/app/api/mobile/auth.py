from fastapi import APIRouter, Depends, Request, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.context import get_language
from app.core.db import get_session
from app.core.ratelimit import rate_limit
from app.core.types import utc_isoformat
from app.modules.identity import service
from app.modules.identity.schemas import (
    ClientTokenPair,
    OtpRequest,
    OtpRequestResult,
    OtpVerify,
    RefreshRequest,
    TokenPair,
)

router = APIRouter(tags=["auth"])


def _expires_in() -> int:
    return get_settings().access_token_ttl_minutes * 60


@router.post(
    "/auth/otp/request", response_model=OtpRequestResult,
    dependencies=[Depends(rate_limit("otp", limit=3, window_seconds=600))],
)
async def request_otp(
    payload: OtpRequest,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> OtpRequestResult:
    # The code itself is deliberately absent from this response: it travels by
    # SMS, and what comes back is only the request id to quote when verifying.
    request_id, expires_at, resend_after = await service.issue_otp(
        session, payload.phone, get_language(request)
    )
    return OtpRequestResult(
        request_id=request_id, code_length=get_settings().otp_length,
        expires_at=utc_isoformat(expires_at), resend_after=resend_after,
    )


@router.post("/auth/otp/verify", response_model=ClientTokenPair)
async def verify_otp(
    payload: OtpVerify, session: AsyncSession = Depends(get_session)
) -> ClientTokenPair:
    phone = await service.verify_otp(str(payload.request_id), payload.code)
    client, _ = await service.get_or_create_client(session, phone)
    access, refresh = await service.issue_token_pair(session, "client", client.id)
    await session.commit()
    return ClientTokenPair(
        access_token=access, refresh_token=refresh, expires_in=_expires_in(),
        profile_complete=client.profile_complete,
    )


@router.post("/auth/refresh", response_model=TokenPair)
async def refresh(
    payload: RefreshRequest, session: AsyncSession = Depends(get_session)
) -> TokenPair:
    access, rotated = await service.rotate_refresh_token(
        session, payload.refresh_token, "client"
    )
    await session.commit()
    return TokenPair(access_token=access, refresh_token=rotated,
                     expires_in=_expires_in())


@router.post("/auth/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(
    payload: RefreshRequest, session: AsyncSession = Depends(get_session)
) -> Response:
    await service.revoke_refresh_token(session, payload.refresh_token, "client")
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
