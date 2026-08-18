import uuid

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_session
from app.core.deps import require_permission
from app.core.errors import AppError, ErrorCode
from app.core.idempotency import require_idempotency_key
from app.core.pagination import PageParams, page_params, paginate_page
from app.modules.audit.models import Source
from app.modules.audit.service import record
from app.modules.identity.models import AdminUser
from app.modules.staff import service
from app.modules.staff.schemas import (
    AdminUserCreate,
    AdminUserOut,
    AdminUserPage,
    AdminUserPatch,
    TemporaryPassword,
)

router = APIRouter(prefix="/admin/users", tags=["admin-users"])


async def _user_or_404(session: AsyncSession, user_id: uuid.UUID) -> AdminUser:
    user = await session.get(AdminUser, user_id)
    if user is None:
        raise AppError(ErrorCode.NOT_FOUND, "No such user.", 404)
    # The role is served from a joined load on a fresh read, but an instance
    # already in the identity map may not carry it, and a lazy load inside async
    # code raises rather than loading.
    await session.refresh(user)
    return user


@router.get("", response_model=AdminUserPage)
async def list_users(
    params: PageParams = Depends(page_params),
    session: AsyncSession = Depends(get_session),
    _: AdminUser = Depends(require_permission("users.read")),
) -> AdminUserPage:
    stmt = select(AdminUser).order_by(AdminUser.login)
    rows, meta = await paginate_page(session, stmt, params)
    return AdminUserPage(items=[service.admin_out(row) for row in rows],
                         pagination=meta)


@router.post("", response_model=AdminUserOut, status_code=201,
             dependencies=[Depends(require_idempotency_key)])
async def create_user(
    payload: AdminUserCreate,
    session: AsyncSession = Depends(get_session),
    actor: AdminUser = Depends(require_permission("users.write")),
) -> AdminUserOut:
    user = await service.create_user(
        session, login=payload.login, password=payload.password,
        role_id=payload.role_id, full_name=payload.full_name,
        postamat_ids=payload.postamat_ids,
    )
    await record(
        session, event="admin_user.created", source=Source.ADMIN,
        actor=actor.login, message=f"Создана учётная запись {user.login}.",
        details={"user_id": str(user.id), "role_id": str(user.role_id)},
    )
    await session.commit()
    return service.admin_out(user)


@router.patch("/{user_id}", response_model=AdminUserOut)
async def update_user(
    user_id: uuid.UUID,
    payload: AdminUserPatch,
    session: AsyncSession = Depends(get_session),
    actor: AdminUser = Depends(require_permission("users.write")),
) -> AdminUserOut:
    user = await _user_or_404(session, user_id)
    changes = payload.model_dump(exclude_unset=True)
    await service.update_user(session, user, actor, changes)
    await record(
        session, event="admin_user.updated", source=Source.ADMIN,
        actor=actor.login, message=f"Изменена учётная запись {user.login}.",
        details={"user_id": str(user.id), "fields": sorted(changes)},
    )
    await session.commit()
    return service.admin_out(user)


@router.post("/{user_id}/reset-password", response_model=TemporaryPassword,
             dependencies=[Depends(require_idempotency_key)])
async def reset_password(
    user_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    actor: AdminUser = Depends(require_permission("users.write")),
) -> TemporaryPassword:
    user = await _user_or_404(session, user_id)
    password, expires_at = await service.reset_password(session, user)
    # The password itself never reaches the journal: the entry records that a
    # reset happened and who did it, which is what an audit needs.
    await record(
        session, event="admin_user.password_reset", source=Source.ADMIN,
        actor=actor.login, message=f"Сброшен пароль {user.login}.",
        details={"user_id": str(user.id)},
    )
    await session.commit()
    return TemporaryPassword(temporary_password=password, expires_at=expires_at)
