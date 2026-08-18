"""Admin accounts as an operator manages them.

Kept apart from `identity`, which is about proving who somebody is. This module
is about who is allowed to exist and what they may touch — a different question,
answered by a different screen.
"""

import secrets
import string
import uuid
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import utcnow
from app.core.errors import AppError, ErrorCode
from app.core.security import hash_secret
from app.core.types import utc_isoformat
from app.modules.identity.models import AdminUser, Role, RefreshToken
from app.modules.staff.schemas import AdminUserOut, RoleOut

# How long a reset password is good for. Long enough to telephone somebody, not
# long enough to sit unused in a chat history.
TEMPORARY_PASSWORD_HOURS = 24
TEMPORARY_PASSWORD_LENGTH = 16


def role_out(role: Role) -> RoleOut:
    return RoleOut(id=role.id, code=role.code, name=role.name,
                   permissions=role.permissions or [])


def admin_out(admin: AdminUser) -> AdminUserOut:
    return AdminUserOut(
        id=admin.id, login=admin.login, full_name=admin.full_name,
        role=role_out(admin.role), permissions=admin.role.permissions or [],
        postamat_ids=[uuid.UUID(str(one)) for one in (admin.postamat_ids or [])],
        is_active=admin.is_active,
        last_login_at=(
            utc_isoformat(admin.last_login_at) if admin.last_login_at else None
        ),
    )


def temporary_password() -> str:
    alphabet = string.ascii_letters + string.digits
    # secrets, not random: this password opens the panel that opens every door.
    return "".join(secrets.choice(alphabet) for _ in range(TEMPORARY_PASSWORD_LENGTH))


async def _role_or_422(session: AsyncSession, role_id: uuid.UUID) -> Role:
    role = await session.get(Role, role_id)
    if role is None:
        raise AppError(ErrorCode.ROLE_NOT_FOUND, "No such role.", 422,
                       details={"role_id": str(role_id)})
    return role


async def create_user(
    session: AsyncSession,
    *,
    login: str,
    password: str,
    role_id: uuid.UUID,
    full_name: str,
    postamat_ids: list[uuid.UUID],
) -> AdminUser:
    if await session.scalar(select(AdminUser).where(AdminUser.login == login)):
        raise AppError(ErrorCode.USER_LOGIN_TAKEN, "That login is taken.", 409,
                       details={"login": login})
    role = await _role_or_422(session, role_id)

    user = AdminUser(
        login=login, full_name=full_name, password_hash=hash_secret(password),
        role_id=role.id, postamat_ids=[str(one) for one in postamat_ids],
        # Created by somebody else, so the password is known to two people until
        # the owner changes it.
        must_change_password=True,
    )
    session.add(user)
    await session.flush()
    await session.refresh(user)
    return user


async def update_user(
    session: AsyncSession, user: AdminUser, actor: AdminUser, changes: dict
) -> AdminUser:
    locking_out = changes.get("is_active") is False or (
        "role_id" in changes and changes["role_id"] != user.role_id
    )
    if user.id == actor.id and locking_out:
        # An administrator who deactivates or demotes their own account locks
        # the fleet's owner out of their own panel, and nobody else may be able
        # to put it back.
        raise AppError(ErrorCode.CANNOT_MODIFY_SELF,
                       "You cannot change your own access.", 403)

    if changes.get("role_id") is not None:
        await _role_or_422(session, changes["role_id"])
    if changes.get("postamat_ids") is not None:
        changes["postamat_ids"] = [str(one) for one in changes["postamat_ids"]]

    for field, value in changes.items():
        setattr(user, field, value)
    await session.flush()
    await session.refresh(user)
    return user


async def reset_password(
    session: AsyncSession, user: AdminUser
) -> tuple[str, str]:
    """Issue a one-time password and kill every session the account has.

    A reset happens because somebody lost control of the account or of the
    password. Leaving live refresh tokens behind would make the reset cosmetic.
    """
    password = temporary_password()
    user.password_hash = hash_secret(password)
    user.must_change_password = True

    now = utcnow()
    stale = await session.scalars(
        select(RefreshToken).where(
            RefreshToken.subject_type == "admin",
            RefreshToken.subject_id == user.id,
            RefreshToken.revoked_at.is_(None),
        )
    )
    for token in stale:
        token.revoked_at = now
    await session.flush()
    return password, utc_isoformat(now + timedelta(hours=TEMPORARY_PASSWORD_HOURS))
