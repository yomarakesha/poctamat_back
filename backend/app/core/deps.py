import uuid

import jwt
from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_session
from app.core.errors import AppError, ErrorCode
from app.core.security import decode_token
from app.modules.identity.models import AdminUser, Client


def _bearer(request: Request) -> str:
    header = request.headers.get("Authorization", "")
    if not header.startswith("Bearer "):
        raise AppError(ErrorCode.TOKEN_INVALID, "Missing bearer token.", 401)
    return header.removeprefix("Bearer ")


def _claims(request: Request) -> dict:
    try:
        return decode_token(_bearer(request))
    except jwt.ExpiredSignatureError:
        raise AppError(ErrorCode.TOKEN_EXPIRED, "Access token expired.", 401) from None
    except jwt.PyJWTError:
        raise AppError(ErrorCode.TOKEN_INVALID, "Access token invalid.", 401) from None


async def require_client(
    request: Request, session: AsyncSession = Depends(get_session)
) -> Client:
    claims = _claims(request)
    if claims.get("typ") != "client":
        raise AppError(ErrorCode.TOKEN_INVALID, "Not a client token.", 401)
    client = await session.get(Client, uuid.UUID(claims["sub"]))
    if client is None:
        raise AppError(ErrorCode.TOKEN_INVALID, "Unknown client.", 401)
    if client.is_blocked:
        raise AppError(ErrorCode.CLIENT_BLOCKED, "Client is blocked.", 403)
    return client


async def require_admin(
    request: Request, session: AsyncSession = Depends(get_session)
) -> AdminUser:
    claims = _claims(request)
    if claims.get("typ") != "admin":
        raise AppError(ErrorCode.TOKEN_INVALID, "Not an admin token.", 401)
    admin = await session.get(AdminUser, uuid.UUID(claims["sub"]))
    if admin is None or not admin.is_active:
        raise AppError(ErrorCode.ADMIN_ACCOUNT_BLOCKED, "Account is inactive.", 403)
    return admin


def require_permission(code: str):
    async def dependency(admin: AdminUser = Depends(require_admin)) -> AdminUser:
        if code not in (admin.role.permissions or []):
            raise AppError(
                ErrorCode.FORBIDDEN, f"Permission {code} is required.", 403,
                details={"required": code},
            )
        return admin

    return dependency
