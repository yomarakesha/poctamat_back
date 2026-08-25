import json
import logging
import uuid

import httpx
import pytest

import app.modules.notify.service as notify_service
from app.modules.identity.models import PushToken
from app.modules.notify.models import NotificationChannel, NotificationKind
from app.modules.notify.push import (
    FCM_TOKEN_URL,
    FcmPushProvider,
    close_push_provider,
    get_push_provider,
)
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


async def test_a_partly_delivered_push_logs_the_devices_that_failed(
    session, monkeypatch, caplog
):
    # `error` staying unset must not mean the failure disappears: the rejected
    # token is the only signal that a device is gone and should be revoked.
    client_id = uuid.uuid4()
    await _device(session, client_id, "token-good")
    await _device(session, client_id, "token-bad")
    monkeypatch.setattr(
        notify_service, "get_push_provider",
        lambda: _FlakyPushProvider(bad_tokens={"token-bad"}),
    )

    with caplog.at_level(logging.WARNING, logger="app.notify"):
        row = await notify(
            session, client_id=client_id, phone=None,
            kind=NotificationKind.BOOKING_PAID, language="ru",
            channel=NotificationChannel.PUSH, booking_id=None, cell_number=7,
        )

    assert row.error is None
    assert "1 of 2 device(s) failed" in caplog.text
    assert "device gone" in caplog.text


# --- FcmPushProvider ---------------------------------------------------------
#
# The assertion is a real RS256 signature, which needs `cryptography` at
# runtime; signing it is pyjwt's contract, not this module's, so it is stubbed
# out and what gets tested here is the exchange and the send around it.


def _fcm(monkeypatch, handler) -> tuple[FcmPushProvider, httpx.MockTransport]:
    provider = FcmPushProvider(
        project_id="postamat-app",
        client_email="svc@postamat.iam.gserviceaccount.com",
        private_key="-----BEGIN PRIVATE KEY-----unused-----END PRIVATE KEY-----",
    )
    monkeypatch.setattr(provider, "_assertion", lambda: "signed-jwt")
    return provider, httpx.MockTransport(handler)


async def test_fcm_mints_a_bearer_and_posts_the_message(monkeypatch):
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == FCM_TOKEN_URL:
            seen["token_body"] = request.content.decode()
            return httpx.Response(200, json={"access_token": "ya29.a",
                                             "expires_in": 3600})
        seen["url"] = str(request.url)
        seen["auth"] = request.headers["Authorization"]
        seen["message"] = json.loads(request.read())
        return httpx.Response(200, json={"name": "projects/postamat-app/messages/1"})

    provider, transport = _fcm(monkeypatch, handler)
    message_id = await provider.send(
        "device-token", "Ячейка оплачена", "Ячейка 7 ждёт вас",
        {"booking_id": "b-1"}, _transport=transport,
    )

    assert "assertion=signed-jwt" in seen["token_body"]
    assert seen["url"] == (
        "https://fcm.googleapis.com/v1/projects/postamat-app/messages:send"
    )
    assert seen["auth"] == "Bearer ya29.a"
    assert seen["message"] == {
        "message": {
            "token": "device-token",
            "notification": {"title": "Ячейка оплачена", "body": "Ячейка 7 ждёт вас"},
            "data": {"booking_id": "b-1"},
        }
    }
    assert message_id == "projects/postamat-app/messages/1"


async def test_fcm_mints_one_bearer_for_a_burst_of_pushes(monkeypatch):
    # A single worker tick fans one reminder out to every device on an
    # account. Minting an access token per device would triple the round
    # trips for no gain — the token is good for an hour.
    mints = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal mints
        if str(request.url) == FCM_TOKEN_URL:
            mints += 1
            return httpx.Response(200, json={"access_token": "ya29.a",
                                             "expires_in": 3600})
        return httpx.Response(200, json={"name": "m"})

    provider, transport = _fcm(monkeypatch, handler)
    for token in ("device-1", "device-2", "device-3"):
        await provider.send(token, "t", "b", _transport=transport)

    assert mints == 1


async def test_fcm_raises_on_a_rejected_token_so_the_caller_can_record_it(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == FCM_TOKEN_URL:
            return httpx.Response(200, json={"access_token": "ya29.a",
                                             "expires_in": 3600})
        return httpx.Response(404, json={"error": {"status": "UNREGISTERED"}})

    provider, transport = _fcm(monkeypatch, handler)
    with pytest.raises(httpx.HTTPStatusError) as raised:
        await provider.send("dead-token", "t", "b", _transport=transport)
    assert raised.value.response.status_code == 404


async def test_fcm_reuses_one_client_and_closing_releases_it(monkeypatch):
    provider, _ = _fcm(monkeypatch, lambda request: httpx.Response(200, json={}))

    first = provider._shared_client()
    assert provider._shared_client() is first

    await provider.aclose()
    assert first.is_closed
    assert provider._client is None
    # Closing twice is what a shutdown after a failed startup does.
    await provider.aclose()


async def test_closing_the_push_provider_is_safe_when_none_was_built():
    # The logging provider holds nothing to close, and a process that never
    # sent a push has no provider at all. Shutdown must not care.
    await close_push_provider()
    assert get_push_provider() is not None
