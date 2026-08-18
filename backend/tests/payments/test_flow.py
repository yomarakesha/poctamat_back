import json
import uuid
from datetime import timedelta

from sqlalchemy import select

from app.core.config import get_settings
from app.core.db import utcnow
from app.modules.booking.models import Booking, BookingStatus
from app.modules.catalog.models import Cell
from app.modules.payments.models import Payment, PaymentStatus
from app.modules.payments.provider import sign_payload


async def _booked(client, session, book, admin_token, city, cell_type, postamat):
    session.add(Cell(postamat_id=postamat.id, cell_type_id=cell_type.id, number=1,
                     board=1, output=1))
    await session.commit()
    await client.put("/api/v1/admin/tariffs",
                     headers={"Authorization": f"Bearer {admin_token}"},
                     json={"entries": [
                         {"city_id": str(city.id), "cell_type_id": str(cell_type.id),
                          "duration_hours": hours, "amount_minor": 1800}
                         for hours in (12, 24, 48)
                     ]})
    created = await book({"postamat_id": str(postamat.id),
                          "cell_type_id": str(cell_type.id), "duration_hours": 24,
                          "recipient_phone": "+99365000001"})
    assert created.status_code == 201
    return created.json()


async def _start(client, headers, booking_id, key, bank_code="halk"):
    return await client.post(
        f"/api/v1/bookings/{booking_id}/payments",
        headers=headers | {"Idempotency-Key": key},
        json={"bank_code": bank_code, "return_url": "postamat://payment/result"},
    )


def _webhook(payment_id, succeeded=True):
    body = json.dumps({
        "payment_id": str(payment_id), "provider_payment_id": f"mock-{payment_id}",
        "status": "succeeded" if succeeded else "failed",
        "reason": None if succeeded else "declined",
    }).encode()
    signature = sign_payload(body, get_settings().payment_webhook_secret)
    return body, {"X-Signature": signature, "Content-Type": "application/json"}


async def test_paying_moves_the_booking_and_clears_the_hold(
    client, session, book, client_token, admin_token, city, cell_type, postamat
):
    booking = await _booked(client, session, book, admin_token, city, cell_type, postamat)
    headers = {"Authorization": f"Bearer {client_token}"}

    started = await _start(client, headers, booking["id"], "pay-1")
    assert started.status_code == 201
    assert started.json()["amount"] == {"amount_minor": 1800, "currency": "TMT"}
    assert started.json()["bank_code"] == "halk"
    assert started.json()["redirect_url"].startswith("https://")
    assert started.json()["expires_at"].endswith("Z")

    body, hook_headers = _webhook(started.json()["id"])
    delivered = await client.post("/api/v1/webhooks/payments/mock", content=body,
                                  headers=hook_headers)
    assert delivered.status_code == 200

    detail = await client.get(f"/api/v1/bookings/{booking['id']}", headers=headers)
    assert detail.json()["status"] == BookingStatus.AWAITING_DEPOSIT
    assert detail.json()["hold_expires_at"] is None

    timeline = await client.get(f"/api/v1/bookings/{booking['id']}/timeline",
                                headers=headers)
    happened = {item["step"] for item in timeline.json()["items"]
                if item["occurred_at"]}
    assert happened == {"booked", "paid"}


async def test_the_same_webhook_twice_settles_once(
    client, session, book, client_token, admin_token, city, cell_type, postamat
):
    booking = await _booked(client, session, book, admin_token, city, cell_type, postamat)
    headers = {"Authorization": f"Bearer {client_token}"}
    started = await _start(client, headers, booking["id"], "pay-2")
    body, hook_headers = _webhook(started.json()["id"])

    first = await client.post("/api/v1/webhooks/payments/mock", content=body,
                              headers=hook_headers)
    second = await client.post("/api/v1/webhooks/payments/mock", content=body,
                               headers=hook_headers)
    assert (first.status_code, second.status_code) == (200, 200)

    timeline = await client.get(f"/api/v1/bookings/{booking['id']}/timeline",
                                headers=headers)
    # Paid once, not twice: the second delivery must not walk the booking
    # through payment again.
    happened = [item["step"] for item in timeline.json()["items"]
                if item["occurred_at"]]
    assert happened == ["booked", "paid"]


