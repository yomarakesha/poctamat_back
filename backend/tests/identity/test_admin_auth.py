from sqlalchemy import select

from app.core.security import hash_secret
from app.modules.identity.models import AdminUser, Client, Role


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


async def test_blocked_client_cannot_refresh(client, session):
    from app.modules.identity.service import peek_otp

    phone = "+99362123456"
    await client.post("/api/v1/auth/otp/request", json={"phone": phone})
    issued = await client.post("/api/v1/auth/otp/verify",
                               json={"phone": phone, "code": await peek_otp(phone)})
    assert issued.status_code == 200

    blocked = await session.scalar(select(Client).where(Client.phone == phone))
    blocked.is_blocked = True
    await session.commit()

    # The refresh token outlives the access token by days, so blocking has to bite
    # here as well or the session survives the block.
    response = await client.post(
        "/api/v1/auth/refresh",
        json={"refresh_token": issued.json()["refresh_token"]},
    )
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "CLIENT_BLOCKED"


async def test_deactivated_admin_cannot_refresh(client, session):
    await seed_admin(session)
    login = await client.post("/api/v1/admin/auth/login",
                              json={"login": "admin_ivanov", "password": "secret123"})
    assert login.status_code == 200

    admin = await session.scalar(select(AdminUser).where(AdminUser.login == "admin_ivanov"))
    admin.is_active = False
    await session.commit()

    response = await client.post(
        "/api/v1/admin/auth/refresh",
        json={"refresh_token": login.json()["refresh_token"]},
    )
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "ADMIN_INACTIVE"
