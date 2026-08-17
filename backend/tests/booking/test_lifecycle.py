import pytest

from app.core.errors import AppError, ErrorCode
from app.modules.booking.models import BookingStatus, CELL_HELD_STATUSES
from app.modules.booking.service import (
    ALLOWED_TRANSITIONS,
    assert_transition,
    can_transition,
)


def test_the_happy_path_is_walkable():
    path = [
        BookingStatus.PENDING_PAYMENT, BookingStatus.PAID,
        BookingStatus.AWAITING_DEPOSIT, BookingStatus.AWAITING_PICKUP,
        BookingStatus.COMPLETED,
    ]
    for current, target in zip(path, path[1:]):
        assert can_transition(current, target) is True


def test_a_collected_booking_is_final():
    assert ALLOWED_TRANSITIONS[BookingStatus.COMPLETED] == frozenset()
    assert ALLOWED_TRANSITIONS[BookingStatus.CANCELLED] == frozenset()


def test_a_deposited_parcel_can_no_longer_be_cancelled():
    # Cancelling would free a cell with someone's parcel still inside it.
    assert can_transition(BookingStatus.AWAITING_PICKUP, BookingStatus.CANCELLED) is False
    assert can_transition(BookingStatus.AWAITING_DEPOSIT, BookingStatus.CANCELLED) is True


def test_no_transition_skips_the_deposit():
    assert can_transition(BookingStatus.PAID, BookingStatus.COMPLETED) is False


def test_every_status_is_reachable_and_every_target_is_a_known_status():
    for current, targets in ALLOWED_TRANSITIONS.items():
        assert isinstance(current, BookingStatus)
        for target in targets:
            assert target in ALLOWED_TRANSITIONS


def test_leaving_a_holding_status_for_a_free_one_is_what_frees_the_cell():
    assert BookingStatus.AWAITING_PICKUP in CELL_HELD_STATUSES
    assert BookingStatus.COMPLETED not in CELL_HELD_STATUSES


def test_assert_transition_raises_a_409():
    with pytest.raises(AppError) as caught:
        assert_transition(BookingStatus.COMPLETED, BookingStatus.CANCELLED)
    assert caught.value.code == ErrorCode.BOOKING_INVALID_STATE
    assert caught.value.status_code == 409
