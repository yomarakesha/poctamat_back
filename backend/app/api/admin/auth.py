from fastapi import APIRouter, Depends, Response, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_session
from app.core.deps import require_admin, require_permission
from app.core.errors import AppError, ErrorCode
from app.core.security import verify_secret
from app.modules.identity import service
from app.modules.identity.models import AdminUser, Role
from app.modules.identity.schemas import RefreshRequest

router = APIRouter(prefix="/admin", tags=["admin-auth"])


class LoginRequest(BaseModel):
    login: str
    password: str


class AdminTokens(BaseModel):
    access_token: str
    refresh_token: str


class AdminProfile(BaseModel):
    login: str
    full_name: str
    role: str
    permissions: list[str]


class RoleOut(BaseModel):
    code: str
    name: str
    permissions: list[str]


@router.post("/auth/login", response_model=AdminTokens)
async def login(
    payload: LoginRequest, session: AsyncSession = Depends(get_session)
) -> AdminTokens:
    admin = await session.scalar(select(AdminUser).where(AdminUser.login == payload.login))
    # An unknown login and a wrong password answer identically, so the endpoint
    # cannot be used to enumerate who works here.
    if admin is None or not verify_secret(payload.password, admin.password_hash):
        raise AppError(ErrorCode.ADMIN_CREDENTIALS_INVALID, "Login or password is wrong.", 401)
    if not admin.is_active:
        raise AppError(ErrorCode.ADMIN_ACCOUNT_BLOCKED, "Account is inactive.", 403)

    access, refresh = await service.issue_token_pair(session, "admin", admin.id)
    await session.commit()
    return AdminTokens(access_token=access, refresh_token=refresh)


@router.post("/auth/refresh", response_model=AdminTokens)
async def refresh(
    payload: RefreshRequest, session: AsyncSession = Depends(get_session)
) -> AdminTokens:
    access, rotated = await service.rotate_refresh_token(
        session, payload.refresh_token, "admin"
    )
    await session.commit()
    return AdminTokens(access_token=access, refresh_token=rotated)


@router.post("/auth/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(
    payload: RefreshRequest, session: AsyncSession = Depends(get_session)
) -> Response:
    await service.revoke_refresh_token(session, payload.refresh_token, "admin")
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/auth/me", response_model=AdminProfile)
async def me(admin: AdminUser = Depends(require_admin)) -> AdminProfile:
    return AdminProfile(
        login=admin.login,
        full_name=admin.full_name,
        role=admin.role.code,
        permissions=admin.role.permissions or [],
    )


@router.get("/roles", response_model=list[RoleOut])
async def list_roles(
    session: AsyncSession = Depends(get_session),
    _: AdminUser = Depends(require_permission("roles.read")),
) -> list[RoleOut]:
    rows = await session.scalars(select(Role).order_by(Role.code))
    return [RoleOut(code=r.code, name=r.name, permissions=r.permissions or []) for r in rows]
