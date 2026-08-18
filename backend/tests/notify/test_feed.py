import uuid

from sqlalchemy import select

from app.modules.notify.models import Notification, NotificationChannel, NotificationKind

FEED = "/api/v1/me/notifications"
READ = "/api/v1/me/notifications/read"


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

    response = await client.get(FEED,
                                headers={"Authorization": f"Bearer {client_token}"})
    assert response.status_code == 200
    body = response.json()
    assert len(body["items"]) == 2
    assert body["unread_count"] == 2
    assert body["pagination"]["has_more"] is False


async def test_an_item_carries_the_contracts_field_names(
    client, session, booking_client, client_token
):
    await _seed(session, booking_client.id, 1)

    response = await client.get(FEED,
                                headers={"Authorization": f"Bearer {client_token}"})
    item = response.json()["items"][0]
    # `type` on the wire is the stored `kind`; `read_at` is a timestamp rather
    # than a boolean, because the app sorts and badges from it.
    assert item["type"] == "booking_expired"
    assert item["read_at"] is None
    assert item["created_at"].endswith("Z")


async def test_marking_one_read_leaves_the_others(
    client, session, booking_client, client_token
):
    await _seed(session, booking_client.id, 2)
    headers = {"Authorization": f"Bearer {client_token}"}
    listed = await client.get(f"{FEED}?unread_only=true", headers=headers)
    first = listed.json()["items"][0]["id"]

    marked = await client.post(READ, headers=headers,
                               json={"notification_ids": [first]})
    assert marked.status_code == 200
    assert marked.json()["unread_count"] == 1

    unread = await client.get(f"{FEED}?unread_only=true", headers=headers)
    assert first not in [item["id"] for item in unread.json()["items"]]
    assert len(unread.json()["items"]) == 1


async def test_an_empty_body_marks_everything_read(
    client, session, booking_client, client_token
):
    await _seed(session, booking_client.id, 3)
    headers = {"Authorization": f"Bearer {client_token}"}

    marked = await client.post(READ, headers=headers, json={})
    assert marked.json()["unread_count"] == 0

    unread = await client.get(f"{FEED}?unread_only=true", headers=headers)
    assert unread.json()["items"] == []
    everything = await client.get(FEED, headers=headers)
    assert len(everything.json()["items"]) == 3


async def test_reading_all_leaves_another_clients_notifications_alone(
    client, session, booking_client, client_token
):
    other = uuid.uuid4()
    await _seed(session, booking_client.id, 1)
    await _seed(session, other, 2)

    await client.post(READ, json={},
                      headers={"Authorization": f"Bearer {client_token}"})

    rows = await session.scalars(
        select(Notification).where(Notification.client_id == other)
    )
    assert all(row.read_at is None for row in rows)


async def test_another_clients_notification_cannot_be_marked_read(
    client, session, booking_client, client_token
):
    other = uuid.uuid4()
    await _seed(session, other, 1)
    row = await session.scalar(
        select(Notification).where(Notification.client_id == other)
    )

    # An id list from a client is not a licence to touch another client's row:
    # the update is scoped to the caller, so this is accepted and changes
    # nothing.
    marked = await client.post(READ, json={"notification_ids": [str(row.id)]},
                               headers={"Authorization": f"Bearer {client_token}"})
    assert marked.status_code == 200

    await session.refresh(row)
    assert row.read_at is None


async def test_the_feed_pages_by_cursor(
    client, session, booking_client, client_token
):
    await _seed(session, booking_client.id, 5)
    headers = {"Authorization": f"Bearer {client_token}"}

    first = await client.get(f"{FEED}?limit=3", headers=headers)
    page = first.json()
    assert len(page["items"]) == 3
    assert page["pagination"]["has_more"] is True

    cursor = page["pagination"]["next_cursor"]
    second = await client.get(f"{FEED}?limit=3&cursor={cursor}", headers=headers)
    rest = second.json()
    assert len(rest["items"]) == 2
    assert rest["pagination"]["has_more"] is False
    seen = [item["id"] for item in page["items"] + rest["items"]]
    assert len(set(seen)) == 5


async def test_the_feed_needs_a_client_token(client):
    assert (await client.get(FEED)).status_code == 401
