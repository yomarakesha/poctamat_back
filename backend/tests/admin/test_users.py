import uuid

import pytest
from sqlalchemy import select

from app.modules.identity.models import AdminUser, Role

PASSWORD = "correct-horse-battery-staple"


@pytest.fixture
async def superadmin(session):
    """An account that may manage other accounts, unlike the shared fixture."""
    from app.core.security import hash_secret

    row = Role(code="superadmin", name="Суперадмин",
               permissions=["users.read", "users.write", "roles.read"])
    session.add(row)
    await session.flush()
    admin = AdminUser(login="root_test", full_name="Главный А.А.",
                      password_hash=hash_secret(PASSWORD), role_id=row.id)
    session.add(admin)
    await session.commit()
    await session.refresh(admin)
    return admin


@pytest.fixture
def super_token(superadmin):
    from app.core.security import create_access_token

    return create_access_token("admin", superadmin.id)


def _key():
    return {"Idempotency-Key": str(uuid.uuid4())}


async def test_login_answers_the_token_pair_and_the_user(client, superadmin):
    response = await client.post("/api/v1/admin/auth/login",
                                 json={"login": "root_test", "password": PASSWORD})
    assert response.status_code == 200
    body = response.json()
    assert body["token_type"] == "Bearer"
    assert body["expires_in"] > 0
    assert body["user"]["login"] == "root_test"
    assert body["user"]["role"]["code"] == "superadmin"
    assert "users.write" in body["user"]["permissions"]
    assert body["user"]["last_login_at"].endswith("Z")


async def test_me_answers_the_contracts_user(client, super_token):
    response = await client.get("/api/v1/admin/auth/me",
                                headers={"Authorization": f"Bearer {super_token}"})
    assert response.status_code == 200
    body = response.json()
    assert body["is_active"] is True
    assert body["postamat_ids"] == []
    assert body["role"]["permissions"]


async def test_creating_a_user_returns_it_and_forces_a_password_change(
    client, session, superadmin, super_token
):
    headers = {"Authorization": f"Bearer {super_token}"} | _key()
    response = await client.post("/api/v1/admin/users", headers=headers, json={
        "login": "operator_one", "password": "another-long-password",
        "role_id": str(superadmin.role_id), "full_name": "Оператор О.О.",
    })
    assert response.status_code == 201
    assert response.json()["login"] == "operator_one"

    created = await session.scalar(
        select(AdminUser).where(AdminUser.login == "operator_one")
    )
    # Somebody else chose this password, so two people know it until the owner
    # changes it.
    assert created.must_change_password is True


async def test_a_taken_login_is_refused(client, superadmin, super_token):
    headers = {"Authorization": f"Bearer {super_token}"}
    body = {"login": "root_test", "password": "another-long-password",
            "role_id": str(superadmin.role_id), "full_name": "Двойник"}
    response = await client.post("/api/v1/admin/users", headers=headers | _key(),
                                 json=body)
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "USER_LOGIN_TAKEN"


async def test_a_short_password_is_refused(client, superadmin, super_token):
    response = await client.post(
        "/api/v1/admin/users",
        headers={"Authorization": f"Bearer {super_token}"} | _key(),
        json={"login": "weak_one", "password": "short", "full_name": "Слабый",
              "role_id": str(superadmin.role_id)},
    )
    # An admin password opens every cell in the fleet through remote open.
    assert response.status_code == 422


async def test_an_unknown_role_is_refused(client, super_token):
    response = await client.post(
        "/api/v1/admin/users",
        headers={"Authorization": f"Bearer {super_token}"} | _key(),
        json={"login": "roleless", "password": "another-long-password",
              "full_name": "Без роли", "role_id": str(uuid.uuid4())},
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "ROLE_NOT_FOUND"


async def test_deactivating_yourself_is_refused(client, superadmin, super_token):
    response = await client.patch(
        f"/api/v1/admin/users/{superadmin.id}",
        headers={"Authorization": f"Bearer {super_token}"},
        json={"is_active": False},
    )
    # Otherwise the fleet's owner locks themselves out of the panel that would
    # let them back in.
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "CANNOT_MODIFY_SELF"


async def test_renaming_yourself_is_allowed(client, superadmin, super_token):
    response = await client.patch(
        f"/api/v1/admin/users/{superadmin.id}",
        headers={"Authorization": f"Bearer {super_token}"},
        json={"full_name": "Главный Администратор"},
    )
    assert response.status_code == 200
    assert response.json()["full_name"] == "Главный Администратор"


async def test_resetting_a_password_ends_the_accounts_sessions(
    client, session, superadmin, super_token
):
    from app.modules.identity.models import Role

    role = await session.get(Role, superadmin.role_id)
    created = await client.post(
        "/api/v1/admin/users",
        headers={"Authorization": f"Bearer {super_token}"} | _key(),
        json={"login": "operator_two", "password": "another-long-password",
              "role_id": str(role.id), "full_name": "Оператор Два"},
    )
    theirs = await client.post("/api/v1/admin/auth/login",
                               json={"login": "operator_two",
                                     "password": "another-long-password"})
    assert theirs.status_code == 200

    reset = await client.post(
        f"/api/v1/admin/users/{created.json()['id']}/reset-password",
        headers={"Authorization": f"Bearer {super_token}"} | _key(),
    )
    assert reset.status_code == 200
    assert len(reset.json()["temporary_password"]) == 16
    assert reset.json()["expires_at"].endswith("Z")

    # A reset happens because control of the account was lost; leaving live
    # sessions behind would make it cosmetic.
    replayed = await client.post(
        "/api/v1/admin/auth/refresh",
        json={"refresh_token": theirs.json()["refresh_token"]},
    )
    assert replayed.status_code == 401

    old = await client.post("/api/v1/admin/auth/login",
                            json={"login": "operator_two",
                                  "password": "another-long-password"})
    assert old.status_code == 401
    fresh = await client.post("/api/v1/admin/auth/login",
                              json={"login": "operator_two",
                                    "password": reset.json()["temporary_password"]})
    assert fresh.status_code == 200


async def test_managing_users_needs_the_permission(client, admin_token):
    # The shared operator fixture has no users.* permission.
    listed = await client.get("/api/v1/admin/users",
                              headers={"Authorization": f"Bearer {admin_token}"})
    assert listed.status_code == 403
    assert listed.json()["error"]["code"] == "FORBIDDEN"


async def test_the_user_list_pages(client, session, superadmin, super_token):
    from app.core.security import hash_secret

    session.add_all([
        AdminUser(login=f"user_{n}", full_name=f"Сотрудник {n}",
                  password_hash=hash_secret(PASSWORD), role_id=superadmin.role_id)
        for n in range(3)
    ])
    await session.commit()

    response = await client.get("/api/v1/admin/users?per_page=2",
                                headers={"Authorization": f"Bearer {super_token}"})
    assert response.status_code == 200
    assert len(response.json()["items"]) == 2
    assert response.json()["pagination"]["total"] == 4
