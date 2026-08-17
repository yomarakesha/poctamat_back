import json

import httpx

from app.modules.notify.models import NotificationChannel, NotificationKind
from app.modules.notify.service import notify
from app.modules.notify.sms import PostTmSmsProvider, get_sms_provider


async def test_an_sms_notification_is_recorded_and_sent(session):
    row = await notify(
        session, client_id=None, phone="+99362123456",
        kind=NotificationKind.OTP, language="ru",
        channel=NotificationChannel.SMS, code="12345",
    )
    await session.commit()

    sent = get_sms_provider()
    assert row.channel == NotificationChannel.SMS
    assert row.sent_at is not None
    assert sent.outbox[-1].phone == "+99362123456"
    # The code reaches the phone and nothing else: the stored body must not be
    # where a one-time code is archived in the clear.
    assert "12345" in sent.outbox[-1].text
    assert "12345" not in row.body


async def test_the_language_picks_the_template(session):
    turkmen = await notify(session, client_id=None, phone="+99362123456",
                           kind=NotificationKind.OTP, language="tk",
                           channel=NotificationChannel.SMS, code="12345")
    russian = await notify(session, client_id=None, phone="+99362123456",
                           kind=NotificationKind.OTP, language="ru",
                           channel=NotificationChannel.SMS, code="12345")
    assert turkmen.body != russian.body


async def test_an_unknown_language_falls_back_rather_than_raising(session):
    row = await notify(session, client_id=None, phone="+99362123456",
                       kind=NotificationKind.OTP, language="fr",
                       channel=NotificationChannel.SMS, code="12345")
    assert row.body


async def test_an_in_app_notification_is_not_sent_anywhere(session):
    before = len(get_sms_provider().outbox)
    row = await notify(session, client_id=None, phone=None,
                       kind=NotificationKind.BOOKING_EXPIRING, language="ru",
                       channel=NotificationChannel.IN_APP, cell_number=7, hours=2)
    assert row.sent_at is None
    assert len(get_sms_provider().outbox) == before


async def test_a_provider_failure_is_stored_and_does_not_raise(session, monkeypatch):
    async def broken(phone, text):
        raise RuntimeError("gateway down")

    monkeypatch.setattr(get_sms_provider(), "send", broken)
    row = await notify(session, client_id=None, phone="+99362123456",
                       kind=NotificationKind.OTP, language="ru",
                       channel=NotificationChannel.SMS, code="12345")
    assert row.sent_at is None
    assert "gateway down" in row.error


async def test_the_gateway_gets_the_number_without_a_plus():
    # post.tm takes `phone` as a number. A stored "+99362615986" sent verbatim
    # is rejected, and the login code silently never arrives.
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["json"] = json.loads(request.content)
        captured["auth"] = request.headers["Authorization"]
        captured["url"] = str(request.url)
        return httpx.Response(200, json={"id": "sms-1"})

    provider = PostTmSmsProvider(token="tok")
    message_id = await provider.send(
        "+99362615986", "Postamat: login code 12345",
        _transport=httpx.MockTransport(handler),
    )

    assert captured["json"] == {"phone": 99362615986,
                                "content": "Postamat: login code 12345"}
    assert captured["auth"] == "Bearer tok"
    assert captured["url"] == "https://sms.post.tm/api/clients/sms/create"
    assert message_id == "sms-1"


async def test_a_gateway_error_raises_so_the_caller_can_record_it():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"detail": "bad token"})

    provider = PostTmSmsProvider(token="wrong")
    try:
        await provider.send("+99362615986", "x",
                            _transport=httpx.MockTransport(handler))
    except httpx.HTTPStatusError as error:
        assert error.response.status_code == 401
    else:
        raise AssertionError("a rejected message must not look delivered")
