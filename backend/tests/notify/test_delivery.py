from sqlalchemy import select

from app.modules.catalog.models import Cell
from app.modules.notify.models import Notification
from app.modules.notify.sms import get_sms_provider
from tests.helpers import set_tariffs


async def _last_notification(session, phone):
    return await session.scalar(
        select(Notification).where(Notification.phone == phone)
        .order_by(Notification.created_at.desc())
    )


async def test_the_login_code_is_sent_by_sms(client):
    from app.modules.identity.service import peek_otp

    phone = "+99362123456"
    response = await client.post("/api/v1/auth/otp/request", json={"phone": phone})
    assert response.status_code == 200

    code = await peek_otp(response.json()["request_id"])
    outbox = get_sms_provider().outbox
    assert outbox[-1].phone == phone
    assert code in outbox[-1].text
    # The response still refuses to carry the code itself.
    assert code not in response.text


async def test_the_login_sms_follows_the_requested_language(client):
    russian = await client.post("/api/v1/auth/otp/request",
                                json={"phone": "+99362123401"},
                                headers={"Accept-Language": "ru"})
    assert russian.status_code == 200
    ru_text = get_sms_provider().outbox[-1].text

    turkmen = await client.post("/api/v1/auth/otp/request",
                                json={"phone": "+99362123402"},
                                headers={"Accept-Language": "tk"})
    assert turkmen.status_code == 200
    tk_text = get_sms_provider().outbox[-1].text

    assert ru_text != tk_text


async def _ready(client, session, admin_token, city, cell_type, postamat):
    session.add(Cell(postamat_id=postamat.id, cell_type_id=cell_type.id, number=1,
                     board=1, output=1))
    await session.commit()
    await set_tariffs(client, admin_token, city, cell_type,
                      [(hours, 1800) for hours in (12, 24, 48)])


async def test_the_deposit_code_goes_to_the_courier_by_sms(
    client, session, book, admin_token, city, cell_type, postamat
):
    await _ready(client, session, admin_token, city, cell_type, postamat)
    created = await book({
        "postamat_id": str(postamat.id), "cell_type_id": str(cell_type.id),
        "duration_hours": 24, "recipient_phone": "+99365000001",
        "deposited_by": "courier", "courier_phone": "+99366000002",
    })
    assert created.status_code == 201

    last = get_sms_provider().outbox[-1]
    assert last.phone == "+99366000002"
    # The sender sees the digits too — they read them out when the SMS does not
    # arrive — but only in this one response.
    assert created.json()["deposit_code"] in last.text


async def test_rotating_kills_the_old_deposit_code(
    client, session, book, post_action, admin_token, city, cell_type, postamat
):
    import re

    from app.modules.booking.codes import find_code

    await _ready(client, session, admin_token, city, cell_type, postamat)
    created = await book({
        "postamat_id": str(postamat.id), "cell_type_id": str(cell_type.id),
        "duration_hours": 24, "recipient_phone": "+99365000001",
        "deposited_by": "courier", "courier_phone": "+99366000002",
    })
    original = created.json()["deposit_code"]

    rotated = await post_action(created.json()["id"], "deposit-code/rotate")
    assert rotated.status_code == 200
    assert rotated.json()["resend_after"] == 60

    texted = re.search(r"\d{5}", get_sms_provider().outbox[-1].text).group()
    assert texted == rotated.json()["code"] != original
    # A code that was forwarded to the wrong person stops working the moment the
    # sender asks for another.
    assert await find_code(session, postamat.id, original) is None
    assert await find_code(session, postamat.id, texted) is not None


