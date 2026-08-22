import uuid

import app.modules.notify.service as notify_service
from app.modules.identity.models import PushToken
from app.modules.notify.models import NotificationChannel, NotificationKind
from app.modules.notify.push import get_push_provider
from app.modules.notify.service import notify


class _FlakyPushProvider:
    """Fails for the named tokens, succeeds for every other."""

    name = "flaky"

    def __init__(self, bad_tokens: set[str]):
        self.bad_tokens = bad_tokens

    async def send(self, token, title, body, data=None):
        if token in self.bad_tokens:
            raise RuntimeError("device gone")
        return "ok"


async def _device(session, client_id, token="device-token-1", revoked=False):
    row = PushToken(
        client_id=client_id, token=token, platform="android", app_version="1.0",
    )
    if revoked:
        from app.core.db import utcnow
        row.revoked_at = utcnow()
    session.add(row)
    await session.commit()
    return row


async def test_a_push_goes_to_every_active_device(session):
    client_id = uuid.uuid4()
    await _device(session, client_id, "token-a")
    await _device(session, client_id, "token-b")

    row = await notify(
        session, client_id=client_id, phone=None,
        kind=NotificationKind.BOOKING_PAID, language="ru",
        channel=NotificationChannel.PUSH, booking_id=None, cell_number=7,
    )
    await session.commit()

    outbox = get_push_provider().outbox
    assert {sent.token for sent in outbox} == {"token-a", "token-b"}
    assert row.sent_at is not None
    assert row.error is None


async def test_a_revoked_device_is_never_sent_to(session):
    client_id = uuid.uuid4()
    await _device(session, client_id, "token-live")
    await _device(session, client_id, "token-dead", revoked=True)

    await notify(
        session, client_id=client_id, phone=None,
        kind=NotificationKind.BOOKING_PAID, language="ru",
        channel=NotificationChannel.PUSH, booking_id=None, cell_number=7,
    )

    outbox = get_push_provider().outbox
    assert [sent.token for sent in outbox] == ["token-live"]


async def test_a_client_with_no_registered_device_gets_an_error_not_an_exception(session):
    row = await notify(
        session, client_id=uuid.uuid4(), phone=None,
        kind=NotificationKind.BOOKING_PAID, language="ru",
        channel=NotificationChannel.PUSH, booking_id=None, cell_number=7,
    )
    assert row.error == "no push tokens"
    assert row.sent_at is None


async def test_push_with_no_client_id_is_recorded_as_undeliverable(session):
    row = await notify(
        session, client_id=None, phone=None,
        kind=NotificationKind.BOOKING_PAID, language="ru",
        channel=NotificationChannel.PUSH, booking_id=None, cell_number=7,
    )
    assert row.error == "no client"


async def test_one_failed_device_does_not_mark_a_partly_delivered_push_as_an_error(
    session, monkeypatch
):
    # A client with two devices where only one delivery failed still has the
    # notification. `error` must stay unset so a caller deciding whether to
    # retry does not re-push to the device that already succeeded.
    client_id = uuid.uuid4()
    await _device(session, client_id, "token-good")
    await _device(session, client_id, "token-bad")
    monkeypatch.setattr(
        notify_service, "get_push_provider",
        lambda: _FlakyPushProvider(bad_tokens={"token-bad"}),
    )

    row = await notify(
        session, client_id=client_id, phone=None,
        kind=NotificationKind.BOOKING_PAID, language="ru",
        channel=NotificationChannel.PUSH, booking_id=None, cell_number=7,
    )

    assert row.sent_at is not None
    assert row.error is None


async def test_every_device_failing_is_recorded_as_an_error(session, monkeypatch):
    client_id = uuid.uuid4()
    await _device(session, client_id, "token-bad-1")
    await _device(session, client_id, "token-bad-2")
    monkeypatch.setattr(
        notify_service, "get_push_provider",
        lambda: _FlakyPushProvider(bad_tokens={"token-bad-1", "token-bad-2"}),
    )

    row = await notify(
        session, client_id=client_id, phone=None,
        kind=NotificationKind.BOOKING_PAID, language="ru",
        channel=NotificationChannel.PUSH, booking_id=None, cell_number=7,
    )

    assert row.sent_at is None
    assert row.error is not None
