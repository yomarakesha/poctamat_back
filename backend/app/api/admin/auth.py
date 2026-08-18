from fastapi import APIRouter, Depends, Response, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.db import get_session, utcnow
from app.core.deps import require_admin, require_permission
from app.core.errors import AppError, ErrorCode
from app.core.security import verify_secret
from app.modules.identity import service
from app.modules.identity.models import AdminUser, Role
from app.modules.identity.schemas import RefreshRequest, TokenPair
from app.modules.staff import service as staff_service
from app.modules.staff.schemas import AdminUserOut, RoleOut

router = APIRouter(prefix="/admin", tags=["admin-auth"])


class LoginRequest(BaseModel):
    login: str
    password: str


class AdminLoginResult(TokenPair):
    # The panel renders its menu from the user's permissions; the server checks
    # them again on every call regardless.
    user: AdminUserOut


def _expires_in() -> int:
    return get_settings().access_token_ttl_minutes * 60


@router.post("/auth/login", response_model=AdminLoginResult)
async def login(
    payload: LoginRequest, session: AsyncSession = Depends(get_session)
) -> AdminLoginResult:
    admin = await session.scalar(select(AdminUser).where(AdminUser.login == payload.login))
    # An unknown login and a wrong password answer identically, so the endpoint
    # cannot be used to enumerate who works here.
    if admin is None or not verify_secret(payload.password, admin.password_hash):
        raise AppError(ErrorCode.ADMIN_CREDENTIALS_INVALID, "Login or password is wrong.", 401)
    if not admin.is_active:
        raise AppError(ErrorCode.ADMIN_ACCOUNT_BLOCKED, "Account is inactive.", 403)

    # Stamped here rather than in the token middleware: an access token minted an
    # hour ago is not a sign that anybody is at the keyboard.
    admin.last_login_at = utcnow()
    access, refresh = await service.issue_token_pair(session, "admin", admin.id)
    await session.commit()
    return AdminLoginResult(
        access_token=access, refresh_token=refresh, expires_in=_expires_in(),
        user=staff_service.admin_out(admin),
    )


@router.post("/auth/refresh", response_model=TokenPair)
async def refresh(
    payload: RefreshRequest, session: AsyncSession = Depends(get_session)
) -> TokenPair:
    access, rotated = await service.rotate_refresh_token(
        session, payload.refresh_token, "admin"
    )
    await session.commit()
    return TokenPair(access_token=access, refresh_token=rotated,
                     expires_in=_expires_in())


@router.post("/auth/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(
    payload: RefreshRequest, session: AsyncSession = Depends(get_session)
) -> Response:
    await service.revoke_refresh_token(session, payload.refresh_token, "admin")
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/auth/me", response_model=AdminUserOut)
async def me(admin: AdminUser = Depends(require_admin)) -> AdminUserOut:
    return staff_service.admin_out(admin)


@router.get("/roles", response_model=list[RoleOut])
async def list_roles(
    session: AsyncSession = Depends(get_session),
    _: AdminUser = Depends(require_permission("roles.read")),
) -> list[RoleOut]:
    rows = await session.scalars(select(Role).order_by(Role.code))
    return [staff_service.role_out(row) for row in rows]
