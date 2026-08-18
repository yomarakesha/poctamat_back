from sqlalchemy import func, select

from app.modules.notify.models import NotificationSettings

PATH = "/api/v1/me/notification-settings"


async def test_a_client_with_no_row_gets_the_defaults(client, client_token):
    response = await client.get(PATH,
                                headers={"Authorization": f"Bearer {client_token}"})
    assert response.status_code == 200
    body = response.json()
    assert body["push_parcel_deposited"] is True
    assert body["push_marketing"] is False
    # Read-only and always true: the pickup code travels by SMS and that channel
    # cannot be switched off.
    assert body["sms_always_on"] is True


async def test_reading_twice_does_not_create_two_rows(
    client, session, booking_client, client_token
):
    headers = {"Authorization": f"Bearer {client_token}"}
    await client.get(PATH, headers=headers)
    await client.get(PATH, headers=headers)

    rows = await session.scalar(
        select(func.count()).select_from(NotificationSettings)
        .where(NotificationSettings.client_id == booking_client.id)
    )
    assert rows == 1


async def test_patching_one_flag_leaves_the_rest(client, client_token):
    headers = {"Authorization": f"Bearer {client_token}"}
    response = await client.patch(PATH, headers=headers,
                                  json={"push_marketing": True})
    assert response.status_code == 200
    body = response.json()
    assert body["push_marketing"] is True
    assert body["push_cell_opened"] is True

    again = await client.get(PATH, headers=headers)
    assert again.json()["push_marketing"] is True


async def test_sms_always_on_cannot_be_switched_off(client, client_token):
    headers = {"Authorization": f"Bearer {client_token}"}
    # The field is not in the patch model, so it is ignored rather than stored,
    # and the answer still says true.
    response = await client.patch(PATH, headers=headers,
                                  json={"sms_always_on": False})
    assert response.status_code == 200
    assert response.json()["sms_always_on"] is True


async def test_settings_need_a_client_token(client):
    assert (await client.get(PATH)).status_code == 401
