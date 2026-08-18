import uuid

from fastapi import APIRouter, Depends, Request, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.context import get_language
from app.core.db import get_session
from app.core.deps import require_client
from app.core.ratelimit import rate_limit
from app.core.types import utc_isoformat
from app.modules.identity import service
from app.modules.identity.models import Client
from app.modules.identity.schemas import (
    ClientTokenPair,
    LogoutRequest,
    OtpRequest,
    OtpRequestResult,
    OtpVerify,
    PushTokenCreated,
    PushTokenRequest,
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
    payload: LogoutRequest | None = None,
    session: AsyncSession = Depends(get_session),
    client: Client = Depends(require_client),
) -> Response:
    body = payload or LogoutRequest()
    await service.revoke_client_session(session, client.id, body.refresh_token)
    if body.push_token:
        # Silencing the device is part of signing out: a token left registered
        # keeps pushing about a session that no longer exists.
        await service.revoke_push_token_value(session, client.id, body.push_token)
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/auth/push-tokens", response_model=PushTokenCreated,
             status_code=status.HTTP_201_CREATED)
async def register_push_token(
    payload: PushTokenRequest,
    session: AsyncSession = Depends(get_session),
    client: Client = Depends(require_client),
) -> PushTokenCreated:
    row = await service.register_push_token(
        session, client.id, payload.token, payload.platform, payload.app_version
    )
    await session.commit()
    return PushTokenCreated(push_token_id=row.id)


@router.delete("/auth/push-tokens/{push_token_id}",
               status_code=status.HTTP_204_NO_CONTENT)
async def delete_push_token(
    push_token_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    client: Client = Depends(require_client),
) -> Response:
    await service.revoke_push_token(session, client.id, push_token_id)
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