async def test_an_unsigned_webhook_is_refused(client):
    body = json.dumps({"payment_id": str(uuid.uuid4()), "status": "succeeded"}).encode()
    response = await client.post("/api/v1/webhooks/payments/mock", content=body,
                                 headers={"X-Signature": "nope"})
    assert response.status_code == 400


async def test_a_declined_payment_releases_the_cell(
    client, session, book, client_token, admin_token, city, cell_type, postamat
):
    booking = await _booked(client, session, book, admin_token, city, cell_type, postamat)
    headers = {"Authorization": f"Bearer {client_token}"}
    started = await _start(client, headers, booking["id"], "pay-3")
    body, hook_headers = _webhook(started.json()["id"], succeeded=False)
    await client.post("/api/v1/webhooks/payments/mock", content=body,
                      headers=hook_headers)

    detail = await client.get(f"/api/v1/bookings/{booking['id']}", headers=headers)
    # payment_failed, not cancelled: the bank refused, the customer did not
    # change their mind, and the app draws a different screen for each.
    assert detail.json()["status"] == BookingStatus.PAYMENT_FAILED

    availability = await client.get(f"/api/v1/postamats/{postamat.id}/availability")
    assert availability.json()["items"][0]["free"] == 1


async def test_a_payment_can_be_read_back_by_its_owner_only(
    client, session, book, client_token, admin_token, city, cell_type, postamat
):
    from app.core.security import create_access_token
    from app.modules.identity.models import Client

    booking = await _booked(client, session, book, admin_token, city, cell_type, postamat)
    headers = {"Authorization": f"Bearer {client_token}"}
    started = await _start(client, headers, booking["id"], "pay-5")
    payment_id = started.json()["id"]

    mine = await client.get(f"/api/v1/payments/{payment_id}", headers=headers)
    assert mine.json()["status"] == PaymentStatus.PENDING

    stranger = Client(phone="+99361000055", last_name="Чужой", first_name="Чужой")
    session.add(stranger)
    await session.commit()
    await session.refresh(stranger)
    theirs = await client.get(
        f"/api/v1/payments/{payment_id}",
        headers={"Authorization": f"Bearer {create_access_token('client', stranger.id)}"},
    )
    assert theirs.status_code == 404


async def test_paying_a_booking_twice_is_refused(
    client, session, book, client_token, admin_token, city, cell_type, postamat
):
    booking = await _booked(client, session, book, admin_token, city, cell_type, postamat)
    headers = {"Authorization": f"Bearer {client_token}"}
    started = await _start(client, headers, booking["id"], "pay-6")
    body, hook_headers = _webhook(started.json()["id"])
    await client.post("/api/v1/webhooks/payments/mock", content=body,
                      headers=hook_headers)

    again = await _start(client, headers, booking["id"], "pay-7")
    assert again.status_code == 409
    assert again.json()["error"]["code"] == "BOOKING_ALREADY_PAID"


async def test_money_arriving_after_the_hold_expired_is_flagged_for_a_human(
    client, session, book, client_token, admin_token, city, cell_type, postamat
):
    # No refunds (Ruling Q1), so this is money taken for a booking that no
    # longer exists. It must never disappear quietly.
    from app.modules.audit.models import AuditEntry, Severity
    from app.workers.holds import release_expired_holds

    booking = await _booked(client, session, book, admin_token, city, cell_type, postamat)
    headers = {"Authorization": f"Bearer {client_token}"}
    started = await _start(client, headers, booking["id"], "pay-4")

    row = await session.get(Booking, uuid.UUID(booking["id"]))
    row.hold_expires_at = utcnow() - timedelta(minutes=1)
    await session.commit()
    await release_expired_holds(session)

    body, hook_headers = _webhook(started.json()["id"])
    response = await client.post("/api/v1/webhooks/payments/mock", content=body,
                                 headers=hook_headers)
    assert response.status_code == 200

    payment = await session.scalar(select(Payment))
    assert payment.status == PaymentStatus.SUCCEEDED

    entry = await session.scalar(
        select(AuditEntry).where(AuditEntry.event == "payment.needs_attention")
    )
    assert entry is not None
    assert entry.severity == Severity.WARNING
    assert entry.details["amount_minor"] == 1800


