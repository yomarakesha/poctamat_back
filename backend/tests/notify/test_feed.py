import uuid

from sqlalchemy import select

from app.modules.notify.models import Notification, NotificationChannel, NotificationKind


async def _seed(session, client_id, count):
    for index in range(count):
        session.add(Notification(
            client_id=client_id, kind=NotificationKind.BOOKING_EXPIRED,
            channel=NotificationChannel.IN_APP, title=f"t{index}", body=f"b{index}",
        ))
    await session.commit()


async def test_the_feed_shows_only_this_clients_notifications(
    client, session, booking_client, client_token
):
    await _seed(session, booking_client.id, 2)
    await _seed(session, uuid.uuid4(), 3)

    response = await client.get("/api/v1/notifications",
                                headers={"Authorization": f"Bearer {client_token}"})
    assert response.status_code == 200
    assert response.json()["pagination"]["total"] == 2


async def test_marking_one_read_leaves_the_others(
    client, session, booking_client, client_token
):
    await _seed(session, booking_client.id, 2)
    headers = {"Authorization": f"Bearer {client_token}"}
    listed = await client.get("/api/v1/notifications?unread=true", headers=headers)
    first = listed.json()["items"][0]["id"]

    marked = await client.post(f"/api/v1/notifications/{first}/read", headers=headers)
    assert marked.status_code == 204

    unread = await client.get("/api/v1/notifications?unread=true", headers=headers)
    assert first not in [item["id"] for item in unread.json()["items"]]
    assert len(unread.json()["items"]) == 1


async def test_read_all_empties_the_unread_list(
    client, session, booking_client, client_token
):
    await _seed(session, booking_client.id, 3)
    headers = {"Authorization": f"Bearer {client_token}"}

    await client.post("/api/v1/notifications/read-all", headers=headers)
    unread = await client.get("/api/v1/notifications?unread=true", headers=headers)
    assert unread.json()["items"] == []
    everything = await client.get("/api/v1/notifications", headers=headers)
    assert len(everything.json()["items"]) == 3


async def test_read_all_leaves_another_clients_notifications_alone(
    client, session, booking_client, client_token
):
    other = uuid.uuid4()
    await _seed(session, booking_client.id, 1)
    await _seed(session, other, 2)

    await client.post("/api/v1/notifications/read-all",
                      headers={"Authorization": f"Bearer {client_token}"})

    rows = await session.scalars(
        select(Notification).where(Notification.client_id == other)
    )
    assert all(row.is_read is False for row in rows)


async def test_another_clients_notification_cannot_be_marked_read(
    client, session, booking_client, client_token
):
    other = uuid.uuid4()
    await _seed(session, other, 1)

    row = await session.scalar(
        select(Notification).where(Notification.client_id == other)
    )
    response = await client.post(f"/api/v1/notifications/{row.id}/read",
                                 headers={"Authorization": f"Bearer {client_token}"})
    assert response.status_code == 404


async def test_the_feed_needs_a_client_token(client):
    assert (await client.get("/api/v1/notifications")).status_code == 401
