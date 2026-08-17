from app.core.errors import AppError, ErrorCode
from app.modules.booking.models import BookingStatus

# Written as data rather than as if-branches so the whole machine can be read at
# once and tested exhaustively. Payment moves pending_payment -> paid (Plan 2b);
# the kiosk moves awaiting_deposit -> awaiting_pickup -> completed (Plan 3).
ALLOWED_TRANSITIONS: dict[BookingStatus, frozenset[BookingStatus]] = {
    BookingStatus.PENDING_PAYMENT: frozenset(
        {BookingStatus.PAID, BookingStatus.CANCELLED}
    ),
    BookingStatus.PAID: frozenset(
        {BookingStatus.AWAITING_DEPOSIT, BookingStatus.CANCELLED}
    ),
    BookingStatus.AWAITING_DEPOSIT: frozenset(
        {BookingStatus.AWAITING_PICKUP, BookingStatus.CANCELLED}
    ),
    # No cancellation from here on: the parcel is inside, and freeing the cell
    # would hand someone else a door with a stranger's parcel behind it.
    BookingStatus.AWAITING_PICKUP: frozenset({BookingStatus.COMPLETED}),
    BookingStatus.COMPLETED: frozenset(),
    BookingStatus.CANCELLED: frozenset(),
}


def can_transition(current: BookingStatus, target: BookingStatus) -> bool:
    return target in ALLOWED_TRANSITIONS[current]


def assert_transition(current: BookingStatus, target: BookingStatus) -> None:
    if not can_transition(current, target):
        raise AppError(
            ErrorCode.BOOKING_INVALID_STATE,
            f"A booking in state {current.value} cannot become {target.value}.",
            409, details={"from": current.value, "to": target.value},
        )