async def test_a_second_session_for_one_booking_is_refused(
    client, session, book, client_token, admin_token, city, cell_type, postamat
):
    booking = await _booked(client, session, book, admin_token, city, cell_type, postamat)
    headers = {"Authorization": f"Bearer {client_token}"}
    assert (await _start(client, headers, booking["id"], "pay-a")).status_code == 201

    # Two live sessions are two ways to take the same money; the app polls the
    # one it already has.
    again = await _start(client, headers, booking["id"], "pay-b")
    assert again.status_code == 409
    assert again.json()["error"]["code"] == "PAYMENT_ALREADY_EXISTS"


async def test_an_unknown_bank_is_refused_with_its_own_code(
    client, session, book, client_token, admin_token, city, cell_type, postamat
):
    booking = await _booked(client, session, book, admin_token, city, cell_type, postamat)
    headers = {"Authorization": f"Bearer {client_token}"}

    response = await _start(client, headers, booking["id"], "pay-c", bank_code="tbc")
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "BANK_NOT_SUPPORTED"


async def test_a_pending_payment_can_be_abandoned(
    client, session, book, client_token, admin_token, city, cell_type, postamat
):
    booking = await _booked(client, session, book, admin_token, city, cell_type, postamat)
    headers = {"Authorization": f"Bearer {client_token}"}
    started = await _start(client, headers, booking["id"], "pay-d")

    cancelled = await client.post(
        f"/api/v1/payments/{started.json()['id']}/cancel",
        headers=headers | {"Idempotency-Key": "cancel-1"},
    )
    assert cancelled.status_code == 200
    assert cancelled.json()["status"] == PaymentStatus.CANCELLED

    # The booking is free to try again with another bank.
    retried = await _start(client, headers, booking["id"], "pay-e", bank_code="rysgal")
    assert retried.status_code == 201


async def test_a_settled_payment_cannot_be_cancelled(
    client, session, book, client_token, admin_token, city, cell_type, postamat
):
    booking = await _booked(client, session, book, admin_token, city, cell_type, postamat)
    headers = {"Authorization": f"Bearer {client_token}"}
    started = await _start(client, headers, booking["id"], "pay-f")
    body, hook_headers = _webhook(started.json()["id"])
    await client.post("/api/v1/webhooks/payments/mock", content=body,
                      headers=hook_headers)

    response = await client.post(
        f"/api/v1/payments/{started.json()['id']}/cancel",
        headers=headers | {"Idempotency-Key": "cancel-2"},
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "PAYMENT_NOT_CANCELLABLE"


async def test_the_callback_answers_a_bank_named_path(
    client, session, book, client_token, admin_token, city, cell_type, postamat
):
    # The acquirer that eventually ships will call the path named after its bank
    # code; `mock` stays callable beside it until then.
    booking = await _booked(client, session, book, admin_token, city, cell_type, postamat)
    headers = {"Authorization": f"Bearer {client_token}"}
    started = await _start(client, headers, booking["id"], "pay-g")
    body, hook_headers = _webhook(started.json()["id"])

    delivered = await client.post("/api/v1/webhooks/payments/halk", content=body,
                                  headers=hook_headers)
    assert delivered.status_code == 200


async def test_a_callback_for_the_wrong_amount_is_refused(
    client, session, book, client_token, admin_token, city, cell_type, postamat
):
    import json as _json

    booking = await _booked(client, session, book, admin_token, city, cell_type, postamat)
    headers = {"Authorization": f"Bearer {client_token}"}
    started = await _start(client, headers, booking["id"], "pay-h")

    payload = _json.dumps({
        "payment_id": started.json()["id"],
        "provider_payment_id": "mock-mismatch",
        "status": "succeeded",
        "amount_minor": 1,
    }).encode()
    signature = sign_payload(payload, get_settings().payment_webhook_secret)
    response = await client.post(
        "/api/v1/webhooks/payments/mock", content=payload,
        headers={"X-Signature": signature, "Content-Type": "application/json"},
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "AMOUNT_MISMATCH"


async def test_an_unsigned_callback_names_the_signature(client):
    body = json.dumps({"payment_id": str(uuid.uuid4()), "status": "succeeded"}).encode()
    response = await client.post("/api/v1/webhooks/payments/mock", content=body,
                                 headers={"X-Signature": "nope"})
    assert response.json()["error"]["code"] == "CALLBACK_SIGNATURE_INVALID"
