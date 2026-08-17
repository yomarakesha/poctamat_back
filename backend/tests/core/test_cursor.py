import uuid
from datetime import timedelta

import pytest
from sqlalchemy import select

from app.core.db import utcnow
from app.core.errors import AppError
from app.core.pagination import CursorParams, decode_cursor, encode_cursor, paginate_cursor
from app.modules.notify.models import Notification, NotificationChannel, NotificationKind


async def _feed(session, count, client_id=None):
    client_id = client_id or uuid.uuid4()
    base = utcnow()
    for index in range(count):
        session.add(Notification(
            client_id=client_id, kind=NotificationKind.BOOKING_EXPIRED,
            channel=NotificationChannel.IN_APP, title=f"t{index}", body=f"b{index}",
            created_at=base + timedelta(seconds=index),
        ))
    await session.commit()
    return client_id


def _stmt(client_id):
    return select(Notification).where(Notification.client_id == client_id)


async def test_a_page_reports_more_and_hands_back_a_cursor(session):
    client_id = await _feed(session, 5)
    rows, meta = await paginate_cursor(
        session, _stmt(client_id), CursorParams(limit=3), Notification.created_at
    )
    assert len(rows) == 3
    assert meta.has_more is True
    assert meta.next_cursor


async def test_the_cursor_returns_the_rest_exactly_once(session):
    client_id = await _feed(session, 5)
    first, meta = await paginate_cursor(
        session, _stmt(client_id), CursorParams(limit=3), Notification.created_at
    )
    second, meta2 = await paginate_cursor(
        session, _stmt(client_id), CursorParams(limit=3, cursor=meta.next_cursor),
        Notification.created_at,
    )
    assert len(second) == 2
    assert meta2.has_more is False
    assert meta2.next_cursor is None
    # No overlap and nothing lost: five rows, seen once each.
    assert len({row.id for row in first} & {row.id for row in second}) == 0
    assert len({row.id for row in first} | {row.id for row in second}) == 5


async def test_a_row_inserted_between_pages_cannot_duplicate_an_item(session):
    client_id = await _feed(session, 5)
    first, meta = await paginate_cursor(
        session, _stmt(client_id), CursorParams(limit=3), Notification.created_at
    )
    # Newest-first ordering means an insert lands on page one, which the reader
    # has already passed. The cursor must not shift the rest of the feed.
    session.add(Notification(
        client_id=client_id, kind=NotificationKind.BOOKING_OVERDUE,
        channel=NotificationChannel.IN_APP, title="new", body="new",
        created_at=utcnow() + timedelta(hours=1),
    ))
    await session.commit()

    second, _ = await paginate_cursor(
        session, _stmt(client_id), CursorParams(limit=3, cursor=meta.next_cursor),
        Notification.created_at,
    )
    assert len({row.id for row in first} & {row.id for row in second}) == 0
    assert len(second) == 2


async def test_an_unparseable_cursor_is_refused(session):
    client_id = await _feed(session, 2)
    with pytest.raises(AppError) as caught:
        await paginate_cursor(
            session, _stmt(client_id), CursorParams(limit=2, cursor="not-a-cursor"),
            Notification.created_at,
        )
    assert caught.value.status_code == 422


def test_a_cursor_round_trips():
    moment = utcnow()
    row_id = uuid.uuid4()
    assert decode_cursor(encode_cursor(moment, row_id)) == (moment.isoformat(), str(row_id))
