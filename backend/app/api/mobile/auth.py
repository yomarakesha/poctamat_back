from fastapi import APIRouter, Depends, Request, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.context import get_language
from app.core.db import get_session
from app.core.ratelimit import rate_limit
from app.modules.identity import service
from app.modules.identity.schemas import (
    OtpRequest,
    OtpRequestResult,
    OtpVerify,
    RefreshRequest,
    TokenPair,
)

router = APIRouter(tags=["auth"])


@router.post(
    "/auth/otp/request", response_model=OtpRequestResult,
    dependencies=[Depends(rate_limit("otp", limit=3, window_seconds=600))],
)
async def request_otp(
    payload: OtpRequest,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> OtpRequestResult:
    # The code itself is deliberately absent from this response: only its length
    # and lifetime travel over the wire, so the caller can render the input. The
    # code goes to the phone by SMS.
    ttl = await service.issue_otp(session, payload.phone, get_language(request))
    return OtpRequestResult(code_length=get_settings().otp_length, expires_in_seconds=ttl)


@router.post("/auth/otp/verify", response_model=TokenPair)
async def verify_otp(
    payload: OtpVerify, session: AsyncSession = Depends(get_session)
) -> TokenPair:
    await service.verify_otp(payload.phone, payload.code)
    client, is_new = await service.get_or_create_client(session, payload.phone)
    access, refresh = await service.issue_token_pair(session, "client", client.id)
    await session.commit()
    return TokenPair(access_token=access, refresh_token=refresh, is_new_client=is_new)


@router.post("/auth/refresh", response_model=TokenPair)
async def refresh(
    payload: RefreshRequest, session: AsyncSession = Depends(get_session)
) -> TokenPair:
    access, rotated = await service.rotate_refresh_token(
        session, payload.refresh_token, "client"
    )
    await session.commit()
    return TokenPair(access_token=access, refresh_token=rotated)


@router.post("/auth/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(
    payload: RefreshRequest, session: AsyncSession = Depends(get_session)
) -> Response:
    await service.revoke_refresh_token(session, payload.refresh_token, "client")
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
