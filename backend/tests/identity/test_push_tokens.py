import uuid

from sqlalchemy import select

from app.modules.identity.models import PushToken

PATH = "/api/v1/auth/push-tokens"


async def _register(client, token, value="device-token-1", platform="android"):
    return await client.post(PATH, headers={"Authorization": f"Bearer {token}"},
                             json={"token": value, "platform": platform,
                                   "app_version": "1.0.0+42"})


async def test_registering_a_token_answers_its_id(client, client_token):
    response = await _register(client, client_token)
    assert response.status_code == 201
    uuid.UUID(response.json()["push_token_id"])


async def test_registering_the_same_token_twice_reuses_the_row(client, client_token):
    first = await _register(client, client_token)
    second = await _register(client, client_token)
    # The app re-registers on every launch and the platform reissues tokens on
    # its own schedule, so a repeat is the normal case, not a second row.
    assert first.json()["push_token_id"] == second.json()["push_token_id"]


async def test_an_unknown_platform_is_rejected(client, client_token):
    response = await _register(client, client_token, platform="symbian")
    assert response.status_code == 422


async def test_deleting_a_token_answers_204(client, client_token):
    created = await _register(client, client_token)
    deleted = await client.delete(
        f"{PATH}/{created.json()['push_token_id']}",
        headers={"Authorization": f"Bearer {client_token}"},
    )
    assert deleted.status_code == 204


async def test_deleting_twice_is_a_404(client, client_token):
    created = await _register(client, client_token)
    path = f"{PATH}/{created.json()['push_token_id']}"
    headers = {"Authorization": f"Bearer {client_token}"}
    await client.delete(path, headers=headers)
    assert (await client.delete(path, headers=headers)).status_code == 404


async def test_another_clients_token_is_not_found(client, session, client_token):
    from app.core.security import create_access_token
    from app.modules.identity.models import Client

    other = Client(phone="+99361000777")
    session.add(other)
    await session.commit()
    created = await _register(client, create_access_token("client", other.id))

    response = await client.delete(
        f"{PATH}/{created.json()['push_token_id']}",
        headers={"Authorization": f"Bearer {client_token}"},
    )
    assert response.status_code == 404


async def test_registering_needs_a_client_token(client):
    response = await client.post(PATH, json={"token": "t", "platform": "ios"})
    assert response.status_code == 401


async def test_logout_revokes_the_push_token_it_names(client, session, client_token):
    await _register(client, client_token)

    logged_out = await client.post(
        "/api/v1/auth/logout", json={"push_token": "device-token-1"},
        headers={"Authorization": f"Bearer {client_token}"},
    )
    assert logged_out.status_code == 204

    row = await session.scalar(
        select(PushToken).where(PushToken.token == "device-token-1")
    )
    await session.refresh(row)
    assert row.revoked_at is not None
