import re
import uuid

from app.core.config import get_settings
from app.modules.booking.models import Booking, BookingStatus
from app.modules.catalog.models import Cell
from app.modules.notify.sms import get_sms_provider
from tests.helpers import set_tariffs


async def _ready(client, session, admin_token, city, cell_type, postamat, cells=2):
    session.add_all([
        Cell(postamat_id=postamat.id, cell_type_id=cell_type.id, number=n,
             board=1, output=n)
        for n in range(1, cells + 1)
    ])
    await session.commit()
    await set_tariffs(client, admin_token, city, cell_type,
                      [(hours, 1800) for hours in (12, 24, 48)])


def _body(postamat, cell_type):
    return {"postamat_id": str(postamat.id), "cell_type_id": str(cell_type.id),
            "duration_hours": 24, "recipient_phone": "+99365000001"}


async def _deposit(session, booking_id, postamat):
    """Put the parcel in, the way the kiosk will once it exists."""
    from app.modules.booking import service

    booking = await session.get(Booking, uuid.UUID(booking_id))
    booking.status = BookingStatus.AWAITING_DEPOSIT
    await session.flush()
    _, code = await service.mark_deposited(session, booking, postamat)
    await session.commit()
    return code


async def test_the_hold_can_be_extended_up_to_the_limit(
    client, session, book, post_action, admin_token, city, cell_type, postamat
):
    await _ready(client, session, admin_token, city, cell_type, postamat)
    created = await book(_body(postamat, cell_type))
    first_deadline = created.json()["hold_expires_at"]

    extended = await post_action(created.json()["id"], "extend-hold")
    assert extended.status_code == 200
    assert extended.json()["hold_expires_at"] > first_deadline

    assert (await post_action(created.json()["id"], "extend-hold")).status_code == 200

    # The cell has been off the market long enough by now.
    exhausted = await post_action(created.json()["id"], "extend-hold")
    assert exhausted.status_code == 409
    assert exhausted.json()["error"]["code"] == "HOLD_EXTENSION_LIMIT_EXCEEDED"
    assert exhausted.json()["error"]["details"]["limit"] == (
        get_settings().hold_extensions_max
    )


async def test_a_paid_booking_has_no_hold_to_extend(
    client, session, book, post_action, admin_token, city, cell_type, postamat
):
    from app.modules.booking import service

    await _ready(client, session, admin_token, city, cell_type, postamat)
    created = await book(_body(postamat, cell_type))
    booking = await session.get(Booking, uuid.UUID(created.json()["id"]))
    await service.mark_paid(session, booking)
    await session.commit()

    response = await post_action(created.json()["id"], "extend-hold")
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "BOOKING_ALREADY_PAID"


async def test_the_pickup_code_appears_only_once_the_parcel_is_in(
    client, session, book, client_token, admin_token, city, cell_type, postamat
):
    await _ready(client, session, admin_token, city, cell_type, postamat)
    created = await book(_body(postamat, cell_type))
    headers = {"Authorization": f"Bearer {client_token}"}

    before = await client.get(f"/api/v1/bookings/{created.json()['id']}",
                              headers=headers)
    assert before.json()["pickup_code_sent_at"] is None

    code = await _deposit(session, created.json()["id"], postamat)
    after = await client.get(f"/api/v1/bookings/{created.json()['id']}",
                             headers=headers)
    assert after.json()["pickup_code_sent_at"].endswith("Z")
    # The digits are not in the payload — only the fact that they went out.
    assert code not in after.text


async def test_rotating_the_pickup_code_returns_it_and_kills_the_old_one(
    client, session, book, post_action, admin_token, city, cell_type, postamat
):
    from app.modules.booking.codes import find_code

    await _ready(client, session, admin_token, city, cell_type, postamat)
    created = await book(_body(postamat, cell_type))
    original = await _deposit(session, created.json()["id"], postamat)

    rotated = await post_action(created.json()["id"], "pickup-code/rotate")
    assert rotated.status_code == 200
    assert len(rotated.json()["code"]) == 5

    texted = re.search(r"\d{5}", get_sms_provider().outbox[-1].text).group()
    assert texted == rotated.json()["code"] != original
    assert await find_code(session, postamat.id, original) is None


async def test_transferring_moves_the_grant_to_another_number(
    client, session, book, post_action, client_token, admin_token, city, cell_type,
    postamat,
):
    from app.modules.booking.codes import find_code

    await _ready(client, session, admin_token, city, cell_type, postamat)
    created = await book(_body(postamat, cell_type))
    original = await _deposit(session, created.json()["id"], postamat)

    transferred = await post_action(created.json()["id"], "pickup-code/transfer",
                                    {"new_phone": "+99365000009",
                                     "new_name": "Новый"})
    assert transferred.status_code == 200
    assert transferred.json()["recipient_phone"] == "+99365000009"
    assert get_sms_provider().outbox[-1].phone == "+99365000009"
    # Forwarding a code over a messenger is invisible; this is the audited
    # version of it, and the old code stops working immediately.
    assert await find_code(session, postamat.id, original) is None


async def test_transferring_to_the_same_number_is_refused(
    client, session, book, post_action, admin_token, city, cell_type, postamat
):
    await _ready(client, session, admin_token, city, cell_type, postamat)
    created = await book(_body(postamat, cell_type))
    await _deposit(session, created.json()["id"], postamat)

    response = await post_action(created.json()["id"], "pickup-code/transfer",
                                 {"new_phone": "+99365000001"})
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "TRANSFER_PHONE_SAME"


async def test_transferring_before_the_deposit_is_refused(
    client, session, book, post_action, admin_token, city, cell_type, postamat
):
    await _ready(client, session, admin_token, city, cell_type, postamat)
    created = await book(_body(postamat, cell_type))

    response = await post_action(created.json()["id"], "pickup-code/transfer",
                                 {"new_phone": "+99365000009"})
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "PICKUP_CODE_NOT_ISSUED_YET"