async def test_rotating_twice_in_a_row_is_refused(
    client, session, book, post_action, admin_token, city, cell_type, postamat
):
    await _ready(client, session, admin_token, city, cell_type, postamat)
    created = await book({
        "postamat_id": str(postamat.id), "cell_type_id": str(cell_type.id),
        "duration_hours": 24, "recipient_phone": "+99365000001",
        "deposited_by": "courier", "courier_phone": "+99366000002",
    })

    await post_action(created.json()["id"], "deposit-code/rotate")
    again = await post_action(created.json()["id"], "deposit-code/rotate")
    assert again.status_code == 429
    assert again.json()["error"]["code"] == "CODE_RESEND_TOO_SOON"


async def test_rotating_to_a_new_number_moves_the_courier(
    client, session, book, post_action, admin_token, city, cell_type, postamat
):
    await _ready(client, session, admin_token, city, cell_type, postamat)
    created = await book({
        "postamat_id": str(postamat.id), "cell_type_id": str(cell_type.id),
        "duration_hours": 24, "recipient_phone": "+99365000001",
        "deposited_by": "courier", "courier_phone": "+99366000002",
    })

    rotated = await post_action(created.json()["id"], "deposit-code/rotate",
                                {"phone": "+99366000003"})
    assert rotated.status_code == 200
    assert get_sms_provider().outbox[-1].phone == "+99366000003"


async def test_the_pickup_code_cannot_be_rotated_before_the_parcel_is_in(
    client, session, book, post_action, admin_token, city, cell_type, postamat
):
    await _ready(client, session, admin_token, city, cell_type, postamat)
    created = await book({
        "postamat_id": str(postamat.id), "cell_type_id": str(cell_type.id),
        "duration_hours": 24, "recipient_phone": "+99365000001",
    })

    response = await post_action(created.json()["id"], "pickup-code/rotate")
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "PICKUP_CODE_NOT_ISSUED_YET"


async def test_a_delivery_report_marks_the_sms_delivered(client, session):
    phone = "+99362123498"
    await client.post("/api/v1/auth/otp/request", json={"phone": phone})
    row = await _last_notification(session, phone)
    assert row.provider_message_id is not None
    assert row.delivered_at is None

    report = await client.post(
        "/api/v1/webhooks/sms/log",
        json={"message_id": row.provider_message_id, "status": "delivered"},
    )
    assert report.status_code == 200

    updated = await _last_notification(session, phone)
    assert updated.delivery_status == "delivered"
    assert updated.delivered_at is not None


async def test_a_failed_delivery_is_recorded_without_a_retry(client, session):
    phone = "+99362123497"
    await client.post("/api/v1/auth/otp/request", json={"phone": phone})
    row = await _last_notification(session, phone)

    report = await client.post(
        "/api/v1/webhooks/sms/log",
        json={"message_id": row.provider_message_id, "status": "undelivered",
              "error": "handset unreachable"},
    )
    assert report.status_code == 200

    updated = await _last_notification(session, phone)
    assert updated.delivery_status == "undelivered"
    assert updated.delivered_at is None
    assert updated.error == "handset unreachable"


async def test_a_report_for_an_unknown_message_id_is_still_accepted(client):
    # A retried or stale callback must not become an error the gateway retries
    # forever; there is simply nothing here to update.
    response = await client.post(
        "/api/v1/webhooks/sms/log",
        json={"message_id": "log-does-not-exist", "status": "delivered"},
    )
    assert response.status_code == 200


async def test_an_unknown_sms_provider_is_refused(client):
    response = await client.post(
        "/api/v1/webhooks/sms/carrier-pigeon",
        json={"message_id": "x", "status": "delivered"},
    )
    assert response.status_code == 404


async def test_the_signed_provider_rejects_an_unsigned_report(client):
    # post_tm is not the active provider in tests, but its signature check
    # runs the same way in isolation as it would in production.
    from app.modules.notify.sms import PostTmSmsProvider

    provider = PostTmSmsProvider(token="unused")
    body = b'{"message_id": "abc", "status": "delivered"}'
    try:
        provider.parse_delivery_report({"x-webhook-signature": "nope"}, body)
        assert False, "expected a signature failure"
    except ValueError:
        pass
