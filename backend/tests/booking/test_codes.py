import uuid

from sqlalchemy import select

from app.core.config import get_settings
from app.core.security import hash_pin
from app.modules.booking.codes import find_code, issue_codes, reissue_code
from app.modules.booking.models import AccessCode, Booking, BookingStatus, CodePurpose


def _booking() -> Booking:
    return Booking(
        client_id=uuid.uuid4(), postamat_id=uuid.uuid4(), cell_id=uuid.uuid4(),
        cell_type_id=uuid.uuid4(), duration_hours=24, amount_minor=1800,
        status=BookingStatus.PENDING_PAYMENT, recipient_phone="+99362123456",
    )


def test_three_codes_are_issued_one_per_purpose():
    booking = _booking()
    plaintext = issue_codes(booking)
    assert set(plaintext) == {CodePurpose.DEPOSIT, CodePurpose.COURIER, CodePurpose.PICKUP}
    assert len(booking.codes) == 3


def test_codes_are_five_digits():
    plaintext = issue_codes(_booking())
    for code in plaintext.values():
        assert len(code) == get_settings().pin_length
        assert code.isdigit()


def test_the_plaintext_is_never_stored():
    booking = _booking()
    plaintext = issue_codes(booking)
    stored = {code.code_hash for code in booking.codes}
    assert not (stored & set(plaintext.values()))
    assert {hash_pin(code) for code in plaintext.values()} == stored


def test_reissue_replaces_only_that_purpose():
    booking = _booking()
    first = issue_codes(booking)
    replacement = reissue_code(booking, CodePurpose.COURIER)

    assert replacement != first[CodePurpose.COURIER]
    by_purpose = {code.purpose: code.code_hash for code in booking.codes}
    assert by_purpose[CodePurpose.COURIER] == hash_pin(replacement)
    assert by_purpose[CodePurpose.DEPOSIT] == hash_pin(first[CodePurpose.DEPOSIT])
    assert len(booking.codes) == 3


async def test_find_code_matches_by_digest_within_one_postamat(session):
    booking = _booking()
    plaintext = issue_codes(booking)
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
    plaintext = issue_codes(booking)
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
    plaintext = issue_codes(booking)
    session.add(booking)
    await session.flush()

    booking.status = BookingStatus.CANCELLED
    await session.flush()

    assert await find_code(
        session, booking.postamat_id, plaintext[CodePurpose.PICKUP]
    ) is None
