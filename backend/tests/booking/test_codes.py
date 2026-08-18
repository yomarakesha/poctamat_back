import uuid

from sqlalchemy import select

from app.core.config import get_settings
from app.core.security import hash_pin
from app.modules.booking.codes import find_code, issue_code
from app.modules.booking.models import AccessCode, Booking, BookingStatus, CodePurpose


def _booking() -> Booking:
    return Booking(
        client_id=uuid.uuid4(), postamat_id=uuid.uuid4(), cell_id=uuid.uuid4(),
        cell_type_id=uuid.uuid4(), duration_hours=24, amount_minor=1800,
        status=BookingStatus.PENDING_PAYMENT, recipient_phone="+99362123456",
    )


def _issue_both(booking: Booking) -> dict[CodePurpose, str]:
    return {purpose: issue_code(booking, purpose) for purpose in CodePurpose}


def test_there_are_two_grants_and_no_courier_one():
    # The courier gets the deposit code — that is what the deposit code is — so
    # a third purpose would only be a second live PIN in somebody's hands.
    assert set(CodePurpose) == {CodePurpose.DEPOSIT, CodePurpose.PICKUP}


def test_a_code_is_five_digits():
    code = issue_code(_booking(), CodePurpose.DEPOSIT)
    assert len(code) == get_settings().pin_length
    assert code.isdigit()


def test_the_plaintext_is_never_stored():
    booking = _booking()
    plaintext = _issue_both(booking)
    stored = {code.code_hash for code in booking.codes}
    assert not (stored & set(plaintext.values()))
    assert {hash_pin(code) for code in plaintext.values()} == stored


def test_reissuing_replaces_only_that_purpose():
    booking = _booking()
    first = _issue_both(booking)
    replacement = issue_code(booking, CodePurpose.PICKUP)

    assert replacement != first[CodePurpose.PICKUP]
    by_purpose = {code.purpose: code.code_hash for code in booking.codes}
    assert by_purpose[CodePurpose.PICKUP] == hash_pin(replacement)
    assert by_purpose[CodePurpose.DEPOSIT] == hash_pin(first[CodePurpose.DEPOSIT])
    # Replaced, not added: the old digest is gone, so a forwarded code stops
    # working the moment a new one is minted.
    assert len(booking.codes) == 2


async def test_find_code_matches_by_digest_within_one_postamat(session):
    booking = _booking()
    plaintext = _issue_both(booking)
    session.add(booking)
    await session.flush()

    found = await find_code(session, booking.postamat_id, plaintext[CodePurpose.PICKUP])
    assert found is not None
    assert found.purpose == CodePurpose.PICKUP

    # The same digits at a different cabinet are not this booking's code.
    assert await find_code(session, uuid.uuid4(), plaintext[CodePurpose.PICKUP]) is None


async def test_find_code_ignores_used_codes(session):
    from app.core.db import utcnow

    booking = _booking()
    plaintext = _issue_both(booking)
    session.add(booking)
    await session.flush()

    used = await session.scalar(
        select(AccessCode).where(
            AccessCode.code_hash == hash_pin(plaintext[CodePurpose.DEPOSIT])
        )
    )
    used.used_at = utcnow()
    await session.flush()

    assert await find_code(
        session, booking.postamat_id, plaintext[CodePurpose.DEPOSIT]
    ) is None


async def test_find_code_ignores_codes_of_a_finished_booking(session):
    booking = _booking()
    plaintext = _issue_both(booking)
    session.add(booking)
    await session.flush()

    booking.status = BookingStatus.CANCELLED
    await session.flush()

    assert await find_code(
        session, booking.postamat_id, plaintext[CodePurpose.PICKUP]
    ) is None
