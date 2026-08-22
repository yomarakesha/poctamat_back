### Task 12: Admin authentication and permissions

**Files:**
- Create: `backend/app/api/admin/auth.py`, `backend/app/core/deps.py`, `backend/tests/identity/test_admin_auth.py`
- Modify: `backend/app/main.py`

**Interfaces:**
- Produces: `require_client` and `require_admin` dependencies, `require_permission(code)` dependency factory.
- Endpoints: `POST /api/v1/admin/auth/login`, `POST /api/v1/admin/auth/refresh`, `POST /api/v1/admin/auth/logout`, `GET /api/v1/admin/auth/me`, `GET /api/v1/admin/roles`.

`POST /admin/auth/2fa` is deliberately absent: the flow is not drawn yet and its request body depends on whether it is TOTP, SMS or email.

- [ ] **Step 1: Write the failing test**

```python
from app.core.security import hash_secret
from app.modules.identity.models import AdminUser, Role


async def seed_admin(session) -> None:
    role = Role(code="admin", name="Administrator",
                permissions=["postamats.write", "cells.open"])
    session.add(role)
    await session.flush()
    session.add(AdminUser(login="admin_ivanov", full_name="Иванов И.И.",
                          password_hash=hash_secret("secret123"), role_id=role.id))
    await session.commit()


async def test_login_returns_tokens_and_permissions(client, session):
    await seed_admin(session)
    response = await client.post("/api/v1/admin/auth/login",
                                 json={"login": "admin_ivanov", "password": "secret123"})
    assert response.status_code == 200
    token = response.json()["access_token"]

    me = await client.get("/api/v1/admin/auth/me",
                          headers={"Authorization": f"Bearer {token}"})
    assert me.json()["permissions"] == ["postamats.write", "cells.open"]


async def test_wrong_password_is_401(client, session):
    await seed_admin(session)
    response = await client.post("/api/v1/admin/auth/login",
                                 json={"login": "admin_ivanov", "password": "nope"})
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "CREDENTIALS_INVALID"


async def test_missing_permission_is_403(client, session):
    await seed_admin(session)
    login = await client.post("/api/v1/admin/auth/login",
                              json={"login": "admin_ivanov", "password": "secret123"})
    token = login.json()["access_token"]
    response = await client.get("/api/v1/admin/roles",
                                headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "PERMISSION_DENIED"
```

- [ ] **Step 2: Run it and confirm it fails**

- [ ] **Step 3: Write `backend/app/core/deps.py`**

```python
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
        raise AppError(ErrorCode.ADMIN_INACTIVE, "Account is inactive.", 403)
    return admin


def require_permission(code: str):
    async def dependency(admin: AdminUser = Depends(require_admin)) -> AdminUser:
        if code not in (admin.role.permissions or []):
            raise AppError(
                ErrorCode.PERMISSION_DENIED, f"Permission {code} is required.", 403,
                details={"required": code},
            )
        return admin

    return dependency
```

- [ ] **Step 4: Write `backend/app/api/admin/auth.py`**

```python
from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_session
from app.core.deps import require_admin, require_permission
from app.core.errors import AppError, ErrorCode
from app.core.security import verify_secret
from app.modules.identity import service
from app.modules.identity.models import AdminUser, Role

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
    if admin is None or not verify_secret(payload.password, admin.password_hash):
        raise AppError(ErrorCode.CREDENTIALS_INVALID, "Login or password is wrong.", 401)
    if not admin.is_active:
        raise AppError(ErrorCode.ADMIN_INACTIVE, "Account is inactive.", 403)

    access, refresh = await service.issue_token_pair(session, "admin", admin.id)
    await session.commit()
    return AdminTokens(access_token=access, refresh_token=refresh)


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
```

- [ ] **Step 5: Register the router and run the tests**

```bash
./.venv/Scripts/python.exe -m pytest tests/identity -v
```

- [ ] **Step 6: Commit**

```bash
git add <only the files this task created or modified, listed explicitly> && git commit -m "feat: admin login with permission-based authorisation"
```

---

